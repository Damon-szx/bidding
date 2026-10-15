import json

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.progress import ProgressWatch
from bidding.storage import Store
from model_fixtures import planning_response


def test_plan_edits_failed_tools_and_rephrased_judgments_are_not_new_progress():
    watch = ProgressWatch()
    watch.observe('update_task_plan', {'steps': []}, {'steps': []})
    watch.observe('fetch_tender_detail', {'url': 'x'}, {'error': 'timeout'})
    assert watch.checkpoint()['stalled_rounds'] == 1
    watch.observe('record_project_assessment', {'announcement_id': 'page',
        'judgments': [{'criterion_id': 'c', 'verdict': 'match', 'reason': 'a'}]}, {'eligible': True})
    assert watch.checkpoint()['stalled_rounds'] == 0
    watch.observe('record_project_assessment', {'announcement_id': 'page',
        'judgments': [{'criterion_id': 'c', 'verdict': 'match', 'reason': 'rephrased'}]}, {'eligible': True})
    assert watch.checkpoint()['stalled_rounds'] == 1


def test_distinct_details_are_evidence_progress_not_a_completed_business_phase():
    watch = ProgressWatch()
    for i in range(10):
        watch.observe('navigate_tender_entry', {}, {'source_url': f'https://example.org/{i}'})
    result = watch.checkpoint()
    assert result['new_observations'] == 10 and result['stalled_rounds'] == 0
    assert result['new_business_observations'] == 0
    watch.observe('record_project_assessment', {'announcement_id': 'page'}, {'status': 'needs_navigation'})
    assert watch.checkpoint()['new_business_observations'] == 0
    watch.observe('inspect_document_access', {'announcement_id': 'page', 'entry_id': 'primary'},
        {'document_access_status': 'requires_user_action'})
    assert watch.checkpoint()['new_business_observations'] == 1


@pytest.mark.parametrize('failure_kind', ['budget', 'error'])
async def test_stopping_retains_verified_research_results_without_model_call(tmp_path, monkeypatch, failure_kind):
    from bidding.model import BudgetExceeded
    async def execute(self, message, task_id, session_id, state):
        state['spec'] = {'action': 'research', 'result_kind': 'projects', 'expected_count': 5}
        state['qualified_projects'] = [{'title': '已核验项目', 'source_url': 'https://example.org/p',
            'eligibility': {'summary': '符合已检查条件'}}]
        raise BudgetExceeded('budget') if failure_kind == 'budget' else ValueError('technical failure')
    monkeypatch.setattr(Agent, 'execute', execute)
    store = Store(tmp_path)
    acquisition = Acquisition(store)
    try:
        await Agent(acquisition).run('查找五个项目', 'task')
        task = store.get('task')
        assert task['status'] == 'partial_completed'
        assert '已核验项目' in task['answer'] and '尚缺 4 个' in task['answer']
        assert '整体完成检查未结束' in task['answer']
        assert store.model_logs(task_id='task') == []
    finally:
        await acquisition.close()


async def test_download_stop_preserves_execution_limit_not_single_entry_barrier(tmp_path, monkeypatch):
    from bidding.model import BudgetExceeded
    async def execute(self, message, task_id, session_id, state):
        state['spec'] = {'action': 'download'}
        state['document_access_barriers'] = [{'entry_id': 'primary', 'message': '不在文件获取时间内!'}]
        raise BudgetExceeded('Agent tool-call budget reached; partial results retained')
    monkeypatch.setattr(Agent, 'execute', execute)
    store = Store(tmp_path)
    acquisition = Acquisition(store)
    try:
        await Agent(acquisition).run('下载公告', 'task')
        task = store.get('task')
        assert task['stop_reason'].startswith('Agent tool-call budget reached')
        assert 'Agent tool-call budget reached' in task['answer']
        assert '不代表其他附件受限' in task['answer']
    finally:
        await acquisition.close()


