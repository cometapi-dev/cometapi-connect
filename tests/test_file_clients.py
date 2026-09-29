import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from cometapi_helper.common import BASE_URL, Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-test_configuration_only_123456"


@pytest.mark.parametrize(
    "environment",
    [
        {"OPENAI_API_KEY": "", "ACCESS_CODE": "preserve-auth", "PASSTHROUGH": None},
        ["OPENAI_API_KEY=", "ACCESS_CODE=preserve-auth", "PASSTHROUGH"],
    ],
)
def test_lobechat_compose_overrides_image_defaults_and_restores(tmp_path, environment):
    root = installation(tmp_path, "lobechat")
    path = root / "compose.yaml"
    service = {
        "image": "lobehub/lobe-chat@sha256:abc",
        "user": "501:20",
        "ports": ["127.0.0.1:18104:3210"],
        "volumes": ["./.env.local:/app/.env.local:ro"],
        "environment": environment,
    }
    other = {"image": "postgres:16", "environment": {"POSTGRES_USER": "keep"}}
    path.write_text(json.dumps({"services": {"chat": service, "database": other}}))
    before = path.read_bytes()
    engine = Engine(Context(home=tmp_path, platform="darwin", env={}, use_path=False))
    plan = engine.preview(KEY, ["lobechat"])
    assert KEY not in json.dumps(plan)
    result = engine.apply(plan["plan_id"])
    updated = YAML(typ="safe").load(path.read_text())["services"]
    assert updated["database"] == other
    assert {k: v for k, v in updated["chat"].items() if k not in ("environment", "env_file")} == {
        k: v for k, v in service.items() if k not in ("environment", "env_file")
    }
    env = updated["chat"]["environment"]
    assert "OPENAI_API_KEY" not in env and env["OPENAI_PROXY_URL"] == BASE_URL
    assert updated["chat"]["env_file"] == ["./.cometapi-connect.env"]
    assert KEY not in path.read_text()
    assert (root / ".cometapi-connect.env").read_text() == "OPENAI_API_KEY=" + KEY + "\n"
    assert env["ACCESS_CODE"] == "preserve-auth" and env["PASSTHROUGH"] is None
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, ["lobechat"])["changes"])
    engine.restore(result["transaction_id"])
    assert path.read_bytes() == before


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def installation(home, app):
    root = home / "Projects" / app
    if app == "automatic1111":
        for marker in ("launch.py", "modules/launch_utils.py", "modules/script_callbacks.py"):
            put(root / marker, "# source marker\n")
    elif app == "comfyui":
        for marker in ("main.py", "folder_paths.py", "comfy_api/latest/__init__.py"):
            put(root / marker, "# source marker\n")
    elif app == "openmaic":
        put(root / "package.json", '{"name":"openmaic"}')
        put(root / "lib/server/provider-config.ts", "// source marker\n")
        put(root / ".env.local", "OTHER_PROVIDER=preserved\n")
    elif app == "lobechat":
        put(root / "package.json", '{"name":"@lobehub/chat","version":"1.143.3"}')
        put(root / "src/server/modules/ModelRuntime/index.ts", "// source marker\n")
        put(root / ".env.local", "ACCESS_CODE=keep-existing-access-control\n")
    elif app == "sillytavern":
        put(root / "package.json", '{"name":"SillyTavern"}')
        put(root / "src/endpoints/secrets.js", "// source marker\n")
        put(
            root / "data/default-user/settings.json",
            '{"theme":"dark","oai_settings":{"temperature":0.6}}',
        )
        put(
            root / "data/default-user/secrets.json",
            '{"_migrated":[],"api_key_other":[{"id":"other","value":"keep","active":true}]}',
        )
    elif app == "invokeai":
        root = home / "invokeai"
        put(root / "invokeai.yaml", "schema_version: 4.0.3\nhost: 127.0.0.1\n")
        put(root / "api_keys.yaml", "external_seedream_api_key: keep\n")
    elif app == "goose":
        root = home / ".config/goose"
        put(root / "config.yaml", "active_provider: other\nproviders:\n  other:\n    model: keep\n")
    elif app == "anythingllm":
        root = home / "Library/Application Support/anythingllm-desktop/storage"
        put(root / ".env", "OTHER_PROVIDER=keep\nLLM_PROVIDER=openai\n")
    elif app == "kilo-code":
        root = home / ".config/kilo"
        put(
            root / "kilo.json",
            '{"permission":{"bash":"ask"},"provider":{"other":{"options":{"apiKey":"keep"}}}}',
        )
    elif app == "cline":
        root = home / ".cline/data"
        put(
            root / "settings/providers.json",
            '{"version":1,"modes":{"voiceInput":{"provider":"keep"}},"providers":{}}',
        )
    else:
        root = home / ".openviking"
        put(
            root / "ov.conf",
            '{"storage":{"path":"unchanged"},"embedding":{"model":"existing-embedding"},"vlm":{"credentials":[{"api_key":"old"}]}}',
        )
    return root


