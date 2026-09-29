import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from cometapi_helper import litellm_config
from cometapi_helper.common import BASE_URL, Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-test_litellm_route_only_123456"


def test_proxy_route_preserves_security_and_other_models_and_restores(tmp_path):
    path = tmp_path / "litellm_config.yaml"
    existing = {
        "model_list": [
            {
                "model_name": "existing-embedding",
                "litellm_params": {"model": "openai/text-embedding-3-small", "api_key": "old-key"},
            }
        ],
        "general_settings": {"master_key": "keep-proxy-auth"},
        "litellm_settings": {"success_callback": ["prometheus"]},
    }
    path.write_text(json.dumps(existing))
    before = path.read_bytes()
    engine = Engine(Context(home=tmp_path, env={}, roots=[tmp_path], use_path=False))
    selected = next(a for a in engine.scan()["apps"] if a["id"] == "litellm")
    assert selected["detected"] and selected["mode"] == "automatic"
    plan = engine.preview(KEY, ["litellm"], {"chat_model": "gpt-4.1-mini"})
    assert KEY not in json.dumps(plan) and path.read_bytes() == before
    applied = engine.apply(plan["plan_id"])
    doc = YAML(typ="safe", pure=True).load(path.read_text())
    assert doc["model_list"][0] == existing["model_list"][0]
    assert doc["general_settings"] == existing["general_settings"]
    assert doc["litellm_settings"] == existing["litellm_settings"]
    assert doc["model_list"][1]["litellm_params"]["api_base"] == BASE_URL
    assert doc["model_list"][1]["litellm_params"]["model"] == "openai/gpt-4.1-mini"
    assert all(
        c["action"] == "unchanged"
        for c in engine.preview(KEY, ["litellm"], {"chat_model": "gpt-4.1-mini"})["changes"]
    )
    engine.restore(applied["transaction_id"])
    assert path.read_bytes() == before


def test_proxy_model_name_collision_and_ambiguous_roots_are_rejected(tmp_path):
    doc = {
        "model_list": [
            {
                "model_name": litellm_config.ROUTE,
                "litellm_params": {"api_base": "https://other.example"},
            }
        ]
    }
    with pytest.raises(SetupError, match="another endpoint"):
        litellm_config.update(doc, KEY, "gpt-4.1-mini")
    for name in ("litellm_config.yaml", "config.yaml"):
        (tmp_path / name).write_text(json.dumps({"model_list": []}))
    with pytest.raises(SetupError, match="Multiple"):
        litellm_config.paths(Context(home=tmp_path, env={}, roots=[tmp_path], use_path=False))


def test_process_detection_uses_only_identifiable_proxy_absolute_config_args():
    result = litellm_config.process_paths(
        "\n".join(
            [
                '/usr/bin/python /opt/venv/bin/litellm --config "/tmp/proxy one/config.yaml"',
                "/opt/bin/litellm --config=/tmp/proxy-two.yaml",
                "/opt/bin/litellm --config relative.yaml",
                '/usr/bin/echo "litellm --config /tmp/not-a-proxy.yaml"',
                '/usr/bin/bash -c "litellm --config /tmp/not-an-entrypoint.yaml"',
            ]
        )
    )
    assert result == [Path("/tmp/proxy one/config.yaml"), Path("/tmp/proxy-two.yaml")]
