"""Bounded, read-only discovery from known locations, PATH, and project markers."""

import json
import os
import shutil
from pathlib import Path

import json5

from . import (
    chatbox,
    cherry_store,
    copilot_store,
    dify_store,
    flowise_store,
    fooocus,
    gemini_launcher,
    jan,
    litellm_config,
    lmstudio_store,
    n8n_store,
    python_projects,
    roo_store,
    webui_store,
)
from .adapters import AUTOMATIC_IDS, opencode_paths
from .common import SetupError
from .file_clients import SOURCE_IDS, anything_storage, invoke_root, silly_profile

EXTENSIONS = {
    "continue": "continue.continue-",
    "cline": "saoudrizwan.claude-dev-",
    "roo-code": "rooveterinaryinc.roo-cline-",
    "kilo-code": "kilocode.kilo-code-",
    "copilot": "github.copilot-",
    "github-copilot": "github.copilot-",
}


def catalog():
    entries = []
    for filename in ("coding_catalog.json", "catalog.json"):
        entries.extend(json.loads((Path(__file__).parent / filename).read_text(encoding="utf-8")))
    seen = set()
    for entry in entries:
        if entry["id"] in seen:
            raise ValueError("Duplicate catalog ID")
        seen.add(entry["id"])
    return entries


def child_dirs(path, cap=300):
    try:
        result = []
        with os.scandir(path) as iterator:
            for item in iterator:
                if len(result) >= cap:
                    break
                if item.name.startswith(".") or item.name in (
                    "node_modules",
                    "venv",
                    "dist",
                    "build",
                ):
                    continue
                if item.is_dir(follow_symlinks=False):
                    result.append(Path(item.path))
        return result
    except OSError:
        return []


def project_candidates(ctx):
    result = []
    for root in ctx.project_roots:
        if not root.is_dir() or root.is_symlink():
            continue
        result.append(root)
        first = child_dirs(root)
        result.extend(first)
        for folder in first:
            # Do not traverse inside a project, dependency tree, or hidden directory.
            if not any(
                (folder / marker).exists() for marker in (".git", "package.json", "pyproject.toml")
            ):
                result.extend(child_dirs(folder, 50))
            if len(result) >= 1500:
                return list(dict.fromkeys(result[:1500]))
    return list(dict.fromkeys(result))


def source_app(path):
    try:
        if all(
            (path / marker).is_file()
            for marker in ("webui.py", "fooocus_version.py", "modules/config.py", "launch.py")
        ):
            return "fooocus"
        if all(
            (path / marker).is_file()
            for marker in ("main.py", "folder_paths.py", "comfy_api/latest/__init__.py")
        ):
            return "comfyui"
        if all(
            (path / marker).is_file()
            for marker in ("launch.py", "modules/launch_utils.py", "modules/script_callbacks.py")
        ):
            return "automatic1111"
        package = path / "package.json"
        if not package.is_file() or package.stat().st_size > 256000:
            return None
        name = json.loads(package.read_text(encoding="utf-8")).get("name", "").lower()
        if name in ("nextchat", "chatgpt-next-web") and (path / "app/api/common.ts").is_file():
            return "nextchat"
        if name == "librechat" and (path / "librechat.example.yaml").is_file():
            return "librechat"
        if name == "openmaic" and (path / "lib/server/provider-config.ts").is_file():
            return "openmaic"
        if (
            name in ("@lobehub/chat", "@lobehub/lobehub")
            and (path / "src/server/modules/ModelRuntime/index.ts").is_file()
        ):
            return "lobechat"
        if name.lower() == "sillytavern" and (path / "src/endpoints/secrets.js").is_file():
            return "sillytavern"
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _known_paths(ctx, entry):
    result = []
    for raw in entry.get("detection", {}).get("home_paths", []):
        raw = raw.removeprefix("~/")
        if raw.startswith(".config/"):
            path = ctx.xdg / raw[len(".config/") :]
        elif raw.startswith("AppData/Roaming/") and ctx.env.get("APPDATA"):
            path = Path(ctx.env["APPDATA"]) / raw[len("AppData/Roaming/") :]
        elif raw.startswith("AppData/Local/") and ctx.env.get("LOCALAPPDATA"):
            path = Path(ctx.env["LOCALAPPDATA"]) / raw[len("AppData/Local/") :]
        else:
            path = ctx.home / raw
        if "*" in path.name:
            # Catalog-controlled, one-directory patterns only, never recursive globs.
            try:
                result.extend(str(p) for p in list(path.parent.glob(path.name))[:100])
            except OSError:
                pass
            continue
        if path.exists():
            result.append(str(path))
    # Honor the actual platform environment for relocated configuration directories.
    identity = entry["id"]
    overrides = {
        "claude-code": ("CLAUDE_CONFIG_DIR", ".claude"),
        "codex": ("CODEX_HOME", ".codex"),
        "continue": ("CONTINUE_GLOBAL_DIR", ".continue"),
    }
    if identity in overrides:
        variable, fallback = overrides[identity]
        path = ctx.config_dir(variable, fallback)
        if path.exists():
            result.append(str(path))
    if identity == "opencode" and (ctx.xdg / "opencode").exists():
        result.append(str(ctx.xdg / "opencode"))
    return result


