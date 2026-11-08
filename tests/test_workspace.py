import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from desk.service import DeskService
from desk.integrations import respond, post_json, HandoffRequired, IntegrationProtocolError
from urllib.error import URLError


class Transport:
    def __init__(self):
        self.sent = []

    def connect(self, settings, receive, status):
        status('connected')

    def disconnect(self):
        pass

    def send(self, cid, text):
        self.sent.append((cid, text))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.transport = Transport()
        self.desk = DeskService(Path(self.tmp.name), lambda *_: '回复', self.transport)
        self.desk._queue_profile = lambda cid: None

    def tearDown(self):
        self.desk.close()
        self.desk.pool.shutdown(wait=True)
        self.desk.profile_pool.shutdown(wait=True)
        self.tmp.cleanup()

    def cid(self, text='请人工确认营业时间'):
        return self.desk.demo_message(text)['conversation_id']

    def knowledge(self, title='营业时间', content='每天9点至18点营业', **extras):
        result = self.desk.workspace_action('knowledge', dict(action='save', title=title,
            content=content, source='店铺手册', **extras))
        return next(i for i in result['knowledge'] if i['title'] == title)

    def pending_reply(self, render):
        self.desk.update_settings({'mode': 'live', 'deepseek_api_key': 'test-key'})
        self.desk.connect()
        entered, release = threading.Event(), threading.Event()
        calls = []
        self.addCleanup(release.set)

        def responder(settings, history, text):
            calls.append(settings)
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            return render(settings)

        self.desk.responder = responder
        return entered, release, calls

    def test_knowledge_revision_replaces_pending_reply_with_current_content(self):
        entered, release, calls = self.pending_reply(lambda s: s['_local_knowledge'][0]['text'])
        item = self.knowledge(content='营业时间：18点关门', status='published')
        self.desk.receive('c', 'u', '营业时间', 'm')
        self.assertTrue(entered.wait(1))
        self.desk.workspace_action('knowledge', {'id': item['id'], 'content': '营业时间：20点关门'})
        release.set()
        self.desk.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [('c', '营业时间：20点关门')])

    def test_knowledge_withdrawal_cancels_pending_use_of_removed_fact(self):
        self.assert_knowledge_removal_replaces_reply({'status': 'draft'})

    def test_knowledge_internal_visibility_cancels_pending_public_use(self):
        self.assert_knowledge_removal_replaces_reply({'audience': 'internal'})

    def test_knowledge_delete_cancels_pending_use(self):
        self.assert_knowledge_removal_replaces_reply({'action': 'delete'})

    def assert_knowledge_removal_replaces_reply(self, change):
        entered, release, calls = self.pending_reply(
            lambda s: s['_local_knowledge'][0]['text'] if s['_local_knowledge'] else '资料待确认')
        item = self.knowledge(content='营业时间：18点关门', status='published')
        self.desk.receive('c', 'u', '营业时间', 'm')
        self.assertTrue(entered.wait(1))
        self.desk.workspace_action('knowledge', dict(change, id=item['id']))
        release.set()
        self.desk.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [('c', '资料待确认')])

    def test_customer_profile_revision_replaces_pending_context(self):
        entered, release, calls = self.pending_reply(
            lambda s: s['_customer_context']['profile'] or '旧资料')
        self.desk.receive('c', 'u', '请问', 'm')
        self.assertTrue(entered.wait(1))
        self.desk.customer_update('live:c', {'profile': '客户确认需要纸箱'})
        release.set()
        self.desk.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [('c', '客户确认需要纸箱')])

    def test_saved_analysis_replaces_pending_context(self):
        entered, release, calls = self.pending_reply(lambda s: s['_customer_context']['summary'])
        self.desk.receive('c', 'u', '需要纸箱', 'm')
        self.assertTrue(entered.wait(1))
        mid = self.desk.conversation('live:c')['messages'][0]['id']
        with patch('desk.integrations.analyze_customer', return_value={
                'summary': '新分析：需要纸箱，数量待确认', 'summary_source': 'model',
                'evidence': [{'message_id': mid, 'quote': '需要纸箱'}]}):
            self.desk.analyze('live:c', {'source': 'model'})
        release.set()
        self.desk.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [('c', '新分析：需要纸箱，数量待确认')])

    def test_reference_edits_do_not_replay_already_committed_send(self):
        self.desk.update_settings({'mode': 'live', 'deepseek_api_key': 'test-key'})
        self.desk.connect()
        item = self.knowledge(status='published')
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        deliver = self.desk._deliver

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return deliver(*args, **kwargs)

        self.desk._deliver = blocked
        self.desk.receive('c', 'u', '营业时间', 'm')
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.desk.conversation('live:c')['messages'][-1]['status'], 'sending')
        self.desk.workspace_action('knowledge', {'id': item['id'], 'audience': 'internal'})
        self.desk.customer_update('live:c', {'profile': '新资料', 'stage': 'interested'})
        with patch('desk.integrations.analyze_customer', return_value={
                'summary': '新分析', 'summary_source': 'model', 'evidence': []}):
            self.desk.analyze('live:c', {'source': 'model'})
        release.set()
        self.desk.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [('c', '回复')])
        self.assertEqual(len(self.desk.conversation('live:c')['messages']), 2)

    def test_invalid_enum_values_are_rejected_without_mutating_workspace(self):
        cid = self.cid()
        for body in ({'stage': []}, {'stage': {}}, {'stage': 4}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.desk.customer_update(cid, body)
        for body in ({'status': []}, {'audience': {}}, {'action': []}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.desk.workspace_action('knowledge', dict(title='title', content='text', **body))
        self.assertEqual(self.desk.workspace_state()['knowledge'], [])
        self.assertEqual(self.desk.conversation(cid)['workspace']['stage'], 'new')

    def test_protocol_errors_have_fixed_redacted_diagnostics(self):
        with patch('desk.integrations.request.build_opener') as opener:
            opener.return_value.open.side_effect = URLError('secret-upstream-token')
            with self.assertRaises(IntegrationProtocolError) as caught:
                post_json('https://example.test', {})
            self.assertNotIn('secret-upstream-token', str(caught.exception))
        for raw in (b'not-json-secret-upstream-token', b'[]', b'x' * 1_048_577):
            with self.subTest(size=len(raw)), patch('desk.integrations.request.build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = raw
                with self.assertRaises(IntegrationProtocolError) as caught:
                    post_json('https://example.test', {})
                self.assertNotIn('secret-upstream-token', str(caught.exception))

    def test_workspace_creation_rejects_stale_expected_mode(self):
        self.desk.update_settings({'mode': 'live'})
        with self.assertRaisesRegex(ValueError, '模式'):
            self.desk.workspace_action('knowledge', {'expected_mode': 'demo', 'title': '误存', 'content': '演示资料'})
        self.assertEqual(self.desk.workspace_state()['knowledge'], [])
        self.desk.workspace_action('knowledge', {'expected_mode': 'live', 'title': '当前资料', 'content': '真实资料'})
        self.assertEqual(len(self.desk.workspace_state()['knowledge']), 1)

    def test_agent_analysis_without_evidence_cannot_become_customer_memory(self):
        cid = self.cid('人工，需要纸箱')
        self.desk.update_settings({'agent_url': 'https://agent.example'})
        with patch('desk.integrations.post_json', return_value={'reply': '客户预算100万元'}):
            with self.assertRaisesRegex(ValueError, '原文'):
                self.desk.analyze(cid, {'source': 'agent'})
        self.assertEqual(self.desk.conversation(cid)['workspace']['summary_source'], 'local')
        mid = self.desk.conversation(cid)['messages'][0]['id']
        with patch('desk.integrations.post_json', return_value={'summary': '客户需要纸箱',
                'evidence': [{'message_id': mid, 'quote': '需要纸箱'}]}):
            result = self.desk.analyze(cid, {'source': 'agent'})
        self.assertEqual(result['workspace']['summary_source'], 'agent')
        self.assertEqual(result['workspace']['evidence'][0]['message_id'], mid)

    def test_legacy_unverified_analysis_is_not_reply_context(self):
        cid = self.cid()
        customer = self.desk.workspace.customer(cid)
        customer.update(summary='客户预算100万元', summary_source='agent', evidence=[])
        context = self.desk._customer_context(customer)
        self.assertEqual(context['summary'], '')
        self.assertTrue(context['recent_messages'])

    def test_long_knowledge_returns_matching_passage_with_source(self):
        content = '基础资料。' * 700 + '\n特殊售后政策：签收后7天内可退换。' + '其他资料。' * 100
        item = self.knowledge(title='店铺手册', content=content, status='published')
        document = self.desk.test_integration('local', '特殊售后政策')['documents'][0]
        self.assertEqual(document['id'], item['id'])
        self.assertIn('签收后7天内可退换', document['text'])
        self.assertLessEqual(len(document['text']), 2500)
        self.assertIn(document['text'], content)
        self.assertEqual(document['source'], '店铺手册')

    def test_manual_reply_accepts_handoff_and_resume_resolves_it(self):
        cid = self.cid()
        self.desk.handoff(cid, '库存需确认')
        result = self.desk.send(cid, '我来确认库存')
        self.assertEqual(result['conversation']['status'], 'human')
        self.assertEqual(result['workspace']['handoff']['status'], 'accepted')
        self.assertTrue(result['workspace']['handoff']['accepted_at'])
        resumed = self.desk.resume(cid)
        self.assertEqual(resumed['conversation']['status'], 'ai')
        self.assertEqual(resumed['workspace']['handoff']['status'], 'resolved')
        self.assertEqual(resumed['workspace']['handoff']['reason'], '库存需确认')
        self.assertTrue(resumed['workspace']['handoff']['resolved_at'])

    def test_only_published_public_knowledge_is_retrieved_and_versioned(self):
        draft = self.knowledge()
        self.assertEqual(self.desk.test_integration('local', '营业时间')['documents'], [])
        self.desk.workspace_action('knowledge', {'id': draft['id'], 'status': 'published'})
        docs = self.desk.test_integration('local', '营业时间')['documents']
        self.assertEqual([d['id'] for d in docs], [draft['id']])
        self.assertEqual(docs[0]['version'], 2)
        self.desk.workspace_action('knowledge', {'id': draft['id'], 'audience': 'internal'})
        self.assertEqual(self.desk.test_integration('local', '营业时间')['documents'], [])

    def test_workspace_resources_persist_and_are_mode_isolated(self):
        item = self.knowledge(status='published')
        self.desk.workspace_action('replies', {'title': '欢迎', 'content': '您好，请问需要什么帮助？'})
        cid = self.cid()
        self.desk.customer_update(cid, {'profile': '客户明确说需要纸箱', 'stage': 'interested', 'tags': ['纸箱']})
        self.desk.close()
        self.desk.pool.shutdown(wait=True)
        self.desk = DeskService(Path(self.tmp.name), lambda *_: '回复', self.transport)
        self.assertEqual(len(self.desk.workspace_state()['knowledge']), 1)
        self.assertEqual(self.desk.conversation(cid)['workspace']['stage'], 'interested')
        self.desk.update_settings({'mode': 'live'})
        self.assertEqual(self.desk.workspace_state()['knowledge'], [])
        for action in ('save', 'delete'):
            with self.assertRaises(ValueError):
                self.desk.workspace_action('knowledge', {'action': action, 'id': item['id'], 'title': 'cross-mode'})
        with self.assertRaises(ValueError):
            self.desk.customer_update(cid, {'notes': '不得跨模式'})

    def test_customer_validation_is_atomic_and_due_counts_are_real(self):
        cid = self.cid()
        before = self.desk.conversation(cid)['workspace']
        with self.assertRaises(ValueError):
            self.desk.customer_update(cid, {'profile': '不应保存', 'stage': 'invented'})
        self.assertEqual(self.desk.conversation(cid)['workspace'], before)
        with self.assertRaises(ValueError):
            self.desk.customer_update(cid, {'follow_up': {'at': '2026-01-01T10:00:00'}})
        self.desk.customer_update(cid, {'tags': ['询价', '询价'], 'stage': 'negotiating',
            'follow_up': {'at': '2000-01-01T10:00:00+08:00', 'note': '确认报价'}})
        self.assertEqual(self.desk.workspace_state()['overview']['follow_up_due'], 1)
        row = self.desk.state()['conversations'][0]
        self.assertEqual(row['tags'], ['询价'])
        self.assertEqual(row['stage'], 'negotiating')
        self.desk.customer_update(cid, {'follow_up': {'done': True}})
        self.assertEqual(self.desk.workspace_state()['overview']['follow_up_due'], 0)
        self.assertEqual(self.transport.sent, [])

    def test_material_validates_asset_ownership_and_keeps_server_metadata(self):
        asset = self.desk.assets.put('demo', '说明.txt', b'example')
        state = self.desk.workspace_action('materials', {'title': '说明资料', 'asset_id': asset['id']})
        item = state['materials'][0]
        self.assertEqual(item['name'], '说明.txt')
        self.assertEqual(item['size'], 7)
        self.desk.update_settings({'mode': 'live'})
        with self.assertRaises(ValueError):
            self.desk.workspace_action('materials', {'title': '跨模式资料', 'asset_id': asset['id']})
        self.assertEqual(self.desk.workspace_state()['materials'], [])

    def test_local_memory_and_handoff_use_actual_customer_quotes(self):
        cid = self.cid('人工，我需要10个纸箱')
        customer = self.desk.conversation(cid)['workspace']
        self.assertEqual(customer['summary_source'], 'local')
        self.assertIn('10个纸箱', customer['handoff']['context'])
        result = self.desk.handoff(cid, '请核对纸箱库存')
        self.assertEqual(result['conversation']['status'], 'waiting')
        self.assertEqual(result['workspace']['handoff']['reason'], '请核对纸箱库存')
        self.assertEqual(result['workspace']['evidence'][0]['quote'], '人工，我需要10个纸箱')
        accepted = self.desk.takeover(cid)
        self.assertEqual(accepted['conversation']['status'], 'human')
        self.assertEqual(accepted['workspace']['handoff']['reason'], '请核对纸箱库存')
        self.assertEqual(self.transport.sent, [])

    def test_local_analysis_never_calls_external_service(self):
        cid = self.cid()
        with patch('desk.integrations.post_json') as post:
            result = self.desk.analyze(cid)
        post.assert_not_called()
        self.assertEqual(result['workspace']['summary_source'], 'local')
        self.assertEqual(result['workspace']['profile'], '')

    def test_model_analysis_requires_config_and_validates_evidence(self):
        cid = self.cid('人工，我需要10个纸箱')
        with patch('desk.integrations.post_json') as post:
            with self.assertRaises(ValueError):
                self.desk.analyze(cid, {'source': 'model'})
            post.assert_not_called()
        self.desk.update_settings({'deepseek_api_key': 'test-key'})
        mid = self.desk.conversation(cid)['messages'][0]['id']
        def response(quote):
            return {'choices': [{'message': {'content': json.dumps({'summary': '客户需要10个纸箱，库存待确认',
                'evidence': [{'message_id': mid, 'quote': quote}]})}, 'finish_reason': 'stop'}]}
        with patch('desk.integrations.post_json', return_value=response('客户预算100元')):
            with self.assertRaisesRegex(ValueError, '原文不一致'):
                self.desk.analyze(cid, {'source': 'model'})
        self.assertEqual(self.desk.conversation(cid)['workspace']['summary_source'], 'local')
        with patch('desk.integrations.post_json', return_value=response('我需要10个纸箱')):
            result = self.desk.analyze(cid, {'source': 'model'})
        self.assertEqual(result['workspace']['summary_source'], 'model')
        self.assertEqual(result['workspace']['stage'], 'new')
        self.assertFalse(result['workspace']['summary_stale'])
        self.desk.demo_message('另请人工确认运费', cid)
        updated = self.desk.conversation(cid)['workspace']
        self.assertTrue(updated['summary_stale'])
        self.assertIn('运费', updated['handoff']['context'])
        self.assertEqual(updated['summary'], result['workspace']['summary'])

    def test_analysis_does_not_overwrite_new_customer_edits(self):
        cid = self.cid()
        def analyze(*args):
            self.desk.customer_update(cid, {'notes': '分析期间人工补充'})
            return {'summary': '旧摘要', 'summary_source': 'local', 'evidence': []}
        with patch('desk.integrations.analyze_customer', side_effect=analyze):
            with self.assertRaisesRegex(ValueError, '已更新'):
                self.desk.analyze(cid)
        self.assertNotEqual(self.desk.conversation(cid)['workspace']['summary'], '旧摘要')
        self.assertEqual(self.desk.conversation(cid)['workspace']['notes'], '分析期间人工补充')

    def test_published_knowledge_and_curated_profile_reach_responder_only(self):
        self.desk.update_settings({'mode': 'live', 'deepseek_api_key': 'test-key'})
        self.knowledge(status='published')
        self.knowledge('内部营业时间', '内部底价100元', status='published', audience='internal')
        self.desk.connect()
        cid = self.desk.receive('c', 'u', '人工', 'm')
        self.desk.customer_update(cid, {'profile': '经营纸箱店', 'notes': '本机内部备注'})
        self.desk.resume(cid)
        captured = []
        self.desk.responder = lambda settings, *_: captured.append(settings) or '回复'
        self.desk.receive('c', 'u', '营业时间', 'm2')
        self.desk.pool.shutdown(wait=True)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(len(captured[0]['_local_knowledge']), 1)
        self.assertEqual(captured[0]['_customer_context']['profile'], '经营纸箱店')
        self.assertNotIn('本机内部备注', json.dumps(captured[0]['_customer_context'], ensure_ascii=False))

    def test_empty_agent_response_is_rejected_for_reply_and_explicit_test(self):
        settings = {'deepseek_api_key': 'test-key', 'agent_enabled': True, 'agent_url': 'https://agent.example'}
        with patch('desk.integrations.post_json', return_value={}) as post:
            with self.assertRaises(HandoffRequired):
                respond(settings, [], '问题')
            self.assertEqual(post.call_count, 1)
        self.desk.update_settings({'agent_url': 'https://agent.example'})
        with patch('desk.integrations.post_json', return_value={}):
            with self.assertRaises(ValueError):
                self.desk.test_integration('agent', '问题')
        self.assertEqual(self.transport.sent, [])

    def test_explicit_integration_tests_are_real_calls_but_never_send(self):
        self.desk.update_settings({'knowledge_url': 'https://kb.example', 'agent_url': 'https://agent.example',
                                   'deepseek_api_key': 'test-key'})
        for kind, payload, field in (
            ('knowledge', {'documents': [{'text': '营业到18点', 'source': '门店'}]}, 'documents'),
            ('agent', {'context': '需要核对库存'}, 'context'),
            ('model', {'choices': [{'message': {'content': '您好'}, 'finish_reason': 'stop'}]}, 'reply')):
            with self.subTest(kind=kind), patch('desk.integrations.post_json', return_value=payload) as post:
                result = self.desk.test_integration(kind, '营业时间')
                self.assertTrue(result['ok'])
                self.assertIn(field, result)
                self.assertEqual(post.call_count, 1)
        self.assertEqual(self.transport.sent, [])


if __name__ == '__main__':
    unittest.main()
