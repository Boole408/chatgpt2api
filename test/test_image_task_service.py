from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.image_task_service import ImageTaskService
from services.openai_backend_api import ImagePollTimeoutError
from services.image_upscale_service import UpscaleSource
from services.protocol.conversation import ImageGenerationError


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))

    def test_timeout_can_resume_after_reload_with_original_account(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"

            def timed_out(payload):
                payload["progress_callback"]("generating")
                payload["progress_callback"]("image_stream_resolve_start")
                exc = ImagePollTimeoutError("ChatGPT 生图超时", "conv-1")
                exc.access_token = "original-token"
                raise exc

            service = self.make_service(path, timed_out)
            service.submit_generation(OWNER, client_task_id="resume-me", prompt="cat", model="gpt-image-2",
                                      size=None, base_url="https://example.test")
            failed = wait_for_task(service, OWNER, "resume-me", "error")
            self.assertEqual(failed["conversation_id"], "conv-1")
            self.assertTrue(failed["resumable"])
            self.assertNotIn("access_token", failed)
            self.assertIn("generating", failed["stage_timings_ms"])
            saved = json.loads(path.read_text(encoding="utf-8"))["tasks"][0]
            self.assertEqual(saved["access_token"], "original-token")

            reloaded = self.make_service(path)
            backend = mock.MagicMock()
            backend._poll_image_results.return_value = (["file-1"], [])
            backend.resolve_conversation_image_urls.return_value = ["https://upstream.test/file-1"]
            backend.download_image_bytes.return_value = [b"image"]
            with mock.patch("services.account_service.account_service.get_account", return_value={"access_token": "rotated-token"}), \
                 mock.patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend) as backend_class, \
                 mock.patch("services.protocol.conversation.format_image_result", return_value={"data": [{"url": "https://example.test/images/result.png"}]}) as formatter:
                reloaded.resume_poll(OWNER, "resume-me", 30)
                recovered = wait_for_task(reloaded, OWNER, "resume-me", "success")

            backend_class.assert_called_once_with(access_token="rotated-token")
            backend._poll_image_results.assert_called_once_with("conv-1", 30)
            self.assertEqual(formatter.call_args.args[3], "https://example.test")
            self.assertEqual(recovered["data"][0]["url"], "https://example.test/images/result.png")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tasks"][0]["access_token"], "")

    def test_legacy_timeout_without_account_cannot_resume(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(json.dumps({"tasks": [{"id": "old", "owner_id": "owner-1", "status": "error",
                "error": "生图超时", "conversation_id": "conv-old"}]}), encoding="utf-8")
            service = self.make_service(path)
            self.assertFalse(service.list_tasks(OWNER, ["old"])["items"][0]["resumable"])
            with self.assertRaisesRegex(ValueError, "原任务未保存账号信息"):
                service.resume_poll(OWNER, "old")

    def test_upscale_failure_keeps_original_but_retry_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            generated = 0

            def failed_upscale(payload):
                nonlocal generated
                generated += 1
                payload["progress_callback"]("upscaling")
                payload["upscale_source_callback"](UpscaleSource("original.png", "https://example.test/original.png"))
                error = ImageGenerationError("超分服务失败", status_code=502, code="upscale_failed")
                error.original_path = "original.png"
                error.original_url = "https://example.test/original.png"
                raise error

            service = self.make_service(path, failed_upscale)
            service.submit_generation(OWNER, client_task_id="retry-upscale", prompt="cat", model="gpt-image-2",
                                      size="2k", base_url="https://example.test")
            failed = wait_for_task(service, OWNER, "retry-upscale", "error")
            self.assertFalse(failed["upscale_resumable"])
            reloaded = self.make_service(path, failed_upscale)
            with self.assertRaisesRegex(ValueError, "超分已关闭"):
                reloaded.retry_upscale(OWNER, "retry-upscale")
            self.assertEqual(generated, 1)
            self.assertEqual(next(iter(reloaded._tasks.values()))["original_path"], "original.png")

    def test_restart_during_upscale_preserves_retry_option(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(json.dumps({"tasks": [{"id": "interrupted", "owner_id": "owner-1",
                "status": "running", "mode": "generate", "model": "gpt-image-2", "size": "2k",
                "progress": "upscaling", "original_path": "original.png",
                "original_url": "https://example.test/original.png",
                "created_at": "2099-01-01 00:00:00", "updated_at": "2099-01-01 00:00:00"}]}), encoding="utf-8")
            task = self.make_service(path).list_tasks(OWNER, ["interrupted"])["items"][0]
            self.assertEqual(task["status"], "error")
            self.assertFalse(task["upscale_resumable"])


if __name__ == "__main__":
    unittest.main()
