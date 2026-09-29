"""Existing LiteLLM Proxy YAML routes, separate from SDK configuration."""

import re
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from ruamel.yaml import YAML

from .common import BASE_URL, SetupError
from .storage import read_file

ROUTE = "cometapi-connect"


def supported(path):
    try:
        raw = read_file(path)
        doc = YAML(typ="safe", pure=True).load(raw.decode()) if raw else None
        if not isinstance(doc, dict) or not isinstance(doc.get("model_list"), list):
            return False
        return all(
            isinstance(m, dict)
            and isinstance(m.get("model_name"), str)
            and isinstance(m.get("litellm_params"), dict)
            for m in doc["model_list"]
        )
    except (OSError, ValueError, SetupError):
        return False
    except Exception:
        return False


def process_paths(output):
    """Only absolute --config paths of identifiable proxy commands; no evaluation."""
    found = []
    for line in output.splitlines():
        try:
            args = shlex.split(line)
        except ValueError:
            continue
        # Python entry point paths can precede the CLI; avoid matching arbitrary
        # text in another program's prompt or nested command string.
        if not any(
            PurePosixPath(arg).name in ("litellm", "litellm.exe", "proxy_cli.py")
            for arg in args[:3]
        ):
            continue
        for i, arg in enumerate(args):
            value = (
                args[i + 1]
                if arg in ("--config", "-c") and i + 1 < len(args)
                else arg[len("--config=") :]
                if arg.startswith("--config=")
                else None
            )
            if value and PurePosixPath(value).is_absolute():
                found.append(Path(value))
    return found


