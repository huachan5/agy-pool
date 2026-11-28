from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .store import guard, redact


_CJK = re.compile(r'[㐀-鿿豈-﫿぀-ヿ가-힯\U00020000-\U000323af]')
_TASK = re.compile(r'^\s*(?:[-*+]|\d+\.)\s+\[([ xX/])\]\s+(.+)$')
_CTRL_OR_NL = re.compile(r'[\x00-\x1f\x7f]')

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_LINE_BYTES = 256 * 1024
MAX_HANDOFF_TOKENS = 3500
MAX_PROMPT_CHARS = 250


class AnchorTooLong(ValueError):
    pass


class NoHandoffEvidence(ValueError):
    """Raised when there is zero usable evidence to freeze an M4 handoff packet."""


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    return (8 * cjk + 3 * (len(text) - cjk) + 11) // 12


def _safe_excerpt(text: str, limit: int) -> str:
    if not isinstance(text, str):
        return ''
    safe = redact(text).strip()
    return safe if len(safe) <= limit else safe[:limit - 1] + '…'


def _budget(text: str, tokens: int) -> str:
    if estimate_tokens(text) <= tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid] + '…') <= tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + '…' if tokens > 0 else ''


def _git_excerpt(text: str, budget: int) -> str:
    lines = redact(text).strip().splitlines()
    if len(lines) > 25:
        lines = lines[:25] + [f'…及另外 {len(lines) - 25} 项']
    return _budget('\n'.join(lines), budget)


def _task_progress(text: str) -> str:
    result = []
    states = {'x': 'completed', 'X': 'completed', '/': 'in_progress', ' ': 'pending'}
    for line in text.splitlines():
        if match := _TASK.match(line):
            result.append(f'- {states[match[1]]}：{_safe_excerpt(match[2], 180)}')
        if len(result) == 50:
            break
    return _budget('\n'.join(result), 260) or '无记录'


@dataclass(frozen=True)
class _ParsedTranscript:
    turns: list[list[tuple[str, str]]]
    planned_paths: list[str]
    error: str
    flags: tuple[str, ...]


def _parse_transcript(path: Path | None) -> _ParsedTranscript:
    if path is None:
        return _ParsedTranscript([], [], '', ('transcript_missing',))
    if not path.is_file():
        return _ParsedTranscript([], [], '', ('transcript_missing',))

    entries: dict[int, tuple[str, str, list[str]]] = {}
    flags: list[str] = []
    total_bytes = 0
    saw_bad_line = False
    saw_in_progress = False
    truncated_size = False

    try:
        with path.open('rb') as raw:
            for raw_line in raw:
                total_bytes += len(raw_line)
                if total_bytes > MAX_TRANSCRIPT_BYTES:
                    truncated_size = True
                    break
                if len(raw_line) > MAX_TRANSCRIPT_LINE_BYTES:
                    saw_bad_line = True
                    continue
                line = raw_line.decode('utf-8-sig', errors='replace')
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, RecursionError):
                    saw_bad_line = True
                    continue
                if not isinstance(item, dict) or type(item.get('step_index')) is not int:
                    continue
                step = item['step_index']
                kind, status = item.get('type'), item.get('status')
                if status == 'ERROR':
                    entries[step] = ('ERROR', _safe_excerpt(item.get('content'), 300), [])
                elif status == 'IN_PROGRESS':
                    saw_in_progress = True
                    entries.pop(step, None)
                elif kind in ('USER_INPUT', 'USER_EXPLICIT', 'PLANNER_RESPONSE') and status == 'DONE':
                    calls = item.get('tool_calls')
                    paths = []
                    for call in calls if isinstance(calls, list) else ():
                        if isinstance(call, dict) and call.get('name') in ('edit_file', 'write_file'):
                            args = call.get('args')
                            if isinstance(args, dict) and (value := _safe_excerpt(args.get('Path'), 200)):
                                paths.append(value)
                    norm_kind = 'USER_INPUT' if kind in ('USER_INPUT', 'USER_EXPLICIT') else 'PLANNER_RESPONSE'
                    entries[step] = (norm_kind, _safe_excerpt(item.get('content'), 2500), paths[-25:])
                else:
                    entries.pop(step, None)
    except OSError:
        return _ParsedTranscript([], [], '', ('transcript_unreadable',))

    turns: list[list[tuple[str, str]]] = []
    paths_map: dict[str, None] = {}
    error = ''
    for step in sorted(entries):
        kind, text, edited = entries[step]
        if kind == 'ERROR':
            error = text
            continue
        for file in edited:
            paths_map.pop(file, None)
            paths_map[file] = None
        if kind == 'USER_INPUT' or not turns:
            turns.append([])
            turns = turns[-6:]
        if text:
            turns[-1].append(('用户' if kind == 'USER_INPUT' else '结论', text))
            turns[-1] = turns[-1][-4:] if len(turns[-1]) < 5 else [turns[-1][0], *turns[-1][-3:]]

    if saw_bad_line or saw_in_progress:
        flags.append('tail_incomplete')
    if truncated_size:
        flags.append('transcript_size_capped')
    cleaned_turns = [turn for turn in turns if turn]
    if not cleaned_turns and not error:
        flags.append('transcript_empty')
    return _ParsedTranscript(cleaned_turns, list(paths_map)[-25:], error, tuple(dict.fromkeys(flags)))


