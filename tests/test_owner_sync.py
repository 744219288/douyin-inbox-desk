from pathlib import Path
import tempfile
import threading
import time
import unittest

from desk.service import DeskService


class Transport:
    supports_client_message_id = True

    def __init__(self):
        self.sent = []
        self.echo = True

    def connect(self, settings, message, status):
        self.message = message
        status('connected')

    def send(self, raw, text, client_message_id=None):
        self.sent.append((raw, text, client_message_id))
        if self.echo:
            self.message(conversation_id=raw, sender_id='owner', peer_id='peer', text=text,
                         message_id='server-' + client_message_id, is_outgoing=True,
                         client_message_id=client_message_id, created_at_ms=time.time()*1000)

    def disconnect(self):
        pass


class OwnerSyncTests(unittest.TestCase):
    def test_owner_photo_can_be_enriched_without_duplicate_or_new_handoff(self):
        event = self.owner(media_kind='image', media_url='')
        self.service.receive(**event)
        before = self.service.conversation('live:c')
        self.service.recover_batch([dict(event, media_url='https://p26-sign.douyinpic.com/photo.png', media_secret='01'*32, media_md5='02'*16)])
        after = self.service.conversation('live:c')
        self.assertEqual(len(after['messages']), 1)
        mid = after['messages'][0]['id']
        self.assertEqual(after['messages'][0]['media_url'], '/api/incoming-media/'+mid)
        self.assertEqual(after['workspace']['handoff'], before['workspace']['handoff'])
        self.assertEqual(self.transport.sent, [])

    def test_delayed_owner_history_does_not_replace_latest_preview(self):
        stamp = (self.service.owner_sync_started + 1)*1000
        self.service.receive(**dict(self.owner('recent'), text='最新消息', created_at_ms=stamp+2000))
        self.service.recover_batch([dict(self.owner('older'), text='之前的消息', created_at_ms=stamp)])
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['preview'], '最新消息')
        self.assertEqual(len(view['messages']), 2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.transport = Transport()
        self.service = DeskService(Path(self.tmp.name), lambda *_: '自动答复', self.transport)
        self.service._queue_profile = lambda _: None
        self.service.update_settings({'mode': 'live', 'deepseek_api_key': 'fake'})
        self.service.connect()
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.service.pool.shutdown(wait=True)
        self.service.close()
        self.tmp.cleanup()

    def owner(self, mid='external', **extra):
        return dict(conversation_id='c', sender_id='owner', peer_id='peer', text='本人接待',
                    message_id=mid, is_outgoing=True, created_at_ms=time.time()*1000, **extra)

    def test_external_owner_cancels_pending_and_preserves_unread_media_time(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def reply(*_):
            started.set()
            release.wait(3)
            return '不应发送'
        self.service.responder = reply
        self.service.receive('c', 'peer', '客户问题', 'incoming')
        self.assertTrue(started.wait(1))
        event = self.owner(media_kind='image', media_url='https://example.com/a.jpg')
        self.service.receive(**event)
        self.service.receive(**event)
        with self.service.lock:
            self.assertEqual(self.service._get('live:c')['unread'], 1)
            self.assertEqual(self.service._get('live:c')['sender_id'], 'peer')
        release.set()
        self.service.pool.shutdown(wait=True)
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'human')
        humans = [m for m in view['messages'] if m['role'] == 'human']
        self.assertEqual(len(humans), 1)
        self.assertEqual(humans[0]['origin'], 'douyin')
        self.assertEqual(humans[0]['media_kind'], 'image')
        from datetime import datetime
        self.assertAlmostEqual(datetime.fromisoformat(humans[0]['created_at']).timestamp()*1000,
                               event['created_at_ms'], delta=1)
        self.assertEqual(self.transport.sent, [])

    def test_ai_echo_reuses_message_without_taking_over(self):
        self.service.receive('c', 'peer', '问题', 'incoming')
        self.service.pool.shutdown(wait=True)
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'ai')
        self.assertEqual([m['role'] for m in view['messages']], ['user', 'assistant'])
        self.assertEqual(view['messages'][1]['origin'], 'desk')
        self.assertEqual(self.transport.sent[0][2], view['messages'][1]['id'])

    def test_manual_echo_and_identical_external_text_are_distinct(self):
        self.service.receive('c', 'peer', '人工', 'incoming')
        self.service.send('live:c', '本人接待')
        self.service.resume('live:c')
        self.service.receive(**self.owner())
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'human')
        self.assertEqual([m['origin'] for m in view['messages'] if m['role'] == 'human'], ['desk', 'douyin'])

    def test_mixed_batch_owner_suppresses_following_ai(self):
        stamp = time.time()*1000
        incoming = dict(conversation_id='c', sender_id='peer', text='稍后问题', message_id='incoming', created_at_ms=stamp+10)
        self.service.recover_batch([self.owner(), incoming])
        self.service.pool.shutdown(wait=True)
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'human')
        self.assertEqual({m['text'] for m in view['messages']}, {'本人接待', '稍后问题'})
        self.assertEqual(self.transport.sent, [])

    def test_old_owner_after_resume_is_displayed_without_takeover(self):
        self.service.receive('c', 'peer', '人工', 'incoming')
        event = self.owner()
        self.service.resume('live:c')
        self.service.recover_batch([event])
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'], 'ai')
        self.assertEqual(len(self.service.conversation('live:c')['messages']), 2)

    def test_legacy_history_is_not_added_as_another_message(self):
        event = self.owner()
        event['created_at_ms'] = 1000
        self.service.recover_batch([event])
        self.assertEqual(self.service.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_echo_correlation_survives_restart(self):
        self.transport.echo = False
        self.service.receive('c', 'peer', '问题', 'incoming')
        self.service.pool.shutdown(wait=True)
        sent_mid = self.transport.sent[0][2]
        start = self.service.owner_sync_started
        self.service.close()
        self.service = DeskService(Path(self.tmp.name), lambda *_: '不应重发', self.transport)
        self.service._queue_profile = lambda _: None
        self.service.connect()
        self.assertEqual(start, self.service.owner_sync_started)
        self.service.receive(**self.owner(client_message_id=sent_mid))
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'ai')
        self.assertEqual(len(view['messages']), 2)

    def test_upgrade_removes_only_legacy_imported_copies(self):
        self.service.receive('c', 'peer', '问题', 'incoming')
        self.service.pool.shutdown(wait=True)
        with self.service.lock:
            duplicate = self.service._message('live:c', 'human', '自动答复', 'sent')
            self.service.db.execute('INSERT INTO message_origins VALUES(?,?,?,?)', (duplicate, 'unknown', 'old-echo', ''))
        self.service.receive(**self.owner())
        self.service.close()
        for _ in range(2):
            self.service = DeskService(Path(self.tmp.name), lambda *_: 'unused', self.transport)
            view = self.service.conversation('live:c')
            self.assertEqual([m['role'] for m in view['messages']], ['user', 'assistant', 'human'])
            self.assertEqual([m['text'] for m in view['messages']].count('自动答复'), 1)
            self.assertEqual(view['messages'][-1]['origin'], 'douyin')
            self.assertEqual(self.service.db.execute('SELECT COUNT(*) FROM suppressed_owner_history').fetchone()[0], 1)
            self.service.close()

    def test_batch_echo_does_not_hide_later_incoming(self):
        self.transport.echo = False
        self.service.receive('c', 'peer', '问题', 'incoming')
        deadline = time.time()+2
        while not self.transport.sent and time.time() < deadline:
            time.sleep(.01)
        self.assertTrue(self.transport.sent)
        sent_mid = self.transport.sent[0][2]
        event = self.owner(client_message_id=sent_mid)
        incoming = dict(conversation_id='c', sender_id='peer', text='新的问题', message_id='later',
                        created_at_ms=time.time()*1000+100)
        self.service.recover_batch([event, incoming])
        self.service.pool.shutdown(wait=True)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'], 'ai')

    def test_prepared_send_passes_exact_registered_id(self):
        self.transport.prepare = lambda raw: {'raw': raw}
        def prepared(target, text, client_message_id=None):
            return self.transport.send(target['raw'], text, client_message_id=client_message_id)
        self.transport.send_prepared = prepared
        self.service.receive('c', 'peer', '问题', 'incoming')
        self.service.pool.shutdown(wait=True)
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'ai')
        self.assertEqual(self.transport.sent[0][2], view['messages'][1]['id'])

    def test_client_id_from_other_conversation_is_not_echo(self):
        self.service.receive('other', 'other-peer', '人工', 'incoming')
        self.service.send('live:other', '本人接待')
        mid = self.transport.sent[0][2]
        self.service.receive(**self.owner(client_message_id=mid))
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'human')
        self.assertEqual(view['messages'][0]['origin'], 'douyin')

    def test_missing_timestamp_history_does_not_hide_newer_customer(self):
        event = self.owner()
        event['created_at_ms'] = 0
        incoming = dict(conversation_id='c', sender_id='peer', text='新的问题', message_id='later',
                        created_at_ms=time.time()*1000)
        self.service.recover_batch([event, incoming])
        self.service.pool.shutdown(wait=True)
        self.assertEqual(len(self.transport.sent), 1)

    def test_external_takeover_survives_inflight_send_failure(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def failing_send(*args, **kwargs):
            started.set()
            release.wait(3)
            raise RuntimeError('fake transport failure')
        self.transport.send = failing_send
        self.service.receive('c', 'peer', '问题', 'incoming')
        self.assertTrue(started.wait(1))
        self.service.receive(**self.owner())
        self.assertEqual(self.service._get('live:c')['status'], 'human')
        release.set()
        self.service.pool.shutdown(wait=True)
        view = self.service.conversation('live:c')
        self.assertEqual(view['conversation']['status'], 'human')
        self.assertEqual(view['workspace']['handoff']['status'], 'accepted')
        self.assertEqual(next(m for m in view['messages'] if m['role'] == 'assistant')['status'], 'failed')

    def test_asset_echo_keeps_single_local_message(self):
        from desk.assets import send_asset
        def send_file(raw, path, kind, client_message_id=None):
            self.transport.sent.append((raw, kind, client_message_id))
            self.transport.message(conversation_id=raw, sender_id='owner', peer_id='peer',
                text='[文件]', message_id='asset-server', is_outgoing=True,
                client_message_id=client_message_id, created_at_ms=time.time()*1000,
                media_kind=kind, media_url='https://example.com/file.txt')
        self.transport.send_asset = send_file
        self.service.receive('c', 'peer', '人工', 'incoming')
        asset = self.service.assets.put('live', 'example.txt', b'fixture')
        result = send_asset(self.service, 'live:c', asset['id'])
        outgoing = [m for m in result['messages'] if m['role'] == 'human']
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing[0]['origin'], 'desk')
        self.assertEqual(outgoing[0]['media_url'], asset['url'])
        self.assertEqual(outgoing[0]['id'], self.transport.sent[0][2])


if __name__ == '__main__':
    unittest.main()
