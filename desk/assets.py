"""Local immutable attachments and explicitly requested manual delivery."""
import base64
import binascii
import json
from pathlib import Path
import re
from uuid import uuid4

MAX_ASSET_SIZE = 10 * 1024 * 1024
TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.gif': 'image/gif', '.webp': 'image/webp', '.pdf': 'application/pdf',
         '.txt': 'text/plain', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.zip': 'application/zip'}


class AssetStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / 'attachments'
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, mode, name, data):
        if mode not in {'live', 'demo'}:
            raise ValueError('无效模式')
        if (not isinstance(name, str) or not name.strip() or len(name) > 120
                or re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name.endswith((' ', '.'))):
            raise ValueError('文件名无效，请使用不含路径的文件名')
        ext = Path(name).suffix.lower()
        if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
            raise ValueError('请更换文件名')
        if ext not in TYPES:
            raise ValueError('支持 PNG/JPG/GIF/WebP、PDF/TXT/DOCX/XLSX/ZIP 文件')
        if isinstance(data, str):
            try:
                data = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError('文件编码无效') from None
        if not isinstance(data, bytes) or not data or len(data) > MAX_ASSET_SIZE:
            raise ValueError('文件不能为空或超过 10 MB')
        kind = 'image' if TYPES[ext].startswith('image/') else 'file'
        signatures = {'.png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                      '.jpg': data.startswith(b'\xff\xd8\xff'), '.jpeg': data.startswith(b'\xff\xd8\xff'),
                      '.gif': data[:6] in (b'GIF87a', b'GIF89a'),
                      '.webp': data[:4] == b'RIFF' and data[8:12] == b'WEBP'}
        if kind == 'image' and not signatures[ext]:
            raise ValueError('图片内容与文件类型不符')
        aid = uuid4().hex
        folder = self.root / aid
        folder.mkdir()
        (folder / name).write_bytes(data)
        item = {'id': aid, 'mode': mode, 'name': name, 'size': len(data),
                'kind': kind, 'mime': TYPES[ext], 'url': '/api/assets/' + aid}
        (folder / 'metadata.json').write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
        return {k: v for k, v in item.items() if k != 'mode'}

    def get(self, aid, mode):
        if not isinstance(aid, str) or not re.fullmatch(r'[a-f0-9]{32}', aid):
            raise ValueError('附件不存在')
        try:
            item = json.loads((self.root / aid / 'metadata.json').read_text(encoding='utf-8'))
            if item['mode'] != mode:
                raise ValueError('附件不属于当前模式')
            path = self.root / aid / item['name']
            if path.parent != self.root / aid or not path.is_file():
                raise ValueError('附件文件不存在')
            return {k: v for k, v in item.items() if k != 'mode'}
        except (OSError, KeyError, json.JSONDecodeError):
            raise ValueError('附件不存在或已损坏') from None

    def path(self, aid, mode):
        return self.root / aid / self.get(aid, mode)['name']

    def list(self, mode):
        result = []
        for file in self.root.glob('*/metadata.json'):
            try:
                result.append(self.get(file.parent.name, mode))
            except ValueError:
                continue
        return result


def send_asset(desk, cid, aid):
    """Uses the same conversation reservation as text; uncertain sends never retry."""
    from .service import now
    with desk._send_lock(cid):
        with desk.lock:
            row = desk._get(cid)
            asset = desk.assets.get(aid, row['mode'])
            path = desk.assets.path(aid, row['mode'])
            text = ('[图片] ' if asset['kind'] == 'image' else '[文件] ') + asset['name']
            desk._cancel(cid, 'human')
            desk.workspace.accept_handoff(cid)
            mid = desk._message(cid, 'human', text, 'pending')
            desk.db.execute('INSERT INTO message_media VALUES(?,?,?)', (mid, asset['kind'], asset['url']))
            if row['mode'] == 'live' and desk.connection['status'] != 'connected':
                desk._failure(cid, mid, '连接已断开，附件未发送')
                raise ValueError('连接已断开，附件未发送')
            desk.db.execute("UPDATE messages SET status='sending' WHERE id=?", (mid,))
            desk._register_outbound(cid, mid)
            desk.active_sends += 1
        success = False
        try:
            if row['mode'] == 'live':
                kwargs = {'client_message_id': mid} if getattr(desk.transport, 'supports_client_message_id', False) else {}
                desk.transport.send_asset(row['raw_id'], path, asset['kind'], **kwargs)
            success = True
        except Exception:
            pass
        with desk.lock:
            try:
                if success:
                    desk.db.execute("UPDATE messages SET status='sent',error=NULL WHERE id=?", (mid,))
                    desk.db.execute('UPDATE conversations SET preview=?,updated_at=? WHERE id=?', (text, now(), cid))
                else:
                    desk._failure(cid, mid, '附件发送失败，结果可能不确定；请到抖音核对，未自动重试')
            finally:
                desk.active_sends -= 1
                desk._close_db_if_idle()
            if not success:
                raise ValueError('附件发送失败，请到抖音核对后再决定是否重发')
            if desk.closed or desk.settings['mode'] != row['mode']:
                return {'id': mid, 'status': 'sent'}
            return desk.conversation(cid)
