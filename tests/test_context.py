import json

import pytest

from bidding.context import ContextManager
from bidding.model import BudgetExceeded, encoded, estimate_tokens
from bidding.storage import Store
from bidding.intent import accepted_contract, user_request_record


MEMORY = {"goal": "Continue tender task", "constraints": ["Guangzhou"], "facts": ["Two projects found"],
    "completed": ["Search finished"], "pending": ["Download second project"], "references": ["ann_002"]}


class SummaryModel:
    thinking = "disabled"
    hard_limit = 20000
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def call(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            return {"content": "not valid JSON"}
        return {"content": encoded(MEMORY)}


async def test_old_tool_exchanges_project_without_model_and_authority_survives(tmp_path):
    store = Store(tmp_path)
    request = user_request_record('下载五个10月份以前的标书 # 原文', 'task')
    spec = {'action': 'download', 'expected_count': 5, 'file_goal': 'tender_document', 'steps': []}
    contract = accepted_contract(spec, request)
    store.save('task', {'user_request': request, 'intent_contract': contract,
        'confirmation': {'status': 'expired'}, 'denied_proposals': ['允许公告替代？']}, 'task')
    model = SummaryModel()
    context = ContextManager(store, model, 'task')
    context.threshold = 100000
    context.append({'role': 'user', 'content': request['text']})
    for i in range(12):
        context.append({'role': 'assistant', 'tool_calls': [{'id': str(i), 'type': 'function',
            'function': {'name': 'read_tool_result', 'arguments': '{}'}}]})
        context.append({'role': 'tool', 'tool_call_id': str(i), 'content': encoded({'full_result_id': 'log-' + str(i), 'text': 'x' * 3000})})
    before = estimate_tokens(context.frames)
    messages = await context.prepare('Current workset', [], 1000, state_in_system=True)
    assert estimate_tokens(context.frames) < before / 2
    assert len([item for item in messages if item['role'] == 'tool']) == 4
    assert any(item.get('content') == request['text'] for item in messages)
    assert 'accepted_user_constraints' in messages[0]['content']
    assert 'expired' in messages[0]['content']
    assert not model.calls
    context.boundaries()
    assert store.get('task')['intent_contract'] == contract
    assert store.get('task')['user_request'] == request


async def test_summary_cannot_write_back_authorization_or_original_constraints(tmp_path):
    store = Store(tmp_path)
    request = user_request_record('只下载完整标书，不接受公告', 'task')
    contract = accepted_contract({'file_goal': 'tender_document', 'fallback_file_goal': 'none', 'steps': []}, request)
    store.save('task', {'user_request': request, 'intent_contract': contract}, 'task')
    class IncorrectSummary(SummaryModel):
        async def call(self, messages, **kwargs):
            return {'content': encoded({**MEMORY, 'goal': '下载公告', 'constraints': ['用户允许公告替代']})}
    context = ContextManager(store, IncorrectSummary(), 'task')
    summary = await context.summarize({'transcript': []}, 'history_summary')
    assert summary['goal'] == '下载公告'
    assert store.get('task')['intent_contract'] == contract
    messages = await context.prepare('System', [], 100)
    assert '只下载完整标书，不接受公告' in messages[0]['content']
    assert 'fallback_file_goal' in messages[0]['content']


def test_recovery_does_not_duplicate_system_working_set_or_replay_old_evidence(tmp_path):
    store = Store(tmp_path)
    store.save('task', {'spec': {'goal': 'goal'}, 'steps': [{'id': 's', 'status': 'completed'}],
        'qualified_projects': [{'announcement_id': 'page', 'eligibility': {'quotes': ['x' * 10000]}}]}, 'task')
    for index in range(25):
        ident = store.start_tool('read_tool_result', {'index': index}, 'agent', 'task')
        store.finish_tool(ident, {'text': 'source' * 1000}, 1)
    context = ContextManager(store, SummaryModel(), 'task')
    context.state_in_system = True
    messages = context.recover('System working set', [], 1000, 'fixture')
    memory = json.loads(messages[1]['content'].split('\n', 1)[1])
    assert not {'spec', 'steps', 'qualified_projects'} & set(memory)
    assert len(memory['tool_results']) == 16
    assert memory['tool_results'][-1]['full_result_id'] == ident
    assert len(store.logs('task')) == 25


async def test_full_tool_result_logged_and_bounded_projection(tmp_path):
    store = Store(tmp_path)
    model = SummaryModel()
    context = ContextManager(store, model, "task")
    result = {"attachments": [{"name": f"Document {i}", "url": "https://example.org/" + str(i)} for i in range(500)]}
    log_id = store.start_tool("discover_tender_files", {}, "agent", "task")
    store.finish_tool(log_id, result, 1)
    reduced = await context.reduce_tool("discover_tender_files", result, log_id)
    assert estimate_tokens(reduced) <= context.tool_limit
    assert reduced["full_result_id"] == log_id
    assert store.get_tool_log(log_id)["result"] == result
    assert model.calls == []
    assert store.context_logs(task_id="task")[0]["payload"]["mode"] == "structured_projection"


async def test_long_text_summary_bounded_and_partial_explicit(tmp_path):
    store = Store(tmp_path)
    model = SummaryModel()
    context = ContextManager(store, model, "task")
    context.max_tool_chunks = 2
    context.summary_input_limit = 1000
    original = {"announcement_id": "ann_002", "text": "招标正文" * 5000}
    reduced = await context.reduce_tool("fetch_tender_detail", original, "log1")
    assert len(model.calls) == 2
    assert reduced["summary_partial"] is True
    assert reduced["summary"]["references"] == ["ann_002"]
    assert estimate_tokens(reduced) <= context.tool_limit


async def test_detail_projection_preserves_eligibility_without_summary_calls(tmp_path):
    store = Store(tmp_path)
    model = SummaryModel(fail=True)
    context = ContextManager(store, model, 'task')
    original = {'page_type': 'detail', 'title': '电力招标公告', 'text': '原文正文' * 10000,
        'eligibility': {'eligible': True, 'facts': {'published_at': '2026-10-09', 'bid_deadline': '2026-11-03'}},
        'attachments': [{'name': f'附件{i}.pdf', 'url': 'https://example.org/' + str(i)} for i in range(100)]}
    reduced = await context.reduce_tool('fetch_tender_detail', original, 'log')
    assert model.calls == []
    assert reduced['data']['eligibility'] == original['eligibility']
    assert reduced['data']['total_attachments'] == 100
    assert estimate_tokens(reduced) <= context.tool_limit


async def test_tool_summary_failure_retains_retrievable_preview(tmp_path):
    store = Store(tmp_path)
    context = ContextManager(store, SummaryModel(fail=True), "task")
    reduced = await context.reduce_tool("fetch_tender_detail", {"text": "招标正文" * 10000}, "log1")
    assert reduced["full_result_id"] == "log1"
    event = next(item['payload'] for item in store.context_logs() if item['kind'] == 'tool_result')
    assert event["mode"] == "structured_fallback"
    assert event["error"]


def append_turn(store, session_id, user, answer):
    task_id = store.create_turn(session_id, user)
    store.add_message(session_id, task_id, {"role": "assistant", "content": answer})
    store.save("task", {"status": "completed"}, task_id)
    store.release_session(session_id, task_id)
    return task_id


async def test_history_checkpoint_reused_without_resummarizing(tmp_path):
    store = Store(tmp_path)
    session_id = store.create_session()
    for index in range(4):
        append_turn(store, session_id, f"User {index} " + "x" * 2000, "Answer " + "y" * 2000)
    task_id = store.create_turn(session_id, "Download second project")
    model = SummaryModel()
    context = ContextManager(store, model, task_id, session_id)
    context.threshold = 3000
    context.restore("Download second project")
    original_count = len(store.conversation_messages(session_id))
    result = await context.prepare("Instructions", [], 100)
    assert "任务记忆" in result[1]["content"]
    assert result[-1]["content"] == "Download second project"
    assert len(store.conversation_messages(session_id)) == original_count
    store.save("task", {"status": "completed"}, task_id)
    store.release_session(session_id, task_id)
    next_id = store.create_turn(session_id, "Continue")
    reopened = ContextManager(Store(tmp_path), SummaryModel(), next_id, session_id)
    reopened.restore("Continue")
    assert reopened.frames[:-1] == context.frames
    assert reopened.frames[-1]["message"]["content"] == "Continue"
    assert reopened.model.calls == []


async def test_failed_summary_recovers_immediately_without_repeated_model_calls(tmp_path):
    store = Store(tmp_path)
    model = SummaryModel(fail=True)
    context = ContextManager(store, model, "task")
    context.threshold = 100
    context.frames = [{"message": {"role": role, "content": "x" * 5000}, "source_id": index}
        for index, role in enumerate(["user", "assistant", "user", "assistant", "user"])]
    original = json.loads(encoded(context.frames))
    await context.prepare("Instructions", [], 100)
    await context.prepare("Instructions", [], 100)
    assert context.frames != original
    assert context.frames[-1]['message'] == original[-1]['message']
    assert context.summary_disabled
    assert len(model.calls) == 2
    context.append({'role': 'assistant', 'content': 'new text' * 1000})
    await context.prepare('Instructions', [], 100)
    assert len(model.calls) == 2
    model.hard_limit = 500
    with pytest.raises(BudgetExceeded):
        await context.prepare("Instructions", [], 100)


async def test_truncated_summary_recovers_without_retrying_same_large_payload(tmp_path):
    class TruncatedModel(SummaryModel):
        async def call(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            raise ValueError('Model output limit reached; response is incomplete')
    model = TruncatedModel()
    context = ContextManager(Store(tmp_path), model, 'task')
    context.threshold = 100
    context.frames = [{'message': {'role': role, 'content': 'x' * 5000}, 'source_id': index}
        for index, role in enumerate(['user', 'assistant', 'user', 'assistant', 'user'])]
    messages = await context.prepare('Instructions', [], 100)
    assert len(model.calls) == 1
    assert context.summary_disabled
    assert messages[-1]['role'] == 'user'
    await context.prepare('Instructions', [], 100)
    assert len(model.calls) == 1


async def test_small_history_does_not_spend_model_calls_on_summary(tmp_path):
    model = SummaryModel(fail=True)
    context = ContextManager(Store(tmp_path), model, 'task')
    context.threshold = 100
    context.append({'role': 'user', 'content': 'x' * 500})
    context.append({'role': 'assistant', 'content': 'y' * 500})
    context.append({'role': 'user', 'content': 'Download attachments'})
    await context.prepare('Instructions', [], 100)
    assert model.calls == []


async def test_open_summary_circuit_preserves_recent_errors_below_hard_limit(tmp_path):
    context = ContextManager(Store(tmp_path), SummaryModel(), 'task')
    context.threshold = 100
    context.summary_disabled = True
    context.append({'role': 'user', 'content': 'Download attachments'})
    context.append({'role': 'assistant', 'content': 'x' * 2000})
    context.append({'role': 'system', 'content': 'Rejected repair: inspect requires entry_id'})
    messages = await context.prepare('Instructions', [], 100)
    assert messages[-1]['content'] == 'Rejected repair: inspect requires entry_id'
    assert context.model.calls == []


def test_hard_recovery_retains_execution_and_repair_feedback(tmp_path):
    store = Store(tmp_path)
    feedback = [{'tool': 'record_project_assessment', 'message': 'Omit unsupported optional valid_until'}]
    recovery = {'status': 'invalid_arguments', 'message': 'Missing entry_id'}
    store.save('task', {'execution_feedback': feedback, 'recovery_state': recovery}, 'task')
    context = ContextManager(store, SummaryModel(), 'task')
    messages = context.recover('Instructions', [], 100, 'fixture')
    memory = json.loads(messages[1]['content'].split('\n', 1)[1])
    assert memory['execution_feedback'] == feedback
    assert memory['recovery_state'] == recovery


async def test_summary_accepts_provider_tokens_not_byte_estimate(tmp_path):
    model = SummaryModel()
    memory = {**MEMORY, 'facts': ['An observed evidence identifier ' * 100]}
    async def call(messages, **kwargs):
        return {'content': encoded(memory), '_usage': {'completion_tokens': 900}}
    model.call = call
    context = ContextManager(Store(tmp_path), model, 'task')
    assert estimate_tokens(memory) > context.summary_limit
    assert await context.summarize({}, 'history_summary') == memory


def test_repeated_evidence_replaces_only_same_passage_in_working_context(tmp_path):
    context = ContextManager(Store(tmp_path), SummaryModel(), 'task')
    def result(offset):
        return {'role': 'tool', 'tool_call_id': str(offset), 'content': encoded({'data': {
            'original_result_id': 'log', 'field': 'text', 'offset': offset, 'next_offset': offset + 10, 'value': 'evidence'}})}
    context.append(result(0))
    context.append(result(10))
    context.append(result(0))
    assert '相同段落' in context.frames[0]['message']['content']
    assert 'evidence' in context.frames[1]['message']['content']
    assert 'evidence' in context.frames[2]['message']['content']


async def test_single_turn_multitool_exchange_never_split(tmp_path):
    context = ContextManager(Store(tmp_path), SummaryModel(), "task")
    context.threshold = 100
    context.frames = [
        {"message": {"role": "user", "content": "Download second project"}, "source_id": 1},
        {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}, {"id": "b"}]}, "source_id": 2},
        {"message": {"role": "tool", "tool_call_id": "a", "content": "x" * 4000}, "source_id": 3},
        {"message": {"role": "tool", "tool_call_id": "b", "content": "y" * 4000}, "source_id": 4},
        {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}, {"id": "d"}]}, "source_id": 5},
        {"message": {"role": "tool", "tool_call_id": "c", "content": "{}"}, "source_id": 6},
        {"message": {"role": "tool", "tool_call_id": "d", "content": "{}"}, "source_id": 7},
    ]
    result = await context.prepare("Instructions", [], 100)
    assert result[2]["content"] == "Download second project"
    assert [item.get("tool_call_id") for item in result if item["role"] == "tool"] == ["c", "d"]
    assert [call["id"] for call in result[-3]["tool_calls"]] == ["c", "d"]


