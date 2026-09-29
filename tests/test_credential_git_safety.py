"""Credential writes stay private in isolated, real Git repositories."""

import copy
import json
import os
import shutil
import subprocess

import pytest

from cometapi_helper import detection
from cometapi_helper import engine as engine_module
from cometapi_helper.adapters import Change
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine
from cometapi_helper.python_projects import CONFIG
from cometapi_helper.storage import read_file

KEY = "sk-git-safety-fixture-only"
OLD_KEY = "sk-previous-git-safety-fixture"
GIT = shutil.which("git")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "shell-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    # Restrict discovery to the fixture's Python app; never inspect real clients.
    entry = next(item for item in detection.catalog() if item["id"] == "openai-sdk")
    entry = copy.deepcopy(entry)
    entry["detection"]["app_names"] = []
    monkeypatch.setattr(detection, "catalog", lambda: [copy.deepcopy(entry)])


def git(root, *args, check=True):
    if GIT is None:
        pytest.skip("Git is required for repository fixtures")
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        [
            GIT,
            "-c",
            "user.name=CometAPI Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            *args,
        ],
        capture_output=True,
        text=True,
        check=check,
        env=environment,
        timeout=15,
    )


def project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "requirements.txt").write_text("openai\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from openai import OpenAI\nclient = OpenAI(timeout=17)\n", encoding="utf-8"
    )
    return root