def _dialogue(path: Path | None) -> tuple[list[list[tuple[str, str]]], list[str], str]:
    parsed = _parse_transcript(path)
    return parsed.turns, parsed.planned_paths, parsed.error


@dataclass(frozen=True)
class HandoffResult:
    text: str
    tokens: int


@dataclass(frozen=True)
class M4HandoffResult:
    text: str
    tokens: int
    quality: str  # 'complete' | 'partial'
    evidence_flags: tuple[str, ...]
    sha256: str
    bytes: int


def build_handoff_prompt(packet_path: Path) -> str:
    """Build a safe single-line <=250-char -i prompt referencing a verified packet path."""
    path_str = packet_path.as_posix()
    if _CTRL_OR_NL.search(path_str):
        raise ValueError('交接包路径含控制字符或换行，已拒绝构造启动指令。')
    guard(path_str)
    prompt = f'请先用 read_file 读取交接包 {path_str}，核对目标、进度与待核对项后再继续任务（不自动同意工具审批）。'
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = f'请先读取交接包 {path_str}，核对目标和进度后再继续任务。'
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = f'读取交接包 {path_str} 并继续。'
    if len(prompt) > MAX_PROMPT_CHARS or _CTRL_OR_NL.search(prompt):
        raise ValueError('交接包路径太长或包含非法字符，请缩短 AGY_POOL_HOME 后重试。')
    return prompt


