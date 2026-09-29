"""Configuration transactions tested only in temporary, isolated home directories."""

import copy
import json
import os
import stat
from pathlib import Path

import json5
import pytest
import tomlkit
from ruamel.yaml import YAML

from cometapi_helper import detection
from cometapi_helper import engine as engine_module
from cometapi_helper.common import ANTHROPIC_URL, BASE_URL, Context, SetupError
from cometapi_helper.engine import Engine
from cometapi_helper.storage import read_file

KEY = "sk-cometapi_test_secret_123456"
OLD_KEY = "sk-old_private_value_987654"
MODELS = {
    "chat_model": "test-chat-model",
    "claude_model": "claude-test-model",
    "codex_model": "test-responses-model",
}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # Keep actual detection of config folders/source markers, but do not inspect
    # globally installed applications on the developer's computer.
    entries = copy.deepcopy(detection.catalog())
    for entry in entries:
        entry.setdefault("detection", {})["app_names"] = []
    monkeypatch.setattr(detection, "catalog", lambda: copy.deepcopy(entries))
    return Engine(Context(home=tmp_path, env={}, use_path=False))


def put(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if mode is not None:
        path.chmod(mode)
    return path


def yaml_data(path):
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def apply_apps(engine, apps):
    return engine.apply(engine.preview(KEY, apps, MODELS)["plan_id"])


def find_app(engine, identity):
    return next(app for app in engine.scan()["apps"] if app["id"] == identity)


def manifest(engine, transaction_id):
    return engine.ctx.state_dir / "backups" / transaction_id / "manifest.json"


def source_checkout(engine, kind, name=None):
    root = engine.ctx.home / "Projects" / (name or kind)
    put(
        root / "package.json",
        json.dumps({"name": "chatgpt-next-web" if kind == "nextchat" else "librechat"}),
    )
    if kind == "nextchat":
        put(root / "app/api/common.ts", "// Marker only, never executed.\n")
    else:
        put(root / "librechat.example.yaml", "version: 1.3.1\n")
    return root


def test_preview_is_read_only_and_redacts_both_credentials(engine):
    path = put(
        engine.ctx.home / ".claude/settings.json",
        json.dumps({"env": {"ANTHROPIC_API_KEY": OLD_KEY}, "theme": "dark"}),
    )
    before = path.read_bytes()
    preview = engine.preview(KEY, ["claude-code"], MODELS)
    assert preview["changes"][0]["action"] == "update"
    assert KEY not in json.dumps(preview)
    assert OLD_KEY not in json.dumps(preview)
    assert path.read_bytes() == before
    assert not engine.ctx.state_dir.exists()


def test_multiple_apps_preserve_settings_and_restore_exact_bytes_and_modes(engine):
    claude = put(
        engine.ctx.home / ".claude/settings.json",
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "env": {"OTHER_SECRET": OLD_KEY, "ANTHROPIC_API_KEY": OLD_KEY},
            }
        ),
        0o640,
    )
    codex = put(
        engine.ctx.home / ".codex/config.toml",
        '# keep comment\napproval_policy = "on-request"\n[projects."/example"]\ntrust_level = "trusted"\n',
        0o644,
    )
    aider = put(
        engine.ctx.home / ".aider.conf.yml",
        "# keep YAML comment\nread: [CONVENTIONS.md]\nweak-model: old-model\n",
        0o600,
    )
    before = {path: path.read_bytes() for path in (claude, codex, aider)}
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in before}
    result = apply_apps(engine, ["claude-code", "codex", "aider", "claude-code"])
    assert len(result["changes"]) == 3
    saved = json.loads(claude.read_text())
    assert saved["permissions"] == {"allow": ["Read"]}
    assert saved["env"]["OTHER_SECRET"] == OLD_KEY
    assert "ANTHROPIC_API_KEY" not in saved["env"]
    assert saved["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert saved["env"]["ANTHROPIC_BASE_URL"] == ANTHROPIC_URL
    assert "# keep comment" in codex.read_text()
    assert tomlkit.parse(codex.read_text())["projects"]["/example"]["trust_level"] == "trusted"
    assert "# keep YAML comment" in aider.read_text()
    assert yaml_data(aider)["read"] == ["CONVENTIONS.md"]
    assert yaml_data(aider)["weak-model"] == "openai/test-chat-model"
    for path in before:
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    public = json.dumps(result) + json.dumps(engine.history())
    assert KEY not in public and OLD_KEY not in public
    engine.restore(result["transaction_id"])
    for path in before:
        assert path.read_bytes() == before[path]
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == modes[path]
    assert engine.history()["transactions"][0]["status"] == "restored"


def test_codex_replaces_mutually_exclusive_auth_without_touching_other_providers(engine):
    path = put(
        engine.ctx.home / ".codex/config.toml",
        """
[model_providers.other]
base_url = "https://example.invalid/v1"
env_key = "OTHER_KEY"
[model_providers.cometapi]
env_key = "OLD_KEY"
[model_providers.cometapi.auth]
command = "old-credential-command"
""",
    )
    apply_apps(engine, ["codex"])
    document = tomlkit.parse(path.read_text())
    provider = document["model_providers"]["cometapi"]
    assert provider["experimental_bearer_token"] == KEY
    assert provider["wire_api"] == "responses"
    assert "env_key" not in provider and "auth" not in provider
    assert document["model_providers"]["other"]["env_key"] == "OTHER_KEY"


def test_every_automatic_adapter_is_idempotent(engine):
    source_checkout(engine, "nextchat")
    source_checkout(engine, "librechat")
    put(engine.ctx.home / ".config/opencode/opencode.json", '{"provider": {}}')
    apps = ["claude-code", "codex", "aider", "continue", "opencode", "nextchat", "librechat"]
    first = apply_apps(engine, apps)
    paths = [Path(item["path"]) for item in first["changes"]]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    preview = engine.preview(KEY, apps, MODELS)
    assert all(item["action"] == "unchanged" for item in preview["changes"])
    second = engine.apply(preview["plan_id"])
    assert second["transaction_id"] is None
    assert len(engine.history()["transactions"]) == 1
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == before[path] for path in paths)


