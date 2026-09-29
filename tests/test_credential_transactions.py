import json

import pytest

from cometapi_helper import credentials
from cometapi_helper.common import BASE_URL, Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-test_native_credential_123456"


@pytest.fixture
def native_store(monkeypatch):
    class Store:
        value = credentials.encode("original-account", b"original-secret")
        fail = False

        def read(self, server):
            assert server == BASE_URL
            return Store.value

        def write(self, server, value):
            assert server == BASE_URL
            if Store.fail:
                Store.fail = False
                raise SetupError("Simulated credential access denial")
            Store.value = value

    monkeypatch.setattr(credentials, "MacInternetPassword", Store)
    return Store


def setup(tmp_path):
    config = tmp_path / ".config/zed/settings.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{"theme":"existing","agent":{"default_model":{"provider":"old","model":"keep"}}}'
    )
    return Engine(Context(home=tmp_path, platform="darwin", env={}, use_path=False)), config


@pytest.mark.parametrize("existing", [True, False])
def test_file_and_keychain_apply_and_restore_together(tmp_path, native_store, existing):
    engine, config = setup(tmp_path)
    original = config.read_bytes()
    if not existing:
        native_store.value = None
    old = native_store.value
    preview = engine.preview(KEY, ["zed"])
    assert native_store.value == old and config.read_bytes() == original
    assert KEY not in json.dumps(preview)
    result = engine.apply(preview["plan_id"])
    assert native_store.value == credentials.encode("Bearer", KEY.encode())
    assert json.loads(config.read_text())["theme"] == "existing"
    assert all(c["action"] == "unchanged" for c in engine.preview(KEY, ["zed"])["changes"])
    engine.restore(result["transaction_id"])
    assert native_store.value == old and config.read_bytes() == original


def test_keychain_failure_rolls_back_completed_file(tmp_path, native_store):
    engine, config = setup(tmp_path)
    original, old = config.read_bytes(), native_store.value
    preview = engine.preview(KEY, ["zed"])
    native_store.fail = True
    with pytest.raises(SetupError, match="rolled back"):
        engine.apply(preview["plan_id"])
    assert config.read_bytes() == original and native_store.value == old


def test_newer_key_prevents_restore_of_every_target(tmp_path, native_store):
    engine, config = setup(tmp_path)
    result = engine.apply(engine.preview(KEY, ["zed"])["plan_id"])
    configured = config.read_bytes()
    newer = credentials.encode("Bearer", b"newer-secret")
    native_store.value = newer
    with pytest.raises(SetupError, match="changed since setup"):
        engine.restore(result["transaction_id"])
    assert config.read_bytes() == configured and native_store.value == newer


def test_restore_failure_reapplies_completed_file(tmp_path, native_store):
    engine, config = setup(tmp_path)
    result = engine.apply(engine.preview(KEY, ["zed"])["plan_id"])
    configured, key = config.read_bytes(), native_store.value
    native_store.fail = True
    with pytest.raises(SetupError, match="undone"):
        engine.restore(result["transaction_id"])
    assert config.read_bytes() == configured and native_store.value == key


def test_unknown_credential_target_is_rejected_before_access():
    with pytest.raises(SetupError, match="Unrecognized"):
        credentials.read(
            {"kind": "macos-internet-password", "server": "https://example.com", "app": "zed"}
        )


@pytest.mark.parametrize("platform", ["linux"])
def test_zed_does_not_claim_untested_platform_credentials(tmp_path, platform):
    engine = Engine(Context(home=tmp_path, platform=platform, env={}, use_path=False))
    with pytest.raises(SetupError):
        engine.preview(KEY, ["zed"])
