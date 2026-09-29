"""Windows launch/discovery for native VS Code adapters."""

import json
import os
import re
import subprocess
from pathlib import Path

from .common import SetupError
from .storage import no_links, read_file


def resources(app):
    candidates = [app / "resources/app", *app.glob("*/resources/app")]
    candidates = [p for p in candidates if (p / "package.json").is_file()]
    if len(candidates) != 1:
        raise SetupError("Select one supported Windows VS Code application version.")
    root = candidates[0]
    no_links(app / "Code.exe")
    try:
        package = json.loads(read_file(root / "package.json"))
        product = json.loads(read_file(root / "product.json"))
        if (
            package.get("name"),
            product.get("applicationName"),
            product.get("win32AppUserModelId"),
        ) != ("Code", "code", "Microsoft.VisualStudioCode"):
            raise ValueError()
        if not (app / "Code.exe").is_file():
            raise ValueError()
    except (OSError, TypeError, ValueError):
        raise SetupError(
            "Windows native setup requires an official VS Code installation with its expected resource layout."
        ) from None
    return root


def locations(ctx):
    apps = [p for p in ctx.roots if (p / "Code.exe").is_file()]
    if not apps:
        local = Path(ctx.env.get("LOCALAPPDATA", str(ctx.home / "AppData/Local")))
        defaults = [local / "Programs/Microsoft VS Code"]
        if ctx.env.get("ProgramFiles"):
            defaults.append(Path(ctx.env["ProgramFiles"]) / "Microsoft VS Code")
        apps = [p for p in defaults if (p / "Code.exe").is_file()]
    if len(apps) != 1:
        raise SetupError(
            "Select one Windows VS Code installation; macOS uses its application bundle."
        )
    app = apps[0]
    resources(app)
    profiles = [p for p in ctx.roots if (p / "User/globalStorage/state.vscdb").is_file()]
    extensions = [p for p in ctx.roots if p.name == "extensions" and p.is_dir()]
    portable = app / "data"
    if portable.is_dir():
        expected_profile, expected_extensions = portable / "user-data", portable / "extensions"
        if (profiles and profiles != [expected_profile]) or (
            extensions and extensions != [expected_extensions]
        ):
            raise SetupError(
                "Portable VS Code uses its own data profile and extensions; overrides would be ignored."
            )
        return app, expected_profile, expected_extensions
    if not profiles:
        profiles = [Path(ctx.env.get("APPDATA", str(ctx.home / "AppData/Roaming"))) / "Code"]
    if not extensions:
        extensions = [ctx.home / ".vscode/extensions"]
    if len(profiles) != 1 or len(extensions) != 1:
        raise SetupError("Select one initialized default VS Code profile and extensions directory.")
    return app, profiles[0], extensions[0]


def require_closed(app, profile):
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = "Get-CimInstance Win32_Process -Filter \"Name = 'Code.exe'\" -ErrorAction Stop | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    try:
        output = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        rows = json.loads(output) if output.strip() else []
        rows = [rows] if isinstance(rows, dict) else rows
        for row in rows:
            command = row.get("CommandLine")
            if not command:
                raise ValueError("Cannot inspect a Code process")
            if "--type=" in command:
                continue
            match = re.search(r'--user-data-dir(?:=|\s+)(?:"([^"]+)"|([^\s]+))', command)
            executable_path = row.get("ExecutablePath")
            if not executable_path:
                raise ValueError("Cannot inspect a Code installation")
            running_app = Path(executable_path).parent
            if (running_app / "data").is_dir():
                selected = running_app / "data/user-data"
            elif match:
                selected = Path(match.group(1) or match.group(2))
                if not selected.is_absolute():
                    raise ValueError("Cannot resolve a relative Code profile")
            else:
                selected = (
                    Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Code"
                )
            if os.path.normcase(os.path.abspath(selected)) == os.path.normcase(
                os.path.abspath(profile)
            ):
                raise SetupError(
                    "Close the selected VS Code profile before configuring or restoring it."
                )
    except SetupError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise SetupError(
            "Cannot verify whether the selected Windows VS Code profile is closed."
        ) from None


def executable(app):
    return app / "Code.exe" if (app / "Code.exe").is_file() else app / "Contents/MacOS/Code"


def launch_options(app):
    if (app / "Code.exe").is_file():
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        return {"startupinfo": startup}
    return {"start_new_session": True}


def stop(process, app):
    if process.poll() is not None:
        return
    if (app / "Code.exe").is_file():
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process.wait(timeout=10)
    else:
        import signal

        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
