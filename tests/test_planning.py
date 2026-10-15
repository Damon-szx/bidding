import asyncio
import json

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.model import BudgetExceeded
from bidding.planning import TaskPlan, TaskSpec, normalize_steps, infer_plan
from bidding.storage import Store
from model_fixtures import planning_response


def step(ident, tool='fetch_tender_detail', deps=None, args=None, critical=False):
    return {'id': ident, 'description': '步骤' + ident, 'tool': tool, 'arguments': args or {}, 'depends_on': deps or [], 'critical': critical}


def spec(steps=None):
    return TaskSpec(goal='明确用户目标', action='research', complete_when=['原文核验完成'], steps=steps or []).model_dump()


def planner(tmp_path, steps):
    store = Store(tmp_path)
    state = {'status': 'running', 'session_id': 'session'}
    plan = TaskPlan(store, 'task', 'session', state, ['fetch_tender_detail', 'navigate_tender_entry'])
    plan.install(spec(steps))
    return store, state, plan


def test_dependency_cycles_and_undeclared_output_references_are_rejected():
    with pytest.raises(ValueError, match='cycle'):
        spec([step('a', deps=['b']), step('b', deps=['a'])])
    with pytest.raises(ValueError, match='declared dependency'):
        spec([step('a'), step('b', args={'url': {'$ref': 'a.source_url'}})])


async def test_repaired_assessment_completes_original_node_and_unblocks_download(tmp_path):
    store = Store(tmp_path)
    state = {'status': 'running'}
    plan = TaskPlan(store, 'task', None, state, ['fetch_tender_detail', 'record_project_assessment', 'plan_acquisition'])
    plan.install(spec([step('read'), step('s3', 'record_project_assessment', ['read'],
        {'announcement_id': {'$ref': 'read.announcement_id'}, 'judgments': [], 'summary': 'placeholder'}),
        step('download', 'plan_acquisition', ['s3'], {'announcement_id': 'page'})]))
    async def invoke(name, args):
        result = {'announcement_id': 'page'} if name == 'fetch_tender_detail' else {'error': 'ValueError',
            'message': 'A match needs original-source evidence'} if not args.get('judgments') and name == 'record_project_assessment' else {'ok': True}
        ident = store.start_tool(name, args, 'agent', 'task')
        store.finish_tool(ident, result, 1)
        return result, ident
    await plan.execute(['read', 's3'], invoke)
    assert state['steps'][2]['status'] == 'blocked'
    assert plan.bind_assessment({'announcement_id': 'another-page', 'judgments': [], 'summary': 'x'}) is None
    node = plan.bind_assessment({'announcement_id': 'page', 'judgments': [{'criterion_id': 'c', 'verdict': 'match'}], 'summary': 'evidence'})
    assert node['id'] == 's3'
    result = await plan.execute(['s3', 'download'], invoke)
    assert result['execution_status'] == 'completed'
    assert result['not_ready'] == []
    assert state['steps'][2]['status'] == 'completed'


async def test_scheduler_reports_dependency_status_without_claiming_execution(tmp_path):
    _, _, plan = planner(tmp_path, [step('a'), step('b', deps=['a'])])
    async def unused(*args):
        raise AssertionError('No ready work')
    result = await plan.execute(['b'], unused)
    assert result['execution_status'] == 'not_ready'
    assert result['outputs'] == []
    assert result['blockers'][0]['dependencies'] == [{'step_id': 'a', 'status': 'pending', 'error': None}]


