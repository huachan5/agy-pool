import json
import os
import subprocess
import sys
import time
import uuid

import pytest


@pytest.mark.parametrize('fixture, expected', [('ok', 0), ('wrong', 4), ('missing', 1), ('invalid', 1),
                                               ('empty', 1), ('old', 1)])
def test_switch_auth_only_reads_fresh_private_probe(tmp_path, monkeypatch, fixture, expected):
    from agy_pool.runtime import RuntimeIssue, identity, read_auth_probe

    t = time.time() - 2
    shared = tmp_path / 'shared.json'
    shared.write_text(json.dumps({'email': 'backup@example.test', 'timestamp': t + 1}))
    monkeypatch.setenv('AGY_STATUS_STATE', str(shared))
    probe = tmp_path / 'probe.json'
    if fixture == 'invalid':
        probe.write_text('{')
    elif fixture != 'missing':
        probe.write_text(json.dumps({'email': 'main@example.test' if fixture == 'wrong' else (
            '' if fixture == 'empty' else 'backup@example.test'), 'timestamp': t - 1 if fixture == 'old' else t + 1}))
    if expected == 0:
        assert read_auth_probe(probe, not_before=t, expected=identity('backup@example.test')).hint == 'b***@example.test'
    else:
        with pytest.raises(RuntimeIssue) as exc:
            read_auth_probe(probe, not_before=t, expected=identity('backup@example.test'))
        assert exc.value.code == expected
        assert '@example.test' not in str(exc.value).replace('m***@example.test', '').replace('b***@example.test', '')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows kernel')
def test_named_mutex_blocks_another_process_and_releases_after_owner_exits(tmp_path):
    from agy_pool.runtime import AgyRuntime

    name = 'Local\\agy-pool-test-' + uuid.uuid4().hex
    source = '''from agy_pool.runtime import AgyRuntime, RuntimeIssue
import sys
try:
    with AgyRuntime().switch_lock(sys.argv[1]):
        pass
except RuntimeIssue as exc:
    raise SystemExit(exc.code)
'''
    with AgyRuntime().switch_lock(name):
        result = subprocess.run([sys.executable, '-c', source, name], cwd=os.getcwd(), timeout=10)
        assert result.returncode == 3
    result = subprocess.run([sys.executable, '-c', source, name], cwd=os.getcwd(), timeout=10)
    assert result.returncode == 0


@pytest.mark.parametrize('result', ['missing', 'wrong', 'correct'])
def test_verification_clears_stale_and_new_probe_files(tmp_path, monkeypatch, result):
    from agy_pool.runtime import AgyRuntime, RuntimeIssue, identity

    probe = tmp_path / 'dedicated.json'
    probe.write_text(json.dumps({'email': 'backup@example.test', 'timestamp': time.time()}))
    runtime = AgyRuntime()

    def simulate_hud(cwd, path):
        assert not path.exists()
        if result != 'missing':
            email = 'backup@example.test' if result == 'correct' else 'main@example.test'
            path.write_text(json.dumps({'email': email, 'timestamp': time.time()}))

    monkeypatch.setattr(runtime, '_run_interactive', simulate_hud)
    if result == 'correct':
        assert runtime.verify_current_auth(tmp_path, probe, identity('backup@example.test')).hint == 'b***@example.test'
    else:
        with pytest.raises(RuntimeIssue) as exc:
            runtime.verify_current_auth(tmp_path, probe, identity('backup@example.test'))
        assert exc.value.code == (4 if result == 'wrong' else 1)
    assert not probe.exists()


def test_git_context_reports_a_real_worktree(tmp_path):
    from agy_pool.runtime import AgyRuntime

    def git(*args):
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, timeout=10)

    git('init', '--quiet')
    path = tmp_path / 'example.py'
    path.write_text('first = 1\n')
    git('add', 'example.py')
    path.write_text('first = 2\n')
    status, diff = AgyRuntime.git_context(tmp_path)
    assert 'AM example.py' in status
    assert 'example.py' in diff


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows kernel')
def test_windows_pid_requires_matching_creation_time():
    from agy_pool.runtime import AgyRuntime

    runtime = AgyRuntime()
    stamp = runtime.process_creation(os.getpid())
    assert runtime.is_process_alive(os.getpid(), stamp)
    assert not runtime.is_process_alive(os.getpid(), stamp + 1)
    assert not runtime.is_process_alive(99999999, stamp)
    assert all(proc.pid > 0 for proc in runtime.scan_agy_processes())


