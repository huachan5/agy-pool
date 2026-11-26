from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .store import guard, redact


_CJK = re.compile(r'[㐀-鿿豈-﫿぀-ヿ가-힯\U00020000-\U000323af]')
_TASK = re.compile(r'^\s*(?:[-*+]|\d+\.)\s+\[([ xX/])\]\s+(.+)$')


class AnchorTooLong(ValueError):
    pass


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


def _dialogue(path: Path | None) -> tuple[list[list[tuple[str, str]]], list[str], str]:
    if path is None:
        return [], [], ''
    entries = {}
    try:
        with path.open(encoding='utf-8-sig', errors='replace') as file:
            for line in file:
                try:
                    item = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(item, dict) or type(item.get('step_index')) is not int:
                    continue
                step = item['step_index']
                kind, status = item.get('type'), item.get('status')
                if status == 'ERROR':
                    entries[step] = ('ERROR', _safe_excerpt(item.get('content'), 300), [])
                elif kind in ('USER_INPUT', 'PLANNER_RESPONSE') and status == 'DONE':
                    calls = item.get('tool_calls')
                    paths = []
                    for call in calls if isinstance(calls, list) else ():
                        if isinstance(call, dict) and call.get('name') in ('edit_file', 'write_file'):
                            args = call.get('args')
                            if isinstance(args, dict) and (value := _safe_excerpt(args.get('Path'), 200)):
                                paths.append(value)
                    entries[step] = (kind, _safe_excerpt(item.get('content'), 2500), paths[-25:])
                else:
                    entries.pop(step, None)
    except OSError:
        return [], [], ''
    turns: list[list[tuple[str, str]]] = []
    paths: dict[str, None] = {}
    error = ''
    for step in sorted(entries):
        kind, text, edited = entries[step]
        if kind == 'ERROR':
            error = text
            continue
        for file in edited:
            paths.pop(file, None)
            paths[file] = None
        if kind == 'USER_INPUT' or not turns:
            turns.append([])
            turns = turns[-6:]
        if text:
            turns[-1].append(('用户' if kind == 'USER_INPUT' else '结论', text))
            turns[-1] = turns[-1][-4:] if len(turns[-1]) < 5 else [turns[-1][0], *turns[-1][-3:]]
    return [turn for turn in turns if turn], list(paths)[-25:], error


@dataclass(frozen=True)
class HandoffResult:
    text: str
    tokens: int


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
