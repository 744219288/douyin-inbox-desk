import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import login
from desk.login_page import LoginState
from desk.browser_login import BrowserLoginError
import server


class LoginWorkerTests(unittest.TestCase):
    def test_launch_waits_until_helper_is_ready(self):
        with patch('server.login_page_ready', side_effect=[False, False, True]), patch('server.subprocess.Popen') as spawn, patch('server.time.sleep'):
            spawn.return_value.poll.return_value = None
            self.assertTrue(server.start_login_helper()['ok'])
            spawn.assert_called_once()

    def test_failed_launch_does_not_return_navigation_url(self):
        with patch('server.login_page_ready', return_value=False), patch('server.subprocess.Popen') as spawn:
            spawn.return_value.poll.return_value = 1
            with self.assertRaisesRegex(ValueError, '未能启动'):
                server.start_login_helper()

    def test_existing_helper_is_reused(self):
        with patch('server.login_page_ready', return_value=True), patch('server.subprocess.Popen') as spawn:
            self.assertTrue(server.start_login_helper()['ok'])
            spawn.assert_not_called()

    def test_only_completed_browser_session_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            state = LoginState()
            expected = {'cookie': 'synthetic', 'ticket': 'synthetic-ticket'}
            with patch('desk.browser_login.run_browser_login', return_value=expected):
                login.login_worker(state, data)
            self.assertEqual(json.loads((data / 'douyin-session.json').read_text()), expected)
            self.assertEqual(state.public()['status'], 'success')
            self.assertNotIn('synthetic', json.dumps(state.public()))

    def test_failure_does_not_replace_previous_session_or_leak_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            target = data / 'douyin-session.json'
            target.write_text('previous')
            state = LoginState()
            with patch('desk.browser_login.run_browser_login', side_effect=RuntimeError('secret-token-123')):
                login.login_worker(state, data)
            self.assertEqual(target.read_text(), 'previous')
            self.assertEqual(state.public()['status'], 'error')
            self.assertNotIn('secret-token', (data / 'login-diagnostic.json').read_text(encoding='utf-8'))

    def test_cancel_before_persistence_does_not_save(self):
        with tempfile.TemporaryDirectory() as directory:
            state = LoginState()
            def result(*args):
                state.cancel_event.set()
                return {'cookie': 'synthetic'}
            with patch('desk.browser_login.run_browser_login', side_effect=result):
                login.login_worker(state, Path(directory))
            self.assertFalse((Path(directory) / 'douyin-session.json').exists())
            self.assertEqual(state.public()['status'], 'cancelled')

    def test_controller_rejects_overlapping_start_then_allows_retry(self):
        state = LoginState()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker(*args):
            entered.set()
            release.wait(2)
        controller = login.LoginController(state, Path('.'))
        original = controller._run
        def run():
            try: original()
            finally: finished.set()
        controller._run = run
        with patch('login.login_worker', side_effect=worker):
            self.assertTrue(controller.start())
            self.assertTrue(entered.wait(1))
            self.assertFalse(controller.start())
            controller.cancel()
            self.assertTrue(state.cancel_event.is_set())
            release.set()
            self.assertTrue(finished.wait(1))
            finished.clear()
            self.assertTrue(controller.start())
            self.assertTrue(finished.wait(1))
            self.assertFalse(state.cancel_event.is_set())

    def test_http_2046_has_actionable_safe_message(self):
        message = login.safe_error(RuntimeError('error_code=2046 secret-123'))
        self.assertIn('官方页面', message)
        self.assertNotIn('secret-123', message)


if __name__ == '__main__':
    unittest.main()
