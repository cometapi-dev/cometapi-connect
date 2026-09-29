"""Keep credentials out of Git indexes without modifying repositories' indexes."""

import os
import shutil
import subprocess
from pathlib import Path

from .common import SetupError
from .storage import no_links, read_file

GIT_ERROR = (
    "Cannot verify Git protection for a credential file. Install Git or fix repository access, "
    "then preview again. No credentials were written."
)
TRACKED_ERROR = (
    "A credential file is tracked by Git. Move its secrets to a private, ignored configuration "
    "before connecting. CometAPI Connect does not change the Git index."
)
IGNORE_ERROR = (
    "A credential file is no longer ignored by Git. Preview again to restore its protection."
)
IGNORE_FIELD = "Exclude private credentials from source control"
MARKER = "# CometAPI Connect private credentials\n"


def _git(root, *arguments, accepted=(0,)):
    executable = shutil.which("git")
    if not executable:
        raise SetupError(GIT_ERROR)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            [
                executable,
                *(["--literal-pathspecs"] if arguments[0] == "ls-files" else []),
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                *arguments,
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SetupError(GIT_ERROR) from None
    if result.returncode not in accepted:
        raise SetupError(GIT_ERROR)
    return result


def check_tracked(path):
    """Check every enclosing worktree, including nested repos and .git files."""
    path = Path(os.path.abspath(path))
    no_links(path)
    roots = []
    # A store that is itself a worktree cannot be protected from its parent.
    if path.is_dir() and os.path.lexists(path / ".git"):
        raise SetupError(GIT_ERROR)
    for parent in path.parents:
        marker = parent / ".git"
        if not os.path.lexists(marker):
            continue
        value = _git(parent, "rev-parse", "--show-toplevel").stdout
        try:
            root = Path(os.fsdecode(value).rstrip("\r\n"))
            if not root.is_absolute() or root.resolve() != parent.resolve():
                raise ValueError()
            relative = path.relative_to(parent).as_posix()
        except (ValueError, OSError):
            raise SetupError(GIT_ERROR) from None
        if _git(parent, "ls-files", "-z", "--", relative).stdout:
            raise SetupError(TRACKED_ERROR)
        roots.append(parent)
    return roots


def check_ignored(path):
    roots = check_tracked(path)
    if (
        roots
        and _git(
            roots[0],
            "check-ignore",
            "--no-index",
            "-q",
            "--",
            Path(path).relative_to(roots[0]).as_posix(),
            accepted=(0, 1),
        ).returncode
    ):
        raise SetupError(IGNORE_ERROR)


def credential_paths(path, resource=None):
    """Native keyrings and remote Docker credentials are not filesystem targets."""
    if resource:
        if not resource.get("database") or resource.get("kind") == "dify-local-docker-native-v1":
            return []
    path = Path(path)
    result = [path]
    if resource and path.suffix in (".sqlite", ".db", ".vscdb"):
        # SQLite can put the same credential pages in a journal or WAL.
        result.extend(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))
    return result


def check_changes(changes, *, ignored=False):
    for change in changes:
        if change.contains_credentials:
            for path in credential_paths(change.path, change.resource):
                (check_ignored if ignored else check_tracked)(path)


def _pattern(path):
    # Anchor to this directory; escape Git wildcards and trailing spaces.
    name = path.name
    if any(char in name for char in "\r\n\0"):
        raise SetupError("Cannot safely ignore this credential filename.")
    return "/" + "".join("\\" + char if char in "\\*?[]!# " else char for char in name)


def protect_changes(changes):
    """Stage exact ignore entries with the existing transaction machinery."""
    from .adapters import Change

    groups = {}
    for change in changes:
        if not change.contains_credentials:
            continue
        for path in credential_paths(change.path, change.resource):
            roots = check_tracked(path)
            if not roots and not change.private_sidecar:
                continue
            # An existing effective ignore rule needs no extra managed file.
            if (
                roots
                and not _git(
                    roots[0],
                    "check-ignore",
                    "--no-index",
                    "-q",
                    "--",
                    path.relative_to(roots[0]).as_posix(),
                    accepted=(0, 1),
                ).returncode
            ):
                continue
            ignore = path.parent / ".gitignore"
            group = groups.setdefault(ignore, (change, set()))
            group[1].add(_pattern(path))
    protections = []
    for path, (owner, patterns) in sorted(groups.items(), key=lambda item: str(item[0])):
        before = read_file(path)
        try:
            text = (before or b"").decode("utf-8")
        except UnicodeError:
            raise SetupError("Cannot safely update the project's credential ignore file.") from None
        block = MARKER + "".join(pattern + "\n" for pattern in sorted(patterns))
        after = (
            text
            if text.endswith(block)
            else text + ("\n" if text and not text.endswith("\n") else "") + block
        )
        protections.append(
            Change(
                owner.app_id,
                owner.app_name,
                path,
                before,
                after.encode("utf-8"),
                [IGNORE_FIELD],
                preserve_mode=True,
                credential_protection=True,
            )
        )
    # Ignore entries are written before any credential, and rolled back last.
    return protections + changes


def protect_state(state_dir):
    """Keep credential backups private even when the user's home is a worktree."""
    from .adapters import Change

    if not check_tracked(state_dir):
        return []
    path = state_dir / ".gitignore"
    before = read_file(path)
    try:
        text = (before or b"").decode("utf-8")
    except UnicodeError:
        raise SetupError("Cannot safely update the project's credential ignore file.") from None
    block = MARKER + "/*\n"
    after = (
        text
        if text.endswith(block)
        else text + ("\n" if text and not text.endswith("\n") else "") + block
    )
    return [
        Change(
            "cometapi-connect",
            "CometAPI Connect",
            path,
            before,
            after.encode(),
            [IGNORE_FIELD],
            preserve_mode=True,
            credential_protection=True,
            persistent_protection=True,
        )
    ]


def check_state(state_dir, *, ignored=False):
    check_tracked(state_dir)
    if ignored:
        check_ignored(state_dir / "backups")
