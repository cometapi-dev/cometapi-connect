import json
from types import SimpleNamespace

import pytest

from cometapi_helper import credentials, jan
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine


def test_windows_uses_roaming_config_relocation(tmp_path):
    base = tmp_path / "roaming/Jan"
    base.mkdir(parents=True)
    data = tmp_path / "relocated"
    (base / "settings.json").write_text(json.dumps({"data_folder": str(data)}))
    ctx = Context(
        home=tmp_path, platform="win32", env={"APPDATA": str(base.parent)}, use_path=False
    )
    assert jan.settings_path(ctx) == data / "settings.json"


def test_windows_running_jan_is_rejected(tmp_path, monkeypatch):
    ctx = Context(home=tmp_path, platform="win32", env={}, use_path=False)
    monkeypatch.setattr(
        jan.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout='"Jan.exe","123","Console","1","1 K"\n'
        ),
    )
    with pytest.raises(SetupError, match="Quit Jan"):
        jan.require_closed(ctx)


def test_windows_jan_engine_native_secret_transaction(tmp_path, monkeypatch):
    path = tmp_path / "jan/settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "theme": "kept",
                "model-provider": json.dumps(
                    {"version": 17, "state": {"providers": [], "deletedModels": []}}
                ),
            }
        )
    )

    class Native:
        value = None

        def __init__(self, account):
            assert account == jan.PROVIDER

        def read(self, target):
            assert target == "CometAPI Connect.jan-providers"
            return Native.value

        def write(self, target, value):
            assert target == "CometAPI Connect.jan-providers"
            Native.value = value

    monkeypatch.setattr(credentials, "WindowsGenericPassword", Native)
    monkeypatch.setattr(jan, "require_closed", lambda ctx: None)
    engine = Engine(
        Context(home=tmp_path, platform="win32", env={}, roots=[path.parent], use_path=False)
    )
    before = path.read_bytes()
    key = "sk-test-windows-jan-secret"
    plan = engine.preview(key, ["jan"])
    assert key not in json.dumps(plan)
    result = engine.apply(plan["plan_id"])
    assert key not in path.read_text()
    assert Native.value == credentials.encode(jan.PROVIDER, json.dumps([key]).encode())
    assert all(c["action"] == "unchanged" for c in engine.preview(key, ["jan"])["changes"])
    engine.restore(result["transaction_id"])
    assert path.read_bytes() == before and Native.value is None


def test_windows_jan_removes_only_local_samplers_transactionally(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = profile / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model-provider": json.dumps(
                    {"version": 17, "state": {"providers": [], "deletedModels": []}}
                )
            }
        )
    )
    assistant = profile / "assistants/jan/assistant.json"
    assistant.parent.mkdir(parents=True)
    assistant.write_text(
        json.dumps(
            {
                "id": "jan",
                "object": "assistant",
                "instructions": "preserved",
                "tools": [{"type": "retrieval"}],
                "parameters": {
                    "top_k": 20,
                    "repeat_penalty": 1.12,
                    "temperature": 0.7,
                    "top_p": 0.8,
                },
            }
        )
    )
    before = assistant.read_bytes()

    class Native:
        value = None

        def __init__(self, account):
            pass

        def read(self, target):
            return Native.value

        def write(self, target, value):
            Native.value = value

    monkeypatch.setattr(credentials, "WindowsGenericPassword", Native)
    monkeypatch.setattr(jan, "require_closed", lambda ctx: None)
    engine = Engine(
        Context(home=tmp_path, platform="win32", env={}, roots=[profile], use_path=False)
    )
    plan = engine.preview("sk-test-jan-samplers", ["jan"])
    result = engine.apply(plan["plan_id"])
    doc = json.loads(assistant.read_text())
    assert doc["parameters"] == {"temperature": 0.7, "top_p": 0.8}
    assert doc["instructions"] == "preserved"
    assert doc["tools"] == [{"type": "retrieval"}]
    assert all(
        c["action"] == "unchanged"
        for c in engine.preview("sk-test-jan-samplers", ["jan"])["changes"]
    )
    engine.restore(result["transaction_id"])
    assert assistant.read_bytes() == before


def test_isolated_backend_uses_desktop_assistant_path(tmp_path):
    profile = tmp_path / "backend"
    profile.mkdir()
    (profile / "settings.json").write_text(
        json.dumps({"model-provider": json.dumps({"version": 17, "state": {"providers": []}})})
    )
    base = tmp_path / "roaming/Jan"
    base.mkdir(parents=True)
    desktop = tmp_path / "desktop-data"
    assistant = desktop / "assistants/jan/assistant.json"
    assistant.parent.mkdir(parents=True)
    assistant.write_text("{}")
    (base / "settings.json").write_text(json.dumps({"data_folder": str(desktop)}))
    ctx = Context(
        home=tmp_path,
        platform="win32",
        roots=[profile],
        env={"APPDATA": str(base.parent), "JAN_DATA_FOLDER": str(profile)},
        use_path=False,
    )
    assert jan.assistant_path(ctx) == assistant
    ctx.env.pop("JAN_DATA_FOLDER")
    assert (
        jan.assistant_path(ctx) is None
    )  # An explicit unrelated profile must not alter the default profile.
