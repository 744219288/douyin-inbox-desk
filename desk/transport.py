"""Optional live adapter for the pinned Douyin_Spider source; imported lazily."""
import json
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "Douyin_Spider"


def harden_http(client):
    """Enforce TLS and bounded timeouts without editing the vendor snapshot."""
    if getattr(client, "_desk_hardened", False):
        return
    module_request = client.request
    session_request = client.Session.request

    def options(kwargs):
        kwargs["verify"] = True
        value = kwargs.get("timeout", 20)
        kwargs["timeout"] = min(float(value), 20) if isinstance(value, (float, int)) and value > 0 else 20
        return kwargs

    def secure_request(method, url, **kwargs):
        return module_request(method, url, **options(kwargs))

    def secure_session(self, method, url, **kwargs):
        return session_request(self, method, url, **options(kwargs))

    client.request = secure_request
    client.Session.request = secure_session
    client._desk_hardened = True


def load_vendor():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    try:
        from loguru import logger
        logger.disable("dy_apis")
        logger.disable("builder")
        logger.disable("utils")
        from utils import http_client
        harden_http(http_client)
        from builder.auth import DouyinAuth
        from dy_apis.douyin_api import DouyinAPI
        from dy_apis.douyin_recv_msg import DouyinRecvMsg
        from static import Live_pb2, Response_pb2
        return DouyinAuth, DouyinAPI, DouyinRecvMsg, Live_pb2, Response_pb2
    except ImportError:
        raise RuntimeError("缺少抖音运行依赖，请先运行「安装真实连接依赖.cmd」") from None


def read_session(data_dir):
    path = Path(data_dir) / "douyin-session.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def parse_incoming(raw, own_uid, Live_pb2, Response_pb2):
    frame = Live_pb2.PushFrame()
    frame.ParseFromString(raw)
    if frame.payloadType != "pb":
        return None
    response = Response_pb2.Response()
    response.ParseFromString(frame.payload)
    stranger = False
    if response.body.WhichOneof("body") == "new_message_notify":
        msg = response.body.new_message_notify.message
    else:
        # The pinned upstream schema omits ResponseBody.503. Its nested
        # StrangerNewMessageNotify stores the standard MessageBody at field2.
        from .stranger import _fields, _one
        body = _fields(response.body.SerializeToString())
        if 503 not in body:
            return None
        notify = _fields(_one(body, 503))
        msg = Response_pb2.MessageBody()
        msg.ParseFromString(_one(notify, 2, b""))
        stranger = True
    if msg.conversation_type != 1 or not msg.sender:
        return None
    if msg.message_type in (1, 50001) or not msg.conversation_id:
        return None
    identity = str(msg.server_message_id or msg.index_in_conversation)
    if identity == "0":
        return None  # No stable identity: do not risk duplicate automatic replies.
    from .media import decode_content
    decoded = decode_content(msg.message_type, msg.content)
    text = decoded['text']
    if not isinstance(text, str) or not text.strip():
        return None
    event = {"conversation_id": msg.conversation_id, "sender_id": str(msg.sender),
             **decoded, "message_id": identity}
    event['conversation_short_id'] = str(msg.conversation_short_id)
    from .stranger import _fields, _text, message_identity
    fields = _fields(msg.SerializeToString())
    event.update(message_identity(fields, own_uid))
    sec_uid = '' if event['is_outgoing'] else _text(fields, 14)
    if sec_uid:
        event['sec_uid'] = sec_uid
    from .stranger import _integer
    event['created_at_ms'] = _integer(fields, 10)
    if stranger:
        event["name"] = "陌生人私信"
    return event


