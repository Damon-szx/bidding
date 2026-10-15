import json
import asyncio

import httpx
import pytest

from bidding.agent import Agent, TOOLS
from bidding.acquisition import Acquisition
from bidding.context import ContextManager
from bidding.model import BudgetExceeded, encoded
from bidding.specialists import Specialists
from bidding.storage import Store
from model_fixtures import planning_response


async def test_coordinator_hands_repeated_field_reading_to_page_agent(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    monkeypatch.setenv('COORDINATOR_MAX_EVIDENCE_READS', '2')
    store = Store(tmp_path)
    store.save('announcement', {'source_url': 'https://example.org/list', 'page_type': 'listing', 'text': '来源原文',
        'entries': [{'entry_id': 'e', 'title': '采购项目', 'url': 'https://example.org/detail'}],
        'pagination': [], 'attachments': []}, 'page')
    main_calls, child_calls = 0, 0
    def call(name, args):
        return {'role': 'assistant', 'tool_calls': [{'id': str(main_calls) + '-' + str(child_calls), 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}]}
    def serve(request):
        nonlocal main_calls, child_calls
        if response := planning_response(request, expected_count=3):
            return response
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith('# 页面与文件 Agent'):
            child_calls += 1
            assert 'read_tool_result' in {tool['function']['name'] for tool in body['tools']} if child_calls == 1 else True
            assert 'plan_acquisition' not in {tool['function']['name'] for tool in body.get('tools', [])}
            source = next(log['id'] for log in store.logs(task_id='task') if log['tool'] == 'render_tender_page')
            answer = call('read_tool_result', {'log_id': source, 'field': 'text'}) if child_calls == 1 else {'role': 'assistant', 'content': '已读取证据，尚未完成项目核验。'}
        else:
            main_calls += 1
            source = next((log['id'] for log in store.logs(task_id='task') if log['tool'] == 'render_tender_page'), None)
            if main_calls == 1:
                answer = call('render_tender_page', {'url': 'https://example.org/list'})
            elif main_calls <= 3:
                answer = call('read_tool_result', {'log_id': source, 'field': 'entries' if main_calls == 2 else 'pagination'})
            elif main_calls == 4:
                assert 'read_tool_result' not in {tool['function']['name'] for tool in body['tools']}
                answer = call('delegate_agent', {'agent': 'page_files', 'objective': '补读来源正文并返回核验缺口', 'evidence_ids': [source]})
            else:
                answer = {'role': 'assistant', 'content': '未完成项目核验。'}
        return httpx.Response(200, json={'choices': [{'message': answer}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client())
    agent = Agent(acquisition)
    async def render(url):
        return {**store.get('page'), 'announcement_id': 'page'}
    agent.runtime.handlers['render_tender_page'] = render
    try:
        await agent.run('查找三个采购项目', 'task')
        task = store.get('task')
        assert task['coordinator_read_count'] == 2 and child_calls == 2
        handoff = next(value for value in task['specialist_checkpoints'].values() if value['agent'] == 'page_files')
        assert {'listing_progress', 'selected_files', 'barriers', 'remaining_file_gap'} <= handoff.keys()
    finally:
        await acquisition.close()


class ScriptModel:
    thinking = 'disabled'
    hard_limit = 20000
    last_log_id = None

    def __init__(self, answers):
        self.answers, self.calls = iter(answers), []

    async def call(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.last_log_id = 'model-' + str(len(self.calls))
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


def call(name, args):
    return {'role': 'assistant', 'tool_calls': [{'id': 'call-' + name, 'type': 'function',
        'function': {'name': name, 'arguments': encoded(args)}}]}


async def test_page_agent_receives_saved_source_and_uses_last_round_for_real_assessment(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECIALIST_MAX_ROUNDS', '1')
    store = Store(tmp_path)
    source = {'announcement_id': 'detail', 'page_type': 'detail', 'title': '采购公告',
        'source_url': 'https://example.org/detail', 'published_at': '2026-10-09',
        'text': '采购公告 2026-10-09 下载公告文件',
        'attachments': [{'url': 'https://example.org/notice.pdf', 'name': '下载公告文件'}]}
    store.save('announcement', source, 'detail')
    log = store.start_tool('navigate_tender_entry', {}, 'agent', 'task')
    store.finish_tool(log, source, 1)
    state = {'spec': {'action': 'download', 'expected_count': 1, 'file_goal': 'public_files',
        'criteria': [{'id': 'recent', 'description': '近期发布'}]}, 'observed_pages': ['detail']}
    args = {'announcement_id': 'detail', 'judgments': [{'criterion_id': 'recent', 'verdict': 'match',
        'reason': '日期符合', 'quotes': ['2026-10-09']}], 'summary': '近期公告',
        'files': [{'url': source['attachments'][0]['url'], 'document_type': 'announcement_document', 'quote': '下载公告文件'}]}
    model = ScriptModel([call('record_project_assessment', args)])
    async def invoke(name, arguments, ident):
        from bidding.eligibility import validate_assessment
        assert name == 'record_project_assessment'
        evaluation = validate_assessment(store.get('detail'), state['spec'], arguments['judgments'],
            arguments['summary'], arguments['files'])
        evaluation.update(announcement_id='detail', assessment_id='actual-assessment')
        state['qualified_projects'] = [{'announcement_id': 'detail', 'assessment_id': 'actual-assessment',
            'title': source['title'], 'source_url': source['source_url'], 'eligibility': evaluation}]
        result_id = store.start_tool(name, arguments, 'agent', 'task')
        store.finish_tool(result_id, evaluation, 1)
        return evaluation, result_id
    result = await Specialists(model, store, 'task', None, state, TOOLS, invoke).delegate('page_files', '核验并选择公告')
    assert len(model.calls) == 1
    packet = json.loads(model.calls[0][0][1]['content'])
    assert packet['evidence_ids'] == [log]
    assert packet['observed_evidence'][0]['full_result_id'] == log
    allowed = {tool['function']['name'] for tool in model.calls[0][1]['tools']}
    assert 'record_project_assessment' in allowed and 'navigate_tender_entry' not in allowed
    assert result['status'] == 'completed'
    assert result['selected_files'][0]['assessment_id'] == 'actual-assessment'
    assert result['remaining_file_gap'] == 0 and result['pending_assessments'] == []


async def test_page_handoff_rejects_prose_only_success_and_binds_observed_page_id(tmp_path, monkeypatch):
    monkeypatch.setenv('SPECIALIST_MAX_ROUNDS', '1')
    store = Store(tmp_path)
    source = {'announcement_id': 'detail', 'page_type': 'detail', 'text': '已保存原文', 'attachments': []}
    store.save('announcement', source, 'detail')
    log = store.start_tool('navigate_tender_entry', {}, 'agent', 'task', 'session')
    store.finish_tool(log, source, 1)
    state = {'spec': {'action': 'download', 'expected_count': 5}, 'observed_pages': ['detail']}
    model = ScriptModel([{'content': '已核验5个项目，全部下载成功，需要用户再次授权。'}])
    result = await Specialists(model, store, 'task', 'session', state, TOOLS, None).delegate('page_files', '核验', ['detail'])
    assert result['evidence_ids'] == [log]
    assert result['status'] == 'partial_completed'
    assert result['verified_projects'] == [] and result['selected_files'] == []
    assert result['pending_assessments'][0]['announcement_id'] == 'detail'
    assert result['remaining_file_gap'] == 5
    assert '全部下载成功' not in result['summary'] and '再次授权' not in result['summary']


def test_matched_project_without_selected_document_remains_pending(tmp_path):
    store = Store(tmp_path)
    source = {'announcement_id': 'detail', 'page_type': 'detail', 'text': '公告原文'}
    log = store.start_tool('navigate_tender_entry', {}, 'agent', 'task')
    store.finish_tool(log, source, 1)
    state = {'spec': {'action': 'download', 'file_goal': 'tender_document', 'expected_count': 1},
        'qualified_projects': [{'announcement_id': 'detail', 'assessment_id': 'assessment',
            'eligibility': {'files': [{'url': 'https://example.org/file', 'document_type': 'unknown'}]}}]}
    result = Specialists(None, store, 'task', None, state, TOOLS, None).handoff(
        'page_files', 'run', [log], '已完成')
    assert result['selected_files'] == [] and result['remaining_file_gap'] == 1
    assert result['status'] == 'partial_completed'
    assert result['pending_assessments'][0]['announcement_id'] == 'detail'


async def test_specialist_isolated_context_capability_and_trace(tmp_path):
    store = Store(tmp_path)
    model = ScriptModel([call('search_tenders', {'query': '电力招标'}), {'content': '候选仍需原文核验'}])
    async def invoke(name, args, ident):
        assert name == 'search_tenders'
        log_id = store.start_tool(name, args, 'agent', 'task', model_call_id=ident)
        result = {'candidates': [{'title': '电力项目', 'source_url': 'https://example.org/a'}]}
        store.finish_tool(log_id, result, 1)
        return result, log_id
    specialist = Specialists(model, store, 'task', None, {'spec': {'goal': '电力项目'}}, TOOLS, invoke)
    result = await specialist.delegate('search', '寻找电力项目')
    assert result['evidence_ids']
    assert len(model.calls[0][0]) == 2
    assert {item['function']['name'] for item in model.calls[0][1]['tools']} == {'search_tenders', 'read_tool_result', 'record_search_progress'}
    assert all(item['function']['name'] != 'delegate_agent' for item in model.calls[0][1]['tools'])
    assert any(item['payload'].get('log_id') == result['evidence_ids'][0] for item in store.context_logs())


async def test_specialist_rejects_outside_tool_and_foreign_evidence(tmp_path):
    store = Store(tmp_path)
    log = store.start_tool('search_tenders', {}, 'agent', 'other')
    store.finish_tool(log, {}, 1)
    invoked = []
    async def invoke(*args):
        invoked.append(args)
    model = ScriptModel([call('download_tender_file', {}), {'content': '权限边界已阻止下载'}])
    specialist = Specialists(model, store, 'task', None, {}, TOOLS, invoke)
    with pytest.raises(ValueError, match='outside'):
        await specialist.delegate('search', '搜索', [log])
    await specialist.delegate('search', '搜索')
    assert invoked == []
    assert 'capability boundary' in model.calls[-1][0][-1]['content']


async def test_reflection_failure_not_pass_and_budget_propagates(tmp_path):
    store = Store(tmp_path)
    model = ScriptModel([{'content': 'not JSON'}, BudgetExceeded('budget')])
    specialist = Specialists(model, store, 'task', None, {}, TOOLS, None)
    result = await specialist.review('全部完成')
    assert result['verdict'] == 'needs_input'
    event = store.context_logs()[-1]['payload']
    assert event['status'] == 'failed' and event['exception']['type'] == 'JSONDecodeError'
    with pytest.raises(BudgetExceeded):
        await specialist.review('继续')


async def test_project_only_reflection_rejects_file_repair_and_receives_observations(tmp_path):
    store = Store(tmp_path)
    page_id = store.save('announcement', {'page_type': 'detail', 'title': 'project', 'source_url': 'https://example.org/p'})
    repair = {'id': 'files', 'description': 'Inspect files', 'tool': 'discover_tender_files',
        'arguments': {'announcement_id': page_id}, 'depends_on': [], 'critical': True}
    model = ScriptModel([{'content': encoded({'verdict': 'revise', 'summary': 'Files missing',
        'issues': [], 'next_actions': [], 'repair_steps': [repair]})}])
    state = {'spec': {'action': 'research', 'file_goal': 'none'}, 'observed_pages': [page_id, page_id]}
    specialist = Specialists(model, store, 'task', None, state, TOOLS, None)
    result = await specialist.review('No progress', phase='execution_recovery')
    assert result['repair_steps'] == []
    data = json.loads(model.calls[0][0][-1]['content'])
    assert len(data['observed_pages']) == 1 and data['verified_count'] == 0
    assert {item['name'] for item in data['repair_tool_schemas']} == {'read_tool_result', 'record_project_assessment'}


async def test_final_reflection_uses_compact_results_not_full_tool_evidence(tmp_path):
    model = ScriptModel([{'content': encoded({'verdict': 'pass', 'summary': '符合', 'issues': [], 'next_actions': []})}])
    state = {'spec': {'action': 'research', 'file_goal': 'none'},
        'evidence_working_set': [{'text': '巨大工具正文' * 10000}],
        'steps': [{'id': 'done', 'status': 'completed'}], 'qualified_projects': []}
    specialist = Specialists(model, Store(tmp_path), 'task', 'session', state, TOOLS, None)
    assert (await specialist.review('精简成果'))['verdict'] == 'pass'
    data = json.loads(model.calls[0][0][-1]['content'])
    assert data['evidence_working_set'] == [] and data['steps'] == []
    assert len(model.calls[0][0]) == 2


async def test_execution_review_receives_exact_schemas_observed_ids_and_errors(tmp_path):
    repair = {'id': 'inspect', 'description': 'Check observed file entry', 'tool': 'inspect_document_access',
        'arguments': {'announcement_id': 'page', 'entry_id': 'button'}, 'depends_on': [], 'critical': True}
    model = ScriptModel([{'content': encoded({'verdict': 'revise', 'summary': 'Inspect the observed entry',
        'issues': [], 'next_actions': ['Inspect'], 'repair_steps': [repair]})}])
    state = {'execution_feedback': [{'message': 'Missing entry_id'}],
        'recovery_state': {'status': 'invalid_arguments'},
        'evidence_working_set': [{'document_access': [{'entry_id': 'button'}]}]}
    specialist = Specialists(model, Store(tmp_path), 'task', None, state, TOOLS, None)
    result = await specialist.review('No download progress', phase='execution_recovery')
    assert result['repair_steps'][0]['arguments']['entry_id'] == 'button'
    data = json.loads(model.calls[0][0][-1]['content'])
    schemas = {item['name']: item['parameters'] for item in data['repair_tool_schemas']}
    assert set(schemas['inspect_document_access']['required']) == {'announcement_id', 'entry_id'}
    assert schemas['inspect_document_access']['additionalProperties'] is False
    assert data['execution_feedback'] == state['execution_feedback']
    assert data['recovery_state'] == state['recovery_state']


async def test_summary_repair_and_checkpoint_recovery_logged(tmp_path):
    store = Store(tmp_path)
    memory = {'goal': '寻找电力项目', 'constraints': [], 'facts': [], 'completed': [], 'pending': ['核验公告'], 'references': []}
    model = ScriptModel([{'content': 'bad JSON'}, {'content': encoded(memory)}])
    context = ContextManager(store, model, 'task')
    assert await context.summarize({'text': '原文'}, 'history_summary') == memory
    events = store.context_logs()
    assert any(item['payload'].get('exception', {}).get('validation_errors') for item in events)
    assert any(item['payload'].get('action') == 'repair_summary_format_or_size' and item['payload']['status'] == 'completed' for item in events)
    context.frames = [{'message': {'role': role, 'content': '重复网页正文' * 2000 if role == 'assistant' else '寻找电力项目'}, 'source_id': index + 1}
        for index, role in enumerate(['user', 'assistant', 'user'])]
    restored = context.recover('instructions', [], 100, 'summary_still_large')
    assert restored[-1]['content'] == '寻找电力项目'
    assert store.context_logs()[-1]['payload']['next_action'] == 'continue_with_evidence_reread'


async def test_producer_revises_after_independent_review(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'true')
    producer = 0
    review_count = 0
    def serve(request):
        nonlocal producer, review_count
        if planned := planning_response(request, action='research'):
            return planned
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith('# 反思 Agent'):
            review_count += 1
            answer = {'content': encoded({'verdict': 'revise' if review_count == 1 else 'pass',
                'summary': '需说明证据不足' if review_count == 1 else '已明确限制', 'issues': [], 'next_actions': ['补充证据或明确未完成']})}
        else:
            producer += 1
            if producer == 2:
                assert any('独立评审反馈' in (item.get('content') or '') for item in body['messages'])
            answer = {'content': '尚无已核验项目，任务未完成' if producer == 2 else '已找到项目'}
        return httpx.Response(200, json={'choices': [{'message': answer}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    async with client_class() as client:
        await Agent(Acquisition(store, client)).run('查找项目', 'task')
    assert producer == review_count == 2
    assert store.get('task')['review']['verdict'] == 'pass'
    assert len([item for item in store.model_logs() if item['purpose'] == 'reflection']) == 2


async def test_download_review_blocks_queue_for_wrong_document_type(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'true')
    criteria = [{'id': 'industry', 'description': '电力行业', 'requires_expiry': False}]
    responses = iter([call('fetch_tender_detail', {'url': 'https://example.org/bid'}),
        call('record_project_assessment', {'announcement_id': 'page', 'judgments': [{'criterion_id': 'industry',
            'verdict': 'match', 'reason': '电力项目', 'quotes': ['电力项目']}], 'summary': '符合电力行业条件',
            'files': [{'url': 'https://example.org/a.pdf', 'document_type': 'prequalification_notice', 'quote': '资格预审公告.pdf'}]}),
        call('plan_acquisition', {'announcement_id': 'page', 'scope': 'count', 'count': 1}),
        {'content': '只有资格预审公告，尚无完整标书'}])
    def serve(request):
        if planned := planning_response(request, action='download', file_goal='tender_document', expected_count=1, criteria=criteria):
            return planned
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith('# 反思 Agent'):
            assert json.loads(body['messages'][-1]['content'])['phase'] == 'download_planning'
            answer = {'content': encoded({'verdict': 'revise', 'summary': '资格预审公告不是完整标书',
                'issues': ['缺少完整标书'], 'next_actions': ['补充完整招标文件证据']})}
        else:
            answer = next(responses)
        return httpx.Response(200, json={'choices': [{'message': answer}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    async with client_class() as client:
        acquisition = Acquisition(store, client)
        async def fetch(url):
            page = {'source_url': url, 'page_type': 'detail', 'title': '电力项目', 'text': '电力项目 资格预审公告',
                'attachments': [{'name': '资格预审公告.pdf', 'url': 'https://example.org/a.pdf'}]}
            store.save('announcement', page, 'page')
            return {**page, 'announcement_id': 'page'}
        acquisition.fetch_tender_detail = fetch
        await Agent(acquisition).run('下载一个电力项目的完整标书', 'task')
    task = store.get('task')
    assert not task.get('batch_id')
    assert task['status'] == 'failed'
    assert task['error_code'] == 'NO_FILES_DOWNLOADED'
    assert task['review']['verdict'] == 'revise'
    assert '资格预审公告不是完整标书' in task['answer']


@pytest.mark.parametrize('role,expected', [('search', 3), ('page_files', 1)])
async def test_specialist_parallel_limits_and_browser_serialization(tmp_path, role, expected):
    class SlowModel(ScriptModel):
        active = maximum = 0
        async def call(self, messages, **kwargs):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {'content': '未联网，验收完成'}
    model = SlowModel([])
    specialist = Specialists(model, Store(tmp_path), 'task', None, {}, TOOLS, None)
    await asyncio.gather(*(specialist.delegate(role, '能力验收') for _ in range(4)))
    assert model.maximum == expected