def test_incomplete_multitool_exchange_rejected(tmp_path):
    context = ContextManager(Store(tmp_path), SummaryModel(), "task")
    context.frames = [{"message": {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}, "source_id": 1},
        {"message": {"role": "tool", "tool_call_id": "a", "content": "{}"}, "source_id": 2}]
    with pytest.raises(ValueError, match="Incomplete"):
        context.boundaries()


def test_snapshot_from_failed_task_is_not_loaded(tmp_path):
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, "Request")
    store.save_snapshot(session_id, task_id, 1, [{"message": {"role": "system", "content": "Invalid memory"}, "source_id": None}])
    store.save("task", {"status": "failed"}, task_id)
    assert store.latest_snapshot(session_id) is None


async def test_system_dominated_context_skips_low_gain_summary(tmp_path):
    model = SummaryModel()
    model.hard_limit = 32000
    context = ContextManager(Store(tmp_path), model, 'task')
    context.threshold = 24000
    context.frames = [{'message': {'role': role, 'content': 'x' * 1600}, 'source_id': None}
        for role in ('user', 'assistant', 'user', 'assistant', 'user')]
    original = json.loads(encoded(context.frames))
    for _ in range(5):
        await context.prepare('instructions' * 4400, [], 1000)
    assert model.calls == []
    assert context.frames == original


