import json
import plistlib
from types import SimpleNamespace

import pytest

from cometapi_helper import lmstudio_store as lm
from cometapi_helper.common import Context, SetupError


@pytest.fixture
def target(tmp_path, monkeypatch):
    app = tmp_path / "LM Studio.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "ai.elementlabs.lmstudio",
                "CFBundleShortVersionString": "0.4.23+1",
            }
        )
    )
    root = tmp_path / ".lmstudio"
    plugin = root / "extensions/plugins" / lm.PLUGIN
    plugin.mkdir(parents=True)
    (plugin / "manifest.json").write_text(
        json.dumps(
            {
                "type": "plugin",
                "runner": "node",
                "owner": "lmstudio",
                "name": "openai-compat-endpoint",
                "revision": 9,
            }
        )
    )
    (root / ".internal").mkdir()
    (root / "conversations").mkdir()
    (root / ".internal/conversation-config.json").write_text(
        json.dumps({"selectedConversation": "123.conversation.json"})
    )
    (root / "conversations/123.conversation.json").write_text(
        json.dumps(
            {
                "plugins": [],
                "pluginConfigs": {},
                "messages": [{"text": "original chat"}],
                "name": "Preserve me",
            }
        )
    )
    monkeypatch.setattr(
        lm.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="other process")
    )
    return lm.find(Context(home=tmp_path, platform="darwin", roots=[app, root], env={}))


def test_apply_idempotent_restore_preserves_messages_and_other_plugin(target):
    path = lm.Path(target["database"])
    path.write_text(
        json.dumps(
            {
                "json": {"plugins": [["other/plugin", {"fields": [{"key": "x", "value": 1}]}]]},
                "meta": {"values": {"plugins": ["map"]}},
            }
        )
    )
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-fixture-key", "gpt-4.1-mini-2025-04-14")
    lm.write(target, after, before)
    assert lm.read(target) == after
    assert lm.prepare_value(after, "sk-fixture-key", "gpt-4.1-mini-2025-04-14") == after
    chat = lm.Path(target["root"]) / "conversations" / target["chat"]
    doc = json.loads(chat.read_text())
    doc["messages"].append({"text": "new reply"})
    chat.write_text(json.dumps(doc))
    lm.write(target, before, after)
    assert lm.read(target) == before
    assert json.loads(chat.read_text())["messages"] == [
        {"text": "original chat"},
        {"text": "new reply"},
    ]
    assert json.loads(path.read_text())["json"]["plugins"] == [
        ["other/plugin", {"fields": [{"key": "x", "value": 1}]}]
    ]


def test_changed_connection_refuses_restore(target):
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-fixture-key", "gpt-4.1-mini-2025-04-14")
    lm.write(target, after, before)
    newer = lm.prepare_value(after, "sk-new-key", "gpt-4.1-mini-2025-04-14")
    lm.write(target, newer, after)
    with pytest.raises(SetupError, match="changed"):
        lm.write(target, before, after)
    assert lm.read(target) == newer


def test_failed_second_file_recovers_first(target, monkeypatch):
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-fixture-key", "gpt-4.1-mini-2025-04-14")
    real = lm.atomic_write
    calls = []

    def fail_once(path, raw):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("fixture disk failure")
        real(path, raw)

    monkeypatch.setattr(lm, "atomic_write", fail_once)
    with pytest.raises(SetupError, match="recovered"):
        lm.write(target, after, before)
    assert lm.read(target) == before
    assert not lm.Path(target["database"]).exists()


def test_open_app_blocks_writes(target, monkeypatch):
    monkeypatch.setattr(
        lm.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout="/Applications/LM Studio.app/Contents/MacOS/LM Studio"
        ),
    )
    with pytest.raises(SetupError, match="Close LM Studio"):
        lm.read(target)


