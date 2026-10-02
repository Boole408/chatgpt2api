from __future__ import annotations

import base64
import time
import unittest
from io import BytesIO
from unittest import mock

from PIL import Image
from fastapi import HTTPException

from services.protocol import openai_v1_image_edit, openai_v1_image_generations
from services.openai_backend_api import ChatRequirements, ImagePollTimeoutError, OpenAIBackendAPI
from services.image_upscale_service import ImageUpscaleError, UpscaleSource
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    ImageOutput,
    _image_poll_budget,
    format_request_image_result,
    normalize_codex_image_size,
    stream_codex_image_outputs,
    stream_image_outputs_with_pool,
)
from utils.helper import image_upscale_target, route_image_model_for_size


class ImageHighResolutionRoutingTests(unittest.TestCase):
    def test_25_is_sent_to_upstream_in_prepare_and_generation(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://example.test"
        backend.session = mock.Mock()
        backend._image_headers = mock.Mock(return_value={})
        backend.session.post.return_value.status_code = 200
        backend.session.post.return_value.json.return_value = {"conduit_token": "conduit"}
        requirements = ChatRequirements(token="requirements")

        backend._prepare_image_conversation("a cat", requirements, "gpt-image-2.5")
        self.assertEqual(backend.session.post.call_args.kwargs["json"]["model"], "gpt-image-2.5")
        backend._start_image_generation("a cat", requirements, "conduit", "gpt-image-2.5")
        self.assertEqual(backend.session.post.call_args.kwargs["json"]["model"], "gpt-image-2.5")

    def test_1k_result_keeps_original_base64_unchanged(self):
        original = base64.b64encode(b"original-image").decode("ascii")
        with mock.patch("services.protocol.conversation.save_image_bytes", return_value="/images/original") as save, \
             mock.patch("services.protocol.conversation.upscale_image_bytes") as upscale:
            result = format_request_image_result(
                [{"b64_json": original}], ConversationRequest(model="gpt-image-2", size="1024x1024"), 1)
        self.assertEqual(result["data"][0]["b64_json"], original)
        save.assert_called_once_with(b"original-image", None)
        upscale.assert_not_called()

    def test_multiple_upscaled_images_keep_order_and_response_format(self):
        originals = [base64.b64encode(value).decode("ascii") for value in (b"first", b"second")]
        outputs = [b"upscaled-first", b"upscaled-second"]
        source = UpscaleSource("original.png", "/images/original.png")
        with mock.patch("services.protocol.conversation.upscale_image_bytes",
                        side_effect=[(output, source) for output in outputs]) as upscale, \
             mock.patch("services.protocol.conversation.save_image_bytes",
                        side_effect=["/images/first.png", "/images/second.png"]):
            result = format_request_image_result(
                [{"b64_json": value} for value in originals],
                ConversationRequest(model="gpt-image-2", upscale_target="2048x2048", response_format="url"), 1)
        self.assertEqual([item["url"] for item in result["data"]],
                         ["/images/first.png", "/images/second.png"])
        self.assertTrue(all("b64_json" not in item for item in result["data"]))
        self.assertEqual([call.args[0] for call in upscale.call_args_list], [b"first", b"second"])

    def test_upscale_failure_never_returns_original_as_high_resolution(self):
        source = UpscaleSource("original.png", "/images/original.png")
        with mock.patch("services.protocol.conversation.upscale_image_bytes",
                        side_effect=ImageUpscaleError("超分失败", source=source)):
            with self.assertRaises(ImageGenerationError) as raised:
                format_request_image_result(
                    [{"b64_json": base64.b64encode(b"original").decode("ascii")}],
                    ConversationRequest(model="gpt-image-2", upscale_target="2048x2048"), 1)
        self.assertEqual(raised.exception.code, "upscale_failed")
        self.assertEqual(raised.exception.original_path, "original.png")
        self.assertEqual(raised.exception.original_url, "/images/original.png")

    def test_poll_budget_is_shared_and_preserves_conversation_id(self):
        request = ConversationRequest(deadline=time.monotonic() + 0.2)
        self.assertLessEqual(_image_poll_budget(request, 300, "conv-1"), 0.2)
        request.deadline = time.monotonic() - 1
        with self.assertRaises(ImagePollTimeoutError) as raised:
            _image_poll_budget(request, 300, "conv-1")
        self.assertEqual(raised.exception.conversation_id, "conv-1")

    def test_regular_model_keeps_1k_sizes(self):
        self.assertEqual(
            route_image_model_for_size("gpt-image-2", "1920x1088"),
            ("gpt-image-2", "1920x1088"),
        )

    def test_regular_models_reject_2k_and_4k_when_upscale_is_disabled(self):
        for model in ("gpt-image-2", "gpt-image-2.5"):
            for size in ("2k", "4k", "2048x1536", "3840x2160"):
                with self.subTest(model=model, size=size), self.assertRaises(HTTPException) as raised:
                    route_image_model_for_size(model, size)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertIsNone(image_upscale_target(model, size))

    def test_25_edit_uses_original_image_without_upscale(self):
        captured = []

        def fake_stream(request):
            captured.append(request)
            return iter(())

        with mock.patch.object(openai_v1_image_edit, "stream_image_outputs_with_pool", fake_stream):
            openai_v1_image_edit.handle({
                "model": "gpt-image-2.5", "prompt": "enhance", "size": "1024x1536",
                "images": [(b"image", "reference.png", "image/png")], "stream": True,
            })
        self.assertEqual(captured[0].model, "gpt-image-2.5")
        self.assertEqual(captured[0].size, "1024x1536")
        self.assertIsNone(captured[0].upscale_target)

    def test_custom_16_9_2k_is_rejected_without_codex(self):
        with self.assertRaises(HTTPException):
            route_image_model_for_size("gpt-image-2", "2048x1152")

    def test_unavailable_high_resolution_fails_instead_of_routing_to_codex(self):
        with self.assertRaises(HTTPException) as raised:
            route_image_model_for_size("gpt-image-2", "4096x4096")
        self.assertEqual(raised.exception.status_code, 400)

    def test_resolution_aliases_are_normalized(self):
        self.assertEqual(route_image_model_for_size("gpt-image-2.5", "1K"),
                         ("gpt-image-2.5", "1024x1024"))
        self.assertEqual(route_image_model_for_size("codex-gpt-image-2", "4k"),
                         ("codex-gpt-image-2", "3840x2160"))

    def test_existing_codex_model_is_not_rewritten(self):
        self.assertEqual(
            route_image_model_for_size("plus-codex-gpt-image-2", "3840x2160"),
            ("plus-codex-gpt-image-2", "3840x2160"),
        )

    def test_generations_handler_uses_25_without_upscale(self):
        captured = []

        def fake_stream(request):
            captured.append(request)
            return iter(())

        with mock.patch.object(openai_v1_image_generations, "stream_image_outputs_with_pool", fake_stream):
            openai_v1_image_generations.handle({
                "model": "gpt-image-2.5",
                "prompt": "poster",
                "size": "1024x1024",
            })

        self.assertEqual(captured[0].model, "gpt-image-2.5")
        self.assertEqual(captured[0].size, "1024x1024")
        self.assertIsNone(captured[0].upscale_target)

    def test_high_resolution_request_is_rejected_without_paid_codex_account(self):
        request = ConversationRequest(
            model="codex-gpt-image-2",
            prompt="high resolution poster",
            size="3840x2160",
        )

        with mock.patch(
            "services.protocol.conversation.account_service.has_ready_image_account",
            return_value=False,
        ):
            with self.assertRaises(ImageGenerationError) as raised:
                list(stream_image_outputs_with_pool(request))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.code, "codex_image_account_unavailable")
        self.assertEqual(raised.exception.param, "size")

    def test_high_resolution_request_runs_when_paid_codex_account_exists(self):
        result = {"b64_json": "aW1hZ2U=", "url": "/images/result.png"}
        request = ConversationRequest(
            model="codex-gpt-image-2",
            prompt="high resolution poster",
            size="3840x2160",
        )

        with mock.patch(
            "services.protocol.conversation.account_service.has_ready_image_account",
            return_value=True,
        ), mock.patch(
            "services.protocol.conversation._generate_single_image",
            return_value=[ImageOutput(kind="result", model=request.model, index=1, total=1, data=[result])],
        ):
            outputs = list(stream_image_outputs_with_pool(request))

        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(outputs[0].data, [result])

    def test_poll_timeout_keeps_conversation_for_resume_instead_of_regenerating(self):
        request = ConversationRequest(model="gpt-image-2", prompt="poster")

        def fake_stream(_backend, _request, index, total):
            if fake_stream.calls == 0:
                fake_stream.calls += 1
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text="working",
                    conversation_id="conversation-timeout",
                )
                raise ImagePollTimeoutError("polling timed out", "conversation-timeout")
            yield ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"b64_json": "aW1hZ2U="}],
                conversation_id="conversation-success",
            )

        fake_stream.calls = 0
        with mock.patch(
            "services.protocol.conversation.account_service.get_available_access_token",
            side_effect=["token-timeout", "token-success"],
        ) as get_token, mock.patch(
            "services.protocol.conversation.account_service.get_account",
            side_effect=lambda token: {"email": f"{token}@example.com"},
        ), mock.patch(
            "services.protocol.conversation.account_service.mark_image_result",
        ) as mark_result, mock.patch(
            "services.protocol.conversation.OpenAIBackendAPI",
        ), mock.patch(
            "services.protocol.conversation.stream_image_outputs",
            side_effect=fake_stream,
        ), mock.patch(
            "services.protocol.conversation._remove_image_conversation_later",
        ) as remove_conversation:
            with self.assertRaises(ImagePollTimeoutError) as raised:
                list(stream_image_outputs_with_pool(request))

        self.assertEqual(get_token.call_count, 1)
        self.assertEqual(raised.exception.conversation_id, "conversation-timeout")
        self.assertEqual(raised.exception.access_token, "token-timeout")
        remove_conversation.assert_not_called()
        mark_result.assert_any_call("token-timeout", False)

    def test_codex_result_is_normalized_to_exact_requested_size(self):
        source = BytesIO()
        Image.new("RGB", (917, 1716), "red").save(source, format="PNG")

        normalized = normalize_codex_image_size(
            base64.b64encode(source.getvalue()).decode("ascii"),
            "1152x2048",
        )

        with Image.open(BytesIO(base64.b64decode(normalized))) as image:
            self.assertEqual(image.size, (1152, 2048))

    def test_codex_result_with_exact_size_is_not_reencoded(self):
        source = BytesIO()
        Image.new("RGB", (64, 32), "blue").save(source, format="PNG")
        encoded = base64.b64encode(source.getvalue()).decode("ascii")

        self.assertEqual(normalize_codex_image_size(encoded, "64x32"), encoded)

    def test_codex_result_applies_exif_orientation_before_size_check(self):
        source = BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (32, 64), "green").save(source, format="JPEG", exif=exif)

        normalized = normalize_codex_image_size(
            base64.b64encode(source.getvalue()).decode("ascii"),
            "64x32",
        )

        with Image.open(BytesIO(base64.b64decode(normalized))) as image:
            self.assertEqual(image.size, (64, 32))
            self.assertEqual(image.format, "PNG")

    def test_codex_stream_returns_png_format_and_exact_pixels(self):
        source = BytesIO()
        Image.new("RGB", (46, 86), "purple").save(source, format="PNG")
        encoded = base64.b64encode(source.getvalue()).decode("ascii")

        class FakeBackend:
            def iter_codex_image_response_events(self, **_kwargs):
                return iter([{"type": "image_generation_call", "result": encoded}])

        outputs = list(stream_codex_image_outputs(
            FakeBackend(),
            ConversationRequest(prompt="poster", size="64x128", response_format="b64_json"),
        ))

        self.assertEqual(outputs[0].data[0]["output_format"], "png")
        with Image.open(BytesIO(base64.b64decode(outputs[0].data[0]["b64_json"]))) as image:
            self.assertEqual(image.size, (64, 128))

    def test_large_ratio_change_preserves_full_foreground_on_extended_canvas(self):
        source_image = Image.new("RGB", (100, 100), "blue")
        for x in range(100):
            for y in range(100):
                if x < 10 or x >= 90 or y < 10 or y >= 90:
                    source_image.putpixel((x, y), (255, 0, 0))
        source = BytesIO()
        source_image.save(source, format="PNG")

        normalized = normalize_codex_image_size(
            base64.b64encode(source.getvalue()).decode("ascii"),
            "64x128",
        )

        with Image.open(BytesIO(base64.b64decode(normalized))).convert("RGB") as image:
            self.assertEqual(image.size, (64, 128))
            # The red left/right edge remains in the centered foreground. A
            # destructive 1:2 center crop would have removed both edges.
            red, green, blue = image.getpixel((0, 64))
            self.assertGreater(red, 200)
            self.assertLess(green, 40)
            self.assertLess(blue, 40)


if __name__ == "__main__":
    unittest.main()
