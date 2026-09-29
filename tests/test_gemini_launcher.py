import json
import os
import shutil
import subprocess

import pytest

from cometapi_helper import gemini_launcher as gemini
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-gemini-fixture-key-123456"


def fixture(home, shell="zsh", version="0.58.0"):
    root = home / "npm with spaces"
    entry = root / "bin/gemini"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "console.log(JSON.stringify({key:process.env.GEMINI_API_KEY, endpoint:process.env.GOOGLE_GEMINI_BASE_URL, other:process.env.GOOGLE_API_KEY, trust:process.env.GEMINI_CLI_TRUST_WORKSPACE, args:process.argv.slice(2)}));",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {"name": "@google/gemini-cli", "version": version, "bin": {"gemini": "bin/gemini"}}
        )
    )
    (root / "node").write_bytes(b"fixture")
    (home / ".gemini").mkdir()
    (home / ".gemini/settings.json").write_text(
        json.dumps(
            {
                "security": {"folderTrust": {"enabled": True}},
                "tools": {"approvalMode": "default"},
                "ui": {"theme": "Default"},
            }
        )
    )
    (home / ".gemini/trustedFolders.json").write_text('{"/existing/test":"TRUST_FOLDER"}')
    (home / (".zshrc" if shell == "zsh" else ".bashrc")).write_text(
        "# User preference\nexport OTHER=value\n"
    )
    return Context(
        home=home, platform="darwin", env={"SHELL": "/bin/" + shell}, roots=[root], use_path=False
    )


@pytest.mark.parametrize(
    "shell,version",
    [
        ("zsh", "0.58.0"),
        ("bash", "0.58.0"),
        ("zsh", "0.23.0"),
        ("bash", "0.23.0"),
        ("zsh", "999.0.0"),
    ],
)
def test_engine_preserves_trust_and_shell_idempotent_restore(tmp_path, shell, version):
    ctx = fixture(tmp_path, shell, version)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    engine = Engine(ctx)
    app = next(a for a in engine.scan()["apps"] if a["id"] == "gemini-cli")
    assert app["mode"] == "automatic" and app["detected"]
    preview = engine.preview(KEY, ["gemini-cli"])
    assert KEY not in json.dumps(preview)
    assert all(p.read_bytes() == v for p, v in before.items())
    applied = engine.apply(preview["plan_id"])
    trust = tmp_path / ".gemini/trustedFolders.json"
    assert trust.read_bytes() == before[trust]
    settings = json.loads((tmp_path / ".gemini/settings.json").read_text())
    assert settings["security"]["folderTrust"] == {"enabled": True}
    assert settings["tools"] == {"approvalMode": "default"}
    assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"
    assert settings["model"]["name"] == "gemini-3.5-flash"
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, ["gemini-cli"])["changes"])
    assert KEY not in json.dumps(engine.history())
    assert KEY not in (tmp_path / (".zshrc" if shell == "zsh" else ".bashrc")).read_text()
    if os.name != "nt":
        assert (
            tmp_path / ".gemini/cometapi-connect/connection.json"
        ).stat().st_mode & 0o777 == 0o600
    engine.restore(applied["transaction_id"])
    assert all(p.read_bytes() == v for p, v in before.items())
    assert not (tmp_path / ".gemini/cometapi-connect/connection.json").exists()


@pytest.mark.parametrize(
    "profile",
    [
        b'alias gemini="other"\n',
        b"function gemini { echo original; }\n",
        b"gemini() { echo original; }\n",
        (gemini.START + "\nbroken").encode(),
    ],
)
def test_existing_shell_overrides_are_preserved(tmp_path, profile):
    ctx = fixture(tmp_path)
    (tmp_path / ".zshrc").write_bytes(profile)
    with pytest.raises(SetupError):
        gemini.changes("Gemini CLI", ctx, KEY, "gemini-3.5-flash")
    assert (tmp_path / ".zshrc").read_bytes() == profile


def test_accept_version_change_but_reject_policy_or_missing_windows_install(tmp_path):
    ctx = fixture(tmp_path, version="999.0.0")
    assert gemini.locate(ctx)["version"] == "999.0.0"
    with pytest.raises(SetupError, match="policy"):
        gemini.settings_update(
            {"security": {"auth": {"enforcedType": "oauth-personal"}}}, "gemini-3.5-flash"
        )
    ctx.platform = "win32"
    with pytest.raises(SetupError, match="Windows"):
        gemini.locate(ctx)


def test_launcher_supplies_only_child_env_and_blocks_changed_installation(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime required for launcher execution")
    ctx = fixture(tmp_path)
    changes = gemini.changes("Gemini CLI", ctx, KEY, "gemini-3.5-flash")
    for c in changes:
        c.path.parent.mkdir(parents=True, exist_ok=True)
        c.path.write_bytes(c.after)
    launcher = tmp_path / ".gemini/cometapi-connect/launch.cjs"
    env = {**os.environ, "GOOGLE_API_KEY": "previous-google-key"}
    env.pop("GEMINI_CLI_TRUST_WORKSPACE", None)
    output = subprocess.run(
        [node, str(launcher), "argument with space"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(output.stdout) == {
        "key": KEY,
        "endpoint": "https://api.cometapi.com",
        "args": ["argument with space"],
    }
    assert env["GOOGLE_API_KEY"] == "previous-google-key"
    manifest = ctx.roots[0] / "package.json"
    package = json.loads(manifest.read_text())
    package["version"] = "999.0.0"
    manifest.write_text(json.dumps(package))
    upgraded = subprocess.run([node, str(launcher)], env=env, capture_output=True, text=True)
    assert upgraded.returncode == 0
    package["name"] = "other-cli"
    manifest.write_text(json.dumps(package))
    blocked = subprocess.run([node, str(launcher)], env=env, capture_output=True, text=True)
    assert blocked.returncode == 1 and KEY not in blocked.stdout + blocked.stderr


def test_restore_rejects_later_shell_edits(tmp_path):
    engine = Engine(fixture(tmp_path))
    applied = engine.apply(engine.preview(KEY, ["gemini-cli"])["plan_id"])
    profile = tmp_path / ".zshrc"
    profile.write_text(profile.read_text() + "# later edit\n")
    with pytest.raises(SetupError):
        engine.restore(applied["transaction_id"])
    assert profile.read_text().endswith("# later edit\n")


def test_dev8_managed_launcher_upgrades_and_restores(tmp_path):
    engine = Engine(fixture(tmp_path))
    engine.apply(engine.preview(KEY, ["gemini-cli"])["plan_id"])
    launcher = tmp_path / ".gemini/cometapi-connect/launch.cjs"
    launcher.write_bytes(gemini.LEGACY_LAUNCHER.encode())
    tx = engine.apply(engine.preview(KEY, ["gemini-cli"])["plan_id"])
    assert launcher.read_bytes() == gemini.LAUNCHER.encode()
    engine.restore(tx["transaction_id"])
    assert launcher.read_bytes() == gemini.LEGACY_LAUNCHER.encode()
