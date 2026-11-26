import contextlib
import errno
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_runtime_selection(monkeypatch):
    from agy_pool import runtime
    from agy_pool.runtime_macos import MacAgyRuntime

    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert isinstance(runtime.default_runtime(), MacAgyRuntime)
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert type(runtime.default_runtime()) is runtime.AgyRuntime


def test_process_scan_includes_only_current_users_agy_and_blocks_on_unknown_identity(monkeypatch):
    from agy_pool.runtime import AgyProcess
    from agy_pool.runtime_macos import MacAgyRuntime

    runtime = MacAgyRuntime()
    kernel_names = ' 100 501 agy\n101 501 agy-helper\n102 502 agy\n103 501 agy\n104 501 agy\n'
    argv_names = ' 100 501 (agy)\n101 501 agy-helper\n102 502 agy\n103 501 renamed-argv\n104 501 agy\n'
    monkeypatch.setattr(os, 'getuid', lambda: 501, raising=False)

    def ps(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=kernel_names if 'ucomm=' in argv else argv_names)

    monkeypatch.setattr(subprocess, 'run', ps)

    def creation(pid):
        if pid == 103:
            raise PermissionError(errno.EPERM, 'fixture')
        return {100: 4321, 104: None}[pid]

    monkeypatch.setattr(runtime, 'process_creation', creation)
    assert runtime.scan_agy_processes() == [AgyProcess(100, 4321), AgyProcess(103, None)]


@pytest.mark.parametrize('result', [SimpleNamespace(returncode=1, stdout=''),
                                    SimpleNamespace(returncode=0, stdout='unrecognized list')])
def test_macos_bad_process_listing_cannot_be_treated_as_no_conflicts(monkeypatch, result):
    from agy_pool.runtime import RuntimeIssue
    from agy_pool.runtime_macos import MacAgyRuntime

    monkeypatch.setattr(os, 'getuid', lambda: 501, raising=False)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: result)
    with pytest.raises(RuntimeIssue, match='未进行换号'):
        MacAgyRuntime().scan_agy_processes()


def test_macos_identity_distinguishes_microseconds_and_ignores_zombies(monkeypatch):
    from agy_pool import runtime_macos as mac

    runtime = mac.MacAgyRuntime()
    monkeypatch.setattr(mac, '_pid_info', lambda pid: mac._BSDInfo(pid=pid, status=2, birth_sec=1700000000, birth_usec=123456))
    assert runtime.is_process_alive(123, 133444736001234560)
    assert not runtime.is_process_alive(123, 133444736001234550)
    monkeypatch.setattr(mac, '_pid_info', lambda pid: mac._BSDInfo(pid=pid, status=5, birth_sec=1700000000, birth_usec=123456))
    assert runtime.process_creation(123) is None


def test_macos_spawn_passes_args_and_leaves_stdio_attached(tmp_path, monkeypatch):
    from agy_pool import runtime_macos as mac

    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(mac.shutil, 'which', lambda name: '/path with space/agy' if name == 'agy' else None)
    observed = []
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: observed.append((argv, kw)))
    env = {'AGY_STATUS_STATE': '/my state.json'}
    mac.MacAgyRuntime()._spawn(tmp_path, env, '接力 "with quotes"')
    assert observed == [(['/path with space/agy', '-i', '接力 "with quotes"'], {'cwd': tmp_path, 'env': env})]


def test_macos_rejects_noninteractive_launch(tmp_path, monkeypatch):
    from agy_pool.runtime import RuntimeIssue
    from agy_pool.runtime_macos import MacAgyRuntime

    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    with pytest.raises(RuntimeIssue, match='终端'):
        MacAgyRuntime()._spawn(tmp_path, {})


def test_macos_interrupted_guide_keeps_terminal_until_child_leaves(monkeypatch):
    from agy_pool.runtime_macos import MacAgyRuntime

    child = SimpleNamespace()
    responses = iter([KeyboardInterrupt(), 0])

    def wait():
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value

    child.wait = wait
    with pytest.raises(KeyboardInterrupt):
        MacAgyRuntime._wait_foreground(child, cancel_on_interrupt=True)
    assert next(responses, 'reaped') == 'reaped'


