"""Run the local workbench: python server.py --port 8765."""
import argparse
import http.client
import json
import mimetypes
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote

from desk.integrations import respond, IntegrationError, IntegrationProtocolError, HandoffRequired
from desk.transport import DouyinTransport
from desk.service import DeskService

ROOT = Path(__file__).resolve().parent


def login_page_ready():
    connection = http.client.HTTPConnection("127.0.0.1", 8767, timeout=0.4)
    try:
        connection.request("GET", "/status")
        response = connection.getresponse()
        if response.status != 200:
            return False
        payload = json.loads(response.read(8192))
        return (isinstance(payload, dict) and isinstance(payload.get("active"), bool)
                and isinstance(payload.get("status"), str)
                and isinstance(payload.get("has_qr"), bool))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def start_login_helper():
    if login_page_ready():
        return {"ok": True, "url": "http://127.0.0.1:8767/"}
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    process = subprocess.Popen([str(python), str(ROOT / "login.py"), "--no-open"],
                               cwd=str(ROOT), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if login_page_ready():
            return {"ok": True, "url": "http://127.0.0.1:8767/"}
        if process.poll() is not None:
            break
        time.sleep(0.15)
    raise ValueError("登录服务未能启动，请运行安装真实连接依赖.cmd后重试，或检查8767端口。")


def make_server(port=8765, data_dir=None, service=None):
    data_dir = Path(data_dir or ROOT / "data")
    desk = service or DeskService(data_dir, respond, DouyinTransport(data_dir))
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Do not log query strings, messages or credentials.

        def _headers(self, code, kind, length):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://*.douyinpic.com https://*.douyinstatic.com https://*.byteimg.com https://*.ibytedtos.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def _json(self, data, code=200):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self._headers(code, "application/json; charset=utf-8", len(raw))
            self.wfile.write(raw)

        def _local(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in allowed or (origin and origin not in {"http://" + item for item in allowed}):
                self._json({"error": "仅允许本机同源访问"}, 403)
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._json({"error": "拒绝跨站请求"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/state":
                    return self._json(desk.state())
                if path == '/api/workspace':
                    return self._json(desk.workspace_state())
                if path.startswith('/api/incoming-media/'):
                    raw, mime = desk.incoming_image(path[len('/api/incoming-media/'):])
                    self._headers(200, mime, len(raw))
                    self.wfile.write(raw)
                    return
                if path.startswith('/api/assets/'):
                    aid = path[len('/api/assets/'):]
                    with desk.lock:
                        mode = desk.settings['mode']
                        asset = desk.assets.get(aid, mode)
                        raw = desk.assets.path(aid, mode).read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', asset['mime'])
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Cache-Control', 'no-store')
                    disposition = 'inline' if asset['kind'] == 'image' else 'attachment'
                    self.send_header('Content-Disposition', disposition + "; filename*=UTF-8''" + quote(asset['name']))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path.startswith("/api/conversations/"):
                    cid = unquote(path[len("/api/conversations/"):])
                    return self._json(desk.conversation(cid))
                files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/emoji-map.js": "emoji-map.js", "/styles.css": "styles.css"}
                if path not in files:
                    return self._json({"error": "页面不存在"}, 404)
                file = ROOT / "static" / files[path]
                raw = file.read_bytes()
                if file.name == "index.html":
                    raw = raw.replace(b"__DESK_TOKEN__", token.encode())
                kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                self._headers(200, kind + "; charset=utf-8", len(raw))
                self.wfile.write(raw)
            except (ValueError, KeyError):
                self._json({"error": "会话不存在"}, 404)
            except Exception:
                self._json({"error": "读取失败，请检查本地服务"}, 500)

        def do_POST(self):
            if not self._local():
                return
            if not secrets.compare_digest(self.headers.get("X-Desk-Token", ""), token):
                return self._json({"error": "页面凭证已失效，请刷新页面"}, 403)
            if self.headers.get_content_type() != "application/json":
                return self._json({"error": "请使用 JSON 请求"}, 415)
            try:
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                limit = 14 * 1024 * 1024 if path == '/api/assets' else 131072
                if length < 0 or length > limit:
                    return self._json({"error": "请求内容过大"}, 413)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求内容必须是对象")
                path = urlsplit(self.path).path
                if path == "/api/settings":
                    result = desk.update_settings(body)
                elif path == '/api/assets':
                    with desk.lock:
                        if 'expected_mode' in body and body['expected_mode'] != desk.settings['mode']:
                            raise ValueError('运行模式已变化，请刷新后重新上传')
                        result = desk.assets.put(desk.settings['mode'], body.get('name'), body.get('data'))
                elif path.startswith('/api/workspace/'):
                    result = desk.workspace_action(path[len('/api/workspace/'):], body)
                elif path == '/api/integrations/test':
                    result = desk.test_integration(body.get('kind'), body.get('query', ''))
                elif path == "/api/login":
                    result = start_login_helper()
                elif path == "/api/connect":
                    result = desk.connect()
                elif path == "/api/disconnect":
                    result = desk.disconnect()
                elif path == "/api/strangers/refresh":
                    result = desk.refresh_strangers()
                elif path == "/api/demo/message":
                    if not isinstance(body.get("text"), str):
                        raise ValueError("请输入消息文字")
                    result = desk.demo_message(body["text"], body.get("conversation_id"))
                elif path.startswith("/api/conversations/"):
                    route = path[len("/api/conversations/"):].rsplit("/", 1)
                    if len(route) != 2:
                        return self._json({"error": "接口不存在"}, 404)
                    cid, action = unquote(route[0]), route[1]
                    if action == "takeover":
                        result = desk.takeover(cid)
                    elif action == "resume":
                        result = desk.resume(cid)
                    elif action == "send":
                        if not isinstance(body.get("text"), str):
                            raise ValueError("请输入消息文字")
                        result = desk.send(cid, body["text"])
                    elif action == 'send-asset':
                        from desk.assets import send_asset
                        result = send_asset(desk, cid, body.get('asset_id'))
                    elif action == 'customer':
                        result = desk.customer_update(cid, body)
                    elif action == 'analyze':
                        result = desk.analyze(cid, body)
                    elif action == 'handoff':
                        result = desk.handoff(cid, body.get('reason', '手动转交人工'))
                    else:
                        return self._json({"error": "接口不存在"}, 404)
                else:
                    return self._json({"error": "接口不存在"}, 404)
                self._json(result if result is not None else {"ok": True})
            except IntegrationError as exc:
                self._json({"error": str(exc), "upstream_status": exc.status}, 502)
            except IntegrationProtocolError as exc:
                self._json({"error": str(exc)}, 502)
            except HandoffRequired as exc:
                self._json({"error": str(exc)}, 422)
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)[:200]}, 400)
            except Exception:
                self._json({"error": "操作失败，请检查连接状态和运行记录"}, 500)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.desk = desk
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    server = make_server(args.port, args.data_dir)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"客服面板已启动：{url}\n默认不会连接抖音；按 Ctrl+C 停止服务。", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server.desk.close()


if __name__ == "__main__":
    main()
