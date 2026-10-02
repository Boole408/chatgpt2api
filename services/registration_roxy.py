"""Local Roxy API lifecycle; each run owns its newly created profile."""
from __future__ import annotations

import ipaddress
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse, unquote

from services.registration_mailbox import RegistrationError


def local_api_url(value: str) -> str:
    parsed = urlparse(str(value).strip())
    try:
        local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        local = False
        port = None
    if not local or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or not port:
        raise RegistrationError("Roxy API 地址须为本机地址，例如 http://127.0.0.1:50000")
    return str(value).strip().rstrip("/")


def identifier(value):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


class RoxyClient:
    def __init__(self, settings):
        self.base = local_api_url(settings.get("roxy_api_base", "http://127.0.0.1:50000"))
        self.token = settings.get("roxy_api_token", "")
        if not self.token:
            raise RegistrationError("请先在浏览器设置中保存 Roxy API Token")
        self.workspace = identifier(settings.get("roxy_workspace_id"))
        self.project = identifier(settings.get("roxy_project_id"))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path: str, body=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"token": self.token, "Content-Type": "application/json"}, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RegistrationError(f"Roxy API {path} 返回 HTTP {exc.code}，请检查本地 API 与 Token") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RegistrationError("无法连接本地 Roxy API，请启动 RoxyBrowser 并启用 API") from None
        except (ValueError, TypeError):
            raise RegistrationError("Roxy API 返回格式无法识别") from None
        if not isinstance(data, dict) or data.get("code") not in {None, 0, 200, "0", "200"} or data.get("success") is False:
            raise RegistrationError(f"Roxy API {path} 拒绝请求，请检查权限、工作区及项目配置")
        return data.get("data", data)

    def workspaces(self):
        data = self.request("/browser/workspace")
        rows = data.get("rows", data.get("list", [])) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise RegistrationError("Roxy 工作区列表格式无法识别")
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            wid = row.get("id") or row.get("workspaceId")
            if not wid:
                continue
            projects = row.get("project_details") or []
            for project in projects or [{}]:
                items.append({"workspace_id": str(wid), "workspace_name": str(row.get("workspaceName") or row.get("name") or wid)[:80],
                              "project_id": str(project.get("projectId") or ""), "project_name": str(project.get("projectName") or "")[:80]})
        return {"ok": True, "items": items, "message": f"Roxy API 连接成功，读取到 {len(items)} 个工作区/项目"}

    def create(self, proxy_url: str = ""):
        if not self.workspace:
            raise RegistrationError("请先选择 Roxy 工作区及项目")
        body = {"name": "chatgpt2api-" + uuid.uuid4().hex[:12], "workspaceId": str(self.workspace),
                "os": "macOS" if sys.platform == "darwin" else "Windows" if sys.platform == "win32" else "Linux"}
        if self.project:
            body["projectId"] = str(self.project)
        if proxy_url:
            parsed = urlparse(proxy_url)
            protocol = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5", "socks5h": "SOCKS5"}.get(parsed.scheme)
            if not protocol or not parsed.hostname or not parsed.port:
                raise RegistrationError("Roxy 不支持当前全局代理格式")
            body["proxyInfo"] = {"moduleId": 0, "proxyMethod": "custom", "proxyCategory": protocol, "ipType": "IPV4", "protocol": protocol,
                                 "host": parsed.hostname, "port": str(parsed.port), "proxyUserName": unquote(parsed.username or ""), "proxyPassword": unquote(parsed.password or "")}
        # Create is never retried: a lost response could otherwise create duplicates.
        data = self.request("/browser/create", body)
        profile = data.get("dirId") or data.get("id") or data.get("profileId") if isinstance(data, dict) else None
        if not profile:
            raise RegistrationError("Roxy 创建环境后未返回环境 ID，请在 Roxy 中检查临时环境")
        return str(profile)

    def open(self, profile):
        data = self.request("/browser/open", {"workspaceId": str(self.workspace), "dirId": str(profile), "headless": False, "forceOpen": False, "args": []})
        if not isinstance(data, dict):
            raise RegistrationError("Roxy 未返回 CDP 调试地址")
        endpoint = next((data[key] for key in ("ws", "wsEndpoint", "ws_endpoint", "debuggerWsUrl", "http", "debuggerAddress", "debuggingPortUrl") if data.get(key)), "")
        if not endpoint and data.get("debuggingPort"):
            endpoint = f"http://127.0.0.1:{data['debuggingPort']}"
        if not endpoint:
            raise RegistrationError("Roxy 未返回 CDP 调试地址")
        endpoint = str(endpoint)
        if "://" not in endpoint:
            endpoint = "http://" + endpoint
        parsed = urlparse(endpoint)
        try:
            local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        if not local or parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.username or parsed.password:
            raise RegistrationError("Roxy 返回了非本机 CDP 地址，已阻止连接")
        return endpoint

    def cleanup(self, profile):
        self.request("/browser/close", {"workspaceId": str(self.workspace), "dirId": str(profile)})
        # Roxy close may return before the environment is eligible for deletion.
        for attempt in range(3):
            try:
                self.request("/browser/delete", {"workspaceId": str(self.workspace), "dirIds": [str(profile)], "isSoftDelete": False})
                return
            except RegistrationError:
                if attempt == 2:
                    raise
                time.sleep(2)


class RoxyLease:
    def __init__(self, settings, row, update):
        self.client = RoxyClient(settings)
        self.row, self.update, self.browser = row, update, None
        self.cleanup_allowed = False

    def connect(self, playwright, proxy_url=""):
        if self.row.get("_roxy_profile"):
            owner = self.row.get("_roxy_owner")
            if owner and owner != {"base": self.client.base, "workspace": self.client.workspace}:
                raise RegistrationError("待回收临时环境属于原 Roxy API/工作区，请恢复原设置后重试")
            self.cleanup_allowed = True
            self.client.cleanup(self.row["_roxy_profile"])
            self.update(_roxy_profile=None)
            self.cleanup_allowed = False
        profile = self.client.create(proxy_url)
        self.update(_roxy_profile=profile, _roxy_owner={"base": self.client.base, "workspace": self.client.workspace})
        self.cleanup_allowed = True
        self.browser = playwright.chromium.connect_over_cdp(self.client.open(profile), timeout=20000)
        if not self.browser.contexts:
            raise RegistrationError("Roxy CDP 连接后未发现浏览器会话")
        return self.browser, self.browser.contexts[0]

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.cleanup_allowed and self.row.get("_roxy_profile"):
                self.client.cleanup(self.row["_roxy_profile"])
                self.update(_roxy_profile=None)
