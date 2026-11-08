"""Explicit, user-run QR login helper. Does not send private messages."""
import argparse
import json
import os
import re
import threading
import time
import traceback
import webbrowser
from desk.transport import load_vendor, ROOT


def safe_error(exc):
    text = str(exc).lower()
    if isinstance(exc, ImportError) or "缺少抖音" in text:
        return "缺少登录依赖，请先运行“安装真实连接依赖.cmd”。"
    if "certificate" in text or "ssl" in text or "curl: (60)" in text:
        return "HTTPS 证书校验失败，请检查系统时间或代理的证书配置。"
    if "resolve" in text or "curl: (6)" in text:
        return "无法解析抖音服务器地址，请检查网络和 DNS。"
    if "timeout" in text or "timed out" in text or "curl: (28)" in text:
        return "登录请求或扫码等待超时，请检查网络后重新打开扫码登录。"
    if "error_code=7" in text or "访问太频繁" in text:
        return "抖音返回访问过于频繁，请等待几分钟再重试。"
    if "获取二维码失败" in text:
        return "抖音未返回可用二维码，可能是接口变化或当前网络受限。"
    codes = re.findall(r"error_code[\s'\":=]+(\d+)", text)
    if "2046" in codes:
        return "抖音要求进一步验证（2046）。请使用浏览器登录，在官方页面完成身份验证；重复扫描此二维码不能完成这一步。"
    if codes:
        return "抖音登录接口返回错误码 " + codes[0] + "，本次未完成登录。请稍后重试。"
    return "登录未完成（" + type(exc).__name__ + "），已记录不含凭证的诊断信息。"


def http_login_worker(state, data):
    started = time.monotonic()
    stage = "检查依赖"
    try:
        Auth, *_ = load_vendor()
        from dy_apis.login_api import DYLoginApi
        original_bootstrap = DYLoginApi.bootstrap_auth
        original_get_qrcode = DYLoginApi.get_qrcode

        def bootstrap(*args, **kwargs):
            nonlocal stage
            stage = "初始化登录"
            state.update("starting", "正在初始化抖音登录，可能需要几十秒，请保持页面打开…")
            return original_bootstrap(*args, **kwargs)

        def get_qrcode(api, *args, **kwargs):
            nonlocal stage
            if state.cancel_event.is_set():
                raise RuntimeError("已取消")
            if time.monotonic() - started > 300:
                raise TimeoutError()
            stage = "获取二维码"
            state.update("starting", "正在向抖音获取二维码…")
            return original_get_qrcode(api, *args, **kwargs)

        def on_qrcode(url):
            nonlocal stage
            if state.cancel_event.is_set():
                raise RuntimeError("已取消")
            if time.monotonic() - started > 300:
                raise TimeoutError()
            stage = "等待扫码"
            state.on_qrcode(url)

        DYLoginApi.bootstrap_auth = staticmethod(bootstrap)
        DYLoginApi.get_qrcode = get_qrcode
        try:
            auth = Auth.from_qrcode_login(bootstrap_creator=False, show_qr=False, on_qrcode=on_qrcode)
        finally:
            DYLoginApi.bootstrap_auth = staticmethod(original_bootstrap)
            DYLoginApi.get_qrcode = original_get_qrcode
        if time.monotonic() - started > 360:
            raise TimeoutError()
        if not auth.ticket or not auth.private_key:
            raise RuntimeError("本次登录未获得配套私信凭证，请重新登录")
        if state.cancel_event.is_set():
            state.update("cancelled", "已取消登录，未保存新凭证。")
            return
        result = {"cookie": auth.cookie_str, "ticket": auth.ticket, "ts_sign": auth.ts_sign,
                  "client_cert": auth.client_cert, "private_key": auth.private_key,
                  "dtrait_blob": auth.dtrait_blob}
        path = data / "douyin-session.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        state.update("success", "登录凭证已保存。回客服面板配置 DeepSeek，选择真实模式后点击连接账号。")
    except Exception as exc:
        if state.cancel_event.is_set():
            state.update("cancelled", "已取消登录，未保存新凭证。")
            return
        diagnostic = {"stage": stage, "type": type(exc).__name__, "message": safe_error(exc),
                      "elapsed_seconds": round(time.monotonic() - started, 1),
                      "numeric_codes": re.findall(r"(?:error_code[\s'\":=]+|curl: \()(\d+)", str(exc)),
                      "stack": [{"file": f.filename, "line": f.lineno, "function": f.name}
                                for f in traceback.extract_tb(exc.__traceback__)]}
        (data / "login-diagnostic.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        status = "verification_required" if "2046" in diagnostic["numeric_codes"] else "error"
        state.update(status, stage + "未完成：" + safe_error(exc))


def login_worker(state, data, method="browser"):
    if method == "http":
        return http_login_worker(state, data)
    from desk.browser_login import run_browser_login, BrowserLoginError
    started = time.monotonic()
    try:
        result = run_browser_login(state, data)
        if state.cancel_event.is_set():
            state.update("cancelled", "已取消登录，未保存新凭证。")
            return
        path = data / "douyin-session.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        state.update("success", "登录凭证已保存。返回客服面板，点击连接账号；私信连接状态将单独确认。")
    except Exception as exc:
        # Never print a browser exception: it may contain a credential URL.
        message = str(exc) if isinstance(exc, BrowserLoginError) else safe_error(exc)
        status = "cancelled" if state.cancel_event.is_set() else "error"
        state.update(status, message)
        diagnostic = {"stage": "浏览器登录", "type": type(exc).__name__, "message": message,
                      "elapsed_seconds": round(time.monotonic() - started, 1)}
        (data / "login-diagnostic.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")


class LoginController:
    def __init__(self, state, data, method="browser"):
        self.state, self.data, self.method = state, data, method
        self.lock = threading.Lock()
        self.active = False

    def start(self):
        with self.lock:
            if self.active:
                return False
            self.active = True
            self.state.cancel_event.clear()
            self.state.update("starting", "正在打开抖音官方登录窗口…")
            threading.Thread(target=self._run, daemon=True).start()
            return True

    def _run(self):
        try:
            login_worker(self.state, self.data, self.method)
        finally:
            with self.lock:
                self.active = False

    def cancel(self):
        self.state.cancel_event.set()


def main():
    from desk.login_page import LoginState, make_login_server
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--method", choices=("browser", "http"), default="browser")
    args = parser.parse_args()
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    os.chdir(data)
    state = LoginState()
    controller = LoginController(state, data, args.method)
    try:
        server = make_login_server(state, controller=controller)
    except OSError:
        print("登录页面可能已打开，请访问 http://127.0.0.1:8767 。", flush=True)
        if not args.no_open:
            webbrowser.open("http://127.0.0.1:8767")
        return 1
    print("扫码登录页面：http://127.0.0.1:8767\n请保持程序运行；Ctrl+C 关闭。", flush=True)
    controller.start()
    if not args.no_open:
        webbrowser.open("http://127.0.0.1:8767")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.cancel()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
