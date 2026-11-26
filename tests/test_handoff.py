import json

import pytest


def test_anchor_limit_and_sensitive_rejection():
    from agy_pool.handoff import AnchorTooLong, HandoffBuilder, estimate_tokens
    from agy_pool.store import SensitiveData

    assert estimate_tokens('测试池pool') == 3
    HandoffBuilder.validate_anchor('中' * 900)
    with pytest.raises(AnchorTooLong, match='601'):
        HandoffBuilder.validate_anchor('中' * 901)
    with pytest.raises(SensitiveData):
        HandoffBuilder.validate_anchor('Authorization: Bearer dummy-value')


def test_handoff_keeps_anchor_and_final_turn_and_redacts(tmp_path):
    from agy_pool.handoff import HandoffBuilder, estimate_tokens

    transcript = tmp_path / 'transcript.jsonl'
    entries = [
        {'step_index': 1, 'type': 'USER_INPUT', 'status': 'DONE', 'content': 'FIRST'},
        {'step_index': 2, 'type': 'PLANNER_RESPONSE', 'status': 'IN_PROGRESS', 'content': 'PRIVATE_DRAFT'},
        {'step_index': 2, 'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': 'FINAL Bearer test-fixture',
         'tool_calls': [{'name': 'edit_file', 'args': {'Path': 'src/auth.py'}}]},
        {'step_index': 3, 'type': 'GENERIC', 'status': 'DONE', 'content': 'NOISY_OUTPUT'},
        {'step_index': 4, 'type': 'GENERIC', 'status': 'ERROR', 'content': 'ERR Cookie: SID=dummy-test'},
    ]
    transcript.write_text('\n'.join(map(json.dumps, entries)) + '\n{', encoding='utf-8')
    anchor = '# 不变\n  保留空格\n' + '中' * 870
    result = HandoffBuilder().build(anchor, transcript=transcript,
                                    task='- [x] 已完成\n' + '- [ ] ' + '中' * 20000,
                                    git_status='\n'.join(f' M src/{i}.py' for i in range(500)),
                                    provenance={'segment_id': 'T1-seg1'})
    assert anchor in result.text
    assert all(value in result.text for value in ('FIRST', 'FINAL [REDACTED]', 'ERR [REDACTED]', 'src/auth.py', 'T1-seg1', '已完成'))
    assert not any(secret in result.text for secret in ('PRIVATE_DRAFT', 'NOISY_OUTPUT', 'test-fixture', 'dummy-test'))
    assert result.tokens == estimate_tokens(result.text) <= 3500


@pytest.mark.parametrize('sensitive', ['token: fabricated-fixture', '{"secret": "fabricated-fixture"}'])
def test_sensitive_field_in_plain_text_is_rejected_and_redacted(tmp_path, sensitive):
    from agy_pool.handoff import HandoffBuilder
    from agy_pool.store import SensitiveData

    with pytest.raises(SensitiveData):
        HandoffBuilder.validate_anchor(sensitive)
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text(json.dumps({'step_index': 1, 'type': 'USER_INPUT', 'status': 'DONE',
                                     'content': '前缀 ' + sensitive}), encoding='utf-8')
    text = HandoffBuilder().build('Keep the goal', transcript=transcript).text
    assert '前缀' in text and '[REDACTED]' in text
    assert 'fabricated-fixture' not in text
    HandoffBuilder.validate_anchor('Token budget and secret management')


def test_missing_sources_are_explicit():
    from agy_pool.handoff import HandoffBuilder

    text = HandoffBuilder().build('only anchor').text
    assert all(value in text for value in ('L1', 'L2', 'L3', '暂无历史对话记录', '非 Git 仓库'))
