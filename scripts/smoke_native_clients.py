"""Offline frozen Gemini/Cherry transactions against isolated fake profiles."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cometapi_helper import cherry_store as cherry


def smoke(executable):
    fake = "sk-native-smoke-key-123456789"
    with tempfile.TemporaryDirectory(prefix="cometapi-native-smoke-") as temp:
        home = Path(temp).resolve()
        npm = home / "npm"
        (npm / "bin").mkdir(parents=True)
        (npm / "bin/gemini").write_text("// This fixture is never executed.\n")
        (npm / "node").write_text("fixture")
        (npm / "package.json").write_text(
            json.dumps(
                {"name": "@google/gemini-cli", "version": "0.58.0", "bin": {"gemini": "bin/gemini"}}
            )
        )
        (home / ".gemini").mkdir()
        (home / ".gemini/settings.json").write_text('{"security":{"folderTrust":{"enabled":true}}}')
        (home / ".gemini/trustedFolders.json").write_text(
            '{"/already-trusted-test":"TRUST_FOLDER"}'
        )
        (home / ".zshrc").write_text("# keep original shell preference\n")
        originals = {
            p: p.read_bytes()
            for p in [
                home / ".zshrc",
                home / ".gemini/settings.json",
                home / ".gemini/trustedFolders.json",
            ]
        }
        profile = home / "cherry-profile"
        path = profile / "Local Storage/leveldb"
        path.mkdir(parents=True)
        llm = {
            "providers": [{"id": "previous", "apiKey": "old-fake-key"}],
            "settings": {},
            "defaultModel": {"id": "old", "provider": "previous"},
        }
        doc = {
            "llm": json.dumps(llm),
            "_persist": '{"version":204,"rehydrated":true}',
            "assistants": json.dumps(
                {
                    "defaultAssistant": {"id": "template"},
                    "assistants": [{"id": "default", "topics": []}],
                }
            ),
            "settings": '{"theme":"keep"}',
        }
        with cherry.binding().DB(str(path), create_if_missing=True) as db:
            db.put(b"VERSION", b"1")
            db.put(cherry.KEY, b"\x00" + json.dumps(doc).encode("utf-16-le"))
        target = {"kind": cherry.KIND, "app": "cherry-studio", "database": str(path)}
        native_before = cherry.read(target)
        env = {**os.environ, "COMETAPI_KEY": fake}
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        common = [str(executable), "--home", str(home), "--root", str(npm), "--root", str(profile)]
        configure = [
            "configure",
            "--apps",
            "gemini-cli",
            "cherry-studio",
            "--chat-model",
            "gemini-3.5-flash",
            "--yes",
        ]

        def run(args):
            p = subprocess.run(common + args, cwd=home, env=env, capture_output=True, timeout=60)
            assert p.returncode == 0, "Frozen native-client operation failed"
            assert fake.encode() not in p.stdout + p.stderr, "Credential printed"

        run(configure + ["--dry-run"])
        assert cherry.read(target) == native_before
        assert all(p.read_bytes() == v for p, v in originals.items())
        run(configure)
        state = json.loads(cherry.read(target))
        assert state["llm"]["providers"][0]["apiKey"] == fake
        assert state["assistant_models"]["default"]["model"]["provider"] == cherry.PROVIDER
        assert (
            json.loads((home / ".gemini/cometapi-connect/connection.json").read_text())["apiKey"]
            == fake
        )
        assert (home / ".gemini/trustedFolders.json").read_bytes() == originals[
            home / ".gemini/trustedFolders.json"
        ]
        manifests = list((home / ".cometapi-helper/backups").glob("*/manifest.json"))
        assert len(manifests) == 1
        run(configure)
        assert list((home / ".cometapi-helper/backups").glob("*/manifest.json")) == manifests
        run(["restore", manifests[0].parent.name, "--yes"])
        assert cherry.read(target) == native_before
        assert all(p.read_bytes() == v for p, v in originals.items())
        assert not (home / ".gemini/cometapi-connect/connection.json").exists()
    return {
        "ok": True,
        "apps": ["gemini-cli", "cherry-studio"],
        "dry_run": True,
        "idempotent": True,
        "restored": True,
        "trust_preserved": True,
        "network_requests": 0,
        "temporary_home_removed": True,
    }


if __name__ == "__main__":
    print(json.dumps(smoke(Path(sys.argv[1]).resolve())))
