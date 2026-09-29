"""Persistent, app-scoped Gemini CLI environment for interactive bash/zsh.

Gemini's .env loader drops custom endpoints in untrusted workspaces. This
launcher supplies environment variables only to Gemini, without changing trust.
"""

import json
import re
import shlex
import shutil
from pathlib import Path

from .common import SetupError
from .storage import read_file

START = "# >>> CometAPI Connect: Gemini CLI >>>"
END = "# <<< CometAPI Connect: Gemini CLI <<<"
LAUNCHER = """// CometAPI Connect: Gemini CLI environment launcher v1
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
let config;
try {
  config = JSON.parse(fs.readFileSync(path.join(__dirname, 'connection.json'), 'utf8'));
  if (config.kind !== 'cometapi-gemini-v1' || config.endpoint !== 'https://api.cometapi.com' ||
      !/^sk-[A-Za-z0-9_-]{8,250}$/.test(config.apiKey) || !path.isAbsolute(config.entry) ||
      !path.isAbsolute(config.package)) throw new Error();
  const pkg = JSON.parse(fs.readFileSync(config.package, 'utf8'));
  if (pkg.name !== '@google/gemini-cli' ||
      path.resolve(path.dirname(config.package), pkg.bin.gemini) !== config.entry) throw new Error();
} catch {
  console.error('CometAPI Connect: Gemini configuration is unavailable. Reconnect Gemini in the helper.');
  process.exit(1);
}
const env = {...process.env, GEMINI_API_KEY: config.apiKey, GOOGLE_GEMINI_BASE_URL: config.endpoint};
// These alternative provider variables would otherwise override Gemini auth.
for (const name of ['GOOGLE_API_KEY', 'GOOGLE_GENAI_USE_VERTEXAI', 'GOOGLE_GENAI_USE_GCA']) delete env[name];
const child = spawn(process.execPath, [config.entry, ...process.argv.slice(2)], {env, stdio: 'inherit'});
child.on('error', () => { console.error('CometAPI Connect: the configured Gemini installation could not start.'); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = code === null ? (signal === 'SIGINT' ? 130 : 1) : code; });
"""

# Upgrade our own dev8 launcher while preserving edits made by the user.
LEGACY_LAUNCHER = LAUNCHER.replace(
    "pkg.name !== '@google/gemini-cli' ||",
    "pkg.name !== '@google/gemini-cli' || pkg.version !== config.version ||",
)


def locate(ctx):
    if ctx.platform == "win32":
        return locate_windows(ctx)
    if ctx.platform not in ("darwin", "linux"):
        raise SetupError("Gemini shell setup currently supports macOS/Linux bash and zsh.")
    shell = Path(ctx.env.get("SHELL", "/bin/zsh" if ctx.platform == "darwin" else "/bin/bash")).name
    if shell not in ("zsh", "bash"):
        raise SetupError("This Gemini shell needs a dedicated launcher adapter.")
    candidates, nodes = [], []
    for root in ctx.roots:
        candidates.extend((root / "node_modules/.bin/gemini", root / "bin/gemini"))
        nodes.extend((root / "node", root / "bin/node"))
    candidates = [p for p in candidates if p.is_file()]
    nodes = [p for p in nodes if p.is_file()]
    if not candidates and ctx.use_path:
        found = shutil.which("gemini", path=ctx.env.get("PATH"))
        if found:
            candidates.append(Path(found))
    if not nodes and ctx.use_path:
        found = shutil.which("node", path=ctx.env.get("PATH"))
        if found:
            nodes.append(Path(found))
    entries = list(dict.fromkeys(p.resolve() for p in candidates))
    nodes = list(dict.fromkeys(p.resolve() for p in nodes))
    if len(entries) != 1 or len(nodes) != 1:
        raise SetupError("Select one installed npm Gemini CLI and its Node runtime.")
    entry = entries[0]
    version = None
    for root in list(entry.parents)[:3]:
        package = root / "package.json"
        if not package.is_file():
            continue
        try:
            doc = json.loads(read_file(package))
            declared = doc.get("bin", {}).get("gemini")
            if (
                doc.get("name") == "@google/gemini-cli"
                and declared
                and (root / declared).resolve() == entry
            ):
                version = doc.get("version")
                break
        except (ValueError, TypeError, AttributeError):
            pass
    if not isinstance(version, str) or not version:
        raise SetupError("Cannot identify the npm Gemini package and its declared entry point.")
    if shell == "zsh":
        profiles = [ctx.config_dir("ZDOTDIR", ".") / ".zshrc"]
    else:
        login = next(
            (
                ctx.home / p
                for p in (".bash_profile", ".bash_login", ".profile")
                if (ctx.home / p).is_file()
            ),
            ctx.home / ".bash_profile",
        )
        profiles = [ctx.home / ".bashrc", login]
    return {
        "entry": entry,
        "package": package,
        "node": nodes[0],
        "version": version,
        "shell": shell,
        "profiles": profiles,
        "settings_root": (
            ctx.home if version == "0.23.0" else ctx.config_dir("GEMINI_CLI_HOME", ".")
        )
        / ".gemini",
    }