async def test_detail_assessment_closes_only_corresponding_deferred_listing_node(tmp_path):
    store, state, plan = planner(tmp_path, [])
    for entry in ('first', 'second'):
        ident = store.start_tool('record_project_assessment', {}, 'agent', 'task', 'session')
        store.finish_tool(ident, {'status': 'needs_navigation'}, 0)
        state['steps'].append({**step(entry, 'record_project_assessment', args={
            'announcement_id': 'listing', 'entry_id': entry}), 'status': 'needs_input', 'log_id': ident})
    ident = store.start_tool('record_project_assessment', {}, 'agent', 'task', 'session')
    result = {'announcement_id': 'detail', 'assessment_id': 'assessment', 'eligible': True}
    store.finish_tool(ident, result, 0)
    plan.reconcile_assessment({'navigation': {'parent_announcement_id': 'listing', 'entry_id': 'first'}}, result, ident)
    assert state['steps'][0]['status'] == 'completed'
    assert state['steps'][1]['status'] == 'needs_input'
    assert plan.resolve({'$ref': 'first.announcement_id'}) == 'detail'
    async def unused(*args):
        pytest.fail('Already completed through existing evidence')
    assert (await plan.execute(['first'], unused))['execution_status'] == 'completed'


def test_plan_update_rejects_empty_executable_arguments(tmp_path):
    store = Store(tmp_path)
    from bidding.agent import TOOLS
    plan = TaskPlan(store, 'task', None, {'steps': []}, ['record_project_assessment'], TOOLS)
    with pytest.raises(ValueError, match='Missing required planned arguments'):
        plan.update([step('s3', 'record_project_assessment')])


def test_recovery_plan_rejects_invented_inspection_arguments(tmp_path):
    from bidding.agent import TOOLS
    plan = TaskPlan(Store(tmp_path), 'task', None, {'steps': []}, ['inspect_document_access'], TOOLS)
    with pytest.raises(ValueError, match='Unexpected planned arguments'):
        plan.validate_arguments('inspect_document_access', {'announcement_id': 'page',
            'entry_id': 'observed-button', 'entry_titles': ['invented']})


def test_orchestration_and_mixed_ids_rejected_before_business_execution(tmp_path):
    from bidding.agent import TOOLS
    store = Store(tmp_path)
    plan = TaskPlan(store, 'task', None, {'steps': []}, [item['function']['name'] for item in TOOLS], TOOLS)
    with pytest.raises(ValueError, match='Orchestration'):
        plan.update([step('resume', 'resume_task', args={'task_id': 'previous'})])
    page_id = store.save('announcement', {'entries': [{'entry_id': 'project-entry'}],
        'document_access': [{'entry_id': 'access-entry'}]})
    with pytest.raises(ValueError, match='observed page'):
        plan.validate_observed_arguments('discover_tender_files', {'announcement_id': 'project-entry'})
    with pytest.raises(ValueError, match='entry_id'):
        plan.validate_observed_arguments('inspect_document_access', {'announcement_id': page_id, 'entry_id': page_id})
    plan.validate_observed_arguments('inspect_document_access', {'announcement_id': page_id, 'entry_id': 'access-entry'})


def test_verified_projects_are_not_counted_as_download_ready_without_files(tmp_path):
    _, state, plan = planner(tmp_path, [])
    state['spec']['file_goal'] = 'tender_document'
    state['qualified_projects'] = [{'eligibility': {'files': [{'document_type': 'unknown'}]}}]
    progress = plan.working_view()['goal_progress']
    assert progress['verified_count'] == 1
    assert progress['download_ready_count'] == 0
    state['qualified_projects'][0]['eligibility']['files'][0]['document_type'] = 'tender_document'
    assert plan.working_view()['goal_progress']['download_ready_count'] == 1


def test_failed_step_can_be_repaired_by_dependency_change_without_changing_arguments(tmp_path):
    _, state, plan = planner(tmp_path, [step('a'), step('b', deps=['a'])])
    state['steps'][1].update(status='failed', error='dependency problem')
    plan.update([step('b', deps=[])])
    node = next(item for item in state['steps'] if item['id'] == 'b')
    assert node['depends_on'] == []
    assert node['status'] == 'pending'


def test_reference_normalization_repairs_known_steps_without_inventing_ids():
    steps = normalize_steps([step('a'), step('b', args={'entry_id': 'a.entries[0].entry_id',
        'url': 'https://example.org/a', 'literal': 'other.source_url'})])
    assert steps[1]['arguments']['entry_id'] == {'$ref': 'a.entries.0.entry_id'}
    assert steps[1]['depends_on'] == ['a']
    assert steps[1]['arguments']['literal'] == 'other.source_url'
    assert steps[1]['arguments']['url'] == 'https://example.org/a'


