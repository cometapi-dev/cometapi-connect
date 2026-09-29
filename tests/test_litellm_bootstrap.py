import json
import subprocess

import pytest

from cometapi_helper import litellm_config
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-fixture-bootstrap-only-123456"


def installation(home):
    root = home / "python install"
    cli = root / "bin/litellm"
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/sh\n# from litellm import run_server\nprintf "%s\\n" "$@"\n')
    cli.chmod(0o755)
    meta = root / "lib/python3.12/site-packages/litellm-1.79.0.dist-info/METADATA"
    meta.parent.mkdir(parents=True)
    meta.write_text("Name: litellm\nVersion: 1.79.0\n")
    return root


def test_first_time_shell_loads_config_and_preserves_explicit_config(tmp_path):
    root = installation(tmp_path)
    profile = tmp_path / ".zshrc"
    profile.write_text("# existing shell settings\n")
    engine = Engine(
        Context(
            home=tmp_path,
            platform="darwin",
            env={"SHELL": "/bin/zsh"},
            roots=[root],
            use_path=False,
        )
    )
    plan = engine.preview(KEY, ["litellm"])
    assert KEY not in json.dumps(plan)
    tx = engine.apply(plan["plan_id"])
    config = tmp_path / ".config/litellm/cometapi-connect.yaml"
    script = config.parent / "launch.sh"
    if __import__("os").name != "nt":
        output = subprocess.check_output(["/bin/sh", str(script), "--port", "18109"], text=True)
        assert output.splitlines() == [
            "--config",
            str(config),
            "--host",
            "127.0.0.1",
            "--port",
            "18109",
        ]
        output = subprocess.check_output(
            ["/bin/sh", str(script), "--config", "/custom config.yaml"], text=True
        )
        assert output.splitlines() == ["--config", "/custom config.yaml"]
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, ["litellm"])["changes"])
    engine.restore(tx["transaction_id"])
    assert profile.read_text() == "# existing shell settings\n"
    assert not config.exists() and not script.exists()


def test_user_alias_is_not_overwritten(tmp_path):
    root = installation(tmp_path)
    profile = tmp_path / ".zshrc"
    profile.write_text('alias litellm="my-wrapper"\n')
    ctx = Context(
        home=tmp_path, platform="darwin", env={"SHELL": "/bin/zsh"}, roots=[root], use_path=False
    )
    with pytest.raises(SetupError, match="alias"):
        litellm_config.changes("LiteLLM", ctx, KEY, "gpt-4.1-mini")
    assert profile.read_text() == 'alias litellm="my-wrapper"\n'


@pytest.mark.parametrize("apps", [["gemini-cli", "litellm"], ["litellm", "gemini-cli"]])
def test_two_selected_apps_share_shell_without_losing_either_block(tmp_path, apps):
    from test_gemini_launcher import fixture

    ctx = fixture(tmp_path)
    ctx.roots.append(installation(tmp_path))
    profile = tmp_path / ".zshrc"
    original = profile.read_bytes()
    engine = Engine(ctx)
    plan = engine.preview(KEY, apps)
    assert len([c for c in plan["changes"] if c["path"] == str(profile)]) == 1
    tx = engine.apply(plan["plan_id"])
    assert (
        "function gemini()" in profile.read_text() and "function litellm()" in profile.read_text()
    )
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, apps)["changes"])
    engine.restore(tx["transaction_id"])
    assert profile.read_bytes() == original


def test_new_litellm_release_uses_same_cli_contract(tmp_path):
    root = installation(tmp_path)
    meta = next(root.glob("lib/python*/site-packages/*/METADATA"))
    meta.write_text("Name: litellm\nVersion: 99.0.0\n")
    ctx = Context(
        home=tmp_path, platform="darwin", env={"SHELL": "/bin/zsh"}, roots=[root], use_path=False
    )
    engine = Engine(ctx)
    plan = engine.preview(KEY, ["litellm"])
    tx = engine.apply(plan["plan_id"])
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, ["litellm"])["changes"])
    engine.restore(tx["transaction_id"])
    assert not (tmp_path / ".config/litellm/cometapi-connect.yaml").exists()
    (root / "bin/litellm").write_text("# another entry point")
    with pytest.raises(SetupError):
        litellm_config.cli_installation(ctx)
