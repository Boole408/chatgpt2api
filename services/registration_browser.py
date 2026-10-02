"""Visible-form OAuth registration driver; never bypasses security challenges."""
from __future__ import annotations

import re
import os
import sys
import time
from urllib.parse import parse_qs, urlparse, unquote

from services.proxy_service import proxy_settings
from services.registration_mailbox import MailboxClient, ManualRequired, RegistrationError
from services.registration_roxy import RoxyLease


def interactive_available() -> bool:
    return os.getenv("CHATGPT2API_REGISTRATION_HEADLESS", "").lower() not in {"1", "true"} and (sys.platform in {"darwin", "win32"} or bool(os.getenv("DISPLAY")))


def safe_location(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"[A-Za-z0-9_.%+-]{40,}|[^/]*@[^/]*", "[redacted]", parsed.path)
    return f"{parsed.scheme}://{parsed.hostname or ''}{':' + str(parsed.port) if parsed.port else ''}{path}"


def browser_proxy() -> dict | None:
    url = proxy_settings.get_profile().proxy_url
    if not url:
        return None
    parsed = urlparse(url)
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    result = {"server": f"{scheme}://{host}:{parsed.port or (1080 if scheme.startswith('socks') else 80)}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


def first_visible(locator):
    for index in range(locator.count()):
        element = locator.nth(index)
        if element.is_visible():
            return element
    return None


def submit(page) -> None:
    button = first_visible(page.get_by_role("button", name=re.compile(r"^(Continue|Next|Verify|Create account|Sign up|Agree|Accept|继续|下一步|验证|创建账户|注册|同意)$", re.I)))
    if button is None:
        button = first_visible(page.locator('button[type="submit"], input[type="submit"]'))
    if button is None or not button.is_enabled():
        raise ManualRequired("页面没有可用的继续按钮，需要人工处理")
    button.click()


def otp_inputs(page):
    fields = [field for field in page.locator('input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[inputmode="numeric"][maxlength="1"]').all() if field.is_visible()]
    if not fields and re.search(r"email.verification|verify.email", page.url, re.I):
        fields = [field for field in page.locator('input[inputmode="numeric"], input[type="tel"]').all() if field.is_visible()]
    return fields


def fill_otp(page, code: str) -> None:
    fields = otp_inputs(page)
    if len(fields) == 1:
        fields[0].fill(code)
    elif len(fields) == len(code):
        for field, digit in zip(fields, code):
            field.fill(digit)
    else:
        raise ManualRequired("无法识别验证码输入控件，需要人工处理")


def registration_session(page, origin: str, email: str) -> bool:
    """Confirm registration through the browser's own authenticated session."""
    # Browser fetch follows the Roxy profile's proxy and cookies; the separate
    # Playwright request client may use a different route when attached via CDP.
    data = page.evaluate("""async url => {
        try {
            const response = await fetch(url, {credentials: 'include', signal: AbortSignal.timeout(10000)});
            if (!response.ok) return null;
            const session = await response.json();
            return {authenticated: Boolean(session.accessToken), email: session.user?.email || ''};
        } catch { return null; }
    }""", origin + "/api/auth/session")
    if not isinstance(data, dict) or not data.get("authenticated"):
        return False
    actual = str(data.get("email") or "").lower()
    if actual != email.lower():
        raise RegistrationError("注册会话邮箱不匹配，已阻止 OAuth 授权")
    return True


def fill_profile(page, row: dict) -> bool:
    if not re.search(r"about.you|profile|birthday", urlparse(page.url).path, re.I):
        return False
    name = first_visible(page.get_by_label(re.compile(r"full name|姓名|全名|^name$", re.I)))
    if name is None:
        name = first_visible(page.locator('input[name="name"], input[autocomplete="name"]'))
    if name is None:
        return False
    name.fill(row["name"])
    age = first_visible(page.get_by_label(re.compile(r"^age$|年龄", re.I)))
    if age is None:
        age = first_visible(page.locator('input[name="age"]'))
    if age is not None:
        age.fill(str(row["age"]))
    else:
        birthday = first_visible(page.locator('input[type="date"], input[name="birthday"], input[name="birthdate"], input[name="date_of_birth"]'))
        if birthday is not None:
            birthday.fill(row["birthday"])
        else:
            values = {"year": row["birthday"][:4], "month": str(int(row["birthday"][5:7])), "day": str(int(row["birthday"][8:]))}
            for part, value in values.items():
                field = first_visible(page.get_by_role("spinbutton", name=re.compile(part, re.I)))
                if field is None:
                    field = first_visible(page.locator(f'input[name="{part}"]'))
                if field is not None:
                    field.fill(value)
                    continue
                field = first_visible(page.get_by_role("combobox", name=re.compile(part, re.I)))
                if field is None or field.evaluate("el => el.tagName") != "SELECT":
                    raise ManualRequired("无法识别生日填写控件，需要人工处理")
                field.select_option(value=value)
    return True


class RegistrationBrowser:
    def run(self, oauth, row: dict, settings: dict, mailbox: MailboxClient, check, update, consume, *, manual=None, diagnose=None) -> str:
        try:
            from playwright.sync_api import sync_playwright, Error as BrowserError
        except ImportError:
            raise RegistrationError("浏览器依赖未安装，请按部署文档安装 Playwright") from None
        callback = ""
        factory = oauth if callable(oauth) else lambda: oauth
        oauth_data = None if callable(oauth) else oauth
        oauth_created_at = time.monotonic()
        prefix = urlparse(oauth_data["redirect_uri_prefix"]) if oauth_data else None
        baseline = set()
        sent_at = time.time()
        use_roxy = settings.get("registration_driver", "chromium") == "roxy"
        interactive = manual is not None and (use_roxy or interactive_available())
        with sync_playwright() as playwright:
            lease = None
            try:
                if use_roxy:
                    lease = RoxyLease(settings, row, update)
                    browser, context = lease.connect(playwright, proxy_settings.get_profile().proxy_url)
                else:
                    browser = playwright.chromium.launch(headless=not interactive, proxy=browser_proxy())
                    context = browser.new_context(locale="en-US")
            except RegistrationError:
                if lease:
                    lease.close()
                raise
            except BrowserError:
                if lease:
                    lease.close()
                raise RegistrationError("浏览器启动或连接失败，请检查所选驱动及运行依赖") from None
            try:
                page = context.new_page()
                page.set_default_timeout(5000)
                page.set_default_navigation_timeout(20000)
                failures = []
                context.on("response", lambda response: failures.append({"url": safe_location(response.url), "status": response.status}) if response.status >= 400 and len(failures) < 20 else None)
                context.on("requestfailed", lambda request: failures.append({"url": safe_location(request.url), "status": "network_failed"}) if len(failures) < 20 else None)

                def intercept(route):
                    nonlocal callback
                    parsed = urlparse(route.request.url)
                    if prefix and parsed.scheme == prefix.scheme and parsed.netloc == prefix.netloc and parsed.path == prefix.path:
                        callback = route.request.url
                        route.abort()  # Prevent the platform from consuming the OAuth code.
                    else:
                        route.continue_()

                context.route("**/*", intercept)
                oauth_started = bool(row.get("registered"))
                if oauth_started:
                    oauth_data = factory()
                    oauth_created_at = time.monotonic()
                    prefix = urlparse(oauth_data["redirect_uri_prefix"])
                registration_url = (oauth_data or {}).get("registration_url", settings.get("_registration_url", "https://chatgpt.com/auth/login"))
                registration_origin = urlparse(registration_url)
                registration_origin = f"{registration_origin.scheme}://{registration_origin.netloc}"
                update(stage="opening", message="打开 OAuth 授权页面" if oauth_started else "打开 ChatGPT 注册页面")
                try:
                    page.goto(oauth_data["authorize_url"] if oauth_started else registration_url, wait_until="domcontentloaded")
                except BrowserError:
                    if not callback:
                        raise RegistrationError("注册或授权页面加载失败，请检查网络或代理") from None
                email_submitted = False
                email_submitted_at = 0.0
                email_attempts = 0
                signup_selected = bool(row.get("registered"))
                password_submitted = False
                password_is_signup = False
                otp_submitted = False
                otp_deadline = None
                otp_submitted_at = 0.0
                otp_attempts = 0
                profile_submitted = False
                consent_submitted = False
                challenge_started = None
                while not callback:
                    check()
                    text = page.locator("body").inner_text(timeout=5000)
                    if re.search(r"checking your browser|verify you are human|security verification|enable javascript and cookies|sorry, you have been blocked|正在进行安全验证|验证您是人类", text, re.I) or re.search(r"just a moment", page.title(), re.I) or first_visible(page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]')):
                        if challenge_started is None:
                            challenge_started = time.monotonic()
                        if time.monotonic() - challenge_started >= 10:
                            if diagnose:
                                diagnose(page, "security_challenge", failures)
                            if not interactive:
                                raise ManualRequired("OpenAI 安全验证需要人工处理；当前无可交互桌面，请使用本地可见浏览器或配置 DISPLAY/VNC 后重试")
                            paused_at = time.monotonic()
                            manual("请在保留的浏览器窗口完成安全验证，再点击继续", page)
                            paused = time.monotonic() - paused_at
                            if otp_deadline is not None:
                                otp_deadline += paused
                            if oauth_started and time.monotonic() - oauth_created_at >= 540 and callable(oauth):
                                oauth_data = factory()
                                oauth_created_at = time.monotonic()
                                prefix = urlparse(oauth_data["redirect_uri_prefix"])
                                callback = ""
                                page.goto(oauth_data["authorize_url"], wait_until="domcontentloaded")
                            challenge_started = None
                            continue
                        mailbox.sleep(0.5)
                        continue
                    challenge_started = None
                    if password_submitted and password_is_signup and not row.get("password_set") and first_visible(page.locator('input[type="password"]')) is None:
                        update(password_set=True, _registration_password=settings.get("registration_password", ""))
                    if not oauth_started and page.url.startswith(registration_origin + "/") and urlparse(page.url).path != "/auth/login" and not first_visible(page.locator('input[type="email"], input[type="password"], input[name="name"], input[autocomplete="name"]')) and not otp_inputs(page):
                        try:
                            session_ready = registration_session(page, registration_origin, row["email"])
                        except BrowserError:
                            mailbox.sleep(1)
                            continue  # Navigation can replace the JS context during the check.
                        if session_ready:
                            update(registered=True, stage="opening_oauth", message="注册完成，复用浏览器会话打开 OAuth")
                            oauth_data = factory()
                            oauth_created_at = time.monotonic()
                            prefix = urlparse(oauth_data["redirect_uri_prefix"])
                            oauth_started = True
                            email_submitted = password_submitted = otp_submitted = profile_submitted = consent_submitted = False
                            email_attempts = 0
                            signup_selected = True
                            otp_deadline = None
                            otp_attempts = 0
                            page.goto(oauth_data["authorize_url"], wait_until="domcontentloaded")
                            continue
                    fields = otp_inputs(page)
                    if (first_visible(page.locator('input[type="tel"], input[autocomplete="tel"]')) and not fields) or re.search(r"verify your phone|验证您的手机", text, re.I):
                        raise ManualRequired("页面要求手机验证，需要人工处理")
                    if re.search(r"too many requests|too many attempts", text, re.I):
                        raise RegistrationError("验证请求过多，请稍后重试")
                    rejected_code = re.search(r"incorrect code|invalid code|wrong code|code.*expired|验证码.*(?:错误|无效|过期)|コード.*(?:正しくありません|無効|有効期限|間違)", text, re.I) or any(field.get_attribute("aria-invalid") == "true" for field in fields)
                    if otp_submitted and not rejected_code and time.monotonic() - otp_submitted_at >= 30 and fields:
                        if diagnose:
                            diagnose(page, "otp_navigation_stalled", failures)
                        if not interactive:
                            raise RegistrationError("验证码已提交但页面 30 秒未继续，未判定注册成功；请检查诊断后重试")
                        paused_at = time.monotonic()
                        manual("验证码提交后页面未继续，请在原 Chromium 窗口检查提示并处理，再点击继续", page)
                        paused = time.monotonic() - paused_at
                        if otp_deadline is not None:
                            otp_deadline += paused
                        otp_submitted_at = time.monotonic()
                        continue
                    if rejected_code and otp_submitted and time.monotonic() - otp_submitted_at >= 2:
                        if otp_attempts >= 3:
                            raise RegistrationError("验证码连续被拒绝，已达到 3 次提交上限")
                        update(stage="retrying_code", message="验证码被拒绝，5 秒后读取最新邮件")
                        resend = first_visible(page.get_by_role("button", name=re.compile(r"resend|send (?:a )?(?:new |another )?code|重新发送|重发|再送信", re.I)))
                        if resend is None:
                            resend = first_visible(page.get_by_role("link", name=re.compile(r"resend|send (?:a )?(?:new |another )?code|重新发送|重发|再送信", re.I)))
                        if resend is not None and resend.is_enabled():
                            baseline.update(message["id"] for message in mailbox.read())
                            sent_at = time.time()
                            resend.click()
                        mailbox.sleep(5)
                        otp_submitted = False
                    if not signup_selected:
                        signup = first_visible(page.get_by_role("link", name=re.compile(r"^(Sign up|Create account|注册|创建账户)$", re.I)))
                        if signup is None:
                            signup = first_visible(page.get_by_role("button", name=re.compile(r"^(Sign up|Create account|注册|创建账户)$", re.I)))
                        if signup is not None:
                            signup_selected = True
                            signup.click()
                            mailbox.sleep(0.5)
                            continue
                    email = first_visible(page.locator('input[type="email"], input[autocomplete="email"], input[name="username"]'))
                    if email is not None and email_submitted and time.monotonic() - email_submitted_at >= 18 and not fields and not first_visible(page.locator('input[type="password"]')):
                        # ChatGPT may clear the input during its login?email SPA transition.
                        # Allow navigation to settle before refilling, with a bounded budget.
                        if email_attempts >= 3:
                            if diagnose:
                                diagnose(page, "email_navigation_stalled", failures)
                            if not interactive:
                                raise RegistrationError("邮箱提交后页面未继续，已达到 3 次提交上限")
                            manual("邮箱提交后页面未继续，请在原浏览器窗口检查提示并处理，再点击继续", page)
                            email_submitted_at = time.monotonic()
                            continue
                        email_submitted = False
                    if email is None and not email_submitted and not fields:
                        email_option = first_visible(page.get_by_role("button", name=re.compile(r"^(Continue with email|Use email|使用邮箱继续|使用邮箱)$", re.I)))
                        if email_option is not None:
                            email_option.click()
                            mailbox.sleep(0.5)
                            continue
                    if email is not None and not email_submitted:
                        update(stage="email", message="提交注册邮箱")
                        baseline = {message["id"] for message in mailbox.read()}
                        email.fill(row["email"])
                        sent_at = time.time()
                        submit(page)
                        email_submitted = True
                        email_submitted_at = time.monotonic()
                        email_attempts += 1
                    elif fields:
                        if not otp_submitted:
                            update(stage="waiting_code", message="等待转发的邮箱验证码")
                            if otp_deadline is None:
                                otp_deadline = time.monotonic() + 180
                            code = mailbox.wait_code(row["email"], sent_at, baseline, consume, deadline=otp_deadline)
                            fill_otp(page, code)
                            update(stage="verifying", message="提交验证码")
                            # Some split OTP controls submit automatically on digit six.
                            if first_visible(page.locator('button[type="submit"], input[type="submit"]')) or first_visible(page.get_by_role("button", name=re.compile(r"^(Continue|Verify|Next|继续|验证|下一步)$", re.I))):
                                submit(page)
                            otp_submitted = True
                            otp_attempts += 1
                            otp_submitted_at = time.monotonic()
                    elif first_visible(page.locator('input[type="password"]')) and not password_submitted:
                        signup_password = bool(re.search(r"create.account|signup|sign-up|set.password", page.url, re.I)) or bool(re.search(r"create (?:a |your )?password|设置密码|创建密码", text, re.I))
                        if not signup_password and not row.get("password_set"):
                            alternative = first_visible(page.get_by_role("button", name=re.compile(r"code|验证码", re.I)))
                            if alternative is None:
                                alternative = first_visible(page.get_by_role("link", name=re.compile(r"code|验证码", re.I)))
                            if alternative is None:
                                raise ManualRequired("已有账号要求未知密码，无法自动登录")
                            baseline = {message["id"] for message in mailbox.read()}
                            sent_at = time.time()
                            alternative.click()
                            mailbox.sleep(0.5)
                            continue
                        if not settings.get("registration_password"):
                            raise ManualRequired("注册要求设置密码，请先在接码设置中配置注册密码")
                        update(stage="password", message="设置注册密码" if signup_password else "登录已注册账号")
                        for field in page.locator('input[type="password"]').all():
                            if field.is_visible():
                                field.fill(settings["registration_password"])
                        baseline = {message["id"] for message in mailbox.read()}
                        sent_at = time.time()
                        submit(page)
                        password_submitted = True
                        password_is_signup = signup_password
                    elif not profile_submitted and fill_profile(page, row):
                        update(stage="profile", message="填写姓名和年龄")
                        try:
                            submit(page)
                        except BrowserError:
                            if diagnose:
                                diagnose(page, "profile_submission_failed", failures)
                            if not interactive:
                                raise RegistrationError("个人资料提交失败，请检查诊断后重试") from None
                            manual("个人资料提交未完成，请在原浏览器窗口检查并提交，再点击继续", page)
                        profile_submitted = True
                    elif re.search(r"already (?:have|exists|registered)|已注册|已有账户", text, re.I):
                        login = first_visible(page.get_by_role("link", name=re.compile(r"^(Log in|Sign in|登录)$", re.I)))
                        if login is not None:
                            update(stage="login", message="邮箱已存在，转到登录并核实会话")
                            login.click()
                            email_submitted = False
                            otp_submitted = False
                    elif not consent_submitted and re.search(r"allow .*access|authorize|授权|access your", text, re.I):
                        approve = first_visible(page.get_by_role("button", name=re.compile(r"^(Allow|Authorize|Accept|Continue|允许|授权|继续)$", re.I)))
                        if approve is not None:
                            update(stage="authorizing", message="完成 OAuth 授权")
                            approve.click()
                            consent_submitted = True
                    mailbox.sleep(0.5)
                parsed_callback = parse_qs(urlparse(callback).query)
                if not parsed_callback.get("code") or not parsed_callback.get("state"):
                    raise RegistrationError("OAuth 回调缺少 code 或 state")
                if not oauth_data:
                    raise RegistrationError("尚未创建 OAuth 会话，已阻止回调兑换")
                expected_state = parse_qs(urlparse(oauth_data["authorize_url"]).query).get("state")
                if expected_state and parsed_callback["state"] != expected_state:
                    raise RegistrationError("OAuth 回调 state 不匹配，已阻止兑换")
                if password_submitted and password_is_signup:
                    update(password_set=True, _registration_password=settings.get("registration_password", ""))
                update(registered=True, stage="exchanging", message="兑换 OAuth Token")
                return callback
            except BrowserError:
                if diagnose:
                    try:
                        diagnose(page, "browser_operation_failed", failures)
                    except Exception:
                        pass
                raise RegistrationError("注册页面操作失败，可能是页面发生变化，请重试或人工处理") from None
            finally:
                if lease:
                    lease.close()
                else:
                    browser.close()
