import json
import threading
import unittest
from types import SimpleNamespace

from desk.browser_login import (BrowserLoginError, CRYPT_KEY, PROTECT_KEY,
                                STORAGE_SCRIPT, _verification_response, _wait_for_session, extract_session)


def fixtures():
    encode = lambda obj: json.dumps({"data": json.dumps(obj)})
    return ([{"name": "sessionid", "value": "private-session", "domain": ".douyin.com"},
             {"name": "bd_ticket_guard_ts_sign_id", "value": "ts.2", "domain": ".douyin.com"}],
            {PROTECT_KEY: encode({"ticket": "private-ticket", "ts_sign": "ts.2.secret",
                                  "client_cert": "private-cert"}),
             CRYPT_KEY: encode({"ec_privateKey": "01" * 32})})


class State:
    def __init__(self):
        self.events = []
        self.cancel_event = threading.Event()

    def update(self, status, message):
        self.events.append((status, message))


class Context:
    def __init__(self, cookies, storage):
        self.cookie_data, self.storage = cookies, storage
        self.time = 0
        self.hooks = {}
        self.pages = [self]
        self.url = "https://www.douyin.com/"
        self.after_wait = lambda: None

    def on(self, event, fn): self.hooks[event] = fn
    def remove_listener(self, event, fn): self.hooks.pop(event)
    def is_closed(self): return False
    def evaluate(self, script, keys):
        assert keys == [PROTECT_KEY, CRYPT_KEY]
        return self.storage
    def cookies(self, url):
        assert url == "https://www.douyin.com/"
        return self.cookie_data
    def wait_for_timeout(self, ms):
        self.time += ms / 1000
        self.after_wait()


class BrowserLoginTests(unittest.TestCase):
    def test_complete_session(self):
        cookies, storage = fixtures()
        session = extract_session(cookies, storage)
        self.assertEqual(session["ticket"], "private-ticket")
        self.assertIn("sessionid=private-session", session["cookie"])

    def test_missing_or_empty_login_cookie_is_not_success(self):
        cookies, storage = fixtures()
        self.assertIsNone(extract_session([], storage))
        cookies[0]["value"] = ""
        self.assertIsNone(extract_session(cookies, storage))

    def test_missing_malformed_or_invalid_keys(self):
        cookies, storage = fixtures()
        for value in (None, "not JSON", json.dumps({"data": "{}"}),
                      json.dumps({"data": json.dumps({"ec_privateKey": "secret-bad-key"})})):
            storage[CRYPT_KEY] = value
            self.assertIsNone(extract_session(cookies, storage))

    def test_mismatched_sign_and_unrelated_domain(self):
        cookies, storage = fixtures()
        cookies[1]["value"] = "different"
        self.assertIsNone(extract_session(cookies, storage))
        cookies[0]["domain"] = "evil-douyin.com"
        self.assertIsNone(extract_session(cookies, storage))

    def test_2046_waits_then_eventual_success(self):
        cookies, storage = fixtures()
        context = Context([], storage)
        state = State()
        def advance():
            context.hooks["response"](SimpleNamespace(
                url="https://login.douyin.com/passport/web/check_qrconnect/",
                json=lambda: {"data": {"error_code": 2046}}))
            context.cookie_data = cookies
        context.after_wait = advance
        result = _wait_for_session(context, state, 5, clock=lambda: context.time)
        self.assertEqual(result["ticket"], "private-ticket")
        self.assertIn("verification_required", [event[0] for event in state.events])
        self.assertFalse(context.hooks)

    def test_missing_credentials_waits_and_times_out_without_secret(self):
        cookies, storage = fixtures()
        context = Context(cookies, {})
        state = State()
        with self.assertRaisesRegex(BrowserLoginError, "超时") as caught:
            _wait_for_session(context, state, 1, clock=lambda: context.time)
        output = repr(state.events) + str(caught.exception)
        self.assertIn("私信", output)
        self.assertNotIn("private-session", output)

    def test_closed_browser_and_cancel(self):
        context = Context([], {})
        context.pages = []
        with self.assertRaisesRegex(BrowserLoginError, "关闭"):
            _wait_for_session(context, State(), 1, clock=lambda: context.time)
        context.pages = [context]
        state = State()
        state.cancel_event.set()
        with self.assertRaisesRegex(BrowserLoginError, "取消"):
            _wait_for_session(context, state, 1, clock=lambda: context.time)

    def test_foreign_origin_never_read(self):
        cookies, storage = fixtures()
        context = Context(cookies, storage)
        context.url = "https://evil.example/"
        context.evaluate = lambda *args: self.fail("read foreign storage")
        with self.assertRaisesRegex(BrowserLoginError, "超时"):
            _wait_for_session(context, State(), 1, clock=lambda: context.time)

    def test_other_response_not_inspected(self):
        response = SimpleNamespace(url="https://other.example/passport/web/check_qrconnect/",
                                   json=lambda: self.fail("read foreign response"))
        self.assertFalse(_verification_response(response))

    def test_in_page_origin_guard_precedes_storage_reads(self):
        self.assertIn("location.origin !== 'https://www.douyin.com'", STORAGE_SCRIPT)
        self.assertLess(STORAGE_SCRIPT.index("location.origin"), STORAGE_SCRIPT.index("localStorage.getItem"))


if __name__ == "__main__":
    unittest.main()