def test_model_reference_spellings_and_output_contracts():
    for reference in ('$ref:a.announcement_id', '$.a.result.announcement_id'):
        result = normalize_steps([step('a'), step('b', args={'announcement_id': reference})])
        assert result[1]['arguments']['announcement_id'] == {'$ref': 'a.announcement_id'}
    with pytest.raises(ValueError, match='Unknown output reference.*available fields'):
        normalize_steps([step('scan', 'scan_tender_listing'), step('child', 'delegate_agent',
            args={'evidence_ids': ['$ref:scan.entries']})])
    with pytest.raises(ValueError, match='Unknown output reference'):
        normalize_steps([step('child', 'delegate_agent'), step('download', 'plan_acquisition',
            args={'announcement_id': '$ref:child.final_announcement_ids'})])


def test_remove_only_update_unblocks_ordering_but_never_discards_data_dependency(tmp_path):
    _, state, plan = planner(tmp_path, [step('failed'), step('next', deps=['failed'])])
    state['steps'][0]['status'] = 'failed'
    plan.update(remove_step_ids=['failed'])
    assert len(state['steps']) == 1
    assert state['steps'][0]['depends_on'] == []
    assert state['steps'][0]['status'] == 'pending'
    _, state, plan = planner(tmp_path, [step('failed'), step('next', deps=['failed'],
        args={'url': {'$ref': 'failed.source_url'}})])
    state['steps'][0]['status'] = 'failed'
    before = json.dumps(state['steps'], sort_keys=True)
    with pytest.raises(ValueError):
        plan.update(remove_step_ids=['failed'])
    assert json.dumps(state['steps'], sort_keys=True) == before


def test_existing_step_patch_reuses_description_but_new_step_requires_it(tmp_path):
    _, state, plan = planner(tmp_path, [step('a', args={'url': 'https://example.org/old'})])
    state['steps'][0]['status'] = 'failed'
    plan.update([{'id': 'a', 'arguments': {'url': 'https://example.org/new'}}])
    assert state['steps'][0]['description'] == '步骤a'
    assert state['steps'][0]['arguments']['url'] == 'https://example.org/new'
    assert state['steps'][0]['status'] == 'pending'
    with pytest.raises(ValueError):
        plan.update([{'id': 'new', 'tool': 'fetch_tender_detail', 'arguments': {'url': 'https://example.org'}}])


def test_coordinator_projects_assessed_text_without_changing_original_evidence(tmp_path):
    _, state, plan = planner(tmp_path, [])
    raw = {'full_result_id': 'original', 'data': {'announcement_id': 'page', 'page_type': 'detail',
        'title': 'project', 'source_url': 'https://example.org/detail', 'text': 'source text' * 100,
        'text_complete': True}}
    state['evidence_working_set'] = [{'tool': 'navigate_tender_entry', 'evidence': raw}]
    state['qualified_projects'] = [{'announcement_id': 'page', 'eligibility': {'files': []}}]
    projected = plan.working_view()['evidence_working_set'][0]['evidence']
    assert 'text' not in projected['data']
    assert raw['data']['text'] == 'source text' * 100


def test_json_string_reference_is_normalized_and_unknown_reference_is_not_invented():
    steps = normalize_steps([step('a'), step('b', args={
        'announcement_id': '{"$ref":"a.announcement_id"}', 'literal': '{"$ref":"missing.id"}'})])
    assert steps[1]['arguments']['announcement_id'] == {'$ref': 'a.announcement_id'}
    assert steps[1]['depends_on'] == ['a']
    assert steps[1]['arguments']['literal'] == '{"$ref":"missing.id"}'


