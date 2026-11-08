"""Bind a single-account data directory before any inbox or send starts."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path


class AccountMismatch(RuntimeError):
    pass


def bind_account(data_dir, uid):
    uid = str(uid)
    if not uid.isdigit() or int(uid) <= 0:
        raise AccountMismatch('无法验证登录账号，请重新登录')
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'desk.sqlite3', timeout=20)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS service_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        row = db.execute("SELECT value FROM service_metadata WHERE key='account_uid'").fetchone()
        if row:
            owners = {row[0]}
        else:
            # Adopt existing installations only when their persisted identity agrees.
            owners = set()
            routes = root / 'routes.json'
            if routes.exists():
                owner = json.loads(routes.read_text(encoding='utf-8')).get('uid')
                if owner:
                    owners.add(str(owner))
            recovery = root / 'recovery-window.json'
            if recovery.exists():
                owners.update(json.loads(recovery.read_text(encoding='utf-8')).keys())
            has_conversations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
            if not owners and has_conversations and db.execute("SELECT 1 FROM conversations WHERE mode='live' LIMIT 1").fetchone():
                raise AccountMismatch('账号归属无法确认：现有聊天记录缺少账号凭据，请先恢复该目录的账号记录')
        if owners and owners != {uid}:
            raise AccountMismatch('账号不匹配：此数据目录属于其他抖音账号，请登录原账号；其他账号需使用独立工作目录')
        db.execute("INSERT OR IGNORE INTO service_metadata VALUES('account_uid',?)", (uid,))
