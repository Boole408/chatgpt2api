"""Exercise the real browser driver against a local fixture, without external accounts."""
from __future__ import annotations

import threading
import json
import tempfile
from pathlib import Path
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse, parse_qs

from services.registration_browser import RegistrationBrowser, fill_profile, registration_session
from services.registration_mailbox import ManualRequired, RegistrationError


class Handler(BaseHTTPRequestHandler):
    paths = []
    manual_cleared = threading.Event()
    def do_GET(self):
        path = urlparse(self.path).path
        self.paths.append(path)
        if path == "/manual-status":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready" if self.manual_cleared.is_set() else b"waiting")
            return
        if path == "/api/auth/session":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            session = {"user": {"email": "target@example.com"}, "accessToken": "fixture-session"} if "fixture=authenticated" in self.headers.get("Cookie", "") else {}
            self.wfile.write(json.dumps(session).encode())
            return
        pages = {
            "/manual": '<h1>Verify you are human</h1><script>setInterval(async()=>{if(await (await fetch("/manual-status")).text()==="ready") location.href="/signup-chain"},200)</script>',
            "/signup-chain": '<form action="/verify-session"><label>Email<input type="email" name="email"></label><button type="submit">Continue</button></form>',
            "/verify-session": '<form action="/profile-session"><label>Code<input name="otp" autocomplete="one-time-code"></label><button type="submit">Verify</button></form>',
            "/profile-session": '<form action="/home"><label>Full name<input name="name"></label><label>Age<input name="age"></label><button type="submit">Continue</button></form>',
            "/email-choice": '<form action="/signup"><button type="submit">Continue with email</button></form>',
            "/script-blocked": '<p>Enable JavaScript and cookies to continue</p>',
            "/stalled-otp": '<form action="/stalled-otp"><input name="otp" autocomplete="one-time-code"><button type="submit">Verify</button></form>',
            "/resent": '<form action="/retry-code"><label>Code<input name="otp" autocomplete="one-time-code" required></label><button type="submit">Verify</button></form>',
            "/signup-session": '<form action="/home"><label>Email<input type="email" name="email"></label><button type="submit">Continue</button></form>',
            "/home": '<h1>ChatGPT</h1>',
            "/oauth": '<a href="/callback?code=fixture-code&state=fixture-state" id="proceed">Continue</a><script>location.href=document.getElementById("proceed").href</script>',
            "/split-code": '<form action="/profile">' + ''.join(f'<input name="digit{i}" inputmode="numeric" maxlength="1" required>' for i in range(6)) + '<button type="submit">Verify</button></form>',
            "/retry-code": '<form action="/retry-code"><label>Code<input name="otp" autocomplete="one-time-code" required></label><button type="submit">Verify</button></form>',
            "/signup": '<form action="/verify"><label>Email<input type="email" name="email" required></label><button type="submit">Continue</button></form>',
            "/email-transition": '<form action="/email-cleared"><input type="email" name="email" required><button type="submit">Continue</button></form>',
            "/email-cleared": '<form action="/verify"><input type="email" name="email" required><button type="submit">Continue</button></form>',
            "/stalled-email": '<form action="/stalled-email"><input type="email" name="email" required><button type="submit">Continue</button></form>',
            "/verify": '<form action="/profile"><label>Code<input name="code" autocomplete="one-time-code" required></label><button type="submit">Verify</button></form>',
            "/profile": '<form action="/callback"><label>Full name<input name="name" required></label><label>Age<input name="age" required></label><input type="hidden" name="code" value="fixture-code"><input type="hidden" name="state" value="fixture-state"><button type="submit">Continue</button></form>',
            "/password": '<form action="/verify"><label>Create password<input type="password" name="password" required></label><button type="submit">Continue</button></form><p>Create your password</p>',
            "/existing": '<label>Password<input type="password"></label><p>Sign in</p>',
            "/challenge": '<h1>Verify you are human</h1>',
            "/phone": '<label>Phone<input type="tel"></label>',
            "/birthday": '<label>Full name<input name="name"></label><label>Birthday<input type="date" name="birthday"></label>',
        }
        if path == "/retry-code" and parse_qs(urlparse(self.path).query).get("otp"):
            if parse_qs(urlparse(self.path).query)["otp"] == ["234567"]:
                self.send_response(302)
                self.send_header("Location", "/profile")
                self.end_headers()
                return
            else:
                pages[path] += '<p role="alert">Invalid code</p><form action="/resent"><button type="submit">Resend code</button></form>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if path == "/home":
            self.send_header("Set-Cookie", "fixture=authenticated; Path=/")
        self.end_headers()
        self.wfile.write(("<!doctype html><html><body>" + pages.get(path, "callback should be intercepted") + "</body></html>").encode())

    def log_message(self, *_args):
        pass


class BrowserDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def run_fixture(self, path, *, fast_clock=False, codes=None, authorize_path=None):
        row = {"email": "target@example.com", "name": "Oliver Smith", "age": 31, "birthday": "1995-10-03", "registered": False}
        mailbox = mock.Mock()
        mailbox.read.return_value = []
        mailbox.wait_code.return_value = "123456"
        if codes:
            mailbox.wait_code.side_effect = codes
        mailbox.sleep.side_effect = lambda _: None
        deadline = time.monotonic() + 20
        def check():
            if time.monotonic() > deadline:
                self.fail("Fixture did not finish within 20 seconds")
        updates = []
        def update(**values):
            row.update(values)
            updates.append(values)
        oauth = {"authorize_url": self.base + (authorize_path or path), "registration_url": self.base + path, "redirect_uri_prefix": self.base + "/callback"}
        clock = [0]
        if fast_clock:
            mailbox.sleep.side_effect = lambda amount: clock.__setitem__(0, clock[0] + amount)
        clock_patch = mock.patch("services.registration_browser.time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)) if fast_clock else mock.patch("services.registration_browser.browser_proxy", return_value=None)
        with clock_patch, mock.patch("services.registration_browser.browser_proxy", return_value=None):
            callback = RegistrationBrowser().run(oauth, row, {"registration_password": "fixture-password"}, mailbox, check, update, lambda _: None)
        return callback, row, mailbox, updates

    def test_email_otp_profile_and_callback_interception(self):
        callback, row, mailbox, updates = self.run_fixture("/signup")
        self.assertIn("code=fixture-code", callback)
        self.assertIn("state=fixture-state", callback)
        self.assertTrue(row["registered"])
        self.assertEqual(row["stage"], "exchanging")
        mailbox.wait_code.assert_called_once()
        self.assertTrue(any(update.get("stage") == "profile" for update in updates))

    def test_password_registration_branch(self):
        _, row, _, _ = self.run_fixture("/password")
        self.assertTrue(row["password_set"])

    def test_cleared_email_transition_is_refilled_after_wait(self):
        callback, row, _, updates = self.run_fixture("/email-transition", fast_clock=True)
        self.assertIn("code=fixture-code", callback)
        self.assertTrue(row["registered"])
        self.assertEqual(sum(u.get("stage") == "email" for u in updates), 2)

    def test_stalled_email_submissions_are_bounded(self):
        with self.assertRaisesRegex(RegistrationError, "3 次提交上限"):
            self.run_fixture("/stalled-email", fast_clock=True)

    def test_registration_session_then_oauth_in_same_context(self):
        callback, row, _, updates = self.run_fixture("/signup-session", authorize_path="/oauth")
        self.assertIn("code=fixture-code", callback)
        self.assertTrue(row["registered"])
        self.assertTrue(any(item.get("stage") == "opening_oauth" for item in updates))

    def test_registration_session_identity_mismatch_blocks_oauth(self):
        page = mock.Mock()
        page.evaluate.return_value = {"authenticated": True, "email": "other@example.com"}
        with self.assertRaisesRegex(RegistrationError, "邮箱不匹配"):
            registration_session(page, self.base, "target@example.com")

    def test_oauth_expiring_during_manual_wait_gets_new_session(self):
        clock = [0]
        mailbox = mock.Mock()
        mailbox.read.return_value = []
        mailbox.sleep.side_effect = lambda amount: clock.__setitem__(0, clock[0] + amount)
        calls = []
        def factory():
            calls.append(clock[0])
            return {"authorize_url": self.base + ("/challenge" if len(calls) == 1 else "/oauth?state=fixture-state"), "redirect_uri_prefix": self.base + "/callback"}
        def manual(reason, page):
            clock[0] += 700
        row = {"email": "target@example.com", "registered": True}
        with mock.patch("services.registration_browser.time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)), mock.patch("services.registration_browser.browser_proxy", return_value=None), mock.patch("services.registration_browser.interactive_available", return_value=True):
            callback = RegistrationBrowser().run(factory, row, {}, mailbox, lambda: None, lambda **kw: row.update(kw), lambda _: None, manual=manual)
        self.assertEqual(len(calls), 2)
        self.assertIn("state=fixture-state", callback)

    def test_unrelated_name_form_is_not_profile(self):
        page = mock.Mock(url=self.base + "/preferences")
        self.assertFalse(fill_profile(page, {"name": "fixture"}))
        page.get_by_label.assert_not_called()

    def test_otp_submit_without_navigation_is_not_success(self):
        with self.assertRaisesRegex(RegistrationError, "30 秒未继续"):
            self.run_fixture("/stalled-otp", fast_clock=True)

    def test_split_otp_inputs(self):
        _, row, mailbox, _ = self.run_fixture("/split-code")
        self.assertTrue(row["registered"])
        mailbox.wait_code.assert_called_once()

    def test_email_choice_opens_email_form(self):
        _, row, _, _ = self.run_fixture("/email-choice")
        self.assertTrue(row["registered"])

    def test_rejected_code_waits_and_reuses_original_deadline(self):
        Handler.paths.clear()
        _, row, mailbox, updates = self.run_fixture("/retry-code", fast_clock=True, codes=["123456", "234567"])
        self.assertTrue(row["registered"])
        self.assertEqual(mailbox.wait_code.call_count, 2)
        mailbox.sleep.assert_any_call(5)
        deadlines = [call.kwargs["deadline"] for call in mailbox.wait_code.call_args_list]
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertEqual(sum(item.get("stage") == "retrying_code" for item in updates), 1)
        self.assertIn("/resent", Handler.paths)

    def test_unknown_existing_password_needs_manual_handling(self):
        with self.assertRaisesRegex(ManualRequired, "未知密码"):
            self.run_fixture("/existing")

    def test_challenge_and_phone_require_manual_handling(self):
        with self.assertRaisesRegex(ManualRequired, "安全验证"):
            self.run_fixture("/challenge", fast_clock=True)
        with self.assertRaisesRegex(ManualRequired, "安全验证"):
            self.run_fixture("/script-blocked", fast_clock=True)
        with self.assertRaisesRegex(ManualRequired, "手机验证"):
            self.run_fixture("/phone")

    def test_profile_birthday(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(self.base + "/birthday")
                self.assertTrue(fill_profile(page, {"name": "Emma Wilson", "age": 31, "birthday": "1995-10-03"}))
                self.assertEqual(page.locator('input[type="date"]').input_value(), "1995-10-03")
            finally:
                browser.close()

    def test_challenge_pause_keeps_context_and_blocks_next_account(self):
        from services.auto_registration_service import AutoRegistrationService
        from services.registration_mailbox import MailboxClient
        Handler.manual_cleared.clear()
        with tempfile.TemporaryDirectory() as directory:
            service = AutoRegistrationService(Path(directory))
            service.save_settings(credential_line="central@example.com----password----client----refresh")
            settings = service.settings.snapshot()
            settings["_registration_url"] = self.base + "/manual"
            reached = threading.Event()
            starts = []

            def start(email):
                # OAuth must be created after registration, never before the challenge.
                self.assertTrue(service.jobs[0]["rows"][0]["registered"])
                starts.append(email)
                return {"session_id": "session", "authorize_url": self.base + "/oauth?state=fixture-state", "redirect_uri_prefix": self.base + "/callback"}

            driver = service.driver
            def run(factory, row, *args, **kwargs):
                if row["email"] != "target@example.com":
                    reached.set()
                    raise ManualRequired("fixture next account")
                return driver.run(factory, row, *args, **kwargs)

            with mock.patch.object(service.settings, "snapshot", return_value=settings), mock.patch.object(service, "_verify_email"), mock.patch.object(service, "importer", return_value={}), mock.patch("services.auto_registration_service.account_service.list_accounts", return_value=[]), mock.patch("services.auto_registration_service.oauth_login_service.start", side_effect=start), mock.patch("services.auto_registration_service.oauth_login_service.finish", return_value={"access_token": "fixture-token"}), mock.patch.object(service, "driver", SimpleNamespace(run=run)), mock.patch.object(MailboxClient, "read", return_value=[]), mock.patch.object(MailboxClient, "wait_code", return_value="123456") as otp, mock.patch("services.registration_browser.browser_proxy", return_value=None), mock.patch("services.registration_browser.interactive_available", return_value=True):
                job = service.create("target@example.com\nnext@example.com")
                self.addCleanup(service.shutdown)
                end = time.monotonic() + 25
                while time.monotonic() < end and service.get(job["id"])["status"] != "waiting_manual":
                    time.sleep(.1)
                paused = service.get(job["id"])
                self.assertEqual(paused["status"], "waiting_manual")
                self.assertEqual(paused["remaining"], 2)
                self.assertFalse(starts)
                self.assertFalse(reached.is_set())
                row_id = paused["rows"][0]["id"]
                service.manual_action(job["id"], row_id, "show")
                Handler.manual_cleared.set()
                service.manual_action(job["id"], row_id, "continue")
                service.thread.join(timeout=15)
                if service.thread.is_alive():
                    service.shutdown()
                    self.fail("Resumed original context did not complete")
                result = service.get(job["id"])
                self.assertEqual(result["rows"][0]["status"], "success")
                self.assertEqual(starts, ["target@example.com"])
                self.assertTrue(reached.is_set())
                otp.assert_called_once()
