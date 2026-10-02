"""ccmtc mailbox adapter. Credentials and email bodies never enter task logs."""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from curl_cffi import requests

MAIL_URL = "https://ccmtc.cfd/api/mailboxes/temp/messages"
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class RegistrationError(Exception):
    pass


class ManualRequired(RegistrationError):
    pass


class RegistrationStopped(RegistrationError):
    pass


def parse_emails(value: str) -> dict:
    text = str(value or "").lstrip("\ufeff").strip()
    errors, duplicates, emails = [], [], []
    if text.startswith(("[", "{")):
        try:
            entries = json.loads(text)
            if isinstance(entries, dict):
                entries = entries.get("emails")
            if not isinstance(entries, list):
                raise ValueError
        except (ValueError, TypeError):
            return {"emails": [], "duplicates": [], "errors": [{"line": 1, "error": "JSON 应为邮箱数组或包含 emails 数组的对象"}]}
    else:
        entries = text.splitlines()
    seen = set()
    for line, item in enumerate(entries, 1):
        raw = item.get("email", "") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            errors.append({"line": line, "error": "邮箱必须是字符串"})
            continue
        email = raw.strip().lower()
        if not email:
            continue
        if len(email) > 254 or not EMAIL_RE.fullmatch(email):
            errors.append({"line": line, "error": "邮箱格式无效"})
        elif email in seen:
            duplicates.append({"line": line, "email": email})
        else:
            emails.append(email)
            seen.add(email)
    if len(emails) > 500:
        errors.append({"line": 0, "error": "每批最多 500 个邮箱"})
    return {"emails": emails, "duplicates": duplicates, "errors": errors}


def received_timestamp(value: str) -> float:
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            date = parsedate_to_datetime(str(value))
        except (ValueError, TypeError):
            return 0
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.timestamp()


def normalize_message(item: dict) -> dict:
    message = item.get("message", item)
    if not isinstance(message, dict):
        raise RegistrationError("取件接口返回了无法识别的邮件")
    body = message.get("body", "")
    if isinstance(body, dict):
        body = body.get("content", "")
    body = str(body or message.get("html") or message.get("text") or message.get("bodyPreview") or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    sender = message.get("from") or message.get("sender") or ""
    subject = str(message.get("subject") or "")
    received = str(message.get("receivedDateTime") or message.get("received_at") or message.get("date") or "")
    # Message IDs must be stable across polling, even when a protocol omits id.
    identity = str(message.get("id") or message.get("uid") or message.get("message_id") or json.dumps([received, sender, subject, text], sort_keys=True))
    recipients = []
    for key in ("toRecipients", "to", "recipients"):
        if message.get(key):
            recipients.extend(EMAIL_RE.findall(json.dumps(message[key], ensure_ascii=False)))
    for key in ("internetMessageHeaders", "headers"):
        headers = message.get(key) or []
        if isinstance(headers, dict):
            headers = [{"name": name, "value": value} for name, value in headers.items()]
        if isinstance(headers, list):
            for header in headers:
                if isinstance(header, dict) and str(header.get("name", "")).lower() in {"to", "x-original-to", "delivered-to", "envelope-to"}:
                    recipients.extend(EMAIL_RE.findall(str(header.get("value", ""))))
    return {"id": hashlib.sha256(identity.encode()).hexdigest(), "received": received_timestamp(received),
            "sender": json.dumps(sender, ensure_ascii=False), "subject": subject, "text": text,
            "recipients": {address.lower() for address in recipients}}


def select_code(messages: list[dict], email: str, since: float, baseline: set[str], used: set[str], central_email: str = "") -> tuple[str, str] | None:
    candidates = []
    for mail in messages:
        if mail["id"] in baseline or mail["id"] in used or mail["received"] < since - 2:
            continue
        sender = mail["sender"].lower()
        if not re.search(r"(?<![a-z0-9.-])(?:[a-z0-9-]+\.)*openai\.com(?:[\"\s>]|$)", sender):
            continue
        content = mail["subject"] + " " + mail["text"]
        if not re.search(r"code|verify|verification|验证码|登录代码|登录码|認証コード|確認コード|コード", content, re.I):
            continue
        body_addresses = {address.lower() for address in EMAIL_RE.findall(mail["text"])}
        addressed = mail["recipients"] | body_addresses
        # A forwarded envelope may only contain the central mailbox. Prefer an
        # explicit target match; unrelated domain recipients disqualify a mail.
        if email not in addressed:
            if central_email and mail["recipients"] - {central_email.lower()}:
                continue
            domain = email.rsplit("@", 1)[-1]
            if any(address.rsplit("@", 1)[-1] == domain for address in addressed):
                continue
        codes = set(re.findall(r"(?<!\d)(\d{6})(?!\d)", content))
        if len(codes) == 1:
            candidates.append((email in addressed, next(iter(codes)), mail["id"], mail["received"]))
    exact = [item for item in candidates if item[0]]
    selected = exact or candidates
    if selected:
        newest = max(item[3] for item in selected)
        selected = [item for item in selected if item[3] == newest]
        if len({item[1] for item in selected}) > 1:
            raise RegistrationError("同一时间收到多封不同验证码邮件，无法确定最新一封；请稍后重试")
    return (selected[0][1], selected[0][2]) if selected else None


class MailboxClient:
    def __init__(self, settings: dict, check: Callable[[], None], used: set[str] | None = None):
        self.settings, self.check = settings, check
        self.used = used if used is not None else set()

    def read(self) -> list[dict]:
        payload = {key: self.settings[key] for key in ("email", "password", "client_id", "refresh_token")}
        payload.update(mail_protocol=self.settings.get("protocol", "imap"), folder="inbox", page=1, page_size=100, include_bodies=True)
        for attempt in range(3):
            self.check()
            try:
                response = requests.post(MAIL_URL, json=payload, impersonate="chrome", timeout=20)
            except requests.RequestsError:
                if attempt == 2:
                    raise RegistrationError("ccmtc 取件网络失败，请检查网络") from None
                self.sleep(2)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    self.sleep(2)
                    continue
            if response.status_code != 200:
                raise RegistrationError(f"ccmtc 取件失败（HTTP {response.status_code}）")
            try:
                result = response.json()
                if not result.get("success"):
                    raise RegistrationError("ccmtc 拒绝取件，请检查凭据和协议")
                items = result["data"]["items"]
                if not isinstance(items, list):
                    raise ValueError
                return [normalize_message(item) for item in items if isinstance(item, dict)]
            except (KeyError, ValueError, TypeError):
                raise RegistrationError("ccmtc 返回格式发生变化，无法读取邮件") from None
        raise RegistrationError("ccmtc 暂时不可用")

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(0.25, max(0, end - time.monotonic())))

    def wait_code(self, email: str, since: float, baseline: set[str], consume: Callable[[str], None], *, deadline: float | None = None) -> str:
        end = deadline if deadline is not None else time.monotonic() + 180
        while time.monotonic() < end:
            self.check()
            messages = self.read()
            code = select_code(messages, email, since, baseline, self.used, self.settings.get("email", ""))
            if code:
                received = next(message["received"] for message in messages if message["id"] == code[1])
                # A rejected newest code must never cause a retry with an older one.
                baseline.update(message["id"] for message in messages if message["received"] <= received)
                self.used.add(code[1])
                consume(code[1])
                return code[0]
            self.sleep(min(5, max(0, end - time.monotonic())))
        raise RegistrationError("等待邮箱验证码超时（180 秒）")