def test_is_process_alive_returns_false_on_permission_error(monkeypatch):
    from agy_pool.runtime import AgyRuntime

    runtime = AgyRuntime()
    monkeypatch.setattr(runtime, 'process_creation', lambda pid: (_ for _ in ()).throw(PermissionError(5, 'denied')))
    assert runtime.is_process_alive(4200, 123456) is False


def test_silent_auth_probe_hides_console_and_cleans_up(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agy_pool import runtime as rt

    runtime = rt.AgyRuntime()
    probe = tmp_path / 'probe.json'
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(rt.shutil, 'which', lambda name: 'C:\\agy\\agy.exe' if name == 'agy.exe' else None)

    spawned = []
    killed = []

    class FakeChild:
        def __init__(self):
            self.pid = 7788
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def kill(self):
            self.stopped = True

        def wait(self, timeout=None):
            self.stopped = True
            return 0

    def fake_popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        probe.write_text(json.dumps({'email': 'main@example.test', 'timestamp': time.time()}))
        return FakeChild()

    monkeypatch.setattr(rt.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(rt, 'STARTUPINFO', SimpleNamespace, raising=False)
    monkeypatch.setattr(rt.subprocess, 'STARTUPINFO', lambda: SimpleNamespace(dwFlags=0, wShowWindow=1), raising=False)
    monkeypatch.setattr(rt.subprocess, 'STARTF_USESHOWWINDOW', 1, raising=False)
    monkeypatch.setattr(rt.subprocess, 'CREATE_NEW_CONSOLE', 0x10, raising=False)
    monkeypatch.setattr(runtime, '_terminate_process_tree', lambda pid: killed.append(pid))

    ident = runtime.verify_current_auth(tmp_path, probe, rt.identity('main@example.test'))
    assert ident.hint == 'm***@example.test'
    assert not probe.exists()
    assert len(spawned) == 1
    assert spawned[0][1]['startupinfo'].wShowWindow == 0
    assert spawned[0][1]['creationflags'] == 0x10
    assert killed == [7788]


def test_windows_tty_launch_runs_in_current_terminal_and_waits(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agy_pool import runtime as rt

    runtime = rt.AgyRuntime()
    monkeypatch.setattr(rt.shutil, 'which', lambda name: 'C:\\agy\\agy.exe' if name == 'agy.exe' else None)
    monkeypatch.setattr(rt.subprocess, 'CREATE_NEW_CONSOLE', 0x10, raising=False)
    monkeypatch.delenv('AGY_POOL_NEW_CONSOLE', raising=False)

    observed = []
    waited = []
    child = SimpleNamespace(
        pid=5566,
        poll=lambda: None,
        wait=lambda timeout=None: (waited.append(timeout), (_ for _ in ()).throw(subprocess.TimeoutExpired(['agy'], 3)) if timeout else 0)[-1],
    )
    monkeypatch.setattr(rt.subprocess, 'Popen', lambda argv, **kw: (observed.append((argv, kw)), child)[1])
    monkeypatch.setattr(runtime, 'process_creation', lambda pid: 999)

    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(isatty=lambda: True))
    proc = runtime.launch_agy(tmp_path, {'AGY_POOL_THREAD_ID': 'T1'}, None, lambda _p: None)
    assert proc.pid == 5566
    assert 'creationflags' not in observed[0][1]
    assert runtime.finish_session() is True
    assert waited == [3.0, None]

    # Non-TTY falls back to CREATE_NEW_CONSOLE
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    runtime._spawn(tmp_path, {})
    assert observed[1][1]['creationflags'] == 0x10
