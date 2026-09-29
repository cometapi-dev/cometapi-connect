"""Chatbox's verified desktop settings schema; no renderer database edits."""

import csv
import json
import subprocess
import uuid
from pathlib import Path

from .common import ANTHROPIC_URL, SetupError
from .storage import read_file

PROVIDER_ID = "cometapi-connect"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_document(document):
    """Reject uninitialized/unknown schemas instead of migrating app state."""
    error = "Chatbox automatic setup requires an initialized configuration with compatible provider settings."
    if not isinstance(document, dict) or type(document.get("configVersion")) is not int:
        raise SetupError(error)
    settings = document.get("settings")
    # Chatbox 1.23.3 moved session metadata to SQLite (config 15) and added
    # unrelated skill/MCP settings (settings 6); provider fields are unchanged.
    if not isinstance(settings, dict) or type(settings.get("__version")) is not int:
        raise SetupError(error)
    if not isinstance(document.get("configs"), dict) or not isinstance(
        settings.get("providers"), dict
    ):
        raise SetupError(error)
    try:
        uuid.UUID(document["configs"]["uuid"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise SetupError(error) from None
    return settings


def supported_config(path):
    try:
        data = read_file(path)
        if data is None:
            return False
        validate_document(json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object))
        return True
    except (SetupError, OSError, ValueError, UnicodeError):
        return False


def config_paths(ctx):
    # An explicitly supplied, identifiable profile takes precedence over defaults.
    explicit = [
        root / "config.json" for root in ctx.roots if supported_config(root / "config.json")
    ]
    if explicit:
        return list(dict.fromkeys(explicit))
    if ctx.platform == "darwin":
        base = ctx.home / "Library/Application Support"
    elif ctx.platform == "win32":
        base = ctx.config_dir("APPDATA", "AppData/Roaming")
    else:
        base = ctx.xdg
    # Official packaged name, community edition, and older desktop folder names.
    paths = [
        base / name / "config.json"
        for name in ("xyz.chatboxapp.app", "xyz.chatboxapp.ce", "Chatbox", "chatbox")
    ]
    return [path for path in paths if path.exists()]


def selected_config(ctx):
    paths = config_paths(ctx)
    if len(paths) != 1 or not supported_config(paths[0]):
        raise SetupError(
            "Chatbox needs one supported, initialized profile. Open and quit Chatbox once, or add the intended profile folder under additional scan paths; use guided setup if multiple profiles remain."
        )
    return paths[0]


def _process_names(ctx):
    if ctx.platform == "win32":
        system_root = ctx.env.get("SystemRoot") or ctx.env.get("SYSTEMROOT") or "C:\\Windows"
        executable = Path(system_root) / "System32/tasklist.exe"
        if not executable.is_absolute():
            raise SetupError(
                "Cannot verify whether Chatbox is closed. Use guided setup on this system."
            )
        command = [str(executable), "/FO", "CSV", "/NH"]
    else:
        command = ["/bin/ps", "-A", "-o", "comm="]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise OSError("Process inventory failed")
    except (OSError, subprocess.SubprocessError):
        raise SetupError(
            "Cannot verify whether Chatbox is closed. Use guided setup on this system."
        ) from None
    if ctx.platform == "win32":
        return [row[0] for row in csv.reader(result.stdout.splitlines()) if row]
    return result.stdout.splitlines()


def require_closed(ctx):
    # Process names only: never collect command arguments that could contain keys.
    for name in _process_names(ctx):
        name = name.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if name.startswith("chatbox") or name.startswith("xyz.chatboxapp"):
            raise SetupError(
                "Quit Chatbox completely before configuring or restoring it. A running app can overwrite its saved settings."
            )


def update_document(document, key, model):
    settings = validate_document(document)
    providers = settings["providers"]
    custom = settings.setdefault("customProviders", [])
    if not isinstance(custom, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in custom
    ):
        raise SetupError(
            "Chatbox's custom provider list has an unsupported format. Use its provider settings."
        )
    identities = [item["id"] for item in custom]
    if len(identities) != len(set(identities)):
        raise SetupError(
            "Chatbox contains duplicate custom provider IDs. Resolve them in Chatbox before setup."
        )
    existing = next((item for item in custom if item["id"] == PROVIDER_ID), None)
    if existing is not None:
        if (
            existing.get("name") != "CometAPI"
            or existing.get("type") != "openai"
            or existing.get("isCustom") is not True
        ):
            raise SetupError(
                "Chatbox already uses the CometAPI Connect provider ID for a different provider. Its settings were kept."
            )
    else:
        if PROVIDER_ID in providers:
            raise SetupError(
                "Chatbox contains an unmatched CometAPI Connect provider setting. Resolve it in Chatbox before setup."
            )
        custom.append({"id": PROVIDER_ID, "name": "CometAPI", "type": "openai", "isCustom": True})
    provider = providers.setdefault(PROVIDER_ID, {})
    if not isinstance(provider, dict):
        raise SetupError("Chatbox's CometAPI provider has an unsupported format.")
    models = provider.setdefault("models", [])
    if not isinstance(models, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("modelId"), str) for item in models
    ):
        raise SetupError("Chatbox's CometAPI model list has an unsupported format.")
    if len({item["modelId"] for item in models}) != len(models):
        raise SetupError("Chatbox's CometAPI model list contains duplicate IDs.")
    existing_model = next((item for item in models if item["modelId"] == model), None)
    if existing_model is None:
        models.append(
            {
                "modelId": model,
                "type": "chat",
                "nickname": "CometAPI " + model,
                "capabilities": [],
                "maxOutput": 2048,
            }
        )
    elif existing_model.get("type", "chat") != "chat":
        raise SetupError(
            "The selected Chatbox CometAPI model is configured for a non-chat task. Select a chat model."
        )
    excluded = provider.get("excludedModels")
    if excluded is not None:
        if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
            raise SetupError("Chatbox's excluded model list has an unsupported format.")
        provider["excludedModels"] = [item for item in excluded if item != model]
    provider.update({"apiHost": ANTHROPIC_URL, "apiPath": "/v1/chat/completions", "apiKey": key})
    settings["defaultChatModel"] = {"provider": PROVIDER_ID, "model": model}
