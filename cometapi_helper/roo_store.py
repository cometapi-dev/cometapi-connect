"""Version-pinned Roo configuration through a native VS Code extension host.

No encrypted SQLite editing. Launch an empty configuration host for the selected
closed profile, use Roo's native SecretStorage, then close that owned process.
"""

import json
import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path

from . import windows_vscode
from .common import BASE_URL, SetupError
from .storage import no_links, read_file, write_json

KIND = "roo-vscode-native-v1"
NAME = "CometAPI Connect"
ID = "cometapi-connect"
FIELDS = [
    "apiProvider",
    "openAiBaseUrl",
    "openAiModelId",
    "openAiHeaders",
    "openAiUseAzure",
    "openAiR1FormatEnabled",
    "openAiStreamingEnabled",
    "openAiCustomModelInfo",
    "includeMaxTokens",
    "modelMaxTokens",
    "currentApiConfigName",
    "listApiConfigMeta",
]
ASSETS = Path(__file__).parent / "assets/roo_bridge"


class IncompleteNativeWrite(SetupError):
    """The host stopped before confirming commit or rollback; keep the backup."""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "app", "profile", "extensions", "database"}
        or target["kind"] != KIND
    ):
        raise SetupError("Invalid Roo native configuration resource.")
    for field in ("app", "profile", "extensions", "database"):
        if not isinstance(target[field], str) or not Path(target[field]).is_absolute():
            raise SetupError("Roo configuration paths must be absolute.")
        no_links(Path(target[field]))
    app, profile, extensions = (Path(target[x]) for x in ("app", "profile", "extensions"))
    if str(profile / "User/globalStorage/state.vscdb") != target["database"]:
        raise SetupError("Roo profile/database paths do not match.")
    try:
        windows = (app / "Code.exe").is_file()
        if windows:
            windows_vscode.resources(app)
            if (app / "data").is_dir() and (
                profile != app / "data/user-data" or extensions != app / "data/extensions"
            ):
                raise ValueError()
        info = (
            {"CFBundleIdentifier": "com.microsoft.VSCode", "CFBundleExecutable": "Code"}
            if windows
            else plistlib.loads(read_file(app / "Contents/Info.plist"))
        )
        if (info.get("CFBundleIdentifier"), info.get("CFBundleExecutable")) != (
            "com.microsoft.VSCode",
            "Code",
        ):
            raise ValueError()
        candidates = list(extensions.glob("rooveterinaryinc.roo-cline-*"))
        if len(candidates) != 1:
            raise ValueError()
        package = json.loads(read_file(candidates[0] / "package.json"))
        if (package.get("publisher", "").lower(), package.get("name")) != (
            "rooveterinaryinc",
            "roo-cline",
        ):
            raise ValueError()
        for path in (windows_vscode.executable(app), profile / "User/globalStorage/state.vscdb"):
            no_links(path)
            if not path.is_file():
                raise ValueError()
        # Named VS Code profiles require their own resource mapping.
        if (profile / "User/profiles").exists() and any((profile / "User/profiles").iterdir()):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise SetupError(
            "Roo requires one initialized default VS Code profile and Roo Code on macOS or Windows."
        ) from None
    return app, profile, extensions


def find(ctx):
    if ctx.platform == "win32":
        app, profile, extensions = windows_vscode.locations(ctx)
        target = {
            "kind": KIND,
            "app": str(app),
            "profile": str(profile),
            "extensions": str(extensions),
            "database": str(profile / "User/globalStorage/state.vscdb"),
        }
        validate(target)
        return target
    if ctx.platform != "darwin":
        raise SetupError("The Roo native credential bridge currently supports macOS and Windows.")
    apps = [r for r in ctx.roots if r.name == "Visual Studio Code.app"]
    profiles = [r for r in ctx.roots if (r / "User/globalStorage/state.vscdb").is_file()]
    extensions = [r for r in ctx.roots if list(r.glob("rooveterinaryinc.roo-cline-*"))]
    if not apps:
        apps = [
            p
            for p in (
                ctx.home / "Applications/Visual Studio Code.app",
                Path("/Applications/Visual Studio Code.app"),
            )
            if p.is_dir()
        ]
    if not profiles:
        profiles = [ctx.home / "Library/Application Support/Code"]
    if not extensions:
        extensions = [ctx.home / ".vscode/extensions"]
    if any(len(paths) != 1 for paths in (apps, profiles, extensions)):
        raise SetupError("Select one VS Code application, profile and Roo extension directory.")
    target = {
        "kind": KIND,
        "app": str(apps[0]),
        "profile": str(profiles[0]),
        "extensions": str(extensions[0]),
        "database": str(profiles[0] / "User/globalStorage/state.vscdb"),
    }
    validate(target)
    return target