def test_file_formats_are_explicit_and_pdf_goal_cannot_be_widened():
    assert TaskSpec(goal='Download change notice', action='download', file_goal='public_files',
        file_formats=['docx', 'zip'], complete_when=['Saved requested change notice']).file_formats == ['docx', 'zip']
    with pytest.raises(ValueError, match='PDF-only'):
        TaskSpec(goal='PDF only', action='download', file_goal='public_pdf', file_formats=['pdf', 'zip'], complete_when=['Saved PDF'])
    with pytest.raises(ValueError, match='cycle'):
        normalize_steps([step('a', args={'url': 'b.source_url'}), step('b', args={'url': 'a.source_url'})])


async def test_normalized_navigation_resolves_observed_list_entry(tmp_path):
    steps = normalize_steps([step('a'), step('b', tool='navigate_tender_entry',
        args={'announcement_id': 'a.announcement_id', 'entry_id': 'a.entries[0].entry_id'})])
    store, state, plan = planner(tmp_path, steps)
    async def invoke(name, args):
        if name == 'navigate_tender_entry':
            assert args == {'announcement_id': 'ann', 'entry_id': 'entry'}
            output = {'ok': True}
        else:
            output = {'announcement_id': 'ann', 'entries': [{'entry_id': 'entry'}]}
        log_id = store.start_tool(name, args, 'agent', 'task')
        store.finish_tool(log_id, output, 1)
        return output, log_id
    await plan.execute(['a', 'b'], invoke)
    assert all(item['status'] == 'completed' for item in state['steps'])
    working = plan.working_view()
    assert not working['active_steps']
    assert 'steps' not in working['spec']
    assert working['checkpoints'][1]['log_id']


