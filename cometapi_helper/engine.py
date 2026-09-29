"""Preview, transaction, rollback, and guarded restore shared by CLI and wizard."""

import json
import re
import secrets
import stat
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import (
    chatbox,
    cherry_store,
    copilot_store,
    credential_safety,
    credentials,
    dify_store,
    flowise_store,
    jan,
    lmstudio_store,
    n8n_store,
    roo_store,
    webui_store,
)
from .adapters import prepare
from .common import DEFAULT_MODELS, Context, SetupError, validate_key, validate_models
from .detection import scan
from .storage import (
    FileLock,
    atomic_write,
    digest,
    no_links,
    private_mkdir,
    read_file,
    transaction_path,
    write_json,
)


def merge_shell_changes(changes):
    """Compose only the two known managed shell blocks against the same snapshot."""
    names = {"gemini-cli": "Gemini CLI", "litellm": "LiteLLM"}
    result, by_path = [], {}

    def block_pattern(identity):
        start = "# >>> CometAPI Connect: " + names[identity] + " >>>"
        end = "# <<< CometAPI Connect: " + names[identity] + " <<<"
        return re.compile(re.escape(start) + r"\n.*?" + re.escape(end) + r"\n", re.S)

    def put(text, pattern, block):
        if pattern.search(text):
            return pattern.sub(lambda _: block, text, count=1)
        return text + ("\n" if text and not text.endswith("\n") else "") + block

    for change in changes:
        if change.path not in by_path:
            by_path[change.path] = len(result)
            result.append(change)
            continue
        index = by_path[change.path]
        prior = result[index]
        if (
            {prior.app_id, change.app_id} != set(names)
            or prior.resource
            or change.resource
            or prior.before != change.before
            or change.path.name
            not in (".zshrc", ".bashrc", ".bash_profile", ".bash_login", ".profile")
        ):
            raise SetupError(
                "Two app configurations resolve to the same path; configure them separately."
            )
        try:
            original = (change.before or b"").decode("utf-8")
            for item in (prior, change):
                pattern = block_pattern(item.app_id)
                blocks = pattern.findall(item.after.decode("utf-8"))
                if len(blocks) != 1 or put(original, pattern, blocks[0]).encode() != item.after:
                    raise ValueError()
            combined = put(
                prior.after.decode("utf-8"), block_pattern(change.app_id), blocks[0]
            ).encode()
        except (UnicodeError, ValueError, TypeError):
            raise SetupError(
                "The shared shell profile changes could not be combined safely."
            ) from None
        result[index] = replace(
            prior,
            after=combined,
            app_name=prior.app_name + " + " + change.app_name,
            fields=prior.fields + change.fields,
            warnings=prior.warnings + change.warnings,
        )
    return result


def read_target(path, resource=None):
    if resource and resource.get("kind") == lmstudio_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("LM Studio resource does not match the transaction.")
        return lmstudio_store.read(resource)
    if resource and resource.get("kind") == copilot_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Copilot resource does not match the transaction.")
        return copilot_store.read(resource)
    if resource and resource.get("kind") == dify_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Dify resource does not match the transaction.")
        return dify_store.read(resource)
    if resource and resource.get("kind") == roo_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Roo resource path does not match the transaction.")
        return roo_store.read(resource)
    if resource and resource.get("kind") == cherry_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        return cherry_store.read(resource)
    if resource and resource.get("kind") == flowise_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        return flowise_store.read(resource)
    if resource and resource.get("kind") == n8n_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        return n8n_store.read(resource)
    if resource and resource.get("kind") == webui_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        return webui_store.read(resource)
    return credentials.read(resource) if resource else read_file(path)


def write_target(path, value, mode=0o600, resource=None, expected=None):
    if resource and resource.get("kind") == lmstudio_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("LM Studio resource does not match the transaction.")
        return lmstudio_store.write(resource, value, expected)
    if resource and resource.get("kind") == copilot_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Copilot resource does not match the transaction.")
        return copilot_store.write(resource, value, expected)
    elif resource and resource.get("kind") == dify_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Dify resource does not match the transaction.")
        dify_store.write(resource, value, expected)
    elif resource and resource.get("kind") == roo_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Roo resource path does not match the transaction.")
        roo_store.write(resource, value, expected)
    elif resource and resource.get("kind") == cherry_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        cherry_store.write(resource, value, expected)
    elif resource and resource.get("kind") == flowise_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        flowise_store.write(resource, value, expected)
    elif resource and resource.get("kind") == n8n_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        n8n_store.write(resource, value, expected)
    elif resource and resource.get("kind") == webui_store.KIND:
        if str(path) != resource.get("database"):
            raise SetupError("Database resource path does not match the transaction.")
        webui_store.write(resource, value, expected)
    elif resource:
        credentials.write(resource, value)
    elif value is None:
        path.unlink()
    else:
        atomic_write(path, value, mode)