def test_restore_removes_files_created_by_setup(engine):
    result = apply_apps(engine, ["claude-code", "codex", "continue"])
    paths = [Path(item["path"]) for item in result["changes"]]
    engine.restore(result["transaction_id"])
    assert all(not path.exists() for path in paths)
    with pytest.raises(SetupError, match="not an applied"):
        engine.restore(result["transaction_id"])


def test_apply_refuses_concurrent_changes_before_any_writes(engine):
    claude = put(engine.ctx.home / ".claude/settings.json", "{}")
    aider = put(engine.ctx.home / ".aider.conf.yml", "model: original\n")
    preview = engine.preview(KEY, ["claude-code", "aider"], MODELS)
    aider.write_text("model: changed-outside-helper\n")
    with pytest.raises(SetupError, match="changed after preview"):
        engine.apply(preview["plan_id"])
    assert claude.read_text() == "{}"
    assert aider.read_text() == "model: changed-outside-helper\n"


def test_restore_refuses_newer_config_before_restoring_any_file(engine):
    result = apply_apps(engine, ["claude-code", "aider"])
    claude = engine.ctx.home / ".claude/settings.json"
    aider = engine.ctx.home / ".aider.conf.yml"
    current_claude = claude.read_bytes()
    aider.write_text("# user's newer configuration\n")
    with pytest.raises(SetupError, match="newer settings were kept"):
        engine.restore(result["transaction_id"])
    assert claude.read_bytes() == current_claude
    assert aider.read_text() == "# user's newer configuration\n"


def test_backup_tampering_fails_integrity_check(engine):
    claude = put(engine.ctx.home / ".claude/settings.json", "{}")
    result = apply_apps(engine, ["claude-code"])
    current = claude.read_bytes()
    backup = manifest(engine, result["transaction_id"]).parent / "0.bak"
    backup.write_text("tampered original")
    with pytest.raises(SetupError, match="integrity"):
        engine.restore(result["transaction_id"])
    assert claude.read_bytes() == current


