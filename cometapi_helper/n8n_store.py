"""Existing n8n OpenAI credentials in a single-user SQLite installation.

The ciphertext format matches n8n-core's CipherAes256CBC. Backups contain the
original ciphertext, never a decrypted copy of the credential database.
"""

import base64
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from .cbc_crypto import aes_cbc
from .common import BASE_URL, SetupError
from .storage import MAX_FILE_BYTES, no_links, read_file

KIND = "n8n-sqlite-openai-v1"
FLAGS = ("isManaged", "isGlobal", "isResolvable", "resolvableAllowFallback")


def encryption_key(path):
    try:
        value = json.loads(read_file(path) or b"{}")["encryptionKey"]
        if not isinstance(value, str) or not 16 <= len(value) <= 1024 or not value.isascii():
            raise ValueError()
        return value
    except (KeyError, ValueError, TypeError):
        raise SetupError("n8n's local encryption key is missing or unsupported.") from None


def crypt(data, key, decrypt=False):
    try:
        raw = base64.b64decode(data, validate=True) if decrypt else data
        if decrypt and (raw[:8] != b"Salted__" or len(raw) < 32 or len(raw) % 16):
            raise ValueError()
        salt = raw[8:16] if decrypt else os.urandom(8)
        password = key.encode("ascii") + salt
        first = hashlib.md5(password).digest()
        second = hashlib.md5(first + password).digest()
        iv = hashlib.md5(second + password).digest()
        if decrypt:
            return aes_cbc(raw[16:], first + second, iv, True)
        return base64.b64encode(b"Salted__" + salt + aes_cbc(raw, first + second, iv)).decode()
    except (ValueError, TypeError, UnicodeError):
        raise SetupError(
            "Cannot decrypt this n8n credential format with the local key. No credentials were changed."
        ) from None


def resource(path):
    key = encryption_key(path.parent / "config")
    return {
        "kind": KIND,
        "app": "n8n",
        "database": str(path),
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
    }


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "app", "database", "key_sha256"}
        or target.get("kind") != KIND
        or target.get("app") != "n8n"
    ):
        raise SetupError("Unrecognized n8n credential target.")
    path = Path(target["database"])
    if not path.is_absolute() or path.name != "database.sqlite":
        raise SetupError("n8n needs an absolute path to its initialized database.sqlite.")
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        no_links(candidate)
        if candidate.exists():
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SetupError("n8n database files must be regular files without links.")
    if not path.is_file():
        raise SetupError("n8n's initialized database was not found.")
    key = encryption_key(path.parent / "config")
    if hashlib.sha256(key.encode()).hexdigest() != target["key_sha256"]:
        raise SetupError("n8n's encryption key changed. Restore requires its original key.")
    return path


def connect(target, writable=False):
    path = validate(target)
    c = sqlite3.connect(
        path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=2
    )
    try:
        # Ownership and credential rows must belong to the same DB snapshot.
        c.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
        columns = c.execute("PRAGMA table_info(credentials_entity)").fetchall()
        required = {"id", "name", "type", "data", "updatedAt", "resolverId", "usageScope", *FLAGS}
        if not required.issubset({r[1] for r in columns}) or not any(
            r[1] == "id" and r[5] == 1 for r in columns
        ):
            raise SetupError("This n8n credential schema is not supported.")
        # A workspace/account picker is required for shared installations.
        projects = c.execute("SELECT id,type FROM project").fetchall()
        if (
            len(projects) != 1
            or projects[0][1] != "personal"
            or c.execute('SELECT COUNT(*) FROM "user"').fetchone()[0] != 1
        ):
            raise SetupError(
                "This n8n installation needs account-specific setup; automatic local setup supports one personal account."
            )
        rows = c.execute(
            "SELECT id,"
            + ",".join(FLAGS)
            + ",resolverId,usageScope FROM credentials_entity WHERE type=?",
            ("openAiApi",),
        ).fetchall()
        if not rows or len(rows) > 100:
            raise SetupError(
                "n8n needs 1–100 existing OpenAI credentials; no workflow credentials were invented."
            )
        for row in rows:
            if any(row[1:5]) or row[5] is not None or row[6] != "project":
                raise SetupError(
                    "Managed, shared or dynamic n8n credentials need a separate adapter."
                )
            shares = c.execute(
                "SELECT projectId,role FROM shared_credentials WHERE credentialsId=?", (row[0],)
            ).fetchall()
            if shares != [(projects[0][0], "credential:owner")]:
                raise SetupError(
                    "n8n credential ownership does not match the sole personal account."
                )
        return c
    except Exception:
        c.close()
        raise


