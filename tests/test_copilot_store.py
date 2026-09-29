import hashlib
import json
import time

import pytest

from cometapi_helper import copilot_store as c
from cometapi_helper.adapters import Change
from cometapi_helper.common import Context, SetupError


def before(groups=None, stamp=None, selection=None):
    return c.encode(
        {
            "schema": 1,
            "models": c.pack(c.encode(groups or [])),
            "settings": None,
            "stamp": c.pack(c.encode(stamp)) if stamp else None,
            "selection": selection,
        }
    )


def test_unknown_group_and_duplicate_model_ids_preserved():
    for group in (
        {"name": c.NAME, "vendor": "customendpoint"},
        {"name": "Other", "vendor": "customendpoint", "models": [{"id": "gpt-4o-mini"}]},
    ):
        raw = before([group])
        with pytest.raises(SetupError):
            c.prepare_value(raw, "sk-fixture-key", "gpt-4o-mini")


def test_native_plan_never_contains_plaintext_config_file():
    original = before([{"name": "Original", "vendor": "openai", "apiKey": "${input:existing}"}])
    plan = json.loads(c.prepare_value(original, "sk-fixture-key", "gpt-4o-mini"))
    assert plan["operation"] == "configure" and plan["key"] == "sk-fixture-key"
    assert plan["model"]["url"] == "https://api.cometapi.com/v1/chat/completions"
    assert "apiKey" not in plan["model"]


def test_idempotence_requires_key_group_selection_and_utility_match():
    model = "gpt-4o-mini"
    key = "sk-fixture-key"
    group = {
        "name": c.NAME,
        "vendor": "customendpoint",
        "apiKey": "${input:chat.lm.secret.native}",
        "models": [c.model_config(model)],
    }
    stamp = {
        "group_sha256": hashlib.sha256(c.encode(group)).hexdigest(),
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "selection": "custom/id",
    }
    snapshot = json.loads(before([group], stamp, "custom/id"))
    qualified = c.model_config(model)["name"] + " (customendpoint)"
    snapshot["settings"] = c.pack(
        c.encode({"chat.utilityModel": qualified, "chat.utilitySmallModel": qualified})
    )
    raw = c.encode(snapshot)
    assert c.prepare_value(raw, key, model) == raw
    assert c.prepare_value(raw, "sk-another-key", model) != raw
    snapshot["selection"] = "different"
    assert c.prepare_value(c.encode(snapshot), key, model) != c.encode(snapshot)


def test_resource_platform_and_identity_rejected(tmp_path):
    with pytest.raises(SetupError):
        c.find(Context(home=tmp_path, platform="win32"))
    with pytest.raises(SetupError):
        c.validate({"kind": c.KIND, "database": "relative"})


def test_engine_records_materialized_native_state_and_restores(tmp_path, monkeypatch):
    from cometapi_helper import engine as module
    from cometapi_helper.storage import digest

    engine = module.Engine(Context(home=tmp_path, env={}, use_path=False))
    original = b"original"
    actual = b"native-ref-123"
    state = [original]
    target = tmp_path / "state.vscdb"
    change = Change(
        "github-copilot",
        "Copilot",
        target,
        original,
        b"pending-plan",
        [],
        resource={"kind": c.KIND, "database": str(target)},
    )
    engine.plans["fixture"] = (time.monotonic(), [change])
    monkeypatch.setattr(module, "read_target", lambda *a: state[0])

    def write(path, value, *args, **kwargs):
        state[0] = actual if value == b"pending-plan" else value
        return state[0]

    monkeypatch.setattr(module, "write_target", write)
    tx = engine.apply("fixture")["transaction_id"]
    manifest = json.loads((engine.ctx.state_dir / "backups" / tx / "manifest.json").read_text())
    assert manifest["entries"][0]["after_sha256"] == digest(actual)
    engine.restore(tx)
    assert state[0] == original


def test_restore_removes_only_new_helper_files_and_preserves_other_database_rows(tmp_path):
    import sqlite3

    profile = tmp_path / "profile"
    user = profile / "User"
    (user / "globalStorage").mkdir(parents=True)
    database = user / "globalStorage/state.vscdb"
    target = {"profile": str(profile), "database": str(database)}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE ItemTable(key TEXT PRIMARY KEY,value BLOB)")
        db.executemany(
            "INSERT INTO ItemTable VALUES (?,?)",
            [(c.SELECTION, "new-selection"), ("unrelated", "keep")],
        )
    for path in c.paths(target).values():
        path.write_bytes(b"new")
    snapshot = {
        "models": c.pack(b"[ /* original */ ]"),
        "settings": None,
        "stamp": None,
        "selection": None,
    }
    c.restore_files(target, snapshot)
    assert (user / "chatLanguageModels.json").read_bytes() == b"[ /* original */ ]"
    assert (
        not (user / "settings.json").exists()
        and not (user / "cometapi-connect-copilot.json").exists()
    )
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM ItemTable").fetchall() == [("unrelated", "keep")]
