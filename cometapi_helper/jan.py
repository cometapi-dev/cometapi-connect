"""Jan Desktop v0.8.4 settings store (version 17) and native keyring."""

import csv
import json
import subprocess
from pathlib import Path

from .common import BASE_URL, SetupError
from .storage import read_file

PROVIDER = "CometAPI Connect"


def store(document):
    try:
        blob = json.loads(document["model-provider"])
        state = blob["state"]
        providers = state["providers"]
        if type(blob.get("version")) is not int:
            raise ValueError()
        if not isinstance(providers, list) or any(
            not isinstance(p, dict) or not isinstance(p.get("provider"), str) for p in providers
        ):
            raise ValueError()
        names = [p["provider"].casefold() for p in providers]
        if len(names) != len(set(names)):
            raise ValueError()
        return blob
    except (ValueError, KeyError, TypeError):
        raise SetupError(
            "Jan automatic setup requires an initialized Desktop provider store with compatible fields. This profile was not changed."
        ) from None


def supported(path):
    try:
        store(json.loads(read_file(path) or b"{}"))
        return True
    except (ValueError, SetupError, OSError):
        return False


def settings_path(ctx):
    explicit = [root / "settings.json" for root in ctx.roots if supported(root / "settings.json")]
    if len(explicit) > 1:
        raise SetupError("Multiple Jan Desktop profiles found; scan one profile at a time.")
    if explicit:
        return explicit[0]
    if ctx.env.get("JAN_DATA_FOLDER"):
        root = Path(ctx.env["JAN_DATA_FOLDER"]).expanduser()
    else:
        base = (
            ctx.config_dir("APPDATA", "AppData/Roaming") / "Jan"
            if ctx.platform == "win32"
            else ctx.home / "Library/Application Support/Jan"
        )
        config = json.loads(read_file(base / "settings.json") or b"{}")
        root = (
            Path(config["data_folder"]).expanduser() if config.get("data_folder") else base / "data"
        )
    if not root.is_absolute():
        raise SetupError("Jan's data folder must be an absolute path.")
    return root / "settings.json"


def assistant_path(ctx):
    """Find only the initialized default assistant associated with this profile.

    JAN_DATA_FOLDER redirects the backend settings store, but Desktop assistant
    files still follow the app's data_folder configuration in Jan 0.8.4.
    """
    profiles = [root for root in ctx.roots if supported(root / "settings.json")]
    paths = [
        root / "assistants/jan/assistant.json"
        for root in profiles
        if (root / "assistants/jan/assistant.json").is_file()
    ]
    if len(paths) > 1:
        raise SetupError("Multiple Jan default assistants found; scan one profile at a time.")
    if paths:
        return paths[0]
    if profiles and not ctx.env.get("JAN_DATA_FOLDER"):
        return None
    base = (
        ctx.config_dir("APPDATA", "AppData/Roaming") / "Jan"
        if ctx.platform == "win32"
        else ctx.home / "Library/Application Support/Jan"
    )
    try:
        config = json.loads(read_file(base / "settings.json") or b"{}")
        root = (
            Path(config["data_folder"]).expanduser() if config.get("data_folder") else base / "data"
        )
    except (ValueError, TypeError, AttributeError):
        raise SetupError("Jan's Desktop data-folder configuration is not recognized.") from None
    if not root.is_absolute():
        raise SetupError("Jan's Desktop data folder must be an absolute path.")
    path = root / "assistants/jan/assistant.json"
    return path if path.is_file() else None


def update_assistant(document):
    if (
        document.get("id") != "jan"
        or document.get("object") != "assistant"
        or not isinstance(document.get("parameters"), dict)
    ):
        raise SetupError("Jan's initialized default assistant has an unsupported format.")
    for name in ("top_k", "repeat_penalty"):
        document["parameters"].pop(name, None)


def require_closed(ctx):
    if ctx.platform not in ("darwin", "win32"):
        raise SetupError(
            "Jan's native credential adapter is currently verified on macOS and Windows."
        )
    try:
        command = (
            ["tasklist.exe", "/FO", "CSV", "/NH"]
            if ctx.platform == "win32"
            else ["/bin/ps", "-A", "-o", "comm="]
        )
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise OSError()
    except (OSError, subprocess.SubprocessError):
        raise SetupError("Cannot verify whether Jan is closed. No settings were changed.") from None
    names = (
        [row[0] for row in csv.reader(result.stdout.splitlines()) if row]
        if ctx.platform == "win32"
        else result.stdout.splitlines()
    )
    for name in names:
        if name.strip().rsplit("/", 1)[-1].casefold() in (
            "jan",
            "jan-cli",
            "jan.exe",
            "jan-cli.exe",
        ):
            raise SetupError(
                "Quit Jan and Jan CLI before configuring or restoring the shared Desktop settings."
            )


def update(document, model):
    blob = store(document)
    state = blob["state"]
    providers = state["providers"]
    provider = next((p for p in providers if p["provider"].casefold() == PROVIDER.casefold()), None)
    if provider is None:
        provider = {"provider": PROVIDER, "active": True, "models": [], "settings": []}
        providers.append(provider)
    elif provider["provider"] != PROVIDER or provider.get("base_url") != BASE_URL:
        raise SetupError(
            "Jan already uses the CometAPI Connect name for another endpoint. Its settings were kept."
        )
    models = provider.get("models")
    if not isinstance(models, list) or any(
        not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in models
    ):
        raise SetupError("Unrecognized Jan model list.")
    selected = next((m for m in models if m["id"] == model), None)
    if selected is None:
        selected = {"id": model, "name": model, "capabilities": ["completion"]}
        models.append(selected)
    provider.update(
        active=True,
        base_url=BASE_URL,
        api_type="openai",
        custom_header=[],
        settings=[
            {
                "key": "api-key",
                "title": "API Key",
                "description": "Stored in your system keyring.",
                "controller_type": "input",
                "controller_props": {"value": "", "type": "password"},
            },
            {
                "key": "base-url",
                "title": "Base URL",
                "description": "CometAPI endpoint",
                "controller_type": "input",
                "controller_props": {"value": BASE_URL},
            },
        ],
    )
    provider.pop("api_key", None)
    provider.pop("api_key_fallbacks", None)
    state.update(selectedProvider=PROVIDER, selectedModel=selected)
    # Jan v17 tracks hidden IDs globally. Re-enable only the selected model.
    deleted = state.get("deletedModels", [])
    if not isinstance(deleted, list) or any(not isinstance(m, str) for m in deleted):
        raise SetupError("Unrecognized Jan deleted-model tracking.")
    state["deletedModels"] = [m for m in deleted if m != model]
    document["model-provider"] = json.dumps(blob, ensure_ascii=False, separators=(",", ":"))
