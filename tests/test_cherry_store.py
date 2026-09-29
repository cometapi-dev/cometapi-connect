import json

import pytest

from cometapi_helper import cherry_store as cherry
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

plyvel = cherry.binding()
KEY = "sk-fixture_cherry_configuration_only_123456"


def fixture(home):
    profile = home / "profile"
    path = profile / "Local Storage/leveldb"
    path.mkdir(parents=True)
    llm = {
        "providers": [{"id": "original", "apiKey": "old-private-key", "enabled": True}],
        "settings": {"other": "preserved"},
        "defaultModel": {"id": "previous", "provider": "original"},
    }
    doc = {
        "llm": json.dumps(llm),
        "_persist": '{"version":204,"rehydrated":true}',
        "assistants": json.dumps(
            {
                "defaultAssistant": {"id": "template"},
                "assistants": [{"id": "default", "name": "Original", "topics": []}],
            }
        ),
        "settings": '{"theme":"dark"}',
    }
    with plyvel.DB(str(path), create_if_missing=True) as db:
        db.put(b"VERSION", b"1")
        db.put(cherry.KEY, b"\x00" + json.dumps(doc).encode("utf-16-le"))
        db.put(b"_file://\x00\x01another-key", b"\x01keep-me")
        db.put(b"other-origin", b"keep-this-too")
    ctx = Context(home=home, platform="darwin", roots=[profile], env={}, use_path=False)
    return ctx, path


def test_engine_preview_no_db_write_and_restore_preserves_unrelated_changes(tmp_path):
    ctx, path = fixture(tmp_path)
    engine = Engine(ctx)
    before = {p: p.read_bytes() for p in path.iterdir()}
    preview = engine.preview(KEY, ["cherry-studio"])
    assert before == {p: p.read_bytes() for p in path.iterdir()}
    assert KEY not in json.dumps(preview)
    tx = engine.apply(preview["plan_id"])
    assert all(
        c["action"] == "unchanged" for c in engine.preview(KEY, ["cherry-studio"])["changes"]
    )
    assert KEY not in json.dumps(engine.history())
    with plyvel.DB(str(path)) as db:
        doc, llm = cherry.document(db)
        assert llm["providers"][0]["apiKey"] == KEY
        assert llm["providers"][1]["apiKey"] == "old-private-key"
        assert llm["defaultModel"]["provider"] == cherry.PROVIDER
        assert db.get(b"_file://\x00\x01another-key") == b"\x01keep-me"
        assert db.get(b"other-origin") == b"keep-this-too"
        assistants = json.loads(doc["assistants"])
        assert assistants["assistants"][0]["model"]["provider"] == cherry.PROVIDER
        assistants["assistants"][0]["topics"].append({"id": "new-conversation"})
        doc["assistants"] = json.dumps(assistants)
        doc["settings"] = '{"theme":"new-user-choice"}'
        db.put(cherry.KEY, b"\x00" + json.dumps(doc).encode("utf-16-le"))
    engine.restore(tx["transaction_id"])
    with plyvel.DB(str(path)) as db:
        doc, llm = cherry.document(db)
        assert doc["settings"] == '{"theme":"new-user-choice"}'
        assert llm["defaultModel"] == {"id": "previous", "provider": "original"}
        assert [p["id"] for p in llm["providers"]] == ["original"]
        assistants = json.loads(doc["assistants"])
        assert "model" not in assistants["assistants"][0]
        assert assistants["assistants"][0]["topics"] == [{"id": "new-conversation"}]
        assert db.get(b"_file://\x00\x01another-key") == b"\x01keep-me"
        total = sum(
            len(k) - len(cherry.PREFIX) + len(v) for k, v in db.iterator(prefix=cherry.PREFIX)
        )
        assert db.get(b"META:file://").endswith(b"\x10" + cherry.varint(total))


def test_native_database_lock_refuses_write(tmp_path):
    ctx, path = fixture(tmp_path)
    target = cherry.find(ctx)
    before = cherry.read(target)
    after = cherry.prepare_value(before, KEY, "gpt-4.1-mini")
    with plyvel.DB(str(path)):
        with pytest.raises(SetupError, match="locked"):
            cherry.write(target, after, before)
    assert cherry.read(target) == before


def test_later_provider_edits_block_restore(tmp_path):
    ctx, path = fixture(tmp_path)
    engine = Engine(ctx)
    tx = engine.apply(engine.preview(KEY, ["cherry-studio"])["plan_id"])
    with plyvel.DB(str(path)) as db:
        doc, llm = cherry.document(db)
        llm["providers"][0]["apiKey"] = "later-user-key"
        doc["llm"] = json.dumps(llm)
        db.put(cherry.KEY, b"\x00" + json.dumps(doc).encode("utf-16-le"))
    with pytest.raises(SetupError, match="changed"):
        engine.restore(tx["transaction_id"])
    assert b"later-user-key" in cherry.read(cherry.find(ctx))


def test_unsupported_schema_and_links_refused(tmp_path):
    ctx, path = fixture(tmp_path)
    with plyvel.DB(str(path)) as db:
        doc, _ = cherry.document(db)
        doc["_persist"] = '{"version":999,"rehydrated":false}'
        db.put(cherry.KEY, b"\x00" + json.dumps(doc).encode("utf-16-le"))
    with pytest.raises(SetupError, match="structure"):
        cherry.find(ctx)
    if __import__("os").name != "nt":
        (path / "linked").symlink_to(tmp_path / "outside")
        with pytest.raises(SetupError, match="symbolic"):
            cherry.database_files(path)


def test_new_cherry_store_version_is_preserved(tmp_path):
    ctx, path = fixture(tmp_path)
    with plyvel.DB(str(path)) as db:
        doc, _ = cherry.document(db)
        doc["_persist"] = '{"version":999,"rehydrated":true}'
        db.put(cherry.KEY, b"\x00" + json.dumps(doc).encode("utf-16-le"))
    target = cherry.find(ctx)
    before = cherry.read(target)
    after = cherry.prepare_value(before, "sk-fixture-new", "gpt-4.1-mini")
    cherry.write(target, after, before)
    with plyvel.DB(str(path)) as db:
        doc, _ = cherry.document(db)
        assert json.loads(doc["_persist"])["version"] == 999
    cherry.write(target, before, after)
    assert cherry.read(target) == before