@pytest.mark.parametrize(
    "app",
    [
        "automatic1111",
        "openmaic",
        "sillytavern",
        "invokeai",
        "openviking",
        "goose",
        "anythingllm",
        "kilo-code",
        "comfyui",
        "cline",
        "lobechat",
    ],
)
def test_transaction_preserves_other_settings_is_idempotent_and_restores(tmp_path, app):
    root = installation(tmp_path, app)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    engine = Engine(Context(home=tmp_path, platform="darwin", env={}, use_path=False))
    selected = next(item for item in engine.scan()["apps"] if item["id"] == app)
    assert selected["mode"] == "automatic"
    preview = engine.preview(KEY, [app])
    assert KEY not in json.dumps(preview)
    assert all(p.read_bytes() == contents for p, contents in before.items())
    applied = engine.apply(preview["plan_id"])
    assert KEY not in json.dumps(applied) + json.dumps(engine.history())
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, [app])["changes"])
    if app == "automatic1111":
        script = root / "extensions/sd-webui-cometapi/scripts/cometapi_media.py"
        assert (
            script.read_bytes()
            == (
                Path(__file__).parents[1] / "cometapi_helper/assets/sd_webui_cometapi.py"
            ).read_bytes()
        )
        assert KEY not in script.read_text(encoding="utf-8")
        assert json.loads((script.parent.parent / "cometapi.json").read_text())["api_key"] == KEY
    elif app == "comfyui":
        extension = root / "custom_nodes/cometapi_connect"
        assert json.loads((extension / "cometapi.json").read_text())["api_key"] == KEY
        assert KEY not in (extension / "__init__.py").read_text()
        assert "comfy_entrypoint" in (extension / "__init__.py").read_text()
    elif app == "openmaic":
        raw = (root / ".env.local").read_text()
        assert "OTHER_PROVIDER=preserved" in raw
        assert "IMAGE_OPENAI_API_KEY=" + KEY in raw
    elif app == "lobechat":
        raw = (root / ".env.local").read_text()
        assert "ACCESS_CODE=keep-existing-access-control" in raw
        assert "OPENAI_API_KEY=" + KEY in raw
        assert "OPENAI_PROXY_URL=" + BASE_URL in raw
    elif app == "sillytavern":
        settings = json.loads((root / "data/default-user/settings.json").read_text())
        secrets = json.loads((root / "data/default-user/secrets.json").read_text())
        assert settings["theme"] == "dark"
        assert settings["oai_settings"]["temperature"] == 0.6
        assert settings["oai_settings"]["chat_completion_source"] == "custom"
        assert settings["oai_settings"]["custom_url"] == BASE_URL
        assert secrets["api_key_other"][0]["value"] == "keep"
        assert secrets["api_key_custom"][0]["value"] == KEY
    elif app == "invokeai":
        config = YAML(typ="safe").load((root / "api_keys.yaml").read_text())
        assert config["external_seedream_api_key"] == "keep"
        assert config["external_openai_base_url"] == "https://api.cometapi.com"
    elif app == "goose":
        config = YAML(typ="safe").load((root / "config.yaml").read_text())
        assert config["providers"]["other"]["model"] == "keep"
        assert "GOOSE_DISABLE_KEYRING" not in config
        provider = json.loads((root / "custom_providers/cometapi_connect.json").read_text())
        assert provider["headers"]["Authorization"] == "Bearer " + KEY
    elif app == "anythingllm":
        raw = (root / ".env").read_text()
        assert "OTHER_PROVIDER=keep" in raw
        assert "LLM_PROVIDER=generic-openai" in raw
        assert "GENERIC_OPEN_AI_API_KEY=" + KEY in raw
        assert "GENERIC_OPENAI_STREAMING_DISABLED=false" in raw
    elif app == "kilo-code":
        config = json.loads((root / "kilo.json").read_text())
        assert config["permission"]["bash"] == "ask"
        assert config["provider"]["other"]["options"]["apiKey"] == "keep"
        assert config["provider"]["openai-compatible"]["options"]["baseURL"] == BASE_URL
    elif app == "cline":
        config = json.loads((root / "settings/providers.json").read_text())
        assert config["modes"]["voiceInput"]["provider"] == "keep"
        assert config["lastUsedProvider"] == "openai-compatible"
        assert config["providers"]["openai-compatible"]["settings"]["apiKey"] == KEY
    else:
        config = json.loads((root / "ov.conf").read_text())
        assert config["embedding"]["model"] == "existing-embedding"
        assert config["vlm"]["api_base"] == BASE_URL
        assert "credentials" not in config["vlm"]
    touched = [Path(c["path"]) for c in applied["changes"]]
    engine.restore(applied["transaction_id"])
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert all(not p.exists() for p in touched if p not in before)


def test_sillytavern_never_overwrites_other_user_accounts(tmp_path):
    root = installation(tmp_path, "sillytavern")
    put(root / "config.yaml", "enableUserAccounts: true\n")
    engine = Engine(Context(home=tmp_path, env={}, use_path=False))
    with pytest.raises(SetupError):
        engine.preview(KEY, ["sillytavern"])


def test_new_adapters_refuse_malformed_config_without_partial_writes(tmp_path):
    root = installation(tmp_path, "sillytavern")
    put(root / "data/default-user/secrets.json", '{"api_key_custom":[],"api_key_custom":[]}')
    before = (root / "data/default-user/settings.json").read_bytes()
    engine = Engine(Context(home=tmp_path, env={}, use_path=False))
    with pytest.raises(SetupError):
        engine.preview(KEY, ["sillytavern"])
    assert (root / "data/default-user/settings.json").read_bytes() == before


def test_packaged_extension_matches_reviewed_source():
    root = Path(__file__).parents[1]
    assert (root / "cometapi_helper/assets/sd_webui_cometapi.py").read_bytes() == (
        root / "integrations/sd-webui-cometapi/scripts/cometapi_media.py"
    ).read_bytes()


def test_cline_bounds_context_for_ignored_output_limit(tmp_path):
    root = installation(tmp_path, "cline")
    engine = Engine(Context(home=tmp_path, env={}, use_path=False))
    engine.apply(
        engine.preview(KEY, ["cline"], {"chat_model": "gpt-4o-mini-2024-07-18"})["plan_id"]
    )
    settings = json.loads((root / "settings/providers.json").read_text())["providers"][
        "openai-compatible"
    ]["settings"]
    assert settings["contextWindow"] == 16384
    assert settings["maxTokens"] == 2048