@pytest.mark.parametrize('review_mode', ['disabled', 'review', 'failure'])
async def test_coordinator_stops_plan_only_loop_after_bounded_recovery(tmp_path, monkeypatch, review_mode):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('AGENT_NO_PROGRESS_ROUNDS', '1')
    monkeypatch.setenv('AGENT_MAX_RECOVERY_ROUNDS', '1')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false' if review_mode == 'disabled' else 'true')
    reviews = []
    async def review(self, proposed_answer, phase):
        reviews.append(phase)
        if review_mode == 'failure':
            raise ValueError('review fixture failed')
        return {'verdict': 'revise', 'summary': 'Repair dependency', 'issues': [], 'next_actions': []}
    monkeypatch.setattr('bidding.agent.Specialists.review', review)
    store = Store(tmp_path)
    calls = 0
    def serve(request):
        nonlocal calls
        if value := planning_response(request, action='research'):
            return value
        calls += 1
        if calls == 2:
            body = json.loads(request.content)
            assert 'get_task_plan' not in {item['function']['name'] for item in body.get('tools', [])}
        answer = {'role': 'assistant', 'tool_calls': [{'id': str(calls), 'type': 'function',
            'function': {'name': 'get_task_plan', 'arguments': json.dumps({})}}]}
        return httpx.Response(200, json={'choices': [{'message': answer}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client())
    try:
        await Agent(acquisition).run('查找三个项目', 'task')
        task = store.get('task')
        assert calls == 2
        plan_logs = [log for log in store.logs('task') if log['tool'] == 'get_task_plan']
        assert plan_logs[-1]['result']['reused'] is True
        assert task['status'] == 'partial_completed'
        assert 'Execution stalled' in task['error']
        statuses = [event['payload']['status'] for event in store.context_logs('task') if event['kind'] == 'execution_recovery']
        assert statuses == (['failed', 'started', 'stopped'] if review_mode == 'failure' else ['started', 'stopped'])
        assert reviews == ([] if review_mode == 'disabled' else ['execution_recovery'])
    finally:
        await acquisition.close()


async def test_invalid_read_reuses_error_but_corrected_arguments_execute(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    root = store.start_tool('fixture', {}, 'agent', 'task')
    store.finish_tool(root, {'text': 'Original evidence'}, 1)
    calls = 0
    def serve(request):
        nonlocal calls
        if value := planning_response(request, action='research'):
            return value
        calls += 1
        if calls < 4:
            arguments = {'log_id': root, 'field': 'entries' if calls < 3 else 'text'}
            answer = {'role': 'assistant', 'tool_calls': [{'id': str(calls), 'type': 'function',
                'function': {'name': 'read_tool_result', 'arguments': json.dumps(arguments)}}]}
        else:
            answer = {'role': 'assistant', 'content': 'Evidence available'}
        return httpx.Response(200, json={'choices': [{'message': answer}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client())
    try:
        await Agent(acquisition).run('Inspect original text', 'task')
        logs = [log for log in store.logs('task') if log['tool'] == 'read_tool_result']
        assert len(logs) == 3
        assert logs[0]['result']['error'] == 'ValueError'
        assert logs[1]['result']['reused'] is True
        assert logs[1]['result']['must_change_arguments'] is True
        assert logs[2]['result']['value'] == 'Original evidence'
        assert store.get('task')['execution_feedback']
    finally:
        await acquisition.close()


@pytest.mark.parametrize('tool_name', ['read_tool_result', 'plan_acquisition'])
async def test_reflection_advice_does_not_execute_or_edit_plan(tmp_path, monkeypatch, tool_name):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('AGENT_NO_PROGRESS_ROUNDS', '1')
    monkeypatch.setenv('AGENT_MAX_RECOVERY_ROUNDS', '1')
    monkeypatch.setenv('REFLECTION_ENABLED', 'true')
    store = Store(tmp_path)
    root = store.start_tool('fixture', {}, 'agent', 'task')
    store.finish_tool(root, {'text': 'Original evidence'}, 1)
    async def review(self, proposed_answer, phase='final_result'):
        if phase != 'execution_recovery':
            return {'verdict': 'pass', 'summary': 'Checked', 'issues': [], 'next_actions': []}
        arguments = {'log_id': root, 'field': 'text'} if tool_name == 'read_tool_result' else {'announcement_id': 'page', 'scope': 'count'}
        return {'verdict': 'revise', 'summary': 'Read missing source', 'issues': [], 'next_actions': [],
            'repair_steps': [{'id': 'repair', 'description': 'Bounded repair', 'tool': tool_name,
                'arguments': arguments, 'depends_on': [], 'critical': False}]}
    monkeypatch.setattr('bidding.agent.Specialists.review', review)
    calls = 0
    def serve(request):
        nonlocal calls
        if value := planning_response(request, action='research'):
            return value
        calls += 1
        if calls > 1 and tool_name == 'read_tool_result':
            answer = {'role': 'assistant', 'content': 'Evidence available'}
        else:
            answer = {'role': 'assistant', 'tool_calls': [{'id': str(calls), 'type': 'function',
                'function': {'name': 'get_task_plan', 'arguments': '{}'}}]}
        return httpx.Response(200, json={'choices': [{'message': answer}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client())
    try:
        await Agent(acquisition).run('Inspect sources', 'task')
        task = store.get('task')
        assert not [event for event in store.context_logs('task') if event['kind'] == 'execution_repair']
        advice = [event for event in store.context_logs('task') if event['kind'] == 'execution_advice']
        assert len(advice) == 1 and advice[0]['payload']['executed'] is False
        assert not any(step['id'] == 'repair' for step in task['steps'])
        assert not any(log['tool'] == tool_name for log in store.logs('task'))
        if tool_name == 'read_tool_result':
            assert task['status'] == 'completed', task
        else:
            assert task['status'] == 'partial_completed'
    finally:
        await acquisition.close()
