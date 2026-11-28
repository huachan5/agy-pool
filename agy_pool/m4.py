from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .handoff import HandoffBuilder, NoHandoffEvidence, build_handoff_prompt
from .store import (
    PoolStore,
    SensitiveData,
    StoreError,
    guard,
    redact,
    sanitize_title,
    utc_now,
    write_text_atomic,
)


# Standardized M4 reason codes (§9.3)
REASON_IDENTITY_UNVERIFIED = 'identity_unverified'
REASON_KEY_ROUTE_UNAVAILABLE = 'key_route_unavailable'
REASON_SHARED_SESSION = 'shared_session'
REASON_QUOTA_ZERO = 'quota_zero'
REASON_QUOTA_UNKNOWN = 'quota_unknown'
REASON_AUTH_EXPIRED = 'auth_expired'
REASON_AUTH_MISMATCH = 'auth_mismatch'
REASON_CONSUMER_UNBOUND = 'consumer_unbound'
REASON_ORIGINAL_TERMINAL_UNVERIFIED = 'original_terminal_unverified'
REASON_SPAWN_UNCERTAIN = 'spawn_uncertain'
REASON_DELIVERY_UNVERIFIED = 'delivery_unverified'
REASON_NO_EVIDENCE = 'no_evidence'
REASON_PACKET_CORRUPT = 'packet_corrupt'
REASON_INTENT_CONFLICT = 'intent_conflict'
REASON_NOT_NORMAL_EXIT = 'not_normal_exit'
REASON_EVENT_CONFLICT = 'event_conflict'
REASON_INVALID_EVENT = 'invalid_event'
REASON_INVALID_STATE = 'invalid_state'

_VALID_CONVERSATION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', re.ASCII)
_VALID_NAMESPACE = re.compile(r'[a-z0-9][a-z0-9_.:-]{0,63}', re.ASCII)
_VALID_FIRST_QUERY_SOURCES = frozenset({'native_transcript', 'native_user_submit'})
_VALID_TITLE_SOURCES = frozenset({'native_title_event', 'native_session_title', 'native_custom_title'})
_VALID_READ_SOURCES = frozenset({'native_file_read', 'fixture_successful_read', 'tool_read_file_ok'})
_ANSI_STRIP_RE = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])')

DEFAULT_QUOTA_TTL_SECONDS = 300
DEFAULT_AUTH_TTL_SECONDS = 120
MAX_CLOCK_SKEW_SECONDS = 30
LOW_QUOTA_THRESHOLD = 5.0