class Engine:
    def __init__(self, context=None):
        self.ctx = context or Context()
        self.plans = {}
        self.lock = threading.RLock()

    def scan(self, roots=None):
        with self.lock:
            if roots is not None:
                if not isinstance(roots, list) or len(roots) > 20:
                    raise SetupError("Provide at most 20 project scan folders.")
                paths = []
                for root in roots:
                    if not isinstance(root, str) or not Path(root).expanduser().is_absolute():
                        raise SetupError("Additional scan folders must be absolute paths.")
                    path = Path(root).expanduser()
                    if not path.is_dir():
                        raise SetupError("A scan folder does not exist: " + str(path))
                    paths.append(path)
                self.ctx.roots = paths
            apps, _ = scan(self.ctx)
            from . import __version__

            return {
                "apps": apps,
                "defaults": DEFAULT_MODELS,
                "platform": self.ctx.platform,
                "home": str(self.ctx.home),
                "version": __version__,
            }

    def preview(self, api_key, apps, models=None):
        key = validate_key(api_key)
        models = validate_models(models)
        if (
            not isinstance(apps, list)
            or not apps
            or len(apps) > 50
            or not all(isinstance(x, str) for x in apps)
        ):
            raise SetupError("Select at least one app with automatic setup.")
        with self.lock:
            available, repositories = scan(self.ctx)
            indexed = {app["id"]: app for app in available}
            changes = []
            for identity in dict.fromkeys(apps):
                if identity not in indexed or indexed[identity]["mode"] != "automatic":
                    raise SetupError(
                        "A selected app requires guided setup or a verified source checkout."
                    )
                changes.extend(
                    prepare(
                        identity, indexed[identity]["name"], self.ctx, key, models, repositories
                    )
                )
            changes = merge_shell_changes(changes)
            changes = credential_safety.protect_changes(changes)
            changes = credential_safety.protect_state(self.ctx.state_dir) + changes
            # Keep one short-lived plan; never write prospective credentials to disk.
            self.plans.clear()
            plan_id = secrets.token_urlsafe(24)
            self.plans[plan_id] = (time.monotonic(), changes)
            warnings = list(
                dict.fromkeys(warning for change in changes for warning in change.warnings)
            )
            warnings.insert(
                0,
                "Close selected apps before applying so they cannot overwrite the changes. Keys are saved in the apps' configuration or system credential store; private backups may contain previous credentials.",
            )
            if self.ctx.platform == "win32":
                warnings.append(
                    "On Windows, configuration and backup files inherit your user folder's access controls. POSIX owner-only permissions are not a Windows ACL guarantee."
                )
            return {
                "plan_id": plan_id,
                "changes": [change.public() for change in changes],
                "warnings": warnings,
            }

    def apply(self, plan_id):
        with self.lock:
            if not isinstance(plan_id, str) or plan_id not in self.plans:
                raise SetupError(
                    "The preview expired or was already used. Preview the changes again."
                )
            created, changes = self.plans.pop(plan_id)
            if time.monotonic() - created > 600:
                raise SetupError("The preview expired. Preview the changes again.")
            credential_safety.check_changes(changes)
            credential_safety.check_state(self.ctx.state_dir)
            persistent = [
                change
                for change in changes
                if change.persistent_protection and change.action != "unchanged"
            ]
            active = [
                change
                for change in changes
                if not change.persistent_protection and change.action != "unchanged"
            ]
            if any(change.app_id == "chatbox" for change in changes):
                chatbox.require_closed(self.ctx)
            if any(change.app_id == "jan" for change in changes):
                jan.require_closed(self.ctx)
            if not active and not persistent:
                return {
                    "transaction_id": None,
                    "changes": [],
                    "message": "The selected configurations already match. No files changed.",
                }
            with FileLock(self.ctx.state_dir):
                for change in changes:
                    if read_target(change.path, change.resource) != change.before:
                        raise SetupError(
                            "A file changed after preview: " + str(change.path) + ". Preview again."
                        )
                # State protection must precede backups and is never rolled back:
                # recovery records can retain old credentials after any outcome.
                for change in persistent:
                    atomic_write(change.path, change.after)
                credential_safety.check_state(self.ctx.state_dir, ignored=True)
                if not active:
                    return {
                        "transaction_id": None,
                        "changes": [change.public() for change in persistent],
                        "message": "Private credential backup protection updated.",
                    }
                timestamp = datetime.now(timezone.utc)
                identity = timestamp.strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(6)
                directory = transaction_path(self.ctx.state_dir, identity)
                private_mkdir(directory)
                entries = []
                for index, change in enumerate(active):
                    mode = (
                        stat.S_IMODE(change.path.stat().st_mode)
                        if change.before is not None and not change.resource
                        else None
                    )
                    backup = str(index) + ".bak" if change.before is not None else None
                    if backup:
                        credential_safety.check_state(self.ctx.state_dir, ignored=True)
                        atomic_write(directory / backup, change.before)
                    entries.append(
                        {
                            "path": str(change.path),
                            "app_id": change.app_id,
                            "app_name": change.app_name,
                            "before_sha256": digest(change.before),
                            "after_sha256": digest(change.after),
                            "mode": mode,
                            "backup": backup,
                            "contains_credentials": change.contains_credentials,
                            "restore_contains_credentials": change.contains_credentials
                            or change.restore_contains_credentials,
                            "credential_protection": change.credential_protection,
                            **({"resource": change.resource} if change.resource else {}),
                        }
                    )
                manifest = {
                    "id": identity,
                    "created_at": timestamp.isoformat(),
                    "status": "preparing",
                    "entries": entries,
                }
                write_json(directory / "manifest.json", manifest)
                completed = []
                try:
                    for change, entry in zip(active, entries):
                        if change.app_id == "chatbox":
                            chatbox.require_closed(self.ctx)
                        if read_target(change.path, change.resource) != change.before:
                            raise SetupError(
                                "A file changed while applying. Changes will be rolled back."
                            )
                        credential_safety.check_changes([change], ignored=True)
                        actual_after = write_target(
                            change.path,
                            change.after,
                            mode=entry["mode"]
                            if change.preserve_mode and entry["mode"] is not None
                            else 0o600,
                            resource=change.resource,
                            expected=change.before,
                        )
                        if actual_after is not None:
                            if (
                                not isinstance(actual_after, bytes)
                                or not change.resource
                                or change.resource.get("kind") != copilot_store.KIND
                            ):
                                raise roo_store.IncompleteNativeWrite(
                                    "Unexpected native write acknowledgment"
                                )
                            change.after = actual_after
                            entry["after_sha256"] = digest(actual_after)
                        completed.append((change, entry))
                    manifest["status"] = "applied"
                    write_json(directory / "manifest.json", manifest)
                except Exception as error:
                    failures = (
                        [str(change.path)]
                        if isinstance(error, roo_store.IncompleteNativeWrite)
                        else []
                    )
                    for change, entry in reversed(completed):
                        # A failed/unknown credential rollback needs its ignore rule.
                        if failures and change.credential_protection:
                            continue
                        try:
                            if read_target(change.path, change.resource) != change.after:
                                raise SetupError("File changed during rollback")
                            if (
                                change.contains_credentials or change.restore_contains_credentials
                            ) and change.before is not None:
                                for path in credential_safety.credential_paths(
                                    change.path, change.resource
                                ):
                                    credential_safety.check_tracked(path)
                            write_target(
                                change.path,
                                change.before,
                                entry["mode"],
                                change.resource,
                                expected=change.after,
                            )
                        except Exception:
                            failures.append(str(change.path))
                    manifest["status"] = "rollback_failed" if failures else "rolled_back"
                    try:
                        write_json(directory / "manifest.json", manifest)
                    except OSError:
                        pass
                    if failures:
                        raise SetupError(
                            "Setup failed and some files could not be rolled back. Preserve backup "
                            + identity
                            + " and recover the files listed in its manifest."
                        ) from None
                    raise SetupError(
                        "Setup failed. All completed changes were rolled back; check file permissions and try again."
                    ) from None
                return {
                    "transaction_id": identity,
                    "changes": [change.public() for change in persistent + active],
                    "message": "Configuration saved. Restart the selected apps and follow any app-specific notes.",
                }

    def history(self):
        with self.lock:
            folder = self.ctx.state_dir / "backups"
            no_links(folder)
            if not folder.exists():
                return {"transactions": []}
            results = []
            for directory in sorted(folder.iterdir(), reverse=True):
                try:
                    transaction_path(self.ctx.state_dir, directory.name)
                    value = json.loads(
                        (read_file(directory / "manifest.json") or b"{}").decode("utf-8")
                    )
                    status = value["status"]
                    results.append(
                        {
                            "id": directory.name,
                            "created_at": value["created_at"],
                            "apps": list(
                                dict.fromkeys(entry["app_name"] for entry in value["entries"])
                            ),
                            "restored": status in ("restored", "rolled_back"),
                            "status": status,
                        }
                    )
                except (SetupError, KeyError, ValueError, OSError):
                    continue
            return {"transactions": results}

    def restore(self, transaction_id):
        with self.lock, FileLock(self.ctx.state_dir):
            directory = transaction_path(self.ctx.state_dir, transaction_id)
            try:
                manifest = json.loads(
                    (read_file(directory / "manifest.json") or b"{}").decode("utf-8")
                )
                entries = manifest["entries"]
            except (ValueError, KeyError):
                raise SetupError("The selected backup is missing or invalid.") from None
            if manifest["status"] != "applied":
                raise SetupError(
                    "This backup is not an applied transaction. Interrupted operations require recovery using its manifest."
                )
            if any(entry.get("app_id") == "chatbox" for entry in entries):
                chatbox.require_closed(self.ctx)
            staged = []
            for index, entry in enumerate(entries):
                path = Path(entry["path"])
                if not path.is_absolute():
                    raise SetupError("Invalid configuration path in backup.")
                resource = entry.get("resource")
                current = read_target(path, resource)
                if digest(current) != entry["after_sha256"]:
                    raise SetupError(
                        "Restore stopped because a file changed since setup: "
                        + str(path)
                        + ". Your newer settings were kept."
                    )
                before = None
                if entry["backup"] is not None:
                    if entry["backup"] != str(index) + ".bak":
                        raise SetupError("Invalid backup filename.")
                    before = read_file(directory / entry["backup"])
                    if before is None or digest(before) != entry["before_sha256"]:
                        raise SetupError(
                            "Backup integrity check failed. No settings were restored."
                        )
                sensitive = entry.get(
                    "restore_contains_credentials", entry.get("contains_credentials", False)
                )
                if sensitive and before is not None:
                    for credential_path in credential_safety.credential_paths(path, resource):
                        credential_safety.check_tracked(credential_path)
                staged.append((path, before, current, entry["mode"], resource, sensitive))
            # Restore credentials before removing their transaction's ignore rules.
            protections = {
                Path(entry["path"]) for entry in entries if entry.get("credential_protection")
            }
            staged.sort(key=lambda item: item[0] in protections)
            completed = []
            try:
                for path, before, current, mode, resource, sensitive in staged:
                    if any(
                        entry.get("app_id") == "chatbox" and entry["path"] == str(path)
                        for entry in entries
                    ):
                        chatbox.require_closed(self.ctx)
                    if any(
                        entry.get("app_id") == "jan" and entry["path"] == str(path)
                        for entry in entries
                    ):
                        jan.require_closed(self.ctx)
                    if read_target(path, resource) != current:
                        raise SetupError("A file changed during restore.")
                    if sensitive and before is not None:
                        for credential_path in credential_safety.credential_paths(path, resource):
                            credential_safety.check_tracked(credential_path)
                    write_target(path, before, mode, resource, expected=current)
                    after_sensitive = next(
                        entry.get("contains_credentials", False)
                        for entry in entries
                        if entry["path"] == str(path)
                    )
                    completed.append((path, before, current, resource, after_sensitive))
                manifest["status"] = "restored"
                write_json(directory / "manifest.json", manifest)
            except Exception as error:
                failed = isinstance(error, roo_store.IncompleteNativeWrite)
                for path, before, current, resource, sensitive in reversed(completed):
                    try:
                        if read_target(path, resource) != before:
                            raise SetupError("A file changed during restore rollback.")
                        if sensitive and current is not None:
                            for credential_path in credential_safety.credential_paths(
                                path, resource
                            ):
                                credential_safety.check_ignored(credential_path)
                        write_target(path, current, resource=resource, expected=before)
                    except Exception:
                        failed = True
                if failed:
                    manifest["status"] = "restore_failed"
                    try:
                        write_json(directory / "manifest.json", manifest)
                    except OSError:
                        pass
                    raise SetupError(
                        "Restore was interrupted. Keep backup "
                        + transaction_id
                        + " for manual recovery."
                    ) from None
                raise SetupError(
                    "Restore failed. Completed restore steps were undone; check permissions and try again."
                ) from None
            return {"message": "Original configuration files restored. Restart the affected apps."}
