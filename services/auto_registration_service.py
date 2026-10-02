from __future__ import annotations

import copy
import json
import os
import secrets
import queue
import re
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from curl_cffi import requests

from services.account_service import account_service
from services.config import DATA_DIR
from services.oauth_login_service import OAuthLoginError, oauth_login_service
from services.openai_oauth import auth_base
from services.proxy_service import proxy_settings
from services.registration_browser import RegistrationBrowser, interactive_available, safe_location
from services.registration_roxy import RoxyClient, local_api_url
from services.registration_mailbox import MailboxClient, ManualRequired, RegistrationError, RegistrationStopped, parse_emails

ACTIVE = {"queued", "running", "stopping", "waiting_manual"}
FAILED = {"failed", "manual_required", "interrupted", "cancelled"}
PUBLIC_FIELDS = {"id", "email", "name", "age", "status", "stage", "message", "error", "warning", "registered", "authorized", "imported", "password_set", "manual_expires_at", "diagnostics"}


class AuthorizationExpired(RegistrationError):
    pass


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class RegistrationSettings:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def public(self) -> dict:
        data = self.snapshot()
        return {"site_url": "https://ccmtc.cfd/mail", "email": data.get("email", ""),
                "client_id": data.get("client_id", ""), "protocol": data.get("protocol", "imap"), "folder": "inbox",
                "configured": all(data.get(key) for key in ("email", "client_id", "refresh_token")),
                "has_mailbox_password": bool(data.get("password")), "has_registration_password": bool(data.get("registration_password")),
                "registration_driver": data.get("registration_driver", "chromium"), "manual_available": data.get("registration_driver") == "roxy" or interactive_available(),
                "roxy_api_base": data.get("roxy_api_base", "http://127.0.0.1:50000"), "roxy_workspace_id": data.get("roxy_workspace_id", ""),
                "roxy_project_id": data.get("roxy_project_id", ""), "has_roxy_api_token": bool(data.get("roxy_api_token"))}

    def save(self, credential_line: str = "", protocol: str = "imap", registration_password: str | None = None,
             registration_driver: str | None = None, roxy_api_base: str | None = None, roxy_api_token: str | None = None,
             roxy_workspace_id: str | None = None, roxy_project_id: str | None = None) -> dict:
        if protocol not in {"imap", "graph"}:
            raise RegistrationError("请选择 IMAP 或 Graph")
        with self.lock:
            data = self.snapshot()
            if credential_line.strip():
                parts = [part.strip() for part in credential_line.strip().split("----", 3)]
                if len(parts) != 4 or not parse_emails(parts[0])["emails"] or not parts[2] or not parts[3]:
                    raise RegistrationError("接码凭据格式应为 邮箱----密码----client_id----refresh_token")
                data.update(zip(("email", "password", "client_id", "refresh_token"), parts))
            data["protocol"] = protocol
            if registration_driver is not None:
                if registration_driver not in {"chromium", "roxy"}:
                    raise RegistrationError("请选择 Chromium 或 RoxyBrowser")
                data["registration_driver"] = registration_driver
            if roxy_api_base is not None:
                data["roxy_api_base"] = local_api_url(roxy_api_base)
            for key, value in (("roxy_api_token", roxy_api_token), ("roxy_workspace_id", roxy_workspace_id), ("roxy_project_id", roxy_project_id)):
                if value is not None:
                    data[key] = value.strip()
            if registration_password is not None:
                if registration_password and len(registration_password) < 8:
                    raise RegistrationError("注册密码至少 8 位")
                data["registration_password"] = registration_password
            write_private(self.path, data)
            self.data = data
        return self.public()


