import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from desk.recovery import fetch_ordinary, fetch_history, recovery_start, RecoveryInbox
from desk.stranger import _field as f, StrangerError
import json

def msg(mid,sender,stamp):
    return f(1,b'0:1:11:22')+f(2,1)+f(3,mid)+f(5,777)+f(6,7)+f(7,sender)+f(8,json.dumps({'text':'hello'}).encode())+f(10,stamp)

class RecoveryTests(unittest.TestCase):
    def test_incremental_page_does_not_wait_for_all_history(self):
        inbox=RecoveryInbox(self.auth,incremental=True)
        with patch('desk.recovery.request',side_effect=[{1:[msg(1,22,1000)],5:[1],4:[10]},{1:[msg(2,22,2000)]}]) as call:
            first=inbox.fetch(normal=True)
            self.assertEqual(call.call_count,1)
            self.assertTrue(first['partial'])
            self.assertEqual(first['events'][0]['message_id'],'1')
            second=inbox.fetch(normal=True)
            self.assertEqual(second['events'][0]['message_id'],'2')
            self.assertFalse(second['partial'])

    def test_missing_media_priority_does_not_fetch_entire_list(self):
        inbox=RecoveryInbox(self.auth,incremental=True)
        incoming={'conversation_id':'c','conversation_short_id':'123','message_id':'9','created_at_ms':2000}
        inbox.prioritize(incoming)
        with patch('desk.recovery.request') as request:
            self.assertEqual(inbox.fetch()['events'],[incoming])
            request.assert_not_called()
            self.assertEqual(inbox.routes['123'],(True,'c','9'))

    def test_full_refresh_reads_normal_and_stranger_and_deduplicates(self):
        inbox = RecoveryInbox(object())
        normal = {'conversation_id': 'normal', 'conversation_short_id': '1', 'message_id': '2', 'created_at_ms': 2000}
        stranger = {'conversation_id': 'stranger', 'conversation_short_id': '3', 'message_id': '4', 'created_at_ms': 1000}
        with patch('desk.recovery.fetch_ordinary', return_value={'events': [normal], 'truncated': False}) as ordinary, patch('desk.recovery.fetch_stranger', return_value={'events': [normal, stranger], 'truncated': False}) as strangers:
            snapshot = inbox.fetch_all()
        ordinary.assert_called_once()
        strangers.assert_called_once()
        self.assertEqual(len(snapshot['events']), 2)
        self.assertTrue(inbox.routes['1'][0])
        self.assertFalse(inbox.routes['3'][0])

    def test_explicit_stranger_refresh_uses_stranger_endpoint(self):
        inbox=RecoveryInbox(object())
        inbox.normal_next=True
        with patch('desk.recovery.fetch_stranger',return_value={'events':[]}) as stranger,patch('desk.recovery.fetch_ordinary') as ordinary:
            inbox.fetch(normal=False)
            stranger.assert_called_once()
            ordinary.assert_not_called()
            self.assertTrue(inbox.normal_next)

    def test_stranger_history_missing_latest_uses_regular_history(self):
        inbox=RecoveryInbox(object())
        inbox.routes['777']=(False,'0:1:11:22','2')
        for history in ([],[{'message_id':'1'}]):
            with patch('desk.recovery.fetch_messages',return_value=history), patch('desk.recovery.fetch_history',return_value=[{'message_id':'2'}]) as fallback:
                self.assertEqual(inbox.history('777'),[{'message_id':'2'}])
                fallback.assert_called_once_with(inbox.auth,'0:1:11:22','777')
        with patch('desk.recovery.fetch_messages',return_value=[{'message_id':'2'}]), patch('desk.recovery.fetch_history') as fallback:
            self.assertEqual(inbox.history('777'),[{'message_id':'2'}])
            fallback.assert_not_called()

    def setUp(self):
        self.auth=type('Auth',(),{'get_uid':lambda _:11})()

    def test_latest_owner_reply_is_kept_for_client_sync(self):
        with patch('desk.recovery.request',return_value={1:[msg(1,22,1000),msg(2,11,2000)]}):
            events = fetch_ordinary(self.auth)['events']
            self.assertEqual([e['message_id'] for e in events], ['2'])
            self.assertTrue(events[0]['is_outgoing'])

    def test_normal_pages_gather_all_and_reject_nonadvancing_cursor(self):
        with patch('desk.recovery.request') as request:
            request.side_effect=[{1:[msg(1,22,1000)],5:[1],4:[10]},{1:[msg(2,22,2000)]}]
            result=fetch_ordinary(self.auth)
            self.assertEqual(result['events'][0]['message_id'],'2')
            request.side_effect=[{5:[1],4:[0]}]
            with self.assertRaises(StrangerError):fetch_ordinary(self.auth)

    def test_history_retains_outgoing_boundary(self):
        with patch('desk.recovery.request',return_value={1:[msg(2,22,2000),msg(1,11,1000)],3:[1]}):
            result=fetch_history(self.auth,'0:1:11:22','777')
            self.assertTrue(result[0]['is_outgoing'])
            self.assertFalse(result[1]['is_outgoing'])
            with self.assertRaises(StrangerError):fetch_history(self.auth,'wrong','777')

    def test_recovery_start_persists_and_accounts_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('desk.recovery.time.time',return_value=200000):
                first=recovery_start(Path(directory),'11')
            with patch('desk.recovery.time.time',return_value=300000):
                self.assertEqual(recovery_start(Path(directory),'11'),first)
                self.assertGreater(recovery_start(Path(directory),'22'),first)