async def test_authorization_repair_keeps_draft_without_replaying_full_history(tmp_path):
    class Model:
        budget, charged, hard_limit = 150000, 0, 32000
        task_id, session_id = 'task', None
        def __init__(self):
            self.store = Store(tmp_path)
            self.calls = []
        async def call(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            self.calls.append(payload)
            draft = {**spec(), 'action': 'download', 'authorization_quote': '' if len(self.calls) == 1 else '下载三个文件'}
            return {'content': json.dumps(draft)}
    model = Model()
    result = await infer_plan(model, [{'role': 'user', 'content': 'old history' * 500}], '下载三个文件', [])
    assert result['authorization_quote'] == '下载三个文件'
    assert len(model.calls) == 2
    assert 'draft_plan' in model.calls[1]
    assert 'conversation' not in model.calls[1]


async def test_independent_steps_parallel_then_dependent_step_resolves_output(tmp_path):
    store, state, plan = planner(tmp_path, [step('a'), step('b'), step('c', deps=['a', 'b'], args={'url': {'$ref': 'a.source_url'}})])
    entered = set()
    both = asyncio.Event()
    called = []
    async def invoke(name, args):
        ident = 'a' if len(entered) == 0 else 'b' if len(entered) == 1 else 'c'
        entered.add(ident)
        if len(entered) == 2:
            both.set()
        if ident != 'c':
            await asyncio.wait_for(both.wait(), 1)
        else:
            assert args == {'url': 'https://example.org/a'}
        result = {'source_url': 'https://example.org/' + ident}
        log_id = store.start_tool(name, args, 'agent', 'task', 'session')
        store.finish_tool(log_id, result, 1)
        called.append(ident)
        return result, log_id
    await plan.execute(['a', 'b', 'c'], invoke)
    assert called[-1] == 'c'
    assert all(item['status'] == 'completed' for item in state['steps'])
    waves = [event['payload'] for event in store.context_logs(task_id='task') if event['kind'] == 'execution_wave']
    assert len(waves) == 1
    assert waves[0]['status'] == 'completed'
    assert waves[0]['step_ids'] == ['a', 'b']
    assert waves[0]['max_concurrency'] == 3
    assert {item['log_id'] for item in waves[0]['members']} == {
        item['log_id'] for item in state['steps'] if item['id'] in ('a', 'b')}


def test_project_only_working_set_prioritizes_saved_assessment_without_file_goal(tmp_path):
    store, state, plan = planner(tmp_path, [])
    page_id = store.save('announcement', {'page_type': 'detail', 'title': 'Observed project', 'source_url': 'https://example.org/p'})
    state['observed_pages'] = [page_id, page_id]
    view = plan.working_view()
    assert len(view['awaiting_assessment']) == 1
    assert view['awaiting_assessment'][0]['announcement_id'] == page_id
    assert 'download_ready_count' not in view['goal_progress']
    with pytest.raises(ValueError, match='Project-only'):
        plan.validate_observed_arguments('inspect_document_access', {'announcement_id': page_id})
    state['qualified_projects'] = [{'announcement_id': page_id}]
    assert plan.working_view()['awaiting_assessment'] == []


def test_reflection_repair_cannot_reread_complete_coverage(tmp_path):
    from bidding.results import read_result
    store, state, plan = planner(tmp_path, [])
    root = store.start_tool('render_tender_page', {}, 'agent', 'task', 'session')
    store.finish_tool(root, {'text': '原文完整'}, 1)
    args = {'log_id': root, 'field': 'text'}
    observed = read_result(store, root, 'task', 'session', field='text')
    log = store.start_tool('read_tool_result', args, 'agent', 'task', 'session')
    store.finish_tool(log, observed, 1)
    plan.remember_read(log)
    with pytest.raises(ValueError, match='already read'):
        plan.validate_observed_arguments('read_tool_result', args)


async def test_failure_blocks_dependents_not_independent_projects(tmp_path):
    store, state, plan = planner(tmp_path, [step('a'), step('b', deps=['a']), step('independent')])
    count = 0
    async def invoke(name, args):
        nonlocal count
        count += 1
        return ({'error': 'PermanentFailure', 'message': 'missing'} if count == 1 else {'ok': True}), 'log'
    await plan.execute(['a', 'b', 'independent'], invoke)
    assert [item['status'] for item in state['steps']] == ['failed', 'blocked', 'completed']
    plan.update([step('replacement'), step('b', deps=['replacement']), step('independent')])
    assert state['steps'][1]['status'] == 'pending'
    assert state['steps'][2]['status'] == 'completed'


async def test_navigation_steps_are_serialized(tmp_path):
    store, state, plan = planner(tmp_path, [step('a', tool='navigate_tender_entry'), step('b', tool='navigate_tender_entry')])
    active, maximum = 0, 0
    async def invoke(name, args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {'ok': True}, 'log'
    await plan.execute(['a', 'b'], invoke)
    assert maximum == 1


async def test_budget_exit_cancels_other_steps_and_preserves_checkpoints(tmp_path):
    store, state, plan = planner(tmp_path, [step('a'), step('b')])
    count = 0
    async def invoke(name, args):
        nonlocal count
        count += 1
        if count == 1:
            await asyncio.sleep(0)
            raise BudgetExceeded('stop')
        await asyncio.sleep(10)
    with pytest.raises(BudgetExceeded):
        await plan.execute(['a', 'b'], invoke)
    assert all(item['status'] == 'pending' for item in state['steps'])


def test_resume_reuses_completed_results_and_checks_session(tmp_path):
    store, state, plan = planner(tmp_path, [step('a'), step('b', deps=['a'])])
    old = {'id': 'old', 'status': 'interrupted', 'session_id': 'session', 'spec': spec([step('a'), step('b', deps=['a'])]),
        'steps': [{**step('a'), 'status': 'completed', 'attempts': 1, 'log_id': 'retained'}, {**step('b', deps=['a']), 'status': 'running', 'attempts': 1}]}
    plan.resume(old)
    assert state['steps'][0]['log_id'] == 'retained'
    assert state['steps'][1]['status'] == 'pending'
    with pytest.raises(ValueError, match='conversation'):
        plan.resume({**old, 'session_id': 'other'})


def test_partial_plan_update_keeps_checkpoints_and_all_omitted_steps(tmp_path):
    store, state, plan = planner(tmp_path, [step('a'), step('b', deps=['a']), step('c', deps=['b'])])
    state['steps'][0].update(status='completed', log_id='saved')
    plan.update([step('b', deps=['a'], args={'url': 'https://example.org'})])
    by_id = {item['id']: item for item in state['steps']}
    assert by_id['a']['log_id'] == 'saved'
    assert 'c' in by_id
    plan.update([], remove_step_ids=['c'])
    assert 'c' not in {item['id'] for item in state['steps']}
    with pytest.raises(ValueError, match='non-running'):
        plan.update([], remove_step_ids=['a'])


async def test_ambiguous_intent_asks_before_any_external_call(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        return planning_response(request, questions=['你指今天发布，还是今天开始获取招标文件？'])
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    acquisition = Acquisition(store, client_class())
    await Agent(acquisition).run('找今天开始招标的项目', 'task')
    assert store.get('task')['status'] == 'awaiting_clarification'
    assert len(requests) == 1
    assert not store.logs(task_id='task')
    await acquisition.close()


async def test_agent_executes_persisted_plan_with_real_dependency_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    steps = [step('a', args={'url': 'https://example.org/a'}), step('b', args={'url': 'https://example.org/b'})]
    responses = [{'role': 'assistant', 'tool_calls': [{'id': 'execute', 'type': 'function', 'function': {'name': 'execute_plan_steps', 'arguments': '{"step_ids":["a","b"]}'}}]}, {'role': 'assistant', 'content': '已完成页面读取'}]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda request: planning_response(request, steps=steps) or httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]}))))
    store = Store(tmp_path)
    acquisition = Acquisition(store, client_class())
    active, maximum = 0, 0
    async def fetch(url):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        result = {'source_url': url, 'title': url, 'page_type': 'detail', 'text': '公告原文', 'attachments': []}
        result['announcement_id'] = store.save('announcement', result)
        return result
    acquisition.fetch_tender_detail = fetch
    await Agent(acquisition).run('读取这两个页面', 'task')
    assert maximum == 2
    assert store.get('task')['status'] == 'completed'
    assert all(item['status'] == 'completed' for item in store.get('task')['steps'])
    assert len([item for item in store.logs(task_id='task') if item['tool'] == 'fetch_tender_detail']) == 2
    await acquisition.close()


