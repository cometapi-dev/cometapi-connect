import copy
import importlib.util
import json
import tempfile
from pathlib import Path

import pytest

from cometapi_helper import dify_store
from cometapi_helper.common import SetupError
from cometapi_helper.roo_store import IncompleteNativeWrite

spec = importlib.util.spec_from_file_location("dify_native", dify_store.ASSET)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def fixture():
    return {
        "schema": 1,
        "models": [{"id": "m1", "credential": "c1"}],
        "credentials": [
            {
                "id": "c1",
                "model_name": "gpt-4.1-mini",
                "encrypted_config": json.dumps(
                    {
                        "api_key": "encrypted-old",
                        "endpoint_url": "https://old.invalid/v1",
                        "mode": "chat",
                        "context_size": "8192",
                        "endpoint_model_name": "gpt-4.1-mini",
                    }
                ),
            }
        ],
    }


def test_changes_only_connection_and_keeps_cipher_for_idempotence():
    before = fixture()
    after = native.configured(
        before, "sk-new-test-key", lambda _: "old", lambda key: "cipher:" + key
    )
    assert before == fixture()
    assert after["models"] == before["models"]
    data = json.loads(after["credentials"][0]["encrypted_config"])
    assert data == dict(
        json.loads(before["credentials"][0]["encrypted_config"]),
        api_key="cipher:sk-new-test-key",
        endpoint_url=native.BASE,
    )
    assert (
        native.configured(
            after,
            "sk-new-test-key",
            lambda _: "sk-new-test-key",
            lambda _: pytest.fail("must not reencrypt"),
        )
        == after
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"headers": {"Authorization": "bad"}},
        {"api_type": "responses"},
        {"compatibility_mode": "extended"},
        {"web_search_support": "tool_standard"},
        {"stream_mode_auth": "use"},
        {"mode": "completion"},
        {"proxy": "http://other"},
    ],
)
def test_custom_routing_refused_without_mutation(extra):
    before = fixture()
    data = json.loads(before["credentials"][0]["encrypted_config"])
    data.update(extra)
    before["credentials"][0]["encrypted_config"] = json.dumps(data)
    original = copy.deepcopy(before)
    with pytest.raises(ValueError):
        native.configured(before, "new", lambda _: "old", lambda _: "cipher")
    assert before == original


def resource():
    return {
        "kind": dify_store.KIND,
        "endpoint": "unix:///tmp/docker.sock",
        "container": "a" * 64,
        "image": ("sha256:" + "b" * 64),
        "database": str(Path(tempfile.gettempdir()) / ("dify-" + "a" * 64 + ".native")),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "ssh://remote"),
        ("endpoint", "tcp://127.0.0.1:2375"),
        ("container", "other"),
        ("image", "unknown"),
        ("database", "relative"),
    ],
)
def test_resource_rejects_remote_and_changed_identity(field, value, monkeypatch):
    target = resource()
    target[field] = value
    monkeypatch.setattr(dify_store, "docker", lambda *_: pytest.fail("must reject before Docker"))
    with pytest.raises(SetupError):
        dify_store.validate(target)


def test_write_failure_keeps_backup_semantics(monkeypatch):
    monkeypatch.setattr(dify_store, "validate", lambda _: None)

    def failed(*args, **kwargs):
        raise SetupError("timeout")

    monkeypatch.setattr(dify_store, "docker", failed)
    with pytest.raises(IncompleteNativeWrite):
        dify_store.write(resource(), b"{}", b"{}")
    with pytest.raises(SetupError) as caught:
        dify_store.read(resource())
    assert not isinstance(caught.value, IncompleteNativeWrite)


def test_native_conflict_is_not_uncertain_commit(monkeypatch):
    monkeypatch.setattr(dify_store, "validate", lambda _: None)
    monkeypatch.setattr(
        dify_store, "docker", lambda *a, **k: b'COMETAPI_DIFY={"ok":false,"error":"conflict"}\n'
    )
    with pytest.raises(SetupError, match="changed after preview") as caught:
        dify_store.write(resource(), b"{}", b"{}")
    assert not isinstance(caught.value, IncompleteNativeWrite)


def test_api_container_validation_excludes_workers(monkeypatch):
    target = resource()

    def inspection(mode):
        return json.dumps(
            [
                target["container"],
                target["image"],
                True,
                "1001:1001",
                "/app/api",
                mode,
                "langgenius/dify-api:99.0.0",
            ]
        ).encode()

    monkeypatch.setattr(dify_store, "docker", lambda *a, **k: inspection([]))
    with pytest.raises(SetupError):
        dify_store.validate(target)
    monkeypatch.setattr(dify_store, "docker", lambda *a, **k: inspection([True]))
    dify_store.validate(target)


