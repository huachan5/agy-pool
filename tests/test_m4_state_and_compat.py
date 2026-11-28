from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agy_pool.cli import main as cli_main
from agy_pool.m4 import (
    EventMailbox,
    M4Engine,
    M4Error,
    REASON_AUTH_EXPIRED,
    REASON_AUTH_MISMATCH,
    REASON_CONSUMER_UNBOUND,
    REASON_DELIVERY_UNVERIFIED,
    REASON_NOT_NORMAL_EXIT,
    REASON_ORIGINAL_TERMINAL_UNVERIFIED,
    REASON_PACKET_CORRUPT,
    REASON_QUOTA_ZERO,
    REASON_SHARED_SESSION,
    REASON_SPAWN_UNCERTAIN,
    SharedDomainBarrier,
)
from agy_pool.runtime import AgyProcess, identity
from agy_pool.store import PoolStore, StoreError


def _setup_env(tmp_path: Path):
    store = PoolStore(tmp_path / 'pool.db', tmp_path)
    store.init_db()
    main_id = identity('main@example.test')
    backup_id = identity('backup@example.test')
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision) VALUES ('main','Main',?,?,'active',1)",
            (main_id.hint, main_id.digest),
        )
        tx.execute(
            "INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision) VALUES ('backup','Backup',?,?,'ready',1)",
            (backup_id.hint, backup_id.digest),
        )
    barrier = SharedDomainBarrier(domain_id='test-shared-domain', barrier_dir=tmp_path / 'barriers')
    return store, M4Engine(store, barrier=barrier), barrier, main_id, backup_id


def _register_bound_t1(engine: M4Engine, tmp_path: Path, main_id, t0: datetime, *, launch_id='S01', cid='c1'):
    engine.ingest_event(
        {
            'v': 1, 'event': 'conversation_evidence', 'launch_id': launch_id, 'producer_instance_id': 'H01',
            'seq': 1, 'native_namespace': 'agy-local', 'conversation_id': cid,
            'kind': 'first_user_query', 'source': 'native_transcript', 'source_ref': 'step:0',
            'safe_goal_excerpt': '排查登录窗口问题',
        },
        cwd=tmp_path,
        now=t0,
    )
    engine.ingest_event(
        {
            'v': 1, 'event': 'conversation_evidence', 'launch_id': launch_id, 'producer_instance_id': 'H01',
            'seq': 2, 'native_namespace': 'agy-local', 'conversation_id': cid,
            'kind': 'native_title', 'source': 'native_title_event', 'safe_title': '排查登录窗口',
        },
        now=t0,
    )
    engine.ingest_event(
        {
            'v': 1, 'event': 'session_identity', 'launch_id': launch_id, 'producer_instance_id': 'H01',
            'seq': 3, 'native_namespace': 'agy-local', 'conversation_id': cid,
            'identity_hash': main_id.digest, 'source': 'native_identity',
            'source_observed_at': t0.isoformat().replace('+00:00', 'Z'),
        },
        now=t0,
    )