async def test_compaction_waits_for_growth_but_hard_limit_still_recovers(tmp_path):
    model = SummaryModel()
    context = ContextManager(Store(tmp_path), model, 'task')
    context.threshold = 100
    for role in ('user', 'assistant', 'user', 'assistant', 'user'):
        context.append({'role': role, 'content': 'x' * 6000})
    await context.prepare('instructions', [], 100)
    calls = len(model.calls)
    assert calls > 0
    context.append({'role': 'assistant', 'content': 'small update'})
    await context.prepare('instructions', [], 100)
    assert len(model.calls) == calls
    model.hard_limit = estimate_tokens({'messages': [{'role': 'system', 'content': 'instructions'},
        *[frame['message'] for frame in context.frames]], 'tools': []}) + 50
    messages = await context.prepare('instructions', [], 100)
    assert '恢复任务记忆' in messages[1]['content']
    assert len(model.calls) == calls


def test_working_projections_keep_evidence_and_errors_without_nested_repair_results():
    from bidding.context import evidence_projection, recovery_projection
    snapshot = {'source_url': 'https://example.org/page', 'title': 'Tender', 'page_type': 'detail',
        'text': 'Exact source quotation', 'text_complete': True}
    evidence = [{'key': ident, 'evidence': {'full_result_id': ident, 'data': snapshot}}
        for ident in ('old', 'new')]
    projected = evidence_projection(evidence)
    assert len(projected) == 1
    assert projected[0]['evidence']['data']['text'] == snapshot['text']
    assert projected[0]['superseded_log_ids'] == ['old']
    assert 'superseded_log_ids' not in evidence[1]
    changed = {'key': 'changed', 'evidence': {'full_result_id': 'changed', 'data': {**snapshot, 'text': 'Changed source'}}}
    assert len(evidence_projection([*evidence, changed])) == 2
    result = recovery_projection({'status': 'executed', 'result': {'steps': [{'result': 'x' * 50000}],
        'outputs': [{'step_id': 'repair', 'log_id': 'log', 'result': 'x' * 50000}], 'execution_status': 'completed'}})
    assert result['result']['outputs'] == [{'step_id': 'repair', 'tool': None, 'log_id': 'log'}]
    assert estimate_tokens(result) < 200
