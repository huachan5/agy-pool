import json
import os
import sqlite3
from contextlib import contextmanager

import pytest


class FakeRuntime:
    def __init__(self):
        self.email = 'main@example.test'
        self.procs = []
        self.launches = []
        self.guide = None
        self.fail = False
        self.locked = False
        self.git_context = lambda cwd: (None, '')
        self.brain_root = None

    @contextmanager
    def switch_lock(self, name=None):
        from agy_pool.runtime import RuntimeIssue

        if self.locked:
            raise RuntimeIssue('已有换号', 3)
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def read_current_auth(self):
        from agy_pool.runtime import identity

        return identity(self.email)

    def verify_current_auth(self, cwd, probe, expected):
        from agy_pool.runtime import RuntimeIssue

        actual = self.read_current_auth()
        if actual.digest != expected.digest:
            raise RuntimeIssue('账号不符', 4)
        return actual

    def guide_account_switch(self, cwd, hint, probe):
        if isinstance(self.guide, BaseException):
            raise self.guide
        assert self.guide is not None
        self.email = self.guide

    def scan_agy_processes(self):
        return self.procs[:]

    def process_creation(self, pid):
        return 100 if pid == os.getpid() else next((p.created for p in self.procs if p.pid == pid), None)

    def is_process_alive(self, pid, created):
        return self.process_creation(pid) == created and created is not None

    def launch_agy(self, cwd, env, prompt, on_started):
        from agy_pool.runtime import AgyProcess, RuntimeIssue

        proc = AgyProcess(1000 + len(self.launches), 200 + len(self.launches))
        self.launches.append((env, prompt))
        on_started(proc)
        if self.fail:
            raise RuntimeIssue('3 秒内退出')
        self.procs.append(proc)
        return proc


@pytest.fixture
def pool(tmp_path, monkeypatch):
    from agy_pool.cli import main

    monkeypatch.setenv('AGY_POOL_HOME', str(tmp_path / 'data'))
    monkeypatch.delenv('AGY_POOL_DB', raising=False)
    monkeypatch.delenv('AGY_POOL_THREAD_ID', raising=False)
    monkeypatch.chdir(tmp_path)
    fake = FakeRuntime()
    fake.brain_root = tmp_path / 'brain-fixture'
    call = lambda *args: main(list(args), runtime=fake)
    assert call('account', 'add', 'main', '--from-current') == 0
    fake.email = 'backup@example.test'
    assert call('account', 'add', 'backup', '--from-current') == 0
    fake.email = 'main@example.test'
    assert call('start', '任务甲', '--account', 'main') == 0
    return call, fake, tmp_path / 'data'


def segment_state(path, segment='T1-seg1'):
    with sqlite3.connect(path / 'pool.db') as conn:
        return conn.execute('SELECT status FROM segments WHERE segment_id=?', (segment,)).fetchone()[0]


def test_source_cannot_be_forced_into_account_switch(pool):
    call, fake, home = pool
    assert call('switch', 'T1', '--to', 'backup', '--force') == 6
    assert segment_state(home) == 'running'
    assert (home / 'handoffs' / 'T1-seg1.md').is_file()
    assert len(fake.launches) == 1


def test_cancel_has_no_target_and_does_not_reset_actual_account(pool, capsys):
    call, fake, home = pool
    fake.procs.clear()
    fake.guide = KeyboardInterrupt()
    assert call('switch', 'T1', '--to', 'backup') == 130
    bundle = home / 'handoffs' / 'T1-seg1.md'
    frozen = bundle.read_bytes()
    fake.email = 'backup@example.test'
    assert call('switch', 'T1', '--cancel') == 0
    assert '不改变' in capsys.readouterr().out
    assert segment_state(home) == 'closed'
    assert bundle.read_bytes() == frozen
    assert call('start', 'not assigned to main', '--account', 'main') == 4
    assert fake.email == 'backup@example.test'
    assert len(fake.launches) == 1


def test_wrong_account_cannot_launch_and_correct_retry_reuses_bundle(pool, capsys):
    call, fake, home = pool
    fake.procs.clear()
    fake.guide = 'main@example.test'
    assert call('switch', 'T1', '--to', 'backup') == 4
    assert segment_state(home) == 'handoff_pending'
    frozen = (home / 'handoffs' / 'T1-seg1.md').read_bytes()
    assert call('anchor', 'edit', 'T1', '--text', '不能覆盖已冻结的交接') == 2
    fake.guide = 'backup@example.test'
    assert call('switch', 'T1', '--to', 'backup') == 0
    assert segment_state(home) == 'handed_off'
    assert (home / 'handoffs' / 'T1-seg1.md').read_bytes() == frozen
    env, prompt = fake.launches[-1]
    assert env['AGY_POOL_SEGMENT_ID'] == 'T1-seg2'
    assert env['AGY_POOL_ACCOUNT'] == 'backup'
    assert len(prompt) <= 250 and '\n' not in prompt and 'T1-seg1.md' in prompt
    capsys.readouterr()
    assert call('status', 'T1', '--json') == 0
    status = json.loads(capsys.readouterr().out)
    assert status['current_segment']['status'] == 'running'
    assert status['current_segment']['seq'] == 2
    assert status['thread']['anchor_path'] == 'anchors/T1.md'


