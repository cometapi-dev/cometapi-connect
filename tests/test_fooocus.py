import json

import pytest

from cometapi_helper import fooocus
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_fooocus_compatible_hook_is_reversible(tmp_path, monkeypatch, newline):
    root = tmp_path / "Projects/Fooocus"
    root.mkdir(parents=True)
    original = b"# synthetic version guard fixture\nshared.gradio_root.launch()\n"
    original = original.replace(b"\n", newline)
    for relative, raw in (
        ("webui.py", original),
        ("fooocus_version.py", b"version = '2.5.5'\n"),
        ("modules/config.py", b"# marker\n"),
        ("launch.py", b"# marker\n"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    engine = Engine(Context(home=tmp_path, env={}, use_path=False))
    key = "sk-fooocus_fake_configuration_123456"
    plan = engine.preview(key, ["fooocus"])
    assert key not in json.dumps(plan) and (root / "webui.py").read_bytes() == original
    applied = engine.apply(plan["plan_id"])
    patched = (root / "webui.py").read_bytes()
    assert patched.count(fooocus.HOOK.replace(b"\n", newline)) == 1
    if newline == b"\r\n":
        assert b"\n" not in patched.replace(b"\r\n", b"")
    source = (root / "extensions/cometapi_connect/scripts/cometapi_media.py").read_text(
        encoding="utf-8"
    )
    assert key not in source and "fooocus_config.path_outputs" in source
    assert "script_callbacks" not in source
    compile(source, "test-cloud-panel", "exec")
    assert all(c["action"] == "unchanged" for c in engine.preview(key, ["fooocus"])["changes"])
    engine.restore(applied["transaction_id"])
    assert (root / "webui.py").read_bytes() == original
    assert not (root / "extensions/cometapi_connect/cometapi.json").exists()
    (root / "webui.py").write_bytes(original + b"# user edits\n")
    entry = next(a for a in engine.scan()["apps"] if a["id"] == "fooocus")
    assert entry["mode"] == "automatic"
    assert engine.preview(key, ["fooocus"])["plan_id"]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_fooocus_duplicate_or_modified_hook_is_rejected(monkeypatch, newline):
    raw = b"shared.gradio_root.launch()\n"
    raw = raw.replace(b"\n", newline)
    patched = fooocus.patch(raw)
    assert fooocus.patch(patched) == patched
    with pytest.raises(SetupError):
        fooocus.patch(fooocus.HOOK + patched)
    with pytest.raises(SetupError):
        fooocus.patch(patched.replace(b"_cometapi_attach(shared", b"_modified_attach(shared"))


@pytest.mark.parametrize(
    "raw",
    [
        b"# shared.gradio_root.launch()\n",
        b"if True:\n    shared.gradio_root.launch()\n",
        b"x=shared.gradio_root.launch()\n",
        b"shared.gradio_root.launch()\nshared.gradio_root.launch()\n",
    ],
)
def test_incompatible_launch_structure_is_preserved(raw):
    with pytest.raises(SetupError):
        fooocus.patch(raw)