def _bundled_copilot_candidates(ctx):
    """Known VSCode layouts only; a Code executable alone is not evidence."""
    resources = []
    if ctx.platform == "darwin":
        for root in (ctx.home / "Applications", Path("/Applications")):
            for name in ("Visual Studio Code.app", "Visual Studio Code - Insiders.app"):
                resources.append(root / name / "Contents/Resources/app")
    elif ctx.platform == "win32":
        roots = [ctx.home / "AppData/Local/Programs"]
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if ctx.env.get(variable):
                root = Path(ctx.env[variable])
                if root.is_absolute():
                    roots.extend((root, root / "Programs"))
        for root in roots:
            for name in ("Microsoft VS Code", "Microsoft VS Code Insiders"):
                resources.append(root / name / "resources/app")
    else:
        resources.extend(
            Path(root) / "resources/app"
            for root in (
                "/usr/share/code",
                "/usr/share/code-insiders",
                "/opt/visual-studio-code",
                "/opt/vscode",
            )
        )
    if ctx.use_path:
        for command in ("code", "code-insiders"):
            executable = shutil.which(command, path=ctx.env.get("PATH", ""))
            if executable:
                try:
                    installation = Path(executable).resolve().parent.parent
                    # macOS: Resources/app/bin/code; Windows/Linux: <install>/bin/code.
                    resources.extend((installation, installation / "resources/app"))
                except (OSError, RuntimeError):
                    pass
    return list(
        dict.fromkeys(
            root / "extensions" / folder / "package.json"
            for root in resources
            for folder in ("copilot", "github.copilot-chat")
        )
    )


def _bundled_copilot_paths(ctx):
    result = []
    limit = 1024 * 1024
    for manifest in _bundled_copilot_candidates(ctx):
        try:
            if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > limit:
                continue
            with manifest.open("rb") as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                continue
            package = json.loads(raw)
            if not isinstance(package, dict):
                continue
            publisher, name = package.get("publisher"), package.get("name")
            if (
                isinstance(publisher, str)
                and isinstance(name, str)
                and publisher.casefold() == "github"
                and name.casefold() == "copilot-chat"
            ):
                result.append(str(manifest))
        except (OSError, ValueError, UnicodeError):
            continue
    return result


