from __future__ import annotations

import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from services.image_upscale_service import ImageUpscaleError, upscale_image_bytes


def png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "blue").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.payload


class ImageUpscaleServiceTests(unittest.TestCase):
    def test_original_is_saved_and_exact_worker_result_is_returned(self):
        original = png((1024, 1024))
        enlarged = png((2048, 2048))
        with (
            mock.patch.dict(os.environ, {"IMAGE_UPSCALE_WORKER_URL": "http://upscale:8091"}),
            mock.patch("services.image_upscale_service.image_storage_service.save",
                       return_value=SimpleNamespace(rel="original.png", url="/images/original.png")) as save,
            mock.patch("services.image_upscale_service.urllib.request.urlopen",
                       return_value=FakeResponse(enlarged)) as call,
        ):
            output, source = upscale_image_bytes(original, "2048x2048")
        self.assertEqual(output, enlarged)
        self.assertEqual(source.path, "original.png")
        save.assert_called_once_with(original, None)
        self.assertEqual(call.call_args.args[0].headers["X-target-size"], "2048x2048")

    def test_mismatched_aspect_ratio_fails_without_worker_call(self):
        with (
            mock.patch("services.image_upscale_service.image_storage_service.save",
                       return_value=SimpleNamespace(rel="original.png", url="/images/original.png")),
            mock.patch("services.image_upscale_service.urllib.request.urlopen") as call,
        ):
            with self.assertRaises(ImageUpscaleError) as raised:
                upscale_image_bytes(png((1024, 1024)), "3840x2160")
        self.assertEqual(raised.exception.source.path, "original.png")
        call.assert_not_called()

    def test_worker_failure_keeps_original_reference(self):
        with (
            mock.patch.dict(os.environ, {"IMAGE_UPSCALE_WORKER_URL": ""}),
            mock.patch("services.image_upscale_service.image_storage_service.save",
                       return_value=SimpleNamespace(rel="original.png", url="/images/original.png")),
        ):
            with self.assertRaises(ImageUpscaleError) as raised:
                upscale_image_bytes(png((1024, 1024)), "2048x2048")
        self.assertEqual(raised.exception.source.url, "/images/original.png")
        self.assertEqual(raised.exception.status_code, 503)

    def test_source_is_reported_before_worker_failure(self):
        sources = []
        with (
            mock.patch.dict(os.environ, {"IMAGE_UPSCALE_WORKER_URL": ""}),
            mock.patch("services.image_upscale_service.image_storage_service.save",
                       return_value=SimpleNamespace(rel="original.png", url="/images/original.png")),
        ):
            with self.assertRaises(ImageUpscaleError):
                upscale_image_bytes(png((1024, 1024)), "2048x2048", source_callback=sources.append)
        self.assertEqual([(source.path, source.url) for source in sources],
                         [("original.png", "/images/original.png")])


if __name__ == "__main__":
    unittest.main()