def encode(doc):
    value = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    if len(value) > MAX_FILE_BYTES:
        raise SetupError("n8n's OpenAI credentials exceed the supported size.")
    return value


def snapshot(c):
    return encode(
        {
            r[0]: r[1]
            for r in c.execute("SELECT id,data FROM credentials_entity WHERE type='openAiApi'")
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
        raise SetupError("Cannot read n8n's credential database.") from None


def prepare_value(target, before, api_key):
    secret = encryption_key(validate(target).parent / "config")
    doc = json.loads(before)
    for identity, ciphertext in doc.items():
        try:
            data = json.loads(crypt(ciphertext, secret, decrypt=True))
            if not isinstance(data, dict):
                raise ValueError()
            # Custom headers can override Authorization or disclose another key.
            if data.get("header") or any(
                isinstance(v, str) and v.startswith("=") for v in data.values()
            ):
                raise SetupError(
                    "n8n credentials with custom headers or expressions need individual configuration."
                )
            if (
                data.get("apiKey") == api_key
                and data.get("url") == BASE_URL
                and not data.get("organizationId")
            ):
                continue
            data.update(apiKey=api_key, url=BASE_URL, organizationId="")
            doc[identity] = crypt(encode(data), secret)
        except (ValueError, TypeError):
            raise SetupError(
                "n8n's decrypted OpenAI credential is not a supported object."
            ) from None
    return encode(doc)


def write(target, value, expected):
    try:
        doc = json.loads(value)
        if not isinstance(doc, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in doc.items()
        ):
            raise ValueError()
        secret = encryption_key(validate(target).parent / "config")
        for ciphertext in doc.values():
            if not isinstance(json.loads(crypt(ciphertext, secret, decrypt=True)), dict):
                raise ValueError()
    except (ValueError, TypeError):
        raise SetupError("Invalid n8n credential backup.") from None
    c = None
    try:
        c = connect(target, writable=True)
        current = snapshot(c)
        if current != expected or set(json.loads(current)) != set(doc):
            raise SetupError(
                "n8n credentials changed during setup. Existing changes were preserved."
            )
        for identity, ciphertext in doc.items():
            c.execute(
                "UPDATE credentials_entity SET data=?,updatedAt=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=? AND type='openAiApi'",
                (ciphertext, identity),
            )
        c.commit()
    except sqlite3.Error:
        raise SetupError("Cannot update n8n's credential database.") from None
    finally:
        if c is not None:
            c.close()


def find(ctx):
    if (
        ctx.env.get("DB_TYPE", "sqlite") != "sqlite"
        or ctx.env.get("N8N_ENV_FEAT_ENCRYPTION_KEY_ROTATION", "false") == "true"
    ):
        raise SetupError("This n8n deployment needs a different database/encryption adapter.")
    root = ctx.config_dir("N8N_USER_FOLDER", ".") / ".n8n"
    candidates = [root / "database.sqlite"]
    for folder in ctx.roots:
        candidates.extend((folder / "database.sqlite", folder / ".n8n/database.sqlite"))
    paths = list(dict.fromkeys(p for p in candidates if p.is_file()))
    if len(paths) != 1:
        raise SetupError("Select one initialized local n8n data directory.")
    key = encryption_key(paths[0].parent / "config")
    if ctx.env.get("N8N_ENCRYPTION_KEY", key) != key:
        raise SetupError("n8n's environment encryption key differs from its local configuration.")
    read(resource(paths[0]))
    return paths[0]