def test_apply_failure_rolls_back_completed_files(engine, monkeypatch):
    claude = put(engine.ctx.home / ".claude/settings.json", "{}", 0o640)
    aider = put(engine.ctx.home / ".aider.conf.yml", "model: original\n")
    preview = engine.preview(KEY, ["claude-code", "aider"], MODELS)
    original_write = engine_module.atomic_write

    def fail_second_file(path, data, mode=0o600):
        if path == aider:
            raise OSError("write denied " + KEY)
        original_write(path, data, mode)

    monkeypatch.setattr(engine_module, "atomic_write", fail_second_file)
    with pytest.raises(SetupError, match="rolled back") as error:
        engine.apply(preview["plan_id"])
    assert KEY not in str(error.value)
    assert claude.read_text() == "{}"
    assert aider.read_text() == "model: original\n"
    if os.name != "nt":
        assert stat.S_IMODE(claude.stat().st_mode) == 0o640
    assert engine.history()["transactions"][0]["status"] == "rolled_back"


def test_apply_rollback_deletes_a_newly_created_file(engine, monkeypatch):
    preview = engine.preview(KEY, ["claude-code", "aider"], MODELS)
    original_write = engine_module.atomic_write
    aider = engine.ctx.home / ".aider.conf.yml"

    def fail_second_file(path, data, mode=0o600):
        if path == aider:
            raise PermissionError("injected failure")
        original_write(path, data, mode)

    monkeypatch.setattr(engine_module, "atomic_write", fail_second_file)
    with pytest.raises(SetupError):
        engine.apply(preview["plan_id"])
    assert not (engine.ctx.home / ".claude/settings.json").exists()
    assert not aider.exists()


def test_rollback_does_not_overwrite_an_external_edit(engine, monkeypatch):
    claude = put(engine.ctx.home / ".claude/settings.json", "{}")
    aider = engine.ctx.home / ".aider.conf.yml"
    preview = engine.preview(KEY, ["claude-code", "aider"], MODELS)
    original_write = engine_module.atomic_write

    def concurrent_change_then_failure(path, data, mode=0o600):
        if path == aider:
            claude.write_text('{"external": true}')
            raise OSError("injected failure")
        original_write(path, data, mode)

    monkeypatch.setattr(engine_module, "atomic_write", concurrent_change_then_failure)
    with pytest.raises(SetupError, match="could not be rolled back"):
        engine.apply(preview["plan_id"])
    assert json.loads(claude.read_text()) == {"external": True}
    assert engine.history()["transactions"][0]["status"] == "rollback_failed"


def test_restore_failure_undoes_completed_restore_steps(engine, monkeypatch):
    claude = put(engine.ctx.home / ".claude/settings.json", "{}")
    aider = put(engine.ctx.home / ".aider.conf.yml", "model: original\n")
    result = apply_apps(engine, ["claude-code", "aider"])
    current = {path: path.read_bytes() for path in (claude, aider)}
    original_write = engine_module.atomic_write

    def fail_second_restore(path, data, mode=0o600):
        if path == aider:
            raise OSError("injected failure")
        original_write(path, data, mode)

    monkeypatch.setattr(engine_module, "atomic_write", fail_second_restore)
    with pytest.raises(SetupError, match="restore steps were undone"):
        engine.restore(result["transaction_id"])
    assert all(path.read_bytes() == current[path] for path in current)
    assert json.loads(manifest(engine, result["transaction_id"]).read_text())["status"] == "applied"
    monkeypatch.setattr(engine_module, "atomic_write", original_write)
    engine.restore(result["transaction_id"])
    assert claude.read_text() == "{}" and aider.read_text() == "model: original\n"


@pytest.mark.parametrize("link_kind", ["file", "parent", "dangling", "hardlink"])
def test_configuration_links_are_refused(engine, link_kind):
    outside = put(engine.ctx.home / "untouched/actual.json", "{}")
    destination = engine.ctx.home / ".claude/settings.json"
    destination.parent.mkdir()
    try:
        if link_kind == "parent":
            destination.parent.rmdir()
            destination.parent.symlink_to(outside.parent, target_is_directory=True)
        elif link_kind == "hardlink":
            os.link(outside, destination)
        else:
            destination.symlink_to(
                outside if link_kind == "file" else outside.parent / "missing.json"
            )
    except OSError as error:
        pytest.skip("Platform cannot create this test link: " + type(error).__name__)
    with pytest.raises(SetupError):
        engine.preview(KEY, ["claude-code"], MODELS)
    assert outside.read_text() == "{}"
    assert not engine.ctx.state_dir.exists()