# ---------------------------------------------------------------------------
# F07: Full main -> backup -> main state machine, crash recovery & negative gates
# ---------------------------------------------------------------------------
def test_f07_full_roundtrip_and_delivery_verification(tmp_path):
    store, engine, barrier, main_id, backup_id = _setup_env(tmp_path)
    t0 = datetime(2026, 11, 28, 10, 0, 0, tzinfo=timezone.utc)
    _register_bound_t1(engine, tmp_path, main_id, t0)

    # Confirm switch main -> backup; repeat confirmation reuses same intent.
    intent = engine.confirm_switch_intent(
        thread_id='T1', source_segment_id='T1-seg1', source_launch_id='S01',
        terminal_binding='pane-L', target_account_id='backup',
    )
    dup_intent = engine.confirm_switch_intent(
        thread_id='T1', source_segment_id='T1-seg1', source_launch_id='S01',
        terminal_binding='pane-L', target_account_id='backup',
    )
    assert dup_intent['intent_id'] == intent['intent_id']
    assert intent['state'] == 'await_exit'

    # Abnormal exit rejected.
    with pytest.raises(M4Error) as exc:
        engine.mark_source_exited(intent['intent_id'], expected_revision=intent['revision'], normal_exit=False, source_launch_id='S01')
    assert exc.value.reason_code == REASON_NOT_NORMAL_EXIT

    # Normal /exit advances to preparing_packet.
    intent = engine.mark_source_exited(intent['intent_id'], expected_revision=intent['revision'], normal_exit=True, source_launch_id='S01')
    assert intent['state'] == 'preparing_packet'

    # Freeze packet.
    transcript = tmp_path / 'seg1.jsonl'
    transcript.write_text(json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'status': 'DONE', 'content': '修复登录'}), encoding='utf-8')
    frozen = engine.freeze_packet(intent['intent_id'], expected_revision=intent['revision'], transcript=transcript)
    intent = frozen['intent']
    pkt = frozen['packet']
    assert intent['state'] == 'packet_ready' and pkt['quality'] == 'complete'

    # Begin login -> issues challenge and writes SharedDomainBarrier.
    challenge = engine.begin_login(
        intent['intent_id'], expected_revision=intent['revision'],
        terminal_binding='pane-L', conflict_scan_ok=True, shared_conflicts=[],
    )
    assert barrier.read() is not None

    # Wrong account auth observation rejected (packet kept!).
    t1 = t0 + timedelta(seconds=10)
    engine.ingest_event(
        {
            'v': 1, 'event': 'auth_observation', 'probe_id': challenge.probe_id, 'producer_instance_id': 'P1',
            'challenge': challenge.challenge_ref, 'seq': 1, 'identity_hash': main_id.digest,
            'auth_epoch': 'ep-1', 'source': 'fixture_auth',
            'source_observed_at': (t1 + timedelta(seconds=1)).isoformat().replace('+00:00', 'Z'),
        },
        now=t1 + timedelta(seconds=1),
    )
    with pytest.raises(M4Error) as exc:
        engine.verify_target_and_prepare(
            intent['intent_id'], expected_revision=intent['revision'] + 1,
            terminal_binding='pane-L', challenge=challenge,
            consumer_context_bound=True, conflict_scan_ok=True, shared_conflicts=[],
            now=t1 + timedelta(seconds=2),
        )
    assert exc.value.reason_code == REASON_AUTH_MISMATCH

    # Explicit retry from DIFFERENT pane fails with original_terminal_unverified!
    with pytest.raises(M4Error) as exc:
        engine.retry_intent(intent['intent_id'], terminal_binding='pane-OTHER')
    assert exc.value.reason_code == REASON_ORIGINAL_TERMINAL_UNVERIFIED

    # Explicit retry from ORIGINAL pane succeeds and re-issues challenge.
    intent = engine.retry_intent(intent['intent_id'], terminal_binding='pane-L')
    challenge2 = engine.begin_login(
        intent['intent_id'], expected_revision=intent['revision'],
        terminal_binding='pane-L', conflict_scan_ok=True, shared_conflicts=[],
    )
    t2 = datetime.now(timezone.utc) + timedelta(seconds=2)
    engine.ingest_event(
        {
            'v': 1, 'event': 'auth_observation', 'probe_id': challenge2.probe_id, 'producer_instance_id': 'P2',
            'challenge': challenge2.challenge_ref, 'seq': 1, 'identity_hash': backup_id.digest,
            'auth_epoch': 'ep-2', 'source': 'fixture_auth',
            'source_observed_at': t2.isoformat().replace('+00:00', 'Z'),
        },
        now=t2,
    )

    # consumer_unbound blocks before -i is ever passed.
    with pytest.raises(M4Error) as exc:
        engine.verify_target_and_prepare(
            intent['intent_id'], expected_revision=intent['revision'] + 1,
            terminal_binding='pane-L', challenge=challenge2,
            consumer_context_bound=False, conflict_scan_ok=True, shared_conflicts=[],
            now=t2 + timedelta(seconds=1),
        )
    assert exc.value.reason_code == REASON_CONSUMER_UNBOUND

    # Retry again with consumer_context_bound=True -> prepared -> spawn_armed.
    intent = engine.retry_intent(intent['intent_id'], terminal_binding='pane-L')
    challenge3 = engine.begin_login(
        intent['intent_id'], expected_revision=intent['revision'],
        terminal_binding='pane-L', conflict_scan_ok=True, shared_conflicts=[],
    )
    t3 = datetime.now(timezone.utc) + timedelta(seconds=2)
    engine.ingest_event(
        {
            'v': 1, 'event': 'auth_observation', 'probe_id': challenge3.probe_id, 'producer_instance_id': 'P3',
            'challenge': challenge3.challenge_ref, 'seq': 1, 'identity_hash': backup_id.digest,
            'auth_epoch': 'ep-3', 'source': 'fixture_auth',
            'source_observed_at': t3.isoformat().replace('+00:00', 'Z'),
        },
        now=t3,
    )
    prep = engine.verify_target_and_prepare(
        intent['intent_id'], expected_revision=intent['revision'] + 1,
        terminal_binding='pane-L', challenge=challenge3,
        consumer_context_bound=True, conflict_scan_ok=True, shared_conflicts=[],
        now=t3 + timedelta(seconds=1),
    )
    attempt = prep['attempt']
    armed = engine.arm_spawn(
        intent['intent_id'], attempt['attempt_id'],
        terminal_binding='pane-L', consumer_context_bound=True,
        conflict_scan_ok=True, shared_conflicts=[],
        now=t3 + timedelta(seconds=2),
    )
    assert pkt['relative_path'].split('/')[-1] in armed['prompt']

    # Simulate uncertain spawn outcome -> retry MUST be forbidden (no double launch!).
    engine.record_spawn_outcome(attempt['attempt_id'], outcome='uncertain', reason='pid_missing')
    with pytest.raises(M4Error) as exc:
        engine.retry_intent(intent['intent_id'], terminal_binding='pane-L')
    assert exc.value.reason_code == REASON_SPAWN_UNCERTAIN

    # Late new_conversation arrives AFTER auth TTL expired -> still reconciles once, advances to #2, preserves title!
    t_late = t3 + timedelta(minutes=30)
    consumed = engine.ingest_event(
        {
            'v': 1, 'event': 'new_conversation', 'launch_id': attempt['launch_id'],
            'attempt_id': attempt['attempt_id'], 'producer_instance_id': 'H02', 'seq': 1,
            'native_namespace': 'agy-local', 'conversation_id': 'c2',
            'identity_hash': backup_id.digest, 'auth_epoch': 'ep-3',
            'source': 'fixture_new_conversation', 'source_ref': 'frame:2',
            'source_observed_at': (t3 + timedelta(seconds=5)).isoformat().replace('+00:00', 'Z'),
        },
        now=t_late,
    )
    assert consumed['status'] == 'consumed'
    assert consumed['segment_id'] == 'T1-seg2'
    assert consumed['delivery_state'] == REASON_DELIVERY_UNVERIFIED
    assert barrier.read() is None

    # #2 native title must NOT overwrite T1's original #1 title!
    engine.ingest_event(
        {
            'v': 1, 'event': 'conversation_evidence', 'launch_id': attempt['launch_id'],
            'producer_instance_id': 'H02', 'seq': 2, 'native_namespace': 'agy-local',
            'conversation_id': 'c2', 'kind': 'native_title', 'source': 'native_title_event',
            'safe_title': '第二棒新标题不应覆盖',
        },
        now=t_late,
    )
    assert store.thread_status_readonly('T1')['thread']['title'] == '排查登录窗口'

    # Model self-report is NOT accepted as packet_read evidence.
    fake_read = engine.ingest_event(
        {
            'v': 1, 'event': 'packet_read', 'launch_id': attempt['launch_id'],
            'attempt_id': attempt['attempt_id'], 'producer_instance_id': 'H02', 'seq': 3,
            'native_namespace': 'agy-local', 'conversation_id': 'c2',
            'packet_digest': pkt['sha256'], 'source': 'model_self_report', 'source_ref': 'msg:1',
        },
        now=t_late,
    )
    assert fake_read['status'] == 'ignored'

    # Verified native packet_read transitions delivery_state to 'delivered'.
    real_read = engine.ingest_event(
        {
            'v': 1, 'event': 'packet_read', 'launch_id': attempt['launch_id'],
            'attempt_id': attempt['attempt_id'], 'producer_instance_id': 'H02', 'seq': 4,
            'native_namespace': 'agy-local', 'conversation_id': 'c2',
            'packet_digest': pkt['sha256'], 'source': 'native_file_read', 'source_ref': 'step:3',
        },
        now=t_late,
    )
    assert real_read['delivery_state'] == 'delivered'
    assert store.thread_status_readonly('T1')['delivery_state'] == 'delivered'


