from __future__ import annotations

import json
import tempfile
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.auto_registration_service import RegistrationSettings
from services.registration_mailbox import RegistrationError
from services.registration_roxy import RoxyClient, RoxyLease, local_api_url


class Handler(BaseHTTPRequestHandler):
    requests = []
    endpoint = "127.0.0.1:9222"
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"code": 0, "data": {"rows": [{"id": 12, "workspaceName": "Fixture", "project_details": [{"projectId": 34, "projectName": "Test"}]}]}}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.requests.append((self.path, body, self.headers.get("token")))
        data = {"dirId": 56} if self.path == "/browser/create" else {"http": self.endpoint} if self.path == "/browser/open" else {}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"code": 0, "data": data}).encode())

    def log_message(self, *_):
        pass


class RoxyTests(unittest.TestCase):
    def setUp(self):
        Handler.requests.clear()
        Handler.endpoint = "127.0.0.1:9222"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = {"roxy_api_base": f"http://127.0.0.1:{self.server.server_port}", "roxy_api_token": "fixture-private-token", "roxy_workspace_id": "12", "roxy_project_id": "34"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_settings_keep_token_private_and_preserve_it(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = RegistrationSettings(Path(directory) / "settings.json")
            response = settings.save(registration_driver="roxy", **self.settings)
            self.assertNotIn("fixture-private-token", json.dumps(response))
            self.assertTrue(response["has_roxy_api_token"])
            settings.save(protocol="imap")
            self.assertEqual(settings.snapshot()["roxy_api_token"], "fixture-private-token")

    def test_remote_or_authenticated_api_urls_are_rejected(self):
        for url in ("https://example.com:50000", "http://token@localhost:50000", "http://localhost:50000/path", "http://localhost:50000?token=x"):
            with self.assertRaises(RegistrationError): local_api_url(url)

    def test_workspace_read_does_not_create_environment(self):
        data = RoxyClient(self.settings).workspaces()
        self.assertEqual(data["items"][0]["project_id"], "34")
        self.assertFalse(Handler.requests)

    def test_cdp_uses_created_profile_context_and_cleans_owned_profile(self):
        row = {}
        def update(**values): row.update(values)
        browser = mock.Mock(contexts=[object()])
        playwright = mock.Mock()
        playwright.chromium.connect_over_cdp.return_value = browser
        lease = RoxyLease(self.settings, row, update)
        connected, context = lease.connect(playwright, "socks5://user:private-password@localhost:1080")
        self.assertIs(context, browser.contexts[0])
        self.assertEqual(row["_roxy_profile"], "56")
        create = Handler.requests[0][1]
        self.assertEqual(create["workspaceId"], "12")
        self.assertEqual(create["proxyInfo"]["protocol"], "SOCKS5")
        self.assertEqual(create["proxyInfo"]["proxyPassword"], "private-password")
        self.assertFalse(Handler.requests[1][1]["headless"])
        playwright.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9222", timeout=20000)
        lease.close()
        self.assertIsNone(row["_roxy_profile"])
        self.assertEqual([item[0] for item in Handler.requests], ["/browser/create", "/browser/open", "/browser/close", "/browser/delete"])
        self.assertEqual(Handler.requests[-1][1], {"workspaceId": "12", "dirIds": ["56"], "isSoftDelete": False})

    def test_connect_failure_still_reclaims_new_profile(self):
        row = {}
        lease = RoxyLease(self.settings, row, lambda **values: row.update(values))
        playwright = mock.Mock()
        playwright.chromium.connect_over_cdp.side_effect = RuntimeError("fixture")
        with self.assertRaises(RuntimeError): lease.connect(playwright)
        lease.close()
        self.assertIsNone(row["_roxy_profile"])

    def test_foreign_workspace_never_deletes_existing_environment(self):
        row = {"_roxy_profile": "56", "_roxy_owner": {"base": self.settings["roxy_api_base"], "workspace": 99}}
        lease = RoxyLease(self.settings, row, lambda **values: row.update(values))
        with self.assertRaises(RegistrationError): lease.connect(mock.Mock())
        lease.close()
        self.assertFalse(Handler.requests)

    def test_failed_cleanup_preserves_profile_for_retry(self):
        row = {}
        lease = RoxyLease(self.settings, row, lambda **values: row.update(values))
        browser = mock.Mock(contexts=[object()])
        lease.connect(SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *a, **k: browser)))
        with mock.patch.object(lease.client, "cleanup", side_effect=RegistrationError("cleanup failure")):
            with self.assertRaises(RegistrationError): lease.close()
        self.assertEqual(row["_roxy_profile"], "56")

    def test_cleanup_waits_for_close_then_retries_same_owned_profile(self):
        client = RoxyClient(self.settings)
        with mock.patch.object(client, "request", side_effect=[{}, RegistrationError("still closing"), {}]) as request, mock.patch("services.registration_roxy.time.sleep") as pause:
            client.cleanup("owned-profile")
        self.assertEqual(request.call_args_list[1], request.call_args_list[2])
        self.assertEqual(request.call_args_list[1].args[1]["dirIds"], ["owned-profile"])
        pause.assert_called_once_with(2)

    def test_actual_chromium_cdp_attachment_preserves_context_cookie(self):
        from playwright.sync_api import sync_playwright
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        Handler.endpoint = f"127.0.0.1:{port}"
        row = {}
        with sync_playwright() as playwright:
            launched = playwright.chromium.launch(args=[f"--remote-debugging-port={port}"])
            lease = RoxyLease(self.settings, row, lambda **values: row.update(values))
            try:
                browser, context = lease.connect(playwright)
                context.add_cookies([{"name": "fixture", "value": "same-context", "url": self.settings["roxy_api_base"]}])
                page = context.new_page()
                page.goto(self.settings["roxy_api_base"])
                self.assertIn("same-context", page.evaluate("document.cookie"))
                self.assertIs(context, browser.contexts[0])
                lease.close()
                self.assertIsNone(row["_roxy_profile"])
            finally:
                launched.close()
