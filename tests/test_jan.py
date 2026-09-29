import json

import pytest

from cometapi_helper import credentials, jan
from cometapi_helper.common import BASE_URL, Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-test_jan_native_store_123456"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "jan-data/settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "theme": "keep",
                "model-provider": json.dumps(
                    {
                        "version": 17,
                        "state": {
                            "providers": [
                                {
                                    "provider": "other",
                                    "base_url": "https://example.com",
                                    "models": [],
                                }
                            ],
                            "selectedProvider": "other",
                            "selectedModel": None,
                            "deletedModels": ["hidden"],
                        },
                    }
                ),
            }
        )
    )

    class Native:
        value = None

        def __init__(self, account):
            assert account == jan.PROVIDER

        def read(self, service):
            assert service == "jan-providers"
            return Native.value

        def write(self, service, value):
            assert service == "jan-providers"
            Native.value = value

    monkeypatch.setattr(credentials, "MacGenericPassword", Native)
    monkeypatch.setattr(jan, "require_closed", lambda ctx: None)
    engine = Engine(
        Context(home=tmp_path, platform="darwin", env={}, roots=[path.parent], use_path=False)
    )
    return engine, path, Native


def test_jan_native_transaction_preserves_nonsecret_state_and_restores(profile):
    engine, path, native = profile
    before = path.read_bytes()
    plan = engine.preview(KEY, ["jan"], {"chat_model": "gpt-4.1-mini"})
    assert KEY not in json.dumps(plan)
    result = engine.apply(plan["plan_id"])
    doc = json.loads(path.read_text())
    state = json.loads(doc["model-provider"])["state"]
    assert doc["theme"] == "keep" and state["providers"][0]["provider"] == "other"
    assert state["selectedProvider"] == jan.PROVIDER
    assert state["selectedModel"]["id"] == "gpt-4.1-mini"
    assert state["providers"][1]["base_url"] == BASE_URL
    assert KEY not in path.read_text()
    assert native.value == credentials.encode(jan.PROVIDER, json.dumps([KEY]).encode())
    assert all(
        c["action"] == "unchanged"
        for c in engine.preview(KEY, ["jan"], {"chat_model": "gpt-4.1-mini"})["changes"]
    )
    engine.restore(result["transaction_id"])
    assert path.read_bytes() == before and native.value is None


def test_jan_live_app_blocks_both_targets(profile, monkeypatch):
    engine, path, native = profile
    before = path.read_bytes()
    plan = engine.preview(KEY, ["jan"])

    def denied(ctx):
        raise SetupError("Quit Jan")

    monkeypatch.setattr(jan, "require_closed", denied)
    with pytest.raises(SetupError, match="Quit Jan"):
        engine.apply(plan["plan_id"])
    assert path.read_bytes() == before and native.value is None


@pytest.mark.parametrize("version", ["17", None, True])
def test_jan_unknown_store_is_not_offered_as_automatic(profile, version):
    engine, path, _ = profile
    doc = json.loads(path.read_text())
    blob = json.loads(doc["model-provider"])
    blob["version"] = version
    doc["model-provider"] = json.dumps(blob)
    path.write_text(json.dumps(doc))
    entry = next(a for a in engine.scan()["apps"] if a["id"] == "jan")
    assert entry["mode"] == "guided"
    with pytest.raises(SetupError):
        engine.preview(KEY, ["jan"])


def test_jan_foreign_provider_name_collision_is_preserved(profile):
    engine, path, native = profile
    doc = json.loads(path.read_text())
    blob = json.loads(doc["model-provider"])
    blob["state"]["providers"][0]["provider"] = jan.PROVIDER
    doc["model-provider"] = json.dumps(blob)
    path.write_text(json.dumps(doc))
    before = path.read_bytes()
    with pytest.raises(SetupError, match="another endpoint"):
        engine.preview(KEY, ["jan"])
    assert path.read_bytes() == before and native.value is None


@pytest.mark.parametrize("version", [16, 18, 999])
def test_compatible_jan_store_preserves_version(profile, version):
    engine, path, _ = profile
    doc = json.loads(path.read_text())
    blob = json.loads(doc["model-provider"])
    blob["version"] = version
    doc["model-provider"] = json.dumps(blob)
    path.write_text(json.dumps(doc))
    original = path.read_bytes()
    tx = engine.apply(engine.preview(KEY, ["jan"])["plan_id"])
    assert json.loads(json.loads(path.read_text())["model-provider"])["version"] == version
    engine.restore(tx["transaction_id"])
    assert path.read_bytes() == original
