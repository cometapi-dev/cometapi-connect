"""Media transport safety without a host installation or network access."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-offline_media_fixture_only_123456"
SOURCES = [
    "integrations/sd-webui-cometapi/scripts/cometapi_media.py",
    "cometapi_helper/assets/sd_webui_cometapi.py",
    "cometapi_helper/assets/comfy_cloud.py",
]


@pytest.fixture(params=SOURCES)
def media(request, monkeypatch, tmp_path):
    def unexpected_network(*args, **kwargs):
        raise AssertionError("Tests must mock every HTTP request")

    transport = ModuleType("requests")
    transport.request = Mock(side_effect=unexpected_network)
    transport.get = Mock(side_effect=unexpected_network)
    host = ModuleType("modules")
    host.paths = SimpleNamespace(data_path=str(tmp_path))
    host.script_callbacks = SimpleNamespace(on_ui_tabs=Mock())
    pil = ModuleType("PIL")
    pil.Image = Mock()
    folders = ModuleType("folder_paths")
    folders.get_output_directory = lambda: str(tmp_path)
    for name, module in {
        "requests": transport,
        "gradio": ModuleType("gradio"),
        "modules": host,
        "PIL": pil,
        "folder_paths": folders,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("media_fixture", ROOT / request.param)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CONFIG_PATH = tmp_path / "cometapi.json"
    return module


def response(status=200, chunks=(b"image bytes",)):
    result = MagicMock(status_code=status)
    result.__enter__.return_value = result
    result.iter_content.return_value = chunks
    return result


def test_image_delivery_shapes_and_destination_validation(media):
    url = "https://replicate.delivery/example/output.webp"
    assert media.output_urls(url) == url
    assert media.output_urls([url]) == url
    for value in (
        "http://replicate.delivery/a",
        "https://replicate.delivery.evil.test/a",
        "https://user:secret@replicate.delivery/a",
        "https://127.0.0.1/a",
        "https://replicate.delivery:8443/a",
        [],
        [url, url],
        {"url": url},
    ):
        with pytest.raises(ValueError):
            media.output_urls(value)


def test_authenticated_requests_reject_redirects(media, monkeypatch):
    request = Mock(return_value=response(302))
    monkeypatch.setattr(media.requests, "request", request)
    with pytest.raises(RuntimeError, match="HTTP 302"):
        media.api("/v1/videos/fixture", KEY)
    assert request.call_args.kwargs["allow_redirects"] is False
    assert request.call_args.args[1] == "https://api.cometapi.com/v1/videos/fixture"


def test_image_download_omits_credentials_and_refuses_overwrite(media, monkeypatch, tmp_path):
    get = Mock(return_value=response())
    monkeypatch.setattr(media.requests, "get", get)
    target = tmp_path / "image"
    media.download("https://replicate.delivery/a", target)
    assert get.call_args.kwargs["headers"] == {}
    assert get.call_args.kwargs["allow_redirects"] is False
    assert target.read_bytes() == b"image bytes"
    with pytest.raises(FileExistsError):
        media.download("https://replicate.delivery/a", target)
    assert target.read_bytes() == b"image bytes"


def test_video_credentials_never_reach_delivery_host(media, tmp_path):
    with pytest.raises(ValueError, match="Authenticated downloads"):
        media.download("https://replicate.delivery/a", tmp_path / "video", KEY)
    media.requests.get.assert_not_called()


@pytest.mark.parametrize("chunks", [(b"12345",), ()])
def test_download_size_limits(media, monkeypatch, tmp_path, chunks):
    monkeypatch.setattr(media.requests, "get", Mock(return_value=response(chunks=chunks)))
    with pytest.raises(ValueError, match="size limit|Empty"):
        media.download("https://replicate.delivery/a", tmp_path / "image", limit=4)


def test_ambiguous_submission_is_not_retried_and_records_are_redacted(media, monkeypatch):
    api = Mock(return_value={})
    monkeypatch.setattr(media, "api", api)
    updates = list(media.generate("image", media.IMAGE_MODELS[0], "A mug " + KEY, KEY))
    assert api.call_count == 1
    assert "no automatic resubmission" in updates[-1][2]
    record = next(media.OUTPUT.glob("*.json")).read_text()
    assert KEY not in record
    assert KEY not in json.dumps(updates)
    assert json.loads(record)["status"] == "error"
    assert not media.LOCK.locked()


def test_output_failure_does_not_submit_or_keep_lock(media, monkeypatch, tmp_path):
    target = tmp_path / "occupied"
    target.write_text("existing file")
    monkeypatch.setattr(media, "OUTPUT", target)
    api = Mock()
    monkeypatch.setattr(media, "api", api)
    updates = list(media.generate("image", media.IMAGE_MODELS[0], "A mug", KEY))
    api.assert_not_called()
    assert "FileExistsError" in updates[-1][2]
    assert not media.LOCK.locked()
