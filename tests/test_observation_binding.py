import json

import httpx
import pytest

from bidding.agent import Agent, TOOLS
from bidding.acquisition import Acquisition
from bidding.planning import TaskPlan
from bidding.results import read_result, canonical_read
from bidding.specialists import Specialists
from bidding.storage import Store
from test_results import logged
from test_planning import step, spec
from test_specialists import ScriptModel
from model_fixtures import planning_response


def test_source_and_slice_identity_are_stable_and_aliases_are_canonical(tmp_path):
    store = Store(tmp_path)
    root = logged(store, 'render_tender_page', {'announcement_id': 'page', 'source_url': 'https://fixture.example', 'entries': list(range(20))})
    first = read_result(store, root, 'task', field='entries', limit=5)
    wrapper = logged(store, 'read_tool_result', first, {'log_id': root, 'field': 'entries'})
    again = read_result(store, wrapper, 'task', field='value', limit=5)
    assert first['slice_id'] == again['slice_id']
    assert first['source_ref'] == again['source_ref']
    assert first['source_ref']['result_id'] == root
    assert first['source_ref']['announcement_id'] == 'page'
    assert first['end_offset'] == 5
    assert read_result(store, root, 'task', field='entries', offset=5, limit=5)['slice_id'] != first['slice_id']
    assert canonical_read(store, {'log_id': wrapper, 'field': 'value'}, 'task')['field'] == 'entries'
    with pytest.raises(ValueError, match='not accessible'):
        canonical_read(store, {'log_id': root}, 'other')


def test_observed_pages_complete_only_matching_field_and_full_coverage(tmp_path):
    store = Store(tmp_path)
    state = {'status': 'running'}
    plan = TaskPlan(store, 'task', None, state, [item['function']['name'] for item in TOOLS])
    root = logged(store, 'render_tender_page', {'entries': list(range(20)), 'text': 'full text', 'announcement_id': 'page'})
    plan.install(spec([step('render', 'render_tender_page', args={'url': 'https://fixture.example'}),
        step('read', 'read_tool_result', ['render'], {'log_id': {'$ref': 'render.log_id'}, 'field': 'text'}),
        step('delegate', 'delegate_agent', ['read'], {'agent': 'page_files', 'objective': 'placeholder'})]))
    state['steps'][0].update(status='completed', log_id=root)
    assert plan.resolve({'$ref': 'render.log_id'}) == root
    for offset in (0, 10):
        value = read_result(store, root, 'task', field='entries', offset=offset, limit=10)
        plan.remember_read(logged(store, 'read_tool_result', value, {'log_id': root, 'field': 'entries', 'offset': offset}))
    assert state['steps'][1]['status'] == 'pending'  # Entries never satisfy a text read.
    plan.update([step('read', 'read_tool_result', ['render'], {'log_id': root, 'field': 'entries'})])
    read = next(item for item in state['steps'] if item['id'] == 'read')
    assert read['status'] == 'completed'
    assert plan.resolve({'$ref': 'read.value'}) == list(range(20))
    node = plan.bind_delegation({'agent': 'page_files', 'objective': 'Read observed projects 1,2,3', 'evidence_ids': [root]})
    assert node['id'] == 'delegate' and node['depends_on'] == ['read']
    assert node['arguments']['objective'] == 'Read observed projects 1,2,3'
    assert state['spec']['goal'] == '明确用户目标'
    assert store.get_tool_log(read['log_id'])['source'] == 'checkpoint'


def test_partial_and_other_source_reads_do_not_unlock_dependencies(tmp_path):
    store = Store(tmp_path)
    root = logged(store, 'render_tender_page', {'entries': list(range(20))})
    other = logged(store, 'render_tender_page', {'entries': list(range(20))})
    state = {'status': 'running'}
    plan = TaskPlan(store, 'task', None, state, ['read_tool_result', 'delegate_agent'])
    plan.install(spec([step('read', 'read_tool_result', args={'log_id': root, 'field': 'entries'}),
        step('delegate', 'delegate_agent', ['read'], {'agent': 'page_files', 'objective': 'old'})]))
    for source, offset in ((root, 0), (other, 10)):
        result = read_result(store, source, 'task', field='entries', offset=offset, limit=10)
        plan.remember_read(logged(store, 'read_tool_result', result))
    assert state['steps'][0]['status'] == 'pending'
    with pytest.raises(ValueError, match='prerequisites'):
        plan.bind_delegation({'agent': 'page_files', 'objective': 'new'})


async def test_reading_more_than_four_fields_reuses_aliases_without_business_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    root = logged(store, 'render_tender_page', {'entries': list(range(6)), 'page_type': 'listing', 'pagination': []})
    wrapper = logged(store, 'read_tool_result', read_result(store, root, 'task', field='entries', limit=1))
    calls = []
    for index in range(6):
        calls.append({'id': str(index), 'type': 'function', 'function': {'name': 'read_tool_result',
            'arguments': json.dumps({'log_id': root, 'field': 'entries', 'offset': index, 'limit': 1})}})
    calls.append({'id': 'reuse', 'type': 'function', 'function': {'name': 'read_tool_result',
        'arguments': json.dumps({'log_id': wrapper, 'field': 'value', 'offset': 0, 'limit': 1})}})
    calls.append({'id': 'field', 'type': 'function', 'function': {'name': 'read_tool_result',
        'arguments': json.dumps({'log_id': root, 'field': 'page_type'})}})
    responses = iter([{'role': 'assistant', 'tool_calls': calls}, {'role': 'assistant', 'content': '读取完成'}])
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(
        lambda request: planning_response(request) or httpx.Response(200, json={'choices': [{'message': next(responses)}]}))))
    async with client_class() as network:
        acquisition = Acquisition(store, network)
        await Agent(acquisition).run('读取列表信息', 'task')
        reads = [log for log in store.logs('task') if log.get('model_call_id') is not None]
        assert len(reads) == 8 and all(log['status'] == 'completed' for log in reads)
        assert reads[6]['result']['reused']
        assert not store.get('task')['steps']
        await acquisition.close()


async def test_singleton_reflection_step_is_normalized_but_invalid_steps_stay_rejected(tmp_path):
    store = Store(tmp_path)
    repair = step('read', 'read_tool_result', args={'log_id': 'root', 'field': 'entries'})
    model = ScriptModel([{'content': json.dumps({'verdict': 'revise', 'summary': '读取证据', 'repair_steps': repair})}])
    result = await Specialists(model, store, 'task', None, {}, TOOLS, None).review('继续', phase='execution_recovery')
    assert result['repair_steps'] == [repair]
    assert len(model.calls) == 1
    invalid = {'verdict': 'revise', 'summary': '修复', 'repair_steps': [dict(repair, unexpected=True)]}
    model = ScriptModel([{'content': json.dumps(invalid)}, {'content': json.dumps(invalid)}])
    result = await Specialists(model, store, 'task', None, {}, TOOLS, None).review('继续', phase='execution_recovery')
    assert result['verdict'] == 'needs_input'
    assert len(model.calls) == 2