def paths(ctx, allow_empty=False):
    candidates = []
    # Existing user-selected/project config paths only. Do not invent a global
    # file that a proxy or SDK never reads.
    for root in ctx.project_roots:
        for filename in ("litellm_config.yaml", "litellm-config.yaml", "config.yaml"):
            candidates.append(root / filename)
    managed = ctx.xdg / "litellm/cometapi-connect.yaml"
    if managed.is_file():
        candidates.append(managed)
    if ctx.use_path and ctx.platform in ("darwin", "linux"):
        try:
            result = subprocess.run(
                ["/bin/ps", "-A", "-o", "args="], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                candidates.extend(process_paths(result.stdout))
        except (OSError, subprocess.TimeoutExpired):
            pass
    result = [path for path in dict.fromkeys(candidates) if supported(path)]
    if not result and allow_empty:
        return []
    if len(result) != 1:
        raise SetupError(
            "Select one LiteLLM Proxy installation with an existing model_list YAML configuration. Multiple proxies must be configured separately."
        )
    return result


HEADER = b"# CometAPI Connect managed LiteLLM proxy v1\n"
START = "# >>> CometAPI Connect: LiteLLM >>>"
END = "# <<< CometAPI Connect: LiteLLM <<<"


def cli_installation(ctx):
    if ctx.platform not in ("darwin", "linux"):
        raise SetupError("First-time LiteLLM setup currently supports macOS/Linux bash and zsh.")
    shell = Path(ctx.env.get("SHELL", "/bin/zsh" if ctx.platform == "darwin" else "/bin/bash")).name
    if shell not in ("bash", "zsh"):
        raise SetupError("This LiteLLM shell needs a dedicated launcher adapter.")
    candidates = [r / "bin/litellm" for r in ctx.roots if (r / "bin/litellm").is_file()]
    if not candidates and ctx.use_path:
        found = shutil.which("litellm", path=ctx.env.get("PATH"))
        if found:
            candidates = [Path(found)]
    if len(candidates) != 1:
        raise SetupError("Select one installed LiteLLM Proxy CLI for first-time setup.")
    cli = candidates[0].resolve()
    raw = read_file(cli)
    if not raw or b"from litellm import run_server" not in raw:
        raise SetupError("Unrecognized LiteLLM CLI entry point.")
    manifests = list(
        cli.parent.parent.glob("lib/python*/site-packages/litellm-*.dist-info/METADATA")
    )
    matches = []
    for manifest in manifests:
        try:
            text = (read_file(manifest) or b"").decode("utf-8")
        except (SetupError, OSError, UnicodeError):
            continue
        name = re.search(r"^Name: (.+)$", text, re.M)
        version = re.search(r"^Version: (.+)$", text, re.M)
        if name and version and name[1].strip() == "litellm" and version[1].strip():
            matches.append(version[1].strip())
    if len(matches) != 1:
        raise SetupError(
            "First-time LiteLLM setup requires one identifiable LiteLLM CLI installation with its native run_server entry point."
        )
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
    return cli, profiles


def launcher_bytes(cli, config):
    # Existing explicit model/config commands retain their original behavior.
    return (
        "#!/bin/sh\n# CometAPI Connect LiteLLM launcher v1\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in -c|--config|--config=*|--model|--model=*|--help|--version)\n'
        "    exec " + shlex.quote(str(cli)) + ' "$@" ;;\n  esac\ndone\n'
        "exec "
        + shlex.quote(str(cli))
        + " --config "
        + shlex.quote(str(config))
        + ' --host 127.0.0.1 "$@"\n'
    ).encode()


def shell_bytes(before, launcher):
    try:
        text = (before or b"").decode("utf-8")
    except UnicodeError:
        raise SetupError("The LiteLLM shell profile is not UTF-8.") from None
    pattern = re.compile(re.escape(START) + r"\n.*?" + re.escape(END) + r"\n?", re.S)
    matches = list(pattern.finditer(text))
    if len(matches) > 1 or text.count(START) != len(matches) or text.count(END) != len(matches):
        raise SetupError("The existing LiteLLM launcher block was edited; it was preserved.")
    if re.search(
        r"(?m)^\s*(?:alias\s+litellm=|function\s+litellm\b|litellm\s*\(\s*\))",
        pattern.sub("", text),
    ):
        raise SetupError("An existing LiteLLM shell alias/function was preserved.")
    block = (
        START
        + "\nfunction litellm() {\n  command /bin/sh "
        + shlex.quote(str(launcher))
        + ' "$@"\n}\n'
        + END
        + "\n"
    )
    if matches:
        return (text[: matches[0].start()] + block + text[matches[0].end() :]).encode()
    return (text + ("\n" if text and not text.endswith("\n") else "") + block).encode()


def changes(name, ctx, key, model):
    from .adapters import Change, edit

    found = paths(ctx, allow_empty=True)
    managed = ctx.xdg / "litellm/cometapi-connect.yaml"
    if found and found != [managed]:
        return [
            edit(
                "litellm",
                name,
                path,
                "yaml",
                lambda doc: update(doc, key, model),
                ["CometAPI Connect proxy route (cometapi-connect)"],
                [
                    "Restart this existing proxy to load its updated route. Existing aliases and authentication remain in place."
                ],
                contains_credentials=True,
            )
            for path in found
        ]
    cli, profiles = cli_installation(ctx)
    before = read_file(managed)
    if before is not None and not before.startswith(HEADER):
        raise SetupError("An unrelated file occupies the managed LiteLLM configuration path.")

    def configure(doc):
        if before is None:
            doc.update(
                model_list=[],
                general_settings={"master_key": key},
                litellm_settings={"telemetry": False, "num_retries": 0},
            )
        update(doc, key, model)
        doc["general_settings"]["master_key"] = key

    config = edit(
        "litellm",
        name,
        managed,
        "yaml",
        configure,
        ["Create or update the local CometAPI proxy and authentication"],
        [
            "Configures the litellm command in new bash/zsh terminals. It loads the managed YAML and binds to localhost. Use your CometAPI key to access the local proxy, with model alias cometapi-connect. Explicit --config/--model commands keep their own settings. Absolute CLI invocations and SDK calls are separate."
        ],
        contains_credentials=True,
    )
    if before is None:
        config.after = HEADER + config.after
    launcher = managed.parent / "launch.sh"
    script = launcher_bytes(cli, managed)
    old_script = read_file(launcher)
    if old_script is not None and old_script != script:
        raise SetupError("The existing LiteLLM launcher was changed; it was preserved.")
    result = [
        config,
        Change(
            "litellm",
            name,
            launcher,
            old_script,
            script,
            ["Automatic LiteLLM startup configuration"],
        ),
    ]
    for path in profiles:
        before_profile = read_file(path)
        result.append(
            Change(
                "litellm",
                name,
                path,
                before_profile,
                shell_bytes(before_profile, launcher),
                ["Persistent litellm command"],
            )
        )
    return result


def update(doc, key, model):
    models = doc.get("model_list")
    if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
        raise SetupError("Unsupported LiteLLM Proxy model_list.")
    matches = [m for m in models if m.get("model_name") == ROUTE]
    if len(matches) > 1:
        raise SetupError("Multiple LiteLLM routes use the CometAPI Connect name.")
    if matches:
        entry = matches[0]
        params = entry.get("litellm_params")
        if not isinstance(params, dict) or params.get("api_base", "").rstrip("/") != BASE_URL:
            raise SetupError(
                "The LiteLLM CometAPI Connect route already points to another endpoint."
            )
    else:
        entry = {"model_name": ROUTE, "litellm_params": {}}
        models.append(entry)
    params = entry["litellm_params"]
    params.update(model="openai/" + model, api_base=BASE_URL, api_key=key)
    params.setdefault("max_tokens", 2048)
    params.setdefault("timeout", 60)
    # Keep existing aliases, master keys, access control, callbacks, fallbacks,
    # and non-chat routes exactly as they are.
