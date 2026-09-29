"""Chatbox integration tests use disposable profiles and fake process inventories."""

import copy
import json
import stat
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from cometapi_helper import chatbox, detection
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-cometapi_fake_test_key_123456"
READ_PROCESS_NAMES = chatbox._process_names


def initial_config():
    return {
        "configVersion": 14,
        "configs": {"uuid": "01234567-89ab-cdef-0123-456789abcdef"},
        "windowState": {"width": 1200},
        "settings": {
            "__version": 2,
            "theme": "dark",
            "providers": {
                "openai": {
                    "apiKey": "old-provider-key",
                    "apiHost": "https://api.openai.com",
                    "models": [{"modelId": "gpt-4o", "capabilities": ["vision"]}],
                }
            },
            "customProviders": [
                {"id": "private-provider", "name": "Private", "type": "anthropic", "isCustom": True}
            ],
            "defaultChatModel": {"provider": "openai", "model": "gpt-4o"},
            "threadNamingModel": {"provider": "private-provider", "model": "private-model"},
        },
    }


def put(path, document=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(initial_config() if document is None else document, separators=(",", ":"))
    )
    return path


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    entries = copy.deepcopy(detection.catalog())
    for entry in entries:
        entry["detection"]["app_names"] = []
    monkeypatch.setattr(detection, "catalog", lambda: copy.deepcopy(entries))
    monkeypatch.setattr(chatbox, "_process_names", lambda ctx: [])
    return Engine(Context(home=tmp_path, platform="linux", env={}, use_path=False))


def app(engine):
    return next(row for row in engine.scan()["apps"] if row["id"] == "chatbox")


@pytest.mark.parametrize("versions", [(14, 2), (15, 6), (99, 99), (13, 1)])
def test_custom_provider_preserves_other_settings_and_restores_exact_bytes(isolated, versions):
    initial = initial_config()
    initial["configVersion"], initial["settings"]["__version"] = versions
    path = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json", initial)
    path.chmod(0o640)
    original_mode = stat.S_IMODE(path.stat().st_mode)
    before = path.read_bytes()
    old = json.loads(before)
    assert app(isolated)["mode"] == "automatic"
    preview = isolated.preview(KEY, ["chatbox"])
    assert path.read_bytes() == before and KEY not in json.dumps(preview)
    result = isolated.apply(preview["plan_id"])
    saved = json.loads(path.read_text())
    settings = saved["settings"]
    assert settings["providers"]["openai"] == old["settings"]["providers"]["openai"]
    assert settings["customProviders"][0] == old["settings"]["customProviders"][0]
    assert settings["threadNamingModel"] == old["settings"]["threadNamingModel"]
    assert saved["configs"] == old["configs"] and saved["windowState"] == old["windowState"]
    assert settings["theme"] == "dark"
    provider = settings["providers"]["cometapi-connect"]
    assert (
        provider["apiHost"] + provider["apiPath"] == "https://api.cometapi.com/v1/chat/completions"
    )
    assert provider["apiKey"] == KEY
    assert settings["defaultChatModel"] == {"provider": "cometapi-connect", "model": "gpt-5.4-mini"}
    assert isolated.apply(isolated.preview(KEY, ["chatbox"])["plan_id"])["transaction_id"] is None
    isolated.restore(result["transaction_id"])
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == original_mode


@pytest.mark.parametrize(
    "platform,env,relative",
    [
        ("darwin", {}, "Library/Application Support/xyz.chatboxapp.app/config.json"),
        ("win32", {}, "AppData/Roaming/xyz.chatboxapp.app/config.json"),
        ("linux", {}, ".config/xyz.chatboxapp.app/config.json"),
    ],
)
def test_platform_default_paths(isolated, platform, env, relative):
    isolated.ctx.platform = platform
    isolated.ctx.env = env
    path = put(isolated.ctx.home / relative)
    assert chatbox.selected_config(isolated.ctx) == path
    assert app(isolated)["mode"] == "automatic"


@pytest.mark.parametrize("platform,variable", [("win32", "APPDATA"), ("linux", "XDG_CONFIG_HOME")])
def test_relocated_platform_config_directory(isolated, platform, variable):
    isolated.ctx.platform = platform
    isolated.ctx.env = {variable: str(isolated.ctx.home / "relocated")}
    path = put(isolated.ctx.home / "relocated/xyz.chatboxapp.app/config.json")
    assert chatbox.selected_config(isolated.ctx) == path


@pytest.mark.parametrize(
    "field,value",
    [
        ("configVersion", None),
        ("configVersion", True),
        ("configVersion", "14"),
        ("settings_version", "2"),
        ("settings_version", None),
        ("uuid", "not-a-uuid"),
    ],
)
def test_unknown_schema_stays_guided_without_writes(isolated, field, value):
    doc = initial_config()
    if field == "settings_version":
        doc["settings"]["__version"] = value
    elif field == "uuid":
        doc["configs"]["uuid"] = value
    else:
        doc[field] = value
    path = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json", doc)
    before = path.read_bytes()
    assert app(isolated)["detected"] and app(isolated)["mode"] == "guided"
    with pytest.raises(SetupError):
        isolated.preview(KEY, ["chatbox"])
    assert path.read_bytes() == before


def test_ambiguous_defaults_require_an_explicit_profile(isolated):
    first = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json")
    second = put(isolated.ctx.home / ".config/xyz.chatboxapp.ce/config.json")
    assert app(isolated)["mode"] == "guided"
    isolated.ctx.roots = [second.parent]
    assert chatbox.selected_config(isolated.ctx) == second
    result = isolated.apply(isolated.preview(KEY, ["chatbox"])["plan_id"])
    assert result["changes"][0]["path"] == str(second)
    assert "cometapi-connect" not in first.read_text()


