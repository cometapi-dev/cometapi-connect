"""Cherry Studio v1.8.1 Redux v204 provider slice in Chromium Local Storage.

Preview reads a private database copy. Writes use native LevelDB atomic batches,
preserve other Redux slices/keys, and update Chromium's origin size metadata.
"""

import json
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .common import SetupError
from .storage import no_links

KIND = "cherry-leveldb-provider-v1"
KEY = b"_file://\x00\x01persist:cherry-studio"
PREFIX = b"_file://\x00"
PROVIDER = "cometapi-connect"
LIMIT = 64 * 1024 * 1024


def binding():
    try:
        if sys.version_info >= (3, 13):
            import plyvel_next as plyvel
        else:
            import plyvel
        return plyvel
    except ImportError:
        raise SetupError(
            "This build does not include the Cherry Studio database adapter."
        ) from None


def database_files(path):
    no_links(path)
    if not path.is_dir() or not (path / "CURRENT").is_file():
        raise SetupError("Cherry Studio needs an initialized Local Storage database.")
    result = []
    size = 0
    for p in path.iterdir():
        no_links(p)
        info = p.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SetupError("Cherry Studio database contains a linked or non-regular file.")
        size += info.st_size
        if size > LIMIT or len(result) > 200:
            raise SetupError("Cherry Studio database exceeds the bounded configuration reader.")
        result.append(p)
    return result


@contextmanager
def copied_database(path):
    files = database_files(path)
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in files}
    with tempfile.TemporaryDirectory(prefix="cometapi-cherry-") as temp:
        target = Path(temp) / "db"
        target.mkdir(mode=0o700)
        try:
            for p in files:
                shutil.copyfile(p, target / p.name)
        except OSError:
            raise SetupError(
                "Cherry Studio database is locked or unavailable. Quit the app completely and retry."
            ) from None
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in database_files(path)}
        if before != after:
            raise SetupError(
                "Cherry Studio changed its database during preview. Quit it and retry."
            )
        db = None
        try:
            db = binding().DB(
                str(target), create_if_missing=False, paranoid_checks=True, max_open_files=32
            )
            yield db
        except SetupError:
            raise
        except Exception:
            raise SetupError(
                "Cannot safely read this Cherry Studio database. It was not changed."
            ) from None
        finally:
            if db is not None:
                db.close()


def document(db):
    if db.get(b"VERSION") != b"1":
        raise SetupError("Unsupported Chromium Local Storage schema.")
    raw = db.get(KEY)
    try:
        if not raw or len(raw) > 4 * 1024 * 1024 or raw[0] not in (0, 1):
            raise ValueError()
        value = json.loads(raw[1:].decode("utf-16-le" if raw[0] == 0 else "latin-1"))
        persist = json.loads(value["_persist"])
        if (
            not isinstance(persist, dict)
            or type(persist.get("version")) is not int
            or persist.get("rehydrated") is not True
        ):
            raise ValueError()
        llm = json.loads(value["llm"])
        if not isinstance(llm["providers"], list) or not isinstance(llm["settings"], dict):
            raise ValueError()
        ids = [p["id"] for p in llm["providers"]]
        if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
            raise ValueError()
        return value, llm
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise SetupError(
            "Cherry Studio automatic setup requires a compatible initialized Redux provider structure."
        ) from None