def test_symlink_swap_after_preview_is_refused(engine, require_symlinks):
    path = put(engine.ctx.home / ".claude/settings.json", "{}")
    other = put(engine.ctx.home / "other.json", "{}")
    preview = engine.preview(KEY, ["claude-code"], MODELS)
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(SetupError, match="symbolic link"):
        engine.apply(preview["plan_id"])
    assert other.read_text() == "{}"


def test_symlinked_state_directory_is_refused(engine, require_symlinks):
    other = engine.ctx.home / "other-state"
    other.mkdir()
    engine.ctx.state_dir.symlink_to(other, target_is_directory=True)
    with pytest.raises(SetupError, match="symbolic link"):
        engine.preview(KEY, ["claude-code"], MODELS)
    assert list(other.iterdir()) == []
    assert not (engine.ctx.home / ".claude/settings.json").exists()


@pytest.mark.parametrize(
    "identity,relative,text",
    [
        ("claude-code", ".claude/settings.json", '{"env": {"key": "' + OLD_KEY + '"}'),
        ("claude-code", ".claude/settings.json", '{"env": {}, "env": {"old": "' + OLD_KEY + '"}}'),
        ("claude-code", ".claude/settings.json", '["' + OLD_KEY + '"]'),
        ("claude-code", ".claude/settings.json", '{"env": "' + OLD_KEY + '"}'),
        (
            "aider",
            ".aider.conf.yml",
            "openai-api-key: " + OLD_KEY + "\nopenai-api-key: duplicate\n",
        ),
        ("codex", ".codex/config.toml", 'model = "' + OLD_KEY + '"\nmodel = "duplicate"\n'),
        ("continue", ".continue/config.yaml", "schema: v99\nmodels: []\n"),
        ("continue", ".continue/config.yaml", "schema: v1\nmodels: wrong-type\n"),
        (
            "opencode",
            ".config/opencode/opencode.json",
            '{"provider": {}, "secret": "' + OLD_KEY + '"',
        ),
    ],
)
def test_invalid_config_fails_without_writes_or_secret_diagnostics(
    engine, identity, relative, text
):
    path = put(engine.ctx.home / relative, text)
    with pytest.raises(SetupError) as error:
        engine.preview(KEY, [identity], MODELS)
    assert KEY not in str(error.value) and OLD_KEY not in str(error.value)
    assert path.read_text() == text
    assert not engine.ctx.state_dir.exists()


@pytest.mark.parametrize(
    "value", [None, "", "wrong-key", "sk-short", KEY + "\n", KEY + "$(bad)", [KEY]]
)
def test_bad_key_inputs_are_rejected_without_echoing_input(engine, value):
    with pytest.raises(SetupError) as error:
        engine.preview(value, ["claude-code"], MODELS)
    assert KEY not in str(error.value)
    assert not engine.ctx.state_dir.exists()


@pytest.mark.parametrize("apps", [None, [], [123], ["unknown-client"], ["cursor"], "claude-code"])
def test_invalid_or_guided_app_selection_cannot_write(engine, apps):
    with pytest.raises(SetupError):
        engine.preview(KEY, apps, MODELS)
    assert not engine.ctx.state_dir.exists()


def test_model_input_cannot_inject_environment_lines(engine):
    with pytest.raises(SetupError):
        engine.preview(KEY, ["aider"], {"chat_model": "model\nOPENAI_API_KEY=evil"})
    assert not (engine.ctx.home / ".aider.conf.yml").exists()


def test_expired_plan_and_used_plan_cannot_apply(engine, monkeypatch):
    preview = engine.preview(KEY, ["aider"], MODELS)
    created, changes = engine.plans[preview["plan_id"]]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: created + 601)
    with pytest.raises(SetupError, match="expired"):
        engine.apply(preview["plan_id"])
    assert not (engine.ctx.home / ".aider.conf.yml").exists()
    preview = engine.preview(KEY, ["aider"], MODELS)
    engine.apply(preview["plan_id"])
    with pytest.raises(SetupError, match="already used"):
        engine.apply(preview["plan_id"])


