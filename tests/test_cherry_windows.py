import shutil

import pytest
from test_cherry_store import fixture

from cometapi_helper import cherry_store as cherry
from cometapi_helper.common import Context, SetupError


def test_windows_cherry_explicit_native_profile(tmp_path):
    ctx, path = fixture(tmp_path)
    ctx.platform = "win32"
    assert cherry.find(ctx)["database"] == str(path)


def test_windows_cherry_uses_roaming_appdata(tmp_path):
    ctx, path = fixture(tmp_path)
    destination = tmp_path / "redirected roaming/CherryStudio"
    destination.parent.mkdir()
    shutil.move(str(path.parent.parent), destination)
    ctx = Context(
        home=tmp_path, platform="win32", env={"APPDATA": str(destination.parent)}, use_path=False
    )
    assert cherry.find(ctx)["database"] == str(destination / "Local Storage/leveldb")


def test_locked_windows_database_preview_has_safe_error(tmp_path, monkeypatch):
    ctx, path = fixture(tmp_path)

    def locked(*args, **kwargs):
        raise PermissionError("native Windows sharing violation")

    monkeypatch.setattr(cherry.shutil, "copyfile", locked)
    with pytest.raises(SetupError, match="Quit"):
        with cherry.copied_database(path):
            pass
