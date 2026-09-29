"""VS Code's native Custom Endpoint command; never edit encrypted secret rows."""

import base64
import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import json5

from . import windows_vscode
from .common import BASE_URL, SetupError
from .roo_store import IncompleteNativeWrite
from .storage import atomic_write, no_links, read_file, write_json

KIND = "copilot-vscode-native-v1"
NAME = "CometAPI Connect"
ASSETS = Path(__file__).parent / "assets/copilot_bridge"
SELECTION = "chat.currentLanguageModel.panel"


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def pack(value):
    return base64.b64encode(value).decode() if value is not None else None


def unpack(value):
    return base64.b64decode(value, validate=True) if value is not None else None


def paths(target):
    profile = Path(target["profile"])
    return {
        "models": profile / "User/chatLanguageModels.json",
        "settings": profile / "User/settings.json",
        "stamp": profile / "User/cometapi-connect-copilot.json",
    }


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "app", "profile", "extensions", "database"}
        or target.get("kind") != KIND
    ):
        raise SetupError("Invalid Copilot native resource.")
    for key in ("app", "profile", "extensions", "database"):
        if not isinstance(target[key], str) or not Path(target[key]).is_absolute():
            raise SetupError("Copilot paths must be absolute.")
        no_links(Path(target[key]))
    app, profile, extensions = (Path(target[k]) for k in ("app", "profile", "extensions"))
    if str(profile / "User/globalStorage/state.vscdb") != target["database"]:
        raise SetupError("Copilot profile and database do not match.")
    try:
        windows = (app / "Code.exe").is_file()
        root = windows_vscode.resources(app) if windows else app / "Contents/Resources/app"
        if (
            windows
            and (app / "data").is_dir()
            and (profile != app / "data/user-data" or extensions != app / "data/extensions")
        ):
            raise ValueError()
        info = (
            {"CFBundleIdentifier": "com.microsoft.VSCode"}
            if windows
            else plistlib.loads(read_file(app / "Contents/Info.plist"))
        )
        pkg = json.loads(read_file(root / "extensions/copilot/package.json"))
        if (
            info.get("CFBundleIdentifier") != "com.microsoft.VSCode"
            or (pkg.get("publisher"), pkg.get("name")) != ("GitHub", "copilot-chat")
            or not Path(target["database"]).is_file()
            or not windows_vscode.executable(app).is_file()
            or ((profile / "User/profiles").exists() and any((profile / "User/profiles").iterdir()))
        ):
            raise ValueError()
    except (ValueError, OSError, TypeError, AttributeError):
        raise SetupError(
            "Copilot needs one initialized default VS Code profile with bundled Copilot Chat on macOS or Windows."
        ) from None
    return app, profile, extensions


def require_closed(target):
    app, profile, _ = validate(target)
    if (app / "Code.exe").is_file():
        return windows_vscode.require_closed(app, profile)
    try:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "command="], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Cannot verify whether the selected VS Code profile is closed.") from None
    for line in processes.splitlines():
        if "/Contents/MacOS/Code" in line and (
            str(profile) in line or (str(app) in line and "--user-data-dir" not in line)
        ):
            raise SetupError(
                "Close the selected VS Code profile before configuring or restoring Copilot."
            )


def find(ctx):
    if ctx.platform == "win32":
        app, profile, extensions = windows_vscode.locations(ctx)
        target = dict(
            kind=KIND,
            app=str(app),
            profile=str(profile),
            extensions=str(extensions),
            database=str(profile / "User/globalStorage/state.vscdb"),
        )
        validate(target)
        return target
    if ctx.platform != "darwin":
        raise SetupError("This native Copilot adapter currently supports macOS and Windows.")
    apps = [r for r in ctx.roots if r.name == "Visual Studio Code.app"] or [
        p
        for p in (
            ctx.home / "Applications/Visual Studio Code.app",
            Path("/Applications/Visual Studio Code.app"),
        )
        if p.is_dir()
    ]
    profiles = [r for r in ctx.roots if (r / "User/globalStorage/state.vscdb").is_file()] or [
        ctx.home / "Library/Application Support/Code"
    ]
    extensions = [r for r in ctx.roots if r.name == "extensions" and r.is_dir()] or [
        ctx.home / ".vscode/extensions"
    ]
    if any(len(p) != 1 for p in (apps, profiles, extensions)):
        raise SetupError(
            "Select one local VS Code app, initialized default profile and extension directory."
        )
    target = dict(
        kind=KIND,
        app=str(apps[0]),
        profile=str(profiles[0]),
        extensions=str(extensions[0]),
        database=str(profiles[0] / "User/globalStorage/state.vscdb"),
    )
    validate(target)
    return target


