from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.auto_registration_service import AutoRegistrationService, RegistrationSettings
from services.oauth_login_service import OAuthLoginService, OAuthLoginError
from services.registration_mailbox import (
    MailboxClient, ManualRequired, RegistrationError, RegistrationStopped,
    normalize_message, parse_emails, select_code,
)


def mail(identity="one", code="123456", recipient="target@example.com", received=None):
    item = {"id": identity, "receivedDateTime": received or "2099-01-01T00:00:00Z",
            "from": {"emailAddress": {"address": "noreply@tm.openai.com", "name": "OpenAI"}},
            "subject": "Your temporary OpenAI login code", "body": {"content": f"<p>Verification code: <b>{code}</b></p>"},
            "toRecipients": [{"emailAddress": {"address": recipient}}]}
    return normalize_message(item)


class ParsingAndMailboxTests(unittest.TestCase):
    def test_text_bom_deduplicates_and_reports_line_numbers(self):
        result = parse_emails("\ufeffTARGET@example.com\n\ntarget@example.com\nbad address")
        self.assertEqual(result["emails"], ["target@example.com"])
        self.assertEqual(result["duplicates"][0]["line"], 3)
        self.assertEqual(result["errors"][0]["line"], 4)

    def test_json_and_objects(self):
        self.assertEqual(parse_emails('["a@example.com", {"email":"b@example.com"}]')["emails"], ["a@example.com", "b@example.com"])
        self.assertEqual(parse_emails('{"emails":["a@example.com"]}')["emails"], ["a@example.com"])
        self.assertTrue(parse_emails("{bad}")["errors"])
        self.assertTrue(parse_emails('[null]')["errors"])

    def test_batch_limit(self):
        self.assertTrue(parse_emails("\n".join(f"a{i}@example.com" for i in range(501)))["errors"])

    def test_only_new_recent_unused_mail_is_selected(self):
        old, new = mail("old"), mail("new")
        result = select_code([old, new], "target@example.com", time.time(), {old["id"]}, set())
        self.assertEqual(result, ("123456", new["id"]))
        self.assertIsNone(select_code([new], "target@example.com", time.time(), set(), {new["id"]}))
        self.assertIsNone(select_code([mail(received="2000-01-01T00:00:00Z")], "target@example.com", time.time(), set(), set()))

    def test_exact_recipient_wins_and_unrelated_recipient_is_ignored(self):
        expected = mail("exact")
        shared = mail("shared", "234567", "central@outlook.com")
        self.assertEqual(select_code([expected, shared], "target@example.com", 1, set(), set()), ("123456", expected["id"]))
        self.assertIsNone(select_code([mail(recipient="other@example.com")], "target@example.com", 1, set(), set()))

    def test_ambiguous_mail_fails(self):
        with self.assertRaises(RegistrationError):
            select_code([mail("one"), mail("two", "234567")], "target@example.com", 1, set(), set())

    def test_newest_code_is_selected(self):
        old = mail("old", "123456", received="2099-01-01T00:00:00Z")
        newest = mail("new", "234567", received="2099-01-01T00:00:05Z")
        self.assertEqual(select_code([old, newest], "target@example.com", 1, set(), set()), ("234567", newest["id"]))

    def test_japanese_verification_mail_is_selected_with_same_filters(self):
        item = mail()
        item["subject"] = "ChatGPT 認証コード"
        item["text"] = "認証コード: 123456"
        self.assertEqual(select_code([item], "target@example.com", 1, set(), set()), ("123456", item["id"]))
        self.assertIsNone(select_code([item], "target@example.com", 1, {item["id"]}, set()))
        self.assertIsNone(select_code([item], "target@example.com", 1, set(), {item["id"]}))

    def test_cross_domain_delayed_mail_is_rejected_and_central_fallback_works(self):
        delayed = mail(recipient="other@another-domain.test")
        self.assertIsNone(select_code([delayed], "target@example.com", 1, set(), set(), "central@outlook.com"))
        shared = mail(recipient="central@outlook.com")
        self.assertIsNotNone(select_code([shared], "target@example.com", 1, set(), set(), "central@outlook.com"))

    def test_rejected_latest_code_does_not_fall_back_to_older_mail(self):
        old = mail("old", "111111", received="2099-01-01T00:00:00Z")
        latest = mail("latest", "222222", received="2099-01-01T00:00:05Z")
        next_mail = mail("next", "333333", received="2099-01-01T00:00:10Z")
        client = MailboxClient({}, lambda: None)
        baseline = set()
        with mock.patch.object(client, "read", side_effect=[[old, latest], [old, latest], [old, latest, next_mail]]), mock.patch.object(client, "sleep") as sleep:
            self.assertEqual(client.wait_code("target@example.com", 1, baseline, lambda _: None), "222222")
            self.assertEqual(client.wait_code("target@example.com", 1, baseline, lambda _: None), "333333")
        sleep.assert_called_once_with(5)

    def test_sender_and_multiple_codes(self):
        item = mail()
        item["sender"] = '"noreply@notopenai.com"'
        self.assertIsNone(select_code([item], "target@example.com", 1, set(), set()))
        item = mail()
        item["text"] += " 234567"
        self.assertIsNone(select_code([item], "target@example.com", 1, set(), set()))

    def test_request_matches_site_api_and_does_not_expose_body(self):
        settings = {"email": "central@outlook.com", "password": "mail-password", "client_id": "client", "refresh_token": "refresh-secret", "protocol": "imap"}
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "data": {"items": [{"id": "mail", "subject": "test", "body": "content"}]}}
        with mock.patch("services.registration_mailbox.requests.post", return_value=response) as post:
            result = MailboxClient(settings, lambda: None).read()
        payload = post.call_args.kwargs["json"]
        self.assertTrue(payload["include_bodies"])
        self.assertEqual(payload["mail_protocol"], "imap")
        self.assertEqual(len(result), 1)

    def test_network_retries_are_bounded(self):
        from curl_cffi import requests
        settings = dict(email="central@outlook.com", password="", client_id="client", refresh_token="token")
        client = MailboxClient(settings, lambda: None)
        with mock.patch("services.registration_mailbox.requests.post", side_effect=requests.RequestsError("secret-data")) as post, mock.patch.object(client, "sleep"):
            with self.assertRaisesRegex(RegistrationError, "网络失败") as failure:
                client.read()
        self.assertNotIn("secret-data", str(failure.exception))
        self.assertEqual(post.call_count, 3)

    def test_poll_cancel_and_timeout(self):
        client = MailboxClient({}, lambda: None)
        with mock.patch.object(client, "check", side_effect=RegistrationStopped("stop")):
            with self.assertRaises(RegistrationStopped):
                client.wait_code("target@example.com", 1, set(), lambda _: None)
        with mock.patch("services.registration_mailbox.time.monotonic", side_effect=[0, 181]):
            with self.assertRaisesRegex(RegistrationError, "180"):
                client.wait_code("target@example.com", 1, set(), lambda _: None)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.driver = mock.Mock()
        self.importer = mock.Mock(return_value={})
        def adapter(factory, row, settings, mailbox, check, update, consume, **kwargs):
            return self.driver.run(factory(), row, settings, mailbox, check, update, consume)
        from types import SimpleNamespace
        self.service = AutoRegistrationService(Path(self.temporary.name), driver=SimpleNamespace(run=adapter), importer=self.importer)
        self.service.save_settings(credential_line="central@outlook.com----password----client----secret", registration_password="fixed-password")
        self.patch_accounts = mock.patch("services.auto_registration_service.account_service.list_accounts", return_value=[])
        self.patch_accounts.start()
        self.addCleanup(self.patch_accounts.stop)
        self.start = mock.patch("services.auto_registration_service.oauth_login_service.start", return_value={"session_id": "session", "authorize_url": "url"}).start()
        self.finish = mock.patch("services.auto_registration_service.oauth_login_service.finish", return_value={"access_token": "access", "refresh_token": "refresh", "id_token": "id"}).start()
        self.verify = mock.patch.object(self.service, "_verify_email").start()
        self.addCleanup(mock.patch.stopall)
        self.driver.run.return_value = "https://platform.openai.com/auth/callback?code=c&state=s"

    def create(self, content="target@example.com"):
        return self.service.create(content, launch=False)

    def run_job(self, job):
        self.service._run(job["id"], threading.Event())
        return self.service.get(job["id"])

    def test_settings_are_private_and_replacement_preserves_password(self):
        public = self.service.settings.public()
        self.assertNotIn("password", public)
        self.assertNotIn("refresh_token", public)
        self.assertTrue(public["has_registration_password"])
        self.assertEqual(self.service.settings.path.stat().st_mode & 0o777, 0o600)
        self.service.save_settings(protocol="graph")
        self.assertEqual(self.service.settings.snapshot()["registration_password"], "fixed-password")

    def test_serial_success_checkpoints_and_warning(self):
        self.importer.return_value = {"warning": "refresh failed"}
        def driver(oauth, row, settings, mailbox, check, update, consume):
            update(registered=True)
            return "callback"
        self.driver.run.side_effect = driver
        result = self.run_job(self.create("a@example.com\nb@example.com"))
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(row["imported"] and row["authorized"] and row["registered"] for row in result["rows"]))
        self.assertEqual(result["rows"][0]["warning"], "refresh failed")
        self.assertEqual(self.importer.call_count, 2)
        public = json.dumps(result)
        self.assertNotIn("fixed-password", public)
        self.assertNotIn("birthday", public)

    def test_identity_mismatch_never_imports(self):
        self.verify.side_effect = RegistrationError("wrong email")
        result = self.run_job(self.create())
        self.assertEqual(result["failed"], 1)
        self.importer.assert_not_called()

    def test_manual_required_continues_next_email(self):
        self.driver.run.side_effect = [ManualRequired("captcha"), "callback"]
        result = self.run_job(self.create("a@example.com\nb@example.com"))
        self.assertEqual(result["rows"][0]["status"], "manual_required")
        self.assertEqual(result["success"], 1)

    def test_stop_cancels_remaining_rows(self):
        job = self.create("a@example.com\nb@example.com")
        stop = threading.Event()
        stop.set()
        self.service._run(job["id"], stop)
        result = self.service.get(job["id"])
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(all(row["status"] == "cancelled" for row in result["rows"]))
        self.driver.run.assert_not_called()

    def test_retry_preserves_profile_and_registered_flag(self):
        def failure(oauth, row, settings, mailbox, check, update, consume):
            update(registered=True)
            raise RegistrationError("failed OAuth")
        self.driver.run.side_effect = failure
        original = self.run_job(self.create())
        retried = self.service.retry(original["id"], launch=False)
        self.assertEqual(retried["rows"][0]["name"], original["rows"][0]["name"])
        self.assertTrue(retried["rows"][0]["registered"])
        self.assertEqual(retried["rows"][0]["status"], "queued")

    def test_restart_recovers_unfinished_and_preserves_used_ids(self):
        job = self.create()
        self.service.used_ids.add("used")
        self.service._save()
        restored = AutoRegistrationService(Path(self.temporary.name))
        self.assertEqual(restored.get(job["id"])["rows"][0]["status"], "interrupted")
        self.assertEqual(restored.used_ids, {"used"})

    def test_saved_authorization_retries_import_without_repeating_oauth(self):
        self.importer.side_effect = [RegistrationError("temporary import failure"), {}]
        first = self.run_job(self.create())
        self.assertTrue(first["rows"][0]["authorized"])
        self.assertNotIn("access", json.dumps(first))
        retry = self.service.retry(first["id"], launch=False)
        result = self.run_job(retry)
        self.assertEqual(result["success"], 1)
        self.assertEqual(self.finish.call_count, 1)
        self.assertEqual(self.driver.run.call_count, 1)

    def test_manual_wait_stops_and_restart_marks_interrupted(self):
        from types import SimpleNamespace
        page = mock.Mock()
        page.wait_for_timeout.side_effect = lambda _: time.sleep(.02)
        def driver(factory, row, settings, mailbox, check, update, consume, **kwargs):
            kwargs["manual"]("fixture manual", page)
            self.fail("Cancelled manual wait resumed")
        self.service.driver = SimpleNamespace(run=driver)
        job = self.service.create("target@example.com\nnext@example.com")
        self.addCleanup(self.service.shutdown)
        end = time.monotonic() + 3
        while time.monotonic() < end and self.service.get(job["id"])["status"] != "waiting_manual":
            time.sleep(.02)
        self.assertEqual(self.service.get(job["id"])["status"], "waiting_manual")
        restored = AutoRegistrationService(Path(self.temporary.name))
        self.assertEqual(restored.get(job["id"])["rows"][0]["status"], "interrupted")
        self.service.stop(job["id"])
        self.service.thread.join(3)
        result = self.service.get(job["id"])
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(all(row["status"] == "cancelled" for row in result["rows"]))
        self.assertFalse(self.service.controls)

    def test_manual_timeout_releases_slot(self):
        from types import SimpleNamespace
        page = mock.Mock()
        page.wait_for_timeout.side_effect = lambda _: time.sleep(.02)
        self.service.manual_timeout_seconds = .01
        def driver(factory, row, settings, mailbox, check, update, consume, **kwargs):
            kwargs["manual"]("fixture manual", page)
        self.service.driver = SimpleNamespace(run=driver)
        result = self.run_job(self.create())
        self.assertEqual(result["rows"][0]["status"], "manual_required")
        self.assertFalse(self.service.controls)

    def test_settings_and_new_job_are_locked_during_running_job(self):
        self.create()
        with self.assertRaises(RegistrationError):
            self.service.save_settings(protocol="graph")
        with self.assertRaises(RegistrationError):
            self.create("other@example.com")

    def test_existing_pool_email_is_skipped(self):
        with mock.patch("services.auto_registration_service.account_service.list_accounts", return_value=[{"email": "target@example.com"}]):
            job = self.create()
        result = self.run_job(job)
        self.assertEqual(result["rows"][0]["status"], "skipped")
        self.driver.run.assert_not_called()

    def test_raw_exception_is_not_exposed(self):
        self.driver.run.side_effect = RuntimeError("refresh-secret https://callback?code=secret")
        result = self.run_job(self.create())
        self.assertNotIn("refresh-secret", json.dumps(result))
        self.assertNotIn("code=secret", self.service.path.read_text())

    def test_oauth_http_status_is_visible_without_raw_response(self):
        self.finish.side_effect = OAuthLoginError("OpenAI 拒绝换 token (HTTP 403): refresh-secret code=secret")
        result = self.run_job(self.create())
        self.assertIn("HTTP 403", result["rows"][0]["error"])
        self.assertNotIn("refresh-secret", json.dumps(result))
        self.assertNotIn("code=secret", self.service.path.read_text())
        self.importer.assert_not_called()


