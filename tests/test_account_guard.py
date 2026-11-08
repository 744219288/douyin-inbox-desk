from contextlib import closing
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from desk.account_guard import bind_account, AccountMismatch
from desk.transport import DouyinTransport


class AccountGuardTests(unittest.TestCase):
    def test_different_account_is_blocked_before_listener_or_inbox_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bind_account(root, '123')
            (root/'douyin-session.json').write_text(json.dumps({'cookie':'test','ticket':'test','private_key':'test'}))
            auth = Mock()
            auth.get_uid.return_value = '456'
            factory, receiver = Mock(), Mock()
            factory.from_cookie.return_value = auth
            finished = threading.Event()
            states = []
            def status(value, detail):
                states.append((value, detail))
                if value == 'error':
                    finished.set()
            with patch('desk.transport.load_vendor', return_value=(factory,Mock(),receiver,Mock(),Mock())):
                transport = DouyinTransport(root)
                transport.connect({}, Mock(), status)
                self.assertTrue(finished.wait(3))
                self.assertTrue(states[-1][1].startswith('账号不匹配'))
                receiver.assert_not_called()
                self.assertIsNone(transport._stranger_poller)
                transport.disconnect()

    def test_identity_survives_restart_and_rejects_another_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            bind_account(tmp, '123')
            bind_account(tmp, '123')
            with self.assertRaises(AccountMismatch):
                bind_account(tmp, '456')
            with closing(sqlite3.connect(Path(tmp)/'desk.sqlite3')) as db, db:
                self.assertEqual(db.execute("SELECT value FROM service_metadata WHERE key='account_uid'").fetchone()[0], '123')

    def test_legacy_account_cannot_be_claimed_by_new_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'routes.json').write_text(json.dumps({'uid':'123','routes':{}}))
            with self.assertRaises(AccountMismatch):
                bind_account(root, '456')
            bind_account(root, '123')

    def test_conflicting_legacy_accounts_require_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'routes.json').write_text(json.dumps({'uid':'123'}))
            (root/'recovery-window.json').write_text(json.dumps({'456':100}))
            with self.assertRaises(AccountMismatch):
                bind_account(root, '123')

    def test_orphaned_live_records_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(sqlite3.connect(Path(tmp)/'desk.sqlite3')) as db, db:
                db.execute('CREATE TABLE conversations (mode TEXT)')
                db.execute("INSERT INTO conversations VALUES ('live')")
            with self.assertRaises(AccountMismatch):
                bind_account(tmp, '123')

    def test_disconnect_clears_recipient_cache_and_old_send(self):
        transport = DouyinTransport(Path('.'))
        transport._uid = '123'
        transport._routes = {'c':'456'}
        transport._api = object()
        transport._auth = object()
        transport._connected = True
        prepared = (transport._epoch,transport._auth,transport._api,'c',1,'ticket')
        transport.disconnect()
        self.assertEqual(transport.contact_uid('c'),'')
        self.assertIsNone(transport._api)
        with self.assertRaises(RuntimeError):
            transport.send_prepared(prepared,'must not send')