def test_running_chatbox_blocks_preview_apply_and_restore_only_for_chatbox(isolated, monkeypatch):
    path = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json")
    before = path.read_bytes()
    monkeypatch.setattr(
        chatbox, "_process_names", lambda ctx: ["/Applications/Chatbox.app/Contents/MacOS/Chatbox"]
    )
    with pytest.raises(SetupError, match="Quit Chatbox"):
        isolated.preview(KEY, ["chatbox"])
    # Another selected app is not blocked by the unrelated desktop process.
    isolated.apply(isolated.preview(KEY, ["claude-code"])["plan_id"])
    monkeypatch.setattr(chatbox, "_process_names", lambda ctx: [])
    plan = isolated.preview(KEY, ["chatbox"])
    monkeypatch.setattr(chatbox, "_process_names", lambda ctx: ["Chatbox.exe"])
    with pytest.raises(SetupError, match="Quit Chatbox"):
        isolated.apply(plan["plan_id"])
    assert path.read_bytes() == before
    monkeypatch.setattr(chatbox, "_process_names", lambda ctx: [])
    result = isolated.apply(isolated.preview(KEY, ["chatbox"])["plan_id"])
    configured = path.read_bytes()
    monkeypatch.setattr(chatbox, "_process_names", lambda ctx: ["Chatbox Helper (Renderer)"])
    with pytest.raises(SetupError, match="Quit Chatbox"):
        isolated.restore(result["transaction_id"])
    assert path.read_bytes() == configured


def test_provider_id_collision_is_not_overwritten(isolated):
    doc = initial_config()
    doc["settings"]["customProviders"].append(
        {"id": "cometapi-connect", "name": "Unrelated", "type": "openai", "isCustom": True}
    )
    path = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json", doc)
    before = path.read_bytes()
    with pytest.raises(SetupError, match="different provider"):
        isolated.preview(KEY, ["chatbox"])
    assert path.read_bytes() == before


def test_known_comet_model_options_are_preserved_and_selected_model_is_unhidden(isolated):
    path = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json")
    isolated.apply(isolated.preview(KEY, ["chatbox"])["plan_id"])
    doc = json.loads(path.read_text())
    provider = doc["settings"]["providers"]["cometapi-connect"]
    provider["models"][0].update(nickname="My model", maxOutput=8192, capabilities=["vision"])
    provider["excludedModels"] = ["gpt-5.4-mini", "unrelated-hidden-model"]
    path.write_text(json.dumps(doc))
    isolated.apply(isolated.preview(KEY, ["chatbox"])["plan_id"])
    saved = json.loads(path.read_text())["settings"]["providers"]["cometapi-connect"]
    assert saved["models"][0]["nickname"] == "My model" and saved["models"][0]["maxOutput"] == 8192
    assert saved["models"][0]["capabilities"] == ["vision"]
    assert saved["excludedModels"] == ["unrelated-hidden-model"]


def test_changed_profile_after_preview_is_not_overwritten(isolated):
    path = put(isolated.ctx.home / ".config/xyz.chatboxapp.app/config.json")
    plan = isolated.preview(KEY, ["chatbox"])
    doc = json.loads(path.read_text())
    doc["settings"]["theme"] = "light"
    path.write_text(json.dumps(doc))
    newer = path.read_bytes()
    with pytest.raises(SetupError, match="changed after preview"):
        isolated.apply(plan["plan_id"])
    assert path.read_bytes() == newer


def test_unreadable_process_inventory_fails_closed(isolated, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private process details must not be displayed")

    # Use the real inventory wrapper with a simulated OS failure, not a fake app process.
    monkeypatch.setattr(chatbox.subprocess, "run", fail)
    with pytest.raises(SetupError, match="Use guided setup") as error:
        READ_PROCESS_NAMES(isolated.ctx)
    assert "private process details" not in str(error.value)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_unix_inventory_uses_names_and_blocks_only_chatbox(isolated, monkeypatch, platform):
    isolated.ctx.platform = platform
    output = ["/usr/bin/python3\n/usr/bin/NotChatbox\n"]
    calls = []

    def process_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=output[0])

    monkeypatch.setattr(chatbox.subprocess, "run", process_run)
    monkeypatch.setattr(chatbox, "_process_names", READ_PROCESS_NAMES)
    chatbox.require_closed(isolated.ctx)
    assert calls == [["/bin/ps", "-A", "-o", "comm="]]
    output[0] += (
        "/Applications/Chatbox.app/Contents/MacOS/Chatbox\n"
        if platform == "darwin"
        else "chatbox\n"
    )
    with pytest.raises(SetupError, match="Quit Chatbox"):
        chatbox.require_closed(isolated.ctx)


def test_windows_inventory_reads_tasklist_csv_without_collecting_arguments(isolated, monkeypatch):
    isolated.ctx.platform = "win32"
    isolated.ctx.env = {"SystemRoot": "C:\\Windows"}
    monkeypatch.setattr(chatbox, "Path", PureWindowsPath)
    output = [
        '"System","4","Services","0","148 K"\n"NotChatbox.exe","12","Console","1","1,024 K"\n'
    ]
    calls = []

    def process_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=output[0])

    monkeypatch.setattr(chatbox.subprocess, "run", process_run)
    monkeypatch.setattr(chatbox, "_process_names", READ_PROCESS_NAMES)
    chatbox.require_closed(isolated.ctx)
    assert calls == [["C:\\Windows\\System32\\tasklist.exe", "/FO", "CSV", "/NH"]]
    output[0] += '"Chatbox.exe","123","Console","1","15,000 K"\n'
    with pytest.raises(SetupError, match="Quit Chatbox"):
        chatbox.require_closed(isolated.ctx)
