import ctypes as C
import json
import os
import sys
import uuid

import pytest

from cometapi_helper import credentials, file_clients, windows_credentials
from cometapi_helper.common import BASE_URL, Context, SetupError
from cometapi_helper.engine import Engine


def test_windows_zed_default_uses_roaming_appdata(tmp_path):
    ctx = Context(home=tmp_path, platform="win32", env={"APPDATA": str(tmp_path / "roaming")})
    assert file_clients.zed_settings(ctx) == tmp_path / "roaming/Zed/settings.json"


def test_windows_zed_native_target_transaction_and_restore(tmp_path, monkeypatch):
    class Store:
        value = None

        def __init__(self, account):
            assert account == "Bearer"

        def read(self, target):
            assert target == "zed:url=" + BASE_URL
            return Store.value

        def write(self, target, value):
            assert target == "zed:url=" + BASE_URL
            Store.value = value

    monkeypatch.setattr(credentials, "WindowsZedPassword", Store, raising=False)
    config = tmp_path / "AppData/Roaming/Zed/settings.json"
    config.parent.mkdir(parents=True)
    original = b'{"theme":"original"}\n'
    config.write_bytes(original)
    engine = Engine(Context(home=tmp_path, platform="win32", env={}, use_path=False))
    key = "sk-zed-windows-test-value"
    plan = engine.preview(key, ["zed"])
    assert key not in json.dumps(plan)
    tx = engine.apply(plan["plan_id"])
    assert Store.value == credentials.encode("Bearer", key.encode())
    assert json.loads(config.read_text())["theme"] == "original"
    assert all(c["action"] == "unchanged" for c in engine.preview(key, ["zed"])["changes"])
    engine.restore(tx["transaction_id"])
    assert Store.value is None and config.read_bytes() == original


@pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("COMETAPI_TEST_NATIVE_CREDENTIALS") != "1",
    reason="Opt-in Windows native credential integration test",
)
def test_zed_native_credential_stores_raw_bytes_and_local_machine_persistence():
    target = "zed:url=cometapi-test-" + uuid.uuid4().hex
    backend = windows_credentials.WindowsZedPassword("Bearer")
    value = b"sk-zed-raw-test-\xff"
    assert backend.read(target) is None
    try:
        backend.write(target, credentials.encode("Bearer", value))
        pointer = C.POINTER(windows_credentials.CREDENTIAL)()
        assert backend.api.CredReadW(target, 1, 0, C.byref(pointer))
        try:
            item = pointer.contents
            assert C.string_at(item.CredentialBlob, item.CredentialBlobSize) == value
            assert item.Persist == 2
            assert item.UserName == "Bearer"
        finally:
            backend.api.CredFree(pointer)
        assert backend.read(target) == credentials.encode("Bearer", value)
    finally:
        backend.write(target, None)
    assert backend.read(target) is None


def test_zed_windows_resource_rejects_other_server():
    with pytest.raises(SetupError, match="Unrecognized"):
        credentials.read(
            {"kind": "windows-zed-password", "server": "https://other.invalid", "app": "zed"}
        )