def connection(root, key=OLD_KEY):
    path = root / CONFIG
    path.write_text(
        json.dumps(
            {
                "kind": "cometapi-python-project-v1",
                "schema": 1,
                "api_key": key,
                "base_url": "https://api.cometapi.com/v1",
                "chat_model": "test-model",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def make_engine(tmp_path, *roots):
    home = tmp_path / "application-home"
    home.mkdir(exist_ok=True)
    return Engine(Context(home=home, env={}, use_path=False, roots=list(roots)))


def preview(engine):
    return engine.preview(KEY, ["openai-sdk"], {"chat_model": "test-model"})


def assert_safe_error(error):
    assert KEY not in str(error.value)
    assert OLD_KEY not in str(error.value)


def fake_sidecar_adapter(monkeypatch, paths):
    def prepare(identity, name, context, key, models, repositories):
        return [
            Change(
                identity,
                name,
                path,
                read_file(path),
                (key + "\n").encode(),
                ["Private test connection"],
                contains_credentials=True,
                private_sidecar=True,
            )
            for path in paths
        ]

    monkeypatch.setattr(engine_module, "prepare", prepare)
    monkeypatch.setattr(
        engine_module,
        "scan",
        lambda context: (
            [{"id": "openai-sdk", "name": "Test app", "mode": "automatic"}],
            {},
        ),
    )


def test_tracked_connection_is_refused_even_when_ignored(tmp_path):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    path = connection(root)
    (root / ".gitignore").write_text("/" + CONFIG + "\n", encoding="utf-8")
    git(root, "add", "--force", "--", CONFIG)
    engine = make_engine(tmp_path, root)
    before = {item: item.read_bytes() for item in (path, root / "main.py", root / ".gitignore")}

    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert all(item.read_bytes() == value for item, value in before.items())
    assert not engine.ctx.state_dir.exists()


def test_untracked_connection_is_excluded_from_normal_commit(tmp_path):
    root = project(tmp_path / "project with spaces")
    git(root, "init", "--quiet")
    engine = make_engine(tmp_path, root)
    plan = preview(engine)
    assert not (root / CONFIG).exists()
    assert not (root / ".gitignore").exists()
    assert KEY not in json.dumps(plan)
    prepared = engine.plans[plan["plan_id"]][1]
    credentials = [
        change for change in prepared if not change.resource and KEY.encode() in change.after
    ]
    assert credentials
    assert all(change.contains_credentials for change in credentials)

    result = engine.apply(plan["plan_id"])
    assert json.loads((root / CONFIG).read_text())["api_key"] == KEY
    git(root, "check-ignore", "--", CONFIG)
    git(root, "add", "--all")
    assert CONFIG not in git(root, "ls-files").stdout.splitlines()
    git(root, "commit", "--quiet", "-m", "Fixture configuration")
    assert git(root, "grep", "--fixed-strings", KEY, "HEAD", check=False).returncode == 1
    assert KEY not in json.dumps(result)
    if os.name != "nt":
        assert (root / CONFIG).stat().st_mode & 0o777 == 0o600


def test_staging_connection_after_preview_blocks_apply_before_writes(tmp_path):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    path = connection(root)
    engine = make_engine(tmp_path, root)
    before = {item: item.read_bytes() for item in (path, root / "main.py")}
    plan = preview(engine)
    git(root, "add", "--force", "--", CONFIG)

    with pytest.raises(SetupError) as error:
        engine.apply(plan["plan_id"])

    assert_safe_error(error)
    assert all(item.read_bytes() == value for item, value in before.items())
    assert not (root / ".gitignore").exists()
    assert not (engine.ctx.state_dir / "backups").exists()


def test_nested_repository_cannot_hide_an_outer_tracked_connection(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "--quiet")
    root = project(outer / "nested")
    path = connection(root)
    git(outer, "add", "--", "nested/" + CONFIG)
    git(root, "init", "--quiet")
    assert not git(root, "ls-files").stdout
    engine = make_engine(tmp_path, root)
    before = path.read_bytes()

    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert path.read_bytes() == before
    assert not (root / ".gitignore").exists()


def test_untracked_connection_in_nested_repository_is_ignored(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "--quiet")
    root = project(outer / "nested")
    git(root, "init", "--quiet")
    engine = make_engine(tmp_path, root)

    engine.apply(preview(engine)["plan_id"])

    git(root, "check-ignore", "--", CONFIG)
    assert KEY in (root / CONFIG).read_text()


def test_linked_worktree_git_file_is_checked(tmp_path):
    primary = project(tmp_path / "primary")
    git(primary, "init", "--quiet")
    git(primary, "add", "--all")
    git(primary, "commit", "--quiet", "-m", "Fixture source")
    root = tmp_path / "linked worktree"
    git(primary, "worktree", "add", "--quiet", "-b", "test-linked", str(root))
    assert (root / ".git").is_file()
    engine = make_engine(tmp_path, root)
    result = engine.apply(preview(engine)["plan_id"])
    git(root, "check-ignore", "--", CONFIG)
    git(root, "add", "--force", "--", CONFIG)

    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert result["transaction_id"]


def test_missing_git_fails_closed_inside_repository(tmp_path, monkeypatch):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    engine = make_engine(tmp_path, root)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert not (root / CONFIG).exists()
    assert not (root / ".gitignore").exists()


def test_missing_git_allows_non_repository_private_sidecar(tmp_path, monkeypatch):
    root = project(tmp_path / "project")
    engine = make_engine(tmp_path, root)
    monkeypatch.setenv("PATH", "")

    engine.apply(preview(engine)["plan_id"])

    assert json.loads((root / CONFIG).read_text())["api_key"] == KEY
    assert "/" + CONFIG in (root / ".gitignore").read_text()


def test_broken_git_marker_fails_closed(tmp_path):
    root = project(tmp_path / "project")
    (root / ".git").write_text("gitdir: missing-administration-directory\n", encoding="utf-8")
    engine = make_engine(tmp_path, root)

    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert not (root / CONFIG).exists()


def test_git_timeout_fails_closed(tmp_path, monkeypatch):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    engine = make_engine(tmp_path, root)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert not (root / CONFIG).exists()


def test_inherited_git_overrides_cannot_hide_tracked_connection(tmp_path, monkeypatch):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    connection(root)
    git(root, "add", "--", CONFIG)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init", "--quiet")
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    monkeypatch.setenv("GIT_INDEX_FILE", str(unrelated / "different-index"))
    engine = make_engine(tmp_path, root)

    with pytest.raises(SetupError) as error:
        preview(engine)

    assert_safe_error(error)
    assert not (root / ".gitignore").exists()


def test_ignore_negation_is_overridden_and_restored_exactly(tmp_path):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    ignore = root / ".gitignore"
    original = ("# Keep project rules\n*.log\n/" + CONFIG + "\n!/" + CONFIG + "\n").encode()
    ignore.write_bytes(original)
    source = (root / "main.py").read_bytes()
    engine = make_engine(tmp_path, root)

    result = engine.apply(preview(engine)["plan_id"])
    git(root, "check-ignore", "--", CONFIG)
    second = preview(engine)
    assert all(change["action"] == "unchanged" for change in second["changes"])
    assert engine.apply(second["plan_id"])["transaction_id"] is None
    engine.restore(result["transaction_id"])

    assert ignore.read_bytes() == original
    assert (root / "main.py").read_bytes() == source
    assert not (root / CONFIG).exists()


def test_multiple_projects_have_independent_ignored_connections(tmp_path):
    roots = [project(tmp_path / "first"), project(tmp_path / "second")]
    for root in roots:
        git(root, "init", "--quiet")
    engine = make_engine(tmp_path, *roots)

    result = engine.apply(preview(engine)["plan_id"])
    for root in roots:
        git(root, "check-ignore", "--", CONFIG)
        assert json.loads((root / CONFIG).read_text())["api_key"] == KEY
    engine.restore(result["transaction_id"])

    assert all(not (root / CONFIG).exists() for root in roots)
    assert all(not (root / ".gitignore").exists() for root in roots)


def test_sidecars_share_one_ignore_change_and_restore(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    paths = [root / ".first-private.json", root / ".second-private.json"]
    fake_sidecar_adapter(monkeypatch, paths)
    engine = make_engine(tmp_path, root)
    plan = preview(engine)
    assert [item["path"] for item in plan["changes"]].count(str(root / ".gitignore")) == 1
    assert plan["changes"][0]["path"] == str(root / ".gitignore")

    result = engine.apply(plan["plan_id"])
    for path in paths:
        git(root, "check-ignore", "--", path.name)
    assert all(item["action"] == "unchanged" for item in preview(engine)["changes"])
    engine.restore(result["transaction_id"])

    assert all(not path.exists() for path in paths)
    assert not (root / ".gitignore").exists()


def test_git_index_is_rechecked_before_each_credential_write(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    paths = [root / ".first-private.json", root / ".second-private.json"]
    for path in paths:
        path.write_text(OLD_KEY + "\n", encoding="utf-8")
    fake_sidecar_adapter(monkeypatch, paths)
    engine = make_engine(tmp_path, root)
    plan = preview(engine)
    original_write = engine_module.write_target

    def stage_second_after_first(path, *args, **kwargs):
        result = original_write(path, *args, **kwargs)
        if path == paths[0] and path.read_text() == KEY + "\n":
            git(root, "add", "--force", "--", paths[1].name)
        return result

    monkeypatch.setattr(engine_module, "write_target", stage_second_after_first)
    with pytest.raises(SetupError) as error:
        engine.apply(plan["plan_id"])

    assert_safe_error(error)
    assert all(path.read_text() == OLD_KEY + "\n" for path in paths)
    assert not (root / ".gitignore").exists()
    assert engine.history()["transactions"][0]["status"] == "rolled_back"


def test_restore_refuses_writing_previous_credentials_to_tracked_file(tmp_path):
    root = project(tmp_path / "project")
    git(root, "init", "--quiet")
    path = connection(root)
    engine = make_engine(tmp_path, root)
    result = engine.apply(preview(engine)["plan_id"])
    current = path.read_bytes()
    source = (root / "main.py").read_bytes()
    git(root, "add", "--force", "--", CONFIG)

    with pytest.raises(SetupError) as error:
        engine.restore(result["transaction_id"])

    assert_safe_error(error)
    assert path.read_bytes() == current
    assert (root / "main.py").read_bytes() == source
    assert engine.history()["transactions"][0]["status"] == "applied"


def test_failed_credential_rollback_keeps_ignore_protection(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    paths = [root / ".first-private.json", root / ".second-private.json"]
    for path in paths:
        path.write_text(OLD_KEY + "\n", encoding="utf-8")
    fake_sidecar_adapter(monkeypatch, paths)
    engine = make_engine(tmp_path, root)
    plan = preview(engine)
    original_write = engine_module.write_target

    def fail_write_and_rollback(path, data, *args, **kwargs):
        if path == paths[1] and data == (KEY + "\n").encode():
            raise OSError("Injected second credential write failure")
        if path == paths[0] and data == (OLD_KEY + "\n").encode():
            raise OSError("Injected first credential rollback failure")
        return original_write(path, data, *args, **kwargs)

    monkeypatch.setattr(engine_module, "write_target", fail_write_and_rollback)
    with pytest.raises(SetupError) as error:
        engine.apply(plan["plan_id"])

    assert_safe_error(error)
    assert paths[0].read_text() == KEY + "\n"
    assert paths[1].read_text() == OLD_KEY + "\n"
    assert (root / ".gitignore").is_file()
    for path in paths:
        git(root, "check-ignore", "--", path.name)
    assert engine.history()["transactions"][0]["status"] == "rollback_failed"


@pytest.mark.parametrize("existing_credentials", [False, True])
def test_restore_keeps_ignore_until_credentials_are_restored(
    tmp_path, monkeypatch, existing_credentials
):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    paths = [root / ".first-private.json", root / ".second-private.json"]
    if existing_credentials:
        for path in paths:
            path.write_text(OLD_KEY + "\n", encoding="utf-8")
    fake_sidecar_adapter(monkeypatch, paths)
    engine = make_engine(tmp_path, root)
    result = engine.apply(preview(engine)["plan_id"])
    original_write = engine_module.write_target
    credential_writes = []

    def check_protection_during_restore(path, data, *args, **kwargs):
        if path in paths:
            assert (root / ".gitignore").is_file()
            git(root, "check-ignore", "--", path.name)
            credential_writes.append(path)
        return original_write(path, data, *args, **kwargs)

    monkeypatch.setattr(engine_module, "write_target", check_protection_during_restore)
    engine.restore(result["transaction_id"])

    assert credential_writes == paths
    assert not (root / ".gitignore").exists()
    if existing_credentials:
        assert all(path.read_text() == OLD_KEY + "\n" for path in paths)
    else:
        assert all(not path.exists() for path in paths)
    assert engine.history()["transactions"][0]["status"] == "restored"
