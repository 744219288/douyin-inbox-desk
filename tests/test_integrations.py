import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from urllib import request, error

from desk.integrations import respond, post_json, HandoffRequired
from desk.service import DeskService
from server import make_server


class NeverTransport:
    def send(self, *args):
        raise AssertionError("demo used live transport")
    def connect(self, *args):
        raise AssertionError("demo connected externally")
    def disconnect(self):
        raise AssertionError("demo disconnected live transport")


class IntegrationTests(unittest.TestCase):
    def test_upstream_http_enforces_certificate_checks(self):
        from desk.transport import harden_http
        calls = []
        class Session:
            def request(self, method, url, **kwargs):
                calls.append(kwargs)
        client = SimpleNamespace(Session=Session, request=lambda method, url, **kwargs: calls.append(kwargs))
        harden_http(client)
        harden_http(client)
        client.request("POST", "https://example.test", verify=False, timeout=100)
        client.Session().request("GET", "https://example.test", verify=False, timeout=None)
        self.assertEqual(calls, [{"verify": True, "timeout": 20}, {"verify": True, "timeout": 20}])

    def test_prepared_send_cannot_survive_disconnect(self):
        from desk.transport import DouyinTransport
        transport = DouyinTransport(Path("unused-test-data"))
        auth = object()
        calls = []
        api = SimpleNamespace(create_conversation=lambda a, uid: ("cid", 123, "ticket"),
                              get_identity_security_token=lambda a: ("token", "device"),
                              send_msg=lambda *a, **kw: calls.append(a) or True)
        transport._connected = True
        transport._auth, transport._api = auth, api
        transport._routes = {"cid": "42"}
        prepared = transport.prepare("cid")
        transport.disconnect()
        with self.assertRaises(RuntimeError):
            transport.send_prepared(prepared, "must not send")
        self.assertEqual(calls, [])

    def test_optional_extensions_and_deepseek_order(self):
        replies = [{"documents": [{"text": "只营业到18点", "source": "营业规则"}]},
                   {"context": "今天正常营业"},
                   {"choices": [{"message": {"content": "今天营业到18点。"}, "finish_reason": "stop"}]}]
        settings = {"deepseek_api_key": "private-test", "knowledge_enabled": True,
                    "knowledge_url": "https://kb.example/search", "agent_enabled": True,
                    "agent_url": "https://agent.example/query"}
        with patch("desk.integrations.post_json", side_effect=replies) as post:
            self.assertEqual(respond(settings, [], "几点下班？"), "今天营业到18点。")
            calls = post.call_args_list
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[1].args[1]["knowledge"][0]["source"], "营业规则")
            messages = calls[2].args[1]["messages"]
            self.assertEqual(messages[0]["role"], "system")
            self.assertNotIn("private-test", json.dumps(messages))
            self.assertEqual(messages[-1]["content"], "几点下班？")

    def test_no_extensions_when_disabled(self):
        with patch("desk.integrations.post_json", return_value={"choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}]}) as post:
            respond({"deepseek_api_key": "x"}, [], "你好")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")

    def test_agent_handoff_stops_model(self):
        with patch("desk.integrations.post_json", return_value={"handoff": True}) as post:
            with self.assertRaises(HandoffRequired):
                respond({"deepseek_api_key": "x", "agent_enabled": True, "agent_url": "https://agent.example"}, [], "退款")
            self.assertEqual(post.call_count, 1)

    def test_truncated_answer_not_sent(self):
        with patch("desk.integrations.post_json", return_value={"choices": [{"message": {"content": "半句话"}, "finish_reason": "length"}]}):
            with self.assertRaises(HandoffRequired):
                respond({"deepseek_api_key": "x"}, [], "你好")

    def test_invalid_extension_url_never_requested(self):
        for url in ["file:///etc/passwd", "http://remote.example", "https://user:secret@example.org", "https://example.org/#secret"]:
            with self.assertRaises(ValueError):
                post_json(url, {})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests")
        self.desk = DeskService(Path(self.tmp.name), lambda *args: "not used", NeverTransport())
        self.server = make_server(0, service=self.desk)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with request.urlopen(self.base) as res:
            html = res.read().decode()
        self.token = html.split('name="desk-token" content="')[1].split('"')[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.desk.close()
        self.tmp.cleanup()

    def post(self, path, data, token=None, origin=None):
        headers = {"Content-Type": "application/json", "X-Desk-Token": token or self.token}
        if origin:
            headers["Origin"] = origin
        req = request.Request(self.base + path, json.dumps(data).encode(), headers, method="POST")
        with request.urlopen(req) as res:
            return json.load(res)

    def test_csrf_and_secret_boundaries(self):
        with self.assertRaises(error.HTTPError) as caught:
            self.post("/api/settings", {}, token="bad")
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(error.HTTPError) as caught:
            self.post("/api/settings", {}, origin="https://evil.example")
        self.assertEqual(caught.exception.code, 403)
        result = self.post("/api/settings", {"deepseek_api_key": "test-secret-123"})
        self.assertNotIn("test-secret-123", json.dumps(result))
        for path in ["/data/desk.sqlite3", "/vendor/Douyin_Spider/.env"]:
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(self.base + path)
            self.assertEqual(caught.exception.code, 404)

    def test_demo_handoff_and_manual_send_over_http(self):
        result = self.post("/api/demo/message", {"text": "我要转人工"})
        cid = result["conversation_id"]
        with request.urlopen(self.base + "/api/conversations/" + cid) as res:
            self.assertEqual(json.load(res)["conversation"]["status"], "waiting")
        self.post(f"/api/conversations/{cid}/takeover", {})
        result = self.post(f"/api/conversations/{cid}/send", {"text": "我来处理"})
        self.assertEqual(result["messages"][-1]["status"], "sent")
        self.assertEqual(result["conversation"]["status"], "human")


class ProtoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from desk.transport import load_vendor
        try:
            _, _, _, cls.Live, cls.Response = load_vendor()
        except RuntimeError:
            raise unittest.SkipTest("optional live dependencies not installed")

    def frame(self, sender=12, msg_type=7, conversation_type=1):
        response = self.Response.Response()
        msg = response.body.new_message_notify.message
        msg.conversation_id = "test-conversation"
        msg.conversation_type = conversation_type
        msg.server_message_id = 111
        msg.sender = sender
        msg.message_type = msg_type
        msg.content = json.dumps({"text": "你好"})
        frame = self.Live.PushFrame()
        frame.payloadType = "pb"
        frame.payload = response.SerializeToString()
        return frame.SerializeToString()

    def test_preserve_self_but_ignore_groups_and_receipts(self):
        from desk.transport import parse_incoming
        owner = parse_incoming(self.frame(sender=99), '99', self.Live, self.Response)
        self.assertTrue(owner['is_outgoing'])
        self.assertEqual(owner['sender_id'], '99')
        for raw in [self.frame(conversation_type=2), self.frame(msg_type=50001)]:
            self.assertIsNone(parse_incoming(raw, "99", self.Live, self.Response))

    def test_text_and_media_handling(self):
        from desk.transport import parse_incoming
        msg = parse_incoming(self.frame(), "99", self.Live, self.Response)
        self.assertEqual(msg["text"], "你好")
        self.assertEqual(msg["message_id"], "111")
        msg = parse_incoming(self.frame(msg_type=27), "99", self.Live, self.Response)
        self.assertEqual(msg["media_kind"], "image")


if __name__ == "__main__":
    unittest.main()
