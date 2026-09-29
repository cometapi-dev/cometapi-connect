"""Existing Dify model credentials through the local official API container."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from .common import SetupError
from .roo_store import IncompleteNativeWrite
from .storage import read_file

KIND = "dify-local-docker-native-v1"
IMAGE_NAME = re.compile(
    r"(?:docker\.io/)?langgenius/dify-api(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-f]{64})?"
)
ASSET = Path(__file__).parent / "assets/dify_native.py"


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def docker(args, data=None, timeout=15):
    executable = shutil.which("docker")
    if not executable:
        raise SetupError("Dify needs Docker with a running local API container.")
    try:
        result = subprocess.run(
            [executable] + args, input=data, capture_output=True, timeout=timeout
        )
        if result.returncode:
            raise SetupError("Cannot access the selected local Dify Docker instance.")
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise SetupError("The local Dify Docker operation did not finish.") from None


def local_endpoint(endpoint):
    return (
        isinstance(endpoint, str)
        and not any(c in endpoint for c in "\r\n\0")
        and (
            endpoint.startswith("unix:///")
            or endpoint
            in {"npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine"}
        )
    )


def validate(target):
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "endpoint", "container", "image", "database"}
        or target.get("kind") != KIND
        or not isinstance(target.get("image"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", target["image"])
        or not isinstance(target.get("container"), str)
        or not re.fullmatch("[0-9a-f]{64}", target["container"])
        or not local_endpoint(target.get("endpoint"))
        or not isinstance(target.get("database"), str)
        or not Path(target["database"]).is_absolute()
        or Path(target["database"]).name != "dify-" + target["container"] + ".native"
    ):
        raise SetupError(
            "Unrecognized Dify native resource; only local Docker sockets are supported."
        )
    try:
        item = json.loads(
            docker(
                [
                    "--host",
                    target["endpoint"],
                    "inspect",
                    "--format",
                    '[{{json .Id}},{{json .Image}},{{json .State.Running}},{{json .Config.User}},{{json .Config.WorkingDir}},[{{range .Config.Env}}{{if eq . "MODE=api"}}true{{end}}{{end}}],{{json .Config.Image}}]',
                    target["container"],
                ]
            )
        )
        if (
            item[0] != target["container"]
            or item[1] != target["image"]
            or not item[2]
            or item[3] not in {"dify", "1001", "1001:1001"}
            or item[4] != "/app/api"
            or item[5] != [True]
            or not IMAGE_NAME.fullmatch(item[6])
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, IndexError):
        raise SetupError(
            "Dify requires a compatible official API image running as its ordinary user."
        ) from None


def find(ctx):
    if not ctx.use_path or ctx.platform not in {"darwin", "linux", "win32"}:
        raise SetupError("Dify local Docker detection requires Docker on macOS, Linux or Windows.")
    try:
        context = docker(["context", "show"]).decode().strip()
        metadata = json.loads(docker(["context", "inspect", context]))[0]
        endpoint = metadata["Endpoints"]["docker"]["Host"]
        if not local_endpoint(endpoint):
            raise ValueError()
        rows = (
            docker(["--host", endpoint, "ps", "--no-trunc", "--format", "{{.ID}} {{.Image}}"])
            .decode()
            .splitlines()
        )
        identities = [
            fields[0]
            for row in rows
            if len(fields := row.split()) == 2 and IMAGE_NAME.fullmatch(fields[1])
        ]
        candidates = []
        for identity in identities[:20]:
            image = (
                docker(["--host", endpoint, "inspect", "--format", "{{.Image}}", identity])
                .decode()
                .strip()
            )
            target = {
                "kind": KIND,
                "endpoint": endpoint,
                "container": identity,
                "image": image,
                "database": str(ctx.state_dir / "targets" / ("dify-" + identity + ".native")),
            }
            try:
                validate(target)
                candidates.append(target)
            except SetupError:
                continue
        if len(identities) > 20 or len(candidates) != 1:
            raise ValueError()
        return candidates[0]
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise SetupError(
            "Select one running official Dify API container in the local Docker context."
        ) from None


def run(target, request):
    validate(target)
    try:
        output = docker(
            [
                "--host",
                target["endpoint"],
                "exec",
                "-i",
                "--user",
                "1001:1001",
                target["container"],
                "/app/api/.venv/bin/python",
                "-c",
                read_file(ASSET).decode(),
            ],
            encode(request),
            timeout=55,
        )
        responses = [
            line[len(b"COMETAPI_DIFY=") :]
            for line in output.splitlines()
            if line.startswith(b"COMETAPI_DIFY=")
        ]
        if len(responses) != 1:
            raise ValueError()
        result = json.loads(responses[0])
    except (ValueError, TypeError, SetupError, OSError):
        if request["operation"] == "write":
            raise IncompleteNativeWrite(
                "Dify did not confirm its database commit. Preserve the transaction backup."
            ) from None
        raise SetupError("Dify did not finish its native configuration operation.") from None
    if not result.get("ok"):
        error = result.get("error")
        if error == "commit-unconfirmed":
            raise IncompleteNativeWrite(
                "Dify committed credentials but cache refresh was not confirmed. Preserve its backup."
            )
        messages = {
            "conflict": "Dify credentials or model bindings changed after preview; newer settings were preserved.",
            "personal-workspace-required": "Dify automatic setup requires one active owner in one local workspace.",
            "verified-plugin-required": "Dify requires the verified Marketplace OpenAI-compatible plugin with compatible credentials.",
            "existing-chat-models-required": "Dify needs existing OpenAI-compatible chat credentials without load balancing.",
            "unsupported-credential": "Dify has unsupported custom routing or protocol settings; its credentials were preserved.",
        }
        raise SetupError(
            messages.get(
                error, "Dify native configuration could not be completed for this installation."
            )
        )
    return encode(result["snapshot"])


def read(target):
    return run(target, {"operation": "read"})


def prepare_value(target, before, key):
    return run(target, {"operation": "prepare", "expected": json.loads(before), "key": key})


def write(target, value, expected):
    run(
        target,
        {"operation": "write", "expected": json.loads(expected), "desired": json.loads(value)},
    )
