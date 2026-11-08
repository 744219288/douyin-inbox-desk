import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from desk.service import DeskService
from desk.integrations import IntegrationError


class Transport:
    def __init__(self):
        self.sent = []
        self.calls = []
        self.fail = False

    def connect(self, settings, on_message, on_status):
        self.calls.append("connect")
        self.receive = on_message
        self.status = on_status
        on_status("connected", "secret-should-never-appear")

    def send(self, cid, text):
        if self.fail:
            raise RuntimeError("secret-should-never-appear")
        self.sent.append((cid, text))

    def disconnect(self):
        self.calls.append("disconnect")


class ServiceTests(unittest.TestCase):
    def test_restart_marks_interrupted_send_uncertain_and_waiting(self):
        self.live()
        with self.service.lock:
            self.service._receive('live', 'c', '请问价格', 'm', None, schedule_reply=False)
            self.service._message('live:c', 'assistant', '尝试发送的回复', 'sending')
        self.service.close()
        self.service = DeskService(Path(self.tmp.name), lambda *_: '不应重发', self.transport)
        conversation = self.service.conversation('live:c')
        self.assertEqual(conversation['conversation']['status'], 'waiting')
        self.assertEqual(conversation['messages'][-1]['status'], 'failed')
        self.assertIn('核对', conversation['messages'][-1]['error'])
        self.service.connect()
        self.service.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [])

    def test_settings_change_replaces_pending_reply_without_losing_message(self):
        release, done = self.slow_reply()
        calls = []
        self.service.responder = lambda settings, history, text: calls.append(
            (settings['persona'], text)) or '按新设置回复'
        self.service.update_settings({'persona': '新的接待口吻'})
        self.wait_for(lambda: len(self.transport.sent) == 1)
        self.drain(release, done)
        self.assertEqual(calls, [('新的接待口吻', '你好')])
        self.assertEqual(self.transport.sent, [('c', '按新设置回复')])

    def test_settings_change_applies_new_handoff_keyword_to_pending_reply(self):
        release, done = self.slow_reply()
        self.service.update_settings({'handoff_keywords': ['你好']})
        self.drain(release, done)
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'], 'waiting')
        self.assertEqual(self.transport.sent, [])

    def test_settings_save_at_delivery_boundary_does_not_send_twice(self):
        self.live()
        delivery_entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        deliver = self.service._deliver

        def blocked_delivery(*args, **kwargs):
            delivery_entered.set()
            release.wait(3)
            return deliver(*args, **kwargs)

        self.service._deliver = blocked_delivery
        self.service.receive('c', 'u', '问题', 'm')
        self.assertTrue(delivery_entered.wait(1))
        self.service.update_settings({'persona': '更新口吻'})
        release.set()
        self.service.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [('c', '你好')])

    def test_manual_send_reserves_sending_before_delivery(self):
        self.live()
        self.service.receive('c', 'u', '转人工', 'm')
        delivery_entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        deliver = self.service._deliver
        errors = []

        def blocked_delivery(*args, **kwargs):
            delivery_entered.set()
            release.wait(3)
            return deliver(*args, **kwargs)

        def manual_send():
            try:
                self.service.send('live:c', '人工回复')
            except Exception as exc:
                errors.append(exc)

        self.service._deliver = blocked_delivery
        worker = threading.Thread(target=manual_send)
        worker.start()
        try:
            self.assertTrue(delivery_entered.wait(1))
            self.assertEqual(self.service.conversation('live:c')['messages'][-1]['status'], 'sending')
            self.service.update_settings({'persona': '新口吻'})
            self.assertEqual(self.service.conversation('live:c')['messages'][-1]['status'], 'sending')
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.transport.sent, [('c', '人工回复')])

    def test_restart_preserves_manual_takeover_for_interrupted_send(self):
        self.live()
        with self.service.lock:
            self.service._receive('live', 'c', '请问价格', 'm', None, schedule_reply=False)
            self.service.takeover('live:c')
            self.service._message('live:c', 'human', '人工回复', 'sending')
        self.service.close()
        self.service = DeskService(Path(self.tmp.name), lambda *_: '不应重发', self.transport)
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'], 'human')
        self.assertEqual(self.service.conversation('live:c')['messages'][-1]['status'], 'failed')
        self.service.connect()
        self.assertEqual(self.transport.sent, [])

    def test_settings_change_does_not_retry_failed_or_manual_conversations(self):
        self.live()
        self.transport.fail = True
        self.service.receive('failed', 'u', '问题', 'm')
        self.wait_for(lambda: self.service.conversation('live:failed')['conversation']['status'] == 'waiting')
        self.service.receive('manual', 'u', '转人工', 'm')
        self.service.takeover('live:manual')
        self.transport.fail = False
        self.service.update_settings({'persona': '新的口吻'})
        self.service.pool.shutdown(wait=True)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.service.conversation('live:failed')['conversation']['status'], 'waiting')
        self.assertEqual(self.service.conversation('live:manual')['conversation']['status'], 'human')

    def test_sticker_then_text_replies_and_preserves_media(self):
        self.live()
        self.service.receive('c','12','[表情包]','s',media_kind='sticker',media_url='https://p11-sign.douyinpic.com/a.webp')
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'],'ai')
        self.wait_for(lambda:len(self.transport.sent)==1)
        self.service.receive('c','12','1','t')
        self.wait_for(lambda:len(self.transport.sent)==2)
        rows=self.service.conversation('live:c')['messages']
        self.assertEqual(rows[0]['media_kind'],'sticker')
        self.assertTrue(rows[0]['media_url'])

    def test_sticker_merges_pending_context_without_resuming_human(self):
        self.live()
        with self.service.lock:
            self.service.receive('c','12','1','t')
            self.service.receive('c','12','[表情包]','s',media_kind='sticker')
        self.wait_for(lambda:len(self.transport.sent)==1)
        self.service.takeover('live:c')
        self.service.receive('c','12','[表情包]','s2',media_kind='sticker')
        self.service.receive('c','12','2','t2')
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'],'human')
        self.assertEqual(len(self.transport.sent),1)

    def test_recovered_sticker_and_text_generate_one_reply(self):
        self.live()
        self.service.recover_batch([
            {'conversation_id':'c','sender_id':'12','text':'[表情包]','message_id':'s','created_at_ms':1000,'media_kind':'sticker'},
            {'conversation_id':'c','sender_id':'12','text':'1','message_id':'t','created_at_ms':2000}])
        self.wait_for(lambda:len(self.transport.sent)==1)

    def test_recovered_batch_replies_once_and_merges_texts(self):
        self.live()
        events=[{'conversation_id':'c','sender_id':'12','text':text,'message_id':str(i),'created_at_ms':1000*i} for i,text in [(1,'你好'),(2,'问一下价格')]]
        self.transport.receive(recovery=True,batch=events)
        self.wait_for(lambda:len(self.transport.sent)==1)
        self.transport.receive(recovery=True,batch=events)
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(len(self.service.conversation('live:c')['messages']),3)

    def test_recovery_keeps_handoff_and_does_not_send(self):
        self.live()
        self.transport.receive(recovery=True,batch=[
            {'conversation_id':'c','sender_id':'12','text':'转人工','message_id':'1','created_at_ms':1000},
            {'conversation_id':'c','sender_id':'12','text':'你好','message_id':'2','created_at_ms':2000}])
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'],'waiting')
        self.assertEqual(self.transport.sent,[])

    def test_reconnect_resumes_cancelled_generation(self):
        self.live()
        with self.service.lock:
            self.service.receive('c','12','你好','1')
            self.service.disconnect()
        self.service.connect()
        self.wait_for(lambda:len(self.transport.sent)==1)
        self.service.disconnect()
        self.service.connect()
        self.assertEqual(len(self.transport.sent),1)

    def test_reconnect_never_retries_uncertain_inflight_send(self):
        self.live()
        with self.service.lock:
            self.service._receive('live','c','你好','1',None,schedule_reply=False)
            self.service._message('live:c','assistant','已经尝试','sending')
        self.service.disconnect()
        self.service.connect()
        self.assertEqual(self.transport.sent,[])

    def test_stranger_batch_rejected_on_disconnect_is_not_acknowledged(self):
        self.live()
        callback = self.transport.receive
        self.transport.status('disconnected')
        self.assertFalse(callback(batch=[
            {'conversation_id':'c','sender_id':'12','text':'你好','message_id':'1'}]))
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.service.state()['conversations'], [])

    def test_stranger_batch_handoff_prevents_later_text_auto_reply(self):
        self.live()
        self.transport.receive(batch=[
            {'conversation_id':'c','sender_id':'12','text':'转人工','message_id':'1'},
            {'conversation_id':'c','sender_id':'12','text':'补充问题','message_id':'2'}])
        self.assertEqual(self.service.conversation('live:c')['conversation']['status'],'waiting')
        self.assertEqual(self.transport.sent, [])

    def test_model_401_is_visible_and_never_sends(self):
        def denied(*args):
            raise IntegrationError(401)
        self.service.responder = denied
        self.live()
        cid = self.service.receive('test', 'sender', 'hello', 'new')
        self.wait_for(lambda: self.service.conversation(cid)['conversation']['status'] == 'waiting')
        messages = self.service.conversation(cid)['messages']
        self.assertIn('HTTP 401', messages[-1]['error'])
        self.assertEqual(self.transport.sent, [])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.transport = Transport()
        self.service = DeskService(Path(self.tmp.name), lambda *_: "你好", self.transport)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def live(self):
        self.service.update_settings({"mode": "live", "deepseek_api_key": "secret-should-never-appear"})
        self.service.connect()

    def test_connection_error_preserves_only_exact_safe_configuration_message(self):
        self.live()
        detail = "私信发送需要配套签名凭证：设置中的 Cookie 覆盖与扫码凭证不一致，请勾选「清除 Cookie 覆盖」后保存，无需重新扫码。"
        self.transport.status("error", detail)
        self.assertEqual(self.service.state()['connection']['detail'], detail)
        self.transport.status("error", detail + " secret-token")
        self.assertNotIn("secret-token", json.dumps(self.service.state()))

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("background task did not reach expected state")

    def slow_reply(self):
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        def responder(*_):
            started.set()
            release.wait(3)
            done.set()
            return "慢回复"
        self.service.responder = responder
        self.addCleanup(release.set)
        self.live()
        self.service.receive("c", "u", "你好", "m")
        self.assertTrue(started.wait(1))
        return release, done

    def drain(self, release, done):
        release.set()
        self.assertTrue(done.wait(1))
        self.service.pool.shutdown(wait=True)

    def test_demo_has_no_external_calls(self):
        self.service.responder = lambda *_: self.fail("demo called real responder")
        self.service.connect()
        cid = self.service.demo_message("你好")["conversation_id"]
        self.wait_for(lambda: any(m["status"] == "sent" for m in self.service.conversation(cid)["messages"]))
        self.service.send(cid, "人工演示")
        self.service.disconnect()
        self.assertEqual([], self.transport.calls)
        self.assertEqual([], self.transport.sent)
        self.assertIn("【演示回复】", self.service.conversation(cid)["messages"][1]["text"])

    def test_deduplication_survives_restart(self):
        self.live()
        self.service.receive("c", "u", "你好", "m")
        self.wait_for(lambda: len(self.transport.sent) == 1)
        self.service.receive("c", "u", "你好", "m")
        self.service.close()
        self.service = DeskService(Path(self.tmp.name), lambda *_: "你好", self.transport)
        self.service.connect()
        self.service.receive("c", "u", "你好", "m")
        self.assertEqual(2, len(self.service.conversation("live:c")["messages"]))
        self.assertEqual(1, len(self.transport.sent))

    def test_takeover_cancels_slow_reply_and_resume_does_not_replay(self):
        release, done = self.slow_reply()
        self.service.takeover("live:c")
        self.service.resume("live:c")
        self.drain(release, done)
        self.assertEqual([], self.transport.sent)
        self.assertEqual("cancelled", self.service.conversation("live:c")["messages"][1]["status"])

    def test_disconnect_cancels_and_stale_callback_is_ignored(self):
        release, done = self.slow_reply()
        callback = self.transport.receive
        self.service.disconnect()
        self.drain(release, done)
        callback("other", "u", "hello", "new")
        self.assertEqual(1, self.service.state()["stats"]["total"])
        self.assertEqual([], self.transport.sent)

    def test_mode_isolation_cancels_and_uses_raw_transport_id(self):
        release, done = self.slow_reply()
        self.service.update_settings({"mode": "demo"})
        self.drain(release, done)
        self.assertEqual([], self.service.state()["conversations"])
        with self.assertRaises(ValueError):
            self.service.send("live:c", "不能发送")
        self.service.update_settings({"mode": "live"})
        self.service.connect()
        self.service.send("live:c", "人工消息")
        self.assertEqual([("c", "人工消息")], self.transport.sent)

    def test_generation_and_send_failures_mark_waiting_without_secrets(self):
        self.live()
        def fail(*_):
            raise RuntimeError("secret-should-never-appear")
        self.service.responder = fail
        self.service.receive("c", "u", "你好", "m")
        self.wait_for(lambda: self.service.state()["stats"]["waiting"] == 1)
        self.transport.fail = True
        with self.assertRaises(ValueError):
            self.service.send("live:c", "人工回复")
        detail = self.service.conversation("live:c")
        self.assertEqual("failed", detail["messages"][-1]["status"])
        self.assertNotIn("secret-should-never-appear", json.dumps([self.service.state(), detail]))

    def test_keywords_and_nontext_request_human_without_ai(self):
        self.live()
        self.service.responder = lambda *_: self.fail("handoff triggered responder")
        self.service.receive("c", "u", "我要人工", "m")
        self.service.receive("d", "u", "[非文字消息，请人工处理]", "n")
        self.assertEqual(2, self.service.state()["stats"]["waiting"])
        self.assertEqual([], self.transport.sent)

    def test_invalid_extension_is_atomic_and_secret_blank_preserves(self):
        with self.assertRaises(ValueError):
            self.service.update_settings({"knowledge_enabled": True, "mode": "live"})
        self.assertEqual("demo", self.service.state()["mode"])
        self.service.update_settings({"deepseek_api_key": "secret-should-never-appear"})
        self.service.update_settings({"deepseek_api_key": ""})
        self.assertTrue(self.service.state()["settings"]["deepseek_key_set"])
        self.service.update_settings({"clear_deepseek_key": True})
        self.assertFalse(self.service.state()["settings"]["deepseek_key_set"])

    def test_demo_new_conversation_and_explicit_existing_conversation(self):
        first = self.service.demo_message("第一位")["conversation_id"]
        second = self.service.demo_message("第二位")["conversation_id"]
        self.assertNotEqual(first, second)
        self.assertEqual("演示访客 01", self.service.conversation(first)["conversation"]["name"])
        self.assertEqual("演示访客 02", self.service.conversation(second)["conversation"]["name"])
        same = self.service.demo_message("继续", first)["conversation_id"]
        self.assertEqual(first, same)
        self.assertEqual(2, self.service.state()["stats"]["total"])

    def test_extension_url_restrictions_are_atomic(self):
        for url in ("https://user:secret@example.com/api", "https://example.com/api#secret",
                    "http://example.com/api", "https://@example.com", "https://example.com:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.service.update_settings({"knowledge_url": url})
            self.assertEqual("", self.service.state()["settings"]["knowledge_url"])
        for url in ("http://127.0.0.1:8000/query", "http://localhost/query", "http://[::1]:8000/query", "https://example.com/query"):
            self.service.update_settings({"knowledge_url": url, "knowledge_enabled": True})
            self.assertEqual(url, self.service.state()["settings"]["knowledge_url"])

    def test_settings_limits_and_clear_cookie(self):
        for invalid in ({"deepseek_model": "  "}, {"deepseek_model": "x" * 129},
                        {"persona": "x" * 6001}, {"handoff_keywords": ["x"] * 33},
                        {"handoff_keywords": ["x" * 81]}, {"handoff_keywords": [" "]}):
            with self.subTest(invalid=next(iter(invalid))), self.assertRaises(ValueError):
                self.service.update_settings(invalid)
        self.service.update_settings({"douyin_cookie": "private-cookie"})
        self.service.update_settings({"douyin_cookie": ""})
        self.assertTrue(self.service.state()["settings"]["douyin_cookie_set"])
        self.service.update_settings({"clear_douyin_cookie": True})
        self.assertFalse(self.service.state()["settings"]["douyin_cookie_set"])
        self.service.close()
        self.service = DeskService(Path(self.tmp.name), lambda *_: "你好", self.transport)
        self.assertFalse(self.service.state()["settings"]["douyin_cookie_set"])

    def test_new_message_cancels_previous_generation_and_history_is_isolated(self):
        release, done = self.slow_reply()
        calls = []
        self.service.responder = lambda settings, history, text: calls.append((history, text)) or "新回复"
        self.service.receive("c", "u", "第二条", "m2")
        self.wait_for(lambda: len(self.transport.sent) == 1)
        self.drain(release, done)
        self.assertEqual([("c", "新回复")], self.transport.sent)
        self.assertEqual(([{"role": "user", "text": "你好"}], "第二条"), calls[0])

    def test_takeover_response_waits_for_already_started_send(self):
        self.live()
        sending, release, taken = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked_send(cid, text):
            sending.set()
            release.wait(3)
            self.transport.sent.append((cid, text))
        self.transport.send = blocked_send
        self.service.receive("c", "u", "你好", "m")
        self.assertTrue(sending.wait(1))
        def takeover():
            self.service.takeover("live:c")
            taken.set()
        worker = threading.Thread(target=takeover)
        worker.start()
        self.assertFalse(taken.wait(0.05))
        release.set()
        worker.join(1)
        self.assertTrue(taken.is_set())
        self.service.receive("c", "u", "后续消息", "m2")
        self.service.pool.shutdown(wait=True)
        self.assertEqual(1, len(self.transport.sent))
        self.assertEqual("human", self.service.conversation("live:c")["conversation"]["status"])

    def test_resume_only_future_inbound_generates_and_history_is_per_user(self):
        self.live()
        self.service.receive("c", "u", "人工", "m")
        self.service.resume("live:c")
        self.assertEqual([], self.transport.sent)
        calls = []
        self.service.responder = lambda settings, history, text: calls.append((history, text)) or "回复"
        self.service.receive("d", "other", "新用户", "m")
        self.wait_for(lambda: len(self.transport.sent) == 1)
        self.assertEqual(([], "新用户"), calls[0])
        self.service.receive("c", "u", "恢复后新问题", "m2")
        self.wait_for(lambda: len(self.transport.sent) == 2)
        self.assertEqual(([{"role": "user", "text": "人工"}], "恢复后新问题"), calls[1])

    def blocked_send(self):
        sending, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def send(cid, text):
            sending.set()
            release.wait(3)
            self.transport.sent.append((cid, text))
        self.transport.send = send
        self.service.receive("c", "u", "你好", "m")
        self.assertTrue(sending.wait(1))
        return release

    def test_slow_send_does_not_block_state_or_other_conversation_takeover(self):
        self.live()
        self.service.receive("other", "u2", "人工", "m2")
        release = self.blocked_send()
        completed, errors = threading.Event(), []
        def inspect_and_takeover():
            try:
                self.assertEqual(2, self.service.state()["stats"]["total"])
                self.service.takeover("live:other")
                self.assertEqual("human", self.service.conversation("live:other")["conversation"]["status"])
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()
        worker = threading.Thread(target=inspect_and_takeover)
        worker.start()
        try:
            self.assertTrue(completed.wait(0.5), "unrelated operations blocked on network send")
            self.assertEqual([], errors)
        finally:
            release.set()
            worker.join(2)
            self.service.pool.shutdown(wait=True)

    def test_disconnect_records_actual_inflight_send_success(self):
        self.live()
        release = self.blocked_send()
        self.service.disconnect()
        release.set()
        self.service.pool.shutdown(wait=True)
        self.assertEqual("sent", self.service.conversation("live:c")["messages"][-1]["status"])

    def test_close_defers_database_close_until_send_result_is_saved(self):
        self.live()
        release = self.blocked_send()
        self.service.close()
        self.assertFalse(self.service.db_closed)
        release.set()
        self.service.pool.shutdown(wait=True)
        self.assertTrue(self.service.db_closed)
        self.service = DeskService(Path(self.tmp.name), lambda *_: "你好", self.transport)
        self.assertEqual("sent", self.service.conversation("live:c")["messages"][-1]["status"])

    def test_takeover_during_prepare_cancels_before_send(self):
        self.live()
        preparing, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def prepare(raw):
            preparing.set()
            release.wait(3)
            return {"route": raw}
        self.transport.prepare = prepare
        self.transport.send_prepared = lambda route, text: self.transport.sent.append((route["route"], text))
        self.service.receive("c", "u", "你好", "m")
        self.assertTrue(preparing.wait(1))
        self.service.takeover("live:c")
        release.set()
        self.service.pool.shutdown(wait=True)
        self.assertEqual([], self.transport.sent)
        self.assertEqual("cancelled", self.service.conversation("live:c")["messages"][-1]["status"])

    def test_prepared_send_uses_returned_route(self):
        self.live()
        self.transport.prepare = lambda raw: {"route": raw, "token": "opaque"}
        routes = []
        self.transport.send_prepared = lambda route, text: routes.append((route, text))
        self.service.receive("c", "u", "你好", "m")
        self.wait_for(lambda: len(routes) == 1)
        self.assertEqual(({"route": "c", "token": "opaque"}, "你好"), routes[0])
        self.assertEqual([], self.transport.sent)


if __name__ == "__main__":
    unittest.main()
