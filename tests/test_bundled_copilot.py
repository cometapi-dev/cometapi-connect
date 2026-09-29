"""Discovery uses package identity, never launches editors or reads personal state."""

import json
from pathlib import Path

import pytest

from cometapi_helper import detection
from cometapi_helper.common import Context


def context(tmp_path, platform="win32", env=None):
    return Context(home=tmp_path, platform=platform, env=env or {}, use_path=False)


def manifest(tmp_path, value=None):
    path = (
        tmp_path
        / "AppData/Local/Programs/Microsoft VS Code/resources/app/extensions/copilot/package.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(value or {"publisher": "GitHub", "name": "copilot-chat", "version": "0.64.1"})
    )
    return path


def test_bundled_copilot_detected_as_guided_with_manifest_evidence(tmp_path):
    path = manifest(tmp_path)
    apps, _ = detection.scan(context(tmp_path))
    app = next(item for item in apps if item["id"] == "github-copilot")
    assert app["detected"] is True
    assert app["mode"] == "guided"
    assert app["evidence"] == ["Bundled extension: " + str(path)]


def test_editor_installation_without_bundled_extension_is_not_copilot(tmp_path):
    (tmp_path / "AppData/Local/Programs/Microsoft VS Code").mkdir(parents=True)
    apps, _ = detection.scan(context(tmp_path))
    assert next(item for item in apps if item["id"] == "github-copilot")["detected"] is False


@pytest.mark.parametrize(
    "package",
    [
        {"publisher": "Other", "name": "copilot-chat"},
        {"publisher": "GitHub", "name": "unrelated"},
        {"publisher": None, "name": "copilot-chat"},
        {"publisher": "GitHub", "name": []},
        ["GitHub", "copilot-chat"],
    ],
)
def test_unrelated_or_invalid_identity_is_ignored(tmp_path, package):
    manifest(tmp_path, package)
    assert detection._bundled_copilot_paths(context(tmp_path)) == []


@pytest.mark.parametrize(
    "raw",
    [b"{malformed", b"\xff", b"x" * (1024 * 1024 + 1)],
    ids=["malformed-json", "invalid-utf8", "oversized"],
)
def test_invalid_or_oversized_manifest_is_ignored(tmp_path, raw):
    path = manifest(tmp_path)
    path.write_bytes(raw)
    assert detection._bundled_copilot_paths(context(tmp_path)) == []


def test_large_valid_manifest_and_publisher_case(tmp_path):
    path = manifest(
        tmp_path, {"publisher": "github", "name": "copilot-chat", "contributions": "x" * 250000}
    )
    assert detection._bundled_copilot_paths(context(tmp_path)) == [str(path)]


def test_manifest_symlink_is_ignored(tmp_path, require_symlinks):
    path = manifest(tmp_path)
    target = tmp_path / "unrelated.json"
    path.rename(target)
    path.symlink_to(target)
    assert detection._bundled_copilot_paths(context(tmp_path)) == []


def test_standard_macos_and_linux_candidates_are_bounded(tmp_path):
    mac = detection._bundled_copilot_candidates(context(tmp_path, "darwin"))
    assert (
        tmp_path
        / "Applications/Visual Studio Code.app/Contents/Resources/app/extensions/copilot/package.json"
        in mac
    )
    assert len(mac) == 8
    linux = detection._bundled_copilot_candidates(context(tmp_path, "linux"))
    assert Path("/usr/share/code/resources/app/extensions/copilot/package.json") in linux
    assert len(linux) == 8


def test_relocated_windows_programs_manifest(tmp_path):
    root = tmp_path / "relocated"
    path = (
        root / "Programs/Microsoft VS Code Insiders/resources/app/extensions/copilot/package.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text('{"publisher":"GitHub","name":"copilot-chat"}')
    assert detection._bundled_copilot_paths(context(tmp_path, env={"LOCALAPPDATA": str(root)})) == [
        str(path)
    ]


def test_cli_path_resolution_discovers_custom_install_without_execution(tmp_path, monkeypatch):
    install = tmp_path / "custom/Visual Studio Code.app/Contents/Resources/app"
    executable = install / "bin/code"
    executable.parent.mkdir(parents=True)
    executable.write_text("this must never be executed")
    package = install / "extensions/copilot/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"publisher":"GitHub","name":"copilot-chat"}')
    monkeypatch.setattr(
        detection.shutil,
        "which",
        lambda command, path: str(executable) if command == "code" else None,
    )
    ctx = context(tmp_path)
    ctx.use_path = True
    assert detection._bundled_copilot_paths(ctx) == [str(package)]
