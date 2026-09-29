"""Real Git and Compose checks for credential-bearing adapter outputs."""

import json
import os
import shutil
import subprocess

import pytest
from ruamel.yaml import YAML

from cometapi_helper import credential_safety
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-adapter-security-fixture-only"
OLD_KEY = "sk-previous-adapter-fixture-only"


def git(root, *args):
    executable = shutil.which("git")
    if not executable:
        pytest.skip("Git is needed for repository fixtures")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        [executable, "-C", str(root), *args],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )


def lobe(tmp_path, old_key="", env_file=None):
    root = tmp_path / "Projects/lobechat"
    marker = root / "src/server/modules/ModelRuntime/index.ts"
    marker.parent.mkdir(parents=True)
    marker.write_text("// source marker\n")
    (root / "package.json").write_text('{"name":"@lobehub/chat"}')
    service = {
        "image": "lobehub/lobe-chat:latest",
        "environment": {"OPENAI_API_KEY": old_key, "ACCESS_CODE": "keep-auth", "KEEP": "explicit"},
    }
    if env_file is not None:
        service["env_file"] = env_file
    compose = root / "compose.yaml"
    compose.write_text(json.dumps({"services": {"chat": service}}))
    return root, compose, Engine(Context(home=tmp_path, roots=[root], env={}, use_path=False))


@pytest.mark.parametrize("template", ["${OPENAI_API_KEY}", "$OPENAI_API_KEY", ""])
def test_tracked_lobechat_template_round_trip(tmp_path, template):
    root, compose, engine = lobe(tmp_path, template)
    original = compose.read_bytes()
    git(root, "init", "--quiet")
    git(root, "add", "compose.yaml")
    result = engine.apply(engine.preview(KEY, ["lobechat"])["plan_id"])
    assert KEY not in compose.read_text()
    git(root, "check-ignore", ".env.local", ".cometapi-connect.env")
    engine.restore(result["transaction_id"])
    assert compose.read_bytes() == original
    assert not (root / ".cometapi-connect.env").exists()


def test_restore_does_not_reinsert_old_inline_key_into_tracked_compose(tmp_path):
    root, compose, engine = lobe(tmp_path, OLD_KEY)
    git(root, "init", "--quiet")
    git(root, "add", "compose.yaml")
    result = engine.apply(engine.preview(KEY, ["lobechat"])["plan_id"])
    cleaned = compose.read_bytes()
    assert OLD_KEY.encode() not in cleaned and KEY.encode() not in cleaned
    git(root, "add", "compose.yaml")
    with pytest.raises(SetupError, match="tracked by Git"):
        engine.restore(result["transaction_id"])
    assert compose.read_bytes() == cleaned
    assert (root / ".cometapi-connect.env").exists()


@pytest.mark.parametrize(
    "env_file", ["existing.env", ["existing.env"], [{"path": "existing.env", "required": True}]]
)
def test_compose_renders_private_key_preserving_existing_environment(tmp_path, env_file):
    executable = shutil.which("docker")
    if not executable:
        pytest.skip("Docker Compose is needed to render the fixture")
    try:
        version = subprocess.run(
            [executable, "compose", "version"], capture_output=True, timeout=10
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker Compose is unavailable")
    if version.returncode:
        pytest.skip("Docker Compose is unavailable")
    root, compose, engine = lobe(tmp_path, "override-to-remove", env_file)
    (root / "existing.env").write_text(
        "KEEP=from-file\nOTHER=preserved\nOPENAI_API_KEY=old-file-value\n"
    )
    engine.apply(engine.preview(KEY, ["lobechat"])["plan_id"])
    rendered = subprocess.run(
        [executable, "compose", "-f", str(compose), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    environment = json.loads(rendered.stdout)["services"]["chat"]["environment"]
    assert environment["OPENAI_API_KEY"] == KEY
    assert environment["ACCESS_CODE"] == "keep-auth"
    assert environment["KEEP"] == "explicit" and environment["OTHER"] == "preserved"
    assert KEY not in compose.read_text()
    configured = YAML(typ="safe").load(compose.read_text())["services"]["chat"]
    assert configured["env_file"][-1] == "./.cometapi-connect.env"
    assert "OPENAI_API_KEY" not in configured["environment"]


def test_tracked_litellm_yaml_is_refused_without_writes(tmp_path):
    root = tmp_path / "proxy"
    root.mkdir()
    path = root / "litellm_config.yaml"
    path.write_text("model_list: []\n")
    git(root, "init", "--quiet")
    git(root, "add", path.name)
    engine = Engine(Context(home=tmp_path, roots=[root], env={}, use_path=False))
    with pytest.raises(SetupError, match="tracked by Git"):
        engine.preview(KEY, ["litellm"])
    assert path.read_text() == "model_list: []\n"
    assert not engine.ctx.state_dir.exists()


def test_native_database_directory_cannot_be_a_worktree(tmp_path):
    root = tmp_path / "leveldb"
    root.mkdir()
    git(root, "init", "--quiet", "--separate-git-dir", str(tmp_path / "administration"))
    (root / "000001.log").write_text("existing-credential-pages")
    git(root, "add", "000001.log")
    assert (root / ".git").is_file()
    with pytest.raises(SetupError):
        credential_safety.check_tracked(root)


def test_backup_protection_persists_after_restore_in_a_home_repository(tmp_path):
    git(tmp_path, "init", "--quiet")
    config = tmp_path / ".aider.conf.yml"
    original = ("openai-api-key: " + OLD_KEY + "\n").encode()
    config.write_bytes(original)
    engine = Engine(Context(home=tmp_path, env={}, use_path=False))
    plan = engine.preview(KEY, ["aider"])
    assert not engine.ctx.state_dir.exists()
    result = engine.apply(plan["plan_id"])
    backup_root = engine.ctx.state_dir / "backups"
    backups = list(backup_root.glob("*/*.bak"))
    assert any(OLD_KEY.encode() in p.read_bytes() for p in backups)
    for path in backups:
        git(tmp_path, "check-ignore", str(path))
    engine.restore(result["transaction_id"])
    assert config.read_bytes() == original
    assert (engine.ctx.state_dir / ".gitignore").exists()
    for path in backups:
        git(tmp_path, "check-ignore", str(path))


def test_existing_tracked_backups_block_setup_before_writing(tmp_path):
    git(tmp_path, "init", "--quiet")
    engine = Engine(Context(home=tmp_path, env={}, use_path=False))
    backup = engine.ctx.state_dir / "backups/old/0.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text(OLD_KEY)
    git(tmp_path, "add", str(backup))
    with pytest.raises(SetupError, match="tracked by Git"):
        engine.preview(KEY, ["aider"])
    assert not (tmp_path / ".aider.conf.yml").exists()
    assert backup.read_text() == OLD_KEY
