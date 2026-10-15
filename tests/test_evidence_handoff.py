import json

import httpx

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.context import ContextManager, page_preview
from bidding.model import encoded, estimate_tokens
from bidding.storage import Store
from model_fixtures import planning_response
from test_context import SummaryModel


def test_short_page_text_is_complete_and_empty_attachments_are_explicit():
    original = {'announcement_id': 'page', 'source_url': 'https://example.org/project',
        'page_type': 'detail', 'text': 'Original announcement body', 'attachments': [],
        'document_access': [{'entry_id': 'get-file', 'title': 'Get documents'}], 'network': ['noise' * 10000]}
    result = page_preview(original, 'log', 2000)
    assert result['data']['text'] == original['text']
    assert result['data']['text_complete']
    assert result['data']['attachments_complete']
    assert result['data']['attachment_status'] == 'no_observed_attachments'
    assert result['data']['document_access'] == original['document_access']
    assert 'network' not in result['data']
    original['text_truncated'] = True
    assert not page_preview(original, 'log', 2000)['data']['text_complete']


async def test_bounded_batch_keeps_three_short_source_bodies(tmp_path):
    context = ContextManager(Store(tmp_path), SummaryModel(), 'task')
    outputs = []
    for index in range(3):
        page = {'announcement_id': str(index), 'page_type': 'detail', 'text': 'Source text ' * 70, 'attachments': []}
        outputs.append({'step_id': str(index), 'log_id': 'log' + str(index), 'result': page_preview(page, 'log' + str(index), 2000)})
    result = await context.reduce_tool('execute_plan_steps', {'outputs': outputs}, 'batch')
    assert all(item['result']['data']['text_complete'] for item in result['outputs'])
    assert estimate_tokens(result) <= 6000


async def test_repeated_reads_are_cached_audited_and_not_business_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    store = Store(tmp_path)
    session = store.create_session()
    task_id = store.create_turn(session, 'Read evidence')
    root = store.start_tool('fixture', {}, 'agent', task_id, session)
    store.finish_tool(root, {'text': 'source body', 'attachments': []}, 1)
    calls = 0
    physical_reads = 0
    from bidding.agent import read_result
    def counted(*args, **kwargs):
        nonlocal physical_reads
        physical_reads += 1
        return read_result(*args, **kwargs)
    monkeypatch.setattr('bidding.agent.read_result', counted)
    def serve(request):
        nonlocal calls
        if planned := planning_response(request):
            return planned
        calls += 1
        if calls == 1:
            answer = {'role': 'assistant', 'tool_calls': [{'id': str(index), 'type': 'function',
                'function': {'name': 'read_tool_result', 'arguments': encoded({'log_id': root, 'field': 'text'})}}
                for index in range(6)]}
        else:
            answer = {'role': 'assistant', 'content': 'Evidence read'}
        return httpx.Response(200, json={'choices': [{'message': answer}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    try:
        await Agent(acquisition).run('Read evidence', task_id, session)
    finally:
        await acquisition.close()
    task = store.get(task_id)
    assert task['status'] == 'completed', task
    assert task['steps'] == []
    assert physical_reads == 1
    reads = [log for log in store.logs(task_id) if log['tool'] == 'read_tool_result']
    assert len(reads) == 6 and all(log['status'] == 'completed' for log in reads)
    assert sum(log['result'].get('reused', False) for log in reads) == 5
    assert len(task['evidence_working_set']) == 1
    assert task['memory_digest']['evidence_references'] == [root]
    snapshot = store.latest_snapshot(session)
    assert len(snapshot['frames']) == 1
    assert 'tool_calls' not in snapshot['frames'][0]['message']
    assert '任务交接' in snapshot['frames'][0]['message']['content']


async def test_evidence_working_set_deduplicates_and_survives_reduction(tmp_path):
    store = Store(tmp_path)
    store.save('task', {}, 'task')
    context = ContextManager(store, SummaryModel(), 'task')
    page = {'page_type': 'detail', 'announcement_id': 'page', 'text': 'Short complete body', 'attachments': []}
    context.remember_evidence('navigate_tender_entry', {}, page, 'original')
    context.remember_evidence('navigate_tender_entry', {}, page, 'original')
    evidence = store.get('task')['evidence_working_set']
    assert len(evidence) == 1 and evidence[0]['evidence']['data']['text_complete']
    for index in range(25):
        context.remember_evidence('read_tool_result', {}, {'original_result_id': 'original', 'field': 'text',
            'offset': index, 'value': 'x' * 1000}, str(index))
    evidence = store.get('task')['evidence_working_set']
    assert len(evidence) <= 16 and estimate_tokens(evidence) <= 8000


async def test_unobserved_document_entry_is_not_clickable(tmp_path):
    acquisition = Acquisition(Store(tmp_path))
    try:
        try:
            await acquisition.inspect_document_access('unknown', 'invented')
        except ValueError as exc:
            assert 'observed' in str(exc)
        else:
            raise AssertionError('Unobserved button must not be clicked')
    finally:
        await acquisition.close()


def test_unfinished_turn_restores_digest_not_raw_tool_transcript(tmp_path):
    store = Store(tmp_path)
    session = store.create_session()
    task_id = store.create_turn(session, 'Download three projects')
    for index in range(10):
        store.add_message(session, task_id, {'role': 'tool', 'tool_call_id': str(index), 'content': encoded({'text': 'large raw output' * 1000})})
    state = {'message': 'Download three projects', 'status': 'partial_completed', 'answer': 'Login is required',
        'spec': {'expected_count': 3}, 'steps': [], 'document_access_barriers': [{'announcement_id': 'page'}]}
    store.save('task', state, task_id)
    context = ContextManager(store, SummaryModel(), task_id, session)
    context.finish(state)
    store.save('task', state, task_id)
    store.release_session(session, task_id)
    next_id = store.create_turn(session, 'Continue with other sources')
    restored = ContextManager(Store(tmp_path), SummaryModel(), next_id, session)
    restored.restore('Continue with other sources')
    assert len(restored.frames) == 2
    assert '任务交接' in restored.frames[0]['message']['content']
    assert 'Login is required' in restored.frames[0]['message']['content']
    assert restored.frames[-1]['message']['content'] == 'Continue with other sources'
    assert len(store.conversation_messages(session)) == 12
