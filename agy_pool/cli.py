from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .handoff import AnchorTooLong, HandoffBuilder
from .m4 import EventMailbox, HudViewPublisher, M4Engine, M4Error, SharedDomainBarrier
from .pty_proxy import resolve_native_agy, run_pty_proxy
from .runtime import Identity, RuntimeIssue, default_runtime, new_probe, segment_conversation
from .store import PoolStore, SensitiveData, StoreError, guard, redact, utc_now, write_text_atomic


_ACCOUNT_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}', re.ASCII)
_CONVERSATION = re.compile(r'[A-Za-z0-9_-]{1,128}', re.ASCII)


def _parser():
    p = argparse.ArgumentParser(prog='agy-pool', description='在 Windows 和 macOS 上管理 agy 任务与会话交接。')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='初始化本地状态库')
    account = commands.add_parser('account', help='管理账号槽位').add_subparsers(dest='action', required=True)
    add = account.add_parser('add')
    add.add_argument('account_id')
    add.add_argument('--from-current', action='store_true', required=True)
    add.add_argument('--label')
    account.add_parser('list').add_argument('--json', action='store_true')
    account.add_parser('remove').add_argument('account_id')
    start = commands.add_parser('start', help='创建并启动任务线程')
    start.add_argument('title')
    start.add_argument('--account', required=True)
    start.add_argument('--cwd', type=Path)
    anchor = start.add_mutually_exclusive_group()
    anchor.add_argument('--anchor-text')
    anchor.add_argument('--anchor-file', type=Path)
    edit = commands.add_parser('anchor', help='管理任务锚点').add_subparsers(dest='action', required=True).add_parser('edit')
    edit.add_argument('tid', nargs='?')
    text = edit.add_mutually_exclusive_group()
    text.add_argument('--text')
    text.add_argument('--file', type=Path)
    for name in ('handoff', 'switch', 'status', 'close'):
        sub = commands.add_parser(name)
        sub.add_argument('tid', nargs='?')
        if name in ('handoff', 'switch'):
            sub.add_argument('--transcript', type=Path)
        if name == 'switch':
            action = sub.add_mutually_exclusive_group(required=True)
            action.add_argument('--to')
            action.add_argument('--cancel', action='store_true')
            sub.add_argument('--force', action='store_true')
        if name == 'status':
            sub.add_argument('--json', action='store_true')
    listing = commands.add_parser('list', help='查看账号池与线程')
    listing.add_argument('--all', action='store_true')
    listing.add_argument('--json', action='store_true')
    run_cmd = commands.add_parser('run', help='M4 托管前台入口（透明透传原生 agy）')
    run_cmd.add_argument('--downgrade-menu', action='store_true', help='显式启用退出后同窗账号菜单降级')
    run_cmd.add_argument('agy_args', nargs=argparse.REMAINDER)
    return p


def _user_error(message: str):
    return RuntimeIssue(message, 2)


def _identity(account) -> Identity:
    return Identity(account['email_hint'], account['email_hash'])


def _read_text(path: Path) -> str:
    return path.read_bytes().decode('utf-8-sig')


def _json(data):
    print(json.dumps(data, ensure_ascii=False, allow_nan=False))


def _quota(value):
    return '未知' if value is None else f'{value:.0%}'


