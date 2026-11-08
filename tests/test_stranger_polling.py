import unittest
from unittest.mock import Mock, patch
from desk.stranger import StrangerError
from desk.stranger_polling import StrangerPoller


def event(identity, time=2000):
    return {'conversation_id':'c', 'message_id':identity, 'created_at_ms':time}


class PollerTests(unittest.TestCase):
    def test_manual_refresh_reads_upstream_and_coalesces_clicks(self):
        fetch=Mock(return_value={'events':[],'count':0,'truncated':False})
        poller=StrangerPoller(Mock(),None,Mock(),manual_fetch=fetch,recover_on_start=True)
        first=poller.request_refresh()
        self.assertEqual(poller.request_refresh(),first)
        self.assertEqual(fetch.call_count,0)
        poller.wake=Mock()
        poller.wake.wait.side_effect=lambda _:poller.stop_event.set()
        poller.run()
        fetch.assert_called_once()
        self.assertEqual(poller.refresh_state()['status'],'completed')
        self.assertEqual(poller.refresh_state()['conversations'],0)
        poller.fetch.assert_not_called()

    def test_manual_refresh_failure_is_not_success(self):
        poller=StrangerPoller(Mock(),None,Mock(),manual_fetch=Mock(side_effect=StrangerError('limited',code=409,rate_limited=True)))
        poller.request_refresh()
        poller.wake=Mock()
        poller.wake.wait.side_effect=lambda _:poller.stop_event.set()
        poller.run()
        self.assertEqual(poller.refresh_state()['status'],'failed')
        self.assertGreaterEqual(poller.refresh_state()['retry_seconds'],120)

    def test_manual_refresh_cannot_bypass_backoff(self):
        fetch=Mock()
        poller=StrangerPoller(Mock(),None,Mock(),manual_fetch=fetch)
        poller.retry_after=120
        poller.request_refresh()
        poller.stop_event=Mock()
        poller.stop_event.is_set.return_value=False
        poller.stop_event.wait.return_value=True
        with patch('desk.stranger_polling.time.monotonic',return_value=0):
            poller.run()
        poller.stop_event.wait.assert_called_once_with(120)
        fetch.assert_not_called()

    def test_reconnect_recovers_backlog_instead_of_marking_seen(self):
        self.poller.recover_on_start=True
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual([e['message_id'] for e in self.delivered], ['old'])

    def test_official_reply_keeps_earlier_incoming_as_display_only(self):
        self.poller.recover_on_start=True
        self.snapshot['events']=[{**event('new',2500),'conversation_short_id':'123'}]
        self.poller.history_fetch=lambda _:[event('old',500),{**event('reply',2000),'is_outgoing':True},event('new',2500)]
        self.poller.poll_once()
        self.assertEqual([e['message_id'] for e in self.delivered],['old','reply','new'])
        self.assertTrue(self.delivered[0]['history_only'])
        self.assertFalse(self.delivered[-1].get('history_only', False))

    def test_newest_conversation_is_checked_before_old_backlog(self):
        self.poller.recover_on_start = True
        old = {**event('old', 1000), 'conversation_short_id': '1'}
        new = {**event('new', 3000), 'conversation_id': 'new', 'conversation_short_id': '2'}
        self.snapshot['events'] = [old, new]
        calls = []
        def history(short):
            calls.append(short)
            return [new if short == '2' else old]
        self.poller.history_fetch = history
        self.poller.poll_once()
        self.assertEqual(calls, ['2', '1'])

    def test_missing_media_url_is_refetched_for_later_enrichment(self):
        self.poller.recover_on_start = True
        latest = {**event('image', 3000), 'conversation_short_id': '1', 'media_kind': 'image', 'media_url': ''}
        self.snapshot['events'] = [latest]
        self.poller.history_fetch = Mock(side_effect=[[latest], [{**latest, 'media_url': 'https://p11-sign.douyinpic.com/image'}]])
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.poller.history_fetch.call_count, 2)
        self.assertTrue(self.delivered[-1]['media_url'])

    def test_rate_limit_backs_off_and_reports_specific_status(self):
        delays, reports = [], []
        class Stop:
            def is_set(self): return False
            def wait(self, delay):
                delays.append(delay)
                return len(delays) == 4
        def fail():
            raise StrangerError('blocked', code=409, rate_limited=True)
        poller=StrangerPoller(fail,None,lambda *args:reports.append(args))
        poller.stop_event=Stop()
        poller.wake=Mock()
        with patch('desk.stranger_polling.time.monotonic',return_value=0):
            poller.run()
        self.assertEqual(delays,[120,240,480,900])
        self.assertTrue(all(r[0]=='rate_limited' for r in reports))
        self.assertFalse(poller.initialized)

    def setUp(self):
        self.snapshot = {'events':[event('old', 500)], 'server_time_ms':1000, 'truncated':False}
        self.delivered, self.reports = [], []
        self.poller = StrangerPoller(lambda:self.snapshot, self.delivered.append,
                                    lambda *args:self.reports.append(args))

    def test_baseline_skips_old_and_new_is_delivered_once(self):
        self.poller.poll_once()
        self.assertEqual(self.delivered, [])
        self.snapshot['events'] = [event('new')]
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual([e['message_id'] for e in self.delivered], ['new'])

    def test_late_old_and_unknown_time_not_replied(self):
        self.poller.poll_once()
        self.snapshot['events'] = [event('late',500), event('unknown',0)]
        self.poller.poll_once()
        self.assertEqual(self.delivered, [])

    def test_disconnect_during_fetch_drops_result(self):
        self.poller.poll_once()
        self.snapshot['events'] = [event('new')]
        def fetch():
            self.poller.stop()
            return self.snapshot
        self.poller.fetch = fetch
        self.poller.poll_once()
        self.assertEqual(self.delivered, [])

    def test_saturated_snapshot_never_auto_replies(self):
        self.snapshot['truncated'] = True
        self.poller.poll_once()
        self.assertFalse(self.poller.initialized)
        self.assertEqual(self.reports[-1][0], 'limited')

    def test_delivery_rejected_is_retried_after_connection_recovers(self):
        self.poller.poll_once()
        self.snapshot['events'] = [event('new')]
        self.poller.deliver = lambda _:False
        self.poller.poll_once()
        self.poller.deliver = self.delivered.append
        self.poller.poll_once()
        self.assertEqual(len(self.delivered),1)

    def test_changed_conversation_fetches_all_messages_as_one_batch(self):
        self.poller.poll_once()
        latest = {**event('3',3000),'conversation_short_id':'123'}
        self.snapshot['events'] = [latest]
        earlier = {**event('2',2000),'text':'转人工'}
        self.poller.history_fetch = lambda _: [latest, earlier]
        batches = []
        self.poller.deliver_batch = batches.append
        self.poller.poll_once()
        self.assertEqual([e['message_id'] for e in batches[0]], ['2','3'])

    def test_incomplete_history_never_replies_from_latest_snapshot(self):
        self.poller.poll_once()
        self.snapshot['events'] = [{**event('3'),'conversation_short_id':'123'}]
        self.poller.history_fetch = lambda _:[]
        with self.assertRaises(RuntimeError):
            self.poller.poll_once()
        self.assertEqual(self.delivered,[])