def encode(llm):
    return json.dumps(llm, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def assistant_targets(doc):
    try:
        assistants = json.loads(doc["assistants"])
        default = assistants["defaultAssistant"]
        items = [a for a in assistants["assistants"] if a.get("id") == "default"]
        if not isinstance(default, dict) or len(items) != 1:
            raise ValueError()
        return assistants, {"template": default, "default": items[0]}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SetupError("Cherry Studio needs one initialized default assistant.") from None


def snapshot(doc, llm):
    _, targets = assistant_targets(doc)
    return {
        "llm": llm,
        "assistant_models": {
            name: ({"model": item["model"]} if "model" in item else {})
            for name, item in targets.items()
        },
    }


def checked_path(resource):
    if (
        set(resource) != {"kind", "app", "database"}
        or resource.get("kind") != KIND
        or resource.get("app") != "cherry-studio"
    ):
        raise SetupError("Invalid Cherry Studio database target.")
    path = Path(resource["database"])
    if not path.is_absolute() or path.parts[-2:] != ("Local Storage", "leveldb"):
        raise SetupError("Invalid Cherry Studio profile path.")
    return path


def read(resource):
    with copied_database(checked_path(resource)) as db:
        return encode(snapshot(*document(db)))


def find(ctx):
    if ctx.platform not in ("darwin", "win32"):
        raise SetupError(
            "Cherry Studio native database setup is currently verified on macOS and Windows."
        )
    roots = [r for r in ctx.roots if (r / "Local Storage/leveldb/CURRENT").is_file()]
    if not roots:
        base = (
            ctx.config_dir("APPDATA", "AppData/Roaming")
            if ctx.platform == "win32"
            else ctx.home / "Library/Application Support"
        )
        roots = [base / name for name in ("CherryStudio", "Cherry Studio", "cherry-studio")]
        roots = [r for r in roots if (r / "Local Storage/leveldb/CURRENT").is_file()]
    if len(roots) != 1:
        raise SetupError("Select one initialized Cherry Studio profile.")
    target = {
        "kind": KIND,
        "app": "cherry-studio",
        "database": str(roots[0] / "Local Storage/leveldb"),
    }
    read(target)
    return target


def prepare_value(before, key, model):
    state = json.loads(before)
    llm = state["llm"]
    providers = llm["providers"]
    provider = next((p for p in providers if p["id"] == PROVIDER), None)
    if provider is None:
        provider = {
            "id": PROVIDER,
            "name": "CometAPI Connect",
            "type": "openai",
            "isSystem": False,
            "models": [],
        }
        providers.insert(0, provider)
    elif (
        provider.get("name") != "CometAPI Connect"
        or provider.get("type") != "openai"
        or provider.get("apiHost") != "https://api.cometapi.com"
    ):
        raise SetupError(
            "An existing Cherry Studio provider uses the CometAPI Connect ID differently."
        )
    models = provider.get("models")
    if not isinstance(models, list) or any(
        not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in models
    ):
        raise SetupError("Unsupported Cherry Studio model list.")
    selected = next((m for m in models if m["id"] == model), None)
    if selected is None:
        selected = {"id": model, "name": model, "provider": PROVIDER, "group": "CometAPI"}
        models.append(selected)
    if selected.get("provider") != PROVIDER:
        raise SetupError("The Cherry Studio model belongs to another provider.")
    provider.update(apiKey=key, apiHost="https://api.cometapi.com", enabled=True)
    llm["defaultModel"] = selected
    state["assistant_models"] = {name: {"model": selected} for name in state["assistant_models"]}
    return encode(state)


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def write(resource, value, expected):
    path = checked_path(resource)
    database_files(path)
    replacement = json.loads(value)
    if encode(replacement) != value:
        raise SetupError("Invalid Cherry Studio provider backup.")
    db = None
    try:
        # Native LevelDB refuses a database locked by the running Chromium app.
        db = binding().DB(
            str(path), create_if_missing=False, paranoid_checks=True, max_open_files=32
        )
        doc, current = document(db)
        if encode(snapshot(doc, current)) != expected:
            raise SetupError("Cherry Studio providers changed; newer settings were preserved.")
        doc["llm"] = encode(replacement["llm"]).decode("utf-8")
        assistants, targets = assistant_targets(doc)
        if set(replacement["assistant_models"]) != set(targets):
            raise SetupError("Invalid Cherry Studio assistant backup.")
        for name, item in targets.items():
            model = replacement["assistant_models"][name]
            if model:
                item["model"] = model["model"]
            else:
                item.pop("model", None)
        doc["assistants"] = encode(assistants).decode("utf-8")
        raw = b"\x00" + json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-16-le"
        )
        if len(raw) > 4 * 1024 * 1024:
            raise SetupError("Cherry Studio configuration exceeds the supported size.")
        total = sum(len(k) - len(PREFIX) + len(v) for k, v in db.iterator(prefix=PREFIX))
        total += len(raw) - len(db.get(KEY))
        chromium_time = int((time.time() + 11644473600) * 1000000)
        metadata = b"\x08" + varint(chromium_time) + b"\x10" + varint(total)
        with db.write_batch(transaction=True, sync=True) as batch:
            batch.put(KEY, raw)
            batch.put(b"META:file://", metadata)
    except SetupError:
        raise
    except Exception:
        raise SetupError(
            "Cherry Studio database is locked or unsupported. Quit the app completely and retry."
        ) from None
    finally:
        if db is not None:
            db.close()