class HandoffBuilder:
    @staticmethod
    def validate_anchor(text: str, max_tokens: int = 600) -> int:
        tokens = estimate_tokens(text)
        if tokens > max_tokens:
            raise AnchorTooLong(f'锚点 {tokens} Tokens，超过 {max_tokens} 上限，请精简后重试。')
        guard(text)
        return tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return estimate_tokens(text)

    def build(self, anchor: str, *, transcript: Path | None = None, task: str = '',
              git_status: str | None = None, git_diff: str = '', provenance: dict | None = None) -> HandoffResult:
        self.validate_anchor(anchor)
        turns, paths, error = _dialogue(transcript)
        git = '非 Git 仓库' if git_status is None else (
            _git_excerpt(git_status, 110) + '\n' + _git_excerpt(git_diff, 110)).strip() or '无改动'
        l1 = f'# L1 任务现状\n\n## 锚点\n\n{anchor}\n\n## 任务进度\n{_task_progress(task)}\n\n## Git\n{git}\n'
        extras = (f'\n改动文件：{_budget("、".join(paths), 220)}' if paths else '')
        if error:
            extras += f'\n最新报错：{error}'
        l3_lines = []
        for key in ('segment_id', 'transcript_path', 'account_id', 'conversation_id', 'chain'):
            if (value := (provenance or {}).get(key)) is not None:
                l3_lines.append(f'- {key}: {_safe_excerpt(str(value), 280)}')
        l3 = '# L3 溯源指针\n\n' + (_budget('\n'.join(l3_lines), 280) or '无记录') + '\n'

        def render_dialogue(limit: int) -> str:
            return '\n\n'.join('\n'.join(f'- {label}：{_budget(value, limit)}' for label, value in turn)
                               for turn in turns) or '暂无历史对话记录'

        l2_limit = min(1980, 3480 - estimate_tokens(l1 + l3 + extras))
        dialogue = render_dialogue(400)
        while len(turns) > 2 and estimate_tokens(dialogue) > l2_limit:
            turns.pop(0)
            dialogue = render_dialogue(400)
        if turns and estimate_tokens(dialogue) > l2_limit:
            dialogue = render_dialogue(max(1, (l2_limit - 80) // sum(map(len, turns))))
        text = f'{l1}\n# L2 对话精华\n\n{_budget(dialogue, l2_limit)}{extras}\n\n{l3}'
        return HandoffResult(text, estimate_tokens(text))

    def build_m4(
        self,
        anchor: str,
        *,
        transcript: Path | None = None,
        task: str = '',
        git_status: str | None = None,
        git_diff: str = '',
        provenance: dict | None = None,
        anchor_is_placeholder: bool = False,
    ) -> M4HandoffResult:
        """Build an immutable M4 handoff packet with strict quality, redaction, and 3500-token guards."""
        clean_anchor = anchor.strip()
        if clean_anchor:
            self.validate_anchor(anchor)

        parsed = _parse_transcript(transcript)
        turns = [list(t) for t in parsed.turns]
        task_text = _task_progress(task)
        has_task = task_text != '无记录'
        has_git = bool(git_status is not None and (git_status.strip() or git_diff.strip()))
        has_anchor = bool(clean_anchor) and not anchor_is_placeholder
        has_dialogue = bool(turns) or bool(parsed.error)

        if not (has_anchor or has_dialogue or has_task or has_git):
            raise NoHandoffEvidence('缺少可用的交接内容（无日志、无目标、无任务进度或工作区改动），已暂停冻结交接包。')

        flags = list(parsed.flags)
        if not has_anchor:
            flags.append('anchor_missing')
        partial_reasons = {'transcript_missing', 'transcript_unreadable', 'transcript_empty', 'tail_incomplete'}
        quality = 'partial' if any(f in partial_reasons for f in flags) else 'complete'

        notice = ''
        if quality == 'partial':
            notice = (
                f'## 交接完整度：部分交接／待核对\n'
                f'- 证据标记：{", ".join(flags)}\n'
                f'- 说明：末轮日志未完整落盘或缺失，请先核对工作区与目标进度，切勿假定未完成工具已执行。\n\n'
            )

        anchor_section = anchor if clean_anchor else '待核对（自动任务未提供人工锚点）'
        git_body = '非 Git 仓库' if git_status is None else (
            _git_excerpt(git_status, 110) + '\n' + _git_excerpt(git_diff, 110)
        ).strip() or '无改动'
        l1 = (
            f'# L1 任务现状\n\n{notice}'
            f'## 锚点\n\n{anchor_section}\n\n'
            f'## 任务进度\n{task_text}\n\n'
            f'## Git 工作区状态（不归因单分屏）\n{git_body}\n'
        )

        extras = ''
        if parsed.planned_paths:
            extras += f'\n计划涉及文件（仅规划记录，未证实写入成功）：{_budget("、".join(parsed.planned_paths), 200)}'
        if parsed.error:
            extras += f'\n最新报错：{parsed.error}'

        l3_lines = ['- 禁止盲目重放历史命令或工具调用，请先核对当前工作区状态。']
        for key in ('segment_id', 'account_id', 'conversation_id', 'chain', 'intent_id'):
            if (value := (provenance or {}).get(key)) is not None:
                l3_lines.append(f'- {key}: {_safe_excerpt(str(value), 240)}')
        l3 = '# L3 溯源指针\n\n' + _budget('\n'.join(l3_lines), 280) + '\n'

        def render_dialogue(limit: int) -> str:
            return '\n\n'.join(
                '\n'.join(f'- {label}：{_budget(value, limit)}' for label, value in turn)
                for turn in turns
            ) or '暂无已落盘的完整历史对话记录'

        header_footer_tokens = estimate_tokens(l1 + '\n# L2 对话精华\n\n' + extras + '\n\n' + l3)
        l2_limit = max(0, min(1980, MAX_HANDOFF_TOKENS - 20 - header_footer_tokens))
        dialogue = render_dialogue(400) if l2_limit > 0 else ''
        while len(turns) > 2 and estimate_tokens(dialogue) > l2_limit:
            turns.pop(0)
            dialogue = render_dialogue(400)
        if turns and estimate_tokens(dialogue) > l2_limit:
            total_items = sum(map(len, turns))
            dialogue = render_dialogue(max(1, (l2_limit - 80) // max(1, total_items)))

        text = f'{l1}\n# L2 对话精华\n\n{_budget(dialogue, l2_limit)}{extras}\n\n{l3}'
        text = redact(text)
        tokens = estimate_tokens(text)
        if tokens > MAX_HANDOFF_TOKENS:
            # Final hard clamp preserving L1 + partial notice + L3 while trimming L2.
            over = tokens - MAX_HANDOFF_TOKENS + 10
            trimmed_l2 = _budget(dialogue, max(0, l2_limit - over))
            text = redact(f'{l1}\n# L2 对话精华\n\n{trimmed_l2}{extras}\n\n{l3}')
            tokens = estimate_tokens(text)
        if tokens > MAX_HANDOFF_TOKENS:
            raise ValueError(f'交接包最终体积 {tokens} Tokens 超过 {MAX_HANDOFF_TOKENS} 上限。')
        guard(text)

        raw = text.encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        return M4HandoffResult(
            text=text,
            tokens=tokens,
            quality=quality,
            evidence_flags=tuple(dict.fromkeys(flags)),
            sha256=digest,
            bytes=len(raw),
        )
