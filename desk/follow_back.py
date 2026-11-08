"""Account-scoped, durable new-follower queue. No message sending or unfollowing."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import threading
import time


class FollowError(RuntimeError):
    pass


def identity(user):
    uid, sec = str(user.get('uid', '')), user.get('sec_uid', '')
    if not uid.isdigit() or uid == '0' or not isinstance(sec, str) or not re.fullmatch(r'MS4w[A-Za-z0-9_=-]+', sec):
        raise FollowError('粉丝身份不完整，本轮未处理')
    return uid, sec


class FollowStore:
    def __init__(self, folder):
        self.path = Path(folder) / 'follow-back.sqlite3'
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts(uid TEXT PRIMARY KEY, initialized INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS fans(owner TEXT, uid TEXT, sec TEXT, status TEXT,
                    updated REAL, PRIMARY KEY(owner,uid));
            ''')
            # A previous process may have exited after submitting the request.
            db.execute("UPDATE fans SET status='unknown' WHERE status='sending'")
            db.commit()

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def snapshot(self, owner, users):
        """Commit ONLY a fully validated scan; partial scans never create tasks."""
        with closing(self.connect()) as db, db:
            initialized = db.execute('SELECT 1 FROM accounts WHERE uid=?', (owner,)).fetchone()
            for uid, sec in users.items():
                if uid != owner:
                    db.execute('INSERT OR IGNORE INTO fans VALUES(?,?,?,?,?)',
                               (owner, uid, sec, 'pending' if initialized else 'baseline', time.time()))
            db.execute('INSERT OR IGNORE INTO accounts VALUES(?,1)', (owner,))
        return not bool(initialized)

    def next(self, owner):
        with closing(self.connect()) as db:
            return db.execute("SELECT uid,sec,status FROM fans WHERE owner=? AND status IN ('pending','unknown') ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,updated LIMIT 1", (owner,)).fetchone()

    def mark(self, owner, uid, status):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE fans SET status=?,updated=? WHERE owner=? AND uid=?',
                       (status, time.time(), owner, uid))

    def claim(self, owner, uid):
        with closing(self.connect()) as db, db:
            return db.execute("UPDATE fans SET status='sending',updated=? WHERE owner=? AND uid=? AND status='pending'",
                              (time.time(), owner, uid)).rowcount == 1

    def counts(self, owner):
        with closing(self.connect()) as db:
            return dict(db.execute('SELECT status,count(*) FROM fans WHERE owner=? GROUP BY status', (owner,)))

    def recent(self, owner):
        with closing(self.connect()) as db:
            return [dict(uid=uid, sec_uid=sec, status=status) for uid, sec, status in db.execute(
                "SELECT uid,sec,status FROM fans WHERE owner=? AND status<>'baseline' ORDER BY updated DESC LIMIT 20", (owner,))]


class FollowBackWorker:
    """One worker, one write at a time; disabling waits for an already committed write."""
    def __init__(self, folder, backend):
        self.folder, self.backend_factory = Path(folder), backend
        self.gate = threading.RLock()
        self.enabled = False
        self.generation = 0
        self.wake = threading.Event()
        self.thread = None
        self.store = None
        self.token = None
        self.scan = None
        self.next_scan = 0
        self.failures = 0
        self._state = {'status': 'disabled', 'detail': '自动回关未开启', 'counts': {}, 'recent': []}

    def state(self):
        with self.gate:
            return {**self._state, 'counts': dict(self._state['counts']), 'recent': list(self._state['recent'])}

    def configure(self, enabled, *, start=True):
        with self.gate:
            if self.enabled == bool(enabled) and self.thread and self.thread.is_alive():
                return
            self.generation += 1
            self.enabled = bool(enabled)
            if not enabled:
                self.scan = None
                self.token = None
                self._state.update(status='disabled', detail='自动回关已停止')
            elif not self.thread or not self.thread.is_alive():
                self._state.update(status='waiting', detail='等待账号连接，首次完整扫描仅记录现有粉丝')
                if start:
                    self.thread = threading.Thread(target=self.run, daemon=True, name='douyin-follow-back')
                    self.thread.start()
        self.wake.set()

    def report(self, status, detail, counts=None):
        with self.gate:
            if self.enabled:
                self._state.update(status=status, detail=detail)
                if counts is not None:
                    self._state['counts'] = counts
                    self._state['recent'] = self.store.recent(self.token[0]) if self.store and self.token else []

    def run(self):
        while True:
            self.wake.clear()
            with self.gate:
                if not self.enabled:
                    self.thread = None
                    return
            try:
                self.step()
                self.failures = 0
                delay = 60
            except Exception as exc:
                self.scan = None
                self.failures += 1
                delay = min(900, 60 * 2 ** min(self.failures, 4))
                detail = str(exc) if type(exc) is FollowError else '自动回关检查失败，请核对网络和登录状态'
                self.report('error', f'{detail}；{delay} 秒后重新检查')
            self.wake.wait(delay)

    def step(self):
        with self.gate:
            if not self.enabled:
                return
            generation = self.generation
        backend = self.backend_factory()
        if backend is None:
            self.report('waiting', '等待抖音账号连接')
            return
        if self.store is None:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.store = FollowStore(self.folder)
        if backend.token != self.token:
            self.token, self.scan, self.next_scan = backend.token, None, 0
            self.report('scanning', '正在核对当前账号的粉丝', {})
        owner = backend.owner
        if self.scan is None and time.time() >= self.next_scan:
            self.scan = {'users': {}, 'cursor': None, 'cursors': set(), 'pages': 0}
        scan = self.scan
        if scan is not None:
            payload = backend.page(scan['cursor'])
            if (not isinstance(payload, dict) or payload.get('status_code') != 0
                    or not isinstance(payload.get('followers'), list)
                    or payload.get('has_more') not in (0, 1, False, True)):
                raise FollowError('粉丝列表读取失败，未建立基线或增加回关任务')
            users = dict(identity(user) for user in payload['followers'])
            if any(uid in scan['users'] and scan['users'][uid] != sec for uid, sec in users.items()):
                raise FollowError('粉丝身份在分页间发生冲突，本轮未处理')
            scan['users'].update(users)
            scan['pages'] += 1
            if payload['has_more']:
                cursor = payload.get('max_time')
                if (not users or not str(cursor).isdigit() or int(cursor) <= 0
                        or str(cursor) in scan['cursors'] or scan['pages'] >= 500):
                    raise FollowError('粉丝分页不完整，请人工核对；未处理本轮名单')
                scan['cursor'] = str(cursor)
                scan['cursors'].add(str(cursor))
                self.report('scanning', f'正在读取第 {scan["pages"] + 1} 页粉丝')
            else:
                total = payload.get('total')
                if type(total) is int and total > len(scan['users']):
                    raise FollowError('粉丝列表数量不完整，本轮未处理')
                with self.gate:
                    if not self.enabled or generation != self.generation or not backend.active():
                        return
                    first = self.store.snapshot(owner, scan['users'])
                    self.scan = None
                    self.next_scan = time.time() + 300
                self.report('watching', '现有粉丝已记录，开始监测新增粉丝' if first else '粉丝检查完成', self.store.counts(owner))
        task = self.store.next(owner)
        if not task:
            return
        self.process_task(backend, task, generation)

    def process_task(self, backend, task, generation):
        owner = backend.owner
        uid, sec, status = task
        relation = backend.relation(uid, sec)
        if relation['following']:
            self.store.mark(owner, uid, 'confirmed' if status == 'unknown' else 'already_followed')
        elif status == 'unknown':
            # Never convert a lost acknowledgement into a second follow request.
            self.store.mark(owner, uid, 'needs_review')
        elif not relation['follower']:
            self.store.mark(owner, uid, 'not_follower')
        else:
            # All request construction/credential failures happen before committing.
            submit = backend.prepare(uid, sec)
            with self.gate:
                if not self.enabled or generation != self.generation or not backend.active():
                    return
                if not self.store.claim(owner, uid):
                    return
                try:
                    submit()
                except Exception:
                    self.store.mark(owner, uid, 'unknown')
                    self.report('error', '回关请求结果未确认', self.store.counts(owner))
                    raise FollowError('回关请求结果未确认，将只核对关系，不重复发送') from None
                self.store.mark(owner, uid, 'unknown')
            # API acceptance alone is not proof of the resulting relationship.
            if backend.relation(uid, sec)['following']:
                self.store.mark(owner, uid, 'confirmed')
        self.report('watching', '自动回关监测中；未确认的请求不会重复发送', self.store.counts(owner))


class DouyinFollowAPI:
    """Existing authenticated HTTP stack; follow-only, followed by relationship verification."""
    def __init__(self, auth, api, owner, token, active):
        self.auth, self.api, self.owner, self.token = auth, api, owner, token
        self.active = active

    def page(self, cursor):
        sec = self.api.get_my_sec_uid(self.auth)
        identity({'uid': self.owner, 'sec_uid': sec})
        # Verify that the profile returned for the owner's sec_uid is the bound account.
        self.user(self.owner, sec)
        return self.api.get_user_follower_list(self.auth, self.owner, sec, max_time=cursor, count='20')

    def user(self, uid, sec):
        identity({'uid': uid, 'sec_uid': sec})
        result = self.api.get_user_info(self.auth, 'https://www.douyin.com/user/' + sec)
        user = result.get('user', {})
        if result.get('status_code') != 0 or identity(user) != (uid, sec):
            raise FollowError('用户关系核对失败，未执行回关')
        return user

    def relation(self, uid, sec):
        user = self.user(uid, sec)
        follow, follower = user.get('follow_status'), user.get('follower_status')
        if type(follow) is not int or follow not in (0, 1, 2) or type(follower) is not int or follower not in (0, 1):
            raise FollowError('平台未返回明确的关注关系，未执行回关')
        return {'following': follow in (1, 2), 'follower': follower == 1}

    def prepare(self, uid, sec):
        from builder.header import HeaderBuilder, HeaderType
        from builder.params import Params
        from utils import http_client
        identity({'uid': uid, 'sec_uid': sec})
        if uid == self.owner:
            raise FollowError('不能回关当前登录账号')
        path = '/aweme/v1/web/commit/follow/user/'
        referer = 'https://www.douyin.com/user/' + sec
        headers = HeaderBuilder.build(HeaderType.FORM)
        headers.set_referer(referer)
        headers.set_header('origin', 'https://www.douyin.com')
        headers.with_uifid(self.auth)
        headers.with_csrf(self.auth.cookie_str)
        try:
            headers.with_bd(path, self.auth, require_dtrait=True)
        except Exception:
            raise FollowError('当前登录缺少可用的关注操作凭证，需要完成关注接口验证') from None
        params = Params()
        params.with_platform(auth=self.auth, url=referer)
        params.add_param('msToken', self.auth.msToken)
        body = {'type': '1', 'user_id': uid, 'sec_user_id': sec}
        params.with_a_bogus(data=body)

        def submit():
            response = http_client.post('https://www.douyin.com' + path, headers=headers.get(),
                cookies=self.auth.cookie, params=params.get(), data=body,
                timeout=20, verify=True, allow_redirects=False)
            response.raise_for_status()
            result = response.json()
            if result.get('status_code') != 0:
                raise FollowError('平台未确认回关结果')
        return submit
