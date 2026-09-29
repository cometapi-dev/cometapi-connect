"""Experimental CometAPI cloud tab for AUTOMATIC1111; no local checkpoint needed."""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import folder_paths
import requests
from PIL import Image

ORIGIN = "https://api.cometapi.com"
IMAGE_MODELS = ["stability-ai/sdxl", "stability-ai/stable-diffusion-3.5-medium"]
VIDEO_MODELS = ["sora-2", "sora-2-pro"]
OUTPUT = Path(folder_paths.get_output_directory()) / "cometapi"
LOCK = threading.Lock()
CONFIG_PATH = Path(__file__).resolve().parent / "cometapi.json"


def configured_key():
    # This file is installed transactionally by CometAPI Connect with mode 0600.
    # Read it for each job, so rotating the key does not require a server restart.
    if CONFIG_PATH.is_file():
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("api_key", "")
            if not isinstance(value, str) or not re.fullmatch(r"sk-[A-Za-z0-9_-]{8,250}", value):
                raise ValueError()
            return value
        except Exception:
            raise ValueError(
                "The saved CometAPI configuration is invalid; run CometAPI Connect again."
            ) from None
    return os.environ.get("COMETAPI_API_KEY", "").strip()


def api(path, key, payload=None, multipart=False):
    kwargs = {
        "headers": {"Authorization": "Bearer " + key},
        "timeout": (15, 60),
        "allow_redirects": False,
    }
    if payload is not None:
        if multipart:
            kwargs["files"] = {k: (None, str(v)) for k, v in payload.items()}
        else:
            kwargs["json"] = payload
    with requests.request(
        "POST" if payload is not None else "GET", ORIGIN + path, **kwargs
    ) as response:
        if response.status_code not in (200, 201, 202):
            # Provider bodies can contain secrets; never expose them in UI/logs.
            raise RuntimeError("CometAPI returned HTTP " + str(response.status_code))
        return response.json()


def output_urls(value):
    """Replicate outputs may be a single URL or a list, depending on model."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise ValueError("Expected exactly one generated image")
    parsed = urlsplit(values[0])
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not (host == "replicate.delivery" or host.endswith(".replicate.delivery"))
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Unrecognized image delivery host")
    return values[0]


def download(url, destination, key=None, limit=64 * 1024 * 1024):
    headers = {"Authorization": "Bearer " + key} if key else {}
    if key and not url.startswith(ORIGIN + "/v1/videos/"):
        raise ValueError("Authenticated downloads must use the CometAPI video route")
    # Never forward a key to a delivery host or follow an authenticated redirect.
    with requests.get(
        url, headers=headers, timeout=(15, 120), stream=True, allow_redirects=False
    ) as response:
        if response.status_code != 200:
            raise RuntimeError("Media download returned HTTP " + str(response.status_code))
        total = 0
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > limit:
                    raise ValueError("Generated file exceeded download size limit")
                output.write(chunk)
        if total == 0:
            raise ValueError("Empty generated file")


def generate(kind, model, prompt, entered_key):
    try:
        key = entered_key.strip() or configured_key()
    except ValueError as exc:
        yield None, None, str(exc), ""
        return
    if not key:
        yield None, None, "Enter your CometAPI key.", ""
        return
    if (
        not prompt.strip()
        or len(prompt) > 4000
        or model not in (IMAGE_MODELS if kind == "image" else VIDEO_MODELS)
    ):
        yield None, None, "Choose a supported model and enter a prompt of 1–4,000 characters.", ""
        return
    if not LOCK.acquire(blocking=False):
        yield None, None, "Another job is running. Wait for it to finish.", ""
        return
    run_id = uuid.uuid4().hex
    record = {
        "run_id": run_id,
        "kind": kind,
        "model": model,
        "prompt": prompt,
        "started_at": time.time(),
        "status": "submitting",
    }
    record_path = OUTPUT / (run_id + ".json")

    def save_record():
        # The key is never written to the job record, even if pasted into a prompt.
        fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(record, indent=2).replace(key, "[REDACTED]"))

    job_id = None
    try:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        save_record()
        yield None, None, "Submitting one paid job…", ""
        if kind == "image":
            created = api(
                "/replicate/v1/models/" + model + "/predictions",
                key,
                {"input": {"prompt": prompt, "num_outputs": 1}},
            )
        else:
            created = api(
                "/v1/videos",
                key,
                {"model": model, "prompt": prompt, "seconds": "4", "size": "1280x720"},
                multipart=True,
            )
        job_id = created.get("id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", job_id):
            raise ValueError("Response did not contain a usable job ID; no automatic resubmission")
        record.update(job_id=job_id, status=created.get("status", "queued"))
        save_record()
        route = ("/replicate/v1/predictions/" if kind == "image" else "/v1/videos/") + job_id
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = api(route, key)
            phase = status.get("status", "unknown")
            yield None, None, "Job " + job_id + ": " + str(phase), job_id
            if phase in ("failed", "canceled", "cancelled"):
                raise RuntimeError("Provider marked job " + str(phase))
            if phase in ("succeeded", "completed"):
                if kind == "image":
                    destination = OUTPUT / (run_id + ".image")
                    download(output_urls(status.get("output")), destination)
                    with Image.open(destination) as im:
                        im.load()
                        dimensions = list(im.size)
                        extension = {"PNG": ".png", "WEBP": ".webp", "JPEG": ".jpg"}.get(im.format)
                    if not extension:
                        raise ValueError("Unsupported returned image format")
                    final = destination.with_suffix(extension)
                    destination.rename(final)
                    record["dimensions"] = dimensions
                    image_result, video_result = str(final), None
                else:
                    final = OUTPUT / (run_id + ".mp4")
                    download(ORIGIN + route + "/content", final, key, 128 * 1024 * 1024)
                    if final.read_bytes()[4:8] != b"ftyp":
                        raise ValueError("Response was not an MP4 container")
                    image_result, video_result = None, str(final)
                    record.update(requested_seconds=4, requested_size="1280x720")
                record.update(
                    status="completed",
                    output_path=str(final),
                    sha256=hashlib.sha256(final.read_bytes()).hexdigest(),
                    finished_at=time.time(),
                )
                save_record()
                yield (
                    image_result,
                    video_result,
                    "Completed · " + model + " · saved " + final.name,
                    job_id,
                )
                return
            time.sleep(5)
        raise TimeoutError(
            "Job is still pending after ten minutes; use its ID to check later, do not resubmit"
        )
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (ValueError, RuntimeError, TimeoutError))
            else type(exc).__name__
        )
        message = message.replace(key, "[REDACTED]")[:500]
        record.update(status="error", error=message, finished_at=time.time())
        try:
            save_record()
        except OSError:
            # A broken output directory must not hide the error or lock future jobs.
            pass
        yield None, None, message + ((" · Job " + job_id) if job_id else ""), job_id or ""
    finally:
        LOCK.release()
