"""Local, persistent inbox and cancellable reply orchestration."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4
from .integrations import IntegrationError, IntegrationProtocolError, HandoffRequired
from .workspace import WorkspaceStore, STAGES, bounded_text
from .assets import AssetStore


DEFAULTS = {
    "mode": "demo", "deepseek_model": "deepseek-flash", "deepseek_api_key": "",
    "persona": "用账号主人的第一人称口吻，简短、自然、友好地回复。只根据已知资料回答业务事实，不编造价格、库存、承诺。需要人工时说明会转交本人处理。",
    "handoff_keywords": ["人工", "真人", "转人工"],
    "knowledge_enabled": False, "knowledge_url": "", "knowledge_api_key": "",
    "agent_enabled": False, "agent_url": "", "agent_api_key": "", "douyin_cookie": "",
    "auto_follow_enabled": False,
}
SECRETS = {"deepseek_api_key": "deepseek_key_set", "knowledge_api_key": "knowledge_key_set",
           "agent_api_key": "agent_key_set", "douyin_cookie": "douyin_cookie_set"}


def now():
    return datetime.now(timezone.utc).isoformat()


class DeskService:
    def __init__(self, data_dir: Path, responder, transport):
        self.data_dir = Path(data_dir)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.lifecycle = threading.RLock()
        self.db = sqlite3.connect(str(Path(data_dir) / "desk.sqlite3"), check_same_thread=False,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, raw_id TEXT NOT NULL, name TEXT NOT NULL,
                preview TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                unread INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ai',
                generation INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
            CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, created_at);
            CREATE TABLE IF NOT EXISTS message_media (
                message_id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbound (
                mode TEXT NOT NULL, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
                PRIMARY KEY(mode, conversation_id, message_id));
            CREATE TABLE IF NOT EXISTS message_origins (
                message_id TEXT PRIMARY KEY, origin TEXT NOT NULL,
                platform_message_id TEXT NOT NULL DEFAULT '', client_message_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS outbound_ids (
                client_message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS service_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbound_refs (
                mode TEXT NOT NULL, raw_id TEXT NOT NULL, platform_id TEXT NOT NULL,
                local_id TEXT NOT NULL, history_only INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(mode,raw_id,platform_id));
            CREATE TABLE IF NOT EXISTS incoming_sources (
                message_id TEXT PRIMARY KEY, secret TEXT NOT NULL, md5 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reply_sources (
                message_id TEXT PRIMARY KEY, inbound_message_id TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO service_metadata VALUES('owner_sync_started',?)", (str(time.time()),))
        self.owner_sync_started = float(self.db.execute("SELECT value FROM service_metadata WHERE key='owner_sync_started'").fetchone()[0])
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(conversations)')}
        for name, declaration in {'sender_id': "TEXT NOT NULL DEFAULT ''", 'sec_uid': "TEXT NOT NULL DEFAULT ''",
                                  'avatar_url': "TEXT NOT NULL DEFAULT ''", 'profile_checked': 'REAL NOT NULL DEFAULT 0',
                                  'resume_after': 'REAL NOT NULL DEFAULT 0'}.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE conversations ADD COLUMN {name} {declaration}')
        self.workspace = WorkspaceStore(self.db)
        self._remove_legacy_owner_copies()
        self.assets = AssetStore(self.data_dir)
        self.profile_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='contact-profile')
        self.profile_pending = set()
        self.profile_attempts = {}
        row = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        self.settings = dict(DEFAULTS, **(json.loads(row[0]) if row else {}))
        self.responder, self.transport = responder, transport
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="desk-reply")
        self.slots = threading.BoundedSemaphore(64)
        self.events = []
        self.connection = {"status": "disconnected", "detail": "尚未连接"}
        self.epoch = 0
        self.closed = False
        self.transport_started = False
        self.send_locks = {}
        self.active_sends = 0
        self.db_closed = False
        # A process can exit after submitting a send but before saving its result.
        # Never replay that row; surface it for manual reconciliation instead.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE conversations SET status='waiting' WHERE status<>'human' AND id IN (SELECT conversation_id FROM messages WHERE status='sending')")
            interrupted = self.db.execute(
                "UPDATE messages SET status='failed',error=? WHERE status='sending'",
                ("上次运行在发送过程中中断，结果不确定；请先到抖音核对，未自动重试",)).rowcount
            self.db.execute("UPDATE messages SET status='cancelled' WHERE status='pending'")
            self.db.execute("UPDATE conversations SET generation=generation+1")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        if interrupted:
            self._event('error', f'发现 {interrupted} 条上次运行中断的发送，请到抖音核对结果')

    def _remove_legacy_owner_copies(self):
        """Quarantine records introduced by the previous legacy echo importer."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS suppressed_owner_history (message_id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            rows = self.db.execute("SELECT m.*,o.platform_message_id,o.client_message_id,COALESCE(a.kind,'') AS media_kind,COALESCE(a.url,'') AS media_url FROM messages m JOIN message_origins o ON o.message_id=m.id LEFT JOIN message_media a ON a.message_id=m.id WHERE o.origin='unknown'").fetchall()
            for row in rows:
                self.db.execute('INSERT OR IGNORE INTO suppressed_owner_history VALUES(?,?)', (row['id'], json.dumps(dict(row), ensure_ascii=False)))
                self.db.execute('DELETE FROM message_media WHERE message_id=?', (row['id'],))
                self.db.execute('DELETE FROM message_origins WHERE message_id=?', (row['id'],))
                self.db.execute('DELETE FROM messages WHERE id=?', (row['id'],))
            for cid in {r['conversation_id'] for r in rows}:
                last = self.db.execute("SELECT text,created_at FROM messages WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (cid,)).fetchone()
                if last:
                    self.db.execute('UPDATE conversations SET preview=?,updated_at=? WHERE id=?', (last['text'][:160], last['created_at'], cid))
                else:
                    self.db.execute("UPDATE conversations SET preview='' WHERE id=?", (cid,))
                self.workspace.refresh_memory(cid)
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def _event(self, level, message):
        self.events.append({"time": now(), "level": level, "message": message})
        self.events = self.events[-100:]

    def _public(self):
        return {**{k: v for k, v in self.settings.items() if k not in SECRETS},
                **{flag: bool(self.settings[key]) for key, flag in SECRETS.items()}}

    def _get(self, cid):
        if self.closed:
            raise ValueError("服务已关闭")
        row = self.db.execute("SELECT * FROM conversations WHERE id=? AND mode=?",
                              (cid, self.settings["mode"])).fetchone()
        if not row:
            raise ValueError("会话不存在或不属于当前模式")
        return dict(row)

    @staticmethod
    def _view(row):
        return {k: v for k, v in dict(row).items() if k not in ("mode", "raw_id", "sender_id", "sec_uid", "profile_checked", "resume_after")}

    def _queue_profile(self, cid):
        # Called with service lock held; network work never holds this lock.
        row = self._get(cid)
        if (not row['sender_id'] or cid in self.profile_pending or len(self.profile_pending) >= 64
                or time.time() - row['profile_checked'] < 86400 or time.time() - self.profile_attempts.get(cid, 0) < 300):
            return
        self.profile_pending.add(cid)
        self.profile_attempts[cid] = time.time()
        def run():
            try:
                from .profiles import fetch_profile
                profile = fetch_profile(self.data_dir, row['sender_id'], row['sec_uid'])
                with self.lock:
                    if not self.closed:
                        self.db.execute("UPDATE conversations SET name=?,avatar_url=CASE WHEN ?='' THEN avatar_url ELSE ? END,profile_checked=? WHERE id=? AND sender_id=? AND sec_uid=?",
                            (profile['name'],profile['avatar_url'],profile['avatar_url'],time.time(),cid,row['sender_id'],row['sec_uid']))
            except Exception:
                pass  # Keep previous metadata; delivery is independent.
            finally:
                with self.lock:
                    self.profile_pending.discard(cid)
        self.profile_pool.submit(run)

    def state(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM conversations WHERE mode=? ORDER BY updated_at DESC",
                                   (self.settings["mode"],)).fetchall()
            convs = []
            for row in rows:
                customer = self.workspace.customer(row['id'])
                convs.append(dict(self._view(row), stage=customer['stage'], tags=customer['tags'],
                                  follow_up_at=customer['follow_up']['at'], follow_up_done=customer['follow_up']['done']))
            inbox_state = getattr(self.transport, "inbox_state", None)
            follow_state = getattr(self.transport, "follow_back_state", None)
            return {"mode": self.settings["mode"], "connection": dict(self.connection),
                    "stranger": inbox_state() if callable(inbox_state) else None,
                    "follow_back": follow_state() if callable(follow_state) else None,
                    "conversations": convs, "stats": {"total": len(convs), **{
                        s: sum(c["status"] == s for c in convs) for s in ("ai", "waiting", "human")}},
                    "settings": self._public(), "events": list(self.events)}

    def conversation(self, cid):
        with self.lock:
            self._get(cid)
            self.db.execute("UPDATE conversations SET unread=0 WHERE id=?", (cid,))
            messages = [dict(r) for r in self.db.execute(
                "SELECT m.id,m.role,m.text,m.created_at,m.status,m.error,COALESCE(a.kind,'') AS media_kind,COALESCE(a.url,'') AS media_url,COALESCE(o.origin,CASE WHEN m.role='user' THEN 'customer' ELSE 'desk' END) AS origin FROM messages m LEFT JOIN message_media a ON a.message_id=m.id LEFT JOIN message_origins o ON o.message_id=m.id WHERE conversation_id=? ORDER BY created_at,m.rowid", (cid,))]
            for message in messages:
                if message['media_url'] and self.db.execute('SELECT 1 FROM incoming_sources WHERE message_id=?',(message['id'],)).fetchone():
                    message['media_url'] = '/api/incoming-media/' + message['id']
            return {"conversation": self._view(self._get(cid)), "messages": messages,
                    "workspace": self.workspace.customer(cid)}

    def incoming_image(self, mid):
        import hashlib
        import re
        from .incoming_media import fetch_image, image_mime
        if not re.fullmatch(r'[a-f0-9]{32}', mid):
            raise ValueError('图片不存在')
        with self.lock:
            row = self.db.execute('SELECT a.url,s.secret,s.md5,c.mode FROM message_media a JOIN incoming_sources s ON s.message_id=a.message_id JOIN messages m ON m.id=a.message_id JOIN conversations c ON c.id=m.conversation_id WHERE a.message_id=?',(mid,)).fetchone()
            if not row or row['mode'] != self.settings['mode']:
                raise ValueError('图片不属于当前模式')
            source = dict(row)
        fingerprint = hashlib.sha256((source['secret']+source['md5']+source['url']).encode()).hexdigest()
        folder = self.data_dir / 'incoming-cache'
        folder.mkdir(exist_ok=True)
        path = folder / (mid + '-' + fingerprint + '.bin')
        if path.is_file():
            data = path.read_bytes()
            mime = image_mime(data)
        else:
            data, mime = fetch_image(source['url'],source['secret'],source['md5'])
            tmp = folder / (uuid4().hex + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(path)
        with self.lock:
            if self.closed or source['mode'] != self.settings['mode']:
                raise ValueError('模式已切换')
        return data, mime

    def workspace_state(self):
        with self.lock:
            if self.closed:
                raise ValueError('服务已关闭')
            mode = self.settings['mode']
            conversations = [dict(r) for r in self.db.execute('SELECT id,status FROM conversations WHERE mode=?', (mode,))]
            return {'mode': mode, **{name: self.workspace.items(mode, name) for name in ('knowledge', 'replies', 'materials')},
                    'overview': self.workspace.overview(mode, conversations), 'stages': STAGES}

    def workspace_action(self, resource, body):
        with self.lock:
            if self.closed:
                raise ValueError('服务已关闭')
            if isinstance(body, dict) and 'expected_mode' in body and body['expected_mode'] != self.settings['mode']:
                raise ValueError('工作区模式已变化，操作未执行，请刷新后重试')
            result = self.workspace.action(self.settings['mode'], resource, body, self.assets.get)
            if resource == 'knowledge' and result is None:
                self._restart_pending_replies()
            return result if result is not None else self.workspace_state()

    def customer_update(self, cid, body):
        with self.lock:
            self._get(cid)
            before = self._customer_context(self.workspace.customer(cid))
            after = self.workspace.update_customer(cid, body)
            if self._customer_context(after) != before:
                self._restart_pending_replies(cid)
            return self.conversation(cid)

    def handoff(self, cid, reason='人工交接'):
        reason = bounded_text(reason, '交接原因', 1000, True)
        with self._send_lock(cid), self.lock:
            self._get(cid)
            self._cancel(cid, 'waiting')
            self.workspace.record_handoff(cid, reason)
            return self.conversation(cid)

    @staticmethod
    def _customer_context(customer):
        # Notes and operational follow-ups stay local; only curated business
        # profile plus explicitly labelled summary are model reference data.
        context = {key: customer[key] for key in ('profile', 'tags', 'stage', 'summary', 'summary_source', 'summary_stale', 'recent_messages')}
        if context['summary_stale'] or (context['summary_source'] in {'model', 'agent'} and not customer.get('evidence')):
            context['summary'] = ''  # Old or unverified analysis is not reply evidence.
        return context

    def analyze(self, cid, body=None):
        from .integrations import analyze_customer
        body = {} if body is None else body
        if not isinstance(body, dict):
            raise ValueError('分析请求必须是对象')
        with self.lock:
            self._get(cid)
            customer = self.workspace.customer(cid)
            settings = dict(self.settings)
            rows = [dict(r) for r in self.db.execute(
                "SELECT id,role,text FROM messages WHERE conversation_id=? AND id NOT IN (SELECT message_id FROM message_media) AND (role='user' OR (role IN ('assistant','human') AND status='sent')) ORDER BY created_at DESC,rowid DESC LIMIT 40",
                (cid,))][::-1]
            source_ids = [r['id'] for r in rows]
        # No service lock is held during an explicit external analysis.
        result = analyze_customer(settings, rows, self._customer_context(customer), body.get('source', 'local'))
        with self.lock:
            self._get(cid)
            latest = [r['id'] for r in self.db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND id NOT IN (SELECT message_id FROM message_media) AND (role='user' OR (role IN ('assistant','human') AND status='sent')) ORDER BY created_at DESC,rowid DESC LIMIT 40",
                (cid,))][::-1]
            current = self.workspace.customer(cid)
            if settings['mode'] != self.settings['mode'] or latest != source_ids or current['updated_at'] != customer['updated_at']:
                raise ValueError('分析期间会话或资料已更新，结果未覆盖，请重新分析')
            latest_customer_id = next((r['id'] for r in reversed(rows) if r['role'] == 'user'), '')
            self.workspace.save_customer(cid, dict(current, **result, summary_stale=False,
                summary_updated_at=now(), summary_through_message_id=latest_customer_id))
            self._restart_pending_replies(cid)
            return self.conversation(cid)

    def test_integration(self, kind, query):
        from .integrations import test_service
        query = bounded_text(query, '测试问题', 2000, True)
        with self.lock:
            if self.closed:
                raise ValueError('服务已关闭')
            settings = dict(self.settings)
            documents = self.workspace.search(settings['mode'], query)
        return test_service(settings, kind, query, documents)

    def _cancel(self, cid, status=None):
        self.db.execute("UPDATE conversations SET generation=generation+1 WHERE id=?", (cid,))
        if status:
            self.db.execute("UPDATE conversations SET status=? WHERE id=?", (status, cid))
        self.db.execute("UPDATE messages SET status='cancelled' WHERE conversation_id=? AND status='pending'", (cid,))

    def _restart_pending_replies(self, cid=None):
        """Caller holds self.lock through context mutation and task replacement.

        Sending rows have already crossed the atomic delivery reservation and
        must never be cancelled/replayed here. Failed and manual rows likewise
        cannot create new replies merely because reference data was edited.
        """
        query = ("SELECT DISTINCT c.id FROM conversations c JOIN messages m ON m.conversation_id=c.id "
                 "WHERE c.mode=? AND c.status='ai' AND m.role='assistant' AND m.status='pending'")
        params = [self.settings['mode']]
        if cid is not None:
            query += ' AND c.id=?'
            params.append(cid)
        for row in self.db.execute(query, params).fetchall():
            self._cancel(row['id'])
            self._recover_latest(row['id'], check_handoff=True)

    def takeover(self, cid):
        with self._send_lock(cid), self.lock:
            self._get(cid)
            self._cancel(cid, "human")
            self.workspace.accept_handoff(cid)
            return self.conversation(cid)

    def resume(self, cid):
        with self._send_lock(cid), self.lock:
            self._get(cid)
            self._cancel(cid, "ai")
            self.db.execute('UPDATE conversations SET resume_after=? WHERE id=?',(time.time(),cid))
            self.workspace.resolve_handoff(cid)
            return self.conversation(cid)

    def _message(self, cid, role, text, status, created_at=None):
        mid = uuid4().hex
        self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,NULL)", (mid, cid, role, text, created_at or now(), status))
        if status != "pending":
            latest = self.db.execute("SELECT text,created_at FROM messages WHERE conversation_id=? AND status<>'pending' AND text<>'' ORDER BY julianday(created_at) DESC,rowid DESC LIMIT 1", (cid,)).fetchone()
            if latest:
                self.db.execute("UPDATE conversations SET preview=?,updated_at=? WHERE id=?", (latest['text'][:160], latest['created_at'], cid))
        return mid

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError("消息不能为空且不能超过 10000 字")
        return text.strip()

    def send(self, cid, text):
        text = self._text(text)
        with self._send_lock(cid):
            with self.lock:
                row = self._get(cid)
                self._cancel(cid, "human")
                self.workspace.accept_handoff(cid)
                mid = self._message(cid, "human", text, "pending")
                if row["mode"] == "live" and self.connection["status"] != "connected":
                    self._failure(cid, mid, "连接已断开，消息未发送")
                    raise ValueError("连接已断开，消息未发送")
                self.db.execute("UPDATE messages SET status='sending' WHERE id=?", (mid,))
                self._register_outbound(cid, mid)
                self.active_sends += 1
            success = self._deliver(cid, row["raw_id"], row["mode"], mid, text)
            if not success:
                raise ValueError("发送失败，请核对抖音会话后再决定是否重发") from None
            with self.lock:
                if self.closed or self.settings["mode"] != row["mode"]:
                    return {"id": mid, "status": "sent"}
                return self.conversation(cid)

    def _send_lock(self, cid):
        # Never wait for a conversation lock while holding the global state lock.
        with self.lock:
            return self.send_locks.setdefault(cid, threading.Lock())

    def _register_outbound(self, cid, mid):
        """Caller holds self.lock; persist correlation BEFORE calling transport."""
        self.db.execute('INSERT OR IGNORE INTO outbound_ids VALUES(?,?,?)', (mid, cid, mid))
        self.db.execute("INSERT OR IGNORE INTO message_origins(message_id,origin,client_message_id) VALUES(?,'desk',?)", (mid, mid))

    def _deliver(self, cid, raw, mode, mid, text, prepared=None, use_prepared=False):
        """Caller holds its conversation lock and atomically reserved a sending row."""
        success = False
        try:
            if mode == "live":
                options = {'client_message_id': mid} if getattr(self.transport, 'supports_client_message_id', False) else {}
                if use_prepared:
                    self.transport.send_prepared(prepared, text, **options)
                else:
                    self.transport.send(raw, text, **options)
            success = True
        except Exception:
            pass  # External exception details may contain credentials.
        with self.lock:
            try:
                if success:
                    # In-flight sends may finish after disconnect/mode change/close.
                    # Preserve their actual result instead of reporting cancellation.
                    self.db.execute("UPDATE messages SET status='sent',error=NULL WHERE id=?", (mid,))
                    self.db.execute("UPDATE conversations SET preview=?,updated_at=? WHERE id=?", (text[:160], now(), cid))
                else:
                    self._failure(cid, mid, "发送失败，结果可能不确定；请核对抖音会话，未自动重试")
            finally:
                self.active_sends -= 1
                self._close_db_if_idle()
        return success

    def _close_db_if_idle(self):
        if self.closed and not self.active_sends and not self.db_closed:
            self.db.close()
            self.db_closed = True

    def _failure(self, cid, mid, detail):
        self.db.execute("UPDATE messages SET status='failed',error=? WHERE id=?", (detail, mid))
        row = self.db.execute('SELECT status FROM conversations WHERE id=?', (cid,)).fetchone()
        # An owner reply may arrive while a transport request is in flight.
        # Its accepted takeover remains authoritative even if that request fails.
        external_takeover = self.db.execute(
            "SELECT 1 FROM messages m JOIN message_origins o ON o.message_id=m.id "
            "WHERE m.conversation_id=? AND o.origin='douyin' AND m.role='human' "
            "AND m.rowid>(SELECT rowid FROM messages WHERE id=?) LIMIT 1", (cid, mid)).fetchone()
        human = row and row['status'] == 'human' and external_takeover
        self._cancel(cid, 'human' if human else 'waiting')
        if not human:
            self.workspace.record_handoff(cid, detail)
        self._event("error", detail)

    def update_settings(self, values):
        with self.lifecycle:
            return self._update_settings(values)

    def _update_settings(self, values):
        with self.lifecycle, self.lock:
            candidate = dict(self.settings)
            for key, value in values.items():
                if key not in DEFAULTS:
                    continue
                if key in SECRETS and value == "":
                    continue
                if key.endswith("_enabled"):
                    if not isinstance(value, bool):
                        raise ValueError("启用开关必须是布尔值")
                elif key == "handoff_keywords":
                    if (not isinstance(value, list) or len(value) > 32 or
                            any(not isinstance(x, str) or not x.strip() or len(x) > 80 for x in value)):
                        raise ValueError("人工关键词最多 32 项，每项为 1 至 80 字的字符串")
                    value = [x.strip() for x in value]
                elif not isinstance(value, str):
                    raise ValueError("设置字段必须为字符串")
                candidate[key] = value
            if values.get("clear_deepseek_key"):
                candidate["deepseek_api_key"] = ""
            if values.get("clear_douyin_cookie"):
                candidate["douyin_cookie"] = ""
            candidate["deepseek_model"] = candidate["deepseek_model"].strip()
            if not candidate["deepseek_model"] or len(candidate["deepseek_model"]) > 128:
                raise ValueError("模型名必须为 1 至 128 字")
            if len(candidate["persona"]) > 6000:
                raise ValueError("账号口吻设置不能超过 6000 字")
            if candidate["mode"] not in ("demo", "live"):
                raise ValueError("无效运行模式")
            for name in ("knowledge", "agent"):
                url = candidate[name + "_url"]
                if candidate[name + "_enabled"] and not url:
                    raise ValueError("启用扩展前请填写接口 URL")
                if url:
                    if len(url) > 2048:
                        raise ValueError("接口 URL 不能超过 2048 字")
                    try:
                        target = urlparse(url)
                        if (target.scheme not in ("http", "https") or not target.hostname or
                                target.username is not None or target.password is not None or target.fragment):
                            raise ValueError()
                        target.port  # Validate malformed/out-of-range ports before saving.
                    except ValueError:
                        raise ValueError("接口地址必须是完整 HTTP(S) 地址，不能包含账号密码或片段") from None
                    if target.scheme == "http" and target.hostname not in {"localhost", "127.0.0.1", "::1"}:
                        raise ValueError("远程接口必须使用 HTTPS")
            changed_mode = candidate["mode"] != self.settings["mode"]
            pending_replies = [] if changed_mode else [r['id'] for r in self.db.execute(
                "SELECT DISTINCT c.id FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE c.mode=? AND c.status='ai' AND m.role='assistant' AND m.status='pending'",
                (self.settings['mode'],)).fetchall()]
            if changed_mode:
                self.db.execute('UPDATE conversations SET resume_after=?',(time.time(),))
            if changed_mode:
                self._disconnect_locked()
            else:
                self._invalidate()
            self.settings = candidate
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (json.dumps(candidate, ensure_ascii=False),))
            # Only replace work that this save just cancelled. Do not replay
            # historic failures, manual conversations, or messages in another mode.
            for cid in pending_replies:
                self._recover_latest(cid, check_handoff=True)
        if changed_mode:
            self._stop_transport()
        configure_follow = getattr(self.transport, 'set_follow_back', None)
        if callable(configure_follow):
            configure_follow(candidate['auto_follow_enabled'] and candidate['mode'] == 'live')
        return self.state()

    def _invalidate(self):
        self.db.execute("UPDATE conversations SET generation=generation+1")
        self.db.execute("UPDATE messages SET status='cancelled' WHERE status='pending'")

    def _disconnect_locked(self):
        self.epoch += 1
        self._invalidate()
        self.connection = {"status": "disconnected", "detail": "已断开"}

    def _stop_transport(self):
        with self.lock:
            if not self.transport_started:
                return
            self.transport_started = False
        try:
            self.transport.disconnect()
        except Exception:
            with self.lock:
                self._event("error", "连接清理失败，请重启程序后重新连接")

    def refresh_strangers(self):
        with self.lock:
            if self.settings['mode'] != 'live' or self.connection['status'] != 'connected':
                raise ValueError('请先连接真实抖音账号')
            return self.transport.refresh_strangers()

    def disconnect(self):
        with self.lifecycle:
            with self.lock:
                self._disconnect_locked()
            self._stop_transport()
        return self.state()

    def connect(self):
        with self.lifecycle:
            with self.lock:
                if self.connection["status"] in ("connected", "connecting"):
                    return self.state()
                if self.settings["mode"] == "demo":
                    self.connection = {"status": "connected", "detail": "演示连接，不访问外部服务"}
                    return self.state()
                if not self.settings["deepseek_api_key"]:
                    raise ValueError("请先配置 DeepSeek API Key")
                self.epoch += 1
                epoch = self.epoch
                self.connection = {"status": "connecting", "detail": "正在连接抖音"}
                settings = dict(self.settings)
                self.transport_started = True
                for row in self.db.execute("SELECT id FROM conversations WHERE mode='live' AND sec_uid<>''").fetchall():
                    self._queue_profile(row['id'])
            def message(*args, **kwargs):
                with self.lock:
                    if not self.closed and epoch == self.epoch and self.connection["status"] == "connected":
                        if 'batch' in kwargs:
                            # Keep the lock until a whole recovered conversation
                            # has been ingested, so an earlier handoff wins before
                            # any queued AI generation is allowed to run.
                            self._receive_batch(kwargs['batch'], recovery=bool(kwargs.get('recovery')))
                        else:
                            self.receive(*args, **kwargs)
                        return True
                    return False
            def status(value, detail=""):
                with self.lock:
                    if self.closed or epoch != self.epoch:
                        return
                    value = value if value in ("connected", "connecting", "disconnected", "error") else "error"
                    safe_detail = {"connected": "抖音已连接", "connecting": "正在连接抖音",
                                   "disconnected": "抖音连接已断开", "error": "抖音连接失败，请检查本地登录配置"}[value]
                    # Only exact local messages may reach the UI; upstream
                    # exception strings can contain account credentials.
                    if value == "error" and detail in {
                        "私信发送需要配套签名凭证：设置中的 Cookie 覆盖与扫码凭证不一致，请勾选「清除 Cookie 覆盖」后保存，无需重新扫码。",
                        "请先运行「扫码登录.cmd」保存配套登录凭证",
                        "缺少抖音运行依赖，请先运行「安装真实连接依赖.cmd」",
                        "账号不匹配：此数据目录属于其他抖音账号，请登录原账号；其他账号需使用独立工作目录",
                        "账号归属无法确认：现有聊天记录缺少账号凭据，请先恢复该目录的账号记录",
                    }:
                        safe_detail = detail
                    self.connection = {"status": value, "detail": safe_detail}
                    if value == 'connected' and not getattr(self.transport, 'requires_history_before_resume', False):
                        for row in self.db.execute("SELECT id FROM conversations WHERE mode='live' AND status='ai'").fetchall():
                            self._recover_latest(row['id'])
                    if value == 'connected' and callable(getattr(self.transport, 'contact_uid', None)):
                        for row in self.db.execute("SELECT id,raw_id FROM conversations WHERE mode='live'").fetchall():
                            uid = self.transport.contact_uid(row['raw_id'])
                            if uid:
                                self.db.execute('UPDATE conversations SET sender_id=? WHERE id=?', (uid,row['id']))
                                self._queue_profile(row['id'])
                    if value in ("disconnected", "error"):
                        self._invalidate()
                    self._event("error" if value == "error" else "info", safe_detail)
            try:
                self.transport.connect(settings, message, status)
            except Exception:
                status("error")
                raise ValueError("连接失败，请检查本地登录配置") from None
        return self.state()

    def receive(self, conversation_id, sender_id, text, message_id, name=None, sec_uid='', created_at_ms=0, media_kind='', media_url='', is_outgoing=False, client_message_id='', peer_id='', _schedule_reply=True, _history=False, history_only=False, media_secret='', media_md5=''):
        with self.lock:
            if self.closed or self.settings["mode"] != "live" or self.connection["status"] != "connected":
                return
            if is_outgoing:
                cid = self._receive_owner(str(conversation_id), self._text(text), str(message_id),
                    str(client_message_id), created_at_ms, media_kind, media_url, historical=_history, media_secret=media_secret, media_md5=media_md5)
                if not self.db.execute('SELECT 1 FROM conversations WHERE id=?', (cid,)).fetchone():
                    return
                # Sender is the account owner; it must never replace the peer.
                if peer_id:
                    self.db.execute('UPDATE conversations SET sender_id=? WHERE id=?', (str(peer_id), cid))
            else:
                cid = self._receive("live", str(conversation_id), self._text(text), str(message_id), name, schedule_reply=_schedule_reply,created_at_ms=created_at_ms,media_kind=media_kind,media_url=media_url,history_only=history_only,media_secret=media_secret,media_md5=media_md5)
                self.db.execute("UPDATE conversations SET sender_id=?,sec_uid=CASE WHEN ?='' THEN sec_uid ELSE ? END WHERE id=?", (str(sender_id),sec_uid,sec_uid,cid))
            self._queue_profile(cid)
            return cid

    def recover_batch(self, events):
        return self._receive_batch(events, recovery=True)

    def _receive_batch(self, events, recovery=False):
        with self.lock:
            if self.closed or self.settings['mode'] != 'live' or self.connection['status'] != 'connected':
                return False
            touched = set()
            # Capture coverage BEFORE ingestion. An owner event within this batch
            # must not discard other messages from the same recovered history.
            covered = {}
            for event in events:
                raw = str(event['conversation_id'])
                cid = 'live:' + raw
                timestamp = event.get('created_at_ms', 0)
                outgoing = event.get('is_outgoing', False)
                if recovery and not timestamp and not outgoing:
                    continue
                if cid not in covered:
                    sent = self.db.execute("SELECT m.created_at FROM messages m LEFT JOIN message_origins o ON o.message_id=m.id WHERE m.conversation_id=? AND m.role IN ('assistant','human') AND m.status='sent' AND COALESCE(o.origin,'desk')='desk' ORDER BY julianday(m.created_at) DESC,m.rowid DESC LIMIT 1", (cid,)).fetchone()
                    covered[cid] = datetime.fromisoformat(sent['created_at']).timestamp()*1000 if sent else 0
                if recovery and not outgoing and covered[cid] >= timestamp:
                    event = dict(event, history_only=True)
                received_cid = self.receive(**event, _schedule_reply=False, _history=recovery)
                if received_cid and not event.get('history_only'):
                    touched.add(received_cid)
            for cid in touched:
                self._recover_latest(cid)
            return True

    def _receive_owner(self, raw, text, message_id, client_message_id, created_at_ms, media_kind, media_url, historical=False, media_secret='', media_md5=''):
        cid = 'live:' + raw
        self.db.execute('BEGIN IMMEDIATE')
        try:
            inserted = self.db.execute('INSERT OR IGNORE INTO inbound VALUES(?,?,?)', ('live', raw, message_id)).rowcount
            if not inserted:
                self._enrich_media('live', raw, message_id, created_at_ms, media_kind, media_url, media_secret, media_md5, outgoing=True)
                self.db.execute('COMMIT')
                return cid
            own = self.db.execute('SELECT message_id FROM outbound_ids WHERE client_message_id=? AND conversation_id=?', (client_message_id, cid)).fetchone() if client_message_id else None
            if own:
                self.db.execute('UPDATE message_origins SET platform_message_id=? WHERE message_id=?', (message_id, own['message_id']))
                if created_at_ms:
                    self.db.execute('UPDATE messages SET created_at=? WHERE id=?',
                        (datetime.fromtimestamp(created_at_ms/1000, timezone.utc).isoformat(), own['message_id']))
                self.db.execute('COMMIT')
                return cid
            stamp = created_at_ms / 1000 if created_at_ms else (0 if historical else time.time())
            # Legacy history has no reliable source identity. Never manufacture
            # a second chat bubble or a verification task from those old echoes.
            if stamp < self.owner_sync_started:
                self.db.execute('COMMIT')
                return cid
            self.db.execute("INSERT OR IGNORE INTO conversations(id,mode,raw_id,name,updated_at) VALUES(?,'live',?,'抖音联系人',?)", (cid, raw, now()))
            origin = 'douyin'
            received_at = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            mid = self._message(cid, 'human', text, 'sent', created_at=received_at)
            self.db.execute('INSERT INTO message_origins VALUES(?,?,?,?)', (mid, origin, message_id, client_message_id))
            if media_kind:
                from .profiles import safe_avatar
                self.db.execute('INSERT INTO message_media VALUES(?,?,?)', (mid, media_kind, safe_avatar(media_url)))
                self._store_media_secret(mid, media_secret, media_md5)
            row = self._get(cid)
            if origin == 'douyin' and stamp > row['resume_after']:
                self._cancel(cid, 'human')
                self.workspace.record_handoff(cid, '本人已在抖音客户端回复')
                self.workspace.accept_handoff(cid)
                self._event('info', '检测到抖音客户端本人回复，已暂停自动回复并转人工接待')
            self.workspace.refresh_memory(cid)
            self.db.execute('COMMIT')
            return cid
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def _recover_latest(self, cid, check_handoff=False):
        row = self._get(cid)
        if row['status'] != 'ai' or (row['mode'] == 'live' and self.connection['status'] != 'connected'):
            return
        # Pending/sending/sent/failed outgoing rows all block a second attempt.
        # Only work cancelled before sending can resume automatically.
        last = self.db.execute("SELECT id,role,text,created_at FROM messages WHERE conversation_id=? AND status<>'cancelled' AND id NOT IN (SELECT local_id FROM inbound_refs WHERE history_only=1) ORDER BY julianday(created_at) DESC,rowid DESC LIMIT 1",(cid,)).fetchone()
        if not last or last['role'] != 'user':
            return
        if self.db.execute("SELECT 1 FROM reply_sources r JOIN messages m ON m.id=r.message_id WHERE r.inbound_message_id=? AND m.status<>'cancelled' LIMIT 1", (last['id'],)).fetchone():
            return
        if datetime.fromisoformat(last['created_at']).timestamp() <= row['resume_after']:
            return
        if check_handoff and (last['text'].startswith('[非文字消息') or any(
                keyword in last['text'] for keyword in self.settings['handoff_keywords'])):
            self._cancel(cid, 'waiting')
            self.workspace.record_handoff(cid, '待回复消息命中新保存的人工关键词')
            self._event('info', '待回复消息命中新保存的人工关键词，已暂停自动回复')
            return
        self._schedule_reply(cid,row['raw_id'],row['mode'],last['text'],row)
        self._event('info','已恢复一条尚未回复的私信')

    def demo_message(self, text, conversation_id=None):
        with self.lock:
            if self.settings["mode"] != "demo":
                raise ValueError("仅演示模式允许模拟消息")
            raw = "visitor-" + uuid4().hex
            serial = self.db.execute("SELECT COUNT(*) FROM conversations WHERE mode='demo'").fetchone()[0] + 1
            name = f"演示访客 {serial:02d}"
            if conversation_id:
                row = self._get(conversation_id)
                raw, name = row["raw_id"], row["name"]
            cid = self._receive("demo", raw, self._text(text), uuid4().hex, name)
            return {"conversation_id": cid}

    def _enrich_media(self, mode, raw, platform_id, stamp, kind, url, secret='', md5='', outgoing=False):
        from .profiles import safe_avatar
        url = safe_avatar(url)
        if kind not in ('image', 'sticker') or not url:
            return
        if outgoing:
            # Only exact platform identity for a manually sent Douyin message.
            # Never replace local attachments belonging to our own send echoes.
            ref = self.db.execute("SELECT m.id FROM messages m JOIN message_origins o ON o.message_id=m.id WHERE m.conversation_id=? AND o.origin='douyin' AND o.platform_message_id=?", (mode+':'+raw,platform_id)).fetchone()
        else:
            ref = self.db.execute('SELECT local_id FROM inbound_refs WHERE mode=? AND raw_id=? AND platform_id=?', (mode,raw,platform_id)).fetchone()
        if not ref and stamp and not outgoing:
            # Old records predate the explicit platform->local mapping. Only an
            # exact conversation, direction, timestamp and media placeholder match
            # may be repaired; never guess from arbitrary text or a nearby time.
            candidates = self.db.execute("SELECT m.id,m.created_at FROM messages m LEFT JOIN message_media a ON a.message_id=m.id WHERE m.conversation_id=? AND m.role='user' AND (a.kind IN ('image','sticker','unsupported') OR m.text IN ('[表情包]','[非文字消息，请人工处理]'))", (mode+':'+raw,)).fetchall()
            matches = [r for r in candidates if abs(datetime.fromisoformat(r['created_at']).timestamp()*1000-stamp)<1]
            if len(matches)==1:
                ref = (matches[0]['id'],)
                self.db.execute('INSERT OR IGNORE INTO inbound_refs VALUES(?,?,?,?,0)', (mode,raw,platform_id,ref[0]))
        if ref:
            self.db.execute('INSERT INTO message_media VALUES(?,?,?) ON CONFLICT(message_id) DO UPDATE SET kind=excluded.kind,url=excluded.url', (ref[0],kind,url))
            self._store_media_secret(ref[0], secret, md5)

    def _store_media_secret(self, mid, secret, md5):
        import re
        if isinstance(secret,str) and re.fullmatch(r'[a-fA-F0-9]{64}',secret):
            checksum = md5 if isinstance(md5,str) and re.fullmatch(r'[a-fA-F0-9]{32}',md5) else ''
            self.db.execute('INSERT INTO incoming_sources VALUES(?,?,?) ON CONFLICT(message_id) DO UPDATE SET secret=excluded.secret,md5=excluded.md5',(mid,secret,checksum))

    def _receive(self, mode, raw, text, message_id, name, schedule_reply=True, created_at_ms=0, media_kind='', media_url='', history_only=False, media_secret='', media_md5=''):
        cid = mode + ":" + raw
        # Persist deduplication and inbound message atomically, including across restarts.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            inserted = self.db.execute("INSERT OR IGNORE INTO inbound VALUES(?,?,?)", (mode, raw, message_id)).rowcount
            if not inserted:
                self._enrich_media(mode, raw, message_id, created_at_ms, media_kind, media_url, media_secret, media_md5)
                if history_only:
                    self.db.execute('UPDATE inbound_refs SET history_only=1 WHERE mode=? AND raw_id=? AND platform_id=?', (mode,raw,message_id))
                self.db.execute("COMMIT")
                return cid
            self.db.execute("INSERT OR IGNORE INTO conversations(id,mode,raw_id,name,updated_at) VALUES(?,?,?,?,?)",
                            (cid, mode, raw, name or "抖音联系人", now()))
            received_at = datetime.fromtimestamp(created_at_ms/1000,timezone.utc).isoformat() if created_at_ms else None
            mid = self._message(cid, "user", text, "received", created_at=received_at)
            self.db.execute('INSERT INTO inbound_refs VALUES(?,?,?,?,?)', (mode, raw, message_id, mid, int(history_only)))
            if media_kind:
                from .profiles import safe_avatar
                self.db.execute('INSERT INTO message_media VALUES(?,?,?)',(mid,media_kind,safe_avatar(media_url)))
                self._store_media_secret(mid, media_secret, media_md5)
            if history_only:
                latest = self.db.execute('SELECT text,created_at FROM messages WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(cid,)).fetchone()
                self.db.execute('UPDATE conversations SET preview=?,updated_at=? WHERE id=?',(latest['text'][:160],latest['created_at'],cid))
                self.db.execute('COMMIT')
                return cid
            self.db.execute("UPDATE conversations SET unread=unread+1 WHERE id=?", (cid,))
            self._cancel(cid)
            row = self._get(cid)
            handoff = media_kind != 'sticker' and (text.startswith("[非文字消息") or any(k in text for k in self.settings["handoff_keywords"]))
            if handoff and row["status"] != "human":
                self.db.execute("UPDATE conversations SET status='waiting' WHERE id=?", (cid,))
                self.workspace.record_handoff(cid, '消息需要人工处理' if text.startswith('[非文字消息') else '客户消息命中转人工关键词')
                self._event("info", "收到人工服务请求，已暂停自动回复")
            self.workspace.refresh_memory(cid)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        if handoff or row["status"] != "ai" or not schedule_reply:
            return cid
        self._schedule_reply(cid,raw,mode,text,row)
        return cid

    def _schedule_reply(self, cid, raw, mode, text, row):
        mid = self._message(cid, "assistant", "", "pending")
        incoming = self.db.execute("SELECT id FROM messages WHERE conversation_id=? AND role='user' ORDER BY julianday(created_at) DESC,rowid DESC LIMIT 1", (cid,)).fetchone()
        if incoming:
            self.db.execute('INSERT INTO reply_sources VALUES(?,?)', (mid,incoming['id']))
        history = [dict(r) for r in self.db.execute(
            "SELECT role,text FROM messages WHERE conversation_id=? AND (role='user' OR (role IN ('assistant','human') AND status='sent')) ORDER BY created_at DESC,rowid DESC LIMIT 40", (cid,))][::-1]
        for item in history:
            if item["role"] == "human":
                item["role"] = "assistant"
        # Current text is supplied separately to the responder.
        history = history[:-1]
        if not self.slots.acquire(blocking=False):
            self._failure(cid, mid, "待处理消息过多，已转人工")
            return cid
        settings = dict(self.settings)
        if text.startswith('[图片]') or text.startswith('[表情包'):
            settings['persona'] += '\n当前消息是图片或表情。你未获得图片像素，不得声称看到了画面或推断其中产品、文字、情绪。可以结合已知上下文自然回应，并询问客户需要咨询的问题；表情名称仅作为文字线索。'
        settings['_local_knowledge'] = self.workspace.search(mode, text)
        settings['_customer_context'] = self._customer_context(self.workspace.customer(cid))
        future = self.pool.submit(self._reply, cid, raw, mode, row["generation"], self.epoch,
                                  mid, settings, history, text)
        future.add_done_callback(lambda _: self.slots.release())
        return cid

    def _valid(self, cid, generation, epoch, mode):
        if self.closed or self.epoch != epoch or self.settings["mode"] != mode:
            return False
        row = self._get(cid)
        return row["generation"] == generation and row["status"] == "ai" and (
            mode == "demo" or self.connection["status"] == "connected")

    def _reply(self, cid, raw, mode, generation, epoch, mid, settings, history, text):
        stage = "生成回复"
        try:
            with self.lock:
                if not self._valid(cid, generation, epoch, mode):
                    return
            reply = ("【演示回复】收到你的消息：" + text[:200] + "。这是本地模拟回复，未调用 AI。"
                     if mode == "demo" else self._text(self.responder(settings, history, text)))
            with self.lock:
                if not self._valid(cid, generation, epoch, mode):
                    return
            use_prepared = mode == "live" and callable(getattr(self.transport, "prepare", None))
            stage = "准备私信会话"
            prepared = self.transport.prepare(raw) if use_prepared else None
            with self._send_lock(cid):
                with self.lock:
                    if not self._valid(cid, generation, epoch, mode):
                        return
                    # Commit to sending under the same lock as the final validity
                    # check. Settings must not cancel/reschedule this row in the
                    # gap before _deliver begins its external request.
                    self.db.execute("UPDATE messages SET text=?,status='sending' WHERE id=?", (reply, mid))
                    self._register_outbound(cid, mid)
                    self.active_sends += 1
                self._deliver(cid, raw, mode, mid, reply, prepared, use_prepared)
        except Exception as exc:
            with self.lock:
                if self._valid(cid, generation, epoch, mode):
                    detail = str(exc) if isinstance(exc, (IntegrationError, IntegrationProtocolError, HandoffRequired)) else stage + "失败，请检查服务配置"
                    self._failure(cid, mid, detail + "；已转人工，未自动重试")

    def close(self):
        with self.lifecycle:
            with self.lock:
                if self.closed:
                    return
                self._disconnect_locked()
                self.closed = True
            self._stop_transport()
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.profile_pool.shutdown(wait=False, cancel_futures=True)
            with self.lock:
                self._close_db_if_idle()
