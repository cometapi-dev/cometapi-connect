"""Offline tests; run with WebUI's Python and A1111_ROOT pointing to its checkout."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ["A1111_ROOT"])
spec = importlib.util.spec_from_file_location(
    "cometapi_media", Path(__file__).parent / "scripts/cometapi_media.py"
)
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)
KEY = "offline-test-key"


class ExtensionTests(unittest.TestCase):
    def test_both_observed_image_response_shapes(self):
        url = "https://replicate.delivery/example/output.webp"
        self.assertEqual(media.output_urls(url), url)
        self.assertEqual(media.output_urls([url]), url)

    def test_delivery_url_rejects_credential_leaks_and_other_hosts(self):
        for value in [
            "http://replicate.delivery/a",
            "https://replicate.delivery.evil.test/a",
            "https://user:secret@replicate.delivery/a",
            "https://127.0.0.1/a",
            "https://replicate.delivery:8443/a",
            [],
            ["a", "b"],
            {"url": "a"},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                media.output_urls(value)

    def test_authenticated_api_does_not_follow_redirects(self):
        response = MagicMock(status_code=302)
        response.__enter__.return_value = response
        with patch.object(media.requests, "request", return_value=response) as request:
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                media.api("/v1/videos/test", KEY)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(request.call_args.args[1], "https://api.cometapi.com/v1/videos/test")

    def test_image_download_has_no_authorization(self):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"image bytes"]
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(media.requests, "get", return_value=response) as get,
        ):
            media.download("https://replicate.delivery/a", Path(folder) / "image")
        self.assertEqual(get.call_args.kwargs["headers"], {})
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_video_key_cannot_be_sent_to_delivery_host(self):
        with patch.object(media.requests, "get") as get, self.assertRaises(ValueError):
            media.download("https://replicate.delivery/a", Path("/unused"), KEY)
        get.assert_not_called()

    def test_ambiguous_submission_never_retries_and_redacts_record(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(media, "OUTPUT", Path(folder)),
            patch.object(media, "api", return_value={}) as api,
        ):
            updates = list(media.generate("image", media.IMAGE_MODELS[0], "A mug " + KEY, KEY))
            self.assertEqual(api.call_count, 1)
            self.assertIn("no automatic resubmission", updates[-1][2])
            record = next(Path(folder).glob("*.json")).read_text()
            self.assertNotIn(KEY, record)
            self.assertEqual(json.loads(record)["status"], "error")
            self.assertFalse(media.LOCK.locked())

    def test_bad_output_directory_releases_lock_without_submitting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "not-a-directory"
            path.write_text("existing file")
            with patch.object(media, "OUTPUT", path), patch.object(media, "api") as api:
                updates = list(media.generate("image", media.IMAGE_MODELS[0], "A mug", KEY))
            api.assert_not_called()
            self.assertIn("FileExistsError", updates[-1][2])
            self.assertFalse(media.LOCK.locked())


if __name__ == "__main__":
    unittest.main()
