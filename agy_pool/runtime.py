from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes as win
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class RuntimeIssue(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Identity:
    hint: str
    digest: str


@dataclass(frozen=True)
class AgyProcess:
    pid: int
    created: int | None


_EMAIL = re.compile(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}', re.ASCII)
_CONVERSATION = re.compile(r'[A-Za-z0-9_-]{1,128}', re.ASCII)
_DEFAULT_MUTEX = r'Local\agy-pool-machine-switch-lock'


def identity(email) -> Identity:
    normalized = email.strip().lower() if isinstance(email, str) else ''
    if not _EMAIL.fullmatch(normalized):
        raise RuntimeIssue('未读到可验证的登录邮箱，请检查 HUD 是否已启用并重试。')
    local, domain = normalized.split('@', 1)
    return Identity(f'{local[0]}***@{domain}', hashlib.sha256(normalized.encode()).hexdigest()[:16])


def read_hud_state(path: Path) -> dict:
    try:
        with path.open(encoding='utf-8-sig') as file:
            state = json.loads(file.read(65537))
        if isinstance(state, dict):
            return state
    except (OSError, ValueError, RecursionError):
        pass
    return {}


def read_auth_probe(path: Path, *, not_before: float, expected: Identity | None = None) -> Identity:
    state = read_hud_state(path)
    ts = state.get('timestamp')
    if type(ts) not in (int, float) or not not_before <= ts <= time.time() + 30 or not math.isfinite(ts):
        raise RuntimeIssue('没有收到本次会话的新账号状态。请确认 HUD 已显示后再退出，或取消接棒。')
    actual = identity(state.get('email'))
    if expected is not None and actual.digest != expected.digest:
        raise RuntimeIssue(f'账号不符：实际 {actual.hint}，期望 {expected.hint}。请重新登录目标账号或取消接棒。', 4)
    return actual


def segment_conversation(path: Path) -> str | None:
    session_id = read_hud_state(path).get('session_id')
    return session_id if isinstance(session_id, str) and _CONVERSATION.fullmatch(session_id) else None


class _ProcessEntry(ctypes.Structure):
    _fields_ = [('dwSize', win.DWORD), ('cntUsage', win.DWORD), ('pid', win.DWORD),
                ('heap', ctypes.c_size_t), ('module', win.DWORD), ('threads', win.DWORD),
                ('ppid', win.DWORD), ('priority', win.LONG), ('flags', win.DWORD),
                ('exe', win.WCHAR * 260)]


@lru_cache(maxsize=1)
def _kernel():
    if sys.platform != 'win32':
        raise RuntimeIssue('此项操作需要 Windows 10/11。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    entry = ctypes.POINTER(_ProcessEntry)
    ft = ctypes.POINTER(win.FILETIME)
    signatures = {
        'CreateToolhelp32Snapshot': (win.HANDLE, (win.DWORD, win.DWORD)),
        'Process32FirstW': (win.BOOL, (win.HANDLE, entry)),
        'Process32NextW': (win.BOOL, (win.HANDLE, entry)),
        'OpenProcess': (win.HANDLE, (win.DWORD, win.BOOL, win.DWORD)),
        'GetProcessTimes': (win.BOOL, (win.HANDLE, ft, ft, ft, ft)),
        'GetExitCodeProcess': (win.BOOL, (win.HANDLE, ctypes.POINTER(win.DWORD))),
        'CloseHandle': (win.BOOL, (win.HANDLE,)),
        'CreateMutexW': (win.HANDLE, (ctypes.c_void_p, win.BOOL, win.LPCWSTR)),
        'WaitForSingleObject': (win.DWORD, (win.HANDLE, win.DWORD)),
        'ReleaseMutex': (win.BOOL, (win.HANDLE,)),
    }
    for name, (response, args) in signatures.items():
        fn = getattr(kernel, name)
        fn.restype, fn.argtypes = response, args
    return kernel


def _win_error():
    return ctypes.WinError(ctypes.get_last_error())


class AgyRuntime:
    brain_root = Path.home() / '.gemini'

    def __init__(self):
        self._work_child: subprocess.Popen | None = None

    @staticmethod
    def _is_interactive_tty() -> bool:
        try:
            return bool(sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:
            return False

    @contextmanager
    def switch_lock(self, name: str | None = None):
        kernel = _kernel()
        handle = kernel.CreateMutexW(None, False, name or _DEFAULT_MUTEX)
        if not handle:
            raise _win_error()
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result == 0x102:
                raise RuntimeIssue('另一个终端正在换号或启动会话，请稍后重试。', 3)
            if result not in (0, 0x80):
                raise _win_error()
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)

    def process_creation(self, pid: int) -> int | None:
        kernel = _kernel()
        if pid <= 0 or pid > 0xffffffff:
            return None
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise _win_error()
        try:
            code = win.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise _win_error()
            if code.value != 259:
                return None
            created, exited, sys_time, user_time = (win.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(ft) for ft in (created, exited, sys_time, user_time))):
                raise _win_error()
            if exited.dwHighDateTime or exited.dwLowDateTime:
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            kernel.CloseHandle(handle)

    def is_process_alive(self, pid: int, created: int | None) -> bool:
        if created is None:
            return False
        try:
            return self.process_creation(pid) == created
        except PermissionError:
            return False

    def scan_agy_processes(self) -> list[AgyProcess]:
        kernel = _kernel()
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise _win_error()
        matches = []
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                if entry.exe.casefold() == 'agy.exe':
                    try:
                        created = self.process_creation(entry.pid)
                    except OSError:
                        matches.append(AgyProcess(entry.pid, None))
                    else:
                        if created is not None:
                            matches.append(AgyProcess(entry.pid, created))
                found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:
                raise _win_error()
        finally:
            kernel.CloseHandle(snapshot)
        return matches

    def read_current_auth(self) -> Identity:
        path = Path(os.environ.get('AGY_STATUS_STATE', Path.home() / '.antigravity' / 'status-state.json')).expanduser()
        return read_auth_probe(path, not_before=time.time() - 600)

    def _run_interactive(self, cwd: Path, probe: Path) -> None:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('AGY_POOL_')}
        env['AGY_STATUS_STATE'] = str(probe)
        child = self._spawn(cwd, env)
        if self._wait_foreground(child, cancel_on_interrupt=True) != 0:
            raise RuntimeIssue('临时 agy 会话异常退出。请检查 agy 和 HUD 后重试。')

    def _wait_interactive(self, child) -> int:
        return child.wait()

    @staticmethod
    def _terminate_process_tree(pid: int) -> None:
        if sys.platform == 'win32':
            try:
                subprocess.run(
                    ['taskkill', '/PID', str(pid), '/T', '/F'],
                    capture_output=True,
                    timeout=5,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _run_silent_auth_probe(self, cwd: Path, probe: Path, since: float, timeout: float = 10.0) -> bool:
        exe = shutil.which('agy.exe')
        if exe is None:
            raise RuntimeIssue('找不到 agy.exe，请安装 Antigravity CLI 并检查 PATH。')
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('AGY_POOL_')}
        env['AGY_STATUS_STATE'] = str(probe)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        child = subprocess.Popen(
            [exe],
            cwd=cwd,
            env=env,
            startupinfo=si,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if probe.is_file():
                    try:
                        read_auth_probe(probe, not_before=since)
                        return True
                    except RuntimeIssue:
                        pass
                if child.poll() is not None:
                    break
                time.sleep(0.15)
            return False
        finally:
            if child.poll() is None:
                self._terminate_process_tree(child.pid)
                try:
                    child.kill()
                except OSError:
                    pass
                try:
                    child.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def guide_account_switch(self, cwd: Path, expected_hint: str, probe: Path) -> None:
        print(f'请在临时 agy 窗口输入 /logout，浏览器登录 {expected_hint}，再用 /exit 退出。', flush=True)
        try:
            self._run_interactive(cwd, probe)
        finally:
            probe.unlink(missing_ok=True)

    def verify_current_auth(self, cwd: Path, probe: Path, expected: Identity, *, m4_mode: bool = False) -> Identity:
        # A new process starts after the sign-in guide exits; its HUD cannot reuse the guide's pre-login writes.
        probe.unlink(missing_ok=True)
        since = time.time()
        use_silent = (
            not m4_mode
            and sys.platform == 'win32'
            and '_run_interactive' not in self.__dict__
            and type(self)._run_interactive is AgyRuntime._run_interactive
        )
        try:
            if use_silent:
                print('正在静默校验当前登录账号...', flush=True)
                if not self._run_silent_auth_probe(cwd, probe, since) and self._is_interactive_tty():
                    print('静默校验未获取到账号状态，请在校验窗口 HUD 显示后输入 /exit；不需要发送任务消息。', flush=True)
                    self._run_interactive(cwd, probe)
            else:
                print('请等新校验窗口的 HUD 显示后输入 /exit；不需要发送任务消息。', flush=True)
                self._run_interactive(cwd, probe)
            return read_auth_probe(probe, not_before=since, expected=expected)
        finally:
            probe.unlink(missing_ok=True)

    @staticmethod
    def _spawn(cwd: Path, env: dict[str, str], prompt: str | None = None, *, new_console: bool | None = None):
        exe = shutil.which('agy.exe')
        if exe is None:
            raise RuntimeIssue('找不到 agy.exe，请安装 Antigravity CLI 并检查 PATH。')
        argv = [exe] + (['-i', prompt] if prompt else [])
        if new_console is None:
            new_console = (os.environ.get('AGY_POOL_NEW_CONSOLE') == '1') or (not AgyRuntime._is_interactive_tty())
        kwargs: dict = {'cwd': cwd, 'env': env}
        if new_console:
            kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
        return subprocess.Popen(argv, **kwargs)

    @staticmethod
    def _wait_foreground(child, *, cancel_on_interrupt: bool = False) -> int:
        interrupted = False
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                if cancel_on_interrupt and not interrupted:
                    print('已请求取消；请先在 agy 中 /exit，退出后不会启动下一棒。', file=sys.stderr, flush=True)
                interrupted = True
        if cancel_on_interrupt and interrupted:
            raise KeyboardInterrupt
        return code

    def launch_agy(
        self,
        cwd: Path,
        env: dict[str, str],
        prompt: str | None,
        on_started,
        *,
        m4_foreground: bool = False,
    ) -> AgyProcess:
        if m4_foreground:
            if not self._is_interactive_tty():
                raise RuntimeIssue('M4 接棒要求在原交互终端前台运行，禁止回退到新控制台。')
            in_foreground = True
        else:
            in_foreground = self._is_interactive_tty() and os.environ.get('AGY_POOL_NEW_CONSOLE') != '1'
        child = self._spawn(cwd, env, prompt, new_console=not in_foreground)
        if in_foreground:
            self._work_child = child
        try:
            return self._confirm_launch(child, on_started)
        except BaseException:
            if in_foreground and child.poll() is not None:
                child.wait()
                self._work_child = None
            raise

    def finish_session(self) -> bool:
        child = self._work_child
        if child is None:
            return False
        self._work_child = None
        self._wait_foreground(child)
        return True

    def _confirm_launch(self, child, on_started) -> AgyProcess:
        stamp = self.process_creation(child.pid)
        if stamp is None:
            raise RuntimeIssue('agy 启动即退出。请检查安装或登录状态后重试。')
        proc = AgyProcess(child.pid, stamp)
        on_started(proc)
        deadline = time.monotonic() + 3.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if child.poll() is None:
                    return proc
                break
            try:
                child.wait(timeout=remaining)
                break
            except subprocess.TimeoutExpired:
                if child.poll() is None:
                    return proc
                break
            except KeyboardInterrupt:
                if child.poll() is not None:
                    break
                continue
        raise RuntimeIssue('agy 在 3 秒内退出。请检查安装或登录状态后重试。')

    @staticmethod
    def git_context(cwd: Path) -> tuple[str | None, str]:
        def run(*args):
            return subprocess.run(['git', '-c', 'core.fsmonitor=false', '--no-pager', *args], cwd=cwd,
                                  capture_output=True, encoding='utf-8', errors='replace', timeout=3,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                  env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        try:
            status = run('status', '--porcelain')
            if status.returncode != 0:
                return None, ''
            diff = run('diff', '--stat', '--no-ext-diff', '--no-textconv')
            return status.stdout, diff.stdout if diff.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            return None, ''


def default_runtime() -> AgyRuntime:
    if sys.platform == 'darwin':
        from .runtime_macos import MacAgyRuntime

        return MacAgyRuntime()
    return AgyRuntime()


def new_probe(home, prefix: str) -> Path:
    return home.artifact(f'probes/{prefix}-{uuid.uuid4().hex}.json')