def test_failed_launch_does_not_skip_next_segment_number(pool):
    call, fake, home = pool
    fake.procs.clear()
    fake.guide = 'backup@example.test'
    fake.fail = True
    assert call('switch', 'T1', '--to', 'backup') == 1
    assert segment_state(home) == 'closed'
    assert segment_state(home, 'T1-seg2') == 'failed'
    fake.fail = False
    assert call('switch', 'T1', '--to', 'backup') == 0
    assert segment_state(home, 'T1-seg2') == 'running'
    with sqlite3.connect(home / 'pool.db') as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_type='launch_failed'").fetchone()[0] == 1


def test_failed_launch_keeps_frozen_bundle_until_explicit_cancel(pool, tmp_path):
    call, fake, home = pool
    fake.procs.clear()
    fake.guide = 'backup@example.test'
    fake.fail = True
    assert call('switch', 'T1', '--to', 'backup') == 1
    bundle = home / 'handoffs' / 'T1-seg1.md'
    frozen = bundle.read_bytes()
    log = tmp_path / 'replacement.jsonl'
    log.write_text(json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'status': 'DONE', 'content': 'NEW_TURN'}))
    assert call('handoff', 'T1', '--transcript', str(log)) == 2
    assert call('anchor', 'edit', 'T1', '--text', 'NEW_GOAL') == 2
    assert call('switch', 'T1', '--to', 'backup', '--transcript', str(log)) == 2
    assert bundle.read_bytes() == frozen
    assert call('switch', 'T1', '--cancel') == 0
    assert bundle.read_bytes() == frozen
    assert call('anchor', 'edit', 'T1', '--text', 'NEW_GOAL') == 0
    fake.fail = False
    assert call('switch', 'T1', '--to', 'backup', '--transcript', str(log)) == 0
    text = bundle.read_text(encoding='utf-8')
    assert 'NEW_GOAL' in text and 'NEW_TURN' in text


def test_switch_lock_blocks_forced_switch_and_start_before_account_activity(pool):
    call, fake, _ = pool
    fake.procs.clear()
    fake.locked = True
    assert call('switch', 'T1', '--to', 'backup', '--force') == 3
    assert call('start', 'not launched', '--account', 'main') == 3
    assert len(fake.launches) == 1


@pytest.mark.parametrize('confirmation, expected', [('no', 3), ('yes', 0)])
def test_unmanaged_conflict_requires_explicit_yes(pool, monkeypatch, capsys, confirmation, expected):
    import sys
    from types import SimpleNamespace

    from agy_pool.runtime import AgyProcess

    call, fake, _ = pool
    fake.procs = [AgyProcess(717, 134400000000000000)]
    fake.guide = 'backup@example.test'
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr('builtins.input', lambda prompt: confirmation)
    assert call('switch', 'T1', '--to', 'backup', '--force') == expected
    assert 'PID 717' in capsys.readouterr().err
    assert len(fake.launches) == (2 if expected == 0 else 1)


def test_bound_transcript_uses_only_segment_probe_and_persists_conversation(pool):
    from pathlib import Path

    call, fake, home = pool
    probe = Path(fake.launches[0][0]['AGY_STATUS_STATE'])
    probe.write_text(json.dumps({'email': fake.email, 'session_id': 'conversation-1', 'timestamp': 10}))
    local = fake.brain_root / 'antigravity-cli' / 'brain' / 'conversation-1' / '.system_generated' / 'logs' / 'transcript.jsonl'
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'status': 'DONE', 'content': 'ONLY_A'}))
    assert call('handoff', 'T1') == 0
    assert 'ONLY_A' in (home / 'handoffs' / 'T1-seg1.md').read_text(encoding='utf-8')
    with sqlite3.connect(home / 'pool.db') as conn:
        assert conn.execute('SELECT conversation_id,transcript_path FROM segments').fetchone() == ('conversation-1', None)


def test_explicit_log_can_override_prior_explicit_log(pool, tmp_path):
    call, fake, home = pool
    for marker in ('BEFORE', 'AFTER'):
        path = tmp_path / f'{marker}.jsonl'
        path.write_text(json.dumps({'step_index': 1, 'type': 'USER_INPUT', 'status': 'DONE', 'content': marker}))
        assert call('handoff', 'T1', '--transcript', str(path)) == 0
    assert 'AFTER' in (home / 'handoffs' / 'T1-seg1.md').read_text(encoding='utf-8')


def test_custom_db_environment_reopens_the_same_artifact_root(pool, monkeypatch):
    from pathlib import Path

    from agy_pool.store import PoolStore

    call, fake, base = pool
    monkeypatch.setenv('AGY_POOL_DB', str(base / 'custom.db'))
    assert call('account', 'add', 'main', '--from-current') == 0
    assert call('start', '第二个池', '--account', 'main') == 0
    env = fake.launches[-1][0]
    store = PoolStore(env['AGY_POOL_DB'], env['AGY_POOL_HOME'])
    assert store.artifact('probes/seg-T1-seg1.json') == Path(env['AGY_STATUS_STATE'])
    assert store.artifact('anchors/T1.md').is_file()
    assert env['AGY_STATUS_STATE'] != fake.launches[0][0]['AGY_STATUS_STATE']


def test_closed_threads_hide_from_default_list(pool, capsys):
    call, fake, _ = pool
    fake.procs.clear()
    assert call('close', 'T1') == 0
    capsys.readouterr()
    assert call('list', '--json') == 0
    assert json.loads(capsys.readouterr().out)['threads'] == []
    assert call('list', '--all', '--json') == 0
    assert json.loads(capsys.readouterr().out)['threads'][0]['thread_id'] == 'T1'