def test_f07_target_fresh_quota_zero_and_packet_tamper_and_cancel_after_spawn(tmp_path):
    store, engine, _, main_id, backup_id = _setup_env(tmp_path)
    t0 = datetime.now(timezone.utc)
    _register_bound_t1(engine, tmp_path, main_id, t0)

    # Record fresh 0% quota for backup.
    with store.transaction() as tx:
        tx.execute(
            '''INSERT INTO account_quota_samples(
                   account_id, identity_revision, bucket, remaining_percent,
                   sampled_at, received_at, reset_at, window_key, source_kind, source_launch_id
               ) VALUES ('backup', 1, 'gemini_5h', 0.0, ?, ?, NULL, 'w0', 'fixture', 'S01')''',
            (t0.isoformat().replace('+00:00', 'Z'), t0.isoformat().replace('+00:00', 'Z')),
        )

    intent = engine.confirm_switch_intent(
        thread_id='T1', source_segment_id='T1-seg1', source_launch_id='S01',
        terminal_binding='pane-L', target_account_id='backup', source_exited=True,
    )
    frozen = engine.freeze_packet(intent['intent_id'], expected_revision=intent['revision'])
    intent = frozen['intent']
    pkt = frozen['packet']

    # Tampering packet blocks begin_login with packet_corrupt.
    pkt_file = store.artifact(pkt['relative_path'])
    orig_bytes = pkt_file.read_bytes()
    pkt_file.write_text('TAMPERED', encoding='utf-8')
    with pytest.raises(M4Error) as exc:
        engine.begin_login(intent['intent_id'], expected_revision=intent['revision'], terminal_binding='pane-L', conflict_scan_ok=True, shared_conflicts=[])
    assert exc.value.reason_code == REASON_PACKET_CORRUPT

    # Restore packet, retry, verify backup with 0% fresh quota -> blocked with quota_zero!
    pkt_file.write_bytes(orig_bytes)
    intent = engine.retry_intent(intent['intent_id'], terminal_binding='pane-L')
    ch = engine.begin_login(intent['intent_id'], expected_revision=intent['revision'], terminal_binding='pane-L', conflict_scan_ok=True, shared_conflicts=[])
    t1 = datetime.now(timezone.utc) + timedelta(seconds=2)
    engine.ingest_event(
        {
            'v': 1, 'event': 'auth_observation', 'probe_id': ch.probe_id, 'producer_instance_id': 'P1',
            'challenge': ch.challenge_ref, 'seq': 1, 'identity_hash': backup_id.digest,
            'auth_epoch': 'ep-q0', 'source': 'fixture_auth',
            'source_observed_at': t1.isoformat().replace('+00:00', 'Z'),
        },
        now=t1,
    )
    with pytest.raises(M4Error) as exc:
        engine.verify_target_and_prepare(
            intent['intent_id'], expected_revision=intent['revision'] + 1,
            terminal_binding='pane-L', challenge=ch,
            consumer_context_bound=True, conflict_scan_ok=True, shared_conflicts=[],
            now=t1 + timedelta(seconds=1),
        )
    assert exc.value.reason_code == REASON_QUOTA_ZERO