class DouyinTransport:
    supports_client_message_id = True
    requires_history_before_resume = True

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        self._epoch = 0
        self._listener = None
        self._auth = None
        self._api = None
        self._connected = False
        self._routes = {}
        self._uid = None
        self._stranger_poller = None
        self._media_resolver = None
        self._inbox_status = {"status": "disconnected", "detail": "陌生人监听未启动"}
        self._stranger_received = 0
        self._recovered = set()
        from .follow_back import FollowBackWorker
        self._follow_worker = FollowBackWorker(self.data_dir, self._follow_backend)

    def _follow_backend(self):
        from .follow_back import DouyinFollowAPI
        with self._lock:
            if not self._connected or not self._auth or not self._uid:
                return None
            epoch = self._epoch
            return DouyinFollowAPI(self._auth, self._api, self._uid, (self._uid, epoch),
                lambda: self._follow_active(epoch))

    def _follow_active(self, epoch):
        with self._lock:
            return self._connected and epoch == self._epoch

    def set_follow_back(self, enabled):
        self._follow_worker.configure(enabled)

    def follow_back_state(self):
        return self._follow_worker.state()

    def inbox_state(self):
        with self._lock:
            return {**self._inbox_status, "realtime_connected": self._connected,
                    "realtime_received": self._stranger_received,
                    "refresh": self._stranger_poller.refresh_state() if self._stranger_poller else {'status':'idle'}}

    def refresh_strangers(self):
        with self._lock:
            if not self._connected or not self._stranger_poller:
                raise ValueError('请先连接抖音账号')
            return self._stranger_poller.request_refresh()

    def connect(self, settings, on_message, on_status):
        self.disconnect()
        self.set_follow_back(settings.get('auto_follow_enabled', False))
        with self._lock:
            epoch = self._epoch
        def status(value, detail):
            with self._lock:
                if epoch != self._epoch:
                    return
                self._connected = value == "connected"
            on_status(value, detail)

        def run():
            try:
                Auth, api, Receiver, Live, Response = load_vendor()
                session = read_session(self.data_dir)
                override = settings.get("douyin_cookie", "").strip()
                if override and override != session.get("cookie"):
                    raise RuntimeError("私信发送需要配套签名凭证：设置中的 Cookie 覆盖与扫码凭证不一致，请勾选「清除 Cookie 覆盖」后保存，无需重新扫码。")
                cookie = override or session.get("cookie", "")
                if not cookie or not session.get("ticket") or not session.get("private_key"):
                    raise RuntimeError("请先运行「扫码登录.cmd」保存配套登录凭证")
                auth = Auth.from_cookie(cookie, ticket=session.get("ticket"), ts_sign=session.get("ts_sign"),
                                        client_cert=session.get("client_cert"), private_key=session.get("private_key"),
                                        dtrait_blob=session.get("dtrait_blob"), bootstrap_creator=False)
                uid = str(auth.get_uid())
                if not uid or uid in {"None", "0"}:
                    raise RuntimeError("无法验证登录账号，请重新扫码登录")
                from .account_guard import bind_account
                with self._lock:
                    if epoch != self._epoch:
                        return
                    bind_account(self.data_dir, uid)
                owner = self
                class Listener(Receiver):
                    def on_open(self, ws):
                        self._opened = True
                        status("connected", "抖音私信已连接；等待新消息")

                    def on_message(self, ws, message):
                        try:
                            event = parse_incoming(message, uid, Live, Response)
                            if event is None:
                                return
                            with owner._lock:
                                if epoch != owner._epoch:
                                    return
                                if event.get('name') == '陌生人私信':
                                    owner._stranger_received += 1
                                owner._record_route(event)
                                owner._save_routes()
                                checked = event['conversation_id'] in owner._recovered
                                if (not checked or (event.get('media_kind') in ('image','sticker') and not event.get('media_url'))) and event.get('conversation_short_id') not in ('', '0', None):
                                    recovery.prioritize(event)
                                    poller.wake.set()
                            on_message(**{k:v for k,v in event.items() if k != 'conversation_short_id'}, _schedule_reply=checked)
                            media_resolver.enqueue(event)
                        except Exception:
                            # A malformed frame must not be echoed or expose auth URLs.
                            return

                    def on_error(self, ws, error):
                        self._should_reconnect = not self._stopped
                        status("error", "私信连接异常，正在重连；若持续失败请重新登录")

                    def on_close(self, ws, code, message):
                        self._should_reconnect = not self._stopped
                        status("disconnected", "私信连接已关闭，等待重连")

                listener = Listener(auth)
                # Separate HTTP session: inbox reads must not race send headers.
                inbox_auth = Auth.from_cookie(cookie, ticket=session.get("ticket"),
                    ts_sign=session.get("ts_sign"), client_cert=session.get("client_cert"),
                    private_key=session.get("private_key"), bootstrap_creator=False)
                inbox_auth.uid = uid
                from .live_media import LiveMediaResolver
                media_auth = Auth.from_cookie(cookie, ticket=session.get('ticket'),
                    ts_sign=session.get('ts_sign'), client_cert=session.get('client_cert'),
                    private_key=session.get('private_key'), bootstrap_creator=False)
                media_auth.uid = uid

                def enrich_live_media(event):
                    with owner._lock:
                        if epoch != owner._epoch or not owner._connected:
                            return False
                    keys = ('conversation_id', 'sender_id', 'text', 'message_id',
                            'created_at_ms', 'media_kind', 'media_url', 'media_secret', 'media_md5')
                    return on_message(**{key: event[key] for key in keys if key in event},
                                      _schedule_reply=False)

                media_resolver = LiveMediaResolver(media_auth, enrich_live_media)
                from .recovery import RecoveryInbox, recovery_start
                from .stranger_polling import StrangerPoller

                def inbox_report(value, detail):
                    with owner._lock:
                        if epoch == owner._epoch:
                            owner._inbox_status = {"status": value, "detail": detail}

                def inbox_deliver_batch(events):
                    with owner._lock:
                        if epoch != owner._epoch or not owner._connected:
                            return False
                        for event in events:
                            owner._record_route(event)
                        owner._save_routes()
                    accepted = on_message(recovery=True, batch=[{key:event[key] for key in
                               ('conversation_id','sender_id','text','message_id')} |
                               {'name':event.get('name', '抖音联系人'), 'sec_uid':event.get('sec_uid', '') if not event.get('is_outgoing') else '',
                                  'is_outgoing':event.get('is_outgoing', False),
                                  'client_message_id':event.get('client_message_id', ''),
                                  'peer_id':event.get('peer_id', ''),
                                  'created_at_ms':event.get('created_at_ms',0),
                                  'history_only':event.get('history_only',False),
                                  'media_kind':event.get('media_kind',''),
                                  'media_url':event.get('media_url',''),
                                  'media_secret':event.get('media_secret',''),
                                  'media_md5':event.get('media_md5','')} for event in events])
                    if accepted is not False:
                        with owner._lock:
                            if epoch == owner._epoch:
                                owner._recovered.update(event['conversation_id'] for event in events)
                    return accepted

                recovery = RecoveryInbox(inbox_auth, incremental=True)
                poller = StrangerPoller(recovery.fetch, None, inbox_report,
                    manual_fetch=recovery.fetch_all,
                    history_fetch=recovery.history,
                    deliver_batch=inbox_deliver_batch, recover_on_start=True,
                    recovery_since=recovery_start(self.data_dir,uid))
                poller.max_histories = 2
                poller.should_yield = recovery.has_priority
                # List pages and history reads share the same request budget.
                # A new conversation must not immediately trigger a burst.
                inbox_auth._desk_stranger_interval = poller.interval
                inbox_auth._desk_stranger_stop = poller.stop_event
                with owner._lock:
                    if epoch != owner._epoch:
                        listener.stop()
                        return
                    self._auth, self._api, self._uid = auth, api, uid
                    self._listener = listener
                    self._stranger_poller = poller
                    self._media_resolver = media_resolver
                    route_path = self.data_dir / "routes.json"
                    if route_path.exists():
                        saved = json.loads(route_path.read_text(encoding="utf-8"))
                        self._routes = saved.get("routes", {}) if saved.get("uid") == uid else {}
                poller.start()
                media_resolver.start()
                try:
                    listener.start()
                finally:
                    poller.stop()
                    media_resolver.stop()
                status("disconnected", "私信监听已停止")
            except RuntimeError as exc:
                # Only our bounded local diagnostics are safe to surface.
                detail = str(exc) if str(exc).startswith(("缺少抖音", "请先运行", "私信发送需要", "无法验证", "账号不匹配", "账号归属")) else "抖音连接失败，请重新扫码登录或检查依赖"
                status("error", detail)
            except Exception:
                status("error", "抖音连接失败，请重新扫码登录或检查依赖")
        on_status("connecting", "正在验证登录并连接抖音私信")
        threading.Thread(target=run, name="douyin-listener", daemon=True).start()

    def _record_route(self, event):
        peer = event.get('peer_id') if event.get('is_outgoing') else event['sender_id']
        if peer and str(peer) != str(self._uid):
            self._routes[event['conversation_id']] = str(peer)

    def _save_routes(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "routes.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"uid": self._uid, "routes": self._routes}, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def disconnect(self):
        self.set_follow_back(False)
        with self._lock:
            self._epoch += 1
            self._connected = False
            listener, self._listener = self._listener, None
            poller, self._stranger_poller = self._stranger_poller, None
            media_resolver, self._media_resolver = self._media_resolver, None
            self._inbox_status = {"status": "disconnected", "detail": "陌生人监听未启动"}
            self._auth = None
            self._api = None
            self._uid = None
            self._routes = {}
            self._stranger_received = 0
            self._recovered = set()
        if poller:
            poller.stop()
        if media_resolver:
            media_resolver.stop()
        if listener:
            listener.stop()

    def contact_uid(self, conversation_id):
        with self._lock:
            uid = self._routes.get(conversation_id)
            if uid:
                return uid
            parts = conversation_id.split(':')
            if len(parts) == 4 and parts[:2] == ['0','1'] and self._uid in parts[2:]:
                return next((p for p in parts[2:] if p != self._uid and p.isdigit()), '')
            return ''

    def prepare(self, conversation_id):
        with self._lock:
            if not self._connected or self._auth is None:
                raise RuntimeError("抖音未连接")
            uid = self._routes.get(conversation_id)
            if not uid:
                raise RuntimeError("未找到该会话的接收人，请等待对方发送新消息")
            auth, api, epoch = self._auth, self._api, self._epoch
        try:
            cid, short_id, ticket = api.create_conversation(auth, int(uid))
            if cid != conversation_id:
                raise RuntimeError("会话校验不一致，已停止发送")
            api.get_identity_security_token(auth)
            return epoch, auth, api, cid, short_id, ticket
        except Exception:
            raise RuntimeError("无法准备当前私信会话，请重新登录并核对会话") from None

    def send_prepared(self, prepared, text, client_message_id=None):
        epoch, auth, api, cid, short_id, ticket = prepared
        with self._lock:
            if epoch != self._epoch or not self._connected or auth is not self._auth:
                raise RuntimeError("连接已变化，取消发送")
            try:
                ok = api.send_msg(auth, cid, short_id, ticket, text, client_message_id=client_message_id or str(uuid.uuid4()))
                if not ok:
                    raise RuntimeError("抖音未确认发送成功")
            except Exception:
                raise RuntimeError("抖音未确认发送成功，请到抖音核对，避免重复发送") from None

    def send(self, conversation_id, text, client_message_id=None):
        self.send_prepared(self.prepare(conversation_id), text, client_message_id=client_message_id)

    def send_asset(self, conversation_id, path, kind, client_message_id=None):
        """Upload and send only on an explicitly selected, verified conversation."""
        epoch, auth, api, cid, short_id, ticket = self.prepare(conversation_id)
        with self._lock:
            if epoch != self._epoch or not self._connected or auth is not self._auth:
                raise RuntimeError('连接已变化，取消附件发送')
            try:
                if kind not in {'image', 'file'}:
                    raise RuntimeError('不支持的附件类型')
                sender = api.send_image if kind == 'image' else api.send_file
                ok = sender(auth, cid, short_id, ticket, str(path), client_message_id=client_message_id or str(uuid.uuid4()))
                if not ok:
                    raise RuntimeError('抖音未确认附件发送成功')
            except Exception:
                raise RuntimeError('抖音未确认附件发送成功，请核对会话，避免重复发送') from None