def locate_windows(ctx):
    """Resolve npm's Windows shim to the verified package, never execute the shim."""
    shims, nodes = [], []
    for root in ctx.roots:
        shims.extend(
            p for p in (root / "gemini.cmd", root / "node_modules/.bin/gemini.cmd") if p.is_file()
        )
        nodes.extend(p for p in (root / "node.exe", root / "bin/node.exe") if p.is_file())
    if ctx.use_path:
        if not shims:
            found = shutil.which("gemini.cmd", path=ctx.env.get("PATH"))
            if found:
                shims.append(Path(found))
        if not nodes:
            found = shutil.which("node.exe", path=ctx.env.get("PATH"))
            if found:
                nodes.append(Path(found))
    shims = list(dict.fromkeys(p.resolve() for p in shims))
    nodes = list(dict.fromkeys(p.resolve() for p in nodes))
    if len(shims) != 1 or len(nodes) != 1:
        raise SetupError("Select one installed npm Gemini CLI and its Node runtime on Windows.")
    directory = shims[0].parent
    candidates = [
        directory / "node_modules/@google/gemini-cli/package.json",
        directory.parent / "@google/gemini-cli/package.json",
    ]
    packages = [p for p in candidates if p.is_file()]
    if len(packages) != 1:
        raise SetupError("Cannot verify the Windows npm Gemini package/version.")
    package = packages[0]
    try:
        doc = json.loads(read_file(package))
        if (
            doc.get("name") != "@google/gemini-cli"
            or not isinstance(doc.get("version"), str)
            or not isinstance(doc.get("bin", {}).get("gemini"), str)
        ):
            raise ValueError()
        entry = (package.parent / doc["bin"]["gemini"]).resolve()
        if package.parent.resolve() not in entry.parents or not entry.is_file():
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise SetupError(
            "Cannot identify the Windows npm Gemini package and its declared entry point."
        ) from None
    if not any(
        (root / "pwsh.exe").is_file() or (root / "bin/pwsh.exe").is_file() for root in ctx.roots
    ):
        if not ctx.use_path or not shutil.which("pwsh.exe", path=ctx.env.get("PATH")):
            raise SetupError(
                "Windows automatic Gemini setup requires installed PowerShell 7 (pwsh.exe)."
            )
    documents = ctx.home / "Documents"
    # Honor redirected Documents folders for the actual Windows user. Isolated
    # contexts retain paths under their selected home.
    import os

    if os.name == "nt" and ctx.home.resolve() == Path.home().resolve():
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) != 0:
            raise SetupError("Cannot locate the Windows Documents folder for PowerShell profiles.")
        documents = Path(buffer.value)
    return {
        "entry": entry,
        "package": package,
        "node": nodes[0],
        "version": doc["version"],
        "shell": "PowerShell 7",
        "profiles": [documents / "PowerShell/Microsoft.PowerShell_profile.ps1"],
        "settings_root": (
            ctx.home if doc["version"] == "0.23.0" else ctx.config_dir("GEMINI_CLI_HOME", ".")
        )
        / ".gemini",
    }


def powershell_profile_bytes(before, node, launcher):
    try:
        text = (before or b"").decode("utf-8-sig")
    except UnicodeError:
        raise SetupError("The PowerShell profile is not UTF-8 and was not changed.") from None
    pattern = re.compile(re.escape(START) + r"\r?\n.*?" + re.escape(END) + r"\r?\n?", re.S)
    matches = list(pattern.finditer(text))
    if text.count(START) != len(matches) or text.count(END) != len(matches) or len(matches) > 1:
        raise SetupError(
            "The existing Gemini launcher block was edited; preserve it before reconnecting."
        )
    rest = pattern.sub("", text)
    if re.search(
        r"(?im)^\s*(?:function\s+(?:global:|script:)?gemini\b|(?:Set-Alias|New-Alias|sal|nal)\s+(?:-Name\s+)?['\"]?gemini\b)",
        rest,
    ):
        raise SetupError(
            "A user-defined Gemini alias/function already exists in this shell profile."
        )

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    block = (
        START
        + "\nfunction global:gemini {\n  & "
        + quote(node)
        + " "
        + quote(launcher)
        + " @args\n}\n"
        + END
        + "\n"
    )
    if matches:
        updated = text[: matches[0].start()] + block + text[matches[0].end() :]
    else:
        updated = text + ("\n" if text and not text.endswith("\n") else "") + block
    # Windows PowerShell 5 requires a BOM to interpret non-ASCII paths as UTF-8.
    return updated.encode("utf-8-sig")


