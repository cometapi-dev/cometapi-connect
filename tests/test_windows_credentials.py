import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from cometapi_helper import credentials
from cometapi_helper import windows_credentials as windows
from cometapi_helper.common import SetupError
from cometapi_helper.windows_credentials import WindowsGenericPassword


def test_wrong_account_rejected_before_native_write():
    backend = WindowsGenericPassword.__new__(WindowsGenericPassword)
    backend.account = "CometAPI Connect"
    with pytest.raises(SetupError, match="account"):
        backend.write("CometAPI Connect.jan-providers", credentials.encode("other", b"secret"))


@pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("COMETAPI_TEST_NATIVE_CREDENTIALS") != "1",
    reason="Opt-in Windows native credential integration test",
)
def test_native_keyring_roundtrip_preserves_utf16_secret():
    account = "cometapi-test-" + uuid.uuid4().hex
    target = account + ".jan-providers"
    backend = WindowsGenericPassword(account)
    assert backend.read(target) is None
    try:
        value = credentials.encode(
            account, json.dumps(["sk-test-secret", "unicode-测试"]).encode("utf8")
        )
        backend.write(target, value)
        assert backend.read(target) == value
        backend.write(target, None)
        assert backend.read(target) is None
    finally:
        backend.write(target, None)


@pytest.mark.parametrize("error", [5, 1312])
def test_denied_native_read_is_not_missing(monkeypatch, error):
    backend = WindowsGenericPassword.__new__(WindowsGenericPassword)
    backend.account = "account"
    backend.api = SimpleNamespace(CredReadW=lambda *a: False)
    monkeypatch.setattr(windows.C, "get_last_error", lambda: error, raising=False)
    with pytest.raises(SetupError, match="denied or failed"):
        backend.read("account.jan-providers")