class Controller:
    def __init__(self, store: PoolStore, runtime, *, barrier: SharedDomainBarrier | None = None):
        self.store = store
        self.runtime = runtime
        self.barrier = barrier or SharedDomainBarrier()

    def _account(self, account_id: str) -> dict:
        row = self.store.get_account(account_id)
        if not row:
            slots = '、'.join(item['account_id'] for item in self.store.export_snapshot()['accounts']) or '无'
            raise _user_error(f'账号槽位未注册；可用槽位：{slots}。请用 account add 注册。')
        return row

    def _show_accounts(self, rows, current):
        print('账号槽位')
        if not rows:
            print('  暂无账号；请用 account add <id> --from-current 注册。')
        for row in rows:
            aid = row['account_id']
            indicator = '●' if aid == current else ' '
            print(f"  {indicator} {aid}  {row['email_hint']}  5h {_quota(row['gemini_5h_remaining'])}  "
                  f"7d {_quota(row['gemini_7d_remaining'])}  {row.get('quota_updated_at') or '无快照'}")

    def account(self, args):
        if args.action == 'list':
            snapshot = self.store.export_snapshot()
            if args.json:
                _json(snapshot['accounts'])
            else:
                self._show_accounts(snapshot['accounts'], snapshot['active_machine_account'])
            return
        account_id = args.account_id
        if not _ACCOUNT_ID.fullmatch(account_id):
            raise _user_error('账号槽位须为 1–32 个英文字母、数字、下划线或短横线，且以字母或数字开头。')
        if args.action == 'add':
            guard(args.label)
            with self.runtime.switch_lock(self._mutex_name()):
                actual = self.runtime.read_current_auth()
                with self.store.transaction() as tx:
                    clash = tx.execute('SELECT account_id FROM accounts WHERE email_hash=? AND account_id<>?',
                                       (actual.digest, account_id)).fetchone()
                    if clash:
                        raise _user_error(f'当前身份已绑定槽位 {clash[0]}，请检查登录账号。')
                    existing = tx.execute('SELECT email_hash, status FROM accounts WHERE account_id=?', (account_id,)).fetchone()
                    if existing and existing['email_hash'] != actual.digest:
                        raise _user_error('槽位已绑定其他身份，请换一个槽位名。')
                    tx.execute('''INSERT INTO accounts(account_id,label,email_hint,email_hash,status,identity_revision)
                                  VALUES (?,?,?,?,?,1)
                                  ON CONFLICT(account_id) DO UPDATE SET label=COALESCE(excluded.label,accounts.label),
                                  email_hint=excluded.email_hint,status='active',
                                  identity_revision=CASE WHEN accounts.status='removed' THEN accounts.identity_revision+1 ELSE accounts.identity_revision END''',
                               (account_id, args.label, actual.hint, actual.digest, 'active'))
                    self._mark_auth(tx, account_id)
                    tx.record_event('account_add', account_id=account_id)
            print(f'已登记 {account_id}（{actual.hint}）。')
        else:
            with self.runtime.switch_lock(self._mutex_name()), self.store.transaction() as tx:
                if not tx.execute("SELECT 1 FROM accounts WHERE account_id=? AND status<>'removed'", (account_id,)).fetchone():
                    raise _user_error('找不到该账号槽位；请查看 account list。')
                if tx.execute('''SELECT 1 FROM segments s JOIN threads t USING(thread_id)
                                 WHERE s.account_id=? AND (t.status='active' OR s.status IN ('running','starting')) LIMIT 1''',
                              (account_id,)).fetchone():
                    raise _user_error('仍绑定活跃任务或会话；请先退出会话并收尾。')
                if tx.execute('SELECT 1 FROM segments WHERE account_id=? LIMIT 1', (account_id,)).fetchone():
                    tx.execute("UPDATE accounts SET status='removed', identity_revision=identity_revision+1 WHERE account_id=?", (account_id,))
                else:
                    tx.execute('DELETE FROM accounts WHERE account_id=?', (account_id,))
                if tx.get_meta('active_machine_account') == account_id:
                    tx.set_meta('active_machine_account', '')
                tx.record_event('account_remove', account_id=account_id)
            print(f'已移除 {account_id}；既有历史不变。')

    @staticmethod
    def _mark_auth(tx, account_id: str):
        tx.set_meta('active_machine_account', account_id)
        tx.execute("UPDATE accounts SET status='ready' WHERE status='active' AND account_id<>?", (account_id,))
        if account_id:
            tx.execute("UPDATE accounts SET status='active' WHERE account_id=?", (account_id,))

    def _mark_auth_unknown(self):
        with self.store.transaction() as tx:
            self._mark_auth(tx, '')

    def _thread_id(self, requested: str | None, *, closed: bool = False) -> str:
        candidates = self.store.list_threads(include_closed=closed)
        if value := (requested or os.environ.get('AGY_POOL_THREAD_ID')):
            if not any(row['thread_id'] == value for row in candidates):
                raise _user_error('线程不存在或已经收尾，请用 list --all 查看。')
            return value
        cwd = os.path.normcase(str(Path.cwd().resolve()))
        matching = [row['thread_id'] for row in candidates if row['status'] == 'active' and os.path.normcase(row['cwd']) == cwd]
        if len(matching) == 1:
            return matching[0]
        options = '、'.join(matching or [row['thread_id'] for row in candidates]) or '无'
        raise _user_error(f'无法自动确定线程，请显式传入线程号。候选：{options}。')

    def _thread(self, requested: str | None, *, closed=False):
        tid = self._thread_id(requested, closed=closed)
        with self.store.readonly_transaction() as (tx, _version):
            thread = dict(tx.execute('SELECT * FROM threads WHERE thread_id=?', (tid,)).fetchone())
            row = tx.execute('SELECT * FROM segments WHERE segment_id=?', (thread['current_segment_id'],)).fetchone()
        return thread, dict(row) if row else None

    @staticmethod
    def _mutex_name():
        return os.environ.get('AGY_POOL_SWITCH_MUTEX')

    def _assert_barrier_clear(self):
        try:
            self.barrier.assert_clear_for(self.store.db_id)
        except M4Error as exc:
            raise RuntimeIssue(exc.message, 3) from exc

    @contextmanager
    def _switch_lease(self, thread_id, target):
        with self.runtime.switch_lock(self._mutex_name()):
            pid = os.getpid()
            created = self.runtime.process_creation(pid)
            if created is None:
                raise RuntimeIssue('无法确认换号进程身份，请重新打开终端后重试。')
            lease_id = uuid.uuid4().hex
            acquired = False
            try:
                with self.store.transaction() as tx:
                    old = json.loads(tx.get_meta('active_switch_lease') or '{}')
                    if old:
                        if old['owner_pid'] != pid and self.runtime.is_process_alive(old['owner_pid'], old['owner_pid_create_time']):
                            raise RuntimeIssue('本库还有一个活跃的换号过程，请稍后重试。', 3)
                        tx.record_event('switch_lease_reclaimed', thread_id=old['thread_id'])
                    source = tx.execute('SELECT s.account_id FROM threads t JOIN segments s ON s.segment_id=t.current_segment_id WHERE t.thread_id=?', (thread_id,)).fetchone()
                    data = {'switch_id': lease_id, 'thread_id': thread_id, 'from_account': source[0] if source else None,
                            'to_account': target, 'owner_pid': pid, 'owner_pid_create_time': created, 'started_at': utc_now()}
                    tx.set_meta('active_switch_lease', json.dumps(data, ensure_ascii=False))
                acquired = True
                yield
            finally:
                if acquired:
                    with self.store.transaction() as tx:
                        current = json.loads(tx.get_meta('active_switch_lease') or '{}')
                        if current.get('switch_id') == lease_id:
                            tx.execute("DELETE FROM meta WHERE key='active_switch_lease'")

    def _segment_alive(self, segment):
        return segment['pid'] is not None and self.runtime.is_process_alive(segment['pid'], segment['pid_create_time'])

    def _probe(self, segment_id):
        return self.store.artifact(f'probes/seg-{segment_id}.json')

    def _copy_breadcrumb(self, tx, segment):
        probe = self._probe(segment['segment_id'])
        conversation = segment_conversation(probe)
        if conversation:
            tx.execute('UPDATE segments SET conversation_id=? WHERE segment_id=?', (conversation, segment['segment_id']))
        return probe

    def reconcile(self, *, recover=False):
        with self.store.transaction(write=False) as tx:
            segments = [dict(row) for row in tx.execute("SELECT * FROM segments WHERE status IN ('starting','running')")]
        recovered = set()
        for segment in segments:
            if segment['status'] == 'starting' and not recover:
                continue
            if segment.get('segment_origin') == 'm4_native':
                continue
            alive = self._segment_alive(segment)
            if segment['status'] == 'running' and alive:
                continue
            with self.store.transaction() as tx:
                sid = segment['segment_id']
                latest = tx.execute('SELECT status,pid,pid_create_time FROM segments WHERE segment_id=?', (sid,)).fetchone()
                if tuple(latest) != (segment['status'], segment['pid'], segment['pid_create_time']):
                    continue
                if alive:
                    self._commit_launch(tx, segment['thread_id'], sid, segment['seq'], segment['account_id'], segment['parent_segment_id'])
                    recovered.add((segment['thread_id'], segment['account_id']))
                    continue
                probe = self._copy_breadcrumb(tx, segment)
                if segment['status'] == 'starting':
                    self._fail_launch(tx, segment['thread_id'], sid, segment['account_id'], segment['parent_segment_id'])
                else:
                    tx.execute("UPDATE segments SET status='closed',ended_at=? WHERE segment_id=?", (utc_now(), sid))
            probe.unlink(missing_ok=True)
        return recovered

    def start(self, args):
        title = args.title.strip()
        if not title:
            raise _user_error('任务标题不能为空白，请输入任务目标。')
        cwd = (args.cwd or Path.cwd()).expanduser().resolve()
        if not cwd.is_dir():
            raise _user_error('工作目录不存在，请检查 --cwd。')
        guard((title, str(cwd)))
        if args.anchor_file:
            anchor = _read_text(args.anchor_file.expanduser())
        elif args.anchor_text is not None:
            anchor = args.anchor_text
        else:
            anchor = f'# 任务目标\n\n{title}\n\n## 验收与硬约束\n\n- 待补充（可用 agy-pool anchor edit 编辑）\n'
        HandoffBuilder.validate_anchor(anchor)
        account = self._account(args.account)
        with self.runtime.switch_lock(self._mutex_name()):
            self._assert_barrier_clear()
            self.reconcile(recover=True)
            try:
                self.runtime.verify_current_auth(cwd, new_probe(self.store, 'auth'), _identity(account))
            except RuntimeIssue:
                self._mark_auth_unknown()
                raise
            with self.store.transaction() as tx:
                seq = int(tx.get_meta('next_thread_seq', '1'))
                tid, segment_id = f'T{seq}', f'T{seq}-seg1'
                anchor_path = f'anchors/{tid}.md'
                now = utc_now()
                write_text_atomic(self.store.artifact(anchor_path), anchor)
                tx.set_meta('next_thread_seq', str(seq + 1))
                tx.execute('''INSERT INTO threads(thread_id,title,cwd,anchor_path,status,current_segment_id,created_at,updated_at)
                              VALUES (?,?,?,?, 'active',?,?,?)''', (tid, title, str(cwd), anchor_path, segment_id, now, now))
                self._reserve_launch(tx, tid, segment_id, 1, args.account, None, None)
                tx.record_event('thread_start', thread_id=tid, segment_id=segment_id, account_id=args.account)
            proc = self._launch(tid, segment_id, 1, args.account, cwd)
        print(f'已启动 {tid} · {args.account} · #1（PID {proc.pid}）。', flush=True)

    @staticmethod
    def _reserve_launch(tx, tid, sid, seq, account, parent, bundle):
        tx.execute('''INSERT INTO segments(segment_id,thread_id,seq,account_id,parent_segment_id,handoff_in_path,status,started_at)
                      VALUES (?,?,?,?,?,?,'starting',?) ON CONFLICT(segment_id) DO UPDATE SET account_id=excluded.account_id,
                      parent_segment_id=excluded.parent_segment_id,handoff_in_path=excluded.handoff_in_path,
                      handoff_out_path=NULL,conversation_id=NULL,transcript_path=NULL,pid=NULL,pid_create_time=NULL,
                      last_heartbeat_at=NULL,observed_email_hint=NULL,observed_email_hash=NULL,
                      status='starting',started_at=excluded.started_at,ended_at=NULL WHERE segments.status='failed' ''',
                   (sid, tid, seq, account, parent, bundle, utc_now()))

    @staticmethod
    def _commit_launch(tx, tid, sid, seq, account, parent):
        tx.execute("UPDATE segments SET status='running' WHERE segment_id=?", (sid,))
        if parent:
            tx.execute("UPDATE segments SET status='handed_off' WHERE segment_id=?", (parent,))
        tx.execute('UPDATE threads SET current_segment_id=?,segment_count=?,updated_at=? WHERE thread_id=?',
                   (sid, seq, utc_now(), tid))
        Controller._mark_auth(tx, account)
        tx.record_event('switch_commit' if parent else 'launch_commit', thread_id=tid, segment_id=sid, account_id=account)

    @staticmethod
    def _fail_launch(tx, tid, sid, account, parent):
        tx.execute("UPDATE segments SET status='failed',ended_at=? WHERE segment_id=?", (utc_now(), sid))
        if parent:
            tx.execute("UPDATE segments SET status='closed' WHERE segment_id=? AND status='handoff_pending'", (parent,))
        tx.record_event('launch_failed', thread_id=tid, segment_id=sid, account_id=account)

    def _launch(self, tid, sid, seq, account, cwd, parent=None, bundle=None):
        probe = self._probe(sid)
        probe.unlink(missing_ok=True)
        env = {**os.environ, 'AGY_POOL_THREAD_ID': tid, 'AGY_POOL_SEGMENT_ID': sid, 'AGY_POOL_ACCOUNT': account,
               'AGY_POOL_HOME': str(self.store.base_home), 'AGY_POOL_DB': str(self.store.db), 'AGY_STATUS_STATE': str(probe)}
        prompt = None
        if bundle:
            absolute = self.store.artifact(bundle).as_posix()
            prompt = f'请先用 read_file 读取交接包 {absolute}，简述目标、进度和下一步，再继续任务。'
            if len(prompt) > 250:
                prompt = f'读取交接包 {absolute} 并继续任务。'
            if len(prompt) > 250 or '\n' in prompt or '\r' in prompt:
                raise _user_error('交接包路径太长或含换行，请使用更短的 AGY_POOL_HOME 并保留交接包。')

        def started(proc):
            with self.store.transaction() as tx:
                tx.execute('UPDATE segments SET pid=?,pid_create_time=? WHERE segment_id=?', (proc.pid, proc.created, sid))

        try:
            proc = self.runtime.launch_agy(cwd, env, prompt, started)
        except (OSError, RuntimeIssue):
            with self.store.transaction() as tx:
                self._fail_launch(tx, tid, sid, account, parent)
            probe.unlink(missing_ok=True)
            raise
        with self.store.transaction() as tx:
            self._commit_launch(tx, tid, sid, seq, account, parent)
        return proc

    def _find_transcript(self, conversation):
        if not conversation or not _CONVERSATION.fullmatch(conversation):
            return None
        root = self.runtime.brain_root
        for app in ('antigravity-cli', 'antigravity', 'antigravity-ide'):
            brain = root / app / 'brain' / conversation
            path = brain / '.system_generated' / 'logs' / 'transcript.jsonl'
            if path.is_file() and path.resolve().is_relative_to(root.resolve()):
                return path, brain / 'task.md'
        return None

    def _is_frozen(self, segment) -> bool:
        if segment['status'] == 'handoff_pending':
            return True
        with self.store.transaction(write=False) as tx:
            event = tx.execute("""SELECT event_type FROM events WHERE segment_id=?
                                  AND event_type IN ('handoff_frozen','switch_cancel') ORDER BY id DESC LIMIT 1""",
                               (segment['segment_id'],)).fetchone()
        return event is not None and event[0] == 'handoff_frozen'

    def _bundle(self, thread, segment, explicit, *, freeze=False):
        if segment.get('account_binding_state') == 'unverified' or segment.get('account_id') is None:
            raise RuntimeIssue('源会话账号尚未归属（identity_unverified），禁止生成交接包。', 2)
        tid, sid = thread['thread_id'], segment['segment_id']
        relative = f'handoffs/{sid}.md'
        path = self.store.artifact(relative)
        if self._is_frozen(segment):
            if explicit is not None:
                raise _user_error('交接包已冻结，若要更换对话来源请先 switch --cancel。')
            if not path.is_file():
                raise RuntimeIssue('冻结的交接包缺失，请恢复该文件，或取消这次接棒后重新生成。')
            print(f'已冻结的交接包：{path}')
            return relative
        with self.store.transaction(write=False) as tx:
            chain = tx.execute("SELECT seq,account_id FROM segments WHERE thread_id=? AND status<>'failed' AND seq<=? ORDER BY seq",
                               (tid, segment['seq'])).fetchall()
        conversation = segment_conversation(self._probe(sid)) or segment['conversation_id']
        found = self._find_transcript(conversation)
        saved = str(explicit.expanduser().resolve()) if explicit is not None else segment['transcript_path']
        transcript = Path(saved) if saved else (found[0] if found else None)
        if explicit is not None and not transcript.is_file():
            raise _user_error('找不到 --transcript 文件，请检查路径。')
        if transcript is None or not transcript.is_file():
            print('注意：未找到本棒对话日志，将仅交接可用信息。请启用 HUD 或用 --transcript 指定日志。', file=sys.stderr)
        task = ''
        if found:
            task_path = found[1]
            if task_path.is_file() and task_path.resolve().is_relative_to(self.runtime.brain_root.resolve()):
                with task_path.open(encoding='utf-8-sig', errors='replace') as file:
                    task = file.read(262144)
        git_status, git_diff = self.runtime.git_context(Path(thread['cwd']))
        result = HandoffBuilder().build(_read_text(self.store.artifact(thread['anchor_path'])), transcript=transcript,
                 task=task, git_status=git_status, git_diff=git_diff,
                 provenance={'segment_id': sid, 'account_id': segment['account_id'], 'conversation_id': conversation,
                             'transcript_path': str(transcript) if transcript else None,
                             'chain': ' -> '.join(f'#{row[0]}({row[1]})' for row in chain)})
        with self.store.transaction() as tx:
            tx.execute('UPDATE segments SET conversation_id=?,transcript_path=?,handoff_out_path=? WHERE segment_id=?',
                       (conversation, saved, relative, sid))
            write_text_atomic(path, result.text)
            tx.record_event('handoff_frozen' if freeze else 'handoff_preview', thread_id=tid, segment_id=sid,
                            detail={'estimated_size': result.tokens})
        if segment['status'] not in ('running', 'starting'):
            self._probe(sid).unlink(missing_ok=True)
        print(f'交接包：{path}（{result.tokens} / 3,500 Tokens）。')
        return relative

    def _conflicts(self, segment):
        processes = self.runtime.scan_agy_processes()
        with self.store.transaction(write=False) as tx:
            mapping = {(row['pid'], row['pid_create_time']): dict(row) for row in tx.execute('''SELECT s.pid,s.pid_create_time,
                       s.thread_id,s.seq,s.account_id,t.cwd FROM segments s JOIN threads t USING(thread_id) WHERE s.pid IS NOT NULL''')}
        for proc in processes:
            if segment and proc.pid == segment['pid'] and (proc.created is None or proc.created == segment['pid_create_time']):
                raise RuntimeIssue('源会话仍存活，请先在原窗口执行 /exit；--force 不能跳过此检查。', 6)
        return processes, mapping

    def _confirm_conflicts(self, segment, force, *, hard_block_shared: bool = False):
        processes, mapping = self._conflicts(segment)
        if not processes:
            return set()
        for proc in processes:
            row = mapping.get((proc.pid, proc.created))
            started = '未知'
            if proc.created is not None:
                try:
                    started = datetime.fromtimestamp(proc.created / 1e7 - 11644473600, timezone.utc).isoformat(timespec='seconds')
                except (OSError, ValueError, OverflowError):
                    pass
            label = f"托管 {row['thread_id']}·#{row['seq']} {row['account_id']} {redact(row['cwd'])}" if row else '非托管或无法归属'
            print(f'  PID {proc.pid}  {label}  启动时间 {started}', file=sys.stderr)
        if hard_block_shared:
            raise RuntimeIssue('共享登录域存在受影响的 agy 会话（shared_session）；M4 模式下 --force 同样硬阻断，请先退出受影响会话。', 3)
        if not force or not sys.stdin.isatty() or input('继续会影响上述会话。确认请输入 yes：').strip() != 'yes':
            raise RuntimeIssue('发现其他 agy 窗口，请先 /exit，或使用 --force 并在终端确认。', 3)
        return {(p.pid, p.created) for p in processes}

    def _check_new_conflicts(self, segment, approved):
        processes, _ = self._conflicts(segment)
        if any((p.pid, p.created) not in approved for p in processes):
            raise RuntimeIssue('等待期间出现新的 agy 窗口；请退出该窗口后重试，不会自动继续接棒。', 3)

    def switch(self, args):
        if args.cancel and (args.force or args.transcript is not None):
            raise _user_error('--cancel 不可和 --force 或 --transcript 同时使用。')
        tid = self._thread_id(args.tid)
        with self._switch_lease(tid, args.to):
            self._assert_barrier_clear()
            recovered = self.reconcile(recover=True)
            thread, seg = self._thread(tid)
            if not seg:
                raise _user_error('该线程没有可交接的会话。')
            sid = seg['segment_id']
            if args.cancel:
                if not self._is_frozen(seg):
                    raise _user_error('没有待取消的接棒；用 status 查看当前状态。')
                with self.store.transaction() as tx:
                    tx.execute("UPDATE segments SET status='closed' WHERE segment_id=?", (sid,))
                    self._mark_auth(tx, '')
                    tx.record_event('switch_cancel', thread_id=tid, segment_id=sid)
                print('已取消本地接棒，交接包保留；此操作不改变 Google 当前登录状态。')
                return
            if (tid, args.to) in recovered:
                print('已恢复上次接棒的运行状态，无需再启动。')
                return
            if seg.get('account_binding_state') == 'unverified' or seg.get('account_id') is None:
                raise RuntimeIssue('源会话账号尚未归属（identity_unverified），无法换号。', 2)
            account = self._account(args.to)
            if args.to == seg['account_id']:
                raise _user_error('目标槽位与源会话相同，请指定另一个账号。')
            if seg['status'] == 'running':
                self._bundle(thread, seg, args.transcript)
                raise RuntimeIssue('源会话仍存活，已生成交接预览。请先 /exit 再换号。', 6)
            if seg['status'] not in ('closed', 'handoff_pending', 'failed'):
                raise _user_error('当前会话尚不可接棒；请先查看 status。')
            is_m4 = thread.get('origin') == 'm4_native' or seg.get('segment_origin') == 'm4_native'
            approved = self._confirm_conflicts(seg, args.force, hard_block_shared=is_m4)
            bundle = self._bundle(thread, seg, args.transcript, freeze=True)
            cwd = Path(thread['cwd'])
            if len(self.store.artifact(bundle).as_posix()) > 235:
                raise _user_error('交接包路径太长，请缩短 AGY_POOL_HOME 或库路径后重试。')
            with self.store.transaction() as tx:
                if seg['status'] == 'handoff_pending':
                    pending = tx.execute("SELECT account_id FROM events WHERE segment_id=? AND event_type='switch_start' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                    if pending and pending[0] != args.to:
                        raise _user_error('该交接已有不同的目标账号；请先 switch --cancel。')
                tx.execute("UPDATE segments SET status='handoff_pending' WHERE segment_id=?", (sid,))
                self._mark_auth(tx, '')
                tx.record_event('switch_start', thread_id=tid, segment_id=sid, account_id=args.to)
            self._check_new_conflicts(seg, approved)
            self.runtime.guide_account_switch(cwd, account['email_hint'], new_probe(self.store, 'guide'))
            try:
                self.runtime.verify_current_auth(cwd, new_probe(self.store, 'auth'), _identity(account))
            except RuntimeIssue as error:
                self.store.record_event('auth_mismatch' if error.code == 4 else 'auth_probe_unavailable',
                                        thread_id=tid, segment_id=sid, account_id=args.to)
                raise
            self._check_new_conflicts(seg, approved)
            seq = seg['seq'] + 1
            new_id = f'{tid}-seg{seq}'
            with self.store.transaction() as tx:
                self._mark_auth(tx, args.to)
                self._reserve_launch(tx, tid, new_id, seq, args.to, sid, bundle)
            proc = self._launch(tid, new_id, seq, args.to, cwd, parent=sid, bundle=bundle)
        print(f'已启动 {tid} · {args.to} · #{seq}（PID {proc.pid}）。', flush=True)

    def handoff(self, args):
        with self.runtime.switch_lock(self._mutex_name()):
            self.reconcile()
            thread, segment = self._thread(args.tid)
            if not segment:
                raise _user_error('此线程还没有可交接的会话。')
            self._bundle(thread, segment, args.transcript)

    def anchor(self, args):
        with self.runtime.switch_lock(self._mutex_name()):
            thread, segment = self._thread(args.tid)
            if segment and self._is_frozen(segment):
                raise _user_error('交接包已冻结，请先取消本次接棒再修改锚点。')
            path = self.store.artifact(thread['anchor_path'])
            if args.file:
                updated = _read_text(args.file.expanduser())
            elif args.text is not None:
                updated = args.text
            elif sys.stdin.isatty():
                print('请输入锚点内容，单独一行输入 . 完成：')
                lines = []
                while (line := input()) != '.':
                    lines.append(line)
                updated = '\n'.join(lines) + '\n'
            else:
                raise _user_error('请提供 --text 或 --file，或在交互终端编辑。')
            tokens = HandoffBuilder.validate_anchor(updated)
            with self.store.transaction() as tx:
                write_text_atomic(path, updated)
                tx.execute('UPDATE threads SET updated_at=? WHERE thread_id=?', (utc_now(), thread['thread_id']))
                tx.record_event('anchor_edit', thread_id=thread['thread_id'])
        print(f'已保存锚点：{path}（{tokens} / 600 Tokens）。')

    def close(self, args):
        with self.runtime.switch_lock(self._mutex_name()):
            self.reconcile(recover=True)
            thread, segment = self._thread(args.tid, closed=True)
            if segment and self._is_frozen(segment):
                raise _user_error('请先 switch --cancel，再收尾线程。')
            if thread['status'] != 'closed':
                with self.store.transaction() as tx:
                    tx.execute("UPDATE threads SET status='closed',updated_at=? WHERE thread_id=?", (utc_now(), thread['thread_id']))
                    tx.record_event('thread_close', thread_id=thread['thread_id'])
        print(f"已收尾 {thread['thread_id']}；旧窗口不会被终止，历史文件保留。")

    def list(self, args):
        # Strictly read-only (FR-08 / F10): never calls init_db(), reconcile(), process scan, or probe cleanup.
        if not self.store.db.is_file():
            empty = {'active_machine_account': None, 'accounts': [], 'threads': []}
            if args.json:
                _json(empty)
            else:
                print('状态库尚未初始化（只读查询不会自动创建库）。')
            return
        snapshot = self.store.export_snapshot(args.all)
        if args.json:
            _json(snapshot)
            return
        self._show_accounts(snapshot['accounts'], snapshot['active_machine_account'])
        print('任务线程')
        for row in snapshot['threads']:
            acc_display = row['current_account_id'] if row['current_account_id'] is not None else '账号待归属'
            print(f"  {row['thread_id']}  {row['title']}  #{row['current_seq']} · {acc_display} · "
                  f"{row['current_segment_status']}  {row['cwd']}")
        if not snapshot['threads']:
            print('  暂无线程；使用 list --all 查看历史。')

    def status(self, args):
        # Strictly read-only (FR-08 / F10): mode=ro + query_only, zero side effects.
        tid = self._thread_id(args.tid, closed=True)
        info = self.store.thread_status_readonly(tid)
        if not info:
            raise _user_error('线程不存在或已经收尾，请用 list --all 查看。')
        thread = info['thread']
        segment = info['current_segment']
        accounts = info['accounts']
        th_keys = ('thread_id', 'title', 'cwd', 'anchor_path', 'status', 'segment_count')
        seg_keys = ('segment_id', 'seq', 'account_id', 'status', 'pid', 'conversation_id', 'handoff_in_path')
        acc_keys = ('account_id', 'email_hint', 'gemini_5h_remaining')
        if args.json:
            seg_out = None
            if segment:
                seg_out = {key: segment.get(key) for key in seg_keys}
                seg_out['account_binding_state'] = segment.get('account_binding_state', 'verified')
            payload = {
                'thread': {key: thread[key] for key in th_keys},
                'current_segment': seg_out,
                'accounts': [{key: row[key] for key in acc_keys} for row in accounts],
                'handoff_state': info['handoff_state'],
                'pending_intent': info['pending_intent'],
                'delivery_state': info['delivery_state'],
            }
            _json(payload)
        else:
            print(f"{thread['thread_id']}「{thread['title']}」 · {thread['status']}")
            if segment:
                acc_display = segment['account_id'] if segment['account_id'] is not None else '账号待归属'
                print(f"第 {segment['seq']} 棒 · {acc_display} · {segment['status']}")
            if info['pending_intent']:
                pi = info['pending_intent']
                reason = f" ({pi['error_code']})" if pi.get('error_code') else ''
                print(f"待接棒：目标 {pi['target_account_id']} · 状态 {pi['state']}{reason}")
            self._show_accounts(accounts, info['active_machine_account'])

    def run(self, args):
        """M4 managed foreground wrapper entry: spawns native agy with per-launch mailbox & HUD view."""
        native = resolve_native_agy()
        if not native:
            raise RuntimeIssue('找不到原生 agy 可执行文件，请检查安装或 PATH。')
        extra = list(args.agy_args or [])
        if extra and extra[0] == '--':
            extra = extra[1:]
        launch_id = f'LCH-{uuid.uuid4().hex[:12]}'
        mailbox = EventMailbox(self.store, launch_id)
        view = HudViewPublisher(self.store, launch_id)
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('AGY_POOL_')}
        env.update({
            'AGY_POOL_M4_VERSION': '1',
            'AGY_POOL_LAUNCH_ID': launch_id,
            'AGY_POOL_MAILBOX_DIR': str(mailbox.dir),
            'AGY_POOL_VIEW_PATH': str(view.path),
        })
        cwd = Path.cwd().resolve()
        engine = M4Engine(self.store, barrier=self.barrier)
        if sys.platform != 'win32' and sys.stdin.isatty() and sys.stdout.isatty():
            code = run_pty_proxy(
                [native, *extra],
                cwd=cwd,
                env=env,
                hotkey_enabled=True,
                on_tick=lambda: mailbox.poll_and_consume(engine, cwd=cwd),
            )
            if code != 0:
                raise RuntimeIssue(f'agy 退出码 {code}', code)
        else:
            import subprocess

            proc = subprocess.Popen([native, *extra], cwd=cwd, env=env)
            code = proc.wait()
            mailbox.poll_and_consume(engine, cwd=cwd)
            if code != 0:
                raise RuntimeIssue(f'agy 退出码 {code}', code)


