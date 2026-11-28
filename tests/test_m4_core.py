from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agy_pool.handoff import HandoffBuilder, NoHandoffEvidence, build_handoff_prompt, estimate_tokens
from agy_pool.m4 import (
    EventMailbox,
    HudViewPublisher,
    M4Engine,
    M4Error,
    REASON_EVENT_CONFLICT,
    REASON_IDENTITY_UNVERIFIED,
    SharedDomainBarrier,
    display_width,
    format_r4_header_and_footer,
    truncate_display_width,
)
from agy_pool.pty_proxy import AccountMenu, InputStreamParser
from agy_pool.runtime import identity
from agy_pool.store import PoolStore, SensitiveData


def _setup_store(tmp_path: Path):
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
    barrier = SharedDomainBarrier(domain_id='test-domain', barrier_dir=tmp_path / 'barriers')
    return store, M4Engine(store, barrier=barrier), main_id, backup_id


# ---------------------------------------------------------------------------
# F01 & F02: Auto-registration, identity decoupling, and #1-only native title
# ---------------------------------------------------------------------------
def test_f01_first_query_registration_and_identity_decoupling(tmp_path):
    store, engine, main_id, _ = _setup_store(tmp_path)
    t0 = datetime(2026, 11, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Non-first-query sources (empty init, exit, approval, login probe) must NOT register.
    for idx, src in enumerate(('empty_init', 'exit_cmd', 'approval', 'login_probe', 'handoff_prompt', 'partial_tail'), 1):
        res = engine.ingest_event(
            {
                'v': 1,
                'event': 'conversation_evidence',
                'launch_id': 'S01',
                'producer_instance_id': 'H01',
                'seq': idx,
                'native_namespace': 'agy-local',
                'conversation_id': 'conv-1',
                'kind': 'first_user_query',
                'source': src,
                'source_ref': f'step:{idx}',
            },
            cwd=tmp_path,
            now=t0,
        )
        assert res['status'] == 'ignored'
    assert store.list_threads() == []

    # Valid first_user_query registers T1/#1 with account_id=NULL (unverified) even without identity event.
    reg = engine.ingest_event(
        {
            'v': 1,
            'event': 'conversation_evidence',
            'launch_id': 'S01',
            'producer_instance_id': 'H01',
            'seq': 10,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-1',
            'kind': 'first_user_query',
            'source': 'native_transcript',
            'source_ref': 'step:0',
        },
        cwd=tmp_path,
        now=t0,
    )
    assert reg['status'] == 'registered'
    assert reg['thread_id'] == 'T1'
    assert reg['segment_id'] == 'T1-seg1'
    assert reg['account_binding_state'] == 'unverified'

    # Unverified #1 cannot initiate switch.
    with pytest.raises(M4Error) as exc:
        engine.confirm_switch_intent(
            thread_id='T1',
            source_segment_id='T1-seg1',
            source_launch_id='S01',
            terminal_binding='pane-1',
            target_account_id='backup',
        )
    assert exc.value.reason_code == REASON_IDENTITY_UNVERIFIED

    # Duplicate redraw / replay from another producer or cross-pane resume does not create a second thread.
    dup = engine.ingest_event(
        {
            'v': 1,
            'event': 'conversation_evidence',
            'launch_id': 'S99',
            'producer_instance_id': 'H99',
            'seq': 1,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-1',
            'kind': 'first_user_query',
            'source': 'native_transcript',
            'source_ref': 'step:0',
        },
        cwd=tmp_path,
        now=t0,
    )
    assert dup['status'] == 'existing'
    assert dup['thread_id'] == 'T1'
    assert len(store.list_threads()) == 1

    # Same (scope, producer, seq) with DIFFERENT payload is quarantined.
    conflict = engine.ingest_event(
        {
            'v': 1,
            'event': 'conversation_evidence',
            'launch_id': 'S01',
            'producer_instance_id': 'H01',
            'seq': 10,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-TAMPERED',
            'kind': 'first_user_query',
            'source': 'native_transcript',
            'source_ref': 'step:0',
        },
        cwd=tmp_path,
        now=t0,
    )
    assert conflict['status'] == 'quarantined'
    assert conflict['reason'] == REASON_EVENT_CONFLICT

    # Late session_identity binds the exact same T1-seg1 atomically without creating a new thread/segment.
    bound = engine.ingest_event(
        {
            'v': 1,
            'event': 'session_identity',
            'launch_id': 'S01',
            'producer_instance_id': 'H01',
            'seq': 11,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-1',
            'identity_hash': main_id.digest,
            'source': 'native_identity',
            'source_observed_at': '2026-11-28T10:00:05Z',
        },
        now=t0 + timedelta(seconds=5),
    )
    assert bound['status'] == 'bound'
    assert bound['thread_id'] == 'T1'
    assert bound['segment_id'] == 'T1-seg1'
    assert bound['account_id'] == 'main'


def test_f02_title_only_backfilled_from_segment1_native_title(tmp_path):
    store, engine, _, _ = _setup_store(tmp_path)
    t0 = datetime(2026, 11, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Register T1 without identity yet (unverified).
    engine.ingest_event(
        {
            'v': 1,
            'event': 'conversation_evidence',
            'launch_id': 'S01',
            'producer_instance_id': 'H01',
            'seq': 1,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-title-1',
            'kind': 'first_user_query',
            'source': 'native_transcript',
            'source_ref': 'step:0',
        },
        cwd=tmp_path,
        now=t0,
    )
    assert store.list_threads()[0]['title'] == 'todo'

    # Non-native title sources (first_user_query, model_label) must be ignored.
    for idx, src in enumerate(('first_user_query', 'model_label', 'placeholder'), 2):
        res = engine.ingest_event(
            {
                'v': 1,
                'event': 'conversation_evidence',
                'launch_id': 'S01',
                'producer_instance_id': 'H01',
                'seq': idx,
                'native_namespace': 'agy-local',
                'conversation_id': 'conv-title-1',
                'kind': 'native_title',
                'source': src,
                'safe_title': '不应该生效',
            },
            now=t0,
        )
        assert res['status'] == 'ignored'
    assert store.list_threads()[0]['title'] == 'todo'

    # Native title with ANSI control chars is sanitized and works even while account_id is still NULL.
    upd = engine.ingest_event(
        {
            'v': 1,
            'event': 'conversation_evidence',
            'launch_id': 'S01',
            'producer_instance_id': 'H01',
            'seq': 10,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-title-1',
            'kind': 'native_title',
            'source': 'native_title_event',
            'safe_title': '\x1b[31m排查登录窗口\x1b[0m\n',
            'source_observed_at': '2026-11-28T10:00:09Z',
        },
        now=t0,
    )
    assert upd['status'] == 'title_updated'
    assert store.list_threads()[0]['title'] == '排查登录窗口'

    # Sensitive title (email/token) is rejected.
    rej = engine.ingest_event(
        {
            'v': 1,
            'event': 'conversation_evidence',
            'launch_id': 'S01',
            'producer_instance_id': 'H01',
            'seq': 11,
            'native_namespace': 'agy-local',
            'conversation_id': 'conv-title-1',
            'kind': 'native_title',
            'source': 'native_title_event',
            'safe_title': '排查 user@example.com 登录',
        },
        now=t0,
    )
    assert rej['status'] == 'rejected'
    assert store.list_threads()[0]['title'] == '排查登录窗口'


# ---------------------------------------------------------------------------
# F03: Quota boundaries, freshness, attribution, 7d/3P isolation & alert dedup
# ---------------------------------------------------------------------------
def test_f03_quota_thresholds_freshness_and_window_dedup(tmp_path):
    store, engine, main_id, backup_id = _setup_store(tmp_path)
    t0 = datetime(2026, 11, 28, 10, 0, 0, tzinfo=timezone.utc)

    # Register & bind T1-seg1 to main.
    engine.ingest_event(
        {
            'v': 1, 'event': 'conversation_evidence', 'launch_id': 'S01', 'producer_instance_id': 'H01',
            'seq': 1, 'native_namespace': 'agy-local', 'conversation_id': 'c-q1',
            'kind': 'first_user_query', 'source': 'native_transcript', 'source_ref': 'step:0',
        },
        cwd=tmp_path,
        now=t0,
    )
    engine.ingest_event(
        {
            'v': 1, 'event': 'session_identity', 'launch_id': 'S01', 'producer_instance_id': 'H01',
            'seq': 2, 'native_namespace': 'agy-local', 'conversation_id': 'c-q1',
            'identity_hash': main_id.digest, 'source': 'native_id', 'source_observed_at': '2026-11-28T10:00:01Z',
        },
        now=t0,
    )

    def send_quota(seq, rem, *, pool='gemini', bucket='5h', win='win-1', observed='2026-11-28T10:00:30Z',
                   reset='2026-11-28T14:00:00Z', id_hash=main_id.digest, rev=1, now=t0 + timedelta(seconds=40)):
        return engine.ingest_event(
            {
                'v': 1, 'event': 'quota_sample', 'launch_id': 'S01', 'producer_instance_id': 'H01',
                'seq': seq, 'identity_hash': id_hash, 'identity_revision': rev,
                'source': 'fixture_quota', 'pool': pool, 'bucket': bucket,
                'remaining_percent': rem, 'source_observed_at': observed,
                'window_key': win, 'reset_at': reset,
            },
            now=now,
        )

    # 5.1% and 5.001% do NOT trigger.
    assert send_quota(10, 5.1)['low_quota_active'] is False
    assert send_quota(11, 5.001)['low_quota_active'] is False

    # 7d and 3P low quota NEVER trigger M4 handoff alert.
    r7d = send_quota(12, 0.0, pool='gemini', bucket='7d')
    assert r7d['low_quota_active'] is False and r7d['should_notify'] is False
    r3p = send_quota(13, 0.0, pool='3p', bucket='5h')
    assert r3p['low_quota_active'] is False and r3p['should_notify'] is False

    # Wrong account quota does NOT trigger alert for main's session.
    r_wrong = send_quota(14, 1.0, id_hash=backup_id.digest)
    assert r_wrong['low_quota_active'] is False and r_wrong['should_notify'] is False

    # Invalid values (negative, >100, stale, future) ignored.
    assert send_quota(15, -1.0)['status'] == 'ignored'
    assert send_quota(16, 101.0)['status'] == 'ignored'
    assert send_quota(17, 2.0, observed='2026-11-28T09:00:00Z')['low_quota_active'] is False  # stale > 300s
    assert send_quota(18, 2.0, observed='2026-11-28T11:00:00Z')['status'] == 'ignored'       # future

    # Missing window_key with fresh <= 5.0%: low_quota_active=True, should_notify=False (no fabricated window).
    r_nowin = send_quota(19, 4.999, win=None, reset=None)
    assert r_nowin['low_quota_active'] is True
    assert r_nowin['should_notify'] is False

    # Exact 5.0% with valid window_key triggers notification ONCE.
    r_hit = send_quota(20, 5.0, win='win-1')
    assert r_hit['low_quota_active'] is True
    assert r_hit['should_notify'] is True

    # Repeat redraw in same window: still low_quota_active=True, but should_notify=False.
    r_dup = send_quota(21, 0.0, win='win-1')
    assert r_dup['low_quota_active'] is True
    assert r_dup['should_notify'] is False

    # Next reset window notifies once again.
    r_next = send_quota(22, 4.5, win='win-2')
    assert r_next['low_quota_active'] is True
    assert r_next['should_notify'] is True

    # Zero switch_intents created by alerts.
    with store.readonly_transaction() as (tx, _):
        assert tx.execute('SELECT COUNT(*) FROM switch_intents').fetchone()[0] == 0

    # Menu options reflect fresh vs stale/unknown quota without treating unknown as 0%.
    opts = engine.get_menu_options('main', now=t0 + timedelta(seconds=50))
    assert [o['account_id'] for o in opts] == ['main', 'backup']
    assert opts[0]['is_current'] is True and opts[0]['fresh'] is True
    stale_opts = engine.get_menu_options('main', now=t0 + timedelta(minutes=20))
    assert '上次读数' in stale_opts[0]['quota_text']


# ---------------------------------------------------------------------------
# F04 & F05: CSI-u input parser (flag 5 + flag 1 + fragmentation + paste) & AccountMenu
# ---------------------------------------------------------------------------
def test_f04_f05_csi_u_parser_and_account_menu():
    # macOS Otty: S06 flag 5 (ESC[121:89;10u) and flag 1 (ESC[121;10u)
    parser = InputStreamParser(platform='darwin', enabled=True)

    # Normal 'y' (0x79) and Shift+Tab (ESC[9;2u]) and CSI-u query reply (ESC[?5u]) pass through verbatim.
    r1 = parser.feed(b'y\x1b[9;2u\x1b[?5u')
    assert r1.hotkey_triggered is False
    assert r1.forward_bytes == b'y\x1b[9;2u\x1b[?5u'

    # Flag 5 hotkey split byte-by-byte across read() calls!
    seq = b'\x1b[121:89;10u'
    triggered = False
    forwarded = b''
    for byte in seq:
        res = parser.feed(bytes([byte]))
        forwarded += res.forward_bytes
        triggered = triggered or res.hotkey_triggered
    assert triggered is True
    assert forwarded == b''

    # Flag 1 hotkey also recognized.
    r_flag1 = parser.feed(b'\x1b[121;10u')
    assert r_flag1.hotkey_triggered is True and r_flag1.forward_bytes == b''

    # Bracketed paste containing hotkey bytes must NOT trigger hotkey; passed verbatim!
    paste = b'\x1b[200~hello \x1b[121:89;10u world\x1b[201~'
    r_paste = parser.feed(paste)
    assert r_paste.hotkey_triggered is False
    assert r_paste.forward_bytes == paste

    # Windows Terminal Ctrl+Shift+Y (modifier 6)
    win_parser = InputStreamParser(platform='win32', enabled=True)
    assert win_parser.feed(b'\x1b[121;6u').hotkey_triggered is True
    assert win_parser.feed(b'\x1b[121:89;6u').hotkey_triggered is True
    assert win_parser.feed(b'y').hotkey_triggered is False

    # Disabled hotkey router never intercepts.
    disabled = InputStreamParser(platform='darwin', enabled=False)
    r_dis = disabled.feed(b'\x1b[121:89;10u')
    assert r_dis.hotkey_triggered is False
    assert r_dis.forward_bytes == b'\x1b[121:89;10u'

    # Two-row AccountMenu: default focus on current, arrows only move, Esc/current-Enter = None, other-Enter = target.
    menu = AccountMenu([
        {'account_id': 'main', 'is_current': True, 'quota_text': '5h 5%'},
        {'account_id': 'backup', 'is_current': False, 'quota_text': '5h 86%'},
    ])
    assert menu.focus == 0
    assert '↑/↓ 选择' in menu.render()
    closed, target = menu.handle_key(b'\r')  # Enter on current -> None
    assert closed is True and target is None
    menu2 = AccountMenu([
        {'account_id': 'main', 'is_current': True, 'quota_text': '5h 5%'},
        {'account_id': 'backup', 'is_current': False, 'quota_text': '5h 86%'},
    ])
    assert menu2.handle_key(b'\x1b[B') == (False, None)  # Down to backup
    assert menu2.focus == 1
    assert menu2.handle_key(b'\x1b') == (True, None)     # Esc -> None
    assert menu2.handle_key(b'\r') == (True, 'backup')   # Enter on backup -> confirmed


# ---------------------------------------------------------------------------
# F06: M4 Handoff packet quality, planned vs confirmed, budget <= 3500, safe -i prompt
# ---------------------------------------------------------------------------
def test_f06_m4_handoff_packet_and_prompt_guards(tmp_path):
    builder = HandoffBuilder()

    # Zero usable evidence -> raises NoHandoffEvidence.
    with pytest.raises(NoHandoffEvidence):
        builder.build_m4('', anchor_is_placeholder=True)

    # Truncated tail + planned tool call -> partial quality, planned tool calls NOT claimed as succeeded, redacted & <= 3500 tokens.
    transcript = tmp_path / 'transcript.jsonl'
    rows = [
        {'step_index': 0, 'type': 'USER_INPUT', 'status': 'DONE', 'content': '排查 auth.py Bearer secret-xyz'},
        {
            'step_index': 1, 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
            'content': '准备修改文件 ' + '中文测试' * 3000,
            'tool_calls': [{'name': 'edit_file', 'args': {'Path': 'src/auth.py'}}],
        },
        {'step_index': 2, 'type': 'PLANNER_RESPONSE', 'status': 'IN_PROGRESS', 'content': '未完成草稿'},
    ]
    transcript.write_text('\n'.join(map(json.dumps, rows)) + '\n{"broken_tail":', encoding='utf-8')

    res = builder.build_m4(
        '保持目标不变',
        transcript=transcript,
        task='- [x] 定位问题\n- [/] 修复登录',
        git_status=' M src/auth.py',
        provenance={'segment_id': 'T1-seg1', 'account_id': 'main'},
    )
    assert res.quality == 'partial'
    assert 'tail_incomplete' in res.evidence_flags
    assert '部分交接／待核对' in res.text
    assert '未证实写入成功' in res.text
    assert 'secret-xyz' not in res.text
    assert '未完成草稿' not in res.text
    assert res.tokens == estimate_tokens(res.text) <= 3500
    assert len(res.sha256) == 64 and res.bytes > 0

    # Safe -i prompt checks.
    good_path = tmp_path / 'handoffs' / 'PKT-123456.md'
    prompt = build_handoff_prompt(good_path)
    assert len(prompt) <= 250 and '\n' not in prompt and good_path.as_posix() in prompt

    with pytest.raises(ValueError):
        build_handoff_prompt(Path('/tmp/bad\npath.md'))


# ---------------------------------------------------------------------------
# F11: R4 Display-Width & Pill Layout Invariants
# ---------------------------------------------------------------------------
def test_f11_r4_layout_width_and_color_invariants(tmp_path):
    assert display_width('\x1b[31m排查登录\x1b[0m') == 8
    assert display_width(truncate_display_width('非常长的中文任务标题测试截断行为', 10)) <= 10

    narrow = format_r4_header_and_footer(
        model_label='Gemini 3.1',
        task_title='非常长的中英文混合任务标题 Long Task Title For Narrow Terminal',
        account_id='backup',
        hotkey_hint='切换账号 (⌘⇧Y)',
        columns=36,
    )
    assert narrow['account_label'] == 'backup'
    assert narrow['account_bg'] == '#EFEBF6' and narrow['account_fg'] == '#554176'
    assert narrow['task_bg'] == '#F4E0E9' and narrow['task_bold'] is False
    assert any('切换账号 (⌘⇧Y)' in line for line in narrow['footer_lines'])

    # HUD View publisher never leaks secrets/emails and hides hotkey when unverified.
    store, _, _, _ = _setup_store(tmp_path)
    pub = HudViewPublisher(store, 'LCH-1')
    p = pub.publish(
        conversation_id='c1',
        thread_title='todo',
        account_id=None,
        account_binding_state='unverified',
        hotkey_hint='切换账号 (⌘⇧Y)',
        hotkey_available=True,
    )
    view = json.loads(p.read_text(encoding='utf-8'))
    assert view['hotkey_available'] is False and view['hotkey_hint'] is None