class AutoRegistrationService:
    def __init__(self, directory: Path = DATA_DIR, *, driver=None, importer=None):
        self.settings = RegistrationSettings(directory / "registration_settings.json")
        self.path = directory / "registration_jobs.json"
        self.lock = threading.RLock()
        self.driver = driver or RegistrationBrowser()
        self.importer = importer or self._import
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.controls: dict[str, queue.Queue] = {}
        self.manual_timeout_seconds = 900
        state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.jobs = state.get("jobs", [])
        self.used_ids = set(state.get("used_message_ids", []))
        changed = False
        for job in self.jobs:
            if job["status"] in ACTIVE:
                job["status"] = "interrupted"
                for row in job["rows"]:
                    if row["status"] in ACTIVE:
                        row.update(status="interrupted", stage="interrupted", message="服务重启，任务中断，请重试")
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        write_private(self.path, {"jobs": self.jobs, "used_message_ids": sorted(self.used_ids)})

    def _public(self, job: dict) -> dict:
        rows = [{key: copy.deepcopy(value) for key, value in row.items() if key in PUBLIC_FIELDS} for row in job["rows"]]
        return {"id": job["id"], "status": job["status"], "created_at": job["created_at"], "rows": rows,
                "total": len(rows), "success": sum(row["status"] == "success" for row in rows),
                "failed": sum(row["status"] in FAILED for row in rows),
                "remaining": sum(row["status"] in ACTIVE for row in rows)}

    def list_jobs(self) -> dict:
        with self.lock:
            return {"items": [self._public(job) for job in reversed(self.jobs[-20:])]}

    def _get(self, job_id: str) -> dict:
        job = next((job for job in self.jobs if job["id"] == job_id), None)
        if job is None:
            raise RegistrationError("注册任务不存在")
        return job

    def get(self, job_id: str) -> dict:
        with self.lock:
            return self._public(self._get(job_id))

    def busy(self) -> bool:
        return any(job["status"] in ACTIVE for job in self.jobs)

    def save_settings(self, **kwargs) -> dict:
        with self.lock:
            if self.busy():
                raise RegistrationError("任务运行期间不能更换接码设置，请先停止任务")
            return self.settings.save(**kwargs)

    def test_mailbox(self) -> dict:
        with self.lock:
            if self.busy():
                raise RegistrationError("任务运行期间请勿测试取件")
            settings = self.settings.snapshot()
            if not self.settings.public()["configured"]:
                raise RegistrationError("请先保存接码邮箱凭据")
        messages = MailboxClient(settings, lambda: None).read()
        # Do not expose body, OTP or credentials to the browser.
        return {"ok": True, "count": len(messages), "message": f"取件成功，本页读取到 {len(messages)} 封邮件"}

    def test_roxy(self) -> dict:
        with self.lock:
            if self.busy():
                raise RegistrationError("请先停止当前任务，再测试 Roxy 连接")
            settings = self.settings.snapshot()
        return RoxyClient(settings).workspaces()

    def create(self, content: str, *, launch: bool = True) -> dict:
        parsed = parse_emails(content)
        if parsed["errors"] or not parsed["emails"]:
            raise RegistrationError("邮箱列表包含错误或为空，请检查预览")
        with self.lock:
            if self.busy():
                raise RegistrationError("已有注册任务正在运行")
            if not self.settings.public()["configured"]:
                raise RegistrationError("请先保存接码设置并测试取件")
            saved = self.settings.snapshot()
            if saved.get("registration_driver") == "roxy" and (not saved.get("roxy_api_token") or not saved.get("roxy_workspace_id")):
                raise RegistrationError("请先配置 Roxy API Token 并选择工作区")
            existing = {str(item.get("email") or "").lower() for item in account_service.list_accounts()}
            rows = []
            for email in parsed["emails"]:
                age = 26 + secrets.randbelow(35)
                today = date.today()
                birthday = today.replace(year=today.year - age, day=min(today.day, 28)).isoformat()
                rows.append({"id": uuid.uuid4().hex, "email": email, "name": secrets.choice(["James", "Oliver", "Henry", "Emma", "Sophia", "Grace"]) + " " + secrets.choice(["Smith", "Wilson", "Taylor", "Brown", "Miller"]),
                             "age": age, "birthday": birthday, "status": "skipped" if email in existing else "queued",
                             "stage": "skipped" if email in existing else "queued", "message": "邮箱已在号池中，已跳过" if email in existing else "等待注册",
                             "registered": False, "authorized": False, "imported": email in existing, "password_set": False})
            job = {"id": uuid.uuid4().hex, "created_at": time.time(), "status": "queued", "rows": rows}
            self.jobs.append(job)
            self._save()
            if launch:
                self._launch(job)
            return self._public(job)

    def _launch(self, job: dict) -> None:
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(job["id"], self.stop_event), daemon=True, name="account-registration")
        self.thread.start()

    def stop(self, job_id: str) -> dict:
        with self.lock:
            job = self._get(job_id)
            if job["status"] in ACTIVE:
                job["status"] = "stopping"
                self.stop_event.set()
                self._save()
            return self._public(job)

    def retry(self, job_id: str, *, launch: bool = True) -> dict:
        with self.lock:
            if self.busy():
                raise RegistrationError("请等待当前任务停止后再重试")
            job = self._get(job_id)
            selected = [row for row in job["rows"] if row["status"] in FAILED and not row.get("imported")]
            if not selected:
                raise RegistrationError("没有可重试的失败项")
            for row in selected:
                row.update(status="queued", stage="queued", message="等待重试", error="", warning="")
            job["status"] = "queued"
            self._save()
            if launch:
                self._launch(job)
            return self._public(job)

    def shutdown(self) -> None:
        with self.lock:
            self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def manual_action(self, job_id: str, row_id: str, action: str) -> dict:
        if action not in {"show", "continue", "cancel"}:
            raise RegistrationError("未知人工处理动作")
        with self.lock:
            job = self._get(job_id)
            row = next((item for item in job["rows"] if item["id"] == row_id), None)
            if not row or row["status"] != "waiting_manual" or row_id not in self.controls:
                raise RegistrationError("人工会话已结束，请刷新任务进度")
            self.controls[row_id].put(action)
            return self._public(job)

    def diagnostic_image(self, job_id: str, row_id: str) -> Path:
        with self.lock:
            row = next((item for item in self._get(job_id)["rows"] if item["id"] == row_id), None)
            path = self.path.parent / "registration_diagnostics" / f"{row['id'] if row else 'missing'}.png"
            if not row or not path.is_file():
                raise RegistrationError("暂无诊断截图")
            return path

    def _run(self, job_id: str, stop: threading.Event) -> None:
        with self.lock:
            job = self._get(job_id)
            job["status"] = "running"
            settings = self.settings.snapshot()
            self._save()
        for row in job["rows"]:
            if row["status"] != "queued":
                continue
            deadline = time.monotonic() + 600

            def check():
                if stop.is_set():
                    raise RegistrationStopped("任务已停止")
                if time.monotonic() >= deadline:
                    raise RegistrationError("注册流程超时（10 分钟）")

            def update(**values):
                with self.lock:
                    row.update(values)
                    self._save()

            def consume(message_id):
                with self.lock:
                    self.used_ids.add(message_id)
                    self._save()

            def diagnose(page, reason, failures):
                title = re.sub(r"[^\s]*@[^\s]*|\b\d{6}\b|[A-Za-z0-9_.-]{40,}", "[redacted]", page.title())[:160]
                directory = self.path.parent / "registration_diagnostics"
                directory.mkdir(mode=0o700, exist_ok=True)
                path = directory / f"{row['id']}.png"
                captured = False
                try:
                    page.screenshot(path=str(path), mask=[page.locator('input, textarea, [contenteditable="true"]'), page.get_by_text(re.compile(r"@|\b\d{6}\b|Bearer |M\\.", re.I))])
                    path.chmod(0o600)
                    captured = True
                except Exception:
                    path.unlink(missing_ok=True)
                update(diagnostics={"reason": reason, "url": safe_location(page.url), "title": title, "time": time.time(), "failures": failures[-20:], "screenshot": captured})

            def manual(reason, page):
                nonlocal deadline
                started = time.monotonic()
                actions = queue.Queue()
                with self.lock:
                    self.controls[row["id"]] = actions
                    job["status"] = "waiting_manual"
                    update(status="waiting_manual", stage="waiting_manual", message=reason, manual_expires_at=time.time() + self.manual_timeout_seconds)
                try:
                    page.bring_to_front()
                    while True:
                        if stop.is_set():
                            raise RegistrationStopped("任务已停止")
                        if time.monotonic() - started >= self.manual_timeout_seconds:
                            raise ManualRequired("人工验证等待超时（15 分钟），会话已关闭，请重试")
                        try:
                            action = actions.get_nowait()
                        except queue.Empty:
                            page.wait_for_timeout(200)  # Pump browser events in the owning thread.
                            continue
                        if action == "show":
                            page.bring_to_front()
                        elif action == "cancel":
                            raise RegistrationStopped("已取消此账号的人工处理")
                        else:
                            return
                finally:
                    deadline += time.monotonic() - started
                    with self.lock:
                        self.controls.pop(row["id"], None)
                        if job["status"] != "stopping":
                            job["status"] = "running"
                        update(status="running", manual_expires_at=None, stage="resuming", message="继续原浏览器会话，重新核实页面状态")

            try:
                check()
                update(status="running", error="", warning="")
                if row.get("_roxy_profile") and settings.get("registration_driver") != "roxy":
                    raise RegistrationError("此账号的 Roxy 临时环境尚未回收，请使用原 Roxy 配置重试回收")
                tokens = row.get("_tokens")
                identity_verified = False
                if tokens:
                    try:
                        self._verify_email(tokens["access_token"], row["email"])
                        identity_verified = True
                    except AuthorizationExpired:
                        tokens = None
                        update(_tokens=None, authorized=False)
                if not tokens:
                    mailbox = MailboxClient(settings, check, self.used_ids)
                    oauth = None

                    def start_oauth():
                        nonlocal oauth
                        oauth = oauth_login_service.start(row["email"])
                        return oauth

                    row_settings = {**settings}
                    if row.get("_registration_password"):
                        row_settings["registration_password"] = row["_registration_password"]
                    callback = self.driver.run(start_oauth, row, row_settings, mailbox, check, update, consume, manual=manual, diagnose=diagnose)
                    check()
                    if oauth is None:
                        raise RegistrationError("浏览器尚未创建 OAuth 会话，已阻止兑换")
                    tokens = oauth_login_service.finish(oauth["session_id"], callback)
                    update(authorized=True, _tokens=tokens, stage="checking_identity", message="确认账号身份")
                check()
                # Identity validation happens before any persistent account insert.
                if not identity_verified:
                    self._verify_email(tokens["access_token"], row["email"])
                check()
                update(stage="importing", message="加入号池")
                result = self.importer(tokens, row["email"])
                update(imported=True, status="success", stage="complete", message="注册与入池完成", warning=result.get("warning", ""))
            except RegistrationStopped as exc:
                update(status="cancelled", stage="cancelled", message=str(exc))
            except ManualRequired as exc:
                update(status="manual_required", stage="manual_required", message=str(exc), error=str(exc))
            except RegistrationError as exc:
                update(status="failed", stage="failed", message=str(exc), error=str(exc))
            except OAuthLoginError as exc:
                http_status = re.search(r"HTTP (\d{3})", str(exc))
                message = f"OAuth Token 兑换接口返回 HTTP {http_status.group(1)}，尚未入池" if http_status else "OAuth 授权或 Token 兑换失败，请重试"
                update(status="failed", stage="failed", message=message, error=message)
            except Exception:
                # Raw browser/network errors may contain callback URLs and secrets.
                update(status="failed", stage="failed", message="注册任务执行异常，请检查部署环境后重试", error="注册任务执行异常，请检查部署环境后重试")
            finally:
                if row.get("_roxy_profile"):
                    stop.set()  # Do not start another account after incomplete cleanup.
        with self.lock:
            job["status"] = "stopped" if stop.is_set() else "completed"
            self._save()

    @staticmethod
    def _verify_email(token: str, expected: str) -> None:
        try:
            # OpenAI's OIDC discovery advertises this authenticated identity API.
            # Account quota/profile endpoints can be unavailable independently.
            response = requests.get(f"{auth_base}/api/accounts/oauth/userinfo", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                    timeout=20, **proxy_settings.build_session_kwargs(impersonate="chrome"))
            if response.status_code == 401:
                raise AuthorizationExpired("已保存授权凭证失效，需要重新授权")
            if response.status_code != 200:
                raise RegistrationError(f"OAuth 身份接口返回 HTTP {response.status_code}，未导入号池")
            data = response.json()
        except AuthorizationExpired:
            raise
        except RegistrationError:
            raise
        except Exception:
            raise RegistrationError("无法核实 Token 对应邮箱，未导入号池，请重试") from None
        email = str(data.get("email") or (data.get("user") or {}).get("email") or "").lower()
        if email != expected.lower():
            raise RegistrationError("Token 对应邮箱不匹配或缺失，已阻止导入")

    @staticmethod
    def _import(tokens: dict, email: str) -> dict:
        account_service.add_account_items([{**tokens, "email": email, "source_type": "oauth_login"}])
        try:
            result = account_service.refresh_accounts([tokens["access_token"]])
            return {"warning": "已入池，账号资料刷新失败，可在号池中重试刷新" if result.get("errors") else ""}
        except Exception:
            return {"warning": "已入池，账号资料刷新失败，可在号池中重试刷新"}


auto_registration_service = AutoRegistrationService()