def read(target):
    require_closed(target)
    value = {"schema": 1, **{k: pack(read_file(p)) for k, p in paths(target).items()}}
    with sqlite3.connect(Path(target["database"]).as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        row = db.execute("SELECT value FROM ItemTable WHERE key=?", (SELECTION,)).fetchone()
        value["selection"] = row[0] if row else None
    return encode(value)


def model_config(model):
    return {
        "id": model,
        "name": "CometAPI " + model,
        "url": BASE_URL + "/chat/completions",
        "toolCalling": True,
        "vision": False,
        "maxInputTokens": 65536,
        "maxOutputTokens": 8192,
        "streaming": True,
    }


def document(snapshot):
    value = json5.loads((unpack(snapshot["models"]) or b"[]").decode(), allow_duplicate_keys=False)
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise SetupError("Unsupported VS Code model configuration document.")
    return value


def prepare_value(before, key, model):
    snapshot = json.loads(before)
    doc = document(snapshot)
    groups = [g for g in doc if g.get("name") == NAME]
    stamp = json.loads(unpack(snapshot["stamp"]) or b"{}")
    desired_model = model_config(model)
    if len(groups) > 1 or (
        groups and (stamp.get("group_sha256") != hashlib.sha256(encode(groups[0])).hexdigest())
    ):
        raise SetupError(
            "An existing CometAPI group was edited outside the helper; its settings were preserved."
        )
    for group in doc:
        if (
            group.get("name") != NAME
            and group.get("vendor") == "customendpoint"
            and any(m.get("id") == model for m in group.get("models", []) if isinstance(m, dict))
        ):
            raise SetupError(
                "Another Custom Endpoint group uses this model ID. Resolve the ambiguous selection first."
            )
    settings = json5.loads(
        (unpack(snapshot["settings"]) or b"{}").decode(), allow_duplicate_keys=False
    )
    qualified = desired_model["name"] + " (customendpoint)"
    if (
        groups
        and groups[0].get("models") == [desired_model]
        and stamp.get("key_sha256") == hashlib.sha256(key.encode()).hexdigest()
        and stamp.get("selection") == snapshot["selection"]
        and snapshot["selection"]
        and all(
            settings.get(k) == qualified for k in ("chat.utilityModel", "chat.utilitySmallModel")
        )
    ):
        return before
    return encode({"operation": "configure", "key": key, "model": desired_model})


def restore_files(target, snapshot):
    for name, path in paths(target).items():
        data = unpack(snapshot[name])
        if data is None:
            no_links(path)
            if path.exists():
                path.unlink()
        else:
            atomic_write(path, data)
    with sqlite3.connect(target["database"], timeout=3) as db:
        db.execute("BEGIN IMMEDIATE")
        if snapshot["selection"] is None:
            db.execute("DELETE FROM ItemTable WHERE key=?", (SELECTION,))
        else:
            db.execute(
                "INSERT OR REPLACE INTO ItemTable(key,value) VALUES (?,?)",
                (SELECTION, snapshot["selection"]),
            )
        db.commit()


def write(target, value, expected):
    if read(target) != expected:
        raise SetupError("Copilot settings changed after preview; newer settings were preserved.")
    desired = json.loads(value)
    if desired.get("schema") == 1:
        try:
            restore_files(target, desired)
            if read(target) != value:
                raise ValueError()
        except Exception:
            try:
                restore_files(target, json.loads(expected))
                if read(target) != expected:
                    raise ValueError()
            except Exception:
                raise IncompleteNativeWrite(
                    "Copilot restore could not be confirmed. Preserve its backup."
                ) from None
            raise SetupError(
                "Copilot restore failed; the current configuration was recovered."
            ) from None
        return value
    if desired.get("operation") != "configure":
        raise SetupError("Invalid Copilot setup operation.")
    app, profile, extensions = validate(target)
    before = json.loads(expected)
    process = None
    try:
        doc = [g for g in document(before) if g.get("name") != NAME]
        atomic_write(paths(target)["models"], encode(doc))
        with tempfile.TemporaryDirectory(prefix="cometapi-copilot-") as temp:
            directory = Path(temp).resolve()
            directory.chmod(0o700)
            write_json(directory / "request.json", desired)
            env = {
                k: v
                for k, v in os.environ.items()
                if not any(
                    w in k
                    for w in (
                        "API_KEY",
                        "AUTH_TOKEN",
                        "BASE_URL",
                        "ELECTRON_RUN_AS_NODE",
                        "VSCODE_",
                        "COMETAPI_",
                    )
                )
            }
            env["COMETAPI_COPILOT_BRIDGE_DIRECTORY"] = str(directory)
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
            for manifest in extensions.glob("*/package.json"):
                pkg = json.loads(read_file(manifest))
                identity = str(pkg.get("publisher", "")) + "." + str(pkg.get("name", ""))
                if identity.lower() != "github.copilot-chat":
                    args += ["--disable-extension", identity]
            process = subprocess.Popen(
                args,
                cwd=directory,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **windows_vscode.launch_options(app),
            )
            deadline = time.monotonic() + 55
            while (
                process.poll() is None
                and time.monotonic() < deadline
                and not (directory / "response.json").exists()
            ):
                time.sleep(0.15)
            response = json.loads(read_file(directory / "response.json") or b"{}")
            if not response.get("ok"):
                raise SetupError(
                    "Copilot did not complete native setup. Check its startup window and retry."
                )
        windows_vscode.stop(process, app)
        current = json.loads(read(target))
        group = [g for g in document(current) if g.get("name") == NAME]
        if (
            len(group) != 1
            or not str(group[0].get("apiKey", "")).startswith("${input:chat.lm.secret.")
            or group[0].get("models") != [desired["model"]]
            or not current["selection"]
            or desired["key"].encode() in (unpack(current["models"]) or b"")
        ):
            raise SetupError("Copilot native credential or default-model readback failed.")
        write_json(
            paths(target)["stamp"],
            {
                "group_sha256": hashlib.sha256(encode(group[0])).hexdigest(),
                "key_sha256": hashlib.sha256(desired["key"].encode()).hexdigest(),
                "selection": current["selection"],
            },
        )
        return read(target)
    except Exception:
        if process is not None and process.poll() is None:
            windows_vscode.stop(process, app)
        try:
            require_closed(target)
            restore_files(target, before)
            if read(target) != expected:
                raise ValueError()
        except Exception:
            raise IncompleteNativeWrite(
                "Copilot could not confirm rollback. Preserve the transaction backup."
            ) from None
        raise SetupError(
            "Copilot setup failed; original connection settings were restored."
        ) from None
