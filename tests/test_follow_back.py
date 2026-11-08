import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from desk.follow_back import FollowBackWorker, FollowStore, FollowError, DouyinFollowAPI
from desk.service import DeskService


def fan(uid):
    return {'uid': str(uid), 'sec_uid': 'MS4wPerson' + str(uid)}


class Backend:
    owner = '100'
    token = ('100', 1)

    def __init__(self):
        self.users = [fan(1)]
        self.calls = []
        self.following = set()
        self.follower = True
        self.valid = True
        self.fail = False

    def active(self):
        return self.valid

    def page(self, cursor):
        return {'status_code': 0, 'followers': self.users, 'has_more': 0}

    def relation(self, uid, sec):
        return {'following': uid in self.following, 'follower': self.follower}

    def prepare(self, uid, sec):
        def submit():
            self.calls.append(uid)
            if self.fail:
                raise TimeoutError('private credential in upstream error')
            self.following.add(uid)
        return submit


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = Backend()
        self.worker = FollowBackWorker(self.tmp.name, lambda: self.backend)
        self.worker.configure(True, start=False)

    def scan(self):
        self.worker.next_scan = 0
        self.worker.step()

    def add_new(self):
        self.scan()
        self.backend.users.append(fan(2))

    def test_initial_fans_are_baseline_only(self):
        self.scan()
        self.assertEqual(self.worker.store.counts('100'), {'baseline': 1})
        self.assertEqual(self.backend.calls, [])

    def test_all_new_fans_follow_without_any_messages(self):
        self.add_new()
        self.backend.users.append(fan(3))
        self.scan()
        self.worker.step()
        self.assertEqual(self.backend.calls, ['2', '3'])
        self.scan()
        self.assertEqual(self.backend.calls, ['2', '3'])

    def test_restart_keeps_baseline_and_dedup(self):
        self.add_new()
        self.scan()
        self.worker = FollowBackWorker(self.tmp.name, lambda: self.backend)
        self.worker.configure(True, start=False)
        self.scan()
        self.assertEqual(self.backend.calls, ['2'])

    def test_paginated_baseline_commits_only_when_complete(self):
        self.backend.page = Mock(side_effect=[
            {'status_code': 0, 'followers': [fan(1)], 'has_more': 1, 'max_time': 999},
            {'status_code': 0, 'followers': [fan(2)], 'has_more': 0}])
        self.worker.step()
        self.assertEqual(self.worker.store.counts('100'), {})
        self.worker.step()
        self.assertEqual(self.worker.store.counts('100'), {'baseline': 2})
        self.assertEqual(self.backend.calls, [])

    def test_invalid_page_does_not_create_tasks(self):
        self.add_new()
        self.backend.page = Mock(return_value={'status_code': 8, 'followers': [fan(2)], 'has_more': 0})
        with self.assertRaises(FollowError):
            self.scan()
        self.assertEqual(self.worker.store.counts('100'), {'baseline': 1})

    def test_repeated_cursor_is_rejected(self):
        self.backend.page = Mock(return_value={'status_code': 0, 'followers': [fan(1)], 'has_more': 1, 'max_time': 999})
        self.worker.step()
        with self.assertRaises(FollowError):
            self.worker.step()
        self.assertEqual(self.backend.calls, [])

    def test_bad_identity_rejects_whole_page(self):
        self.backend.users.append({'uid': '2', 'sec_uid': '../bad'})
        with self.assertRaises(FollowError):
            self.scan()
        self.assertEqual(self.worker.store.counts('100'), {})

    def test_different_account_has_separate_baseline(self):
        self.add_new()
        self.backend.owner = '200'
        self.backend.token = ('200', 1)
        self.scan()
        self.assertEqual(self.worker.store.counts('200'), {'baseline': 2})
        self.assertEqual(self.backend.calls, [])

    def test_already_followed_is_skipped(self):
        self.add_new()
        self.backend.following.add('2')
        self.scan()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.worker.store.counts('100')['already_followed'], 1)

    def test_no_longer_follower_is_skipped(self):
        self.add_new()
        self.backend.follower = False
        self.scan()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.worker.store.counts('100')['not_follower'], 1)

    def test_disable_after_prepare_prevents_submit(self):
        self.add_new()
        original = self.backend.prepare
        def prepare(uid, sec):
            self.worker.configure(False, start=False)
            return original(uid, sec)
        self.backend.prepare = prepare
        self.scan()
        self.assertEqual(self.backend.calls, [])

    def test_disconnect_after_prepare_prevents_submit(self):
        self.add_new()
        original = self.backend.prepare
        def prepare(uid, sec):
            self.backend.valid = False
            return original(uid, sec)
        self.backend.prepare = prepare
        self.scan()
        self.assertEqual(self.backend.calls, [])

    def test_unknown_result_is_never_resubmitted(self):
        self.add_new()
        self.backend.fail = True
        with self.assertRaisesRegex(FollowError, '未确认'):
            self.scan()
        self.worker.step()
        self.scan()
        self.assertEqual(self.backend.calls, ['2'])
        self.assertEqual(self.worker.store.counts('100')['needs_review'], 1)

    def test_unknown_can_be_confirmed_by_relationship(self):
        self.add_new()
        self.backend.fail = True
        with self.assertRaises(FollowError):
            self.scan()
        self.backend.following.add('2')
        self.worker.step()
        self.assertEqual(self.backend.calls, ['2'])
        self.assertEqual(self.worker.store.counts('100')['confirmed'], 1)

    def test_interrupted_submission_recovers_as_unknown(self):
        self.add_new()
        self.worker.store.snapshot('100', {'2': 'MS4wPerson2'})
        self.worker.store.mark('100', '2', 'sending')
        store = FollowStore(self.tmp.name)
        self.assertEqual(store.next('100')[2], 'unknown')

    def test_disabled_does_not_read_platform(self):
        self.worker.configure(False, start=False)
        self.worker.backend_factory = Mock()
        self.worker.step()
        self.worker.backend_factory.assert_not_called()

    def test_atomic_claim_prevents_duplicate_submitters(self):
        self.add_new()
        self.worker.store.snapshot('100', {'2': 'MS4wPerson2'})
        self.assertTrue(self.worker.store.claim('100', '2'))
        self.assertFalse(self.worker.store.claim('100', '2'))

    def test_disable_reenable_cannot_revive_old_work(self):
        self.add_new()
        original = self.backend.prepare
        def prepare(uid, sec):
            self.worker.configure(False, start=False)
            self.worker.configure(True, start=False)
            return original(uid, sec)
        self.backend.prepare = prepare
        self.scan()
        self.assertEqual(self.backend.calls, [])

    def test_truncated_total_cannot_establish_baseline(self):
        self.backend.page = Mock(return_value={'status_code': 0, 'followers': [fan(1)], 'has_more': 0, 'total': 3})
        with self.assertRaises(FollowError):
            self.scan()
        self.assertEqual(self.worker.store.counts('100'), {})

    def test_credentials_error_leaves_task_pending(self):
        self.add_new()
        self.backend.prepare = Mock(side_effect=FollowError('凭证不可用'))
        with self.assertRaises(FollowError):
            self.scan()
        self.assertEqual(self.worker.store.counts('100')['pending'], 1)
        self.assertEqual(self.backend.calls, [])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.adapter = DouyinFollowAPI(Mock(), self.api, '100', ('100', 1), lambda: True)

    def test_unknown_relationship_fails_closed(self):
        self.api.get_user_info.return_value = {'status_code': 0, 'user': fan(2)}
        with self.assertRaises(FollowError):
            self.adapter.relation('2', 'MS4wPerson2')

    def test_identity_mismatch(self):
        self.api.get_user_info.return_value = {'status_code': 0, 'user': fan(3)}
        with self.assertRaises(FollowError):
            self.adapter.relation('2', 'MS4wPerson2')

    def test_mutual_relationship(self):
        self.api.get_user_info.return_value = {'status_code': 0, 'user': {**fan(2), 'follow_status': 2, 'follower_status': 1}}
        self.assertEqual(self.adapter.relation('2', 'MS4wPerson2'), {'following': True, 'follower': True})


class FollowSettingsTests(unittest.TestCase):
    def test_persist_switch_and_demo_never_enables_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Mock()
            service = DeskService(Path(folder), lambda *_: '', transport)
            try:
                self.assertFalse(service.settings['auto_follow_enabled'])
                service.update_settings({'auto_follow_enabled': True})
                transport.set_follow_back.assert_called_with(False)
                service.update_settings({'mode': 'live'})
                transport.set_follow_back.assert_called_with(True)
                with self.assertRaises(ValueError):
                    service.update_settings({'auto_follow_enabled': 'true'})
                service.update_settings({'auto_follow_enabled': False})
                transport.set_follow_back.assert_called_with(False)
            finally:
                service.close()
            second = DeskService(Path(folder), lambda *_: '', transport)
            try:
                self.assertFalse(second.settings['auto_follow_enabled'])
            finally:
                second.close()