@pytest.mark.parametrize(
    "endpoint", ["npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine"]
)
def test_windows_local_pipe_is_accepted(endpoint, monkeypatch):
    target = resource()
    target["endpoint"] = endpoint
    monkeypatch.setattr(
        dify_store,
        "docker",
        lambda *a, **k: json.dumps(
            [
                target["container"],
                target["image"],
                True,
                "1001:1001",
                "/app/api",
                [True],
                "langgenius/dify-api:99.0.0",
            ]
        ).encode(),
    )
    dify_store.validate(target)


@pytest.mark.parametrize(
    "endpoint",
    [
        "npipe:////remote/pipe/docker_engine",
        "npipe:////localhost/pipe/docker_engine",
        "npipe:////./pipe/unrelated",
        "npipe:////./pipe/docker_engine\n",
        "unix:///tmp/docker.sock\r",
    ],
)
def test_remote_or_unrecognized_pipes_rejected(endpoint, monkeypatch):
    target = resource()
    target["endpoint"] = endpoint
    monkeypatch.setattr(dify_store, "docker", lambda *_: pytest.fail("must reject before Docker"))
    with pytest.raises(SetupError):
        dify_store.validate(target)


def test_windows_detects_verified_local_api_container(monkeypatch, tmp_path):
    from types import SimpleNamespace

    endpoint = "npipe:////./pipe/dockerDesktopLinuxEngine"
    identity = "a" * 64

    def docker(args, **kwargs):
        if args == ["context", "show"]:
            return b"desktop-linux\n"
        if args == ["context", "inspect", "desktop-linux"]:
            return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}]).encode()
        if "ps" in args:
            return (identity + " langgenius/dify-api:99.0.0").encode()
        if "{{.Image}}" in args:
            return ("sha256:" + "b" * 64).encode()
        return json.dumps(
            [
                identity,
                ("sha256:" + "b" * 64),
                True,
                "1001:1001",
                "/app/api",
                [True],
                "langgenius/dify-api:99.0.0",
            ]
        ).encode()

    monkeypatch.setattr(dify_store, "docker", docker)
    target = dify_store.find(SimpleNamespace(platform="win32", use_path=True, state_dir=tmp_path))
    assert target["endpoint"] == endpoint
    assert target["container"] == identity


@pytest.mark.parametrize("image_name", ["unrelated/dify-api:99.0.0", "langgenius/dify-api-evil:1"])
def test_dify_rejects_other_image_identity(monkeypatch, image_name):
    target = resource()
    monkeypatch.setattr(
        dify_store,
        "docker",
        lambda *a, **k: json.dumps(
            [
                target["container"],
                target["image"],
                True,
                "1001:1001",
                "/app/api",
                [True],
                image_name,
            ]
        ).encode(),
    )
    with pytest.raises(SetupError):
        dify_store.validate(target)


def test_dify_rejects_changed_image_between_preview_and_write(monkeypatch):
    target = resource()
    monkeypatch.setattr(
        dify_store,
        "docker",
        lambda *a, **k: json.dumps(
            [
                target["container"],
                "sha256:" + "c" * 64,
                True,
                "1001:1001",
                "/app/api",
                [True],
                "langgenius/dify-api:99",
            ]
        ).encode(),
    )
    with pytest.raises(SetupError):
        dify_store.validate(target)


def test_snapshot_tracks_actual_plugin_binding_for_conflicts():
    from types import SimpleNamespace

    credential = SimpleNamespace(
        id="c",
        tenant_id="t",
        provider_name=native.PROVIDER,
        model_name="model",
        model_type="llm",
        credential_name="existing",
        encrypted_config="ciphertext",
    )
    model = SimpleNamespace(
        id="m", model_name="model", model_type="llm", credential_id="c", is_valid=True
    )
    setting = SimpleNamespace(
        id="s", model_name="model", model_type="llm", enabled=True, load_balancing_enabled=False
    )
    plugin = "langgenius/openai_api_compatible:99.0.0@" + "a" * 64
    snapshot = native.build_snapshot("t", "owner", plugin, [credential], [model], [setting])
    assert snapshot["plugin"] == plugin
    assert snapshot["credentials"][0]["encrypted_config"] == "ciphertext"
    assert snapshot["models"][0]["credential"] == "c"
    changed = native.build_snapshot(
        "t", "owner", plugin + "changed", [credential], [model], [setting]
    )
    assert changed != snapshot  # Changing a binding invalidates the preview snapshot.
