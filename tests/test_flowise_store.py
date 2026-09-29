import json
import sqlite3

import pytest

from cometapi_helper import flowise_store as store
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

SECRET = "fake-flowise-encryption-key-test"
KEY = "sk-flowise_configuration_test_12345"
# Generated independently by the installed official Flowise dependency CryptoJS.AES.
NATIVE = "U2FsdGVkX19hp/SzIJdydYDaXJGUPgbRj4xnGIbXEuxvQM3dmlP0vTbVoXCeLX2ZCTnuvCGgRdqIihXCQbDlBg=="


@pytest.fixture
def database(tmp_path):
    root = tmp_path / ".flowise"
    root.mkdir()
    (root / "encryption.key").write_text(SECRET)
    path = root / "database.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript("""
        CREATE TABLE user(id TEXT,status TEXT);
        CREATE TABLE workspace(id TEXT,organizationId TEXT);
        CREATE TABLE organization(id TEXT);
        CREATE TABLE workspace_user(workspaceId TEXT,userId TEXT,status TEXT);
        CREATE TABLE credential(id TEXT PRIMARY KEY,credentialName TEXT,encryptedData TEXT,workspaceId TEXT,updatedDate TEXT);
        CREATE TABLE chat_flow(id TEXT PRIMARY KEY,flowData TEXT,workspaceId TEXT,updatedDate TEXT,type TEXT);
        INSERT INTO user VALUES('u','active');
        INSERT INTO workspace VALUES('w','o');
        INSERT INTO organization VALUES('o');
        INSERT INTO workspace_user VALUES('w','u','active');
        """)
        c.execute("INSERT INTO credential VALUES(?,?,?,?,NULL)", ("c", "openAIApi", NATIVE, "w"))
        c.execute(
            "INSERT INTO credential VALUES(?,?,?,?,NULL)",
            ("other", "otherProvider", "keep this untouched", "w"),
        )
        flow = {
            "nodes": [
                {
                    "id": "chat",
                    "data": {
                        "name": "chatOpenAI",
                        "version": 6,
                        "credential": "c",
                        "inputs": {
                            "modelName": "gpt-4o-mini",
                            "basepath": "https://api.openai.com/v1",
                            "temperature": 0,
                        },
                    },
                },
                {
                    "id": "chain",
                    "data": {
                        "name": "conversationChain",
                        "inputs": {"model": "{{chat.data.instance}}"},
                    },
                },
            ],
            "edges": [{"source": "chat", "target": "chain"}],
        }
        c.execute(
            "INSERT INTO chat_flow VALUES(?,?,?,NULL,?)",
            ("flow", json.dumps(flow), "w", "CHATFLOW"),
        )
    return path


def engine(database):
    return Engine(Context(home=database.parent.parent, env={}, use_path=False))


def test_native_crypto_configuration_idempotence_and_exact_restore(database):
    target = store.resource(database)
    before = store.read(target)
    assert json.loads(store.crypt(NATIVE, SECRET, True)) == {"openAIApiKey": "old-flowise-fake-key"}
    e = engine(database)
    p = e.preview(KEY, ["flowise"])
    assert KEY not in json.dumps(p)
    tx = e.apply(p["plan_id"])
    after = json.loads(store.read(target))
    assert json.loads(store.crypt(after["credentials"]["c"], SECRET, True)) == {"openAIApiKey": KEY}
    original = json.loads(json.loads(before)["flows"]["flow"])
    flow = json.loads(after["flows"]["flow"])
    original["nodes"][0]["data"]["inputs"]["basepath"] = "https://api.cometapi.com/v1"
    assert flow == original
    assert all(c["action"] == "unchanged" for c in e.preview(KEY, ["flowise"])["changes"])
    e.restore(tx["transaction_id"])
    assert store.read(target) == before
    with sqlite3.connect(database) as c:
        assert (
            c.execute("SELECT encryptedData FROM credential WHERE id='other'").fetchone()[0]
            == "keep this untouched"
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "INSERT INTO user VALUES('u2','active')",
        "UPDATE workspace_user SET userId='another'",
        "UPDATE credential SET workspaceId='other-workspace' WHERE id='c'",
    ],
)
def test_account_scope_is_checked_before_writes(database, mutation):
    with sqlite3.connect(database) as c:
        c.execute(mutation)
    e = engine(database)
    assert next(a for a in e.scan()["apps"] if a["id"] == "flowise")["mode"] == "guided"
    with pytest.raises(SetupError):
        e.preview(KEY, ["flowise"])


def test_custom_headers_and_shared_unsupported_nodes_are_rejected(database):
    target = store.resource(database)
    for shared in (False, True):
        doc = json.loads(store.read(target))
        flow = json.loads(doc["flows"]["flow"])
        if shared:
            flow["nodes"].append({"data": {"name": "openAIEmbeddings", "credential": "c"}})
        else:
            flow["nodes"][0]["data"]["inputs"]["baseOptions"] = '{"Authorization":"fake"}'
        doc["flows"]["flow"] = json.dumps(flow)
        with pytest.raises(SetupError):
            store.prepare_value(target, store.encode(doc), KEY)


def test_later_flow_edits_and_encryption_key_change_block_restore(database):
    e = engine(database)
    tx = e.apply(e.preview(KEY, ["flowise"])["plan_id"])
    with sqlite3.connect(database) as c:
        flow = json.loads(c.execute("SELECT flowData FROM chat_flow").fetchone()[0])
        flow["user_edit"] = "preserve"
        c.execute("UPDATE chat_flow SET flowData=?", (json.dumps(flow),))
    with pytest.raises(SetupError):
        e.restore(tx["transaction_id"])
    t = store.resource(database)
    (database.parent / "encryption.key").write_text("different-flowise-encryption-key")
    with pytest.raises(SetupError, match="key changed"):
        store.read(t)


def test_new_node_version_preserves_fields_and_restore(database):
    with sqlite3.connect(database) as c:
        flow = json.loads(c.execute("SELECT flowData FROM chat_flow WHERE id='flow'").fetchone()[0])
        flow["nodes"][0]["data"]["version"] = 99
        c.execute("UPDATE chat_flow SET flowData=? WHERE id='flow'", (json.dumps(flow),))
    target = store.resource(database)
    before = store.read(target)
    e = engine(database)
    tx = e.apply(e.preview(KEY, ["flowise"])["plan_id"])
    after = json.loads(store.read(target))
    assert json.loads(after["flows"]["flow"])["nodes"][0]["data"]["version"] == 99
    e.restore(tx["transaction_id"])
    assert store.read(target) == before
