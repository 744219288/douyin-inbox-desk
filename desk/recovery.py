"""Read ordinary and stranger inboxes; never marks messages read or accepted."""
import json
import time
import threading
from .stranger import (_field as f, _fields, _one, _integer, _text, _post,
                      parse_response, fetch_stranger, fetch_messages, StrangerError)


def recovery_start(data_dir, uid):
    path=data_dir/'recovery-window.json'
    windows=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if uid in windows:
        value=windows[uid]
        if not isinstance(value,int) or value<=0 or value>int(time.time()*1000):
            raise StrangerError('恢复记录异常，请人工核对')
        return value
    windows[uid]=int((time.time()-86400)*1000)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(windows),encoding='utf-8')
    temp.replace(path)
    return windows[uid]


def request(auth, cmd, fields, endpoint):
    from builder.proto import ProtoBuilder
    req = ProtoBuilder.build_normal_request(auth, cmd)
    req.inbox_type = 1
    req.ClearField('body')
    raw = _post(auth,'https://imapi.douyin.com'+endpoint,req.SerializeToString()+f(8,f(cmd,fields)))
    top = _fields(raw)
    code = _integer(top,3)
    if code or _text(top,4) != 'OK':
        raise StrangerError(f'离线私信读取被拒绝（{code}）',code=code,
                            rate_limited=_text(top,4).strip().lower()=='too many requests')
    if _integer(top,1) != cmd:
        raise StrangerError('离线私信响应命令不匹配')
    body = _fields(_one(top,6,b''))
    if cmd not in body:
        raise StrangerError('离线私信响应不完整')
    return _fields(_one(body,cmd))


def messages(raw_messages, uid):
    wrapped = b''.join(f(4,f(3,message)) for message in raw_messages)
    envelope = f(1,1001)+f(4,b'OK')+f(6,f(1000,wrapped))
    return parse_response(envelope,uid,include_self=True)['events']


def fetch_ordinary(auth):
    cursor=0; visited=set(); latest={}
    for _ in range(10):
        body=request(auth,203,f(1,cursor),'/v2/message/get_by_user_init')
        for event in messages(body.get(1,[]),auth.get_uid()):
            old=latest.get(event['conversation_id'])
            if not old or (event['created_at_ms'],int(event['message_id'])) > (old['created_at_ms'],int(old['message_id'])):
                latest[event['conversation_id']]=event
        if not _integer(body,5):
            return {'events':list(latest.values()),
                    'count':len(latest),'truncated':False,'source':'普通私信'}
        visited.add(cursor)
        cursor=_integer(body,4)
        if cursor in visited:
            raise StrangerError('普通私信分页没有推进')
    raise StrangerError('普通私信会话超过补查上限，请人工检查')


def fetch_history(auth,cid,short_id):
    cursor=0; visited=set(); found={}
    for _ in range(10):
        query=f(1,cid.encode())+f(2,1)+f(3,int(short_id))+f(4,1)+f(5,cursor)+f(6,50)
        body=request(auth,301,query,'/v1/message/get_by_conversation')
        page=messages(body.get(1,[]),auth.get_uid())
        for event in page:
            if event['conversation_id']!=cid or event['conversation_short_id']!=str(short_id):
                raise StrangerError('普通私信历史归属不匹配')
            found[event['message_id']]=event
        # All more-recent messages are present once a previous owner reply is found.
        if not _integer(body,3) or any(e['is_outgoing'] for e in page):
            return sorted(found.values(),key=lambda e:(e['created_at_ms'],int(e['message_id'])))
        visited.add(cursor)
        cursor=_integer(body,2)
        if cursor in visited:
            raise StrangerError('普通私信历史分页没有推进')
    raise StrangerError('普通私信历史超过补查上限，请人工检查')


class RecoveryInbox:
    def __init__(self,auth,incremental=False):
        self.auth=auth
        self.normal_next=True
        self.routes={}
        self.incremental = incremental
        self.cursor = 0
        self.visited = set()
        self.latest = {}
        self.priority = {}
        self.lock = threading.Lock()

    def prioritize(self, event):
        with self.lock:
            self.priority[event['conversation_id']] = dict(event)

    def has_priority(self):
        with self.lock:
            return bool(self.priority)

    def ordinary_page(self):
        body = request(self.auth,203,f(1,self.cursor),'/v2/message/get_by_user_init')
        for event in messages(body.get(1,[]),self.auth.get_uid()):
            old = self.latest.get(event['conversation_id'])
            if not old or (event['created_at_ms'],int(event['message_id'])) > (old['created_at_ms'],int(old['message_id'])):
                self.latest[event['conversation_id']] = event
        more = bool(_integer(body,5))
        if more:
            next_cursor = _integer(body,4)
            if next_cursor == self.cursor or next_cursor in self.visited:
                raise StrangerError('普通私信分页没有推进')
            self.visited.add(self.cursor)
            self.cursor = next_cursor
        else:
            self.cursor = 0
            self.visited.clear()
        return {'events':list(self.latest.values()),'count':len(self.latest),
                'truncated':False,'partial':more,'source':'普通私信逐页补查'}

    def fetch_all(self):
        """Explicit refresh covers both inboxes under the shared read budget."""
        normal = self.fetch(normal=True, full=True)
        normal_routes = dict(self.routes)
        stranger = self.fetch(normal=False)
        # A migrated conversation can occur in both lists. Prefer its ordinary
        # route when both snapshots report the same latest message.
        merged = {}
        for source in (normal, stranger):
            for event in source['events']:
                cid = event['conversation_id']
                old = merged.get(cid)
                if old is None or (event['created_at_ms'], int(event['message_id'])) > (old['created_at_ms'], int(old['message_id'])):
                    merged[cid] = event
        for event in normal['events']:
            if merged.get(event['conversation_id']) is event:
                self.routes[event['conversation_short_id']] = normal_routes[event['conversation_short_id']]
        return {'events': list(merged.values()), 'count': len(merged),
                'truncated': normal.get('truncated', False) or stranger.get('truncated', False),
                'source': '普通与陌生人私信'}

    def fetch(self, normal=None, full=False):
        if normal is None:
            with self.lock:
                queued = list(self.priority.values())
                self.priority.clear()
            if queued:
                for event in queued:
                    self.routes[event['conversation_short_id']] = (True,event['conversation_id'],event['message_id'])
                return {'events':queued,'count':len(queued),'truncated':False}
        if normal is None:
            normal=self.normal_next
            self.normal_next=not normal
        snapshot=(self.ordinary_page() if self.incremental and not full else fetch_ordinary(self.auth)) if normal else fetch_stranger(self.auth)
        for event in snapshot['events']:
            self.routes[event['conversation_short_id']]=(normal,event['conversation_id'],event['message_id'])
            event['name']='抖音联系人' if normal else '陌生人私信'
        return snapshot

    def history(self,short_id):
        normal,cid,latest=self.routes[short_id]
        if normal:
            return fetch_history(self.auth,cid,short_id)
        history=fetch_messages(self.auth,short_id,include_self=True)
        # Some stranger threads are listed but their dedicated history is empty.
        # Fall back to ordinary history, still under the shared request budget.
        if not any(event['message_id']==latest for event in history):
            return fetch_history(self.auth,cid,short_id)
        return history
