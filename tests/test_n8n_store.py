import json
import sqlite3

import pytest

from cometapi_helper import n8n_store
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-n8n_configuration_test_123456"
SECRET = "fake-n8n-encryption-key-for-tests"
# Produced by the installed official n8n-core 2.38.1 CipherAes256CBC, not this implementation.
NATIVE = "U2FsdGVkX1+0fO8/JG5DyYQJbRXdkSOMrUd1eOKFvKwqYUg1XShm6KCRVlkLeSDb1AwSvUvtWcJmRzkwjVkaJgQ/9puspv2BUpNEs9hfqwuldlj3l7L7klT78nISkKRx+rVrC84Jq4ssT7437vJ5jA=="


@pytest.fixture
def database(tmp_path):
    root = tmp_path / ".n8n"
    root.mkdir()
    (root / "config").write_text(json.dumps({"encryptionKey": SECRET}))
    path = root / "database.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript("""
        CREATE TABLE credentials_entity(id TEXT PRIMARY KEY,name TEXT,data TEXT,type TEXT,updatedAt TEXT,
          isManaged INTEGER DEFAULT 0,isGlobal INTEGER DEFAULT 0,isResolvable INTEGER DEFAULT 0,
          resolvableAllowFallback INTEGER DEFAULT 0,resolverId TEXT,usageScope TEXT DEFAULT 'project');
        CREATE TABLE shared_credentials(credentialsId TEXT,projectId TEXT,role TEXT);
        CREATE TABLE project(id TEXT,type TEXT);
        CREATE TABLE user(id TEXT);
        CREATE TABLE workflow_entity(id TEXT,nodes TEXT);
        INSERT INTO project VALUES('p','personal');
        INSERT INTO user VALUES('u');
        INSERT INTO shared_credentials VALUES('credential-one','p','credential:owner');
        INSERT INTO workflow_entity VALUES('workflow-one','keep existing node credential references');
        """)
        c.execute(
            "INSERT INTO credentials_entity(id,name,data,type) VALUES(?,?,?,?)",
            ("credential-one", "Existing credential", NATIVE, "openAiApi"),
        )
        c.execute(
            "INSERT INTO credentials_entity(id,name,data,type) VALUES(?,?,?,?)",
            ("unrelated", "Other provider", "not touched", "otherApi"),
        )
    return path


def engine(database):
    return Engine(Context(home=database.parent.parent, env={}, use_path=False))


def test_native_cipher_and_transaction_preserve_bindings_restore_ciphertext(database):
    target = n8n_store.resource(database)
    before = n8n_store.read(target)
    original = json.loads(n8n_store.crypt(NATIVE, SECRET, decrypt=True))
    assert original["apiKey"] == "old-fake-key"
    e = engine(database)
    plan = e.preview(KEY, ["n8n"])
    assert KEY not in json.dumps(plan) and SECRET not in json.dumps(plan)
    tx = e.apply(plan["plan_id"])
    ciphertext = json.loads(n8n_store.read(target))["credential-one"]
    data = json.loads(n8n_store.crypt(ciphertext, SECRET, decrypt=True))
    assert data == {"apiKey": KEY, "url": "https://api.cometapi.com/v1", "organizationId": ""}
    assert all(x["action"] == "unchanged" for x in e.preview(KEY, ["n8n"])["changes"])
    with sqlite3.connect(database) as c:
        c.execute("INSERT INTO workflow_entity VALUES('new','created after configuration')")
    e.restore(tx["transaction_id"])
    assert n8n_store.read(target) == before
    with sqlite3.connect(database) as c:
        assert (
            c.execute("SELECT data FROM credentials_entity WHERE id='unrelated'").fetchone()[0]
            == "not touched"
        )
        assert c.execute("SELECT COUNT(*) FROM workflow_entity").fetchone()[0] == 2


def test_later_credential_edit_and_key_rotation_block_restore(database):
    e = engine(database)
    tx = e.apply(e.preview(KEY, ["n8n"])["plan_id"])
    with sqlite3.connect(database) as c:
        c.execute("UPDATE credentials_entity SET data=? WHERE id='credential-one'", (NATIVE,))
    with pytest.raises(SetupError):
        e.restore(tx["transaction_id"])
    target = n8n_store.resource(database)
    (database.parent / "config").write_text(
        json.dumps({"encryptionKey": "a-different-encryption-key-now"})
    )
    with pytest.raises(SetupError, match="key changed"):
        n8n_store.read(target)


@pytest.mark.parametrize(
    "mutation",
    [
        "INSERT INTO user VALUES('second')",
        "UPDATE shared_credentials SET projectId='other-project'",
        "UPDATE credentials_entity SET isManaged=1 WHERE id='credential-one'",
        "UPDATE credentials_entity SET isResolvable=1 WHERE id='credential-one'",
    ],
)
def test_shared_or_managed_installations_are_not_silently_reconfigured(database, mutation):
    with sqlite3.connect(database) as c:
        c.execute(mutation)
    e = engine(database)
    assert next(x for x in e.scan()["apps"] if x["id"] == "n8n")["mode"] == "guided"
    with pytest.raises(SetupError):
        e.preview(KEY, ["n8n"])


def test_auth_header_and_dynamic_expression_are_rejected(database):
    target = n8n_store.resource(database)
    for value in (
        {"header": True, "headerName": "Authorization"},
        {"apiKey": "={{$secrets.vault.key}}"},
    ):
        before = n8n_store.encode(
            {"credential-one": n8n_store.crypt(n8n_store.encode(value), SECRET)}
        )
        with pytest.raises(SetupError):
            n8n_store.prepare_value(target, before, KEY)
