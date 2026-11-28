from __future__ import annotations

import os
import re
import select
import shutil
import signal
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# CSI-u hotkey patterns:
# - macOS Otty (S06): flag 5 -> ESC[121:89;10u, flag 1 -> ESC[121;10u (modifier 10 = Shift(1)+Super(8)+1)
# - Windows Terminal: Ctrl+Shift+Y -> ESC[121;6u / ESC[89;6u / ESC[121:89;6u (modifier 6 = Shift(1)+Ctrl(4)+1)
_CSI_U_RE = re.compile(rb'^\x1b\[(\d+)(?::(\d+))?(?::(\d+))?;(\d+)(?::\d+)?u$')
_BRACKETED_PASTE_START = b'\x1b[200~'
_BRACKETED_PASTE_END = b'\x1b[201~'
_MAX_ESC_BUFFER = 256


@dataclass(frozen=True)
class ParsedChunk:
    forward_bytes: bytes
    hotkey_triggered: bool = False


class InputStreamParser:
    """Incremental byte-stream parser for CSI-u hotkeys with fragmentation & bracketed-paste safety.

    - Supports arbitrary read() chunking without mis-firing on partial escapes.
    - Matches Cmd+Shift+Y (modifier 10) on macOS and Ctrl+Shift+Y (modifier 6) on Windows,
      both with alternate keycode subfields (flag 5: ESC[121:89;10u) and without (flag 1: ESC[121;10u).
    - Never triggers on legacy plain 'y' (0x79) or 'Y' (0x59).
    - Passes Shift+Tab (ESC[9;2u / ESC[Z), CSI-u query replies (ESC[?5u), normal text, and
      everything inside bracketed paste (ESC[200~ ... ESC[201~) verbatim to the child.
    """

    def __init__(self, *, platform: str | None = None, enabled: bool = True):
        self.platform = platform or sys.platform
        self.enabled = enabled
        self._buf = bytearray()
        self._in_paste = False

    def _is_target_hotkey(self, seq: bytes) -> bool:
        if not self.enabled:
            return False
        m = _CSI_U_RE.match(seq)
        if not m:
            return False
        key_code = int(m.group(1))
        alt_code = int(m.group(2)) if m.group(2) else None
        modifier = int(m.group(4))
        is_y = key_code in (121, 89) or alt_code in (121, 89)
        if not is_y:
            return False
        if self.platform == 'darwin':
            return modifier == 10  # Shift(1) + Super(8) + 1
        if self.platform == 'win32':
            return modifier == 6   # Shift(1) + Ctrl(4) + 1
        return modifier in (6, 10)

    def feed(self, data: bytes) -> ParsedChunk:
        if not data:
            return ParsedChunk(b'', False)
        self._buf.extend(data)
        out = bytearray()
        triggered = False

        while self._buf:
            if self._in_paste:
                idx = self._buf.find(_BRACKETED_PASTE_END)
                if idx != -1:
                    end_pos = idx + len(_BRACKETED_PASTE_END)
                    out.extend(self._buf[:end_pos])
                    del self._buf[:end_pos]
                    self._in_paste = False
                    continue
                # Keep at most len(_BRACKETED_PASTE_END)-1 trailing bytes in case end marker is split.
                keep = len(_BRACKETED_PASTE_END) - 1
                if len(self._buf) > keep:
                    out.extend(self._buf[:-keep])
                    del self._buf[:-keep]
                break

            esc_pos = self._buf.find(0x1B)
            if esc_pos == -1:
                out.extend(self._buf)
                self._buf.clear()
                break
            if esc_pos > 0:
                out.extend(self._buf[:esc_pos])
                del self._buf[:esc_pos]

            # Now self._buf[0] == 0x1B
            if len(self._buf) == 1:
                # Could be start of CSI sequence split across reads; wait for more bytes.
                break

            second = self._buf[1]
            if second != ord('['):
                # Non-CSI escape (e.g. standalone ESC or SS3/OSC): forward first 2 bytes verbatim.
                out.extend(self._buf[:2])
                del self._buf[:2]
                continue

            # Inside CSI: ESC [ <param bytes 0x30-0x3F>* <intermediate 0x20-0x2F>* <final 0x40-0x7E>
            final_idx = None
            for i in range(2, len(self._buf)):
                b = self._buf[i]
                if 0x40 <= b <= 0x7E:
                    final_idx = i
                    break
                if not (0x20 <= b <= 0x3F):
                    # Malformed CSI: flush up to here verbatim.
                    final_idx = i
                    break

            if final_idx is None:
                if len(self._buf) >= _MAX_ESC_BUFFER:
                    out.extend(self._buf)
                    self._buf.clear()
                break

            seq = bytes(self._buf[:final_idx + 1])
            del self._buf[:final_idx + 1]

            if seq == _BRACKETED_PASTE_START:
                self._in_paste = True
                out.extend(seq)
            elif self._is_target_hotkey(seq):
                triggered = True
            else:
                out.extend(seq)

        return ParsedChunk(bytes(out), triggered)

    def flush(self) -> bytes:
        """Flush any buffered trailing bytes (e.g., standalone ESC on timeout)."""
        if not self._buf:
            return b''
        data = bytes(self._buf)
        self._buf.clear()
        return data