# ---------------------------------------------------------------------------
# F08: M4 shared login conflict hard-blocks even with --force
# ---------------------------------------------------------------------------
def test_f08_m4_shared_conflict_hard_blocks_even_with_force(tmp_path, monkeypatch, capsys):
    from tests.test_cli_e2e import FakeRuntime

    monkeypatch.setenv('AGY_POOL_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('AGY_POOL_BARRIER_DIR', str(tmp_path / 'barriers'))
    monkeypatch.delenv('AGY_POOL_DB', raising=False)
    monkeypatch.chdir(tmp_path)

    store = PoolStore(tmp_path / 'data' / 'pool.db', tmp_path / 'data')
    store.init_db()
    main_id = identity('main@example.test')
    backup_id = identity('backup@example.test')
    with store.transaction() as tx:
        tx.execute("INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision) VALUES ('main','Main',?,?,'active',1)", (main_id.hint, main_id.digest))
        tx.execute("INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision) VALUES ('backup','Backup',?,?,'ready',1)", (backup_id.hint, backup_id.digest))

    engine = M4Engine(store, barrier=SharedDomainBarrier(barrier_dir=tmp_path / 'barriers'))
    t0 = datetime.now(timezone.utc)
    _register_bound_t1(engine, tmp_path, main_id, t0)
    with store.transaction() as tx:
        tx.execute("UPDATE segments SET status='closed' WHERE segment_id='T1-seg1'")

    fake = FakeRuntime()
    fake.procs = [AgyProcess(999, 134400000000000000)]  # Another active agy session!
    code = cli_main(['switch', 'T1', '--to', 'backup', '--force'], runtime=fake)
    assert code == 3
    assert 'shared_session' in capsys.readouterr().err
    assert len(fake.launches) == 0


# ---------------------------------------------------------------------------
# F09 & F10: Dual-pane isolation & strict read-only list/status (mode=ro + query_only)
# ---------------------------------------------------------------------------
def test_f09_f10_dual_pane_isolation_and_strict_readonly_queries(tmp_path, monkeypatch, capsys):
    # F10: Missing DB -> read-only list/status NEVER creates DB or directory!
    missing_home = tmp_path / 'never-created'
    monkeypatch.setenv('AGY_POOL_HOME', str(missing_home))
    monkeypatch.delenv('AGY_POOL_DB', raising=False)
    assert cli_main(['list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == {'active_machine_account': None, 'accounts': [], 'threads': []}
    assert not missing_home.exists()
    assert cli_main(['status', 'T1', '--json']) == 1
    assert not missing_home.exists()
    capsys.readouterr()

    # F09: Same cwd + same account in two panes -> two distinct TIDs; ordinary new session never claims pending packet.
    data_home = tmp_path / 'data'
    monkeypatch.setenv('AGY_POOL_HOME', str(data_home))
    monkeypatch.setenv('AGY_POOL_BARRIER_DIR', str(tmp_path / 'barriers'))
    store = PoolStore(data_home / 'pool.db', data_home)
    store.init_db()
    main_id = identity('main@example.test')
    backup_id = identity('backup@example.test')
    with store.transaction() as tx:
        tx.execute("INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision) VALUES ('main','Main',?,?,'active',1)", (main_id.hint, main_id.digest))
        tx.execute("INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision) VALUES ('backup','Backup',?,?,'ready',1)", (backup_id.hint, backup_id.digest))

    engine = M4Engine(store, barrier=SharedDomainBarrier(barrier_dir=tmp_path / 'barriers'))
    t0 = datetime.now(timezone.utc)
    _register_bound_t1(engine, tmp_path, main_id, t0, launch_id='S-LEFT', cid='conv-left')
    # Right pane opens an ordinary new session in the SAME cwd with SAME account -> T2 (does not touch T1).
    reg_right = engine.ingest_event(
        {
            'v': 1, 'event': 'conversation_evidence', 'launch_id': 'S-RIGHT', 'producer_instance_id': 'H-RIGHT',
            'seq': 1, 'native_namespace': 'agy-local', 'conversation_id': 'conv-right',
            'kind': 'first_user_query', 'source': 'native_transcript', 'source_ref': 'step:0',
        },
        cwd=tmp_path,
        now=t0,
    )
    assert reg_right['thread_id'] == 'T2'
    assert reg_right['account_binding_state'] == 'unverified'

    # Place a dummy probe file to ensure list/status NEVER delete probes or call reconcile/scan_agy_processes!
    probe_file = store.artifact('probes/seg-T1-seg1.json')
    probe_file.write_text('{}', encoding='utf-8')

    class ExplodingRuntime:
        def scan_agy_processes(self):
            raise AssertionError('readonly query must never scan processes')
        def is_process_alive(self, *a):
            raise AssertionError('readonly query must never check process liveness')

    assert cli_main(['list', '--json'], runtime=ExplodingRuntime()) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed['threads']) == 2
    assert probe_file.is_file()  # Probe untouched!

    # Unverified T2 status --json outputs account_id: null + account_binding_state: "unverified"
    assert cli_main(['status', 'T2', '--json'], runtime=ExplodingRuntime()) == 0
    st = json.loads(capsys.readouterr().out)
    assert st['current_segment']['account_id'] is None
    assert st['current_segment']['account_binding_state'] == 'unverified'
    assert 'handoff_state' in st and 'pending_intent' in st and 'delivery_state' in st

    # Text status displays "账号待归属"
    assert cli_main(['status', 'T2'], runtime=ExplodingRuntime()) == 0
    assert '账号待归属' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# F12 & F13: v1 -> v2 migration, duplicate ID quarantine, cold backup/restore, cross-DB barrier
# ---------------------------------------------------------------------------
def test_f12_v1_to_v2_migration_quarantine_and_cold_restore(tmp_path):
    db_path = tmp_path / 'pool.db'
    # Construct a realistic v1 database with unique + duplicate legacy conversation_ids.
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute('''CREATE TABLE accounts (
        account_id TEXT PRIMARY KEY, label TEXT, email_hint TEXT, email_hash TEXT UNIQUE,
        status TEXT NOT NULL, gemini_5h_remaining REAL, gemini_5h_reset_at TEXT,
        gemini_7d_remaining REAL, quota_updated_at TEXT, cooldown_until TEXT)''')
    conn.execute('''CREATE TABLE threads (
        thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL,
        anchor_path TEXT NOT NULL, status TEXT NOT NULL, current_segment_id TEXT,
        segment_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE segments (
        segment_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(thread_id),
        seq INTEGER NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(account_id),
        parent_segment_id TEXT, conversation_id TEXT, transcript_path TEXT, handoff_in_path TEXT,
        handoff_out_path TEXT, pid INTEGER, pid_create_time INTEGER, status TEXT NOT NULL,
        observed_email_hint TEXT, observed_email_hash TEXT, started_at TEXT NOT NULL,
        ended_at TEXT, last_heartbeat_at TEXT)''')
    conn.execute('''CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event_type TEXT NOT NULL,
        thread_id TEXT, segment_id TEXT, account_id TEXT, detail_json TEXT)''')
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    conn.execute("INSERT INTO meta VALUES ('next_thread_seq', '3')")
    conn.execute("INSERT INTO accounts(account_id,email_hint,email_hash,status) VALUES ('main','m***@e.test','hash-m','active')")
    conn.execute("INSERT INTO threads VALUES ('T1','旧手工任务','/tmp','anchors/T1.md','active','T1-seg1',1,'2026-11-27T00:00:00Z','2026-11-27T00:00:00Z')")
    conn.execute("INSERT INTO threads VALUES ('T2','旧重复任务','/tmp','anchors/T2.md','active','T2-seg1',1,'2026-11-27T00:00:00Z','2026-11-27T00:00:00Z')")
    conn.execute("INSERT INTO segments VALUES ('T1-seg1','T1',1,'main',NULL,'unique-c1',NULL,NULL,NULL,NULL,NULL,'closed',NULL,NULL,'2026-11-27T00:00:00Z',NULL,NULL)")
    conn.execute("INSERT INTO segments VALUES ('T1-seg2','T1',2,'main','T1-seg1','dup-c2',NULL,NULL,NULL,NULL,NULL,'closed',NULL,NULL,'2026-11-27T00:01:00Z',NULL,NULL)")
    conn.execute("INSERT INTO segments VALUES ('T2-seg1','T2',1,'main',NULL,'dup-c2',NULL,NULL,NULL,NULL,NULL,'closed',NULL,NULL,'2026-11-27T00:02:00Z',NULL,NULL)")
    conn.commit()
    conn.close()

    # Read-only query works on v1 BEFORE migration without upgrading it!
    store = PoolStore(db_path, tmp_path)
    snap_v1 = store.export_snapshot()
    assert len(snap_v1['threads']) == 2
    with sqlite3.connect(db_path) as check_conn:
        assert check_conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '1'

    # Now migrate v1 -> v2.
    store.init_db()
    bak_v1 = db_path.with_name('pool.db.v1.bak')
    assert bak_v1.is_file()

    with store.readonly_transaction() as (tx, ver):
        assert ver == 2
        claims = [dict(r) for r in tx.execute('SELECT native_id, segment_id FROM conversation_claims').fetchall()]
        assert claims == [{'native_id': 'unique-c1', 'segment_id': 'T1-seg1'}]
        quarantined = {r['segment_id'] for r in tx.execute('SELECT segment_id FROM legacy_quarantine').fetchall()}
        assert quarantined == {'T1-seg2', 'T2-seg1'}

    # CHECK constraint: non-M4 or seq>1 cannot have account_id=NULL.
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as tx:
            tx.execute(
                "INSERT INTO segments(segment_id,thread_id,seq,account_id,status,started_at,segment_origin,account_binding_state) VALUES ('T1-seg3','T1',3,NULL,'running','2026-11-28T00:00:00Z','m4_native','unverified')"
            )

    # Cold restore to isolated path restores v1 cleanly.
    restored = PoolStore.restore_backup(bak_v1, tmp_path / 'rollback' / 'pool.v1.db')
    with sqlite3.connect(restored) as rconn:
        assert rconn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '1'


def test_f13_cross_db_shared_domain_barrier(tmp_path):
    barrier = SharedDomainBarrier(domain_id='shared-auth', barrier_dir=tmp_path / 'barriers')
    store_a = PoolStore(tmp_path / 'a.db', tmp_path)
    store_b = PoolStore(tmp_path / 'b.db', tmp_path)
    store_a.init_db()
    store_b.init_db()

    barrier.write(db_id=store_a.db_id, intent_id='INT-A1', stage='await_login')
    # Same DB + same intent allowed; different DB B is hard-blocked!
    barrier.assert_clear_for(store_a.db_id, 'INT-A1')
    with pytest.raises(M4Error) as exc:
        barrier.assert_clear_for(store_b.db_id)
    assert exc.value.reason_code == REASON_SHARED_SESSION

    # Clearing from wrong DB fails; clearing from owning DB + intent succeeds.
    assert barrier.clear(db_id=store_b.db_id, intent_id='INT-A1') is False
    assert barrier.clear(db_id=store_a.db_id, intent_id='INT-A1') is True
    barrier.assert_clear_for(store_b.db_id)