def test_opencode_schema_and_other_providers_are_preserved(engine):
    key = "provider"
    path = put(
        engine.ctx.home / ".config/opencode/opencode.jsonc",
        "// existing comment\n"
        + json.dumps(
            {
                key: {"other": {"name": "Other", "options": {"private": OLD_KEY}}},
                "permission": {"bash": "ask"},
            }
        ),
    )
    apply_apps(engine, ["opencode"])
    document = json5.loads(path.read_text())
    assert document["permission"] == {"bash": "ask"}
    assert document[key]["other"]["options"]["private"] == OLD_KEY
    assert document["model"] == "cometapi/test-chat-model"
    entry = document[key]["cometapi"]
    assert entry["models"]["test-chat-model"]["name"] == "test-chat-model"
    assert entry["npm"] == "@ai-sdk/openai-compatible"
    assert entry["options"] == {"baseURL": BASE_URL, "apiKey": KEY}


@pytest.mark.parametrize(
    "commands,mode",
    [
        (set(), "automatic"),
        ({"opencode"}, "automatic"),
        ({"opencode2"}, "guided"),
        ({"opencode", "opencode2"}, "automatic"),
    ],
)
def test_opencode_v1_config_respects_detected_commands(engine, monkeypatch, commands, mode):
    path = put(engine.ctx.home / ".config/opencode/opencode.json", '{"provider": {}}')
    before = path.read_bytes()
    engine.ctx.use_path = True
    monkeypatch.setattr(
        detection.shutil,
        "which",
        lambda command, path: "/isolated/bin/" + command if command in commands else None,
    )
    assert find_app(engine, "opencode")["mode"] == mode
    if mode == "guided":
        with pytest.raises(SetupError, match="guided setup"):
            engine.preview(KEY, ["opencode"], MODELS)
    else:
        assert engine.preview(KEY, ["opencode"], MODELS)["changes"][0]["action"] == "update"
    assert path.read_bytes() == before


@pytest.mark.parametrize("model_name", [None, "My compact model"])
def test_opencode_existing_selected_model_settings_are_preserved(engine, model_name):
    model = {"limit": {"context": 32768, "output": 2048}, "options": {"temperature": 0}}
    if model_name is not None:
        model["name"] = model_name
    other_model = {"name": "Other model", "limit": {"output": 1024}}
    path = put(
        engine.ctx.home / ".config/opencode/opencode.json",
        json.dumps(
            {
                "provider": {
                    "cometapi": {"models": {MODELS["chat_model"]: model, "other": other_model}}
                },
            }
        ),
    )
    apply_apps(engine, ["opencode"])
    models = json.loads(path.read_text())["provider"]["cometapi"]["models"]
    assert models[MODELS["chat_model"]] == dict(model, name=model_name or MODELS["chat_model"])
    assert models["other"] == other_model


@pytest.mark.parametrize("model", [None, [], "unexpected", 1])
def test_opencode_malformed_selected_model_is_not_modified(engine, model):
    path = put(
        engine.ctx.home / ".config/opencode/opencode.json",
        json.dumps(
            {
                "provider": {"cometapi": {"models": {MODELS["chat_model"]: model}}},
            }
        ),
    )
    before = path.read_bytes()
    with pytest.raises(SetupError, match="unexpected format"):
        engine.preview(KEY, ["opencode"], MODELS)
    assert path.read_bytes() == before
    assert not engine.ctx.state_dir.exists()


