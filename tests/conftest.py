"""Host capabilities needed by filesystem security tests."""

import pytest


@pytest.fixture
def require_symlinks(tmp_path):
    target = tmp_path / "symlink-capability-target"
    target.write_bytes(b"probe")
    link = tmp_path / "symlink-capability-link"
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symbolic-link creation privilege (WinError 1314)")
        raise
    finally:
        if link.is_symlink():
            link.unlink()
        target.unlink()


@pytest.fixture(autouse=True)
def require_explicit_credential_labels(monkeypatch):
    """Exercise credential classification across every adapter transaction test."""
    from cometapi_helper.engine import Engine

    original = Engine.preview

    def preview(self, api_key, *args, **kwargs):
        result = original(self, api_key, *args, **kwargs)
        for change in self.plans[result["plan_id"]][1]:
            if not change.resource and api_key.encode() in (change.after or b""):
                assert change.contains_credentials, str(change.path)
        return result

    monkeypatch.setattr(Engine, "preview", preview)
