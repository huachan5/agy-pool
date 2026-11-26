from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class StoreError(Exception):
    pass


class SensitiveData(ValueError):
    pass


_SENSITIVE = re.compile(
    r'(?i)(?:\b(?:set-cookie|cookie|authorization|password|token|secret|refresh_token|access_token|oauth_token|'
    r'[\w-]*api[_-]?key|x-(?:antigravity|codeium)-csrf-token)["\']?\s*[:=]\s*["\']?[A-Za-z0-9._~+/=-]{3,}[^\r\n"\']*["\']?'
    r'|\bBearer\s+[^\s,;]+|\bAIza[\w-]{20,}|\bsk-[\w-]{20,}|\bgh[pousr]_[\w-]{20,}'
    r'|\bya29\.[\w.-]+|\beyJ[\w-]+\.[\w-]+\.[\w-]+)'
)
_EMAIL = re.compile(r'(?<![\w.*+-])[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}\b')
_KEYS = {'token', 'cookie', 'secret', 'api_key', 'password', 'email', 'authorization',
         'access_token', 'refresh_token', 'oauth_token'}


def redact(text: str) -> str:
    return _EMAIL.sub('[REDACTED]', _SENSITIVE.sub('[REDACTED]', text))


def guard(value) -> None:
    if isinstance(value, str):
        if redact(value) != value:
            raise SensitiveData('检测到邮箱或凭据，请删去敏感信息后重试。')
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold().replace('-', '_') in _KEYS:
                raise SensitiveData('检测到敏感字段，请删除后重试。')
            guard(key)
            guard(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            guard(item)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY, label TEXT, email_hint TEXT, email_hash TEXT UNIQUE,
        status TEXT NOT NULL, gemini_5h_remaining REAL, gemini_5h_reset_at TEXT,
        gemini_7d_remaining REAL, quota_updated_at TEXT, cooldown_until TEXT)''',
    '''CREATE TABLE IF NOT EXISTS threads (
        thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL,
        anchor_path TEXT NOT NULL, status TEXT NOT NULL, current_segment_id TEXT,
        segment_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS segments (
        segment_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        seq INTEGER NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(account_id),
        parent_segment_id TEXT, conversation_id TEXT, transcript_path TEXT, handoff_in_path TEXT,
        handoff_out_path TEXT, pid INTEGER, pid_create_time INTEGER, status TEXT NOT NULL,
        observed_email_hint TEXT, observed_email_hash TEXT, started_at TEXT NOT NULL,
        ended_at TEXT, last_heartbeat_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event_type TEXT NOT NULL,
        thread_id TEXT, segment_id TEXT, account_id TEXT, detail_json TEXT)''',
    'CREATE INDEX IF NOT EXISTS idx_threads_cwd_status ON threads(cwd,status)',
    'CREATE INDEX IF NOT EXISTS idx_segments_thread_seq ON segments(thread_id,seq)',
    'CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status)',
)


class Session:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(self, sql: str, params=()):
        guard(params)
        return self._connection.execute(sql, params)

    def record_event(self, event_type: str, *, thread_id=None, segment_id=None, account_id=None, detail=None):
        guard(detail)
        self.execute('INSERT INTO events(ts,event_type,thread_id,segment_id,account_id,detail_json) VALUES (?,?,?,?,?,?)',
                     (utc_now(), event_type, thread_id, segment_id, account_id,
                      None if detail is None else json.dumps(detail, ensure_ascii=False, allow_nan=False)))

    def get_meta(self, key: str, default=None):
        row = self.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        self.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key, value))


class PoolStore:
    def __init__(self, db: Path | str, home: Path | str | None = None):
        self.db = Path(db).expanduser().resolve()
        base = Path(home).expanduser().resolve() if home is not None else self.db.parent
        self.base_home = self.home = base
        if self.db != (base / 'pool.db').resolve():
            name = hashlib.sha256(os.path.normcase(str(self.db)).encode('utf-8')).hexdigest()[:24]
            self.home = base / 'pools' / name

    def artifact(self, path: Path | str) -> Path:
        rel = Path(path)
        target = (self.home / rel).resolve()
        if rel.is_absolute() or not target.is_relative_to(self.home.resolve()):
            raise ValueError('无效的工作池相对路径。')
        return target

    def _connect(self):
        conn = sqlite3.connect(self.db, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @staticmethod
    def _version(conn):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'").fetchone():
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                raise StoreError('数据库不是 agy-pool 格式，请检查 AGY_POOL_DB。')
            return None
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row or str(row[0]) not in {'0', '1'}:
            raise StoreError('状态库的版本不受支持，请升级 agy-pool 或检查恢复备份。')
        return int(row[0])

    def init_db(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            version = self._version(conn)
            if version != 1 or conn.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                conn.execute('PRAGMA journal_mode=WAL')
            if version != 1:
                conn.execute('BEGIN IMMEDIATE')
                version = self._version(conn)
                if version == 0:
                    # Read through a separate connection: the writer holds the migration lock.
                    source = self._connect()
                    backup = sqlite3.connect(self.db.with_name(self.db.name + '.v0.bak'))
                    try:
                        source.backup(backup)
                    finally:
                        source.close()
                        backup.close()
                if version != 1:
                    for sql in _SCHEMA:
                        conn.execute(sql)
                    for key, value in (('active_machine_account', ''), ('next_thread_seq', '1')):
                        conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (key, value))
                    Session(conn).set_meta('schema_version', '1')
                conn.commit()
        finally:
            conn.close()
        for folder in ('anchors', 'handoffs', 'probes'):
            self.artifact(folder).mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self, *, write: bool = True):
        conn = self._connect()
        try:
            if not write:
                conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if self._version(conn) != 1:
                raise StoreError('请先初始化状态库。')
            yield Session(conn)
            conn.commit()
        finally:
            conn.close()

    def get_account(self, account_id: str):
        with self.transaction(write=False) as tx:
            row = tx.execute("SELECT * FROM accounts WHERE account_id=? AND status<>'removed'", (account_id,)).fetchone()
            return dict(row) if row else None

    def list_threads(self, include_closed: bool = False) -> list[dict]:
        with self.transaction(write=False) as tx:
            return self._threads(tx, include_closed)

    @staticmethod
    def _threads(tx: Session, include_closed: bool) -> list[dict]:
        sql = '''SELECT t.thread_id,t.title,t.cwd,t.status,t.current_segment_id,
                 s.seq AS current_seq,s.account_id AS current_account_id,s.status AS current_segment_status
                 FROM threads t LEFT JOIN segments s ON t.current_segment_id=s.segment_id'''
        return [dict(row) for row in tx.execute(sql + ('' if include_closed else " WHERE t.status='active'") + ' ORDER BY t.rowid')]

    def record_event(self, event_type: str, **kwargs):
        with self.transaction() as tx:
            tx.record_event(event_type, **kwargs)

    def export_snapshot(self, include_closed: bool = False) -> dict:
        with self.transaction(write=False) as tx:
            return {'active_machine_account': tx.get_meta('active_machine_account', '') or None,
                    'accounts': [dict(row) for row in tx.execute('''SELECT account_id,label,email_hint,status,
                    gemini_5h_remaining,gemini_5h_reset_at,gemini_7d_remaining,quota_updated_at
                    FROM accounts WHERE status<>'removed' ORDER BY account_id''')],
                    'threads': self._threads(tx, include_closed)}