def scan(ctx):
    entries = catalog()
    candidates = project_candidates(ctx)
    verified = {}
    for path in candidates:
        identity = source_app(path)
        if identity:
            verified.setdefault(identity, []).append(path)
    app_roots = [ctx.home / "Applications"]
    if ctx.platform == "darwin":
        app_roots.append(Path("/Applications"))
    elif ctx.platform == "win32":
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if ctx.env.get(variable):
                root = Path(ctx.env[variable])
                app_roots.extend([root, root / "Programs"])
        app_roots.append(ctx.home / "AppData/Local/Programs")
    else:
        app_roots.extend(
            [Path("/opt"), ctx.home / ".local/share/applications", Path("/usr/share/applications")]
        )
    extension_names = []
    for relative in (
        ".vscode/extensions",
        ".vscode-insiders/extensions",
        ".cursor/extensions",
        ".windsurf/extensions",
    ):
        extension_names.extend(str(path) for path in child_dirs(ctx.home / relative, 1000))
    bundled_copilot = _bundled_copilot_paths(ctx)
    apps = []
    for entry in entries:
        detection = entry.get("detection", {})
        evidence = _known_paths(ctx, entry)
        commands_found = set()
        if ctx.use_path:
            # Finder and GUI launchers often supply a minimal PATH.
            locations = ctx.env.get("PATH", "").split(os.pathsep)
            locations.extend(
                str(ctx.home / p)
                for p in (".local/bin", ".bun/bin", ".cargo/bin", ".npm-global/bin", "Library/pnpm")
            )
            if ctx.platform == "win32":
                locations.append(
                    str(Path(ctx.env.get("APPDATA", str(ctx.home / "AppData/Roaming"))) / "npm")
                )
            else:
                locations.append("/usr/local/bin")
                if ctx.platform == "darwin":
                    locations.append("/opt/homebrew/bin")
            search_path = os.pathsep.join(
                dict.fromkeys(location for location in locations if location)
            )
            for command in detection.get("commands", []):
                executable = shutil.which(command, path=search_path)
                if executable:
                    commands_found.add(command)
                    evidence.append("Command: " + executable)
        for root in app_roots:
            for name in detection.get("app_names", []):
                variants = [name, name + ".app", name + ".desktop"]
                evidence.extend(
                    str(root / candidate) for candidate in variants if (root / candidate).exists()
                )
        prefix = EXTENSIONS.get(entry["id"])
        if prefix:
            evidence.extend(p for p in extension_names if Path(p).name.lower().startswith(prefix))
        if entry["id"] == "github-copilot":
            evidence.extend("Bundled extension: " + path for path in bundled_copilot)
        repo_names = {str(n).casefold() for n in detection.get("repo_names", [])}
        for path in candidates:
            if path.name.casefold() in repo_names and any(
                (path / marker).exists()
                for marker in (".git", "package.json", "pyproject.toml", "requirements.txt")
            ):
                evidence.append("Project: " + str(path))
        evidence.extend("Verified source: " + str(path) for path in verified.get(entry["id"], []))
        app = {key: value for key, value in entry.items() if key != "detection"}
        app["mode"] = "automatic" if entry["id"] in AUTOMATIC_IDS else "guided"
        app["detected"] = bool(evidence)
        app["evidence"] = list(dict.fromkeys(evidence))
        if entry["id"] in python_projects.IDS:
            app["automation_scope"] = (
                "Recognized top-level Python project modules · native provider constructors"
            )
            try:
                projects = python_projects.find(ctx, entry["id"], candidates)
                app["detected"] = True
                app["evidence"].extend(str(path) for _, paths in projects for path in paths)
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "lm-studio":
            app["automation_scope"] = (
                "macOS / Windows · LM Studio official remote generator · initialized profile"
            )
            try:
                target = lmstudio_store.find(ctx)
                lmstudio_store.documents(target)
                app["detected"] = True
                app["evidence"].append(target["database"])
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "github-copilot":
            app["automation_scope"] = (
                "macOS / Windows · VS Code · native BYOK chat and utility models"
            )
            try:
                target = copilot_store.find(ctx)
                app["detected"] = True
                app["evidence"].append(target["database"])
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "dify":
            app["automation_scope"] = "Local Docker · Dify API container · existing chat models"
            try:
                target = dify_store.find(ctx)
                dify_store.read(target)
                app["detected"] = True
                app["evidence"].append("Docker: " + target["container"][:12])
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "roo-code":
            app["automation_scope"] = (
                "macOS / Windows · VS Code default or portable profile · Roo Code"
            )
            try:
                target = roo_store.find(ctx)
                app["detected"] = True
                app["evidence"].append(target["database"])
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "cherry-studio":
            app["automation_scope"] = (
                "macOS / Windows · Cherry Studio · compatible Redux provider store"
            )
            try:
                target = cherry_store.find(ctx)
                app["detected"] = True
                app["evidence"].append(target["database"])
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "gemini-cli":
            app["automation_scope"] = "macOS/Linux bash/zsh · Windows PowerShell 7 · npm CLI"
            try:
                gemini_launcher.changes(
                    entry["name"], ctx, "sk-detection-placeholder", "gemini-3.5-flash"
                )
                app["detected"] = True
                app["evidence"].append(str(gemini_launcher.locate(ctx)["entry"]))
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "flowise":
            app["automation_scope"] = "Personal SQLite · existing ChatOpenAI workflows"
            try:
                target = flowise_store.find(ctx)
                flowise_store.prepare_value(
                    target, flowise_store.read(target), "sk-detection-placeholder"
                )
                app["evidence"].append(target["database"])
                app["detected"] = True
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "n8n":
            app["automation_scope"] = "Personal SQLite installation · existing OpenAI credentials"
            try:
                app["evidence"].append(str(n8n_store.find(ctx)))
                app["detected"] = True
            except (SetupError, OSError, ValueError):
                app["mode"] = "guided"
        if entry["id"] == "fooocus":
            app["automation_scope"] = "Fooocus · compatible Gradio launch · CometAPI cloud panel"
            if not verified.get("fooocus") or not all(
                fooocus.supported(p) for p in verified["fooocus"]
            ):
                app["mode"] = "guided"
        if entry["id"] == "litellm":
            app["automation_scope"] = "Proxy YAML · automatic bash/zsh initialization when needed"
            try:
                app["evidence"].extend(
                    str(c.path)
                    for c in litellm_config.changes(
                        entry["name"], ctx, "sk-detection-placeholder", "gpt-4.1-mini"
                    )
                )
                app["detected"] = True
            except SetupError:
                app["mode"] = "guided"
        if entry["id"] == "open-webui":
            app["automation_scope"] = "Local SQLite · per-key config schema"
            try:
                path = webui_store.find(ctx)
                app["evidence"].append(str(path))
                app["detected"] = True
                if ctx.env.get("ENABLE_PERSISTENT_CONFIG", "true").lower() == "false":
                    app["mode"] = "guided"
            except (SetupError, ValueError, OSError):
                app["mode"] = "guided"
        if entry["id"] == "jan":
            app["automation_scope"] = (
                "macOS / Windows Desktop · compatible initialized provider store"
            )
            try:
                path = jan.settings_path(ctx)
                if ctx.platform not in ("darwin", "win32") or not jan.supported(path):
                    app["mode"] = "guided"
                elif path.is_file():
                    app["evidence"].append(str(path))
                    app["detected"] = True
            except (SetupError, ValueError, OSError):
                app["mode"] = "guided"
        if entry["id"] in ("cline", "kilo-code"):
            app["automation_scope"] = "CLI / shared backend"
            extension_only = any(
                Path(p).name.lower().startswith(EXTENSIONS[entry["id"]]) for p in extension_names
            )
            if extension_only and not commands_found:
                app["mode"] = "guided"
                app["automation_scope"] = "Editor extension adapter pending"
                app["description"] += (
                    " This detected editor extension has not been verified against the CLI's shared configuration store."
                )
        if entry["id"] == "zed" and ctx.platform not in ("darwin", "win32"):
            app["mode"] = "guided"
        elif entry["id"] == "zed":
            app["automation_scope"] = "macOS / Windows · system credential store"
        if entry["id"] == "chatbox":
            profiles = chatbox.config_paths(ctx)
            app["evidence"].extend(str(path) for path in profiles)
            app["detected"] = bool(app["evidence"])
            if len(profiles) != 1 or not chatbox.supported_config(profiles[0]):
                app["mode"] = "guided"
        if entry["id"] in ({"nextchat", "librechat"} | SOURCE_IDS):
            app["name"] += " (source install)"
            if not verified.get(entry["id"]):
                app["mode"] = "guided"
        if entry["id"] == "invokeai":
            try:
                root = invoke_root(ctx)
                if (root / "invokeai.yaml").is_file():
                    app["detected"] = True
                    app["evidence"].append(str(root / "invokeai.yaml"))
                else:
                    app["mode"] = "guided"
            except SetupError:
                app["mode"] = "guided"
        if entry["id"] == "anythingllm":
            try:
                storage = anything_storage(ctx)
                if (storage / "anythingllm.db").is_file():
                    app["detected"] = True
                    app["evidence"].append(str(storage))
            except SetupError:
                app["mode"] = "guided"
        if entry["id"] == "sillytavern" and app["mode"] == "automatic":
            try:
                for root in verified["sillytavern"]:
                    silly_profile(root)
            except SetupError:
                app["mode"] = "guided"
        if entry["id"] == "opencode":
            try:
                paths = opencode_paths(ctx)
            except SetupError:
                paths = []
            existing = [p for p in paths if p.exists()]
            supported = bool(paths)
            for path in existing:
                try:
                    if path.stat().st_size < 4 * 1024 * 1024:
                        document = json5.loads(
                            path.read_text(encoding="utf-8-sig"), allow_duplicate_keys=False
                        )
                        # OpenCode 2 is a separate beta executable. A v2-shaped
                        # file is not evidence that the installed runtime reads it.
                        supported = (
                            supported
                            and isinstance(document, dict)
                            and isinstance(document.get("provider", {}), dict)
                            and "providers" not in document
                        )
                    else:
                        supported = False
                except (OSError, ValueError):
                    supported = False
            if "opencode2" in commands_found and "opencode" not in commands_found:
                # A leftover v1 file does not make the detected v2 runtime compatible.
                supported = False
            if not supported:
                app["mode"] = "guided"
        apps.append(app)
    return apps, verified
