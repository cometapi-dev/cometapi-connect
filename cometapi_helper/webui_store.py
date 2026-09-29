"""Open WebUI's per-key SQLite config store, without copying chats or users."""

import json
import sqlite3
import stat
import time
from pathlib import Path

from .common import BASE_URL, SetupError
from .storage import MAX_FILE_BYTES, no_links

KIND = "openwebui-sqlite-config"
KEYS = (
    "openai.enable",
    "openai.api_base_urls",
    "openai.api_keys",
    "openai.api_configs",
    "ui.default_models",
)
IMAGE_KEYS = (
    "image_generation.enable",
    "image_generation.engine",
    "image_generation.model",
    "image_generation.size",
    "image_generation.openai.api_base_url",
    "image_generation.openai.api_key",
    "image_generation.openai.api_version",
    "image_generation.openai.params",
)
PREFIX = "cometapi-connect"


def resource(path, images=False):
    result = {"kind": KIND, "app": "open-webui", "database": str(path)}
    if images:
        result["scope"] = "chat-and-images-v1"
    return result


def target_keys(target):
    return KEYS + IMAGE_KEYS if target.get("scope") == "chat-and-images-v1" else KEYS


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) not in ({"kind", "app", "database"}, {"kind", "app", "database", "scope"})
        or target.get("kind") != KIND
        or target.get("app") != "open-webui"
    ):
        raise SetupError("Unrecognized database configuration target.")
    if "scope" in target and target["scope"] != "chat-and-images-v1":
        raise SetupError("Unrecognized Open WebUI configuration scope.")
    path = Path(target["database"])
    if not path.is_absolute() or path.name != "webui.db":
        raise SetupError("Open WebUI needs an absolute path to its webui.db.")
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        no_links(candidate)
        if candidate.exists():
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SetupError("Open WebUI database files must be regular files without links.")
    if not path.is_file():
        raise SetupError("Open WebUI's initialized database was not found.")
    return path


def connect(target, writable=False):
    path = validate(target)
    connection = sqlite3.connect(
        path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=2
    )
    columns = connection.execute("PRAGMA table_info(config)").fetchall()
    if {r[1] for r in columns} != {"key", "value", "updated_at"} or not any(
        r[1] == "key" and r[5] == 1 for r in columns
    ):
        connection.close()
        raise SetupError(
            "This Open WebUI database uses an unsupported configuration schema. No database changes were made."
        )
    return connection


def encode(doc):
    data = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > MAX_FILE_BYTES:
        raise SetupError("Open WebUI connection settings exceed the supported size.")
    return data


def snapshot(connection, keys=KEYS):
    rows = connection.execute(
        "SELECT key,value FROM config WHERE key IN (" + ",".join("?" for _ in keys) + ")", keys
    ).fetchall()
    try:
        return encode({key: json.loads(value) for key, value in rows})
    except (ValueError, TypeError):
        raise SetupError("Open WebUI connection settings contain invalid JSON.") from None


def read(target):
    try:
        connection = connect(target)
        try:
            return snapshot(connection, target_keys(target))
        finally:
            connection.close()
    except sqlite3.Error:
        raise SetupError("Cannot read Open WebUI's configuration database.") from None


def write(target, value, expected):
    validate(target)
    keys = target_keys(target)
    try:
        doc = json.loads(value)
        if not isinstance(doc, dict) or not set(doc).issubset(keys):
            raise ValueError()
        encode(doc)
    except (ValueError, TypeError):
        raise SetupError("Invalid Open WebUI configuration backup.") from None
    connection = None
    try:
        connection = connect(target, writable=True)
        connection.execute("BEGIN IMMEDIATE")
        if snapshot(connection, keys) != expected:
            raise SetupError(
                "Open WebUI connection settings changed during setup. No database changes were made."
            )
        for key in keys:
            if key in doc:
                connection.execute(
                    "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, json.dumps(doc[key]), int(time.time())),
                )
            else:
                connection.execute("DELETE FROM config WHERE key=?", (key,))
        connection.commit()
    except sqlite3.Error:
        raise SetupError(
            "Open WebUI database update failed or is locked. No partial changes were committed."
        ) from None
    finally:
        if connection is not None:
            connection.close()


def find(ctx):
    candidates = []
    if ctx.env.get("DATA_DIR"):
        candidates.append(Path(ctx.env["DATA_DIR"]).expanduser() / "webui.db")
    for root in ctx.roots:
        candidates.extend(
            root / relative for relative in ("webui.db", "data/webui.db", "backend/data/webui.db")
        )
    found = []
    for path in dict.fromkeys(candidates):
        if path.is_file():
            read(resource(path))
            found.append(path)
    if len(found) != 1:
        raise SetupError("Select one initialized Open WebUI data folder containing webui.db.")
    return found[0]


def prepare_value(before, key, model, image_model=None):
    doc = json.loads(before)
    if not {"openai.api_base_urls", "openai.api_keys", "openai.api_configs"}.issubset(doc):
        raise SetupError(
            "Open WebUI has not persisted its connection defaults yet. Start and stop the server before configuring it."
        )
    urls = doc.setdefault("openai.api_base_urls", [])
    keys = doc.setdefault("openai.api_keys", [])
    configs = doc.setdefault("openai.api_configs", {})
    if (
        not isinstance(urls, list)
        or not isinstance(keys, list)
        or any(not isinstance(v, str) for v in urls + keys)
        or not isinstance(configs, dict)
    ):
        raise SetupError("Open WebUI's connection arrays have an unsupported format.")
    if len(keys) > len(urls):
        raise SetupError("Open WebUI has unmatched connection keys; existing entries were kept.")
    keys.extend([""] * (len(urls) - len(keys)))
    matches = [
        i
        for i, url in enumerate(urls)
        if url.rstrip("/") == BASE_URL
        and isinstance(configs.get(str(i)), dict)
        and configs[str(i)].get("prefix_id") == PREFIX
    ]
    if len(matches) > 1:
        raise SetupError("Multiple CometAPI Connect connections exist in Open WebUI.")
    if matches:
        index = matches[0]
    else:
        if any(isinstance(c, dict) and c.get("prefix_id") == PREFIX for c in configs.values()):
            raise SetupError(
                "Open WebUI already uses the CometAPI Connect prefix for another connection."
            )
        index = len(urls)
        urls.append(BASE_URL)
        keys.append("")
    keys[index] = key
    model_ids = configs.get(str(index), {}).get("model_ids", [])
    if not isinstance(model_ids, list) or any(not isinstance(m, str) for m in model_ids):
        raise SetupError("Open WebUI's CometAPI model list has an unsupported format.")
    if model not in model_ids:
        model_ids.append(model)
    configs[str(index)] = {
        "enable": True,
        "prefix_id": PREFIX,
        "model_ids": model_ids,
        "auth_type": "bearer",
        "connection_type": "external",
        "headers": {},
    }
    doc["openai.enable"] = True
    doc["ui.default_models"] = PREFIX + "." + model
    if image_model is not None:
        if image_model not in ("gpt-image-1", "gpt-image-1.5"):
            raise SetupError(
                "Open WebUI image configuration currently supports gpt-image-1 and gpt-image-1.5."
            )
        doc.update(
            {
                "image_generation.enable": True,
                "image_generation.engine": "openai",
                "image_generation.model": image_model,
                "image_generation.size": "1024x1024",
                "image_generation.openai.api_base_url": BASE_URL,
                "image_generation.openai.api_key": key,
                "image_generation.openai.api_version": "",
                "image_generation.openai.params": {"quality": "low"},
            }
        )
    return encode(doc)
