from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .runtime import AgyProcess, AgyRuntime, RuntimeIssue


class _BSDInfo(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint32), ('status', ctypes.c_uint32),
                ('xstatus', ctypes.c_uint32), ('pid', ctypes.c_uint32),
                ('ppid', ctypes.c_uint32), ('ids_and_reserved', ctypes.c_uint32 * 7),
                ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32),
                ('nfiles', ctypes.c_uint32), ('pgid', ctypes.c_uint32),
                ('jobc', ctypes.c_uint32), ('tty_dev', ctypes.c_uint32),
                ('tty_pgid', ctypes.c_uint32), ('nice', ctypes.c_int32),
                ('birth_sec', ctypes.c_uint64), ('birth_usec', ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _libproc():
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    return lib


def _pid_info(pid: int) -> _BSDInfo | None:
    if not 0 < pid <= 0x7fffffff:
        return None
    info = _BSDInfo()
    size = ctypes.sizeof(info)
    ctypes.set_errno(0)
    result = _libproc().proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    error = ctypes.get_errno()
    if result == size and info.pid == pid:
        return info
    if result <= 0 and error in (errno.ESRCH, errno.ENOENT):
        return None
    raise OSError(error or errno.EIO, '无法读取 macOS 进程身份。')


class MacAgyRuntime(AgyRuntime):
    def __init__(self):
        self._work_child: subprocess.Popen | None = None

    @contextmanager
    def switch_lock(self, name: str | None = None):
        import fcntl

        root = Path.home() / 'Library' / 'Application Support' / 'agy-pool'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        suffix = '-' + hashlib.sha256(name.encode()).hexdigest() if name else ''
        path = root / f'switch{suffix}.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            lock = os.fstat(fd)
            if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid():
                raise RuntimeIssue('换号锁文件无效，请检查本地 agy-pool 数据目录。')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeIssue('同一用户的另一个终端正在换号或启动会话，请稍后重试。', 3) from None
            yield
        finally:
            # Keep the inode: deleting a lock file could let two callers lock different inodes.
            os.close(fd)

    def process_creation(self, pid: int) -> int | None:
        info = _pid_info(pid)
        if info is None or info.status == 5:
            return None
        # Match the persisted timestamp scale so conflict messages use one UTC conversion.
        return (info.birth_sec + 11644473600) * 10000000 + info.birth_usec * 10

    def scan_agy_processes(self) -> list[AgyProcess]:
        try:
            # The argv-based comm field can hide a live process or print it as (agy).
            result = subprocess.run(['/bin/ps', '-Aww', '-o', 'pid=', '-o', 'uid=', '-o', 'ucomm='],
                                    capture_output=True, text=True, errors='replace', timeout=3,
                                    env={**os.environ, 'LC_ALL': 'C'})
        except subprocess.TimeoutExpired:
            raise RuntimeIssue('macOS 进程扫描超时；未进行换号，请重试。') from None
        if result.returncode != 0:
            raise RuntimeIssue('macOS 进程扫描失败；未进行换号，请重试。')
        uid = os.getuid()
        matches = []
        for line in result.stdout.splitlines():
            cols = line.split(maxsplit=2)
            if not cols:
                continue
            if len(cols) != 3 or not cols[0].isascii() or not cols[0].isdigit() or not cols[1].isascii() or not cols[1].isdigit():
                raise RuntimeIssue('macOS 进程清单格式无法识别；未进行换号。')
            pid, owner = map(int, cols[:2])
            if owner != uid or cols[2].strip() != 'agy':
                continue
            try:
                created = self.process_creation(pid)
            except ProcessLookupError:
                continue
            except OSError:
                matches.append(AgyProcess(pid, None))
            else:
                if created is not None:
                    matches.append(AgyProcess(pid, created))
        return matches

    @staticmethod
    def _spawn(cwd: Path, env: dict[str, str], prompt: str | None = None):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeIssue('请在 macOS 交互终端中运行；agy 将占用当前终端。')
        exe = shutil.which('agy')
        if exe is None:
            raise RuntimeIssue('找不到 agy，请安装 macOS 版 Antigravity CLI 并检查 PATH。')
        argv = [exe] + (['-i', prompt] if prompt else [])
        return subprocess.Popen(argv, cwd=cwd, env=env)

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

    def _wait_interactive(self, child) -> int:
        return self._wait_foreground(child, cancel_on_interrupt=True)

    def launch_agy(self, cwd: Path, env: dict[str, str], prompt: str | None, on_started) -> AgyProcess:
        child = self._spawn(cwd, env, prompt)
        try:
            proc = self._confirm_launch(child, on_started)
        except BaseException:
            # A child and its shell must not both read the terminal after an interrupted launch.
            self._wait_foreground(child)
            raise
        self._work_child = child
        return proc

    def finish_session(self) -> bool:
        child = self._work_child
        if child is None:
            return False
        self._work_child = None
        self._wait_foreground(child)
        return True