async def test_answer_format_conditions_do_not_become_project_filters(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda request:
        planning_response(request, action='answer', criteria=[{'id': 'style', 'description': '仅一句话'}]) or httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '项目匹配由模型基于公告证据判断。'}}]}))))
    store = Store(tmp_path)
    acquisition = Acquisition(store, client_class())
    await Agent(acquisition).run('仅一句话说明谁判断，不联网', 'task')
    result = store.get('task')
    assert result['status'] == 'completed'
    assert result['answer'] == '项目匹配由模型基于公告证据判断。'
    assert not store.logs(task_id='task')
    await acquisition.close()


async def test_user_cancellation_stops_steps_and_releases_session(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    planned = [step('read', args={'url': 'https://example.org/tender'})]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda request:
        planning_response(request, steps=planned) or httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [{'id': 'execute', 'type': 'function', 'function': {'name': 'execute_plan_steps', 'arguments': '{"step_ids":["read"]}'}}]}}]}))))
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, '读取项目页面')
    acquisition = Acquisition(store, client_class())
    entered = asyncio.Event()
    async def fetch(url):
        entered.set()
        await asyncio.sleep(60)
    acquisition.fetch_tender_detail = fetch
    agent = Agent(acquisition)
    runner = asyncio.create_task(agent.run('读取项目页面', task_id, session_id))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(ValueError, match='conversation'):
        await agent.cancel(task_id, 'wrong')
    result = await agent.cancel(task_id, session_id)
    assert result['status'] == 'cancelled'
    assert result['steps'][0]['status'] == 'pending'
    assert store.session(session_id)['active_task_id'] is None
    assert runner.done()
    assert any(item['tool'] == 'fetch_tender_detail' and item['status'] == 'failed' for item in store.logs(task_id=task_id))
    await acquisition.close()
