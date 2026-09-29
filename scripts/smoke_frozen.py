#!/usr/bin/env python3
"""Exercise a frozen executable offline, using only an isolated temporary home.

Usage: python scripts/smoke_frozen.py /absolute/path/to/executable

Works with console and windowed executables: configuration uses a fake key in
the child process environment, and verification reads resulting files instead
of relying on stdout. The packaging self-test checks bundled certificates,
catalogs, the HTML page, and configuration parsers inside the frozen runtime.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

FAKE_KEY = "sk-frozen_smoke_only_not_a_real_key_123456"
OLD_FAKE_KEY = "sk-old_smoke_only_not_a_real_key_987654"
CHAT_MODEL = "smoke-chat-model"
CLAUDE_MODEL = "claude-smoke-model"


class SmokeFailure(Exception):
    """A fixed, safe diagnostic that never embeds process output or file data."""


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


def read_json(path, stage):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SmokeFailure(stage + ": expected a valid JSON result file.") from None


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def smoke(executable, timeout):
    require(executable.is_absolute(), "Provide an absolute executable path.")
    require(executable.is_file(), "The executable path must name an existing file.")
    executable = executable.resolve(strict=True)
    child_env = dict(os.environ)
    child_env["COMETAPI_KEY"] = FAKE_KEY
    # Ensure the executable's bundled modules are tested, not a source checkout
    # accidentally exposed through the invoking Python environment.
    child_env.pop("PYTHONPATH", None)
    child_env.pop("PYTHONHOME", None)

    stages = []
    with tempfile.TemporaryDirectory(prefix="cometapi-frozen-smoke-") as temporary:
        isolated_home = Path(temporary).resolve(strict=True)

        def run(stage, arguments):
            try:
                completed = subprocess.run(
                    [str(executable)] + [str(value) for value in arguments],
                    cwd=str(isolated_home),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise SmokeFailure(stage + ": executable exceeded the timeout.") from None
            except OSError:
                raise SmokeFailure(stage + ": executable could not be launched.") from None
            require(completed.returncode == 0, stage + ": executable returned a nonzero exit code.")
            captured = (completed.stdout or b"") + (completed.stderr or b"")
            require(
                FAKE_KEY.encode() not in captured and OLD_FAKE_KEY.encode() not in captured,
                stage + ": process output exposed a test credential.",
            )
            stages.append(stage)

        runtime_result = isolated_home / "runtime-self-test.json"
        run("bundled_runtime", ["--packaging-self-test", runtime_result])
        runtime = read_json(runtime_result, "bundled_runtime")
        require(
            isinstance(runtime, dict) and runtime.get("ok") is True,
            "bundled_runtime: runtime self-test did not confirm success.",
        )
        count = runtime.get("catalog_entries")
        require(
            isinstance(count, int) and not isinstance(count, bool) and count == 35,
            "bundled_runtime: expected exactly 35 retained catalog entries.",
        )
        # The generated --packaging-self-test entry point asserts certifi's
        # certificate file exists before writing ok=true; no HTTPS call is made.
        if "certificate_resources" in runtime:
            require(
                runtime["certificate_resources"] is True,
                "bundled_runtime: certificate-resource validation failed.",
            )

        claude_path = isolated_home / ".claude" / "settings.json"
        aider_path = isolated_home / ".aider.conf.yml"
        claude_path.parent.mkdir()
        originals = {
            claude_path: (
                json.dumps(
                    {
                        "permissions": {"allow": ["Read"]},
                        "env": {"ANTHROPIC_AUTH_TOKEN": OLD_FAKE_KEY, "KEEP_EXISTING": "retained"},
                    },
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
            aider_path: b"# Preserve this comment\nmodel: previous-model\nread: [CONVENTIONS.md]\n",
        }
        original_modes = {}
        for path, data in originals.items():
            path.write_bytes(data)
            if os.name != "nt":
                path.chmod(0o640)
            original_modes[path] = stat.S_IMODE(path.stat().st_mode)

        configure = [
            "--home",
            isolated_home,
            "configure",
            "--apps",
            "claude-code",
            "aider",
            "--chat-model",
            CHAT_MODEL,
            "--claude-model",
            CLAUDE_MODEL,
            "--yes",
        ]
        run("dry_run", configure + ["--dry-run"])
        require(
            all(path.read_bytes() == data for path, data in originals.items()),
            "dry_run: original configuration changed.",
        )
        require(
            not (isolated_home / ".cometapi-helper").exists(),
            "dry_run: created unexpected helper state or backups.",
        )

        run("configure", configure)
        claude = read_json(claude_path, "configure")
        require(
            isinstance(claude, dict) and isinstance(claude.get("env"), dict),
            "configure: Claude configuration has an invalid shape.",
        )
        require(
            claude["env"].get("ANTHROPIC_AUTH_TOKEN") == FAKE_KEY
            and claude["env"].get("ANTHROPIC_BASE_URL") == "https://api.cometapi.com"
            and claude["env"].get("ANTHROPIC_MODEL") == CLAUDE_MODEL,
            "configure: Claude gateway, key, or model was not configured.",
        )
        require(
            claude["env"].get("KEEP_EXISTING") == "retained"
            and claude.get("permissions") == {"allow": ["Read"]},
            "configure: unrelated Claude settings were not preserved.",
        )
        aider = aider_path.read_text(encoding="utf-8")
        require(
            "openai-api-base: https://api.cometapi.com/v1" in aider
            and "openai-api-key: " + FAKE_KEY in aider
            and "model: openai/" + CHAT_MODEL in aider,
            "configure: Aider gateway, key, or model was not configured.",
        )
        require(
            "# Preserve this comment" in aider and "CONVENTIONS.md" in aider,
            "configure: unrelated Aider settings were not preserved.",
        )

        backup_root = isolated_home / ".cometapi-helper" / "backups"
        manifests = list(backup_root.glob("*/manifest.json"))
        require(len(manifests) == 1, "configure: expected exactly one backup transaction.")
        manifest_path = manifests[0]
        transaction_id = manifest_path.parent.name
        require(
            re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", transaction_id) is not None,
            "configure: invalid backup transaction identifier.",
        )
        transaction = read_json(manifest_path, "configure")
        require(
            isinstance(transaction, dict) and transaction.get("status") == "applied",
            "configure: transaction was not marked applied.",
        )
        entries = transaction.get("entries")
        require(
            isinstance(entries, list) and len(entries) == 2,
            "configure: transaction did not include both configuration files.",
        )
        configured = {path: path.read_bytes() for path in originals}
        configured_mtimes = {path: path.stat().st_mtime_ns for path in originals}
        require(
            all(isinstance(entry, dict) for entry in entries),
            "configure: invalid backup entry shape.",
        )
        require(
            {entry.get("path") for entry in entries} == {str(path) for path in originals},
            "configure: transaction referenced files outside the isolated test configs.",
        )
        for index, entry in enumerate(entries):
            path = Path(entry["path"])
            require(
                entry.get("before_sha256") == sha256(originals[path])
                and entry.get("after_sha256") == sha256(configured[path]),
                "configure: transaction file hashes were incorrect.",
            )
            require(
                entry.get("backup") == str(index) + ".bak",
                "configure: transaction used an unexpected backup filename.",
            )
            backup_path = manifest_path.parent / entry["backup"]
            require(
                backup_path.read_bytes() == originals[path],
                "configure: original bytes were not backed up correctly.",
            )
            if os.name != "nt":
                require(
                    stat.S_IMODE(path.stat().st_mode) == 0o600
                    and stat.S_IMODE(backup_path.stat().st_mode) == 0o600,
                    "configure: configuration or backup permissions were not private.",
                )

        run("idempotent_reapply", configure)
        require(
            list(backup_root.glob("*/manifest.json")) == manifests,
            "idempotent_reapply: unexpectedly created another backup transaction.",
        )
        require(
            all(
                path.read_bytes() == configured[path]
                and path.stat().st_mtime_ns == configured_mtimes[path]
                for path in originals
            ),
            "idempotent_reapply: unexpectedly rewrote an unchanged configuration.",
        )

        run("restore", ["--home", isolated_home, "restore", transaction_id, "--yes"])
        require(
            all(path.read_bytes() == data for path, data in originals.items()),
            "restore: original configuration bytes were not restored.",
        )
        require(
            read_json(manifest_path, "restore").get("status") == "restored",
            "restore: backup transaction was not marked restored.",
        )
        if os.name != "nt":
            require(
                all(
                    stat.S_IMODE(path.stat().st_mode) == original_modes[path] for path in originals
                ),
                "restore: original configuration permissions were not restored.",
            )

        # Exercise newly bundled media assets and SQLite from the executable,
        # without importing any helper modules into this verification process.
        media_a = isolated_home / "a1111"
        media_b = isolated_home / "comfyui"
        proxy = isolated_home / "proxy"
        webui = isolated_home / "webui"
        for root, markers in (
            (media_a, ("launch.py", "modules/launch_utils.py", "modules/script_callbacks.py")),
            (media_b, ("main.py", "folder_paths.py", "comfy_api/latest/__init__.py")),
        ):
            for marker in markers:
                path = root / marker
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# isolated source marker\n")
        proxy.mkdir()
        webui.mkdir()
        proxy_config = proxy / "litellm_config.yaml"
        proxy_original = b"model_list: []\ngeneral_settings:\n  master_key: preserve-proxy-auth\n"
        proxy_config.write_bytes(proxy_original)
        db_path = webui / "webui.db"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value JSON,updated_at BIGINT)")
            db.execute("CREATE TABLE chat(id TEXT PRIMARY KEY,content TEXT)")
            db.execute("INSERT INTO chat VALUES('keep-chat','private test conversation')")
            for key, value in {
                "openai.api_base_urls": [],
                "openai.api_keys": [],
                "openai.api_configs": {},
                "auth.enable_signup": False,
            }.items():
                db.execute("INSERT INTO config VALUES(?,?,0)", (key, json.dumps(value)))
            db_original = dict(db.execute("SELECT key,value FROM config"))
        roots = ["--home", isolated_home]
        for path in (media_a, media_b, proxy, webui):
            roots += ["--root", path]
        args = roots + [
            "configure",
            "--apps",
            "automatic1111",
            "comfyui",
            "litellm",
            "open-webui",
            "--chat-model",
            CHAT_MODEL,
            "--image-model",
            "gpt-image-1",
            "--yes",
        ]
        run("media_and_database_configure", args)
        for path in (
            media_a / "extensions/sd-webui-cometapi/cometapi.json",
            media_b / "custom_nodes/cometapi_connect/cometapi.json",
        ):
            require(
                read_json(path, "media_config").get("api_key") == FAKE_KEY,
                "media_config: private credential missing.",
            )
        for path in (
            media_a / "extensions/sd-webui-cometapi/scripts/cometapi_media.py",
            media_b / "custom_nodes/cometapi_connect/__init__.py",
            media_b / "custom_nodes/cometapi_connect/cometapi_cloud.py",
        ):
            require(
                path.is_file() and FAKE_KEY not in path.read_text(encoding="utf-8"),
                "media_config: template missing or contains a credential.",
            )
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        with closing(sqlite3.connect(db_path)) as db, db:
            current = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM config")}
            require(
                current.get("image_generation.model") == "gpt-image-1"
                and current.get("image_generation.openai.api_key") == FAKE_KEY,
                "database_config: native image settings missing.",
            )
        run("media_and_database_idempotent", args)
        active = [
            p
            for p in backup_root.glob("*/manifest.json")
            if read_json(p, "media_config").get("status") == "applied"
        ]
        require(len(active) == 1, "media_config: expected one combined new transaction.")
        run(
            "media_and_database_restore",
            ["--home", isolated_home, "restore", active[0].parent.name, "--yes"],
        )
        require(
            proxy_config.read_bytes() == proxy_original,
            "media_restore: proxy configuration changed.",
        )
        require(
            not (media_a / "extensions/sd-webui-cometapi/cometapi.json").exists()
            and not (media_b / "custom_nodes/cometapi_connect/__init__.py").exists(),
            "media_restore: new extension files were not removed.",
        )
        with closing(sqlite3.connect(db_path)) as db, db:
            require(
                dict(db.execute("SELECT key,value FROM config")) == db_original,
                "database_restore: configuration values were not restored.",
            )
            require(
                db.execute("SELECT content FROM chat WHERE id='keep-chat'").fetchone()[0]
                == "private test conversation",
                "database_restore: unrelated chat changed.",
            )

        n8n = isolated_home / ".n8n"
        n8n.mkdir()
        (n8n / "config").write_text(
            json.dumps({"encryptionKey": "fake-n8n-encryption-key-for-tests"})
        )
        # Official n8n-core 2.38.1 ciphertext for a fake legacy credential.
        native = "U2FsdGVkX1+0fO8/JG5DyYQJbRXdkSOMrUd1eOKFvKwqYUg1XShm6KCRVlkLeSDb1AwSvUvtWcJmRzkwjVkaJgQ/9puspv2BUpNEs9hfqwuldlj3l7L7klT78nISkKRx+rVrC84Jq4ssT7437vJ5jA=="
        with closing(sqlite3.connect(n8n / "database.sqlite")) as db, db:
            db.executescript("""
                CREATE TABLE credentials_entity(id TEXT PRIMARY KEY,name TEXT,data TEXT,type TEXT,updatedAt TEXT,
                  isManaged INTEGER DEFAULT 0,isGlobal INTEGER DEFAULT 0,isResolvable INTEGER DEFAULT 0,
                  resolvableAllowFallback INTEGER DEFAULT 0,resolverId TEXT,usageScope TEXT DEFAULT 'project');
                CREATE TABLE shared_credentials(credentialsId TEXT,projectId TEXT,role TEXT);
                CREATE TABLE project(id TEXT,type TEXT);
                CREATE TABLE user(id TEXT);
                INSERT INTO project VALUES('p','personal');
                INSERT INTO user VALUES('u');
                INSERT INTO shared_credentials VALUES('credential-one','p','credential:owner');
            """)
            db.execute(
                "INSERT INTO credentials_entity(id,name,data,type) VALUES(?,?,?,?)",
                ("credential-one", "Fake legacy credential", native, "openAiApi"),
            )
        args = ["--home", isolated_home, "configure", "--apps", "n8n", "--yes"]
        run("encrypted_n8n_configure", args)
        with closing(sqlite3.connect(n8n / "database.sqlite")) as db, db:
            ciphertext = db.execute("SELECT data FROM credentials_entity").fetchone()[0]
            require(
                ciphertext != native
                and ciphertext.startswith("U2FsdGVkX1")
                and FAKE_KEY not in ciphertext,
                "encrypted_n8n_configure: expected changed encrypted credential.",
            )
        run("encrypted_n8n_idempotent", args)
        active = [
            p
            for p in backup_root.glob("*/manifest.json")
            if read_json(p, "n8n_config").get("status") == "applied"
        ]
        require(len(active) == 1, "n8n_config: expected one credential transaction.")
        run(
            "encrypted_n8n_restore",
            ["--home", isolated_home, "restore", active[0].parent.name, "--yes"],
        )
        with closing(sqlite3.connect(n8n / "database.sqlite")) as db, db:
            require(
                db.execute("SELECT data FROM credentials_entity").fetchone()[0] == native,
                "n8n_restore: original ciphertext not restored.",
            )

    return {
        "ok": True,
        "stages": stages,
        "catalog_entries": count,
        "certificate_resources": "checked_by_bundled_runtime_self_test",
        "configured_apps": 7,
        "media_assets_and_database_restore": True,
        "restored_exact_originals": True,
        "network_requests": 0,
        "temporary_home_removed": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "executable",
        type=Path,
        help="Absolute frozen executable path, including the binary inside a macOS .app",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Maximum seconds per executable invocation (default: 90)",
    )
    args = parser.parse_args()
    try:
        require(5 <= args.timeout <= 600, "Timeout must be between 5 and 600 seconds.")
        result = smoke(args.executable, args.timeout)
    except SmokeFailure as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    except Exception as error:
        # Never expose output, environment, configuration text, or subprocess
        # command lines in a failed smoke test report.
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Smoke test could not finish (" + type(error).__name__ + ").",
                }
            )
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
