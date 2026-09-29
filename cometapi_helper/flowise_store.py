"""Flowise 3.1 SQLite: existing ChatOpenAI nodes and encrypted credentials."""

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

from .common import BASE_URL, SetupError
from .n8n_store import crypt as salted_aes_cbc
from .storage import MAX_FILE_BYTES, no_links, read_file

KIND = "flowise-sqlite-chatopenai-v1"


def encode(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > MAX_FILE_BYTES:
        raise SetupError("Flowise settings exceed the supported size.")
    return data


def key_at(path):
    try:
        value = (read_file(path) or b"").decode("ascii")
    except UnicodeError:
        raise SetupError("This Flowise encryption key format is not supported.") from None
    if not 16 <= len(value) <= 1024:
        raise SetupError("Flowise needs its existing local encryption.key.")
    return value


def crypt(value, key, decrypt=False):
    # CryptoJS.AES uses the same Salted__/MD5 EVP derivation as n8n CBC.
    try:
        return salted_aes_cbc(value, key, decrypt)
    except SetupError:
        raise SetupError(
            "This Flowise credential cannot be decrypted with its local key."
        ) from None


def resource(path, key_path=None):
    key_path = key_path or path.parent / "encryption.key"
    return {
        "kind": KIND,
        "app": "flowise",
        "database": str(path),
        "key_file": str(key_path),
        "key_sha256": hashlib.sha256(key_at(key_path).encode()).hexdigest(),
    }


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "app", "database", "key_file", "key_sha256"}
        or target.get("kind") != KIND
        or target.get("app") != "flowise"
    ):
        raise SetupError("Unrecognized Flowise database target.")
    path, key_path = Path(target["database"]), Path(target["key_file"])
    if (
        not path.is_absolute()
        or path.name != "database.sqlite"
        or not key_path.is_absolute()
        or key_path.name != "encryption.key"
    ):
        raise SetupError("Flowise requires absolute paths to database.sqlite and encryption.key.")
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(path) + suffix)
        no_links(p)
        if p.exists() and (not stat.S_ISREG(p.stat().st_mode) or p.stat().st_nlink != 1):
            raise SetupError("Flowise database files must be regular files without links.")
    if hashlib.sha256(key_at(key_path).encode()).hexdigest() != target["key_sha256"]:
        raise SetupError("Flowise's encryption key changed; the original key is required.")
    return path


def connect(target, writable=False):
    path = validate(target)
    c = sqlite3.connect(
        path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=2
    )
    try:
        c.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
        for table, required in {
            "credential": {"id", "credentialName", "encryptedData", "workspaceId", "updatedDate"},
            "chat_flow": {"id", "flowData", "workspaceId", "updatedDate", "type"},
        }.items():
            cols = c.execute("PRAGMA table_info(" + table + ")").fetchall()
            if not required.issubset({r[1] for r in cols}) or not any(
                r[1] == "id" and r[5] for r in cols
            ):
                raise SetupError("This Flowise database schema is not supported.")
        users = c.execute("SELECT id,status FROM user").fetchall()
        spaces = c.execute("SELECT id,organizationId FROM workspace").fetchall()
        orgs = c.execute("SELECT id FROM organization").fetchall()
        if (
            len(users) != 1
            or users[0][1] != "active"
            or len(spaces) != 1
            or orgs != [(spaces[0][1],)]
        ):
            raise SetupError(
                "Flowise automatic SQLite setup requires one active user and one workspace."
            )
        if c.execute("SELECT workspaceId,userId,status FROM workspace_user").fetchall() != [
            (spaces[0][0], users[0][0], "active")
        ]:
            raise SetupError("Flowise workspace membership does not match the local account.")
        for table in ("credential", "chat_flow"):
            if c.execute(
                "SELECT COUNT(*) FROM " + table + " WHERE workspaceId IS NULL OR workspaceId<>?",
                (spaces[0][0],),
            ).fetchone()[0]:
                raise SetupError("Flowise contains settings belonging to another workspace.")
        return c
    except Exception:
        c.close()
        raise


def snapshot(c):
    return encode(
        {
            "credentials": {
                r[0]: r[1]
                for r in c.execute(
                    "SELECT id,encryptedData FROM credential WHERE credentialName='openAIApi'"
                )
            },
            "flows": {r[0]: r[1] for r in c.execute("SELECT id,flowData FROM chat_flow")},
        }
    )


def read(target):
    try:
        c = connect(target)
        try:
            return snapshot(c)
        finally:
            c.close()
    except sqlite3.Error:
        raise SetupError("Cannot read Flowise's local database.") from None


def string_values(value):
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return set().union(*(string_values(v) for v in value))
    return set()


