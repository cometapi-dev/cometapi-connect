import json
from pathlib import Path

import pytest

from cometapi_helper import gemini_launcher as gemini
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-windows-test-key-123456"


def fixture(tmp_path, version="0.60.0"):
    root = tmp_path / "npm install"
    root.mkdir()
    package = root / "node_modules/@google/gemini-cli"
    (package / "bundle").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@google/gemini-cli",
                "version": version,
                "bin": {"gemini": "bundle/gemini.js"},
            }
        )
    )
    (package / "bundle/gemini.js").write_text('console.log("fixture")')
    (root / "gemini.cmd").write_text("@echo off\r\n")
    (root / "node.exe").write_bytes(b"fixture")
    (root / "pwsh.exe").write_bytes(b"fixture")
    return Context(home=tmp_path, platform="win32", env={}, roots=[root], use_path=False)


def test_windows_engine_profile_idempotence_and_restore(tmp_path):
    ctx = fixture(tmp_path)
    engine = Engine(ctx)
    profile = tmp_path / "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True)
    profile.write_text("# existing user settings\n")
    original = profile.read_bytes()
    preview = engine.preview(KEY, ["gemini-cli"])
    assert KEY not in json.dumps(preview)
    result = engine.apply(preview["plan_id"])
    assert "function global:gemini" in profile.read_text(encoding="utf-8-sig")
    assert KEY not in profile.read_text(encoding="utf-8-sig")
    assert "GEMINI_CLI_TRUST_WORKSPACE" not in profile.read_text(encoding="utf-8-sig")
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, ["gemini-cli"])["changes"])
    engine.restore(result["transaction_id"])
    assert profile.read_bytes() == original
    assert not (tmp_path / ".gemini/cometapi-connect/connection.json").exists()


@pytest.mark.parametrize(
    "content",
    [
        "function gemini { echo user }",
        "Set-Alias gemini user",
        "function global:gemini { echo user }",
    ],
)
def test_windows_profile_conflicts_are_preserved(tmp_path, content):
    ctx = fixture(tmp_path)
    p = tmp_path / "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"
    p.parent.mkdir(parents=True)
    p.write_text(content)
    with pytest.raises(SetupError, match="alias/function"):
        gemini.changes("Gemini CLI", ctx, KEY, "gemini-3.5-flash")
    assert p.read_text() == content


@pytest.mark.parametrize("version", ["999.0.0", "0.23.0"])
def test_windows_new_versions_accepted(tmp_path, version):
    assert gemini.locate(fixture(tmp_path, version))["version"] == version


def test_windows_profile_quotes_literal_paths(tmp_path):
    ctx = fixture(tmp_path)
    gemini.locate(ctx)
    content = gemini.powershell_profile_bytes(
        None, Path("C:/O'Brien/node.exe"), Path("C:/O'Brien/launch.cjs")
    ).decode("utf8")
    assert "'" + str(Path("C:/O'Brien/node.exe")).replace("'", "''") + "'" in content
    assert "@args" in content


def test_windows_powershell_invokes_verified_entry_with_child_only_env(tmp_path):
    import os
    import shutil
    import subprocess

    if os.name != "nt" or not shutil.which("pwsh") or not shutil.which("node"):
        pytest.skip("Windows PowerShell 7 and Node required")
    ctx = fixture(tmp_path)
    (ctx.roots[0] / "node.exe").unlink()
    ctx.env = {"PATH": os.environ["PATH"]}
    ctx.use_path = True
    entry = ctx.roots[0] / "node_modules/@google/gemini-cli/bundle/gemini.js"
    entry.write_text(
        "console.log(JSON.stringify({key:process.env.GEMINI_API_KEY,endpoint:process.env.GOOGLE_GEMINI_BASE_URL,other:process.env.GOOGLE_API_KEY,trust:process.env.GEMINI_CLI_TRUST_WORKSPACE,args:process.argv.slice(2)}))"
    )
    engine = Engine(ctx)
    engine.apply(engine.preview(KEY, ["gemini-cli"])["plan_id"])
    profile = tmp_path / "Documents/PowerShell/Microsoft.PowerShell_profile.ps1"
    command = (
        ". '"
        + str(profile).replace("'", "''")
        + "'; gemini 'argument with spaces' 'apostrophe''s'; if ($env:GEMINI_API_KEY) { exit 99 }"
    )
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env["GOOGLE_API_KEY"] = "other-provider"
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "false"
    result = subprocess.run(
        [shutil.which("pwsh"), "-NoProfile", "-Command", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "key": KEY,
        "endpoint": "https://api.cometapi.com",
        "trust": "false",
        "args": ["argument with spaces", "apostrophe's"],
    }


def test_windows_requires_installed_powershell7(tmp_path):
    ctx = fixture(tmp_path)
    (ctx.roots[0] / "pwsh.exe").unlink()
    with pytest.raises(SetupError, match="PowerShell 7"):
        gemini.locate(ctx)