def test_unknown_generator_and_unsupported_model_rejected(target):
    before = lm.read(target)
    with pytest.raises(SetupError, match="four declared"):
        lm.prepare_value(before, "sk-fixture-key", "invented-model")
    chat = lm.Path(target["root"]) / "conversations" / target["chat"]
    doc = json.loads(chat.read_text())
    doc["plugins"] = ["other/generator"]
    chat.write_text(json.dumps(doc))
    with pytest.raises(SetupError, match="Unsupported"):
        lm.read(target)


def test_symlink_chat_rejected(target, tmp_path, require_symlinks):
    chat = lm.Path(target["root"]) / "conversations" / target["chat"]
    other = tmp_path / "outside"
    other.write_bytes(chat.read_bytes())
    chat.unlink()
    chat.symlink_to(other)
    with pytest.raises(SetupError):
        lm.read(target)


def test_duplicate_fields_rejected_before_write(target):
    before = lm.read(target)
    doc = json.loads(before)
    doc["global"] = {"fields": [{"key": "x", "value": 1}, {"key": "x", "value": 2}]}
    with pytest.raises(SetupError):
        lm.write(target, lm.encode(doc), before)
    assert lm.read(target) == before


def test_first_chat_creation_restore_keeps_generated_messages(target):
    root = lm.Path(target["root"])
    config = root / ".internal/conversation-config.json"
    config.write_text(json.dumps({"selectedConversation": None, "unrelated": "preserved"}))
    target = dict(target, chat=lm.MANAGED_CHAT, create_chat=True)
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-fixture-key", "gpt-4.1-mini-2025-04-14")
    lm.write(target, after, before)
    assert lm.read(target) == after
    assert json.loads(config.read_text())["selectedConversation"] == lm.MANAGED_CHAT
    chat = root / "conversations" / lm.MANAGED_CHAT
    doc = json.loads(chat.read_text())
    doc["messages"] = [{"text": "generated"}]
    chat.write_text(json.dumps(doc))
    lm.write(target, before, after)
    assert lm.read(target) == before
    assert json.loads(chat.read_text())["messages"] == [{"text": "generated"}]
    assert json.loads(config.read_text())["unrelated"] == "preserved"


def test_missing_plugin_is_plannable_but_installer_failure_writes_no_key(target, monkeypatch):
    manifest = lm.Path(target["root"]) / "extensions/plugins" / lm.PLUGIN / "manifest.json"
    manifest.unlink()
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-fixture-key", "gpt-4.1-mini-2025-04-14")

    def failed(*args):
        raise SetupError("fixture installer failed")

    monkeypatch.setattr(lm, "ensure_plugin", failed)
    with pytest.raises(SetupError, match="installer failed"):
        lm.write(target, after, before)
    assert lm.read(target) == before
    assert not lm.Path(target["database"]).exists()


def test_unknown_saved_layout_does_not_get_replaced(target):
    root = lm.Path(target["root"])
    (root / ".internal/ui-state").mkdir()
    (root / ".internal/ui-state/window-1.json").write_text(
        json.dumps({"tabLayouts": {"chat": "invalid"}})
    )
    target = dict(target, chat=lm.MANAGED_CHAT, create_chat=True)
    with pytest.raises(SetupError, match="multi-chat"):
        lm.read(target)


def test_bundled_plugin_bootstrap_does_not_start_app(target):
    import shutil

    plugin = lm.Path(target["root"]) / "extensions/plugins" / lm.PLUGIN
    shutil.rmtree(plugin)
    before = lm.read(target)
    after = lm.prepare_value(before, "sk-fixture-key", "gpt-4.1-mini-2025-04-14")
    lm.write(target, after, before)
    assert (plugin / ".lmstudio/production.js").is_file()
    assert (plugin / "node_modules/@lmstudio/sdk/package.json").is_file()
    assert lm.read(target) == after
    lm.write(target, before, after)
    assert lm.read(target) == before
    assert (plugin / "manifest.json").is_file()