class AccountMenu:
    """In-pane two-row keyboard account selector (FR-04 / AC-03).

    - Default focus is on the currently logged-in account row.
    - Up/Down moves focus only (zero side effects).
    - Esc cancels (returns None, zero side effects).
    - Enter on current account cancels (returns None, zero side effects).
    - Enter on the other account returns the confirmed target account_id.
    """

    def __init__(self, options: list[dict[str, Any]]):
        self.options = options[:2]
        self.focus = 0
        for idx, item in enumerate(self.options):
            if item.get('is_current'):
                self.focus = idx
                break

    def move_up(self) -> None:
        if self.options:
            self.focus = (self.focus - 1) % len(self.options)

    def move_down(self) -> None:
        if self.options:
            self.focus = (self.focus + 1) % len(self.options)

    def confirm(self) -> str | None:
        if not self.options:
            return None
        chosen = self.options[self.focus]
        if chosen.get('is_current'):
            return None
        return str(chosen['account_id'])

    def render(self) -> str:
        lines = []
        for idx, item in enumerate(self.options):
            marker = '›' if idx == self.focus else ' '
            aid = item['account_id']
            cur = ' 当前' if item.get('is_current') else '     '
            quota = item.get('quota_text') or '额度未知'
            if idx == self.focus:
                lines.append(f'\x1b[48;2;235;243;250m\x1b[38;2;15;23;42m {marker} {aid:<8}{cur}  {quota} \x1b[0m')
            else:
                lines.append(f' {marker} {aid:<8}{cur}  {quota} ')
        lines.append(' \x1b[38;2;100;116;139m↑/↓ 选择   Enter 确认   Esc 取消\x1b[0m')
        return '\r\n'.join(lines)

    def handle_key(self, key: bytes) -> tuple[bool, str | None]:
        """Process a single key sequence. Returns (closed, confirmed_target_or_None)."""
        if key in (b'\x1b[A', b'k'):
            self.move_up()
            return False, None
        if key in (b'\x1b[B', b'j'):
            self.move_down()
            return False, None
        if key in (b'\x1b', b'\x03', b'q'):
            return True, None
        if key in (b'\r', b'\n'):
            return True, self.confirm()
        return False, None


def resolve_native_agy(wrapper_paths: set[Path] | None = None) -> str | None:
    """Resolve the real native agy executable while skipping agy-pool wrapper paths to prevent recursion."""
    override = os.environ.get('AGY_NATIVE_BIN')
    if override and Path(override).is_file():
        return str(Path(override).resolve())

    skip = {p.resolve() for p in (wrapper_paths or set())}
    name = 'agy.exe' if sys.platform == 'win32' else 'agy'
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            if resolved in skip:
                continue
            return str(candidate)
    return shutil.which(name)


def run_pty_proxy(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    hotkey_enabled: bool = True,
    on_hotkey: Callable[[], str | None] | None = None,
    on_tick: Callable[[], None] | None = None,
) -> int:
    """macOS transparent PTY proxy with CSI-u hotkey routing and guaranteed TTY restoration."""
    if sys.platform == 'win32':
        raise RuntimeError('run_pty_proxy 仅用于 POSIX/macOS；Windows 走独立前台控制器。')
    import fcntl
    import pty
    import termios
    import tty

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_tty = termios.tcgetattr(stdin_fd) if os.isatty(stdin_fd) else None
    master_fd, slave_fd = pty.openpty()

    def _sync_winsize():
        if os.isatty(stdin_fd):
            try:
                packed = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b'\x00' * 8)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
            except OSError:
                pass

    _sync_winsize()
    prev_winch = signal.getsignal(signal.SIGWINCH)
    parser = InputStreamParser(platform=sys.platform, enabled=hotkey_enabled)
    pid = os.fork()
    if pid == 0:
        # Child process
        os.close(master_fd)
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except OSError:
            pass
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)

    # Parent proxy
    os.close(slave_fd)
    try:
        signal.signal(signal.SIGWINCH, lambda _s, _f: _sync_winsize())
        if old_tty is not None:
            tty.setraw(stdin_fd)
        while True:
            rlist, _, _ = select.select([master_fd, stdin_fd], [], [], 0.05)
            if master_fd in rlist:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                os.write(stdout_fd, chunk)
            if stdin_fd in rlist:
                try:
                    raw = os.read(stdin_fd, 1024)
                except OSError:
                    break
                if not raw:
                    break
                parsed = parser.feed(raw)
                if parsed.forward_bytes:
                    os.write(master_fd, parsed.forward_bytes)
                if parsed.hotkey_triggered and on_hotkey is not None:
                    on_hotkey()
            else:
                trailing = parser.flush()
                if trailing:
                    os.write(master_fd, trailing)
            if on_tick is not None:
                on_tick()
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        # Always restore terminal state and signal handler even on error/interrupt.
        try:
            signal.signal(signal.SIGWINCH, prev_winch)
        except Exception:
            pass
        if old_tty is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
