"""Loopback-only QR login status page; never expose the login URL in JSON."""
import io
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class LoginState:
    def __init__(self):
        self._lock = threading.RLock()
        self._status = "initializing"
        self._message = "正在初始化登录，请稍候"
        self._version = 0
        self._png = None
        self.cancel_event = threading.Event()

    def update(self, status, message):
        with self._lock:
            self._status = str(status)
            self._message = str(message)
            if status != "waiting":
                self._png = None
            self._version += 1

    def on_qrcode(self, url):
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=8, border=4)
        qr.add_data(url)
        qr.make(fit=True)
        output = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(output, format="PNG")
        with self._lock:
            self._png = output.getvalue()
            self._status = "waiting"
            self._message = "请使用抖音 App 扫码，并在手机上确认登录"
            self._version += 1

    def public(self):
        with self._lock:
            return {"status": self._status, "message": self._message,
                    "version": self._version, "has_qr": self._png is not None}

    def qr_png(self):
        with self._lock:
            return self._png


def make_login_server(state, port=8767, controller=None, back_url="http://127.0.0.1:8765/"):
    static = Path(__file__).resolve().parent.parent / "static"
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local_request():
                return
            path = urlsplit(self.path).path
            if path == "/status":
                payload = state.public()
                payload["active"] = bool(controller and controller.active)
                return self.respond(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            if path == "/qr.png":
                png = state.qr_png()
                return self.respond(200 if png else 404, png or b"QR not available", "image/png" if png else "text/plain; charset=utf-8")
            files = {"/": ("login.html", "text/html; charset=utf-8"),
                     "/login.js": ("login.js", "text/javascript; charset=utf-8")}
            if path not in files:
                return self.respond(404, b"Not found", "text/plain; charset=utf-8")
            name, content_type = files[path]
            try:
                body = (static / name).read_bytes()
                if name == "login.html":
                    body = body.replace(b"__LOGIN_TOKEN__", token.encode())
                    body = body.replace(b"http://127.0.0.1:8765/", back_url.encode())
            except OSError:
                return self.respond(500, b"Login page is unavailable", "text/plain; charset=utf-8")
            self.respond(200, body, content_type)

        def local_request(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") not in hosts
                    or (origin and origin not in {"http://" + host for host in hosts})
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.respond(403, b"Forbidden", "text/plain; charset=utf-8")
                return False
            return True

        def do_POST(self):
            if not self.local_request():
                return
            if not secrets.compare_digest(self.headers.get("X-Login-Token", ""), token):
                return self.respond(403, b"Forbidden", "text/plain")
            if not controller:
                return self.respond(503, b"Unavailable", "text/plain")
            path = urlsplit(self.path).path
            if path == "/start":
                if not controller.start():
                    return self.respond(409, b"Already running", "text/plain")
            elif path == "/cancel":
                controller.cancel()
            else:
                return self.respond(404, b"Not found", "text/plain")
            return self.respond(200, b'{"ok":true}', "application/json")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