def test_opencode_beta_shape_does_not_prove_runtime_compatibility(engine):
    path = put(
        engine.ctx.home / ".config/opencode/opencode.jsonc",
        '{"providers": {"existing": {"name": "Keep"}}, "model": "existing/model"}',
    )
    before = path.read_bytes()
    assert find_app(engine, "opencode")["mode"] == "guided"
    with pytest.raises(SetupError):
        engine.preview(KEY, ["opencode"], MODELS)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "document", [{"provider": {}, "providers": {}}, {"provider": []}, {"providers": None}]
)
def test_ambiguous_or_invalid_opencode_schema_is_not_modified(engine, document):
    path = put(engine.ctx.home / ".config/opencode/opencode.json", json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(SetupError):
        engine.preview(KEY, ["opencode"], MODELS)
    assert path.read_bytes() == before


def test_opencode_both_config_filenames_require_guidance(engine):
    put(engine.ctx.home / ".config/opencode/opencode.json", '{"provider": {}}')
    put(engine.ctx.home / ".config/opencode/opencode.jsonc", '{"providers": {}}')
    assert find_app(engine, "opencode")["mode"] == "guided"
    with pytest.raises(SetupError):
        engine.preview(KEY, ["opencode"], MODELS)


@pytest.mark.parametrize("initial", [None, "{}", '{"permission":{"bash":"ask"}}'])
def test_opencode_initializes_documented_v1_schema_without_manual_setup(engine, initial):
    path = engine.ctx.xdg / "opencode/opencode.json"
    if initial is not None:
        put(path, initial)
    result = apply_apps(engine, ["opencode"])
    doc = json.loads(path.read_text())
    assert doc["model"] == "cometapi/test-chat-model"
    assert doc["provider"]["cometapi"]["options"]["apiKey"] == KEY
    if initial and "permission" in initial:
        assert doc["permission"] == {"bash": "ask"}
    engine.restore(result["transaction_id"])
    assert path.read_text() == initial if initial is not None else not path.exists()


def test_continue_legacy_configuration_is_not_shadowed(engine):
    legacy = put(engine.ctx.home / ".continue/config.json", '{"models": [{"title": "Existing"}]}')
    with pytest.raises(SetupError, match="legacy"):
        engine.preview(KEY, ["continue"], MODELS)
    assert legacy.read_text() == '{"models": [{"title": "Existing"}]}'
    assert not legacy.with_suffix(".yaml").exists()


def test_continue_preserves_other_models_roles_and_rules(engine):
    path = put(
        engine.ctx.home / ".continue/config.yaml",
        """name: My Assistant
version: 2.0.0
schema: v1
rules: [Keep comments]
models:
  - name: Local Autocomplete
    provider: ollama
    model: local-model
    roles: [autocomplete]
""",
    )
    apply_apps(engine, ["continue"])
    saved = yaml_data(path)
    assert saved["name"] == "My Assistant" and saved["version"] == "2.0.0"
    assert saved["rules"] == ["Keep comments"]
    assert saved["models"][1] == {
        "name": "Local Autocomplete",
        "provider": "ollama",
        "model": "local-model",
        "roles": ["autocomplete"],
    }
    assert saved["models"][0]["apiKey"] == KEY


@pytest.mark.parametrize("kind", ["nextchat", "librechat"])
def test_source_marker_detects_renamed_repository_and_configures_it(engine, kind):
    root = source_checkout(engine, kind, name="renamed-checkout")
    app = find_app(engine, kind)
    assert app["detected"] is True and app["mode"] == "automatic"
    result = apply_apps(engine, [kind])
    assert all(Path(item["path"]).parent == root for item in result["changes"])
    if kind == "nextchat":
        assert "BASE_URL=" + ANTHROPIC_URL in (root / ".env.local").read_text()
        assert "OPENAI_API_KEY=" + KEY in (root / ".env.local").read_text()
        assert "DEFAULT_MODEL=test-chat-model@openai" in (root / ".env.local").read_text()
        assert 'CUSTOM_MODELS="+test-chat-model@openai"' in (root / ".env.local").read_text()
    else:
        saved = yaml_data(root / "librechat.yaml")
        assert saved["version"] == "1.3.1"
        endpoint = saved["endpoints"]["custom"][0]
        assert endpoint["apiKey"] == "${COMETAPI_API_KEY}"
        assert endpoint["baseURL"] == BASE_URL
        assert KEY not in (root / "librechat.yaml").read_text()
        assert "COMETAPI_API_KEY=" + KEY in (root / ".env").read_text()
    engine.restore(result["transaction_id"])
    assert all(not Path(item["path"]).exists() for item in result["changes"])


@pytest.mark.parametrize("kind", ["nextchat", "librechat"])
def test_repository_name_alone_never_authorizes_source_config(engine, kind):
    root = engine.ctx.home / "Projects" / kind
    put(root / "package.json", json.dumps({"name": kind}))
    assert find_app(engine, kind)["mode"] == "guided"
    with pytest.raises(SetupError):
        engine.preview(KEY, [kind], MODELS)
    assert not (root / ".env").exists() and not (root / ".env.local").exists()


def test_multiple_source_checkouts_are_in_one_transaction(engine):
    first = source_checkout(engine, "nextchat", "first")
    second = source_checkout(engine, "nextchat", "second")
    result = apply_apps(engine, ["nextchat"])
    assert {Path(item["path"]) for item in result["changes"]} == {
        first / ".env.local",
        second / ".env.local",
        first / ".gitignore",
        second / ".gitignore",
    }
    engine.restore(result["transaction_id"])
    assert not (first / ".env.local").exists() and not (second / ".env.local").exists()
    assert not (first / ".gitignore").exists() and not (second / ".gitignore").exists()


def test_environment_file_preserves_comments_unrelated_keys_and_line_endings(engine):
    root = source_checkout(engine, "nextchat")
    path = put(
        root / ".env.local",
        "# my configuration\r\nKEEP_ME=" + OLD_KEY + "\r\nexport OPENAI_API_KEY=old-value\r\n",
    )
    before = path.read_bytes()
    result = apply_apps(engine, ["nextchat"])
    saved = path.read_bytes()
    assert saved.startswith(("# my configuration\r\nKEEP_ME=" + OLD_KEY + "\r\n").encode())
    assert b"\n" not in saved.replace(b"\r\n", b"")
    assert saved.count(b"OPENAI_API_KEY=") == 1
    engine.restore(result["transaction_id"])
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "text",
    ["OPENAI_API_KEY=first\nOPENAI_API_KEY=second\n", 'OPENAI_API_KEY="multiline\nsecret"\n'],
)
def test_ambiguous_target_env_entries_are_not_rewritten(engine, text):
    root = source_checkout(engine, "nextchat")
    path = put(root / ".env.local", text)
    with pytest.raises(SetupError):
        engine.preview(KEY, ["nextchat"], MODELS)
    assert path.read_text() == text


