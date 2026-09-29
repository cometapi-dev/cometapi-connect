import json
import sqlite3

import pytest

from cometapi_helper import webui_store
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-test_webui_configuration_123456"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "webui.db"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE config(key TEXT PRIMARY KEY NOT NULL,value JSON NOT NULL,updated_at BIGINT)"
        )
        c.execute("CREATE TABLE chat(id TEXT PRIMARY KEY,content TEXT)")
        c.execute("INSERT INTO chat VALUES('existing-chat','preserve private conversation')")
        for key, value in {
            "openai.enable": False,
            "openai.api_base_urls": ["https://existing.example/v1"],
            "openai.api_keys": ["old-secret"],
            "openai.api_configs": {"0": {"prefix_id": "keep", "model_ids": ["existing-model"]}},
            "auth.enable_signup": False,
        }.items():
            c.execute("INSERT INTO config VALUES(?,?,0)", (key, json.dumps(value)))
    return path


def test_webui_transaction_changes_only_connection_rows_and_restores(database):
    target = webui_store.resource(database, images=True)
    before = webui_store.read(target)
    engine = Engine(
        Context(
            home=database.parent, platform="darwin", env={}, roots=[database.parent], use_path=False
        )
    )
    plan = engine.preview(KEY, ["open-webui"], {"chat_model": "gpt-4.1-mini"})
    assert KEY not in json.dumps(plan)
    applied = engine.apply(plan["plan_id"])
    doc = json.loads(webui_store.read(target))
    assert doc["openai.api_keys"] == ["old-secret", KEY]
    assert doc["openai.api_configs"]["0"] == {"prefix_id": "keep", "model_ids": ["existing-model"]}
    assert doc["ui.default_models"] == "cometapi-connect.gpt-4.1-mini"
    assert doc["image_generation.enable"] is True
    assert doc["image_generation.model"] == "gpt-image-1.5"
    assert doc["image_generation.openai.api_key"] == KEY
    assert all(
        c["action"] == "unchanged"
        for c in engine.preview(KEY, ["open-webui"], {"chat_model": "gpt-4.1-mini"})["changes"]
    )
    # Activity in unrelated rows must survive restoring connection settings.
    with sqlite3.connect(database) as c:
        c.execute("INSERT INTO chat VALUES('new-chat','new conversation')")
        c.execute("UPDATE config SET value='true' WHERE key='auth.enable_signup'")
    engine.restore(applied["transaction_id"])
    assert webui_store.read(target) == before
    with sqlite3.connect(database) as c:
        assert c.execute("SELECT COUNT(*) FROM chat").fetchone()[0] == 2
        assert (
            c.execute("SELECT value FROM config WHERE key='auth.enable_signup'").fetchone()[0]
            == "true"
        )


def test_webui_legacy_backup_never_captures_or_deletes_image_settings(database):
    target = webui_store.resource(database)
    before = webui_store.read(target)
    with sqlite3.connect(database) as c:
        c.execute(
            "INSERT INTO config VALUES('image_generation.model',?,0)",
            (json.dumps("user-changed-image-model"),),
        )
    assert webui_store.read(target) == before
    webui_store.write(target, before, before)
    with sqlite3.connect(database) as c:
        assert (
            json.loads(
                c.execute("SELECT value FROM config WHERE key='image_generation.model'").fetchone()[
                    0
                ]
            )
            == "user-changed-image-model"
        )


def test_webui_image_change_blocks_restoring_over_later_user_choice(database):
    engine = Engine(
        Context(
            home=database.parent, platform="darwin", env={}, roots=[database.parent], use_path=False
        )
    )
    applied = engine.apply(
        engine.preview(KEY, ["open-webui"], {"image_model": "gpt-image-1"})["plan_id"]
    )
    with sqlite3.connect(database) as c:
        c.execute(
            "UPDATE config SET value=? WHERE key='image_generation.model'",
            (json.dumps("user-choice"),),
        )
    before = webui_store.read(webui_store.resource(database, images=True))
    with pytest.raises(SetupError):
        engine.restore(applied["transaction_id"])
    assert webui_store.read(webui_store.resource(database, images=True)) == before


def test_webui_atomic_compare_prevents_overwriting_newer_connection(database):
    target = webui_store.resource(database)
    before = webui_store.read(target)
    after = webui_store.prepare_value(before, KEY, "gpt-4.1-mini")
    with sqlite3.connect(database) as c:
        c.execute("UPDATE config SET value='true' WHERE key='openai.enable'")
    current = webui_store.read(target)
    with pytest.raises(SetupError, match="changed during setup"):
        webui_store.write(target, after, before)
    assert webui_store.read(target) == current


def test_webui_refuses_unknown_schema_and_unrelated_row_targets(tmp_path, database):
    target = webui_store.resource(database)
    with pytest.raises(SetupError, match="Invalid"):
        webui_store.write(target, b'{"auth.enable_signup":true}', webui_store.read(target))
    with sqlite3.connect(database) as c:
        c.execute("ALTER TABLE config ADD COLUMN unknown TEXT")
    with pytest.raises(SetupError, match="unsupported"):
        webui_store.read(target)


def test_webui_does_not_replace_unpersisted_environment_connections():
    with pytest.raises(SetupError, match="not persisted"):
        webui_store.prepare_value(b"{}", KEY, "gpt-4.1-mini")


def test_webui_existing_prefix_for_other_endpoint_is_not_reused(database):
    target = webui_store.resource(database)
    doc = json.loads(webui_store.read(target))
    doc["openai.api_configs"]["0"]["prefix_id"] = webui_store.PREFIX
    with pytest.raises(SetupError, match="another connection"):
        webui_store.prepare_value(webui_store.encode(doc), KEY, "gpt-4.1-mini")
