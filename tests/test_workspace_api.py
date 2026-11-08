"""Exercise HTTP contracts end to end with temporary data and a local mock only."""
import base64
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import request, error

from desk.integrations import IntegrationError
from desk.service import DeskService
from scripts.mock_extensions import make_mock_server
from server import make_server


class OfflineTransport:
    def disconnect(self):
        pass
    def send(self, *args):
        raise AssertionError('No live message is allowed in contract tests')


class WorkspaceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.desk = DeskService(Path(self.temp.name), lambda *_: 'unused', OfflineTransport())
        self.http = make_server(0, Path(self.temp.name), self.desk)
        self.mock = make_mock_server(0)
        for server in (self.http, self.mock):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'
        self.mockbase = f'http://127.0.0.1:{self.mock.server_port}'
        with request.urlopen(self.base) as response:
            self.token = re.search(r'name="desk-token" content="([^"]+)"', response.read().decode())[1]

    def tearDown(self):
        for server in (self.http, self.mock):
            server.shutdown()
            server.server_close()
        self.desk.close()
        self.desk.pool.shutdown(wait=True)
        self.desk.profile_pool.shutdown(wait=True)
        self.temp.cleanup()

    def call(self, route, payload=None):
        req = request.Request(self.base + route,
            None if payload is None else json.dumps(payload).encode(),
            {'Content-Type': 'application/json', 'X-Desk-Token': self.token})
        with request.urlopen(req) as response:
            return json.load(response)

    def test_knowledge_and_agent_contract_over_real_local_http(self):
        self.call('/api/settings', {'knowledge_url': self.mockbase + '/knowledge', 'agent_url': self.mockbase + '/agent'})
        knowledge = self.call('/api/integrations/test', {'kind': 'knowledge', 'query': '营业时间'})
        self.assertIn('接口测试数据', knowledge['documents'][0]['text'])
        no_match = self.call('/api/integrations/test', {'kind': 'knowledge', 'query': '不匹配'})
        self.assertEqual(no_match['documents'], [])
        agent = self.call('/api/integrations/test', {'kind': 'agent', 'query': '人工'})
        self.assertTrue(agent['handoff'])
        cid = self.call('/api/demo/message', {'text': '人工，我需要20件'})['conversation_id']
        analysis = self.call('/api/conversations/' + cid + '/analyze', {'source': 'agent'})
        self.assertEqual(analysis['workspace']['summary_source'], 'agent')
        self.assertEqual(analysis['workspace']['evidence'][0]['quote'], '人工，我需要20件')
        self.assertEqual([m['role'] for m in analysis['messages']], ['user'])

    def test_crud_customer_and_followup_routes(self):
        created = self.call('/api/workspace/knowledge', {'title': '交付说明', 'content': '以确认的交期为准', 'status': 'draft'})
        item = created['knowledge'][0]
        self.assertEqual(self.call('/api/workspace/knowledge', {'action': 'search', 'query': '交付'})['documents'], [])
        self.call('/api/workspace/knowledge', {'id': item['id'], 'status': 'published'})
        self.assertEqual(len(self.call('/api/workspace/knowledge', {'action': 'search', 'query': '交付'})['documents']), 1)
        cid = self.call('/api/demo/message', {'text': '人工'})['conversation_id']
        route = '/api/conversations/' + cid
        self.call(route + '/customer', {'tags': ['询价'], 'notes': '待核实', 'stage': 'interested',
                  'follow_up': {'at': '2000-01-01T00:00:00Z', 'note': '确认报价'}})
        self.assertEqual(self.call('/api/workspace')['overview']['follow_up_due'], 1)
        result = self.call(route)
        self.assertEqual(result['workspace']['tags'], ['询价'])
        self.assertEqual(result['workspace']['notes'], '待核实')
        self.call('/api/workspace/knowledge', {'id': item['id'], 'action': 'delete'})
        self.assertEqual(self.call('/api/workspace')['knowledge'], [])

    def test_upload_send_and_library_use_actual_api(self):
        cid = self.call('/api/demo/message', {'text': '转人工'})['conversation_id']
        asset = self.call('/api/assets', {'name': '说明.txt', 'data': base64.b64encode(b'test only').decode()})
        library = self.call('/api/workspace/materials', {'title': '说明', 'asset_id': asset['id']})
        self.assertEqual(library['materials'][0]['size'], 9)
        result = self.call('/api/conversations/' + cid + '/send-asset', {'asset_id': asset['id']})
        self.assertEqual(result['messages'][-1]['media_url'], asset['url'])
        self.assertEqual(result['messages'][-1]['status'], 'sent')

    def test_upstream_auth_failure_is_actionable_and_redacted(self):
        with patch.object(self.desk, 'test_integration', side_effect=IntegrationError(401)):
            with self.assertRaises(error.HTTPError) as caught:
                self.call('/api/integrations/test', {'kind': 'agent', 'query': '测试'})
        self.assertEqual(caught.exception.code, 502)
        body = json.load(caught.exception)
        self.assertEqual(body['upstream_status'], 401)
        self.assertIn('鉴权失败', body['error'])

    def test_unconfigured_model_reports_unavailable(self):
        with self.assertRaises(error.HTTPError) as caught:
            self.call('/api/integrations/test', {'kind': 'model', 'query': '测试'})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn('配置', json.load(caught.exception)['error'])

    def test_stale_mode_cannot_create_live_knowledge_or_upload(self):
        self.call('/api/settings', {'mode': 'live'})
        for route, body in [('/api/workspace/knowledge', {'title': '演示资料', 'content': '不能发布到真实环境', 'status': 'published'}),
                            ('/api/assets', {'name': '演示.txt', 'data': base64.b64encode(b'demo only').decode()})]:
            with self.subTest(route=route), self.assertRaises(error.HTTPError) as caught:
                self.call(route, dict(body, expected_mode='demo'))
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.call('/api/workspace')['knowledge'], [])
        self.assertEqual(self.desk.assets.list('live'), [])


if __name__ == '__main__':
    unittest.main()
