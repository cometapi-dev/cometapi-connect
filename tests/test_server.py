import json
import threading
import urllib.error
import urllib.request

import pytest

from cometapi_helper.common import Context
from cometapi_helper.engine import Engine
from cometapi_helper.server import LocalServer, fetch_models


@pytest.fixture
def server(tmp_path):
    context = Context(home=tmp_path.resolve(), env={}, use_path=False)
    running = LocalServer(Engine(context))
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    yield running
    running.shutdown()
    running.server_close()
    thread.join(timeout=3)


def request(server, path, body=None, headers=None, token=True):
    merged = {"X-CometAPI-Token": server.token} if token else {}
    if body is not None:
        merged["Content-Type"] = "application/json"
    merged.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.origin + path, data=data, headers=merged)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, response.read(), response.headers


def test_page_headers_and_no_session_key_in_html(server):
    status, body, headers = request(server, "/", token=False)
    assert status == 200
    assert server.token.encode() not in body
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_page_embeds_offline_translations_without_relaxing_script_policy(server):
    import re

    status, body, headers = request(server, "/", token=False)
    assert status == 200
    text = body.decode("utf-8")
    encoded = re.search(
        r'<script id="translation-data" type="application/json">(.*?)</script>', text, re.S
    ).group(1)
    catalogs = json.loads(encoded)
    assert catalogs["en"]["Review your changes"] == "Review your changes"
    assert "<" not in encoded
    assert "https:" not in headers["Content-Security-Policy"]
    assert "globalThis.CometI18n" in text


@pytest.mark.parametrize(
    "headers,token",
    [
        ({}, False),
        ({"Origin": "https://evil.example"}, True),
        ({"Host": "evil.example"}, True),
        ({"Sec-Fetch-Site": "cross-site"}, True),
        ({"X-CometAPI-Token": "wrong"}, True),
    ],
)
def test_api_rejects_untrusted_requests(server, headers, token):
    status, body, _ = request(server, "/api/scan", headers=headers, token=token)
    assert status == 403
    assert b"apps" not in body


def test_preview_does_not_leak_key_and_writes_only_after_apply(server):
    key = "sk-test-not-a-real-secret"
    path = server.engine.ctx.home / ".claude/settings.json"
    status, body, _ = request(server, "/api/preview", {"api_key": key, "apps": ["claude-code"]})
    assert status == 200, body
    assert key.encode() not in body
    assert not path.exists()
    plan_id = json.loads(body)["plan_id"]
    status, body, _ = request(server, "/api/apply", {"plan_id": plan_id})
    assert status == 200, body
    assert key.encode() not in body
    assert key in path.read_text()
    transaction_id = json.loads(body)["transaction_id"]
    status, body, _ = request(server, "/api/history")
    assert status == 200 and key.encode() not in body
    status, body, _ = request(server, "/api/restore", {"transaction_id": transaction_id})
    assert status == 200, body
    assert not path.exists()


def test_invalid_body_and_routes(server):
    assert request(server, "/api/preview", ["wrong"])[0] == 400
    assert request(server, "/api/apply", {"plan_id": ["wrong"]})[0] == 400
    assert request(server, "/api/history", {})[0] == 404
    assert request(server, "/../../etc/passwd")[0] == 404
    assert (
        request(server, "/api/preview", {"api_key": "sk-secret\ninvalid", "apps": ["aider"]})[0]
        == 400
    )


def test_catalog_fetch_no_auth_and_filters_media(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            return json.dumps(
                {
                    "success": True,
                    "data": [
                        {"id": "text-model", "model_type": "text"},
                        {"id": "image-model", "model_type": "image"},
                        {"id": "future-model", "upcoming": True},
                    ],
                }
            ).encode()

    class Opener:
        def open(self, req, timeout):
            seen.append(req)
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    result = fetch_models()
    assert result["models"] == ["text-model"]
    assert seen[0].full_url == "https://api.cometapi.com/api/models"
    assert not seen[0].has_header("Authorization")
    assert "does not validate" in result["message"]


def test_catalog_failure_is_safe(monkeypatch):
    from cometapi_helper.common import SetupError

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_: (_ for _ in ()).throw(ValueError("sensitive debug body")),
    )
    with pytest.raises(SetupError) as error:
        fetch_models()
    assert "sensitive" not in str(error.value)