def require_closed(target):
    app, profile, _ = validate(target)
    if (app / "Code.exe").is_file():
        return windows_vscode.require_closed(app, profile)
    try:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "command="], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Cannot verify that the selected VS Code profile is closed.") from None
    for line in processes.splitlines():
        if "/Contents/MacOS/Code" in line:
            # A default-profile launch has no override. Do not touch it while open.
            if str(profile) in line or (
                str(app / "Contents/MacOS/Code") in line and "--user-data-dir" not in line
            ):
                raise SetupError(
                    "Close the selected VS Code profile before connecting or restoring Roo."
                )


def _run(target, request):
    app, profile, extensions = validate(target)
    require_closed(target)
    with tempfile.TemporaryDirectory(prefix="cometapi-roo-") as temporary:
        directory = Path(temporary).resolve()
        directory.chmod(0o700)
        write_json(directory / "request.json", request)
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(
                part in k
                for part in (
                    "API_KEY",
                    "AUTH_TOKEN",
                    "BASE_URL",
                    "ELECTRON_RUN_AS_NODE",
                    "VSCODE_",
                    "COMETAPI_",
                )
            )
        }
        env["COMETAPI_ROO_BRIDGE_DIRECTORY"] = str(directory)
        args = [
            str(windows_vscode.executable(app)),
            "--user-data-dir",
            str(profile),
            "--extensions-dir",
            str(extensions),
            "--extensionDevelopmentPath=" + str(ASSETS),
            "--skip-welcome",
            "--skip-release-notes",
            "--new-window",
        ]
        # Only Roo and our short-lived development extension are needed. This
        # avoids activating other installed agents in the configuration host.
        for manifest in sorted(extensions.glob("*/package.json")):
            package = json.loads(read_file(manifest))
            identity = str(package.get("publisher", "")) + "." + str(package.get("name", ""))
            if identity.lower() != "rooveterinaryinc.roo-cline":
                args.extend(["--disable-extension", identity])
        process = None
        try:
            process = subprocess.Popen(
                args,
                cwd=directory,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **windows_vscode.launch_options(app),
            )
            deadline = time.monotonic() + 50
            while (
                process.poll() is None
                and time.monotonic() < deadline
                and not (directory / "response.json").exists()
            ):
                time.sleep(0.15)
            raw = read_file(directory / "response.json")
            if raw is None:
                if request.get("operation") == "write":
                    raise IncompleteNativeWrite(
                        "Roo did not confirm its native write. Keep the transaction backup for recovery."
                    )
                raise SetupError(
                    "Roo did not finish its native configuration operation. Check the app startup prompt, then retry."
                )
            response = json.loads(raw)
            if not response.get("ok"):
                error = response.get("error")
                if error == "rollback-failed":
                    raise IncompleteNativeWrite(
                        "Roo native rollback failed. Preserve the helper backup for recovery."
                    )
                if error == "active-task":
                    raise SetupError(
                        "Roo has an active task. Close it before connecting or restoring."
                    )
                if error == "conflict":
                    raise SetupError(
                        "Roo settings changed after preview; newer settings were preserved."
                    )
                raise SetupError(
                    "Roo native configuration was not completed; its profile may need initialization or a supported version."
                )
            return response["snapshot"]
        finally:
            if process is not None and process.poll() is None:
                # Native writes have awaited their VS Code storage acknowledgments.
                windows_vscode.stop(process, app)


def read(target):
    return encode(_run(target, {"operation": "read"}))


def write(target, value, expected):
    _run(
        target,
        {"operation": "write", "desired": json.loads(value), "expected": json.loads(expected)},
    )


def prepare_value(before, key, model):
    try:
        value = json.loads(before)
        if value.get("schema") != 1 or set(value["values"]) != set(FIELDS):
            raise ValueError()
        profiles = json.loads(value["profiles"])
        existing = profiles["apiConfigs"].get(NAME)
        if existing is not None and existing.get("id") != ID:
            raise SetupError("A user-created Roo profile already uses the CometAPI Connect name.")
        settings = {
            "apiProvider": "openai",
            "openAiBaseUrl": BASE_URL,
            "openAiModelId": model,
            "openAiHeaders": {},
            "openAiUseAzure": False,
            "openAiR1FormatEnabled": False,
            "openAiStreamingEnabled": True,
            "openAiCustomModelInfo": {
                "contextWindow": 65536,
                "maxTokens": 8192,
                "supportsPromptCache": False,
                "supportsImages": False,
            },
            "includeMaxTokens": True,
            "modelMaxTokens": 8192,
        }
        profiles["apiConfigs"][NAME] = dict(settings, id=ID, openAiApiKey=key)
        profiles["currentApiConfigName"] = NAME
        profiles["modeApiConfigs"][value["mode"]] = ID
        metadata = value["values"]["listApiConfigMeta"] or []
        metadata = [item for item in metadata if item["name"] != NAME]
        metadata.append({"name": NAME, "id": ID, "apiProvider": "openai", "modelId": model})
        metadata.sort(key=lambda item: item["name"])
        value["values"].update(settings, currentApiConfigName=NAME, listApiConfigMeta=metadata)
        value["secret"] = key
        value["profiles"] = encode(profiles).decode()
        return encode(value)
    except SetupError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SetupError("Unsupported Roo profile format; configuration was preserved.") from None
