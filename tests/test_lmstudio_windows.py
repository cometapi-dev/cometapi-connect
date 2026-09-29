import json
from types import SimpleNamespace

import pytest

from cometapi_helper import lmstudio_store as lm
from cometapi_helper.common import Context, SetupError


def fixture(tmp_path):
    app = tmp_path / "LM Studio"
    package = app / "resources/app/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(
        json.dumps(
            {"name": "lm-studio", "desktopName": "ai.elementlabs.lmstudio", "version": "0.4.25+1"}
        )
    )
    (app / "LM Studio.exe").write_bytes(b"fixture")
    root = tmp_path / ".lmstudio"
    (root / ".internal").mkdir(parents=True)
    (root / "conversations").mkdir()
    (root / ".internal/conversation-config.json").write_text(
        json.dumps({"selectedConversation": "test.conversation.json"})
    )
    (root / "conversations/test.conversation.json").write_text(
        json.dumps({"plugins": [], "pluginConfigs": {}, "messages": []})
    )
    return Context(home=tmp_path, platform="win32", roots=[app, root], env={}, use_path=False)


def test_windows_native_package_and_configuration(tmp_path, monkeypatch):
    ctx = fixture(tmp_path)
    monkeypatch.setattr(
        lm.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout='"other.exe","123","Console","1","1 K"\n'),
    )
    target = lm.find(ctx)
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-test-secret", "gpt-4.1-mini-2025-04-14")
    lm.write(target, after, before)
    assert lm.read(target) == after
    lm.write(target, before, after)
    assert lm.read(target) == before


def test_windows_running_app_blocks_configuration(tmp_path, monkeypatch):
    ctx = fixture(tmp_path)
    target = lm.find(ctx)
    monkeypatch.setattr(
        lm.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout='"LM Studio.exe","123","Console","1","1 K"\n'),
    )
    with pytest.raises(SetupError, match="Close LM Studio"):
        lm.read(target)


def test_windows_new_version_keeps_identity_checks(tmp_path):
    ctx = fixture(tmp_path)
    p = ctx.roots[0] / "resources/app/package.json"
    d = json.loads(p.read_text())
    d["version"] = "9.0.0"
    p.write_text(json.dumps(d))
    assert lm.find(ctx)["app"] == str(ctx.roots[0])
    d["name"] = "other-app"
    p.write_text(json.dumps(d))
    with pytest.raises(SetupError):
        lm.find(ctx)
