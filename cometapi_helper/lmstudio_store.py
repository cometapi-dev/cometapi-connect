"""LM Studio's native global generator settings and selected chat configuration.

Snapshots contain connection fields only, so recovery preserves new messages.
A reviewed revision 9 generator bundle provisions a missing plugin offline.
"""

import copy
import csv
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from .common import BASE_URL, SetupError
from .roo_store import IncompleteNativeWrite
from .storage import atomic_write, no_links, private_mkdir, read_file

KIND = "lmstudio-generator-config-v2"
PLUGIN = "lmstudio/openai-compat-endpoint"
MANAGED_CHAT = "cometapi-connect.conversation.json"
MODELS = {
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini-2025-04-14",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def read_json(path, default=None):
    raw = read_file(path)
    if raw is None:
        if default is not None:
            return copy.deepcopy(default)
        raise SetupError("LM Studio configuration is not initialized.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, UnicodeError):
        raise SetupError("Unsupported LM Studio configuration document.") from None


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "app", "root", "chat", "database", "create_chat"}
        or target.get("kind") != KIND
        or type(target.get("create_chat")) is not bool
    ):
        raise SetupError("Invalid LM Studio configuration resource.")
    for name in ("app", "root", "database"):
        if not isinstance(target[name], str) or not Path(target[name]).is_absolute():
            raise SetupError("LM Studio paths must be absolute.")
        no_links(Path(target[name]))
    if not isinstance(target["chat"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+\.conversation\.json", target["chat"]
    ):
        raise SetupError("Invalid LM Studio conversation identifier.")
    app, root = Path(target["app"]), Path(target["root"])
    if str(root / ".internal/global-plugin-configs.json") != target["database"]:
        raise SetupError("LM Studio resource path mismatch.")
    try:
        if (app / "LM Studio.exe").is_file():
            no_links(app / "LM Studio.exe")
            info = read_json(app / "resources/app/package.json")
            if (info.get("name"), info.get("desktopName")) != (
                "lm-studio",
                "ai.elementlabs.lmstudio",
            ):
                raise ValueError()
        else:
            info = plistlib.loads(read_file(app / "Contents/Info.plist"))
            if info.get("CFBundleIdentifier") != "ai.elementlabs.lmstudio":
                raise ValueError()
        manifest_path = root / "extensions/plugins" / PLUGIN / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else None
        if manifest is not None and (
            not isinstance(manifest, dict)
            or any(
                manifest.get(k) != v
                for k, v in {
                    "type": "plugin",
                    "runner": "node",
                    "owner": "lmstudio",
                    "name": "openai-compat-endpoint",
                }.items()
            )
        ):
            raise ValueError()
        if target["create_chat"] and target["chat"] != MANAGED_CHAT:
            raise ValueError()
    except (ValueError, TypeError, OSError):
        raise SetupError(
            "Requires an identifiable LM Studio installation and the official OpenAI-compatible generator layout."
        ) from None
    no_links(root / "conversations" / target["chat"])
    return root


def require_closed(target):
    validate(target)
    windows = (Path(target["app"]) / "LM Studio.exe").is_file()
    try:
        command = ["tasklist.exe", "/FO", "CSV", "/NH"] if windows else ["/bin/ps", "-axo", "comm="]
        output = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Cannot verify that LM Studio is closed.") from None
    running = (
        any(row and row[0].casefold() == "lm studio.exe" for row in csv.reader(output.splitlines()))
        if windows
        else any("/LM Studio.app/Contents/MacOS/" in line for line in output.splitlines())
    )
    if running:
        raise SetupError("Close LM Studio before configuring or restoring its generator.")


def find(ctx):
    if ctx.platform == "win32":
        apps = [r for r in ctx.roots if (r / "LM Studio.exe").is_file()]
        if not apps:
            candidates = [ctx.config_dir("LOCALAPPDATA", "AppData/Local") / "Programs/LM Studio"]
            if ctx.env.get("ProgramFiles"):
                candidates.append(Path(ctx.env["ProgramFiles"]) / "LM Studio")
            apps = [p for p in candidates if (p / "LM Studio.exe").is_file()]
    elif ctx.platform == "darwin":
        apps = [r for r in ctx.roots if r.name == "LM Studio.app"] or [
            Path("/Applications/LM Studio.app")
        ]
    else:
        raise SetupError("This LM Studio adapter currently supports macOS and Windows.")
    roots = [r for r in ctx.roots if (r / ".internal/conversation-config.json").is_file()] or [
        ctx.home / ".lmstudio"
    ]
    if len(apps) != 1 or len(roots) != 1:
        raise SetupError("Select one initialized LM Studio profile.")
    selected = read_json(roots[0] / ".internal/conversation-config.json").get(
        "selectedConversation"
    )
    target = {
        "kind": KIND,
        "app": str(apps[0]),
        "root": str(roots[0]),
        "chat": selected or MANAGED_CHAT,
        "create_chat": not bool(selected),
        "database": str(roots[0] / ".internal/global-plugin-configs.json"),
    }
    validate(target)
    return target


def documents(target):
    root = validate(target)
    global_doc = read_json(
        Path(target["database"]),
        {"json": {"plugins": []}, "meta": {"values": {"plugins": ["map"]}}},
    )
    try:
        if global_doc["meta"]["values"]["plugins"] != ["map"]:
            raise ValueError()
        pairs = global_doc["json"]["plugins"]
        if not isinstance(pairs, list) or any(
            not isinstance(p, list)
            or len(p) != 2
            or not isinstance(p[0], str)
            or not isinstance(p[1], dict)
            for p in pairs
        ):
            raise ValueError()
        if len({p[0] for p in pairs}) != len(pairs):
            raise ValueError()
        chat = read_json(
            root / "conversations" / target["chat"], empty_chat() if target["create_chat"] else None
        )
        if (
            not isinstance(chat.get("plugins"), list)
            or any(not isinstance(x, str) for x in chat["plugins"])
            or not isinstance(chat.get("pluginConfigs"), dict)
        ):
            raise ValueError()
        # Preserve local-model and other-generator conversations until explicitly adapted.
        if chat["plugins"] not in ([], [PLUGIN]):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise SetupError("Unsupported LM Studio generator or conversation configuration.") from None
    return global_doc, chat


def read(target):
    require_closed(target)
    global_doc, chat = documents(target)
    own = next(
        (value for identity, value in global_doc["json"]["plugins"] if identity == PLUGIN), None
    )
    result = {
        "global": own,
        "plugins": chat["plugins"],
        "config": chat["pluginConfigs"].get(PLUGIN),
    }
    if target["create_chat"]:
        config, ui = selection_documents(target)
        result["selection"] = {
            "selectedConversation": config.get("selectedConversation"),
            "chat.activeConversationIdentifier": ui.get("chat", {}).get(
                "activeConversationIdentifier"
            ),
            "tabLayouts.chat": ui.get("tabLayouts", {}).get("chat"),
        }
    return encode(result)


def empty_chat():
    return {
        "name": "CometAPI Chat",
        "pinned": False,
        "createdAt": int(time.time() * 1000),
        "preset": "",
        "tokenCount": 0,
        "systemPrompt": "",
        "messages": [],
        "usePerChatPredictionConfig": True,
        "perChatPredictionConfig": {"fields": []},
        "clientInput": "",
        "clientInputFiles": [],
        "userFilesSizeBytes": 0,
        "notes": [],
        "plugins": [],
        "pluginConfigs": {},
        "disabledPluginTools": [],
        "looseFiles": [],
    }


def selection_documents(target):
    root = Path(target["root"])
    config = read_json(root / ".internal/conversation-config.json")
    ui = read_json(root / ".internal/ui-state/window-1.json", {})
    if not isinstance(ui.get("chat", {}), dict) or not isinstance(ui.get("tabLayouts", {}), dict):
        raise SetupError("Unsupported LM Studio UI-state objects.")
    layout = ui.get("tabLayouts", {}).get("chat")
    if layout and (
        not isinstance(layout, dict) or layout.get("type") != "pane" or layout.get("tabs")
    ):
        # A saved nonempty layout can belong to another conversation or split view.
        allowed = ["conversation:" + MANAGED_CHAT]
        if not isinstance(layout, dict) or layout.get("tabs") != allowed:
            raise SetupError(
                "Select an existing LM Studio chat before configuring a saved multi-chat layout."
            )
    return config, ui


def bundled_plugin():
    # PyInstaller macOS bundles link package data from Frameworks to Resources.
    # Resolve this trusted resource path; the pinned digest still checks its bytes.
    asset = (Path(__file__).parent / "assets/lmstudio-openai-compat-rev9.zip").resolve()
    if (
        hashlib.sha256(read_file(asset) or b"").hexdigest()
        != "4f32964b3b137c2e056fa94d99d5b635ee13f3e62ec0c34c34df1f8a4359cda6"
    ):
        raise SetupError("Bundled LM Studio plugin integrity check failed.")
    return asset


def ensure_plugin(target):
    root = validate(target)
    plugin = root / "extensions/plugins" / PLUGIN
    if (plugin / "manifest.json").exists():
        return
    if plugin.exists():
        raise SetupError("An incomplete LM Studio plugin directory already exists.")
    # Carry the reviewed upstream generator and its licensed JS dependencies. This avoids
    # the CLI starting LM Studio while its native configuration is being written.
    asset = bundled_plugin()
    private_mkdir(plugin.parent)
    stage = plugin.parent / (".cometapi-install-" + secrets.token_hex(12))
    if os.name == "nt":
        # Nested official node_modules paths plus atomic temporary filenames can
        # exceed MAX_PATH; extended paths do not change system policy or ACLs.
        absolute = str(stage.absolute())
        stage = Path(
            "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
        )
    private_mkdir(stage)
    try:
        with zipfile.ZipFile(asset) as archive:
            entries = archive.infolist()
            if len(entries) > 3000 or sum(e.file_size for e in entries) > 15000000:
                raise ValueError()
            for entry in entries:
                path = Path(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or entry.is_dir()
                    or (entry.external_attr >> 16) & 0o170000 != 0o100000
                ):
                    raise ValueError()
                atomic_write(stage / path, archive.read(entry))
        # All installed plugin files are public code; keys live elsewhere.
        if plugin.exists():
            raise ValueError()
        os.replace(stage, plugin)
        validate(target)
    except (OSError, ValueError, zipfile.BadZipFile):
        raise SetupError(
            "Bundled LM Studio plugin installation failed before connection configuration."
        ) from None
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def fields_update(value, updates):
    if not isinstance(value, dict):
        raise SetupError("Unsupported LM Studio plugin fields.")
    result = copy.deepcopy(value)
    fields = result.setdefault("fields", [])
    if (
        not isinstance(fields, list)
        or any(
            not isinstance(f, dict) or not isinstance(f.get("key"), str) or "value" not in f
            for f in fields
        )
        or len({f["key"] for f in fields}) != len(fields)
    ):
        raise SetupError("Unsupported LM Studio plugin fields.")
    for key, value in updates.items():
        found = next((f for f in fields if f["key"] == key), None)
        if found is None:
            fields.append({"key": key, "value": value})
        else:
            found["value"] = value
    return result


def prepare_value(before, key, model):
    if model not in MODELS:
        raise SetupError(
            "The installed LM Studio generator only supports its four declared model IDs."
        )
    result = json.loads(before)
    key_name = "openaiApiKey" if model.startswith("gpt-") else "anthropicApiKey"
    result["global"] = fields_update(
        result["global"] or {}, {"overrideBaseUrl": BASE_URL, key_name: key}
    )
    result["plugins"] = [PLUGIN]
    config = result["config"] or {"preset": "", "config": {"fields": []}}
    config["config"] = fields_update(config["config"], {"model": model})
    result["config"] = config
    if "selection" in result:
        result["selection"] = {
            "selectedConversation": MANAGED_CHAT,
            "chat.activeConversationIdentifier": MANAGED_CHAT,
            "tabLayouts.chat": {
                "type": "pane",
                "id": "root",
                "instanceId": "root",
                "tabs": ["conversation:" + MANAGED_CHAT],
                "tabInstanceIds": ["cometapi-connect-tab"],
                "active": 0,
            },
        }
    return encode(result)


def write(target, value, expected):
    require_closed(target)
    if expected is None or read(target) != expected:
        raise SetupError("LM Studio connection settings changed; no files were overwritten.")
    desired = json.loads(value)
    expected_keys = {"global", "plugins", "config"} | (
        {"selection"} if target["create_chat"] else set()
    )
    if set(desired) != expected_keys or desired["plugins"] not in ([], [PLUGIN]):
        raise SetupError("Invalid LM Studio snapshot.")
    if desired["global"] is not None:
        fields_update(desired["global"], {})
    if desired["config"] is not None:
        if not isinstance(desired["config"], dict) or "config" not in desired["config"]:
            raise SetupError("Invalid LM Studio chat configuration.")
        fields_update(desired["config"]["config"], {})
    if desired["plugins"]:
        ensure_plugin(target)
        require_closed(target)
        if read(target) != expected:
            raise SetupError("LM Studio settings changed during plugin installation.")
    global_doc, chat = documents(target)
    pairs = global_doc["json"]["plugins"]
    matches = [i for i, pair in enumerate(pairs) if pair[0] == PLUGIN]
    if desired["global"] is None:
        if matches:
            pairs.pop(matches[0])
    elif matches:
        pairs[matches[0]][1] = desired["global"]
    else:
        pairs.append([PLUGIN, desired["global"]])
    chat["plugins"] = desired["plugins"]
    if desired["config"] is None:
        chat["pluginConfigs"].pop(PLUGIN, None)
    else:
        chat["pluginConfigs"][PLUGIN] = desired["config"]
    updates = [
        (Path(target["database"]), global_doc),
        (Path(target["root"]) / "conversations" / target["chat"], chat),
    ]
    if target["create_chat"]:
        config, ui = selection_documents(target)
        selection = desired["selection"]
        if not isinstance(selection, dict) or set(selection) != {
            "selectedConversation",
            "chat.activeConversationIdentifier",
            "tabLayouts.chat",
        }:
            raise SetupError("Invalid LM Studio selection snapshot.")
        config["selectedConversation"] = selection["selectedConversation"]
        for key in ("chat.activeConversationIdentifier", "tabLayouts.chat"):
            parent, child = key.split(".", 1)
            if selection[key] is None:
                ui.get(parent, {}).pop(child, None)
            else:
                ui.setdefault(parent, {})[child] = selection[key]
        updates.extend(
            [
                (Path(target["root"]) / ".internal/conversation-config.json", config),
                (Path(target["root"]) / ".internal/ui-state/window-1.json", ui),
            ]
        )
    originals = {path: read_file(path) for path, _ in updates}
    try:
        for path, doc in updates:
            atomic_write(path, (json.dumps(doc, indent=2) + "\n").encode())
        if read(target) != value:
            raise SetupError("LM Studio configuration readback did not match.")
    except Exception:
        try:
            for path, raw in originals.items():
                if raw is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write(path, raw)
        except Exception:
            raise IncompleteNativeWrite(
                "LM Studio recovery could not be confirmed; retained backup needs recovery."
            ) from None
        raise SetupError(
            "LM Studio write failed; original configuration files were recovered."
        ) from None
