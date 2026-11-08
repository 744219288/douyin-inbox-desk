import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib import request, error
from desk.assets import AssetStore, send_asset
from desk.service import DeskService
from desk.transport import DouyinTransport
from server import make_server


class OfflineTransport:
    def __init__(self):
        self.sent = []
        self.fail = False
    def disconnect(self):
        pass
    def send_asset(self, cid, path, kind):
        if self.fail:
            raise RuntimeError('private-token-do-not-leak')
        self.sent.append((cid, path.read_bytes(), kind))


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = OfflineTransport()
        self.desk = DeskService(Path(self.tmp.name), lambda *_: 'unused', self.transport)
        self.desk.assets = AssetStore(Path(self.tmp.name))
        self.addCleanup(self.desk.close)

    def test_persist_name_and_mode_isolation(self):
        item = self.desk.assets.put('demo', '规格.txt', b'hello')
        reopened = AssetStore(Path(self.tmp.name))
        self.assertEqual(reopened.path(item['id'], 'demo').read_bytes(), b'hello')
        self.assertEqual(reopened.get(item['id'], 'demo')['name'], '规格.txt')
        with self.assertRaises(ValueError):
            reopened.get(item['id'], 'live')

    def test_invalid_uploads_and_traversal(self):
        for name, data in [('../a.txt', b'a'), ('test.html', b'bad'), ('a.png', b'fake'),
                           ('AUX.txt', b'a'), ('a.txt', b''), ('a.txt', '%%%')]:
            with self.subTest(name=name, data=data), self.assertRaises(ValueError):
                self.desk.assets.put('demo', name, data)
        with self.assertRaises(ValueError):
            self.desk.assets.get('../x', 'demo')

    def test_demo_records_attachment_without_transport(self):
        cid = self.desk.demo_message('转人工')['conversation_id']
        item = self.desk.assets.put('demo', '说明.txt', b'hello')
        result = send_asset(self.desk, cid, item['id'])
        self.assertEqual(result['messages'][-1]['status'], 'sent')
        self.assertEqual(result['messages'][-1]['media_url'], item['url'])
        self.assertEqual(result['conversation']['status'], 'human')
        self.assertEqual(self.transport.sent, [])

    def live_conversation(self):
        self.desk.update_settings({'mode': 'live'})
        with self.desk.lock:
            self.desk._receive('live', 'recipient', '转人工', '1', None, schedule_reply=False)
            self.desk.connection = {'status': 'connected', 'detail': 'test'}
        return 'live:recipient'

    def test_real_adapter_receives_selected_recipient_and_failures_do_not_retry(self):
        cid = self.live_conversation()
        item = self.desk.assets.put('live', '说明.txt', b'hello')
        send_asset(self.desk, cid, item['id'])
        self.assertEqual(self.transport.sent, [('recipient', b'hello', 'file')])
        self.transport.fail = True
        with self.assertRaises(ValueError):
            send_asset(self.desk, cid, item['id'])
        result = self.desk.conversation(cid)
        self.assertEqual(result['messages'][-1]['status'], 'failed')
        self.assertEqual(result['conversation']['status'], 'waiting')
        self.assertNotIn('private-token', json.dumps(result))
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.desk.active_sends, 0)

    def test_disconnected_does_not_invoke_upload(self):
        cid = self.live_conversation()
        self.desk.connection['status'] = 'disconnected'
        item = self.desk.assets.put('live', '说明.txt', b'hello')
        with self.assertRaises(ValueError):
            send_asset(self.desk, cid, item['id'])
        self.assertEqual(self.transport.sent, [])

    def test_http_upload_requires_token_and_download_enforces_mode(self):
        http = make_server(0, Path(self.tmp.name), self.desk)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        base = 'http://127.0.0.1:' + str(http.server_port)
        payload = json.dumps({'name': '说明.txt', 'data': base64.b64encode(b'hello').decode()}).encode()
        with self.assertRaises(error.HTTPError) as failure:
            request.urlopen(request.Request(base + '/api/assets', payload, {'Content-Type': 'application/json'}))
        self.assertEqual(failure.exception.code, 403)
        item = self.desk.assets.put('demo', '说明.txt', b'hello')
        with request.urlopen(base + item['url']) as response:
            self.assertEqual(response.read(), b'hello')
            self.assertIn('attachment', response.headers['Content-Disposition'])
        self.desk.update_settings({'mode': 'live'})
        with self.assertRaises(error.HTTPError) as failure:
            request.urlopen(base + item['url'])
        self.assertEqual(failure.exception.code, 404)

    def test_transport_uses_upstream_media_and_requires_confirmation(self):
        calls = []
        class Api:
            @staticmethod
            def send_file(*args, **kwargs):
                calls.append((args, kwargs))
                return False
        transport = DouyinTransport(Path(self.tmp.name))
        auth = object()
        transport._auth, transport._connected = auth, True
        transport.prepare = lambda _: (transport._epoch, auth, Api, 'cid', 1, 'ticket')
        with self.assertRaises(RuntimeError):
            transport.send_asset('cid', Path('说明.txt'), 'file')
        self.assertEqual(calls[0][0][1], 'cid')
        self.assertEqual(calls[0][0][-1], '说明.txt')
        self.assertTrue(calls[0][1]['client_message_id'])


if __name__ == '__main__':
    unittest.main()
