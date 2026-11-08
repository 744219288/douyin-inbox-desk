import json
import unittest
from unittest.mock import patch
from desk.stranger import _field as f, _fields, parse_response, parse_messages_response, fetch_stranger, StrangerError


def record(sender=22, identity=99, kind=7, text="hello", conv_type=1):
    msg = (f(1, b"0:1:11:22") + f(2, conv_type) + f(3, identity) + f(4, 123456)
           + f(5, 777) + f(6, kind) + f(7, sender)
           + f(8, json.dumps({"text": text}).encode()) + f(10, 1700000000000))
    return f(1, 777) + f(3, msg) + f(4, b"0:1:11:22")


def response(*records, code=0, status=b"OK", more=False, cursor=0):
    inbox = f(1, cursor) + f(2, int(more)) + b"".join(f(4, r) for r in records)
    return f(1, 1001) + f(3, code) + f(4, status) + f(6, f(1000, inbox)) + f(11, 1700000000010)


class StrangerTests(unittest.TestCase):
    def test_shared_read_spacing_can_be_cancelled(self):
        import threading, time
        from types import SimpleNamespace
        from desk.stranger import _wait_read_slot
        stopped=threading.Event()
        stopped.set()
        auth=SimpleNamespace(_desk_stranger_interval=60,_desk_stranger_last_request=time.monotonic(),_desk_stranger_stop=stopped)
        with self.assertRaises(StrangerError):
            _wait_read_slot(auth)

    def test_rate_limit_requires_explicit_server_description(self):
        for description, expected in [(b'too many requests',True),(b'unknown error',False)]:
            with self.assertRaises(StrangerError) as caught:
                parse_response(response(code=409,status=description),'11')
            self.assertEqual(caught.exception.code,409)
            self.assertEqual(caught.exception.rate_limited,expected)

    def test_snapshot_identity_and_timestamp(self):
        result = parse_response(response(record()), "11")
        event = result["events"][0]
        self.assertEqual(event["sender_id"], "22")
        self.assertEqual(event["message_id"], "99")
        self.assertEqual(event["conversation_short_id"], "777")
        self.assertEqual(event["text"], "hello")
        self.assertEqual(event["created_at_ms"], 1700000000000)
        self.assertEqual(result["server_time_ms"], 1700000000010)

    def test_empty_snapshot_is_not_error(self):
        self.assertEqual(parse_response(response(), 11)["events"], [])

    def test_own_group_receipt_missing_identity_filtered(self):
        result = parse_response(response(record(sender=11), record(conv_type=2),
                                         record(kind=50001), record(identity=0)), 11)
        self.assertEqual(result["events"], [])

    def test_duplicate_snapshot_and_saturation(self):
        result = parse_response(response(record(), record(), more=True, cursor=2), 11, count=2)
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(result["truncated"])

    def test_nontext_is_handoff_placeholder(self):
        event = parse_response(response(record(kind=27)), 11)["events"][0]
        self.assertEqual(event["text"], "[图片]")

    def test_system_notice_does_not_trigger_handoff(self):
        events=parse_response(response(record(kind=1,identity=98),record()),11)['events']
        self.assertEqual([e['message_id'] for e in events],['99'])

    def test_error_does_not_echo_remote_content(self):
        with self.assertRaises(StrangerError) as caught:
            parse_response(response(code=403, status=b"SECRET"), 11)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_malformed_protobuf_rejected(self):
        for data in (b"\x00", b"\x0a\x06x", b"\x08\x80", b"\x0b", b"\x08" + b"\xff" * 10):
            with self.subTest(data=data), self.assertRaises(StrangerError):
                _fields(data)

    def test_missing_inbox_not_silently_empty(self):
        with self.assertRaises(StrangerError):
            parse_response(f(1, 1001) + f(4, b"OK"), 11)

    def test_type_confusion_rejected(self):
        with self.assertRaises(StrangerError):
            parse_response(f(1, b"1001") + f(4, b"OK"), 11)

    def test_unknown_fixed_fields_skipped(self):
        self.assertEqual(_fields(b"\x09" + b"x" * 8 + b"\x15" + b"x" * 4)[2], [b"xxxx"])

    def test_history_includes_prior_handoff_and_latest(self):
        old = _fields(record(identity=98, text="转人工"))[3][0]
        new = _fields(record(identity=99, text="hello"))[3][0]
        wire = f(1, 1002) + f(4, b"OK") + f(6, f(1001, f(3, new) + f(3, old)))
        events = parse_messages_response(wire, 11, 777)
        self.assertEqual([e["text"] for e in events], ["转人工", "hello"])
        with self.assertRaises(StrangerError):
            parse_messages_response(wire, 11, 778)

    def test_history_missing_body_rejected(self):
        with self.assertRaises(StrangerError):
            parse_messages_response(f(1, 1002) + f(4, b"OK"), 11, 777)

    def test_pagination_gathers_all_and_rejects_stuck_cursor(self):
        class Auth:
            def get_uid(self): return 11
        with patch("desk.stranger.build_request", return_value=b"request"), patch("desk.stranger._post") as post:
            post.side_effect = [response(record(identity=98), more=True, cursor=10), response(record(identity=99))]
            result = fetch_stranger(Auth())
            self.assertEqual(len(result["events"]), 2)
            self.assertFalse(result["truncated"])
            post.side_effect = [response(more=True, cursor=0)]
            with self.assertRaises(StrangerError): fetch_stranger(Auth())


if __name__ == "__main__":
    unittest.main()