@pytest.mark.parametrize(
    "text",
    [
        'UNRELATED="first line\nOPENAI_API_KEY=not-an-assignment\nlast line"\n',
        'UNRELATED="escaped final quote\\"\nOPENAI_API_KEY=not-an-assignment\n',
    ],
)
def test_target_looking_lines_inside_unrelated_multiline_values_are_refused(engine, text):
    root = source_checkout(engine, "nextchat")
    path = put(root / ".env.local", text)
    with pytest.raises(SetupError, match="multiline"):
        engine.preview(KEY, ["nextchat"], MODELS)
    assert path.read_text() == text
    assert KEY not in path.read_text()


def test_valid_quoted_env_values_with_comments_are_preserved(engine):
    root = source_checkout(engine, "nextchat")
    unrelated = "UNRELATED=\"quoted value\" # explanation\nSINGLE='single quoted' # comment\n"
    path = put(root / ".env.local", unrelated + 'OPENAI_API_KEY="old-key" # target comment\n')
    apply_apps(engine, ["nextchat"])
    assert path.read_text().startswith(unrelated)
    assert "OPENAI_API_KEY=" + KEY in path.read_text()


@pytest.mark.parametrize(
    "existing",
    [
        "-all,+other@openai=Other Model",
        '"-all,+other@openai=Other Model"',
        "'-all,+other@openai=Other Model'",
        '"-all,+other@openai=Other Model" # existing comment',
    ],
)
def test_nextchat_custom_models_merge_preserves_existing_directives(engine, existing):
    root = source_checkout(engine, "nextchat")
    path = put(root / ".env.local", "CODE=keep-access-code\nCUSTOM_MODELS=" + existing + "\n")
    apply_apps(engine, ["nextchat"])
    line = next(line for line in path.read_text().splitlines() if line.startswith("CUSTOM_MODELS="))
    assert (
        json.loads(line.split("=", 1)[1])
        == "-all,+other@openai=Other Model,+test-chat-model@openai"
    )
    assert "CODE=keep-access-code\n" in path.read_text()
    preview = engine.preview(KEY, ["nextchat"], MODELS)
    assert preview["changes"][0]["action"] == "unchanged"


@pytest.mark.parametrize(
    "existing,expected",
    [
        ("+test-chat-model@openai=My alias", "+test-chat-model@openai=My alias"),
        ("+test-chat-model@OpenAI=My alias", "+test-chat-model@OpenAI=My alias"),
        ("-test-chat-model@openai", "-test-chat-model@openai,+test-chat-model@openai"),
        ("+test-chat-model@openai,-all", "+test-chat-model@openai,-all,+test-chat-model@openai"),
        (
            "+test-chat-model@openai,-test-chat-model",
            "+test-chat-model@openai,-test-chat-model,+test-chat-model@openai",
        ),
        (
            "+test-chat-model@openai,-test-chat-model,+all",
            "+test-chat-model@openai,-test-chat-model,+all",
        ),
        ("test-chat-model@anthropic", "test-chat-model@anthropic,+test-chat-model@openai"),
    ],
)
def test_nextchat_existing_route_aliases_and_disable_directives(engine, existing, expected):
    root = source_checkout(engine, "nextchat")
    path = put(root / ".env.local", "CUSTOM_MODELS=" + json.dumps(existing) + "\n")
    apply_apps(engine, ["nextchat"])
    line = next(line for line in path.read_text().splitlines() if line.startswith("CUSTOM_MODELS="))
    assert json.loads(line.split("=", 1)[1]) == expected