class OAuthIdentityTests(unittest.TestCase):
    def test_identity_uses_authenticated_oidc_endpoint(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"email": "target@example.com", "email_verified": True}
        with mock.patch("services.auto_registration_service.requests.get", return_value=response) as get:
            AutoRegistrationService._verify_email("fixture-token", "target@example.com")
        self.assertEqual(get.call_args.args[0], "https://auth.openai.com/api/accounts/oauth/userinfo")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fixture-token")

    def test_identity_mismatch_remains_blocked(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"email": "other@example.com"}
        with mock.patch("services.auto_registration_service.requests.get", return_value=response), self.assertRaisesRegex(RegistrationError, "邮箱不匹配"):
            AutoRegistrationService._verify_email("fixture-token", "target@example.com")

    def test_unavailable_identity_is_distinct_from_mismatch(self):
        response = mock.Mock(status_code=403)
        with mock.patch("services.auto_registration_service.requests.get", return_value=response), self.assertRaisesRegex(RegistrationError, "身份接口返回 HTTP 403"):
            AutoRegistrationService._verify_email("fixture-token", "target@example.com")


class OAuthStateTests(unittest.TestCase):
    def test_mismatched_state_never_exchanges(self):
        service = OAuthLoginService()
        session = service.start()
        with mock.patch.object(service, "_exchange_code") as exchange:
            with self.assertRaises(OAuthLoginError):
                service.finish(session["session_id"], "https://platform.openai.com/auth/callback?code=code&state=wrong")
        exchange.assert_not_called()


