"""Read-only stranger inbox adapter; credentials always come from this login.

Wire evidence: Rockedw/douyin-web-api-sdk, src/main/proto/
DyStrangerMsg{Request,Response}.proto and StrangerMessageFetcher.java.
Pagination and history schema additionally verified against zhinjs/douyin-im,
src/services/im/proto/im.proto and src/services/im/stranger.ts.
"""
import json
import time

URL = "https://imapi.douyin.com/v1/stranger/get_conversation_list"
MAX_BYTES = 4 * 1024 * 1024


class StrangerError(RuntimeError):
    def __init__(self, message, *, code=None, rate_limited=False):
        super().__init__(message)
        self.code = code
        self.rate_limited = rate_limited


def _varint(value):
    if not isinstance(value, int) or value < 0 or value >= 2 ** 64:
        raise ValueError("invalid unsigned integer")
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number, value):
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _fields(data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise StrangerError("陌生人消息响应格式异常")
    pos = 0
    result = {}

    def read():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise StrangerError("陌生人消息响应不完整")
            byte = data[pos]
            pos += 1
            if shift == 63 and byte > 1:
                raise StrangerError("陌生人消息响应格式异常")
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise StrangerError("陌生人消息响应格式异常")

    while pos < len(data):
        tag = read()
        number, kind = tag >> 3, tag & 7
        if number == 0:
            raise StrangerError("陌生人消息响应格式异常")
        if kind == 0:
            value = read()
        elif kind in (1, 2, 5):
            length = read() if kind == 2 else (8 if kind == 1 else 4)
            if length > len(data) - pos:
                raise StrangerError("陌生人消息响应不完整")
            value = data[pos:pos + length]
            pos += length
        else:
            raise StrangerError("陌生人消息响应格式异常")
        result.setdefault(number, []).append(value)
    return result


def _one(fields, number, default=None):
    values = fields.get(number, [])
    if len(values) > 1:
        raise StrangerError("陌生人消息响应字段重复")
    return values[0] if values else default


def _integer(fields, number, default=0):
    value = _one(fields, number, default)
    if not isinstance(value, int):
        raise StrangerError("陌生人消息响应字段类型异常")
    return value


def _text(fields, number):
    value = _one(fields, number, b"")
    try:
        return value.decode("utf-8")
    except (AttributeError, UnicodeError):
        raise StrangerError("陌生人消息响应文字异常") from None


def message_identity(fields, own_uid):
    """MessageBody.ext is field 9 (zhin-im.proto); pinned protobuf omits it.

    Never infer an echo from text. Missing client IDs remain empty.
    """
    client_id = ''
    for raw in fields.get(9, []):
        entry = _fields(raw)
        if _text(entry, 1) == 's:client_message_id':
            value = _text(entry, 2)
            if client_id and client_id != value:
                raise StrangerError('消息客户端标识冲突')
            client_id = value
    sender = str(_integer(fields, 7))
    outgoing = sender == str(own_uid)
    peer = '' if outgoing else sender
    parts = _text(fields, 1).split(':')
    if outgoing and len(parts) == 4 and parts[:2] == ['0', '1'] and str(own_uid) in parts[2:]:
        peer = next((part for part in parts[2:] if part != str(own_uid) and part.isdigit()), '')
    return {'is_outgoing': outgoing, 'client_message_id': client_id, 'peer_id': peer}


def build_request(auth, count=100, cursor=0):
    if not 1 <= count <= 100:
        raise ValueError("count must be 1..100")
    from builder.proto import ProtoBuilder
    request = ProtoBuilder.build_normal_request(auth, 1001)
    request.inbox_type = 1
    request.ClearField("body")
    query = _field(1, cursor) + _field(2, count) + _field(3, 1)
    return request.SerializeToString() + _field(8, _field(1000, query))


def parse_response(data, own_uid, count=100, include_self=False):
    top = _fields(data)
    code = _integer(top, 3)
    if code or _text(top, 4) != "OK":
        raise StrangerError(f"陌生人消息读取被拒绝（状态码 {code}）", code=code,
                            rate_limited=_text(top, 4).strip().lower() == 'too many requests')
    if _integer(top, 1) != 1001:
        raise StrangerError("陌生人消息响应命令不匹配")
    body = _fields(_one(top, 6, b""))
    if 1000 not in body:
        raise StrangerError("陌生人消息响应缺少收件箱数据")
    inbox = _fields(_one(body, 1000))
    records = inbox.get(4, [])
    events = []
    seen = set()
    for record in records:
        conversation = _fields(record)
        message = _fields(_one(conversation, 3, b""))
        sender = _integer(message, 7)
        msg_id = _integer(message, 3)
        msg_type = _integer(message, 6)
        conv_id = _text(message, 1) or _text(conversation, 4)
        short_id = _integer(message, 5) or _integer(conversation, 1)
        if (_integer(message, 2) != 1 or not sender or (str(sender) == str(own_uid) and not include_self)
                or not msg_id or not conv_id or not short_id or msg_type in (1, 50001)):
            continue
        key = (conv_id, msg_id)
        if key in seen:
            continue
        seen.add(key)
        if msg_type == 7:
            try:
                payload = json.loads(_text(message, 8))
            except (ValueError, TypeError):
                raise StrangerError("陌生人文字消息格式异常") from None
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
        else:
            text = "[非文字消息，请人工处理]"
        from .media import decode_content
        decoded = decode_content(msg_type, _text(message, 8))
        if not decoded['text']:
            continue
        events.append({"conversation_id": conv_id, "conversation_short_id": str(short_id),
                       "sender_id": str(sender), "message_id": str(msg_id),
                       **decoded, "message_type": msg_type,
                       "created_at_ms": _integer(message, 10),
                       "sec_uid": '' if str(sender) == str(own_uid) else _text(message, 14),
                       **message_identity(message, own_uid)})
    return {"events": events, "server_time_ms": _integer(top, 11) or _integer(top, 10),
            "count": len(records), "truncated": bool(_integer(inbox, 2)),
            "has_more": bool(_integer(inbox, 2)), "next_cursor": _integer(inbox, 1)}


def _wait_read_slot(auth):
    interval = getattr(auth, '_desk_stranger_interval', 0)
    remaining = interval - (time.monotonic() - getattr(auth, '_desk_stranger_last_request', -interval))
    stop = getattr(auth, '_desk_stranger_stop', None)
    if stop is not None and stop.is_set():
        raise StrangerError('陌生人补拉已停止')
    if remaining > 0:
        if stop is not None:
            if stop.wait(remaining):
                raise StrangerError('陌生人补拉已停止')
        else:
            time.sleep(remaining)
    auth._desk_stranger_last_request = time.monotonic()


def _post(auth, url, payload):
    from builder.header import HeaderBuilder, HeaderType
    from utils import http_client
    headers = HeaderBuilder.build(HeaderType.PROTOBUF)
    headers.set_header("referer", "https://www.douyin.com/")
    _wait_read_slot(auth)
    try:
        response = http_client.post(url, headers=headers.get(), cookies=auth.cookie,
                                    data=payload, verify=True, timeout=20,
                                    allow_redirects=False)
        if response.status_code != 200:
            raise StrangerError(f"陌生人消息读取失败（HTTP {int(response.status_code)}）",
                                code=int(response.status_code), rate_limited=response.status_code == 429)
        if len(response.content) > MAX_BYTES:
            raise StrangerError("陌生人消息响应超过大小限制")
        return response.content
    except StrangerError:
        raise
    except Exception:
        raise StrangerError("陌生人消息读取失败，请检查连接或登录状态") from None


def fetch_stranger(auth, count=100):
    cursor = 0
    visited = set()
    events = {}
    total = 0
    for _ in range(10):
        page = parse_response(_post(auth, URL, build_request(auth, count, cursor)), auth.get_uid(), count, include_self=True)
        total += page["count"]
        for event in page["events"]:
            events[(event["conversation_id"], event["message_id"])] = event
        if not page["has_more"]:
            return {**page, "events": list(events.values()), "count": total, "truncated": False}
        visited.add(cursor)
        cursor = page["next_cursor"]
        if cursor in visited:
            raise StrangerError("陌生人消息分页游标没有推进")
    raise StrangerError("陌生人消息超过本次读取上限，请人工检查收件箱")


def build_messages_request(auth, conversation_short_id):
    short_id = int(conversation_short_id)
    if not 0 < short_id < 2 ** 63:
        raise ValueError("invalid conversation short id")
    from builder.proto import ProtoBuilder
    request = ProtoBuilder.build_normal_request(auth, 1002)
    request.inbox_type = 1
    request.ClearField("body")
    query = _field(1, short_id) + _field(2, 0)  # Never mark read during polling.
    return request.SerializeToString() + _field(8, _field(1001, query))


def parse_messages_response(data, own_uid, conversation_short_id, include_self=False):
    top = _fields(data)
    code = _integer(top, 3)
    if code or _text(top, 4) != "OK":
        raise StrangerError(f"陌生人会话消息读取被拒绝（状态码 {code}）", code=code,
                            rate_limited=_text(top, 4).strip().lower() == 'too many requests')
    if _integer(top, 1) != 1002:
        raise StrangerError("陌生人会话消息响应命令不匹配")
    body = _fields(_one(top, 6, b""))
    if 1001 not in body:
        raise StrangerError("陌生人会话响应缺少消息数据")
    messages = _fields(_one(body, 1001)).get(3, [])
    # Reuse exactly the same message validation/filtering as list snapshots.
    # Wrap each standard MessageBody as StrangerConversation.last_message.
    rows = []
    for message in messages:
        fields = _fields(message)
        if _integer(fields, 5) != int(conversation_short_id):
            raise StrangerError("陌生人会话消息归属不匹配")
        rows.append(_field(4, _field(3, message)))
    envelope = (_field(1, 1001) + _field(4, b"OK")
                + _field(6, _field(1000, b"".join(rows))))
    events = parse_response(envelope, own_uid, include_self=include_self)["events"]
    return sorted(events, key=lambda e: (e["created_at_ms"], int(e["message_id"])))


def fetch_messages(auth, conversation_short_id, include_self=False):
    payload = build_messages_request(auth, conversation_short_id)
    return parse_messages_response(_post(auth, "https://imapi.douyin.com/v1/stranger/get_messages", payload),
                                   auth.get_uid(), conversation_short_id, include_self=include_self)
