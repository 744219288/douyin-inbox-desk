"""Local contract fixture. Never use its sample facts for real customer replies.

Run: .venv/Scripts/python.exe scripts/mock_extensions.py --port 8790
Knowledge URL: http://127.0.0.1:8790/knowledge
Agent URL: http://127.0.0.1:8790/agent
This process does not change workbench settings or contact any outside service.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


def fixture(path, body):
    if path == '/knowledge':
        return {'documents': [{'text': '【接口测试数据】样例营业时间为 09:00—18:00。不可用于真实业务。',
                               'source': '本地接口联调样例'}] if '营业' in body.get('query', '') else []}
    if path == '/agent':
        if body.get('action') == 'analyze':
            users = [m for m in body.get('messages', []) if m.get('role') == 'user']
            last = users[-1] if users else None
            return {'summary': '【接口测试数据】' + (last['text'][:400] if last else '无客户消息'),
                    'evidence': [{'message_id': last['message_id'], 'quote': last['text'][:400]}] if last else []}
        if '人工' in body.get('message', ''):
            return {'handoff': True, 'reason': '【接口测试数据】请求人工接手'}
        return {'context': '【接口测试数据】Agent已收到请求；这不是实际业务查询结果。'}
    raise ValueError('未知测试端点')


def make_mock_server(port=8790):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                    raise ValueError('仅允许本机访问')
                origin = self.headers.get('Origin')
                if origin or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    raise ValueError('仅供本机服务端联调')
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 131072:
                    raise ValueError('请求长度无效')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('需要JSON对象')
                output, status = fixture(self.path, body), 200
            except (ValueError, TypeError, KeyError):
                output, status = {'error': '请求格式或测试端点无效'}, 400
            raw = json.dumps(output, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8790)
    server = make_mock_server(parser.parse_args().port)
    print(f'本地接口联调样例：http://127.0.0.1:{server.server_port}，仅测试数据；Ctrl+C 停止。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
