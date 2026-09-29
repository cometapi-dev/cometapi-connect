"""Configure recognized Python application constructors without global env edits.

The libraries themselves have no universal user config. This adapter recognizes
top-level application modules and gives their actual constructors a private,
module-relative connection file. It never imports or executes scanned projects.
"""

import ast
import json

from .common import BASE_URL, SetupError
from .storage import read_file

IDS = {"openai-sdk", "langchain", "semantica"}
IMPORTS = {
    "openai-sdk": {("openai", "OpenAI"), ("openai", "AsyncOpenAI")},
    "langchain": {("langchain_openai", "ChatOpenAI")},
    "semantica": {("semantica.llms", "OpenAI")},
}
START = "# >>> CometAPI Connect: Python application >>>"
END = "# <<< CometAPI Connect: Python application <<<"
FUNCTION = "__cometapi_connection"
CONFIG = ".cometapi-connect.json"
BLOCK = (
    START
    + """
def __cometapi_connection():
    import json as _json
    from pathlib import Path as _Path
    import stat as _stat
    _file = _Path(__file__).with_name(".cometapi-connect.json")
    _info = _file.lstat()
    if not _stat.S_ISREG(_info.st_mode) or _info.st_nlink != 1 or _info.st_size > 65536:
        raise RuntimeError("CometAPI Connect: unsafe connection file")
    _value = _json.loads(_file.read_text(encoding="utf-8"))
    if _value.get("kind") != "cometapi-python-project-v1" or _value.get("schema") != 1 or _value.get("base_url") != "https://api.cometapi.com/v1":
        raise RuntimeError("CometAPI Connect: reconnect this application")
    return _value
"""
    + END
    + "\n"
)


def _position(raw, lineno, offset):
    return sum(len(line) for line in raw.splitlines(keepends=True)[: lineno - 1]) + offset


def _span(raw, node):
    return _position(raw, node.lineno, node.col_offset), _position(
        raw, node.end_lineno, node.end_col_offset
    )


def _tree(raw):
    try:
        text = raw.decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        raise SetupError("This Python module uses unsupported syntax or encoding.") from None
    return text, tree


def analyze(raw, identity):
    text, tree = _tree(raw)
    if START in text or END in text:
        if text.count(BLOCK) != 1 or text.count(START) != 1 or text.count(END) != 1:
            raise SetupError("The existing Python connection reader was edited; it was preserved.")
    elif FUNCTION in text:
        raise SetupError("A Python module already uses the connection reader name.")
    names, modules = {}, {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.level == 0 and (node.module, alias.name) in IMPORTS[identity]:
                    names[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == module for module, _ in IMPORTS[identity]):
                    modules[alias.asname or alias.name] = alias.name
    protected = set(names) | set(modules)
    # Refuse rebinding/shadowing instead of guessing which constructor will run.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "*" and protected:
                    raise SetupError(
                        "A wildcard import can replace the Python provider; it was preserved."
                    )
                if bound in protected and (
                    node.level != 0 or names.get(bound) != (node.module, alias.name)
                ):
                    raise SetupError("A Python provider import is replaced by another import.")
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in protected and modules.get(bound) != alias.name:
                    raise SetupError("A Python provider module is replaced by another import.")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if ast.unparse(node.value) in modules:
                raise SetupError("A Python provider module is modified by the application.")
        if isinstance(node, ast.ExceptHandler) and node.name in protected:
            raise SetupError("A Python exception binding shadows the provider import.")
        if (
            (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node.id in protected
            )
            or (isinstance(node, ast.arg) and node.arg in protected)
            or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in protected
            )
        ):
            raise SetupError(
                "A Python provider import is rebound or shadowed; this project needs a specific adapter."
            )
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        recognized = isinstance(node.func, ast.Name) and node.func.id in names
        if isinstance(node.func, ast.Attribute):
            qualified = ast.unparse(node.func.value)
            recognized = (modules.get(qualified), node.func.attr) in IMPORTS[identity]
        if not recognized:
            continue
        if node.args or any(keyword.arg is None for keyword in node.keywords):
            raise SetupError(
                "A Python provider uses positional or unpacked configuration; it needs a specific adapter."
            )
        keywords = {keyword.arg: keyword for keyword in node.keywords}
        if len(keywords) != len(node.keywords) or any(
            name in keywords
            for name in (
                "client",
                "async_client",
                "root_client",
                "root_async_client",
                "http_client",
                "http_async_client",
                "default_headers",
                "default_query",
                "organization",
                "openai_organization",
                "project",
            )
        ):
            raise SetupError(
                "A Python provider supplies custom transport, headers or account routing; it was preserved."
            )
        calls.append(node)
    return tree, calls


