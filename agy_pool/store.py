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
from urllib.parse import quote


SCHEMA_VERSION = 2


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
_ANSI_RE = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_CONVERSATION_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', re.ASCII)


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


def sanitize_title(text: str | None) -> str | None:
    """Strip ANSI/control chars from a native title and reject sensitive data."""
    if not isinstance(text, str):
        return None
    cleaned = _ANSI_RE.sub('', text)
    cleaned = _CTRL_RE.sub(' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        return None
    guard(cleaned)
    return cleaned[:200]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'posix':
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def write_text_atomic(path: Path, text: str) -> None:
    _ensure_private_dir(path.parent)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            if os.name == 'posix':
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


_SCHEMA_V1 = (
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

_SEGMENTS_V2_DDL = '''CREATE TABLE {table_name} (
    segment_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id),
    seq INTEGER NOT NULL,
    account_id TEXT REFERENCES accounts(account_id),
    parent_segment_id TEXT,
    conversation_id TEXT,
    transcript_path TEXT,
    handoff_in_path TEXT,
    handoff_out_path TEXT,
    pid INTEGER,
    pid_create_time INTEGER,
    status TEXT NOT NULL,
    observed_email_hint TEXT,
    observed_email_hash TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    last_heartbeat_at TEXT,
    transcript_ref TEXT,
    segment_origin TEXT NOT NULL DEFAULT 'legacy_manual',
    launch_id TEXT,
    intent_id TEXT,
    account_binding_state TEXT NOT NULL DEFAULT 'verified',
    identity_revision INTEGER,
    CHECK (account_binding_state IN ('unverified', 'verified')),
    CHECK (account_binding_state != 'verified' OR account_id IS NOT NULL),
    CHECK (account_id IS NOT NULL OR (segment_origin = 'm4_native' AND seq = 1 AND account_binding_state = 'unverified'))
)'''

_M4_TABLES = (
    '''CREATE TABLE IF NOT EXISTS conversation_claims (
        native_namespace TEXT NOT NULL,
        native_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        segment_id TEXT NOT NULL UNIQUE REFERENCES segments(segment_id),
        claim_kind TEXT NOT NULL,
        first_user_ref TEXT,
        claimed_at TEXT NOT NULL,
        PRIMARY KEY (native_namespace, native_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS switch_intents (
        intent_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        source_segment_id TEXT NOT NULL REFERENCES segments(segment_id),
        source_launch_id TEXT NOT NULL,
        terminal_binding TEXT NOT NULL,
        source_account_id TEXT NOT NULL REFERENCES accounts(account_id),
        target_account_id TEXT NOT NULL REFERENCES accounts(account_id),
        confirmed_at TEXT NOT NULL,
        state TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        blocked_phase TEXT,
        error_code TEXT,
        packet_id TEXT,
        consumed_segment_id TEXT REFERENCES segments(segment_id)
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS idx_switch_intents_active_source
       ON switch_intents(source_segment_id)
       WHERE state NOT IN ('consumed', 'cancelled')''',
    '''CREATE TABLE IF NOT EXISTS launch_attempts (
        attempt_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES switch_intents(intent_id),
        attempt_no INTEGER NOT NULL,
        launch_id TEXT NOT NULL UNIQUE,
        terminal_binding TEXT NOT NULL,
        packet_digest TEXT NOT NULL,
        verified_probe_id TEXT,
        auth_epoch_ref TEXT,
        created_at TEXT NOT NULL,
        armed_at TEXT,
        authorized_auth_observed_at TEXT,
        pid INTEGER,
        pid_create_time INTEGER,
        native_new_id TEXT,
        native_namespace TEXT,
        stage TEXT NOT NULL,
        ended_at TEXT,
        reason TEXT,
        UNIQUE(intent_id, attempt_no)
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS idx_launch_attempts_pending
       ON launch_attempts(intent_id)
       WHERE stage NOT IN ('failed_before_spawn', 'aborted_before_spawn')''',
    '''CREATE TABLE IF NOT EXISTS handoff_packets (
        packet_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES switch_intents(intent_id),
        source_segment_id TEXT NOT NULL REFERENCES segments(segment_id),
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        estimated_tokens INTEGER NOT NULL,
        quality TEXT NOT NULL CHECK (quality IN ('complete', 'partial')),
        evidence_flags TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS account_quota_samples (
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        identity_revision INTEGER NOT NULL,
        bucket TEXT NOT NULL,
        remaining_percent REAL NOT NULL,
        sampled_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        reset_at TEXT,
        window_key TEXT,
        source_kind TEXT NOT NULL,
        source_launch_id TEXT,
        PRIMARY KEY (account_id, bucket)
    )''',
    '''CREATE TABLE IF NOT EXISTS alerts (
        thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        account_id TEXT NOT NULL REFERENCES accounts(account_id),
        bucket TEXT NOT NULL,
        window_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        claim_state TEXT NOT NULL,
        PRIMARY KEY (thread_id, account_id, bucket, window_key)
    )''',
    '''CREATE TABLE IF NOT EXISTS delivery_receipts (
        attempt_id TEXT PRIMARY KEY REFERENCES launch_attempts(attempt_id),
        segment_id TEXT NOT NULL REFERENCES segments(segment_id),
        packet_digest TEXT NOT NULL,
        delivery_state TEXT NOT NULL,
        read_ref TEXT,
        observed_at TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS auth_observations (
        probe_id TEXT PRIMARY KEY,
        challenge_ref TEXT NOT NULL,
        account_id TEXT REFERENCES accounts(account_id),
        identity_hash TEXT NOT NULL,
        identity_revision INTEGER,
        auth_epoch_ref TEXT,
        source TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0
    )''',
    '''CREATE TABLE IF NOT EXISTS protocol_events (
        scope TEXT NOT NULL,
        producer_instance_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        result_json TEXT,
        received_at TEXT NOT NULL,
        PRIMARY KEY (scope, producer_instance_id, seq)
    )''',
    '''CREATE TABLE IF NOT EXISTS legacy_quarantine (
        segment_id TEXT PRIMARY KEY,
        conversation_id TEXT,
        reason TEXT NOT NULL,
        quarantined_at TEXT NOT NULL
    )''',
)

_SCHEMA_V2 = (
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY, label TEXT, email_hint TEXT, email_hash TEXT UNIQUE,
        status TEXT NOT NULL, gemini_5h_remaining REAL, gemini_5h_reset_at TEXT,
        gemini_7d_remaining REAL, quota_updated_at TEXT, cooldown_until TEXT,
        identity_revision INTEGER NOT NULL DEFAULT 1)''',
    '''CREATE TABLE IF NOT EXISTS threads (
        thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL,
        anchor_path TEXT NOT NULL, status TEXT NOT NULL, current_segment_id TEXT,
        segment_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'legacy_manual',
        title_state TEXT NOT NULL DEFAULT 'legacy_manual',
        title_source_segment_id TEXT,
        title_observed_at TEXT)''',
    _SEGMENTS_V2_DDL.format(table_name='segments'),
    '''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event_type TEXT NOT NULL,
        thread_id TEXT, segment_id TEXT, account_id TEXT, detail_json TEXT)''',
    'CREATE INDEX IF NOT EXISTS idx_threads_cwd_status ON threads(cwd,status)',
    'CREATE INDEX IF NOT EXISTS idx_segments_thread_seq ON segments(thread_id,seq)',
    'CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status)',
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_m4_thread_seq ON segments(thread_id,seq) WHERE segment_origin='m4_native'",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_m4_intent ON segments(intent_id) WHERE intent_id IS NOT NULL',
    *_M4_TABLES,
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

    @property
    def db_id(self) -> str:
        return hashlib.sha256(os.path.normcase(str(self.db)).encode('utf-8')).hexdigest()[:24]

    def artifact(self, path: Path | str) -> Path:
        rel = Path(path)
        target = (self.home / rel).resolve()
        if rel.is_absolute() or not target.is_relative_to(self.home.resolve()):
            raise ValueError('无效的工作池相对路径。')
        return target

    def _connect(self, *, readonly: bool = False):
        if readonly:
            if not self.db.is_file():
                raise StoreError('状态库尚未初始化（只读查询不会自动创建库）。')
            uri = f'file:{quote(self.db.as_posix())}?mode=ro'
            conn = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA query_only=ON')
            return conn
        conn = sqlite3.connect(self.db, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @staticmethod
    def _version(conn) -> int | None:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'").fetchone():
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                raise StoreError('数据库不是 agy-pool 格式，请检查 AGY_POOL_DB。')
            return None
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row or str(row[0]) not in {'0', '1', '2'}:
            raise StoreError('状态库的版本不受支持，请升级 agy-pool 或检查恢复备份。')
        return int(row[0])

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}

    def backup_db(self, target_path: Path) -> Path:
        """Create a consistent SQLite backup using the online backup API."""
        _ensure_private_dir(target_path.parent)
        source = self._connect(readonly=True)
        dest = sqlite3.connect(target_path)
        try:
            source.backup(dest)
            dest.commit()
        finally:
            source.close()
            dest.close()
        if os.name == 'posix':
            try:
                os.chmod(target_path, 0o600)
            except OSError:
                pass
        return target_path

    @staticmethod
    def restore_backup(backup_path: Path, dest_db: Path) -> Path:
        """Restore a verified backup into an isolated destination path (never in-place version downgrade)."""
        if not backup_path.is_file():
            raise StoreError('备份文件不存在，无法恢复。')
        if backup_path.resolve() == dest_db.resolve():
            raise StoreError('回退必须恢复到独立路径，禁止原地覆盖。')
        _ensure_private_dir(dest_db.parent)
        src = sqlite3.connect(f'file:{quote(backup_path.resolve().as_posix())}?mode=ro', uri=True)
        try:
            check = src.execute('PRAGMA integrity_check').fetchone()
            if not check or check[0] != 'ok':
                raise StoreError('备份文件完整性检查失败。')
            dst = sqlite3.connect(dest_db)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        if os.name == 'posix':
            try:
                os.chmod(dest_db, 0o600)
            except OSError:
                pass
        return dest_db

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        # Must toggle foreign_keys outside transaction before table rebuild.
        conn.execute('PRAGMA foreign_keys=OFF')
        try:
            conn.execute('BEGIN IMMEDIATE')
            if self._version(conn) != 1:
                conn.rollback()
                return

            acc_cols = self._table_columns(conn, 'accounts')
            if 'identity_revision' not in acc_cols:
                conn.execute('ALTER TABLE accounts ADD COLUMN identity_revision INTEGER NOT NULL DEFAULT 1')

            thr_cols = self._table_columns(conn, 'threads')
            if 'origin' not in thr_cols:
                conn.execute("ALTER TABLE threads ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy_manual'")
            if 'title_state' not in thr_cols:
                conn.execute("ALTER TABLE threads ADD COLUMN title_state TEXT NOT NULL DEFAULT 'legacy_manual'")
            if 'title_source_segment_id' not in thr_cols:
                conn.execute('ALTER TABLE threads ADD COLUMN title_source_segment_id TEXT')
            if 'title_observed_at' not in thr_cols:
                conn.execute('ALTER TABLE threads ADD COLUMN title_observed_at TEXT')

            # Rebuild segments to relax account_id NOT NULL strictly for unverified M4 #1.
            conn.execute(_SEGMENTS_V2_DDL.format(table_name='segments_v2_new'))
            conn.execute('''INSERT INTO segments_v2_new(
                segment_id, thread_id, seq, account_id, parent_segment_id, conversation_id,
                transcript_path, handoff_in_path, handoff_out_path, pid, pid_create_time,
                status, observed_email_hint, observed_email_hash, started_at, ended_at,
                last_heartbeat_at, transcript_ref, segment_origin, launch_id, intent_id,
                account_binding_state, identity_revision
            ) SELECT
                segment_id, thread_id, seq, account_id, parent_segment_id, conversation_id,
                transcript_path, handoff_in_path, handoff_out_path, pid, pid_create_time,
                status, observed_email_hint, observed_email_hash, started_at, ended_at,
                last_heartbeat_at, NULL, 'legacy_manual', NULL, NULL, 'verified', 1
            FROM segments''')
            conn.execute('DROP TABLE segments')
            conn.execute('ALTER TABLE segments_v2_new RENAME TO segments')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_segments_thread_seq ON segments(thread_id,seq)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status)')
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_m4_thread_seq ON segments(thread_id,seq) WHERE segment_origin='m4_native'")
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_m4_intent ON segments(intent_id) WHERE intent_id IS NOT NULL')

            for sql in _M4_TABLES:
                conn.execute(sql)

            # Backfill reliable, non-conflicting legacy conversation IDs; quarantine duplicates/invalid.
            rows = conn.execute(
                'SELECT segment_id, thread_id, conversation_id, started_at FROM segments WHERE conversation_id IS NOT NULL'
            ).fetchall()
            counts: dict[str, int] = {}
            for row in rows:
                cid = row['conversation_id']
                if isinstance(cid, str) and cid:
                    counts[cid] = counts.get(cid, 0) + 1
            now = utc_now()
            for row in rows:
                cid = row['conversation_id']
                if not isinstance(cid, str) or not cid:
                    continue
                if not _CONVERSATION_RE.fullmatch(cid):
                    conn.execute(
                        'INSERT OR IGNORE INTO legacy_quarantine(segment_id,conversation_id,reason,quarantined_at) VALUES (?,?,?,?)',
                        (row['segment_id'], cid[:128], 'invalid_conversation_id', now),
                    )
                elif counts.get(cid, 0) > 1:
                    conn.execute(
                        'INSERT OR IGNORE INTO legacy_quarantine(segment_id,conversation_id,reason,quarantined_at) VALUES (?,?,?,?)',
                        (row['segment_id'], cid, 'duplicate_conversation_id', now),
                    )
                else:
                    conn.execute(
                        '''INSERT OR IGNORE INTO conversation_claims(
                               native_namespace, native_id, thread_id, segment_id,
                               claim_kind, first_user_ref, claimed_at
                           ) VALUES ('legacy', ?, ?, ?, 'legacy_migration', NULL, ?)''',
                        (cid, row['thread_id'], row['segment_id'], row['started_at'] or now),
                    )

            fk_errors = conn.execute('PRAGMA foreign_key_check').fetchall()
            if fk_errors:
                raise StoreError('迁移后外键完整性检查失败，已回滚。')
            integrity = conn.execute('PRAGMA integrity_check').fetchone()
            if not integrity or integrity[0] != 'ok':
                raise StoreError('迁移后数据库完整性检查失败，已回滚。')

            Session(conn).set_meta('schema_version', str(SCHEMA_VERSION))
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.execute('PRAGMA foreign_keys=ON')

    def init_db(self) -> None:
        _ensure_private_dir(self.db.parent)
        conn = self._connect()
        try:
            version = self._version(conn)
            if version != SCHEMA_VERSION or conn.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                conn.execute('PRAGMA journal_mode=WAL')

            if version is None:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    if self._version(conn) is None:
                        for sql in _SCHEMA_V2:
                            conn.execute(sql)
                        for key, value in (('active_machine_account', ''), ('next_thread_seq', '1')):
                            conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (key, value))
                        Session(conn).set_meta('schema_version', str(SCHEMA_VERSION))
                    conn.commit()
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
            elif version == 0:
                self.backup_db(self.db.with_name(self.db.name + '.v0.bak'))
                conn.execute('BEGIN IMMEDIATE')
                try:
                    if self._version(conn) == 0:
                        for sql in _SCHEMA_V1:
                            conn.execute(sql)
                        for key, value in (('active_machine_account', ''), ('next_thread_seq', '1')):
                            conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (key, value))
                        Session(conn).set_meta('schema_version', '1')
                    conn.commit()
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                self.backup_db(self.db.with_name(self.db.name + '.v1.bak'))
                self._migrate_v1_to_v2(conn)
            elif version == 1:
                self.backup_db(self.db.with_name(self.db.name + '.v1.bak'))
                self._migrate_v1_to_v2(conn)
        finally:
            conn.close()
        for folder in ('anchors', 'handoffs', 'probes', 'mailboxes', 'views'):
            _ensure_private_dir(self.artifact(folder))

    @contextmanager
    def transaction(self, *, write: bool = True):
        conn = self._connect(readonly=not write)
        try:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            version = self._version(conn)
            if write and version != SCHEMA_VERSION:
                raise StoreError('请先初始化状态库。')
            if not write and version not in (1, SCHEMA_VERSION):
                raise StoreError('请先初始化状态库。')
            yield Session(conn)
            if write:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def readonly_transaction(self):
        """Strict read-only snapshot transaction (mode=ro + query_only). Never creates or modifies DB."""
        conn = self._connect(readonly=True)
        try:
            conn.execute('BEGIN')
            version = self._version(conn)
            if version not in (1, SCHEMA_VERSION):
                raise StoreError('状态库尚未初始化或版本不受支持。')
            yield Session(conn), version
            conn.rollback()
        finally:
            conn.close()

    def get_account(self, account_id: str):
        with self.transaction(write=False) as tx:
            row = tx.execute("SELECT * FROM accounts WHERE account_id=? AND status<>'removed'", (account_id,)).fetchone()
            return dict(row) if row else None

    def list_threads(self, include_closed: bool = False) -> list[dict]:
        with self.readonly_transaction() as (tx, version):
            return self._threads(tx, include_closed, version=version)

    @staticmethod
    def _threads(tx: Session, include_closed: bool, *, version: int = SCHEMA_VERSION) -> list[dict]:
        if version >= 2:
            sql = '''SELECT t.thread_id,t.title,t.cwd,t.status,t.current_segment_id,
                     t.origin,t.title_state,
                     s.seq AS current_seq,s.account_id AS current_account_id,
                     s.status AS current_segment_status,
                     s.account_binding_state AS current_account_binding_state
                     FROM threads t LEFT JOIN segments s ON t.current_segment_id=s.segment_id'''
        else:
            sql = '''SELECT t.thread_id,t.title,t.cwd,t.status,t.current_segment_id,
                     s.seq AS current_seq,s.account_id AS current_account_id,
                     s.status AS current_segment_status
                     FROM threads t LEFT JOIN segments s ON t.current_segment_id=s.segment_id'''
        return [dict(row) for row in tx.execute(sql + ('' if include_closed else " WHERE t.status='active'") + ' ORDER BY t.rowid')]

    def record_event(self, event_type: str, **kwargs):
        with self.transaction() as tx:
            tx.record_event(event_type, **kwargs)

    def export_snapshot(self, include_closed: bool = False) -> dict:
        with self.readonly_transaction() as (tx, version):
            return {
                'active_machine_account': tx.get_meta('active_machine_account', '') or None,
                'accounts': [dict(row) for row in tx.execute('''SELECT account_id,label,email_hint,status,
                    gemini_5h_remaining,gemini_5h_reset_at,gemini_7d_remaining,quota_updated_at
                    FROM accounts WHERE status<>'removed' ORDER BY account_id''')],
                'threads': self._threads(tx, include_closed, version=version),
            }

    def thread_status_readonly(self, thread_id: str) -> dict | None:
        """Read-only status projection compatible with both v1 and v2 schemas."""
        with self.readonly_transaction() as (tx, version):
            thread_row = tx.execute('SELECT * FROM threads WHERE thread_id=?', (thread_id,)).fetchone()
            if not thread_row:
                return None
            thread = dict(thread_row)
            seg_row = tx.execute('SELECT * FROM segments WHERE segment_id=?', (thread['current_segment_id'],)).fetchone()
            segment = dict(seg_row) if seg_row else None
            accounts = [dict(row) for row in tx.execute('''SELECT account_id,label,email_hint,status,
                gemini_5h_remaining,gemini_5h_reset_at,gemini_7d_remaining,quota_updated_at
                FROM accounts WHERE status<>'removed' ORDER BY account_id''')]
            active_machine = tx.get_meta('active_machine_account', '') or None

            pending_intent = None
            handoff_state = 'idle'
            delivery_state = None
            if version >= 2 and segment:
                intent_row = tx.execute(
                    '''SELECT intent_id,target_account_id,state,blocked_phase,error_code,packet_id
                       FROM switch_intents WHERE thread_id=? ORDER BY rowid DESC LIMIT 1''',
                    (thread_id,),
                ).fetchone()
                if intent_row:
                    handoff_state = intent_row['state']
                    if intent_row['state'] not in ('consumed', 'cancelled'):
                        pending_intent = {
                            'intent_id': intent_row['intent_id'],
                            'target_account_id': intent_row['target_account_id'],
                            'state': intent_row['state'],
                            'blocked_phase': intent_row['blocked_phase'],
                            'error_code': intent_row['error_code'],
                            'packet_available': bool(intent_row['packet_id']),
                        }
                receipt_row = tx.execute(
                    '''SELECT delivery_state,observed_at FROM delivery_receipts
                       WHERE segment_id=? ORDER BY rowid DESC LIMIT 1''',
                    (segment['segment_id'],),
                ).fetchone()
                if receipt_row:
                    delivery_state = receipt_row['delivery_state']

            return {
                'schema_version': version,
                'active_machine_account': active_machine,
                'thread': thread,
                'current_segment': segment,
                'accounts': accounts,
                'handoff_state': handoff_state,
                'pending_intent': pending_intent,
                'delivery_state': delivery_state,
            }
