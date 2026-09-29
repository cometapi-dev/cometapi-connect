"""Cloud-panel hook for compatible Fooocus launch structures."""

import ast
from pathlib import Path

from .common import SetupError
from .storage import read_file

ANCHOR = b"shared.gradio_root.launch("
HOOK = b"""# BEGIN COMETAPI CONNECT CLOUD PANEL v1
from extensions.cometapi_connect.scripts.cometapi_media import attach as _cometapi_attach
_cometapi_attach(shared.gradio_root)
# END COMETAPI CONNECT CLOUD PANEL v1

"""


def patch(raw):
    if raw is None:
        raise SetupError("Fooocus webui.py was not found.")
    windows_hook = HOOK.replace(b"\n", b"\r\n")
    original = raw.replace(HOOK, b"").replace(windows_hook, b"")
    # Require a unique standalone launch statement; comments and release numbers
    # do not determine compatibility. Refuse edited/partial managed hooks.
    if (
        raw.count(HOOK) + raw.count(windows_hook) > 1
        or b"COMETAPI CONNECT CLOUD PANEL" in original
        or original.count(ANCHOR) != 1
    ):
        raise SetupError(
            "Fooocus has an ambiguous or modified cloud hook; its source was preserved."
        )
    try:
        tree = ast.parse(original)
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and ast.dump(n.func)
            == ast.dump(ast.parse("shared.gradio_root.launch", mode="eval").body)
        ]
        if len(calls) != 1 or not any(
            isinstance(n, ast.Expr) and n.value is calls[0] for n in tree.body
        ):
            raise ValueError()
        line = original.splitlines()[calls[0].lineno - 1]
        if not line.startswith(ANCHOR):
            raise ValueError()
    except (SyntaxError, ValueError, UnicodeError):
        raise SetupError(
            "Fooocus needs one compatible top-level Gradio launch statement; its source was preserved."
        ) from None
    hook = windows_hook if b"\r\n" in original else HOOK
    return original.replace(ANCHOR, hook + ANCHOR)


def supported(root):
    try:
        patch(read_file(root / "webui.py"))
        return True
    except (OSError, SetupError):
        return False


def extension_source():
    source = (Path(__file__).parent / "assets/sd_webui_cometapi.py").read_text(encoding="utf-8")
    old = "from modules import paths, script_callbacks"
    assert source.count(old) == 1
    source = source.replace(old, "from modules import config as fooocus_config")
    source = source.replace(
        'OUTPUT = Path(paths.data_path) / "outputs" / "cometapi"',
        "OUTPUT = Path(fooocus_config.path_outputs) / 'cometapi'",
    )
    source = source.replace(
        "Experimental CometAPI cloud tab for AUTOMATIC1111; no local checkpoint needed.",
        "CometAPI cloud panel for Fooocus; uses its configured output directory.",
    )
    assert source.count("script_callbacks.on_ui_tabs(ui)") == 1
    source = source.replace(
        "script_callbacks.on_ui_tabs(ui)",
        """def attach(root):
    # Construct outside the existing Blocks context, then render once into it.
    # Generation runs through the same queued callbacks as the A1111 adapter.
    block = ui()[0][0]
    with root:
        with gr.Accordion("CometAPI cloud generation", open=True):
            block.render()
""",
    )
    compile(source, "cometapi_media.py", "exec")
    return source.encode()
