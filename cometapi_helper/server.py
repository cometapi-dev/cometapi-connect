"""Loopback-only UI server with a per-launch session capability and no request logs."""

import hmac
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import certifi

from .common import SetupError
from .runtime import system_program_environment


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_models():
    # This is a public endpoint. A catalog refresh never sends or verifies a key.
    request = urllib.request.Request(
        "https://api.cometapi.com/api/models",
        headers={"Accept": "application/json", "User-Agent": "CometAPI-Connect/0.1"},
    )
    try:
        opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        )
        with opener.open(request, timeout=20) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("Catalog too large")
        payload = json.loads(data)
        if (
            not isinstance(payload, dict)
            or payload.get("success") is False
            or not isinstance(payload.get("data"), list)
        ):
            raise ValueError("Invalid catalog")
        models = sorted(
            {
                item["id"]
                for item in payload["data"]
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and len(item["id"]) <= 150
                and not item.get("upcoming", False)
                and item.get("model_type", "text") in ("text", "chat", "llm")
            }
        )
        if not models:
            raise ValueError("Empty catalog")
        return {
            "models": models,
            "message": "Public text-model catalog refreshed. This does not validate your key, account access, or client compatibility.",
        }
    except Exception:
        raise SetupError(
            "Could not load CometAPI's public model catalog. Check your connection, or continue with a model ID from cometapi.com/models."
        ) from None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, engine, port=0):
        self.engine = engine
        self.token = secrets.token_urlsafe(32)
        self.last_activity = time.monotonic()
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)

    @property
    def launch_url(self):
        return self.origin + "/#token=" + self.token


class Handler(BaseHTTPRequestHandler):
    server_version = "CometAPIConnect"
    sys_version = ""
    timeout = 25

    def log_message(self, *_):
        pass

    def respond(self, status, payload, content_type="application/json; charset=utf-8"):
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

    def authenticated(self):
        origin = self.headers.get("Origin")
        accepted = (
            self.valid_host()
            and (origin is None or origin == self.server.origin)
            and self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
            and hmac.compare_digest(self.headers.get("X-CometAPI-Token", ""), self.server.token)
        )
        if accepted:
            self.server.last_activity = time.monotonic()
        return accepted

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Invalid local host."})
        if self.path == "/":
            folder = Path(__file__).parent / "static"
            catalogs = {
                path.stem: json.loads(path.read_text(encoding="utf-8"))
                for path in (folder / "locales").glob("*.json")
            }
            # Escape script delimiters in translation data; translations are text, never HTML.
            encoded = (
                json.dumps(catalogs, ensure_ascii=False)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            runtime = (folder / "i18n.js").read_text(encoding="utf-8")
            scripts = (
                '<script id="translation-data" type="application/json">'
                + encoded
                + "</script><script>"
                + runtime
                + "</script>"
            )
            data = (
                (folder / "index.html")
                .read_text(encoding="utf-8")
                .replace("<!-- COMETAPI_I18N -->", scripts)
                .encode("utf-8")
            )
            return self.respond(200, data, "text/html; charset=utf-8")
        if self.path == "/favicon.ico":
            return self.respond(204, b"", "image/x-icon")
        if not self.authenticated():
            return self.respond(
                403, {"error": "This setup session is not authorized. Reopen CometAPI Connect."}
            )
        self.dispatch("GET", {})

    def do_POST(self):
        if not self.authenticated():
            return self.respond(
                403, {"error": "This setup session is not authorized. Reopen CometAPI Connect."}
            )
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.respond(415, {"error": "Expected JSON."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.respond(413, {"error": "Request size is invalid."})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected object")
        except (ValueError, UnicodeError):
            return self.respond(400, {"error": "Invalid request."})
        self.dispatch("POST", payload)

    def do_OPTIONS(self):
        self.respond(403, {"error": "Cross-origin requests are not supported."})

    def dispatch(self, method, payload):
        engine = self.server.engine
        try:
            route = (method, self.path)
            if route == ("GET", "/api/scan"):
                result = engine.scan()
            elif route == ("POST", "/api/scan"):
                result = engine.scan(payload.get("roots", []))
            elif route == ("POST", "/api/models"):
                result = fetch_models()
            elif route == ("POST", "/api/preview"):
                result = engine.preview(
                    payload.get("api_key"), payload.get("apps"), payload.get("models")
                )
            elif route == ("POST", "/api/apply"):
                result = engine.apply(payload.get("plan_id"))
            elif route == ("GET", "/api/history"):
                result = engine.history()
            elif route == ("POST", "/api/restore"):
                result = engine.restore(payload.get("transaction_id"))
            elif route == ("POST", "/api/shutdown"):
                result = {"message": "CometAPI Connect is closed. You can close this browser tab."}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                return self.respond(404, {"error": "Unknown setup action."})
            self.respond(200, result)
        except SetupError as error:
            self.respond(400, {"error": str(error)})
        except Exception:
            # Never return exception diagnostics which may contain existing secrets.
            self.respond(
                500,
                {
                    "error": "The operation could not be completed. Check access to the selected folders and try again."
                },
            )


def serve(engine, port=0, open_browser=True, announce=print):
    with LocalServer(engine, port) as server:
        stopped = threading.Event()

        def housekeeping():
            while not stopped.wait(30):
                with engine.lock:
                    expired = [
                        key
                        for key, (created, _) in engine.plans.items()
                        if time.monotonic() - created > 600
                    ]
                    for key in expired:
                        del engine.plans[key]
                if time.monotonic() - server.last_activity > 1800:
                    server.shutdown()
                    return

        threading.Thread(target=housekeeping, daemon=True).start()
        announce("CometAPI Connect is running locally. Close it using Quit in the setup window.")
        if open_browser:
            with system_program_environment():
                opened = webbrowser.open(server.launch_url)
            if not opened:
                stopped.set()
                raise SetupError(
                    "Could not open your default browser. Set a default browser in your system settings, then reopen CometAPI Connect."
                )
        else:
            announce(server.launch_url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            engine.plans.clear()