class RegistrationApiTests(unittest.TestCase):
    def setUp(self):
        import api.registration as module
        self.module = module
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = AutoRegistrationService(Path(self.temp.name))
        patcher = mock.patch.object(module, "auto_registration_service", self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(module.create_router())
        self.client = TestClient(app)

    def test_all_routes_require_admin(self):
        paths = [("GET", "/settings"), ("POST", "/settings"), ("POST", "/test-mailbox"), ("POST", "/test-roxy"), ("POST", "/preview"),
                 ("GET", "/jobs"), ("POST", "/jobs"), ("GET", "/jobs/id"), ("POST", "/jobs/id/stop"), ("POST", "/jobs/id/retry"), ("POST", "/jobs/id/rows/row/manual"), ("GET", "/jobs/id/rows/row/diagnostic-image")]
        with mock.patch.object(self.module, "require_admin", side_effect=HTTPException(status_code=403)):
            for method, path in paths:
                response = self.client.request(method, "/api/accounts/registration" + path, json={"content": "a@example.com", "action": "continue"})
                self.assertEqual(response.status_code, 403, path)

    def test_preview_and_private_settings(self):
        with mock.patch.object(self.module, "require_admin", return_value={"role": "admin"}):
            response = self.client.post("/api/accounts/registration/preview", json={"content": "a@example.com\nbad"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["errors"][0]["line"], 2)
            response = self.client.post("/api/accounts/registration/settings", json={"credential_line": "central@outlook.com----password----client----secret", "registration_password": "fixed-password"})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("fixed-password", response.text)
            self.assertNotIn("secret", response.text)