def transform(raw, identity):
    tree, calls = analyze(raw, identity)
    if not calls:
        raise SetupError("No supported Python provider constructor found.")
    replacements = []
    for call in calls:
        keywords = {item.arg: item for item in call.keywords}
        targets = [
            (("api_key", "openai_api_key") if identity == "langchain" else ("api_key",), "api_key"),
            (
                ("base_url", "openai_api_base") if identity == "langchain" else ("base_url",),
                "base_url",
            ),
        ]
        if identity != "openai-sdk":
            targets.append(
                (("model", "model_name") if identity == "langchain" else ("model",), "chat_model")
            )
        additions = []
        for aliases, field in targets:
            present = [name for name in aliases if name in keywords]
            if len(present) > 1:
                raise SetupError("A Python constructor supplies conflicting provider aliases.")
            expression = (FUNCTION + "()[" + repr(field) + "]").encode()
            if present:
                value = keywords[present[0]].value
                start, end = _span(raw, value)
                # Preserve computations with side effects. Plain literals,
                # environment reads, and our own reader are recognized.
                allowed = isinstance(value, (ast.Constant, ast.Name))
                if isinstance(value, ast.Subscript):
                    allowed = ast.unparse(value.value) in ("os.environ", FUNCTION + "()")
                if isinstance(value, ast.Call):
                    allowed = ast.unparse(value.func) in ("os.getenv", "os.environ.get")
                if not allowed:
                    raise SetupError(
                        "A Python connection parameter is computed dynamically; it was preserved."
                    )
                replacements.append((start, end, expression))
            else:
                additions.append(aliases[0].encode() + b"=" + expression)
        if additions:
            _, func_end = _span(raw, call.func)
            _, call_end = _span(raw, call)
            opening = raw.index(b"(", func_end, call_end) + 1
            insertion = b", ".join(additions) + (b", " if call.keywords else b"")
            replacements.append((opening, opening, insertion))
    if BLOCK not in raw.decode("utf-8"):
        # Definitions execute after __future__ imports and the module docstring.
        position = 0
        for index, node in enumerate(tree.body):
            if (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ) or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
                _, position = _span(raw, node)
            else:
                break
        # Leave shebang and encoding/header comments in front of the reader.
        if position == 0 and tree.body:
            position = _position(raw, tree.body[0].lineno, 0)
        replacements.append((position, position, ("\n" + BLOCK + "\n").encode()))
    result = raw
    for start, end, value in sorted(replacements, reverse=True):
        result = result[:start] + value + result[end:]
    _tree(result)
    return result, len(calls)


def find(ctx, identity, candidates=None):
    if candidates is None:
        from .detection import project_candidates

        candidates = project_candidates(ctx)
    found = []
    for root in candidates:
        if not any(
            (root / p).is_file() for p in ("pyproject.toml", "requirements.txt", "setup.cfg")
        ):
            continue
        files = sorted(root.glob("*.py"))
        if len(files) > 40:
            continue
        modules = []
        failed = False
        for path in files:
            if path.name.startswith(("test_", "conftest")):
                continue
            try:
                raw = read_file(path)
                if not raw or len(raw) > 256000:
                    continue
                # Cheap filter before parsing. The actual decision is AST-based.
                if not any(module.encode() in raw for module, _ in IMPORTS[identity]):
                    continue
                _, calls = analyze(raw, identity)
                if calls:
                    transform(raw, identity)
                    modules.append(path)
            except (SetupError, OSError, ValueError):
                failed = True
        if modules and not failed:
            found.append((root, modules))
    if not found:
        raise SetupError(
            "No supported top-level Python application modules were found. A library installation alone is not an application configuration."
        )
    if len(found) > 10:
        raise SetupError("Scan at most ten Python projects at a time.")
    return found


def changes(identity, name, ctx, key, models):
    from .adapters import Change

    result = []
    for root, modules in find(ctx, identity):
        warning = [
            "Configures recognized top-level Python application modules, using private connection files beside the source. Restart the application to load changes. Custom transports, unpacked constructor settings, subpackages and other languages are not covered. Restore preserves later source edits by stopping for review."
        ]
        if identity == "openai-sdk":
            warning.append(
                "The SDK adapter changes client endpoint and API key. Existing request models and API operations remain selected by the application; their CometAPI support must be checked per operation."
            )
        for path in modules:
            before = read_file(path)
            after, count = transform(before, identity)
            result.append(
                Change(
                    identity,
                    name,
                    path,
                    before,
                    after,
                    [str(count) + " native provider constructor(s) and private connection reader"],
                    warning,
                    preserve_mode=True,
                )
            )
        path = root / CONFIG
        before = read_file(path)
        if before is not None:
            try:
                document = json.loads(before)
                if (
                    document.get("schema") != 1
                    or document.get("kind") != "cometapi-python-project-v1"
                ):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise SetupError("An unrecognized Python connection file already exists.") from None
        value = {
            "kind": "cometapi-python-project-v1",
            "schema": 1,
            "api_key": key,
            "base_url": BASE_URL,
            "chat_model": models["chat_model"],
        }
        result.append(
            Change(
                identity,
                name,
                path,
                before,
                (json.dumps(value, indent=2) + "\n").encode(),
                ["Private application API key, endpoint and chat model"],
                contains_credentials=True,
                private_sidecar=True,
            )
        )
    return result