def profile_bytes(before, node, launcher):
    try:
        text = (before or b"").decode("utf-8")
    except UnicodeError:
        raise SetupError("The shell profile is not UTF-8 and was not changed.") from None
    pattern = re.compile(re.escape(START) + r"\n.*?" + re.escape(END) + r"\n?", re.S)
    matches = list(pattern.finditer(text))
    if text.count(START) != len(matches) or text.count(END) != len(matches) or len(matches) > 1:
        raise SetupError(
            "The existing Gemini launcher block was edited; preserve it before reconnecting."
        )
    rest = pattern.sub("", text)
    if re.search(r"(?m)^\s*(?:alias\s+gemini=|function\s+gemini\b|gemini\s*\(\s*\))", rest):
        raise SetupError(
            "A user-defined Gemini alias/function already exists in this shell profile."
        )
    block = (
        START
        + "\nfunction gemini() {\n  command "
        + shlex.quote(str(node))
        + " "
        + shlex.quote(str(launcher))
        + ' "$@"\n}\n'
        + END
        + "\n"
    )
    if matches:
        return (text[: matches[0].start()] + block + text[matches[0].end() :]).encode()
    return (text + ("\n" if text and not text.endswith("\n") else "") + block).encode()


def settings_update(doc, model):
    from .adapters import object_at

    security = object_at(doc, "security")
    auth = object_at(security, "auth")
    if auth.get("enforcedType") not in (None, "gemini-api-key"):
        raise SetupError(
            "Gemini authentication is enforced by an existing policy; it was not changed."
        )
    auth["selectedType"] = "gemini-api-key"
    object_at(doc, "model")["name"] = model if model.startswith("gemini-") else "gemini-3.5-flash"


def changes(name, ctx, key, model):
    from .adapters import Change, edit

    info = locate(ctx)
    directory = info["settings_root"] / "cometapi-connect"
    launcher = directory / "launch.cjs"
    connection = directory / "connection.json"
    old_launcher = read_file(launcher)
    if old_launcher is not None and old_launcher not in (
        LAUNCHER.encode(),
        LEGACY_LAUNCHER.encode(),
    ):
        raise SetupError("The existing CometAPI Gemini launcher was changed; it was preserved.")
    before = read_file(connection)
    if before is not None:
        try:
            if json.loads(before).get("kind") != "cometapi-gemini-v1":
                raise ValueError()
        except (ValueError, AttributeError):
            raise SetupError("An unrecognized Gemini connection file already exists.") from None
    value = {
        "kind": "cometapi-gemini-v1",
        "endpoint": "https://api.cometapi.com",
        "apiKey": key,
        "entry": str(info["entry"]),
        "package": str(info["package"]),
        "version": info["version"],
    }
    warnings = [
        "Configures the gemini command in new interactive "
        + info["shell"]
        + " terminals. Existing terminals must be reopened. Absolute-path invocations and scripts that do not load shell profiles keep their own settings. Workspace trust and tool approvals are preserved. Gemini uses a native Gemini model; a non-Gemini model selection defaults to gemini-3.5-flash."
    ]
    if ctx.platform == "win32":
        warnings.append(
            "Configures the standard PowerShell 7 console profile. CMD, Windows PowerShell 5, custom-host profiles, and shells that disable profile loading are not configured. PowerShell execution policy is preserved; profiles must already be permitted by your policy."
        )
    result = [
        Change(
            "gemini-cli",
            name,
            launcher,
            old_launcher,
            LAUNCHER.encode(),
            ["App-scoped Gemini launcher"],
            warnings,
        ),
        Change(
            "gemini-cli",
            name,
            connection,
            before,
            (json.dumps(value, indent=2) + "\n").encode(),
            ["Private API key and Gemini endpoint"],
            contains_credentials=True,
        ),
        edit(
            "gemini-cli",
            name,
            info["settings_root"] / "settings.json",
            "json",
            lambda doc: settings_update(doc, model),
            ["Native API-key authentication and Gemini model"],
        ),
    ]
    for path in info["profiles"]:
        before = read_file(path)
        render = powershell_profile_bytes if ctx.platform == "win32" else profile_bytes
        result.append(
            Change(
                "gemini-cli",
                name,
                path,
                before,
                render(before, info["node"], launcher),
                ["Persistent gemini command in " + info["shell"]],
            )
        )
    return result
