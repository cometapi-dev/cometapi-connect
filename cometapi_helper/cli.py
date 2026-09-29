"""Desktop entry point plus an optional automation-friendly command line."""

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .common import Context, SetupError
from .engine import Engine


def output(value):
    if sys.stdout is not None:
        print(value)


def show_error(message):
    if sys.stderr is not None:
        print("Error: " + message, file=sys.stderr)
    elif sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "CometAPI Connect", 0x10)
    elif sys.platform == "darwin":
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'on run argv\ndisplay dialog (item 1 of argv) with title "CometAPI Connect" buttons {"OK"} default button "OK" with icon stop\nend run',
                message,
            ],
            check=False,
        )


def parser():
    result = argparse.ArgumentParser(
        prog="cometapi-connect",
        description="Set up supported AI apps for CometAPI. Launch without arguments for the local setup window.",
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument(
        "--home",
        type=Path,
        help="Alternate user home (isolates environment overrides and PATH discovery)",
    )
    result.add_argument(
        "--root",
        action="append",
        default=[],
        help="Additional project directory to scan (repeatable)",
    )
    sub = result.add_subparsers(dest="command")
    wizard = sub.add_parser("serve", help="Open the setup window")
    wizard.add_argument("--no-browser", action="store_true")
    wizard.add_argument("--port", type=int, default=0)
    scan = sub.add_parser("scan", help="Detect apps without changing files")
    scan.add_argument("--json", action="store_true")
    configure = sub.add_parser("configure", help="Configure selected apps with a change preview")
    configure.add_argument("--apps", nargs="+", help="App IDs (run scan to list)")
    configure.add_argument(
        "--key-stdin",
        action="store_true",
        help="Read a key from stdin; never place keys in command-line arguments",
    )
    configure.add_argument(
        "--yes",
        action="store_true",
        help="Apply the displayed changes without an interactive confirmation",
    )
    configure.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing any configuration or backups",
    )
    for name in ("chat-model", "claude-model", "codex-model", "image-model"):
        configure.add_argument("--" + name)
    sub.add_parser("history", help="List local backup transactions")
    restore = sub.add_parser(
        "restore", help="Restore a backup if files have not changed since setup"
    )
    restore.add_argument("transaction_id")
    restore.add_argument("--yes", action="store_true")
    sub.add_parser(
        "models", help="Fetch the public text-model catalog; no key or generation request"
    )
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    engine = None
    try:
        context = (
            Context(home=args.home, roots=args.root, env={}, use_path=False)
            if args.home
            else Context(roots=args.root)
        )
        engine = Engine(context)
        if args.command in (None, "serve"):
            from .server import serve

            serve(engine, getattr(args, "port", 0), not getattr(args, "no_browser", False), output)
        elif args.command == "scan":
            scan = engine.scan()
            if args.json:
                output(json.dumps(scan, indent=2))
            else:
                for app in scan["apps"]:
                    output(
                        "{:<18} {:<11} {:<13} {}".format(
                            app["id"],
                            "detected" if app["detected"] else "not found",
                            app["mode"],
                            app["name"],
                        )
                    )
        elif args.command == "configure":
            ids = args.apps
            if not ids:
                if not sys.stdin.isatty():
                    raise SetupError(
                        "Provide --apps for noninteractive setup. Run scan to list supported app IDs."
                    )
                available = [
                    app
                    for app in engine.scan()["apps"]
                    if app["mode"] == "automatic" and app["detected"]
                ]
                for app in available:
                    output(app["id"] + " — " + app["name"])
                ids = input("App IDs to configure (space separated): ").split()
            if args.key_stdin:
                key = sys.stdin.readline(512).rstrip("\r\n")
            elif "COMETAPI_KEY" in os.environ:
                key = os.environ["COMETAPI_KEY"]
            else:
                if not sys.stdin.isatty():
                    raise SetupError("Use --key-stdin or COMETAPI_KEY for noninteractive setup.")
                key = getpass.getpass("CometAPI key (hidden): ")
            models = {
                name: getattr(args, name)
                for name in ("chat_model", "claude_model", "codex_model", "image_model")
                if getattr(args, name)
            }
            plan = engine.preview(key, ids, models)
            del key
            output(json.dumps({k: v for k, v in plan.items() if k != "plan_id"}, indent=2))
            if args.dry_run:
                output("Dry run complete. No files changed.")
            elif args.yes or input("Apply these changes? [y/N] ").strip().lower() == "y":
                output(json.dumps(engine.apply(plan["plan_id"]), indent=2))
            else:
                output("Cancelled. No files changed.")
        elif args.command == "history":
            output(json.dumps(engine.history(), indent=2))
        elif args.command == "restore":
            if (
                args.yes
                or input("Restore backup " + args.transaction_id + "? [y/N] ").strip().lower()
                == "y"
            ):
                output(engine.restore(args.transaction_id)["message"])
        elif args.command == "models":
            from .server import fetch_models

            output(json.dumps(fetch_models(), indent=2))
    except (SetupError, OSError) as error:
        # Only explicitly safe SetupError text is suitable for display.
        message = (
            str(error)
            if isinstance(error, SetupError)
            else "Unable to access the selected files or start the local setup window. Check permissions and try again."
        )
        show_error(message)
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.plans.clear()


if __name__ == "__main__":
    main()