@pytest.mark.parametrize(
    "value",
    [
        "$OTHER_MODELS",
        "${OTHER_MODELS},+other",
        '"${OTHER_MODELS}"',
        '"other\\nmodel"',
        "`external-list`",
    ],
)
def test_nextchat_unresolved_custom_model_values_stop_all_configuration(engine, value):
    root = source_checkout(engine, "nextchat")
    path = put(root / ".env.local", "CUSTOM_MODELS=" + value + "\n")
    before = path.read_bytes()
    with pytest.raises(SetupError) as error:
        engine.preview(KEY, ["claude-code", "nextchat"], MODELS)
    assert KEY not in str(error.value)
    assert path.read_bytes() == before
    assert not (engine.ctx.home / ".claude/settings.json").exists()


def test_nextchat_non_gpt_model_uses_openai_route_and_warns_about_upstream_logs(engine):
    root = source_checkout(engine, "nextchat")
    models = dict(MODELS, chat_model="claude-custom-model")
    preview = engine.preview(KEY, ["nextchat"], models)
    assert any("logs selected API keys" in item for item in preview["warnings"])
    assert KEY not in json.dumps(preview)
    engine.apply(preview["plan_id"])
    saved = (root / ".env.local").read_text()
    assert "DEFAULT_MODEL=claude-custom-model@openai\n" in saved
    assert 'CUSTOM_MODELS="+claude-custom-model@openai"\n' in saved


def test_librechat_keeps_other_endpoints_and_environment_values(engine):
    root = source_checkout(engine, "librechat")
    config = put(
        root / "librechat.yaml",
        """version: 1.3.1
interface:
  privacyPolicy:
    externalUrl: https://example.invalid/privacy
endpoints:
  custom:
    - name: Existing
      apiKey: ${OTHER_KEY}
      baseURL: https://example.invalid/v1
""",
    )
    dotenv = put(root / ".env", "# existing config\nOTHER_KEY=" + OLD_KEY + "\n")
    apply_apps(engine, ["librechat"])
    saved = yaml_data(config)
    assert saved["interface"]["privacyPolicy"]["externalUrl"] == "https://example.invalid/privacy"
    assert saved["endpoints"]["custom"][0]["name"] == "Existing"
    assert saved["endpoints"]["custom"][1]["name"] == "CometAPI"
    assert "OTHER_KEY=" + OLD_KEY in dotenv.read_text()


@pytest.mark.parametrize(
    "variable,identity,filename",
    [
        ("CLAUDE_CONFIG_DIR", "claude-code", "settings.json"),
        ("CODEX_HOME", "codex", "config.toml"),
        ("CONTINUE_GLOBAL_DIR", "continue", "config.yaml"),
    ],
)
def test_absolute_config_directory_override(engine, variable, identity, filename):
    target = engine.ctx.home / "relocated" / identity
    engine.ctx.env[variable] = str(target)
    result = apply_apps(engine, [identity])
    assert result["changes"][0]["path"] == str(target / filename)
    assert target.joinpath(filename).is_file()


def test_read_file_rejects_non_regular_and_oversized_content(engine):
    path = engine.ctx.home / "directory"
    path.mkdir()
    with pytest.raises(SetupError, match="regular"):
        read_file(path)
    large = engine.ctx.home / "large.json"
    with large.open("wb") as stream:
        stream.truncate(4 * 1024 * 1024 + 1)
    with pytest.raises(SetupError, match="too large"):
        read_file(large)


@pytest.mark.parametrize("identity", ["../other", "/tmp/other", "invalid", None])
def test_restore_rejects_non_transaction_identifiers(engine, identity):
    with pytest.raises(SetupError, match="Invalid backup ID"):
        engine.restore(identity)