def test_macos_ctrl_c_during_launch_confirm_keeps_live_child_and_releases_lock(tmp_path, monkeypatch):
    from agy_pool.runtime import AgyProcess
    from agy_pool.runtime_macos import MacAgyRuntime

    runtime = MacAgyRuntime()
    monkeypatch.setattr(runtime, 'process_creation', lambda pid: 999)

    waits = iter([KeyboardInterrupt(), subprocess.TimeoutExpired(['agy'], 2.5)])

    def wait_with_events(timeout=None):
        val = next(waits)
        if isinstance(val, BaseException):
            raise val
        return val

    child = SimpleNamespace(pid=4321, poll=lambda: None, wait=wait_with_events)
    monkeypatch.setattr(runtime, '_spawn', lambda cwd, env, prompt=None: child)

    recorded = []
    proc = runtime.launch_agy(tmp_path, {}, None, recorded.append)
    assert proc == AgyProcess(4321, 999)
    assert recorded == [proc]
    assert runtime._work_child is child

    # If on_started fails while child stays alive, launch_agy must NOT block holding the lock.
    runtime2 = MacAgyRuntime()
    monkeypatch.setattr(runtime2, 'process_creation', lambda pid: 999)
    waited_in_finish = []
    child2 = SimpleNamespace(pid=4322, poll=lambda: None, wait=lambda timeout=None: (waited_in_finish.append(True), 0)[1])
    monkeypatch.setattr(runtime2, '_spawn', lambda cwd, env, prompt=None: child2)

    with pytest.raises(RuntimeError, match='commit boom'):
        runtime2.launch_agy(tmp_path, {}, None, lambda _p: (_ for _ in ()).throw(RuntimeError('commit boom')))
    assert waited_in_finish == []
    assert runtime2.finish_session() is True
    assert waited_in_finish == [True]


@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS libproc')
def test_native_macos_process_identity_and_scan(tmp_path):
    import shutil

    from agy_pool.runtime_macos import MacAgyRuntime

    runtime = MacAgyRuntime()
    stamp = runtime.process_creation(os.getpid())
    assert stamp is not None and runtime.is_process_alive(os.getpid(), stamp)
    assert not runtime.is_process_alive(os.getpid(), stamp - 10)
    assert runtime.process_creation(2000000000) is None
    named = tmp_path / 'agy'
    shutil.copy2('/bin/sleep', named)
    with subprocess.Popen([str(named), '15']) as child:
        try:
            assert any(proc.pid == child.pid and proc.created is not None for proc in runtime.scan_agy_processes())
        finally:
            child.terminate()


@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS terminal')
def test_native_macos_foreground_child_inherits_terminal(tmp_path):
    import pty
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    fixture = tmp_path / 'agy'
    fixture.write_text('#!/bin/sh\n[ -t 0 ] && [ -t 1 ] && [ "$1" = "-i" ] && [ "$2" = "one argument" ]\n')
    fixture.chmod(0o700)
    master, slave = pty.openpty()
    source = "from pathlib import Path; from agy_pool.runtime_macos import MacAgyRuntime; import os; raise SystemExit(MacAgyRuntime()._spawn(Path.cwd(), dict(os.environ), 'one argument').wait())"
    env = {**os.environ, 'PATH': os.pathsep.join((str(tmp_path), os.environ['PATH']))}
    try:
        assert subprocess.run([sys.executable, '-c', source], stdin=slave, stdout=slave, stderr=slave,
                              cwd=repo, timeout=10, env=env).returncode == 0
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(os.name != 'posix', reason='native POSIX shell')
def test_macos_shim_invokes_package_from_another_directory(tmp_path):
    import json
    import shlex
    from pathlib import Path

    launcher = Path(__file__).resolve().parents[1] / 'agy-pool.sh'
    wrapper = tmp_path / 'python3.12'
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    wrapper.chmod(0o700)
    env = {**os.environ, 'PATH': os.pathsep.join((str(tmp_path), os.environ['PATH'])),
           'AGY_POOL_HOME': str(tmp_path / 'pool')}
    env.pop('AGY_POOL_DB', None)
    result = subprocess.run(['/bin/sh', str(launcher), 'list', '--json'], cwd=tmp_path, env=env,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'active_machine_account': None, 'accounts': [], 'threads': []}


@pytest.mark.skipif(os.name != 'posix', reason='native POSIX flock')
def test_native_macos_lock_is_independent_of_pool_and_released(tmp_path, monkeypatch):
    from agy_pool.runtime_macos import MacAgyRuntime

    monkeypatch.setenv('HOME', str(tmp_path))
    runtime = MacAgyRuntime()
    source = '''from agy_pool.runtime_macos import MacAgyRuntime
from agy_pool.runtime import RuntimeIssue
try:
    with MacAgyRuntime().switch_lock():
        pass
except RuntimeIssue as exc:
    raise SystemExit(exc.code)
'''
    child_env = {**os.environ, 'AGY_POOL_DB': str(tmp_path / 'other.db'), 'AGY_POOL_HOME': str(tmp_path / 'other')}
    cmd = [sys.executable, '-c', source]
    with runtime.switch_lock():
        assert subprocess.run(cmd, env=child_env, timeout=5).returncode == 3
    assert subprocess.run(cmd, env=child_env, timeout=5).returncode == 0
