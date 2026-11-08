"""Persistent customer workspace and published knowledge retrieval.

All methods run under DeskService.lock; this module never owns a network client.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

STAGES = [{'id': key, 'label': label} for key, label in (
    ('new', '新咨询'), ('interested', '有意向'), ('negotiating', '洽谈中'),
    ('won', '已成交'), ('closed', '已结束'))]
CUSTOMER_DEFAULT = {
    'profile': '', 'tags': [], 'notes': '', 'stage': 'new', 'summary': '',
    'summary_source': '', 'summary_updated_at': '', 'summary_through_message_id': '',
    'summary_stale': False, 'evidence': [], 'recent_messages': [],
    'handoff': {'reason': '', 'at': '', 'context': '', 'status': '', 'accepted_at': '', 'resolved_at': ''},
    'follow_up': {'at': '', 'note': '', 'done': False}, 'updated_at': '',
}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value, name, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f'{name}必须是{1 if required else 0}至{limit}字文本')
    return value.strip()


def tags(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError('标签最多20项')
    return list(dict.fromkeys(bounded_text(x, '标签', 40, True) for x in value))


def terms(text):
    result = set()
    for word in re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', text.casefold()):
        result.add(word)
        if len(word) > 1 and not word.isascii():
            result.update(word[i:i + 2] for i in range(len(word) - 1))
    return result


def matching_excerpt(content, query_terms, limit=2500):
    """Return a verbatim window near matched terms instead of an unrelated prefix."""
    if len(content) <= limit:
        return content
    folded = content.casefold()
    matched = [term for term in sorted(query_terms, key=lambda t: (-len(t), t))[:128] if term in folded]
    starts = {0}
    for term in matched:
        starts.add(max(0, folded.find(term) - 300))
    start = max(starts, key=lambda offset: (
        sum(len(term) for term in matched if term in content[offset:offset + limit].casefold()), -offset))
    return content[start:start + limit]


class WorkspaceStore:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS workspace_items (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, resource TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS workspace_mode_resource
                ON workspace_items(mode,resource,updated_at);
            CREATE TABLE IF NOT EXISTS customer_workspace (
                conversation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')

    def items(self, mode, resource):
        return [json.loads(r['payload']) for r in self.db.execute(
            'SELECT payload FROM workspace_items WHERE mode=? AND resource=? ORDER BY updated_at DESC,id',
            (mode, resource))]

    def customer(self, cid):
        row = self.db.execute('SELECT payload FROM customer_workspace WHERE conversation_id=?', (cid,)).fetchone()
        return dict(deepcopy(CUSTOMER_DEFAULT), **(json.loads(row['payload']) if row else {}))

    def save_customer(self, cid, customer):
        customer = dict(customer, updated_at=timestamp())
        self.db.execute('INSERT OR REPLACE INTO customer_workspace VALUES(?,?)',
                        (cid, json.dumps(customer, ensure_ascii=False)))
        return customer

    def update_customer(self, cid, values):
        if not isinstance(values, dict):
            raise ValueError('客户资料必须是对象')
        customer = self.customer(cid)
        for field, limit in (('profile', 4000), ('notes', 8000)):
            if field in values:
                customer[field] = bounded_text(values[field], field, limit)
        if 'tags' in values:
            customer['tags'] = tags(values['tags'])
        if 'stage' in values:
            if not isinstance(values['stage'], str) or values['stage'] not in {s['id'] for s in STAGES}:
                raise ValueError('客户阶段无效')
            customer['stage'] = values['stage']
        if 'follow_up' in values:
            value = values['follow_up']
            if not isinstance(value, dict):
                raise ValueError('跟进计划必须是对象')
            follow = dict(customer['follow_up'])
            if 'at' in value:
                at = bounded_text(value['at'], '跟进时间', 50)
                if at:
                    try:
                        date = datetime.fromisoformat(at.replace('Z', '+00:00'))
                        if date.tzinfo is None:
                            raise ValueError()
                        at = date.astimezone(timezone.utc).isoformat()
                    except (ValueError, OverflowError):
                        raise ValueError('跟进时间必须包含时区') from None
                follow['at'] = at
            if 'note' in value:
                follow['note'] = bounded_text(value['note'], '跟进备注', 2000)
            if 'done' in value:
                if not isinstance(value['done'], bool):
                    raise ValueError('跟进完成状态必须是布尔值')
                follow['done'] = value['done']
            customer['follow_up'] = follow
        return self.save_customer(cid, customer)

    def record_handoff(self, cid, reason):
        customer = self.customer(cid)
        customer['handoff'] = dict(customer['handoff'], reason=bounded_text(reason, '交接原因', 1000),
                                   at=timestamp(), status='waiting', accepted_at='', resolved_at='')
        return self.save_customer(cid, customer)

    def accept_handoff(self, cid):
        customer = self.customer(cid)
        handoff = customer['handoff']
        if handoff.get('status') == 'accepted':
            return customer
        customer['handoff'] = dict(handoff, reason=handoff.get('reason') or '操作员主动接管',
                                  at=handoff.get('at') or timestamp(), status='accepted', accepted_at=timestamp(), resolved_at='')
        return self.save_customer(cid, customer)

    def resolve_handoff(self, cid):
        customer = self.customer(cid)
        if not customer['handoff'].get('reason'):
            return customer
        customer['handoff'] = dict(customer['handoff'], status='resolved', resolved_at=timestamp())
        return self.save_customer(cid, customer)

    def refresh_memory(self, cid):
        rows = [dict(r) for r in self.db.execute(
            "SELECT id,text FROM messages WHERE conversation_id=? AND role='user' AND id NOT IN (SELECT message_id FROM message_media) ORDER BY created_at DESC,rowid DESC LIMIT 5", (cid,))][::-1]
        if not rows:
            return
        customer = self.customer(cid)
        evidence = [{'message_id': r['id'], 'quote': r['text'][:400]} for r in rows]
        customer['recent_messages'] = evidence
        context = '\n'.join('• ' + e['quote'] for e in evidence)
        customer['handoff'] = dict(customer['handoff'], context=context)
        if customer['summary_source'] in {'model', 'agent'}:
            customer['summary_stale'] = customer['summary_through_message_id'] != rows[-1]['id']
        else:
            customer.update(summary='客户最近原文摘录（未使用AI）：\n' + context,
                            summary_source='local', evidence=evidence, summary_stale=False,
                            summary_updated_at=timestamp(), summary_through_message_id=rows[-1]['id'])
        self.save_customer(cid, customer)

    def action(self, mode, resource, body, asset_get=None):
        if not isinstance(resource, str) or resource not in {'knowledge', 'replies', 'materials'} or not isinstance(body, dict):
            raise ValueError('工作区资源或请求无效')
        action = body.get('action', 'save')
        if action == 'search':
            if resource != 'knowledge':
                raise ValueError('仅知识库支持检索测试')
            return {'documents': self.search(mode, bounded_text(body.get('query', ''), '检索问题', 2000, True))}
        if not isinstance(action, str) or action not in {'save', 'delete'}:
            raise ValueError('未知工作区操作')
        item_id = bounded_text(body.get('id', ''), '条目ID', 64)
        previous = None
        if item_id:
            row = self.db.execute('SELECT payload FROM workspace_items WHERE id=? AND mode=? AND resource=?',
                                  (item_id, mode, resource)).fetchone()
            if not row:
                raise ValueError('条目不存在或不属于当前模式')
            previous = json.loads(row['payload'])
        if action == 'delete':
            if not item_id:
                raise ValueError('删除需要条目ID')
            self.db.execute('DELETE FROM workspace_items WHERE id=? AND mode=? AND resource=?', (item_id, mode, resource))
            return None
        if not item_id:
            count = self.db.execute('SELECT COUNT(*) FROM workspace_items WHERE mode=? AND resource=?', (mode, resource)).fetchone()[0]
            if count >= 2000:
                raise ValueError('单类条目已达2000条，请先整理旧资料')
        candidate = dict(previous or {}, **body)
        item = {'id': item_id or uuid4().hex,
                'title': bounded_text(candidate.get('title', ''), '标题', 120, True),
                'content': bounded_text(candidate.get('content', ''), '内容', 20000, resource != 'materials'),
                'tags': tags(candidate.get('tags', [])), 'updated_at': timestamp()}
        if resource == 'knowledge':
            if not isinstance(candidate.get('status', 'draft'), str) or candidate.get('status', 'draft') not in {'draft', 'published'}:
                raise ValueError('知识状态只能是草稿或已发布')
            if not isinstance(candidate.get('audience', 'public'), str) or candidate.get('audience', 'public') not in {'public', 'internal'}:
                raise ValueError('资料可见范围无效')
            item.update(status=candidate.get('status', 'draft'), audience=candidate.get('audience', 'public'),
                        version=(previous or {}).get('version', 0) + 1,
                        source=bounded_text(candidate.get('source', ''), '资料来源', 500))
        if resource == 'materials':
            kind = candidate.get('kind', 'link')
            if not isinstance(kind, str) or kind not in {'link', 'image', 'file'}:
                raise ValueError('素材类型无效')
            asset_id = bounded_text(candidate.get('asset_id', ''), '附件ID', 64)
            url = bounded_text(candidate.get('url', ''), '素材链接', 2000)
            item.update(kind=kind, asset_id=asset_id, url=url)
            if asset_id:
                if asset_get is None:
                    raise ValueError('附件服务未就绪')
                asset = asset_get(asset_id, mode)
                item.update(name=asset.get('name', ''), size=asset.get('size', 0),
                            url=asset.get('url', ''), kind=asset.get('kind', kind))
            elif url:
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError()
                    parsed.port
                except ValueError:
                    raise ValueError('素材链接必须是无账号密码的HTTP(S)地址') from None
            elif not item['content']:
                raise ValueError('请填写素材说明、链接或选择附件')
        self.db.execute('INSERT OR REPLACE INTO workspace_items VALUES(?,?,?,?,?)',
                        (item['id'], mode, resource, json.dumps(item, ensure_ascii=False), item['updated_at']))
        return None

    def search(self, mode, query, limit=6):
        query_terms = terms(query)
        if not query_terms:
            return []
        matches = []
        for item in self.items(mode, 'knowledge'):
            if item['status'] != 'published' or item.get('audience', 'public') != 'public':
                continue
            title = terms(item['title'] + ' ' + ' '.join(item['tags']))
            content = terms(item['content'])
            score = len(query_terms & content) + 3 * len(query_terms & title)
            if score:
                matches.append({'id': item['id'], 'title': item['title'], 'text': matching_excerpt(item['content'], query_terms),
                                'source': item['source'] or item['title'], 'version': item.get('version', 1), 'score': score})
        return sorted(matches, key=lambda x: (-x['score'], x['id']))[:limit]

    def overview(self, mode, conversations):
        current = datetime.now(timezone.utc)
        due = 0
        for row in conversations:
            follow = self.customer(row['id'])['follow_up']
            if follow['at'] and not follow['done'] and datetime.fromisoformat(follow['at']) <= current:
                due += 1
        return {'total': len(conversations), 'waiting': sum(c['status'] == 'waiting' for c in conversations),
                'follow_up_due': due,
                'knowledge_published': sum(k['status'] == 'published' for k in self.items(mode, 'knowledge'))}