class M4Error(Exception):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def parse_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = value[:-1] + '+00:00' if value.endswith('Z') else value
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _default_barrier_dir() -> Path:
    override = os.environ.get('AGY_POOL_BARRIER_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'agy-pool' / 'barriers'
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA')
        if local:
            return Path(local) / 'agy-pool' / 'barriers'
    return Path.home() / '.antigravity' / 'agy-pool' / 'barriers'


class SharedDomainBarrier:
    """Persistent cross-DB barrier placed in the shared auth lock directory (§9.4)."""

    def __init__(self, domain_id: str = 'default-shared-auth', barrier_dir: Path | str | None = None):
        self.domain_id = domain_id
        self.root = Path(barrier_dir).expanduser().resolve() if barrier_dir is not None else _default_barrier_dir()
        digest = hashlib.sha256(domain_id.encode('utf-8')).hexdigest()[:16]
        self.path = self.root / f'domain-{digest}.json'

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or 'db_id' not in data or 'intent_id' not in data:
                raise M4Error(REASON_SHARED_SESSION, '共享登录域屏障文件损坏，已保守阻断操作。')
            return data
        except M4Error:
            raise
        except (OSError, ValueError) as exc:
            raise M4Error(REASON_SHARED_SESSION, f'无法读取共享登录域屏障，已保守阻断：{exc}') from exc

    def assert_clear_for(self, db_id: str, intent_id: str | None = None) -> None:
        existing = self.read()
        if existing is None:
            return
        if existing.get('db_id') == db_id and intent_id is not None and existing.get('intent_id') == intent_id:
            return
        raise M4Error(
            REASON_SHARED_SESSION,
            f"共享登录域存在未结清的跨库切换屏障（阶段：{existing.get('stage', 'unknown')}），请先核对并收尾原切换意图。",
        )

    def write(self, *, db_id: str, intent_id: str, stage: str, attempt_id: str | None = None) -> None:
        payload = {
            'domain_id': self.domain_id,
            'db_id': db_id,
            'intent_id': intent_id,
            'attempt_id': attempt_id,
            'stage': stage,
            'updated_at': utc_now(),
        }
        guard(payload)
        write_text_atomic(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def clear(self, *, db_id: str, intent_id: str) -> bool:
        existing = self.read()
        if existing is None:
            return True
        if existing.get('db_id') != db_id or existing.get('intent_id') != intent_id:
            return False
        self.path.unlink(missing_ok=True)
        return True


@dataclass(frozen=True)
class ChallengeToken:
    probe_id: str
    challenge_ref: str
    issued_at: str


class M4Engine:
    """Platform-independent M4 state machine, protocol consumer, quota/alert engine, and recovery manager."""

    def __init__(
        self,
        store: PoolStore,
        *,
        barrier: SharedDomainBarrier | None = None,
        quota_ttl_seconds: int = DEFAULT_QUOTA_TTL_SECONDS,
        auth_ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
    ):
        self.store = store
        self.barrier = barrier or SharedDomainBarrier()
        self.quota_ttl_seconds = quota_ttl_seconds
        self.auth_ttl_seconds = auth_ttl_seconds

    # ------------------------------------------------------------------
    # Versioned v1 Event Ingestion (§3.2, §9.3)
    # ------------------------------------------------------------------
    def ingest_event(
        self,
        event: dict[str, Any],
        *,
        cwd: Path | str | None = None,
        expected_scope: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, dict) or event.get('v') != 1:
            raise M4Error(REASON_INVALID_EVENT, '不支持的事件协议版本。')
        event_type = event.get('event')
        producer = event.get('producer_instance_id')
        seq = event.get('seq')
        if (
            not isinstance(event_type, str)
            or not isinstance(producer, str)
            or not producer
            or type(seq) is not int
            or seq < 0
        ):
            raise M4Error(REASON_INVALID_EVENT, '事件公共头缺失或非法。')

        if event_type == 'auth_observation':
            scope = event.get('probe_id')
        else:
            scope = event.get('launch_id')
        if not isinstance(scope, str) or not scope:
            raise M4Error(REASON_INVALID_EVENT, '事件缺少有效的作用域标识（launch_id / probe_id）。')
        if expected_scope is not None and scope != expected_scope:
            raise M4Error(REASON_INVALID_EVENT, '事件作用域与当前控制器不匹配。')

        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = _iso_utc(now_dt)
        try:
            payload_hash = _canonical_hash(event)
        except (TypeError, ValueError) as exc:
            raise M4Error(REASON_INVALID_EVENT, f'事件载荷非法：{exc}') from exc

        with self.store.transaction() as tx:
            existing = tx.execute(
                'SELECT payload_hash, status, result_json FROM protocol_events WHERE scope=? AND producer_instance_id=? AND seq=?',
                (scope, producer, seq),
            ).fetchone()
            if existing:
                if existing['payload_hash'] == payload_hash:
                    cached = json.loads(existing['result_json']) if existing['result_json'] else {}
                    cached['idempotent_replay'] = True
                    return cached
                tx.record_event(
                    'protocol_event_conflict',
                    detail={'scope': scope, 'producer': producer, 'seq': seq, 'event_type': event_type},
                )
                return {'status': 'quarantined', 'reason': REASON_EVENT_CONFLICT}

            if event_type == 'conversation_evidence':
                result = self._handle_conversation_evidence(tx, event, cwd=cwd, now_iso=now_iso)
            elif event_type == 'session_identity':
                result = self._handle_session_identity(tx, event, now_dt=now_dt, now_iso=now_iso)
            elif event_type == 'quota_sample':
                result = self._handle_quota_sample(tx, event, now_dt=now_dt, now_iso=now_iso)
            elif event_type == 'auth_observation':
                result = self._handle_auth_observation(tx, event, now_dt=now_dt, now_iso=now_iso)
            elif event_type == 'new_conversation':
                result = self._handle_new_conversation(tx, event, now_dt=now_dt, now_iso=now_iso)
            elif event_type == 'packet_read':
                result = self._handle_packet_read(tx, event, now_iso=now_iso)
            else:
                raise M4Error(REASON_INVALID_EVENT, f'未知事件类型：{event_type}')

            tx.execute(
                '''INSERT INTO protocol_events(scope, producer_instance_id, seq, event_type, payload_hash, status, result_json, received_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (
                    scope,
                    producer,
                    seq,
                    event_type,
                    payload_hash,
                    result.get('status', 'ok'),
                    json.dumps(result, ensure_ascii=False, allow_nan=False),
                    now_iso,
                ),
            )
            return result

    def _validate_conv_key(self, event: dict[str, Any]) -> tuple[str, str]:
        ns = event.get('native_namespace')
        cid = event.get('conversation_id')
        if (
            not isinstance(ns, str)
            or not _VALID_NAMESPACE.fullmatch(ns)
            or not isinstance(cid, str)
            or not _VALID_CONVERSATION_ID.fullmatch(cid)
        ):
            raise M4Error(REASON_INVALID_EVENT, '缺少合法的真实对话 ID 或命名域。')
        return ns, cid

    def _handle_conversation_evidence(
        self, tx, event: dict[str, Any], *, cwd: Path | str | None, now_iso: str
    ) -> dict[str, Any]:
        ns, cid = self._validate_conv_key(event)
        kind = event.get('kind')
        source = event.get('source')
        source_ref = event.get('source_ref')
        launch_id = event['launch_id']

        if kind == 'first_user_query':
            if source not in _VALID_FIRST_QUERY_SOURCES or not isinstance(source_ref, str) or not source_ref.strip():
                return {'status': 'ignored', 'reason': 'invalid_first_query_source'}

            # Never let a handoff attempt launch be registered as a brand-new ordinary thread.
            if tx.execute('SELECT 1 FROM launch_attempts WHERE launch_id=?', (launch_id,)).fetchone():
                return {'status': 'ignored', 'reason': 'handoff_launch_reserved'}

            existing_claim = tx.execute(
                'SELECT thread_id, segment_id FROM conversation_claims WHERE native_namespace=? AND native_id=?',
                (ns, cid),
            ).fetchone()
            if existing_claim:
                return {
                    'status': 'existing',
                    'thread_id': existing_claim['thread_id'],
                    'segment_id': existing_claim['segment_id'],
                }

            seq = int(tx.get_meta('next_thread_seq', '1'))
            tid = f'T{seq}'
            sid = f'{tid}-seg1'
            anchor_rel = f'anchors/{tid}.md'
            resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd is not None else str(Path.cwd().resolve())
            guard(resolved_cwd)

            anchor_path = self.store.artifact(anchor_rel)
            if not anchor_path.exists():
                goal_excerpt = event.get('safe_goal_excerpt')
                if isinstance(goal_excerpt, str) and goal_excerpt.strip():
                    safe_goal = redact(goal_excerpt).strip()[:1200]
                    anchor_body = f'# 任务目标（自动摘录）\n\n{safe_goal}\n\n## 验收与硬约束\n\n- 待核对\n'
                else:
                    anchor_body = '# 任务目标\n\n- 待核对（M4 自动登记）\n'
                write_text_atomic(anchor_path, anchor_body)

            tx.set_meta('next_thread_seq', str(seq + 1))
            tx.execute(
                '''INSERT INTO threads(
                       thread_id, title, cwd, anchor_path, status, current_segment_id,
                       segment_count, created_at, updated_at, origin, title_state
                   ) VALUES (?, 'todo', ?, ?, 'active', ?, 1, ?, ?, 'm4_native', 'placeholder')''',
                (tid, resolved_cwd, anchor_rel, sid, now_iso, now_iso),
            )
            tx.execute(
                '''INSERT INTO segments(
                       segment_id, thread_id, seq, account_id, parent_segment_id, conversation_id,
                       status, started_at, segment_origin, launch_id, account_binding_state
                   ) VALUES (?, ?, 1, NULL, NULL, ?, 'running', ?, 'm4_native', ?, 'unverified')''',
                (sid, tid, cid, now_iso, launch_id),
            )
            first_ref = f'{source}:{source_ref.strip()}'
            guard(first_ref)
            tx.execute(
                '''INSERT INTO conversation_claims(
                       native_namespace, native_id, thread_id, segment_id,
                       claim_kind, first_user_ref, claimed_at
                   ) VALUES (?, ?, ?, ?, 'm4_first_user_query', ?, ?)''',
                (ns, cid, tid, sid, first_ref, now_iso),
            )
            tx.record_event(
                'm4_thread_registered',
                thread_id=tid,
                segment_id=sid,
                detail={'launch_id': launch_id, 'account_binding_state': 'unverified'},
            )
            return {'status': 'registered', 'thread_id': tid, 'segment_id': sid, 'account_binding_state': 'unverified'}

        if kind == 'native_title':
            if source not in _VALID_TITLE_SOURCES:
                return {'status': 'ignored', 'reason': 'non_native_title_source'}
            try:
                clean_title = sanitize_title(event.get('safe_title'))
            except SensitiveData:
                return {'status': 'rejected', 'reason': 'sensitive_title'}
            if not clean_title or clean_title.lower() == 'todo':
                return {'status': 'ignored', 'reason': 'empty_or_placeholder_title'}

            claim = tx.execute(
                'SELECT thread_id, segment_id FROM conversation_claims WHERE native_namespace=? AND native_id=?',
                (ns, cid),
            ).fetchone()
            if not claim:
                return {'status': 'ignored', 'reason': 'unclaimed_conversation'}

            seg = tx.execute(
                'SELECT seq, thread_id FROM segments WHERE segment_id=?', (claim['segment_id'],)
            ).fetchone()
            thread = tx.execute(
                'SELECT origin, title_state, title FROM threads WHERE thread_id=?', (claim['thread_id'],)
            ).fetchone()
            if not seg or not thread:
                return {'status': 'ignored', 'reason': 'missing_thread'}
            # FR-02: Only #1 native title may backfill an m4_native thread; never overwrite legacy_manual or from #2/#3.
            if seg['seq'] != 1 or thread['origin'] != 'm4_native' or thread['title_state'] == 'legacy_manual':
                return {'status': 'ignored', 'reason': 'title_locked', 'thread_id': claim['thread_id']}

            observed_at = event.get('source_observed_at') if isinstance(event.get('source_observed_at'), str) else now_iso
            tx.execute(
                '''UPDATE threads
                   SET title=?, title_state='native', title_source_segment_id=?, title_observed_at=?, updated_at=?
                   WHERE thread_id=? AND origin='m4_native' AND title_state<>'legacy_manual' ''',
                (clean_title, claim['segment_id'], observed_at, now_iso, claim['thread_id']),
            )
            tx.record_event(
                'm4_title_updated',
                thread_id=claim['thread_id'],
                segment_id=claim['segment_id'],
                detail={'title_state': 'native'},
            )
            return {'status': 'title_updated', 'thread_id': claim['thread_id'], 'title': clean_title}

        return {'status': 'ignored', 'reason': 'unsupported_evidence_kind'}

    def _handle_session_identity(self, tx, event: dict[str, Any], *, now_dt: datetime, now_iso: str) -> dict[str, Any]:
        ns, cid = self._validate_conv_key(event)
        identity_hash = event.get('identity_hash')
        observed_dt = parse_utc(event.get('source_observed_at'))
        if not isinstance(identity_hash, str) or not identity_hash or observed_dt is None:
            return {'status': 'ignored', 'reason': 'invalid_identity_payload'}
        if observed_dt > now_dt + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            return {'status': 'ignored', 'reason': 'future_timestamp'}

        account = tx.execute(
            "SELECT account_id, identity_revision FROM accounts WHERE email_hash=? AND status<>'removed'",
            (identity_hash,),
        ).fetchone()
        if not account:
            return {'status': 'unmatched', 'reason': REASON_IDENTITY_UNVERIFIED}

        claim = tx.execute(
            'SELECT thread_id, segment_id FROM conversation_claims WHERE native_namespace=? AND native_id=?',
            (ns, cid),
        ).fetchone()
        if not claim:
            return {'status': 'ignored', 'reason': 'unclaimed_conversation'}

        seg = tx.execute(
            'SELECT segment_id, thread_id, launch_id, account_id, account_binding_state, identity_revision FROM segments WHERE segment_id=?',
            (claim['segment_id'],),
        ).fetchone()
        if not seg:
            return {'status': 'ignored', 'reason': 'missing_segment'}
        if seg['launch_id'] != event['launch_id']:
            return {'status': 'rejected', 'reason': 'launch_mismatch'}

        if seg['account_binding_state'] == 'unverified':
            cur = tx.execute(
                '''UPDATE segments
                   SET account_id=?, account_binding_state='verified', identity_revision=?, observed_email_hash=?
                   WHERE segment_id=? AND account_binding_state='unverified' ''',
                (account['account_id'], account['identity_revision'], identity_hash, seg['segment_id']),
            )
            if cur.rowcount == 1:
                tx.record_event(
                    'm4_identity_bound',
                    thread_id=seg['thread_id'],
                    segment_id=seg['segment_id'],
                    account_id=account['account_id'],
                    detail={'identity_revision': account['identity_revision']},
                )
            return {
                'status': 'bound',
                'thread_id': seg['thread_id'],
                'segment_id': seg['segment_id'],
                'account_id': account['account_id'],
                'identity_revision': account['identity_revision'],
            }

        if seg['account_id'] == account['account_id'] and seg['identity_revision'] == account['identity_revision']:
            return {
                'status': 'already_bound',
                'thread_id': seg['thread_id'],
                'segment_id': seg['segment_id'],
                'account_id': account['account_id'],
            }
        return {'status': 'conflict', 'reason': REASON_AUTH_MISMATCH}

    def _handle_quota_sample(self, tx, event: dict[str, Any], *, now_dt: datetime, now_iso: str) -> dict[str, Any]:
        pool = event.get('pool')
        bucket = event.get('bucket')
        rem = event.get('remaining_percent')
        identity_hash = event.get('identity_hash')
        observed_str = event.get('source_observed_at')
        observed_dt = parse_utc(observed_str)

        if (
            not isinstance(pool, str)
            or not isinstance(bucket, str)
            or type(rem) not in (int, float)
            or not math.isfinite(rem)
            or rem < 0.0
            or rem > 100.0
            or not isinstance(identity_hash, str)
            or not identity_hash
            or observed_dt is None
        ):
            return {'status': 'ignored', 'reason': 'invalid_quota_sample', 'low_quota_active': False, 'should_notify': False}

        if observed_dt > now_dt + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            return {'status': 'ignored', 'reason': 'future_timestamp', 'low_quota_active': False, 'should_notify': False}

        account = tx.execute(
            "SELECT account_id, identity_revision FROM accounts WHERE email_hash=? AND status<>'removed'",
            (identity_hash,),
        ).fetchone()
        if not account:
            return {'status': 'ignored', 'reason': REASON_IDENTITY_UNVERIFIED, 'low_quota_active': False, 'should_notify': False}

        ev_rev = event.get('identity_revision')
        if ev_rev is not None and ev_rev != account['identity_revision']:
            return {'status': 'ignored', 'reason': 'stale_identity_revision', 'low_quota_active': False, 'should_notify': False}

        reset_str = event.get('reset_at') if isinstance(event.get('reset_at'), str) else None
        reset_dt = parse_utc(reset_str) if reset_str else None
        window_key = event.get('window_key') if isinstance(event.get('window_key'), str) and event.get('window_key').strip() else None
        bucket_key = f'{pool.lower()}_{bucket.lower()}'
        source_kind = str(event.get('source') or 'unknown')

        # Monotonic update strictly by source_observed_at (never let stale/redraw/7d overwrite newer 5h).
        prev = tx.execute(
            'SELECT sampled_at FROM account_quota_samples WHERE account_id=? AND bucket=?',
            (account['account_id'], bucket_key),
        ).fetchone()
        prev_dt = parse_utc(prev['sampled_at']) if prev else None
        if prev_dt is None or observed_dt > prev_dt:
            tx.execute(
                '''INSERT INTO account_quota_samples(
                       account_id, identity_revision, bucket, remaining_percent,
                       sampled_at, received_at, reset_at, window_key, source_kind, source_launch_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, bucket) DO UPDATE SET
                       identity_revision=excluded.identity_revision,
                       remaining_percent=excluded.remaining_percent,
                       sampled_at=excluded.sampled_at,
                       received_at=excluded.received_at,
                       reset_at=excluded.reset_at,
                       window_key=excluded.window_key,
                       source_kind=excluded.source_kind,
                       source_launch_id=excluded.source_launch_id''',
                (
                    account['account_id'],
                    account['identity_revision'],
                    bucket_key,
                    float(rem),
                    _iso_utc(observed_dt),
                    now_iso,
                    _iso_utc(reset_dt) if reset_dt else None,
                    window_key,
                    source_kind,
                    event['launch_id'],
                ),
            )

        is_fresh = (now_dt - observed_dt).total_seconds() <= self.quota_ttl_seconds
        if reset_dt is not None and now_dt >= reset_dt:
            is_fresh = False

        # FR-03: Only fresh, attributed Gemini 5h <= 5.0% triggers M4 low-quota behavior.
        if bucket_key != 'gemini_5h' or not is_fresh:
            return {
                'status': 'recorded',
                'bucket': bucket_key,
                'fresh': is_fresh,
                'low_quota_active': False,
                'should_notify': False,
            }

        seg = tx.execute(
            '''SELECT segment_id, thread_id, account_id, account_binding_state, identity_revision
               FROM segments WHERE launch_id=? AND status='running' ORDER BY rowid DESC LIMIT 1''',
            (event['launch_id'],),
        ).fetchone()
        if (
            not seg
            or seg['account_binding_state'] != 'verified'
            or seg['account_id'] != account['account_id']
            or seg['identity_revision'] != account['identity_revision']
        ):
            return {
                'status': 'recorded_unbound',
                'bucket': bucket_key,
                'fresh': True,
                'low_quota_active': False,
                'should_notify': False,
            }

        low_active = float(rem) <= LOW_QUOTA_THRESHOLD
        should_notify = False
        if low_active and window_key:
            cur = tx.execute(
                '''INSERT OR IGNORE INTO alerts(thread_id, account_id, bucket, window_key, created_at, claim_state)
                   VALUES (?, ?, 'gemini_5h', ?, ?, 'claimed')''',
                (seg['thread_id'], account['account_id'], window_key, now_iso),
            )
            should_notify = cur.rowcount == 1
            if should_notify:
                tx.record_event(
                    'm4_low_quota_alert',
                    thread_id=seg['thread_id'],
                    segment_id=seg['segment_id'],
                    account_id=account['account_id'],
                    detail={'remaining_percent': float(rem), 'window_key': window_key},
                )

        return {
            'status': 'recorded',
            'bucket': bucket_key,
            'fresh': True,
            'low_quota_active': low_active,
            'should_notify': should_notify,
            'window_key': window_key,
        }

    def _handle_auth_observation(self, tx, event: dict[str, Any], *, now_dt: datetime, now_iso: str) -> dict[str, Any]:
        probe_id = event['probe_id']
        challenge = event.get('challenge')
        identity_hash = event.get('identity_hash')
        auth_epoch = event.get('auth_epoch')
        observed_dt = parse_utc(event.get('source_observed_at'))
        source = event.get('source')

        if (
            not isinstance(challenge, str)
            or not challenge
            or not isinstance(identity_hash, str)
            or not identity_hash
            or not isinstance(auth_epoch, str)
            or not auth_epoch
            or observed_dt is None
            or not isinstance(source, str)
            or not source
        ):
            return {'status': 'rejected', 'reason': REASON_INVALID_EVENT}

        if observed_dt > now_dt + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            return {'status': 'rejected', 'reason': REASON_AUTH_EXPIRED}

        account = tx.execute(
            "SELECT account_id, identity_revision FROM accounts WHERE email_hash=? AND status<>'removed'",
            (identity_hash,),
        ).fetchone()
        expires_dt = observed_dt + timedelta(seconds=self.auth_ttl_seconds)

        tx.execute(
            '''INSERT INTO auth_observations(
                   probe_id, challenge_ref, account_id, identity_hash, identity_revision,
                   auth_epoch_ref, source, observed_at, expires_at, consumed
               ) VALUES (?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(probe_id) DO UPDATE SET
                   challenge_ref=excluded.challenge_ref,
                   account_id=excluded.account_id,
                   identity_hash=excluded.identity_hash,
                   identity_revision=excluded.identity_revision,
                   auth_epoch_ref=excluded.auth_epoch_ref,
                   source=excluded.source,
                   observed_at=excluded.observed_at,
                   expires_at=excluded.expires_at,
                   consumed=0''',
            (
                probe_id,
                challenge,
                account['account_id'] if account else None,
                identity_hash,
                account['identity_revision'] if account else None,
                auth_epoch,
                source,
                _iso_utc(observed_dt),
                _iso_utc(expires_dt),
            ),
        )
        return {
            'status': 'recorded',
            'probe_id': probe_id,
            'account_id': account['account_id'] if account else None,
            'expires_at': _iso_utc(expires_dt),
        }

    def _handle_new_conversation(self, tx, event: dict[str, Any], *, now_dt: datetime, now_iso: str) -> dict[str, Any]:
        ns, cid = self._validate_conv_key(event)
        attempt_id = event.get('attempt_id')
        launch_id = event['launch_id']
        identity_hash = event.get('identity_hash')
        auth_epoch = event.get('auth_epoch')
        observed_dt = parse_utc(event.get('source_observed_at'))

        if not isinstance(attempt_id, str) or not attempt_id or not isinstance(identity_hash, str) or observed_dt is None:
            return {'status': 'rejected', 'reason': REASON_INVALID_EVENT}

        attempt = tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if not attempt or attempt['launch_id'] != launch_id:
            return {'status': 'rejected', 'reason': 'unknown_attempt'}

        intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (attempt['intent_id'],)).fetchone()
        if not intent:
            return {'status': 'rejected', 'reason': 'missing_intent'}

        # Idempotent replay if already consumed with this exact conversation_id.
        if attempt['stage'] == 'consumed':
            if attempt['native_new_id'] == cid:
                return {
                    'status': 'consumed',
                    'thread_id': intent['thread_id'],
                    'segment_id': intent['consumed_segment_id'],
                    'conversation_id': cid,
                }
            tx.record_event('m4_multiple_new_ids_conflict', thread_id=intent['thread_id'], detail={'attempt_id': attempt_id})
            return {'status': 'conflict', 'reason': 'multiple_new_ids'}

        if attempt['stage'] not in ('spawn_armed', 'await_new_id', 'uncertain'):
            return {'status': 'rejected', 'reason': REASON_INVALID_STATE}

        if attempt['native_new_id'] is not None and attempt['native_new_id'] != cid:
            tx.record_event('m4_multiple_new_ids_conflict', thread_id=intent['thread_id'], detail={'attempt_id': attempt_id})
            return {'status': 'conflict', 'reason': 'multiple_new_ids'}

        target_acc = tx.execute(
            "SELECT account_id, email_hash, identity_revision FROM accounts WHERE account_id=? AND status<>'removed'",
            (intent['target_account_id'],),
        ).fetchone()
        if not target_acc or target_acc['email_hash'] != identity_hash:
            return {'status': 'rejected', 'reason': REASON_AUTH_MISMATCH}

        if attempt['auth_epoch_ref'] and auth_epoch and attempt['auth_epoch_ref'] != auth_epoch:
            return {'status': 'rejected', 'reason': REASON_AUTH_MISMATCH}

        armed_dt = parse_utc(attempt['armed_at'])
        if armed_dt is not None and observed_dt < armed_dt - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            return {'status': 'rejected', 'reason': 'predates_spawn_armed'}

        # Must be a brand-new conversation ID, never an existing claimed ID!
        existing_claim = tx.execute(
            'SELECT thread_id, segment_id FROM conversation_claims WHERE native_namespace=? AND native_id=?',
            (ns, cid),
        ).fetchone()
        if existing_claim:
            tx.record_event(
                'm4_new_conversation_id_conflict',
                thread_id=intent['thread_id'],
                detail={'attempt_id': attempt_id},
            )
            return {'status': 'conflict', 'reason': 'conversation_id_conflict'}

        source_seg = tx.execute('SELECT * FROM segments WHERE segment_id=?', (intent['source_segment_id'],)).fetchone()
        packet = tx.execute('SELECT * FROM handoff_packets WHERE packet_id=?', (intent['packet_id'],)).fetchone()
        next_seq = int(source_seg['seq']) + 1
        new_sid = f"{intent['thread_id']}-seg{next_seq}"
        was_cancel_requested = intent['state'] == 'cancel_requested'

        tx.execute(
            '''INSERT INTO segments(
                   segment_id, thread_id, seq, account_id, parent_segment_id, conversation_id,
                   handoff_in_path, pid, pid_create_time, status, observed_email_hash, started_at,
                   segment_origin, launch_id, intent_id, account_binding_state, identity_revision
               ) VALUES (?,?,?,?,?,?,?,?,?,'running',?,?,'m4_native',?,?,'verified',?)''',
            (
                new_sid,
                intent['thread_id'],
                next_seq,
                target_acc['account_id'],
                source_seg['segment_id'],
                cid,
                packet['relative_path'] if packet else None,
                attempt['pid'],
                attempt['pid_create_time'],
                identity_hash,
                _iso_utc(observed_dt),
                launch_id,
                intent['intent_id'],
                target_acc['identity_revision'],
            ),
        )
        tx.execute(
            '''INSERT INTO conversation_claims(
                   native_namespace, native_id, thread_id, segment_id,
                   claim_kind, first_user_ref, claimed_at
               ) VALUES (?, ?, ?, ?, 'm4_handoff_new_conversation', ?, ?)''',
            (ns, cid, intent['thread_id'], new_sid, f"attempt:{attempt_id}", now_iso),
        )
        tx.execute("UPDATE segments SET status='handed_off' WHERE segment_id=?", (source_seg['segment_id'],))
        # FR-02: Never overwrite threads.title when advancing to next segment!
        tx.execute(
            'UPDATE threads SET current_segment_id=?, segment_count=?, updated_at=? WHERE thread_id=?',
            (new_sid, next_seq, now_iso, intent['thread_id']),
        )
        new_intent_state = 'consumed'
        error_note = 'cancel_requested_after_spawn' if was_cancel_requested else None
        tx.execute(
            '''UPDATE switch_intents
               SET state=?, revision=revision+1, blocked_phase=NULL, error_code=?, consumed_segment_id=?
               WHERE intent_id=?''',
            (new_intent_state, error_note, new_sid, intent['intent_id']),
        )
        tx.execute(
            '''UPDATE launch_attempts
               SET stage='consumed', native_new_id=?, native_namespace=?, ended_at=?
               WHERE attempt_id=?''',
            (cid, ns, now_iso, attempt_id),
        )
        tx.execute(
            '''INSERT INTO delivery_receipts(attempt_id, segment_id, packet_digest, delivery_state, read_ref, observed_at)
               VALUES (?, ?, ?, ?, NULL, NULL)''',
            (attempt_id, new_sid, attempt['packet_digest'], REASON_DELIVERY_UNVERIFIED),
        )
        tx.record_event(
            'm4_handoff_consumed',
            thread_id=intent['thread_id'],
            segment_id=new_sid,
            account_id=target_acc['account_id'],
            detail={'intent_id': intent['intent_id'], 'attempt_id': attempt_id, 'cancel_requested': was_cancel_requested},
        )
        self.barrier.clear(db_id=self.store.db_id, intent_id=intent['intent_id'])
        return {
            'status': 'consumed',
            'thread_id': intent['thread_id'],
            'segment_id': new_sid,
            'seq': next_seq,
            'conversation_id': cid,
            'delivery_state': REASON_DELIVERY_UNVERIFIED,
            'cancel_requested': was_cancel_requested,
        }

    def _handle_packet_read(self, tx, event: dict[str, Any], *, now_iso: str) -> dict[str, Any]:
        ns, cid = self._validate_conv_key(event)
        attempt_id = event.get('attempt_id')
        launch_id = event['launch_id']
        digest = event.get('packet_digest')
        source = event.get('source')
        source_ref = event.get('source_ref')

        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(digest, str)
            or not digest
            or source not in _VALID_READ_SOURCES
            or not isinstance(source_ref, str)
            or not source_ref.strip()
        ):
            return {'status': 'ignored', 'reason': 'invalid_read_evidence'}

        attempt = tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if not attempt or attempt['launch_id'] != launch_id or attempt['stage'] != 'consumed':
            return {'status': 'ignored', 'reason': 'attempt_not_consumed'}
        if attempt['native_new_id'] != cid:
            return {'status': 'rejected', 'reason': 'conversation_mismatch'}

        receipt = tx.execute('SELECT * FROM delivery_receipts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if not receipt:
            return {'status': 'ignored', 'reason': 'missing_receipt'}

        read_ref = f'{source}:{source_ref.strip()}'
        guard(read_ref)
        observed_at = event.get('source_observed_at') if isinstance(event.get('source_observed_at'), str) else now_iso

        if digest == attempt['packet_digest']:
            tx.execute(
                "UPDATE delivery_receipts SET delivery_state='delivered', read_ref=?, observed_at=? WHERE attempt_id=?",
                (read_ref, observed_at, attempt_id),
            )
            tx.record_event(
                'm4_packet_delivered',
                segment_id=receipt['segment_id'],
                detail={'attempt_id': attempt_id, 'delivery_state': 'delivered'},
            )
            return {'status': 'delivered', 'attempt_id': attempt_id, 'delivery_state': 'delivered'}

        tx.execute(
            "UPDATE delivery_receipts SET delivery_state='delivery_needs_review', read_ref=?, observed_at=? WHERE attempt_id=?",
            (read_ref, observed_at, attempt_id),
        )
        tx.record_event(
            'm4_packet_digest_mismatch',
            segment_id=receipt['segment_id'],
            detail={'attempt_id': attempt_id, 'delivery_state': 'delivery_needs_review'},
        )
        return {'status': 'mismatch', 'attempt_id': attempt_id, 'delivery_state': 'delivery_needs_review'}

    # ------------------------------------------------------------------
    # Switch Intent & Launch Attempt State Machine (§5.2, §5.3, §9.4)
    # ------------------------------------------------------------------
    def confirm_switch_intent(
        self,
        *,
        thread_id: str,
        source_segment_id: str,
        source_launch_id: str,
        terminal_binding: str,
        target_account_id: str,
        source_exited: bool = False,
    ) -> dict[str, Any]:
        with self.store.transaction() as tx:
            thread = tx.execute('SELECT * FROM threads WHERE thread_id=?', (thread_id,)).fetchone()
            if not thread or thread['status'] != 'active' or thread['current_segment_id'] != source_segment_id:
                raise M4Error(REASON_INVALID_STATE, '源棒不是该任务的当前活跃棒。')

            seg = tx.execute('SELECT * FROM segments WHERE segment_id=?', (source_segment_id,)).fetchone()
            if not seg or seg['launch_id'] != source_launch_id:
                raise M4Error(REASON_ORIGINAL_TERMINAL_UNVERIFIED, '当前分屏控制器未持有该源会话。')
            if seg['account_binding_state'] != 'verified' or not seg['account_id']:
                raise M4Error(REASON_IDENTITY_UNVERIFIED, '当前会话账号尚未核验，无法发起换号。')
            if seg['account_id'] == target_account_id:
                raise M4Error(REASON_INVALID_STATE, '目标账号与当前账号相同，无需切换。')

            target = tx.execute(
                "SELECT account_id FROM accounts WHERE account_id=? AND status<>'removed'", (target_account_id,)
            ).fetchone()
            if not target:
                raise M4Error(REASON_INVALID_STATE, f'目标账号槽位 {target_account_id} 未注册。')

            existing = tx.execute(
                "SELECT * FROM switch_intents WHERE source_segment_id=? AND state NOT IN ('consumed', 'cancelled')",
                (source_segment_id,),
            ).fetchone()
            if existing:
                if existing['target_account_id'] == target_account_id:
                    return dict(existing)
                raise M4Error(REASON_INTENT_CONFLICT, '该源会话已存在其他目标账号的未结束换号意图，请先取消。')

            intent_id = f'INT-{uuid.uuid4().hex[:12]}'
            initial_state = 'preparing_packet' if source_exited else 'await_exit'
            now = utc_now()
            if source_exited and seg['status'] == 'running':
                tx.execute("UPDATE segments SET status='closed', ended_at=? WHERE segment_id=?", (now, source_segment_id))

            tx.execute(
                '''INSERT INTO switch_intents(
                       intent_id, thread_id, source_segment_id, source_launch_id, terminal_binding,
                       source_account_id, target_account_id, confirmed_at, state, revision
                   ) VALUES (?,?,?,?,?,?,?,?,?,1)''',
                (
                    intent_id,
                    thread_id,
                    source_segment_id,
                    source_launch_id,
                    terminal_binding,
                    seg['account_id'],
                    target_account_id,
                    now,
                    initial_state,
                ),
            )
            tx.record_event(
                'm4_switch_intent_confirmed',
                thread_id=thread_id,
                segment_id=source_segment_id,
                account_id=target_account_id,
                detail={'intent_id': intent_id, 'state': initial_state},
            )
            return dict(tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone())

    def mark_source_exited(
        self,
        intent_id: str,
        *,
        expected_revision: int,
        normal_exit: bool,
        source_launch_id: str,
    ) -> dict[str, Any]:
        if not normal_exit:
            raise M4Error(REASON_NOT_NORMAL_EXIT, '源会话未正常安全退出（请先在原窗口执行 /exit）。')
        with self.store.transaction() as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if not intent or intent['source_launch_id'] != source_launch_id:
                raise M4Error(REASON_ORIGINAL_TERMINAL_UNVERIFIED, '意图不存在或不属于当前分屏控制器。')
            if intent['state'] != 'await_exit' or intent['revision'] != expected_revision:
                raise M4Error(REASON_INVALID_STATE, '意图状态已变化（CAS 校验失败）。')

            now = utc_now()
            tx.execute(
                "UPDATE segments SET status='closed', ended_at=? WHERE segment_id=? AND status<>'handed_off'",
                (now, intent['source_segment_id']),
            )
            cur = tx.execute(
                '''UPDATE switch_intents
                   SET state='preparing_packet', revision=revision+1, blocked_phase=NULL, error_code=NULL
                   WHERE intent_id=? AND state='await_exit' AND revision=?''',
                (intent_id, expected_revision),
            )
            if cur.rowcount != 1:
                raise M4Error(REASON_INVALID_STATE, '意图状态并发冲突。')
            tx.record_event('m4_source_exited', thread_id=intent['thread_id'], segment_id=intent['source_segment_id'])
            return dict(tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone())

    def freeze_packet(
        self,
        intent_id: str,
        *,
        expected_revision: int,
        transcript: Path | None = None,
        task: str = '',
        git_status: str | None = None,
        git_diff: str = '',
        anchor_is_placeholder: bool = False,
    ) -> dict[str, Any]:
        with self.store.transaction(write=False) as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if not intent:
                raise M4Error(REASON_INVALID_STATE, '换号意图不存在。')
            if intent['state'] != 'preparing_packet' or intent['revision'] != expected_revision:
                raise M4Error(REASON_INVALID_STATE, '意图当前不在准备交接包阶段。')
            seg = tx.execute('SELECT * FROM segments WHERE segment_id=?', (intent['source_segment_id'],)).fetchone()
            if not seg or seg['account_binding_state'] != 'verified' or seg['status'] != 'closed':
                raise M4Error(REASON_INVALID_STATE, '仅在源棒已归属且安全退出后才允许冻结交接包。')
            thread = tx.execute('SELECT * FROM threads WHERE thread_id=?', (intent['thread_id'],)).fetchone()
            chain = tx.execute(
                "SELECT seq, account_id FROM segments WHERE thread_id=? AND status<>'failed' AND seq<=? ORDER BY seq",
                (intent['thread_id'], seg['seq']),
            ).fetchall()

        anchor_path = self.store.artifact(thread['anchor_path'])
        anchor_text = anchor_path.read_text(encoding='utf-8-sig') if anchor_path.is_file() else ''
        is_placeholder = anchor_is_placeholder or ('M4 自动登记' in anchor_text and len(anchor_text.strip()) < 60)

        try:
            result = HandoffBuilder().build_m4(
                anchor_text,
                transcript=transcript,
                task=task,
                git_status=git_status,
                git_diff=git_diff,
                anchor_is_placeholder=is_placeholder,
                provenance={
                    'segment_id': seg['segment_id'],
                    'account_id': seg['account_id'],
                    'conversation_id': seg['conversation_id'],
                    'intent_id': intent_id,
                    'chain': ' -> '.join(f"#{r['seq']}({r['account_id'] or 'unverified'})" for r in chain),
                },
            )
        except NoHandoffEvidence:
            with self.store.transaction() as tx:
                tx.execute(
                    '''UPDATE switch_intents
                       SET state='blocked', revision=revision+1, blocked_phase='preparing_packet', error_code=?
                       WHERE intent_id=? AND state='preparing_packet' AND revision=?''',
                    (REASON_NO_EVIDENCE, intent_id, expected_revision),
                )
                tx.record_event('m4_packet_no_evidence', thread_id=intent['thread_id'], segment_id=seg['segment_id'])
            raise M4Error(REASON_NO_EVIDENCE, '缺少可用交接内容，已暂停生成交接包。')

        packet_id = f'PKT-{uuid.uuid4().hex[:12]}'
        rel_path = f'handoffs/{packet_id}.md'
        abs_path = self.store.artifact(rel_path)
        write_text_atomic(abs_path, result.text)

        with self.store.transaction() as tx:
            cur = tx.execute(
                '''UPDATE switch_intents
                   SET state='packet_ready', revision=revision+1, blocked_phase=NULL, error_code=NULL, packet_id=?
                   WHERE intent_id=? AND state='preparing_packet' AND revision=?''',
                (packet_id, intent_id, expected_revision),
            )
            if cur.rowcount != 1:
                raise M4Error(REASON_INVALID_STATE, '冻结交接包时意图状态并发变化。')
            tx.execute(
                '''INSERT INTO handoff_packets(
                       packet_id, intent_id, source_segment_id, relative_path, sha256, bytes,
                       estimated_tokens, quality, evidence_flags, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (
                    packet_id,
                    intent_id,
                    seg['segment_id'],
                    rel_path,
                    result.sha256,
                    result.bytes,
                    result.tokens,
                    result.quality,
                    json.dumps(list(result.evidence_flags), ensure_ascii=False),
                    utc_now(),
                ),
            )
            tx.execute('UPDATE segments SET handoff_out_path=? WHERE segment_id=?', (rel_path, seg['segment_id']))
            tx.record_event(
                'm4_packet_frozen',
                thread_id=intent['thread_id'],
                segment_id=seg['segment_id'],
                detail={'packet_id': packet_id, 'quality': result.quality, 'tokens': result.tokens},
            )
            return {
                'intent': dict(tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()),
                'packet': dict(tx.execute('SELECT * FROM handoff_packets WHERE packet_id=?', (packet_id,)).fetchone()),
            }

    def _verify_packet_integrity(self, tx, packet_id: str) -> dict[str, Any]:
        pkt = tx.execute('SELECT * FROM handoff_packets WHERE packet_id=?', (packet_id,)).fetchone()
        if not pkt:
            raise M4Error(REASON_PACKET_CORRUPT, '交接包记录缺失。')
        try:
            abs_path = self.store.artifact(pkt['relative_path'])
        except ValueError as exc:
            raise M4Error(REASON_PACKET_CORRUPT, '交接包路径越界。') from exc
        if abs_path.is_symlink() or not abs_path.is_file():
            raise M4Error(REASON_PACKET_CORRUPT, '交接包文件缺失或为非法符号链接。')
        raw = abs_path.read_bytes()
        if len(raw) != pkt['bytes'] or hashlib.sha256(raw).hexdigest() != pkt['sha256']:
            raise M4Error(REASON_PACKET_CORRUPT, '交接包文件校验和不匹配，已阻断交付。')
        return dict(pkt)

    def _block_intent(self, tx, intent_id: str, phase: str, reason_code: str) -> None:
        tx.execute(
            '''UPDATE switch_intents
               SET state='blocked', revision=revision+1, blocked_phase=?, error_code=?
               WHERE intent_id=?''',
            (phase, reason_code, intent_id),
        )
        tx.record_event('m4_intent_blocked', detail={'intent_id': intent_id, 'phase': phase, 'reason': reason_code})

    def begin_login(
        self,
        intent_id: str,
        *,
        expected_revision: int,
        terminal_binding: str,
        conflict_scan_ok: bool,
        shared_conflicts: list[Any],
    ) -> ChallengeToken:
        with self.store.transaction() as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if not intent or intent['state'] != 'packet_ready' or intent['revision'] != expected_revision:
                raise M4Error(REASON_INVALID_STATE, '意图当前不在可进入登录阶段。')

            if intent['terminal_binding'] != terminal_binding:
                self._block_intent(tx, intent_id, 'await_login', REASON_ORIGINAL_TERMINAL_UNVERIFIED)
                raise M4Error(REASON_ORIGINAL_TERMINAL_UNVERIFIED, '无法证明原分屏控制权连续，已保留交接包待查询。')

            if not conflict_scan_ok or shared_conflicts:
                self._block_intent(tx, intent_id, 'await_login', REASON_SHARED_SESSION)
                raise M4Error(REASON_SHARED_SESSION, '共享登录域存在受影响会话或扫描不完整，已硬阻断换号。')

            try:
                self.barrier.assert_clear_for(self.store.db_id, intent_id)
            except M4Error:
                self._block_intent(tx, intent_id, 'await_login', REASON_SHARED_SESSION)
                raise

            try:
                self._verify_packet_integrity(tx, intent['packet_id'])
            except M4Error:
                self._block_intent(tx, intent_id, 'await_login', REASON_PACKET_CORRUPT)
                raise

            self.barrier.write(db_id=self.store.db_id, intent_id=intent_id, stage='await_login')
            probe_id = f'PRB-{uuid.uuid4().hex[:12]}'
            challenge_ref = f'CHG-{uuid.uuid4().hex[:16]}'
            issued_at = utc_now()
            tx.execute(
                '''UPDATE switch_intents
                   SET state='await_verification', revision=revision+1, blocked_phase=NULL, error_code=NULL
                   WHERE intent_id=? AND state='packet_ready' AND revision=?''',
                (intent_id, expected_revision),
            )
            tx.record_event(
                'm4_login_challenge_issued',
                thread_id=intent['thread_id'],
                segment_id=intent['source_segment_id'],
                account_id=intent['target_account_id'],
                detail={'intent_id': intent_id, 'probe_id': probe_id},
            )
            return ChallengeToken(probe_id=probe_id, challenge_ref=challenge_ref, issued_at=issued_at)

    def verify_target_and_prepare(
        self,
        intent_id: str,
        *,
        expected_revision: int,
        terminal_binding: str,
        challenge: ChallengeToken,
        consumer_context_bound: bool,
        conflict_scan_ok: bool,
        shared_conflicts: list[Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.store.transaction() as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if not intent or intent['state'] != 'await_verification' or intent['revision'] != expected_revision:
                raise M4Error(REASON_INVALID_STATE, '意图不在等待核验阶段。')

            if intent['terminal_binding'] != terminal_binding:
                self._block_intent(tx, intent_id, 'await_verification', REASON_ORIGINAL_TERMINAL_UNVERIFIED)
                raise M4Error(REASON_ORIGINAL_TERMINAL_UNVERIFIED, '原分屏控制权校验失败。')

            if not conflict_scan_ok or shared_conflicts:
                self._block_intent(tx, intent_id, 'await_verification', REASON_SHARED_SESSION)
                raise M4Error(REASON_SHARED_SESSION, '共享登录域存在冲突会话，已阻断启动。')

            try:
                self.barrier.assert_clear_for(self.store.db_id, intent_id)
                pkt = self._verify_packet_integrity(tx, intent['packet_id'])
            except M4Error as exc:
                self._block_intent(tx, intent_id, 'await_verification', exc.reason_code)
                raise

            obs = tx.execute('SELECT * FROM auth_observations WHERE probe_id=?', (challenge.probe_id,)).fetchone()
            target = tx.execute(
                "SELECT account_id, email_hash, identity_revision FROM accounts WHERE account_id=? AND status<>'removed'",
                (intent['target_account_id'],),
            ).fetchone()
            if not obs or not target:
                self._block_intent(tx, intent_id, 'await_verification', REASON_AUTH_EXPIRED)
                raise M4Error(REASON_AUTH_EXPIRED, '未收到有效的目标账号核验观测。')

            obs_dt = parse_utc(obs['observed_at'])
            exp_dt = parse_utc(obs['expires_at'])
            issued_dt = parse_utc(challenge.issued_at)
            if (
                obs['challenge_ref'] != challenge.challenge_ref
                or obs['consumed'] != 0
                or obs_dt is None
                or exp_dt is None
                or issued_dt is None
                or obs_dt < issued_dt
                or now_dt > exp_dt
            ):
                self._block_intent(tx, intent_id, 'await_verification', REASON_AUTH_EXPIRED)
                raise M4Error(REASON_AUTH_EXPIRED, '登录核验已过期或早于本次挑战，请重新核验。')

            if obs['identity_hash'] != target['email_hash'] or obs['identity_revision'] != target['identity_revision']:
                self._block_intent(tx, intent_id, 'await_verification', REASON_AUTH_MISMATCH)
                raise M4Error(REASON_AUTH_MISMATCH, '当前登录账号与目标账号不符，已保留交接包。')

            # Check target fresh 5h quota: if fresh and <= 0.0%, pause and do not spawn!
            q = tx.execute(
                "SELECT remaining_percent, sampled_at, reset_at, identity_revision FROM account_quota_samples WHERE account_id=? AND bucket='gemini_5h'",
                (target['account_id'],),
            ).fetchone()
            quota_warning = None
            if q and q['identity_revision'] == target['identity_revision']:
                q_dt = parse_utc(q['sampled_at'])
                q_reset = parse_utc(q['reset_at'])
                q_fresh = q_dt is not None and (now_dt - q_dt).total_seconds() <= self.quota_ttl_seconds
                if q_reset is not None and now_dt >= q_reset:
                    q_fresh = False
                if q_fresh and float(q['remaining_percent']) <= 0.0:
                    self._block_intent(tx, intent_id, 'await_verification', REASON_QUOTA_ZERO)
                    raise M4Error(REASON_QUOTA_ZERO, '目标账号新鲜 Gemini 5h 额度为 0%，已保留待接棒状态。')
                if not q_fresh:
                    quota_warning = REASON_QUOTA_UNKNOWN
            else:
                quota_warning = REASON_QUOTA_UNKNOWN

            if not consumer_context_bound:
                self._block_intent(tx, intent_id, 'await_verification', REASON_CONSUMER_UNBOUND)
                raise M4Error(REASON_CONSUMER_UNBOUND, '无法在交付前绑定实际消费者认证上下文，已保留交接包且禁止传递 -i。')

            next_no_row = tx.execute(
                'SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM launch_attempts WHERE intent_id=?', (intent_id,)
            ).fetchone()
            attempt_no = int(next_no_row[0])
            attempt_id = f'ATT-{uuid.uuid4().hex[:12]}'
            launch_id = f'LCH-{uuid.uuid4().hex[:12]}'
            now_iso = _iso_utc(now_dt)

            tx.execute(
                '''INSERT INTO launch_attempts(
                       attempt_id, intent_id, attempt_no, launch_id, terminal_binding,
                       packet_digest, verified_probe_id, auth_epoch_ref, created_at,
                       authorized_auth_observed_at, stage
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,'prepared')''',
                (
                    attempt_id,
                    intent_id,
                    attempt_no,
                    launch_id,
                    terminal_binding,
                    pkt['sha256'],
                    challenge.probe_id,
                    obs['auth_epoch_ref'],
                    now_iso,
                    obs['observed_at'],
                ),
            )
            tx.execute(
                '''UPDATE switch_intents
                   SET state='prepared', revision=revision+1, blocked_phase=NULL, error_code=NULL
                   WHERE intent_id=? AND state='await_verification' AND revision=?''',
                (intent_id, expected_revision),
            )
            tx.record_event(
                'm4_attempt_prepared',
                thread_id=intent['thread_id'],
                account_id=target['account_id'],
                detail={'intent_id': intent_id, 'attempt_id': attempt_id, 'quota_warning': quota_warning},
            )
            return {
                'intent': dict(tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()),
                'attempt': dict(tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()),
                'quota_warning': quota_warning,
            }

    def arm_spawn(
        self,
        intent_id: str,
        attempt_id: str,
        *,
        terminal_binding: str,
        consumer_context_bound: bool,
        conflict_scan_ok: bool,
        shared_conflicts: list[Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Cross the single-use spawn_armed boundary and return the validated -i prompt."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = _iso_utc(now_dt)
        with self.store.transaction() as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            attempt = tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if not intent or not attempt or attempt['intent_id'] != intent_id:
                raise M4Error(REASON_INVALID_STATE, '启动候选记录不存在。')
            if intent['state'] != 'prepared' or attempt['stage'] != 'prepared':
                raise M4Error(REASON_INVALID_STATE, '候选启动不在 prepared 状态，禁止重复授权。')

            if intent['terminal_binding'] != terminal_binding or attempt['terminal_binding'] != terminal_binding:
                self._block_intent(tx, intent_id, 'prepared', REASON_ORIGINAL_TERMINAL_UNVERIFIED)
                raise M4Error(REASON_ORIGINAL_TERMINAL_UNVERIFIED, '原分屏控制权校验失败。')

            if not conflict_scan_ok or shared_conflicts:
                self._block_intent(tx, intent_id, 'prepared', REASON_SHARED_SESSION)
                raise M4Error(REASON_SHARED_SESSION, '启动前复查发现共享登录冲突，已阻断 -i 交付。')

            if not consumer_context_bound:
                self._block_intent(tx, intent_id, 'prepared', REASON_CONSUMER_UNBOUND)
                raise M4Error(REASON_CONSUMER_UNBOUND, '启动前实际消费者绑定失效，已阻断 -i 交付。')

            try:
                self.barrier.assert_clear_for(self.store.db_id, intent_id)
                pkt = self._verify_packet_integrity(tx, intent['packet_id'])
            except M4Error as exc:
                self._block_intent(tx, intent_id, 'prepared', exc.reason_code)
                raise

            # Re-verify auth observation freshness & target revision right before crossing spawn_armed.
            obs = tx.execute('SELECT * FROM auth_observations WHERE probe_id=?', (attempt['verified_probe_id'],)).fetchone()
            target = tx.execute(
                "SELECT account_id, email_hash, identity_revision FROM accounts WHERE account_id=? AND status<>'removed'",
                (intent['target_account_id'],),
            ).fetchone()
            exp_dt = parse_utc(obs['expires_at']) if obs else None
            if (
                not obs
                or not target
                or obs['consumed'] != 0
                or exp_dt is None
                or now_dt > exp_dt
                or obs['identity_hash'] != target['email_hash']
                or obs['identity_revision'] != target['identity_revision']
            ):
                self._block_intent(tx, intent_id, 'prepared', REASON_AUTH_EXPIRED)
                raise M4Error(REASON_AUTH_EXPIRED, '启动前核验已过期或槽位已改绑，请重新核验。')

            prompt = build_handoff_prompt(self.store.artifact(pkt['relative_path']))
            self.barrier.write(
                db_id=self.store.db_id,
                intent_id=intent_id,
                stage='spawn_armed',
                attempt_id=attempt_id,
            )
            tx.execute('UPDATE auth_observations SET consumed=1 WHERE probe_id=?', (attempt['verified_probe_id'],))
            tx.execute(
                "UPDATE launch_attempts SET stage='spawn_armed', armed_at=? WHERE attempt_id=? AND stage='prepared'",
                (now_iso, attempt_id),
            )
            tx.execute(
                "UPDATE switch_intents SET state='spawn_armed', revision=revision+1 WHERE intent_id=? AND state='prepared'",
                (intent_id,),
            )
            tx.record_event(
                'm4_spawn_armed',
                thread_id=intent['thread_id'],
                account_id=target['account_id'],
                detail={'intent_id': intent_id, 'attempt_id': attempt_id, 'launch_id': attempt['launch_id']},
            )
            return {
                'attempt': dict(tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()),
                'prompt': prompt,
                'packet_path': self.store.artifact(pkt['relative_path']),
            }

    def record_spawn_outcome(
        self,
        attempt_id: str,
        *,
        outcome: str,
        pid: int | None = None,
        pid_create_time: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in ('started', 'failed_before_spawn', 'uncertain'):
            raise M4Error(REASON_INVALID_STATE, f'未知的启动结果：{outcome}')
        with self.store.transaction() as tx:
            attempt = tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if not attempt or attempt['stage'] not in ('spawn_armed', 'await_new_id'):
                raise M4Error(REASON_INVALID_STATE, '启动记录不在可记录结果的状态。')
            intent_id = attempt['intent_id']
            now = utc_now()
            if outcome == 'started':
                tx.execute(
                    "UPDATE launch_attempts SET stage='await_new_id', pid=?, pid_create_time=? WHERE attempt_id=?",
                    (pid, pid_create_time, attempt_id),
                )
                tx.execute(
                    "UPDATE switch_intents SET state='await_new_id', revision=revision+1 WHERE intent_id=? AND state='spawn_armed'",
                    (intent_id,),
                )
            elif outcome == 'failed_before_spawn':
                tx.execute(
                    "UPDATE launch_attempts SET stage='failed_before_spawn', ended_at=?, reason=? WHERE attempt_id=?",
                    (now, reason or 'failed_before_spawn', attempt_id),
                )
                tx.execute(
                    '''UPDATE switch_intents
                       SET state='blocked', revision=revision+1, blocked_phase='spawn_failed', error_code='failed_before_spawn'
                       WHERE intent_id=?''',
                    (intent_id,),
                )
                self.barrier.clear(db_id=self.store.db_id, intent_id=intent_id)
            else:
                tx.execute(
                    "UPDATE launch_attempts SET stage='uncertain', ended_at=?, reason=? WHERE attempt_id=?",
                    (now, reason or REASON_SPAWN_UNCERTAIN, attempt_id),
                )
                tx.execute(
                    '''UPDATE switch_intents
                       SET state='blocked', revision=revision+1, blocked_phase='spawn_armed', error_code=?
                       WHERE intent_id=? AND state<>'cancel_requested' ''',
                    (REASON_SPAWN_UNCERTAIN, intent_id),
                )
            tx.record_event('m4_spawn_outcome', detail={'attempt_id': attempt_id, 'outcome': outcome})
            return dict(tx.execute('SELECT * FROM launch_attempts WHERE attempt_id=?', (attempt_id,)).fetchone())

    def cancel_intent(self, intent_id: str) -> dict[str, Any]:
        """Cancel locally without killing processes or rolling back official login."""
        with self.store.transaction() as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if not intent:
                raise M4Error(REASON_INVALID_STATE, '换号意图不存在。')
            if intent['state'] in ('consumed', 'cancelled'):
                return dict(intent)

            crossed = tx.execute(
                "SELECT 1 FROM launch_attempts WHERE intent_id=? AND stage IN ('spawn_armed', 'await_new_id', 'uncertain')",
                (intent_id,),
            ).fetchone()
            if crossed:
                tx.execute(
                    "UPDATE switch_intents SET state='cancel_requested', revision=revision+1 WHERE intent_id=?",
                    (intent_id,),
                )
                tx.record_event('m4_intent_cancel_requested', thread_id=intent['thread_id'], detail={'intent_id': intent_id})
            else:
                tx.execute(
                    "UPDATE launch_attempts SET stage='aborted_before_spawn', ended_at=? WHERE intent_id=? AND stage='prepared'",
                    (utc_now(), intent_id),
                )
                tx.execute(
                    "UPDATE switch_intents SET state='cancelled', revision=revision+1 WHERE intent_id=?",
                    (intent_id,),
                )
                self.barrier.clear(db_id=self.store.db_id, intent_id=intent_id)
                tx.record_event('m4_intent_cancelled', thread_id=intent['thread_id'], detail={'intent_id': intent_id})
            return dict(tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone())

    def retry_intent(self, intent_id: str, *, terminal_binding: str) -> dict[str, Any]:
        """Explicit in-pane retry: requires proven original terminal control and zero crossed-boundary attempts."""
        with self.store.transaction() as tx:
            intent = tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone()
            if not intent:
                raise M4Error(REASON_INVALID_STATE, '换号意图不存在。')
            if intent['state'] in ('consumed', 'cancelled', 'cancel_requested'):
                raise M4Error(REASON_INVALID_STATE, '意图已终结或已请求取消，不能重试。')
            if intent['terminal_binding'] != terminal_binding:
                self._block_intent(tx, intent_id, 'retry', REASON_ORIGINAL_TERMINAL_UNVERIFIED)
                raise M4Error(REASON_ORIGINAL_TERMINAL_UNVERIFIED, '原分屏已关闭或无法证明控制权连续，禁止自动/跨窗重试。')

            crossed = tx.execute(
                "SELECT stage FROM launch_attempts WHERE intent_id=? AND stage IN ('spawn_armed', 'await_new_id', 'uncertain')",
                (intent_id,),
            ).fetchone()
            if crossed:
                raise M4Error(REASON_SPAWN_UNCERTAIN, '已跨过启动边界且结果未确证，禁止重复启动新对话。')

            # Revoke any prepared attempt before resetting to packet_ready (or preparing_packet if no packet yet).
            tx.execute(
                "UPDATE launch_attempts SET stage='aborted_before_spawn', ended_at=? WHERE intent_id=? AND stage='prepared'",
                (utc_now(), intent_id),
            )
            if intent['packet_id']:
                self._verify_packet_integrity(tx, intent['packet_id'])
                next_state = 'packet_ready'
            else:
                next_state = 'preparing_packet'

            tx.execute(
                '''UPDATE switch_intents
                   SET state=?, revision=revision+1, blocked_phase=NULL, error_code=NULL
                   WHERE intent_id=?''',
                (next_state, intent_id),
            )
            tx.record_event('m4_intent_retry', thread_id=intent['thread_id'], detail={'intent_id': intent_id, 'state': next_state})
            return dict(tx.execute('SELECT * FROM switch_intents WHERE intent_id=?', (intent_id,)).fetchone())

    def get_menu_options(self, current_account_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Return the two-row account menu items (main/backup) with fresh/stale/unknown 5h quota labels."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.store.readonly_transaction() as (tx, version):
            rows = tx.execute(
                "SELECT account_id, label, email_hint, identity_revision FROM accounts WHERE status<>'removed' AND account_id IN ('main', 'backup') ORDER BY CASE account_id WHEN 'main' THEN 0 ELSE 1 END"
            ).fetchall()
            options = []
            for row in rows:
                aid = row['account_id']
                quota_text = '额度未知'
                fresh = False
                rem = None
                if version >= 2:
                    q = tx.execute(
                        "SELECT remaining_percent, sampled_at, reset_at, identity_revision FROM account_quota_samples WHERE account_id=? AND bucket='gemini_5h'",
                        (aid,),
                    ).fetchone()
                    if q and q['identity_revision'] == row['identity_revision']:
                        q_dt = parse_utc(q['sampled_at'])
                        q_reset = parse_utc(q['reset_at'])
                        rem = float(q['remaining_percent'])
                        age = (now_dt - q_dt).total_seconds() if q_dt else 1e9
                        fresh = age <= self.quota_ttl_seconds and (q_reset is None or now_dt < q_reset)
                        if fresh:
                            quota_text = f'5h {rem:.0f}%'
                        else:
                            mins = max(1, int(age // 60))
                            quota_text = f'5h {rem:.0f}% · 上次读数({mins}m前)'
                options.append({
                    'account_id': aid,
                    'is_current': aid == current_account_id,
                    'remaining_percent': rem,
                    'fresh': fresh,
                    'quota_text': quota_text,
                })
            return options


# ------------------------------------------------------------------
# Per-Instance Private Event Mailbox & HUD View Publisher (§3.2, §9.3)
# ------------------------------------------------------------------
class EventMailbox:
    def __init__(self, store: PoolStore, scope_id: str):
        self.store = store
        self.scope_id = scope_id
        self.dir = store.artifact(f'mailboxes/{scope_id}')

    def publish(self, event: dict[str, Any]) -> Path:
        seq = int(event.get('seq', 0))
        producer = re.sub(r'[^A-Za-z0-9_-]', '_', str(event.get('producer_instance_id', 'p')))[:32]
        name = f'{seq:08d}-{producer}-{uuid.uuid4().hex[:8]}.json'
        path = self.dir / name
        write_text_atomic(path, json.dumps(event, ensure_ascii=False, allow_nan=False))
        return path

    def poll_and_consume(self, engine: M4Engine, *, cwd: Path | str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        results = []
        for path in sorted(self.dir.glob('*.json')):
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            res = engine.ingest_event(raw, cwd=cwd, expected_scope=self.scope_id, now=now)
            # ACK only after SQLite transaction commits.
            path.unlink(missing_ok=True)
            results.append(res)
        return results


class HudViewPublisher:
    """Publishes sanitized read-only HUD view projections (never credentials, emails, or packet paths)."""

    def __init__(self, store: PoolStore, launch_id: str):
        self.store = store
        self.launch_id = launch_id
        self.path = store.artifact(f'views/{launch_id}.json')

    def publish(
        self,
        *,
        conversation_id: str | None,
        thread_title: str | None,
        account_id: str | None,
        account_binding_state: str,
        hotkey_hint: str | None = None,
        hotkey_available: bool = False,
        low_quota_5h: bool = False,
        low_quota_notify: bool = False,
        status_line: str = '',
        handoff_state: str = 'idle',
        reason_code: str | None = None,
    ) -> Path:
        payload = {
            'v': 1,
            'launch_id': self.launch_id,
            'conversation_id': conversation_id,
            'thread_title': thread_title or 'todo',
            'account_id': account_id,
            'account_binding_state': account_binding_state,
            'hotkey_hint': hotkey_hint if (hotkey_available and account_binding_state == 'verified') else None,
            'hotkey_available': bool(hotkey_available and account_binding_state == 'verified'),
            'low_quota_5h': bool(low_quota_5h and account_binding_state == 'verified'),
            'low_quota_notify': bool(low_quota_notify and account_binding_state == 'verified'),
            'status_line': redact(status_line),
            'handoff_state': handoff_state,
            'reason_code': reason_code,
            'updated_at': utc_now(),
        }
        guard(payload)
        write_text_atomic(self.path, json.dumps(payload, ensure_ascii=False))
        return self.path


# ------------------------------------------------------------------
# R4 Frosted-Pill Display Width & Layout Helpers (AC-07 / F11)
# ------------------------------------------------------------------
R4_COLORS = {
    'model_bg': '#EBF3FA',
    'task_bg': '#F4E0E9',
    'task_fg': '#0F172A',
    'account_bg': '#EFEBF6',
    'account_fg': '#554176',
    'active_pool_bg': '#E8F5F3',
    'critical_bg': '#FCEEEF',
}


def display_width(text: str) -> int:
    """Calculate terminal display column width (ANSI stripped, CJK=2, combining=0)."""
    clean = _ANSI_STRIP_RE.sub('', text)
    width = 0
    for ch in clean:
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width


def truncate_display_width(text: str, max_cols: int) -> str:
    if max_cols <= 0:
        return ''
    if display_width(text) <= max_cols:
        return text
    if max_cols == 1:
        return '…'
    out = []
    used = 0
    limit = max_cols - 1
    for ch in _ANSI_STRIP_RE.sub('', text):
        w = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return ''.join(out) + '…'


def format_r4_header_and_footer(
    *,
    model_label: str,
    task_title: str,
    account_id: str | None,
    hotkey_hint: str | None,
    columns: int = 80,
) -> dict[str, Any]:
    """Compute R4 pill layout: truncate task title first, never drop account or available hotkey."""
    acc_text = account_id if account_id else '待归属'
    fixed = display_width(model_label) + display_width(acc_text) + 7
    avail_task = max(4, columns - fixed)
    shown_title = truncate_display_width(task_title or 'todo', avail_task)
    line1 = f'{model_label}   {shown_title} {acc_text}'

    footer_status = '● Idle'
    if hotkey_hint:
        combined = f'{footer_status}   {hotkey_hint}'
        if display_width(combined) <= columns:
            footer_lines = [combined]
        else:
            footer_lines = [footer_status, hotkey_hint]
    else:
        footer_lines = [footer_status]

    return {
        'line1': line1,
        'shown_title': shown_title,
        'account_label': acc_text,
        'account_bg': R4_COLORS['account_bg'],
        'account_fg': R4_COLORS['account_fg'],
        'task_bg': R4_COLORS['task_bg'],
        'task_fg': R4_COLORS['task_fg'],
        'task_bold': False,
        'footer_lines': footer_lines,
    }