def prepare_value(target, before, api_key):
    validate(target)
    secret = key_at(Path(target["key_file"]))
    doc = json.loads(before)
    selected, other_uses = set(), set()
    for identity, raw in doc["flows"].items():
        try:
            flow = json.loads(raw)
            changed = False
            for node in flow["nodes"]:
                data = node["data"]
                inputs = data.get("inputs", {})
                credential = inputs.get("credentialId") or data.get("credential")
                if data.get("name") == "chatOpenAI":
                    if not isinstance(credential, str) or credential not in doc["credentials"]:
                        raise SetupError(
                            "A Flowise ChatOpenAI node needs an existing OpenAI credential."
                        )
                    if inputs.get("baseOptions") or inputs.get("openAIApiKey"):
                        raise SetupError("A Flowise node has custom headers or an inline key.")
                    if any(
                        isinstance(inputs.get(k), str) and "{{" in inputs[k]
                        for k in ("modelName", "basepath", "credentialId")
                    ):
                        raise SetupError(
                            "Dynamic Flowise model/endpoint/credential expressions need individual setup."
                        )
                    selected.add(credential)
                    if inputs.get("basepath") != BASE_URL:
                        inputs["basepath"] = BASE_URL
                        changed = True
                else:
                    # Do not change a key also used by an unadapted node/provider.
                    other_uses.update(set(doc["credentials"]) & string_values(node))
            if changed:
                doc["flows"][identity] = encode(flow).decode()
        except (KeyError, ValueError, TypeError):
            raise SetupError("Flowise contains an unsupported workflow format.") from None
    if not selected or selected & other_uses:
        raise SetupError(
            "Flowise needs existing ChatOpenAI flows whose credentials are not shared with other node types."
        )
    for identity in selected:
        data = json.loads(crypt(doc["credentials"][identity], secret, True))
        if not isinstance(data, dict) or set(data) != {"openAIApiKey"}:
            raise SetupError("This Flowise OpenAI credential has unsupported fields.")
        if data["openAIApiKey"] != api_key:
            doc["credentials"][identity] = crypt(encode({"openAIApiKey": api_key}), secret)
    return encode(doc)


def write(target, value, expected):
    try:
        doc = json.loads(value)
        if (
            not isinstance(doc, dict)
            or set(doc) != {"credentials", "flows"}
            or not all(
                isinstance(v, dict)
                and all(isinstance(k, str) and isinstance(s, str) for k, s in v.items())
                for v in doc.values()
            )
        ):
            raise ValueError()
        secret = key_at(Path(target["key_file"]))
        for ciphertext in doc["credentials"].values():
            if not isinstance(json.loads(crypt(ciphertext, secret, True)), dict):
                raise ValueError()
        for flow in doc["flows"].values():
            if not isinstance(json.loads(flow), dict):
                raise ValueError()
    except (ValueError, TypeError):
        raise SetupError("Invalid Flowise settings backup.") from None
    c = None
    try:
        c = connect(target, True)
        current = snapshot(c)
        original = json.loads(current)
        if current != expected or any(set(doc[k]) != set(original[k]) for k in doc):
            raise SetupError(
                "Flowise settings changed during setup; existing changes were preserved."
            )
        for key, table, column in (
            ("credentials", "credential", "encryptedData"),
            ("flows", "chat_flow", "flowData"),
        ):
            for identity, data in doc[key].items():
                if data != original[key][identity]:
                    c.execute(
                        "UPDATE "
                        + table
                        + " SET "
                        + column
                        + "=?,updatedDate=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                        (data, identity),
                    )
        c.commit()
    except sqlite3.Error:
        raise SetupError("Cannot update Flowise's local database.") from None
    finally:
        if c is not None:
            c.close()


def find(ctx):
    if (
        ctx.env.get("DATABASE_TYPE", "sqlite") != "sqlite"
        or ctx.env.get("SECRETKEY_STORAGE_TYPE", "local") != "local"
    ):
        raise SetupError("Flowise external databases and secret stores need separate adapters.")
    paths = [ctx.config_dir("DATABASE_PATH", ".flowise") / "database.sqlite"]
    for root in ctx.roots:
        paths.extend((root / "database.sqlite", root / ".flowise/database.sqlite"))
    paths = list(dict.fromkeys(p for p in paths if p.is_file()))
    if len(paths) != 1:
        raise SetupError("Select one initialized Flowise data directory.")
    key_path = (
        ctx.config_dir("SECRETKEY_PATH", ".flowise") / "encryption.key"
        if ctx.env.get("SECRETKEY_PATH")
        else paths[0].parent / "encryption.key"
    )
    if ctx.env.get("FLOWISE_SECRETKEY_OVERWRITE", key_at(key_path)) != key_at(key_path):
        raise SetupError("Flowise's environment key differs from its local encryption.key.")
    target = resource(paths[0], key_path)
    read(target)
    return target
