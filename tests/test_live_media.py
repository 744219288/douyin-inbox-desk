import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk.live_media import LiveMediaResolver
from desk.stranger import StrangerError


class LiveMediaTests(unittest.TestCase):
    def setUp(self):
        self.auth = SimpleNamespace(get_uid=lambda: 'owner')
        self.deliver = Mock(return_value=True)
        self.worker = LiveMediaResolver(self.auth, self.deliver)
        self.event = dict(conversation_id='c', conversation_short_id='123',
                          message_id='1', media_kind='image', media_url='', is_outgoing=False)

    def test_coalesces_only_missing_incoming_media(self):
        for item in [self.event, self.event, dict(self.event, is_outgoing=True),
                     dict(self.event, media_url='https://example.org/image')]:
            self.worker.enqueue(item)
        self.assertEqual(len(self.worker.pending), 1)
        self.assertEqual(self.auth._desk_stranger_interval, 2)

    def test_fetches_one_page_and_delivers_only_exact_targets(self):
        self.worker.enqueue(self.event)
        good = dict(self.event, media_url='url')
        unrelated = dict(good, message_id='2')
        with patch('desk.live_media.request', return_value={1: []}) as request, \
             patch('desk.live_media.messages', return_value=[good, unrelated]):
            done = self.worker.resolve(list(self.worker.pending.items()))
        self.assertEqual(done, {('c', '1')})
        request.assert_called_once()
        self.deliver.assert_called_once_with(good)

    def test_rejected_delivery_stays_retryable(self):
        self.worker.enqueue(self.event)
        self.deliver.return_value = False
        with patch('desk.live_media.request', return_value={}), \
             patch('desk.live_media.messages', return_value=[dict(self.event, media_url='url')]):
            self.assertEqual(self.worker.resolve(list(self.worker.pending.items())), set())

    def test_rate_limit_stops_all_fast_reads_during_cooldown(self):
        self.worker.pending[('c', '1')] = (self.event, 0, 0)
        self.worker.resolve = Mock(side_effect=StrangerError('limited', rate_limited=True))
        self.worker.stop_event = Mock()
        self.worker.stop_event.is_set.return_value = False
        self.worker.stop_event.wait.return_value = True
        self.worker.run()
        self.worker.stop_event.wait.assert_called_once_with(120)
        self.worker.resolve.assert_called_once()

    def test_stop_interrupts_request_wait(self):
        self.worker.stop()
        self.assertTrue(self.auth._desk_stranger_stop.is_set())
