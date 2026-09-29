"""Private backups and atomic writes. Refuse links and concurrent edits."""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .common import SetupError

MAX_FILE_BYTES = 4 * 1024 * 1024


def no_links(path):
    path = Path(path).absolute()
    for component in [path] + list(path.parents):
        if component.is_symlink():
            raise SetupError("Refusing a symbolic link in configuration path: " + str(component))
        # Windows junctions/reparse points can also redirect a supposedly local write.
        try:
            attrs = getattr(component.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise SetupError("Refusing a junction or reparse point: " + str(component))


def read_file(path):
    no_links(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise SetupError("Expected a regular, unlinked configuration file: " + str(path))
    if info.st_size > MAX_FILE_BYTES:
        raise SetupError("Configuration is too large to edit safely: " + str(path))
    return path.read_bytes()


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def private_mkdir(path):
    no_links(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for current in reversed(missing):
        current.mkdir(mode=0o700)


def atomic_write(path, data, mode=0o600):
    no_links(path)
    private_mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".cometapi-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        no_links(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


class FileLock:
    """Cross-process lock; OS releases it even if the helper crashes."""

    def __init__(self, state_dir):
        self.path = state_dir / "operation.lock"
        self.stream = None

    def __enter__(self):
        private_mkdir(self.path.parent)
        no_links(self.path)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        self.stream = os.fdopen(fd, "r+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise SetupError(
                "Another setup or restore is running. Try again when it finishes."
            ) from None
        return self

    def __exit__(self, *_):
        self.stream.close()


def transaction_path(state_dir, transaction_id):
    if not isinstance(transaction_id, str) or not re.fullmatch(
        r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", transaction_id
    ):
        raise SetupError("Invalid backup ID.")
    return state_dir / "backups" / transaction_id
