import base64
import json
import unittest
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest import mock

from services.account_service import AccountService


class MemoryStorage:
    def __init__(self, accounts: list[dict[str, Any]] | None = None) -> None:
        self.accounts = list(accounts or [])

    def load_accounts(self) -> list[dict[str, Any]]:
        return list(self.accounts)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = list(accounts)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        return []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        pass

    def health_check(self) -> dict[str, Any]:
        return {"ok": True}

    def get_backend_info(self) -> dict[str, Any]:
        return {"type": "memory"}


def make_jwt(payload: dict[str, Any]) -> str:
    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f'{encode({"alg": "none", "typ": "JWT"})}.{encode(payload)}.sig'


class AccountExportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        counter = mock.patch.object(AccountService, "_get_cumulative_file", return_value=Path(temporary.name) / "counter")
        counter.start()
        self.addCleanup(counter.stop)
        logs = mock.patch("services.account_service.log_service.add")
        logs.start()
        self.addCleanup(logs.stop)

    def test_export_import_round_trip_preserves_source_type(self) -> None:
        for source in ["oauth_login", "web", "codex", "password"]:
            with self.subTest(source=source):
                token = make_jwt({"email": "roundtrip@example.com"})
                original = AccountService(MemoryStorage([{
                    "access_token": token, "refresh_token": "fixture-refresh",
                    "id_token": make_jwt({"email": "roundtrip@example.com"}),
                    "source_type": source, "type": "plus",
                }]))
                exported = original.build_export_items()
                self.assertEqual(exported[0]["source_type"], source)
                restored = AccountService(MemoryStorage())
                restored.add_account_items(json.loads(json.dumps(exported)))
                self.assertEqual(restored.get_account(token)["source_type"], source)
                self.assertEqual(restored.get_account(token)["refresh_token"], "fixture-refresh")
                reloaded = AccountService(restored.storage)
                self.assertEqual(reloaded.get_account(token)["source_type"], source)

    def test_imported_oauth_account_automatically_refreshes_and_persists_rotation(self) -> None:
        from services.openai_oauth import platform_oauth_client_id

        old_token = make_jwt({"email": "refresh@example.com", "exp": 1, "iat": 1})
        original = AccountService(MemoryStorage([{
            "access_token": old_token, "refresh_token": "fixture-refresh",
            "id_token": make_jwt({"email": "refresh@example.com"}),
            "source_type": "oauth_login",
        }]))
        imported = AccountService(MemoryStorage())
        imported.add_account_items(json.loads(json.dumps(original.build_export_items())))
        self.assertIn(old_token, imported.list_expiring_access_tokens())
        self.assertIn(old_token, imported.list_refresh_token_keepalive_tokens())
        new_token = make_jwt({"email": "refresh@example.com", "exp": int(time.time()) + 7 * 86400, "iat": int(time.time())})
        response = mock.Mock(status_code=200, text="fixture response")
        response.json.return_value = {
            "access_token": new_token, "refresh_token": "fixture-rotated-refresh",
            "id_token": make_jwt({"email": "refresh@example.com", "iat": int(time.time())}),
        }
        with mock.patch("curl_cffi.requests.Session") as factory:
            factory.return_value.post.return_value = response
            rotated = imported.refresh_access_token(old_token)
            request_data = factory.return_value.post.call_args.kwargs["data"]
            self.assertEqual(request_data["refresh_token"], "fixture-refresh")
            self.assertEqual(request_data["client_id"], platform_oauth_client_id)
        self.assertEqual(rotated, new_token)
        self.assertEqual(imported.resolve_access_token(old_token), new_token)
        reloaded = AccountService(imported.storage)
        account = reloaded.get_account(new_token)
        self.assertEqual(account["source_type"], "oauth_login")
        self.assertEqual(account["refresh_token"], "fixture-rotated-refresh")
        self.assertTrue(account["last_token_refresh_at"])
        self.assertNotIn(new_token, reloaded.list_expiring_access_tokens())
        self.assertNotIn(new_token, reloaded.list_refresh_token_keepalive_tokens())
        self.assertEqual(reloaded.build_export_items()[0]["refresh_token"], "fixture-rotated-refresh")

    def test_codex_export_marker_does_not_override_explicit_oauth_source(self) -> None:
        for marker in [{"type": "codex"}, {"export_type": "codex"}]:
            with self.subTest(marker=marker):
                payload = AccountService._prepare_account_payload({
                    **marker, "source_type": "oauth_login", "access_token": "fixture-access",
                })
                self.assertEqual(payload["source_type"], "oauth_login")

    def test_build_export_items_uses_codex_shape_and_jwt_claims(self) -> None:
        access_token = make_jwt(
            {
                "exp": 0,
                "iat": 3600,
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"},
                "https://api.openai.com/profile": {"email": "test@example.com"},
            }
        )
        id_token = make_jwt({"email": "fallback@example.com"})
        service = AccountService(
            MemoryStorage(
                [
                    {
                        "access_token": access_token,
                        "id_token": id_token,
                        "refresh_token": "rt_test",
                    }
                ]
            )
        )

        [item] = service.build_export_items([access_token])

        self.assertEqual(item["type"], "codex")
        self.assertEqual(item["email"], "test@example.com")
        self.assertEqual(item["expired"], "1970-01-01T08:00:00+08:00")
        self.assertEqual(item["account_id"], "acct_123")
        self.assertEqual(item["access_token"], access_token)
        self.assertEqual(item["last_refresh"], "1970-01-01T09:00:00+08:00")
        self.assertEqual(item["id_token"], id_token)
        self.assertEqual(item["refresh_token"], "rt_test")

    def test_build_export_items_skips_accounts_missing_complete_tokens(self) -> None:
        complete_access_token = make_jwt({"exp": 0})
        complete_id_token = make_jwt({"email": "complete@example.com"})
        service = AccountService(
            MemoryStorage(
                [
                    {"access_token": "only_access"},
                    {"access_token": "missing_id", "refresh_token": "rt_missing_id"},
                    {"access_token": complete_access_token, "id_token": complete_id_token, "refresh_token": "rt_complete"},
                ]
            )
        )

        items = service.build_export_items()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["access_token"], complete_access_token)
        self.assertEqual(items[0]["id_token"], complete_id_token)
        self.assertEqual(items[0]["refresh_token"], "rt_complete")

    def test_add_account_items_preserves_export_fields_without_overwriting_plan_type(self) -> None:
        service = AccountService(MemoryStorage())

        result = service.add_account_items(
            [
                {
                    "type": "codex",
                    "access_token": "access_token_test",
                    "refresh_token": "rt_test",
                    "account_id": "acct_123",
                }
            ]
        )

        account = service.get_account("access_token_test")
        self.assertEqual(result["added"], 1)
        self.assertIsNotNone(account)
        self.assertEqual(account["type"], "free")
        self.assertEqual(account["export_type"], "codex")
        self.assertEqual(account["refresh_token"], "rt_test")
        self.assertEqual(account["account_id"], "acct_123")
        self.assertEqual(account["source_type"], "codex")


if __name__ == "__main__":
    unittest.main()
