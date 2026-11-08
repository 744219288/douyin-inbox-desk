"""Human-operated official browser login; never clicks or submits messages.

Only the two documented ticketguard storage records are read. The original
project's DouyinAuth.perepare_auth defines their double-JSON record format.
"""
import json
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ORIGIN = "https://www.douyin.com"
PROTECT_KEY = "security-sdk/s_sdk_sign_data_key/web_protect"
CRYPT_KEY = "security-sdk/s_sdk_crypt_sdk"
STORAGE_SCRIPT = """keys => {
    if (location.origin !== 'https://www.douyin.com') return {};
    return Object.fromEntries(keys.map(k => [k, localStorage.getItem(k)]));
}"""


class BrowserLoginError(RuntimeError):
    """Contains only a fixed, safe user-facing explanation."""


def _record(raw):
    try:
        outer = json.loads(raw or "{}")
        value = outer.get("data", {})
        value = json.loads(value) if isinstance(value, str) else value
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def extract_session(cookies, storage):
    """Return a locally validated session or None; never output raw diagnostics.

    This validates format/session binding, not server acceptance. A fresh browser
    profile per attempt prevents keys left over from a different account.
    """
    cookie_map = {}
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".")
        if domain not in ("douyin.com", "www.douyin.com"):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if isinstance(name, str) and isinstance(value, str):
            cookie_map[name] = value
    if not (cookie_map.get("sessionid") or cookie_map.get("sessionid_ss")):
        return None
    protect = _record(storage.get(PROTECT_KEY))
    keys = _record(storage.get(CRYPT_KEY))
    fields = {"ticket": protect.get("ticket"), "ts_sign": protect.get("ts_sign"),
              "client_cert": protect.get("client_cert"), "private_key": keys.get("ec_privateKey")}
    if not all(isinstance(v, str) and v.strip() for v in fields.values()):
        return None
    sign_id = cookie_map.get("bd_ticket_guard_ts_sign_id")
    # Same check as pinned vendor builder/auth.py:ticket_matches_session.
    if sign_id and not fields["ts_sign"].startswith(sign_id):
        return None
    try:
        from ecdsa import SigningKey, NIST256p
        private = fields["private_key"]
        key = (SigningKey.from_pem(private) if "-----BEGIN" in private
               else SigningKey.from_string(bytes.fromhex(private), curve=NIST256p))
        if key.curve != NIST256p:
            return None
    except Exception as exc:
        # Never include a parser exception: it can embed credential input.
        if isinstance(exc, ImportError):
            raise BrowserLoginError("缺少签名依赖，请运行安装真实连接依赖。") from None
        return None
    fields["cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
    return fields


def _verification_response(response):
    parsed = urlsplit(response.url)
    if parsed.scheme != "https" or parsed.hostname != "login.douyin.com":
        return False
    if parsed.path != "/passport/web/check_qrconnect/":
        return False
    try:
        body = response.json()
        data = body.get("data", {})
        return str(body.get("error_code")) == "2046" or (
            isinstance(data, dict) and str(data.get("error_code")) == "2046")
    except Exception:
        return False


def _wait_for_session(context, state, timeout, *, clock=time.monotonic):
    deadline = clock() + timeout
    verification = False

    def on_response(response):
        nonlocal verification
        if _verification_response(response):
            verification = True
            state.update("verification_required", "请在已打开的抖音官方浏览器窗口完成身份验证；完成后会继续接收登录结果。")

    context.on("response", on_response)
    state.update("browser_waiting", "请在打开的抖音官方浏览器中登录；扫码、短信或人脸验证均在官方页面完成。")
    try:
        while clock() < deadline:
            cancelled = getattr(state, "cancel_event", None)
            if cancelled is not None and cancelled.is_set():
                raise BrowserLoginError("已取消本次登录。")
            pages = [page for page in context.pages if not page.is_closed()]
            if not pages:
                raise BrowserLoginError("登录浏览器已关闭，请重新开始登录。")
            for page in pages:
                parsed = urlsplit(page.url)
                if parsed.scheme != "https" or parsed.netloc != "www.douyin.com":
                    continue
                try:
                    storage = page.evaluate(STORAGE_SCRIPT, [PROTECT_KEY, CRYPT_KEY])
                    cookies = context.cookies(ORIGIN + "/")
                except Exception:
                    continue  # Normal during navigation; no raw exception output.
                session = extract_session(cookies, storage)
                if session:
                    return session
                if any(c.get("name") in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies):
                    state.update("browser_waiting", "已检测到网页登录，正在等待配套签名凭证。请在官方页面打开「私信」，无需发送任何消息。")
                elif not verification:
                    state.update("browser_waiting", "请在抖音官方窗口完成登录，完成后本页将自动更新。")
            try:
                pages[0].wait_for_timeout(500)
            except Exception:
                raise BrowserLoginError("登录浏览器已关闭，请重新开始登录。") from None
        raise BrowserLoginError("等待登录超时。请重新开始，并在官方窗口完成全部验证。")
    finally:
        context.remove_listener("response", on_response)


def run_browser_login(state, data, timeout=600):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserLoginError("缺少浏览器登录依赖，请先运行安装真实连接依赖。") from None
    profile_root = (Path(data) / "browser-profile").resolve()
    profile_root.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="login-", dir=profile_root)).resolve()
    context = None
    try:
        with sync_playwright() as playwright:
            for channel in ("msedge", "chrome"):
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), channel=channel, headless=False,
                        viewport=None, no_viewport=True)
                    break
                except Exception:
                    continue
            if context is None:
                raise BrowserLoginError("无法打开 Edge 或 Chrome，请确认已安装浏览器且没有拦截启动。")
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(ORIGIN + "/", wait_until="domcontentloaded", timeout=60000)
                return _wait_for_session(context, state, timeout)
            except BrowserLoginError:
                raise
            except Exception:
                raise BrowserLoginError("官方登录页面无法正常加载，请检查网络后重试。") from None
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        # Only remove the unique profile this attempt created inside our data dir.
        if profile.parent == profile_root and profile.name.startswith("login-"):
            shutil.rmtree(profile, ignore_errors=True)