def main(argv=None, *, runtime=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        base = Path(os.environ.get('AGY_POOL_HOME', Path.home() / '.antigravity' / 'agy-pool')).expanduser()
        db = Path(os.environ.get('AGY_POOL_DB', base / 'pool.db')).expanduser()
        store = PoolStore(db, base)
        is_readonly = args.command in ('list', 'status') or (args.command == 'account' and args.action == 'list')
        if not is_readonly:
            store.init_db()
        if args.command == 'init':
            print(f'状态库已就绪：{redact(str(store.home))}')
        else:
            controller = Controller(store, runtime if runtime is not None else default_runtime())
            command = getattr(controller, args.command)
            if args.command in ('start', 'switch'):
                try:
                    command(args)
                finally:
                    waited = controller.runtime.finish_session()
                if waited:
                    controller.reconcile()
            else:
                command(args)
        return 0
    except KeyboardInterrupt:
        print('已中断；若已冻结交接包，可重跑 switch 或执行 switch --cancel。一直打开的临时窗口请手动 /exit。', file=sys.stderr)
        return 130
    except M4Error as error:
        print(f'{error.message} [{error.reason_code}]', file=sys.stderr)
        return 3
    except RuntimeIssue as error:
        print(error, file=sys.stderr)
        return error.code
    except AnchorTooLong as error:
        print(error, file=sys.stderr)
        return 5
    except EOFError:
        print('未收到确认或编辑内容，请在交互终端重试。', file=sys.stderr)
        return 2
    except (SensitiveData, ValueError) as error:
        print(redact(str(error)), file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError, StoreError) as error:
        print(f'操作失败：{redact(str(error))} 请检查路径、状态库或 agy 安装后重试。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
