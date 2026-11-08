import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desk.transport import DouyinTransport, load_vendor, parse_incoming
from desk.stranger import _field as f, parse_response
from desk.recovery import fetch_ordinary
from desk.stranger_polling import StrangerPoller


def message(mid=123, sender=99, stamp=2000, client='desk-mid'):
    return (f(1, b'0:1:99:12') + f(2, 1) + f(3, mid) + f(5, 777)
            + f(6, 7) + f(7, sender) + f(8, json.dumps({'text': 'hello'}).encode())
            + f(9, f(1, b's:client_message_id') + f(2, client.encode())) + f(10, stamp))


class OwnerTransportTests(unittest.TestCase):
    def test_owner_push_retains_exact_client_identity_and_peer(self):
        *_, live, response = load_vendor()
        for field, nested in ((500, 5), (503, 2)):
            raw = live.PushFrame(payloadType='pb', payload=f(1, 1099) + f(6, f(field, f(nested, message())))).SerializeToString()
            event = parse_incoming(raw, '99', live, response)
            self.assertIsNotNone(event)
            self.assertTrue(event['is_outgoing'])
            self.assertEqual(event['client_message_id'], 'desk-mid')
            self.assertEqual(event['sender_id'], '99')
            self.assertEqual(event['peer_id'], '12')

    def test_history_retains_owner_client_identity(self):
        raw = f(1,1001) + f(4,b'OK') + f(6,f(1000,f(4,f(3,message()))))
        event = parse_response(raw, '99', include_self=True)['events'][0]
        self.assertEqual(event['client_message_id'], 'desk-mid')
        self.assertEqual(event['peer_id'], '12')

    def test_latest_owner_message_remains_in_ordinary_snapshot(self):
        auth = Mock()
        auth.get_uid.return_value = '99'
        with patch('desk.recovery.request', return_value={1:[message()]}):
            result = fetch_ordinary(auth)
        self.assertTrue(result['events'][0]['is_outgoing'])

    def test_poll_delivers_owner_boundary_and_later_incoming_together(self):
        old = dict(conversation_id='c', conversation_short_id='1', message_id='1', created_at_ms=1100)
        owner = dict(old, message_id='2', created_at_ms=1500, is_outgoing=True)
        latest = dict(old, message_id='3', created_at_ms=2000)
        batches = []
        poller = StrangerPoller(lambda:dict(events=[latest],truncated=False), None, Mock(),
            history_fetch=lambda _: [latest, old, owner], deliver_batch=batches.append,
            recover_on_start=True, recovery_since=1000)
        poller.poll_once()
        poller.poll_once()
        self.assertEqual([[e['message_id'] for e in b] for b in batches], [['1','2','3']])
        self.assertTrue(batches[0][0]['history_only'])

    def test_text_and_asset_use_caller_client_message_id(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = DouyinTransport(Path(directory))
            auth, api = object(), Mock()
            transport._auth, transport._connected = auth, True
            transport.prepare = lambda _: (transport._epoch, auth, api, 'c', 1, 'ticket')
            transport.send('c', 'text', client_message_id='text-mid')
            transport.send_asset('c', Path('a.txt'), 'file', client_message_id='asset-mid')
            self.assertEqual(api.send_msg.call_args.kwargs['client_message_id'], 'text-mid')
            self.assertEqual(api.send_file.call_args.kwargs['client_message_id'], 'asset-mid')
            self.assertTrue(transport.supports_client_message_id)

    def test_owner_route_never_points_back_to_self(self):
        transport = DouyinTransport(Path('.'))
        transport._uid = '99'
        transport._routes['c'] = '12'
        transport._record_route(dict(conversation_id='c', sender_id='99', is_outgoing=True, peer_id=''))
        self.assertEqual(transport._routes['c'], '12')
        transport._record_route(dict(conversation_id='c', sender_id='99', is_outgoing=True, peer_id='99'))
        self.assertEqual(transport._routes['c'], '12')
        transport._record_route(dict(conversation_id='other', sender_id='99', is_outgoing=True, peer_id='13'))
        self.assertEqual(transport._routes['other'], '13')

    def test_unknown_owner_timestamp_blocks_history_auto_reply(self):
        owner = dict(conversation_id='c', message_id='1', created_at_ms=0, is_outgoing=True)
        latest = dict(owner, message_id='2', created_at_ms=2000, conversation_short_id='1', is_outgoing=False)
        batches = []
        poller = StrangerPoller(lambda:dict(events=[latest],truncated=False), None, Mock(),
            history_fetch=lambda _: [owner, latest], deliver_batch=batches.append,
            recover_on_start=True, recovery_since=1000)
        with self.assertRaises(RuntimeError):
            poller.poll_once()
        self.assertEqual(batches, [])
