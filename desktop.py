"""Windows desktop entry point. Runtime data stays beside the executable."""
import ctypes
import json
import os
from pathlib import Path
import sys
import threading
import tempfile


def main():
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    bundle = Path(__file__).parent
    os.environ['PATH'] = str(bundle / 'playwright' / 'driver') + os.pathsep + os.environ.get('PATH', '')
    # Windowed executables have no stdout/stderr; dependencies expect streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    import server
    from desk.transport import load_vendor
    from desk.login_page import LoginState, make_login_server
    from login import LoginController

    if '--self-test' in sys.argv:
        # Offline packaging smoke test: never use account data or connect.
        import urllib.request
        from playwright.sync_api import sync_playwright
        import webview
        from webview.platforms import winforms
        load_vendor()
        from desk.incoming_media import browser_image
        assert browser_image((bundle / 'assets' / 'heic-smoke.heic').read_bytes())[1] == 'image/jpeg'
        with sync_playwright() as p:
            assert p.chromium.executable_path
        with tempfile.TemporaryDirectory() as scratch:
            app = server.make_server(0, Path(scratch))
            thread = threading.Thread(target=app.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{app.server_port}/') as response:
                    assert b'app.js' in response.read()
                ready = threading.Event()
                window = webview.create_window('Desktop smoke test', f'http://127.0.0.1:{app.server_port}/', hidden=True)
                def loaded():
                    if 'Douyin Desk' in window.evaluate_js('document.title'):
                        ready.set()
                    window.destroy()
                window.events.loaded += loaded
                def timeout():
                    if not ready.wait(20):
                        window.destroy()
                threading.Thread(target=timeout, daemon=True).start()
                webview.start(gui='edgechromium', private_mode=True)
                assert ready.is_set(), 'WebView did not render the panel'
            finally:
                app.shutdown()
                app.server_close()
                app.desk.close()
        (root / 'desktop-self-test.json').write_text(json.dumps({'ok': True, 'checks': ['vendor imports', 'playwright driver', 'WebView2 rendered panel', 'http assets']}), encoding='utf-8')
        return

    # One desktop owner per Windows session, to prevent duplicate replies.
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\DouyinDeskDesktop')
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(None, '私信工作台已经打开，请切换到已有窗口。', '私信工作台', 64)
        return
    import webview
    data = root / 'data'
    data.mkdir(exist_ok=True)
    app = server.make_server(0, data)
    url = f'http://127.0.0.1:{app.server_port}/'
    state = LoginState()
    controller = LoginController(state, data)
    login_server = make_login_server(state, port=0, controller=controller, back_url=url)
    login_url = f'http://127.0.0.1:{login_server.server_port}/'
    login_window = [None]
    login_lock = threading.Lock()

    def open_login():
        with login_lock:
            if login_window[0] is None:
                window = webview.create_window('登录抖音', login_url, width=680, height=820)
                login_window[0] = window
                def closed():
                    controller.cancel()
                    login_window[0] = None
                window.events.closed += closed
                controller.start()
            else:
                login_window[0].show()
        return {'ok': True, 'url': login_url, 'desktop': True}

    server.start_login_helper = open_login
    for http_server in (app, login_server):
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
    window = webview.create_window('私信工作台 · Douyin Desk', url, width=1380, height=900, min_size=(1000, 680))
    def close_main():
        controller.cancel()
        app.desk.disconnect()
        if login_window[0] is not None:
            login_window[0].destroy()
    window.events.closed += close_main
    try:
        webview.start(gui='edgechromium', private_mode=True)
    finally:
        controller.cancel()
        for http_server in (app, login_server):
            http_server.shutdown()
            http_server.server_close()
        app.desk.close()
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid disclosing credentials from upstream exception strings.
        root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        (root / 'desktop-error.txt').write_text(type(exc).__name__, encoding='utf-8')
        if '--self-test' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, '桌面程序启动失败。请确认已安装 Microsoft Edge WebView2，且程序目录可写。错误类型：' + type(exc).__name__, '私信工作台', 16)
        sys.exit(1)
