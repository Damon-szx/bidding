import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.batch import BatchManager, batch_view
from bidding.confirmation import ask, expire_task, pending_reply, waiting
from bidding.storage import Store
from model_fixtures import planning_response
from test_planning import step


@pytest.mark.parametrize('valid_reply', [True, False])
async def test_denial_does_not_download_and_unverified_reply_cannot_continue(tmp_path, monkeypatch, valid_reply):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    store = Store(tmp_path)
    session = store.create_session()
    store.save('task', {'session_id': session, 'spec': {'action': 'download', 'result_kind': 'projects'},
        'confirmation': {'status': 'answered' if valid_reply else 'expired', 'reply_task_id': 'reply'}}, 'original')
    store.save('task', {'clarification_reply': {'task_id': 'original', 'original_request': '下载三份标书',
        'question': '允许继续吗？'}}, 'reply')
    calls = 0
    def serve(request):
        nonlocal calls
        assert valid_reply, 'Unverified confirmation must not reach the model'
        calls += 1
        return planning_response(request, action='answer') or httpx.Response(200, json={
            'choices': [{'message': {'role': 'assistant', 'content': '已停止，不执行下载。'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    try:
        await Agent(acquisition).run('不允许继续，取消下载', 'reply', session)
        assert not store.list_records('batch')
        assert not store.logs(task_id='reply')
        assert calls == (2 if valid_reply else 0)
        assert store.get('reply')['status'] == ('completed' if valid_reply else 'failed')
    finally:
        await acquisition.close()


async def test_confirmed_public_file_goal_repairs_research_plan_and_refreshes_known_detail(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    session = store.create_session()
    page = {'source_url': 'https://fixture.example/detail', 'title': '招标公告', 'page_type': 'detail',
        'text': '招标公告', 'attachments': [{'name': '公告.pdf', 'url': 'https://fixture.example/notice.pdf'}]}
    page['announcement_id'] = store.save('announcement', page)
    log = store.start_tool('navigate_tender_entry', {}, 'agent', 'older', session)
    store.finish_tool(log, page, 0)
    store.save('task', {'session_id': session, 'spec': {'action': 'research', 'file_goal': 'public_files',
        'result_kind': 'projects'}, 'confirmation': {'status': 'answered', 'reply_task_id': 'reply'}}, 'parent')
    store.save('task', {'clarification_reply': {'task_id': 'parent', 'original_request': '公告也可以下载',
        'question': '允许下载公开公告吗？'}}, 'reply')
    steps = [step('read', 'fetch_tender_detail', args={'url': page['source_url']}),
        step('plan', 'plan_acquisition', ['read'], {'announcement_id': {'$ref': 'read.announcement_id'},
            'scope': 'count', 'count': 1, 'attachment_urls': ['https://fixture.example/notice.pdf']})]
    plans = 0
    def serve(request):
        nonlocal plans
        body = json.loads(request.content)
        proposed_steps = steps if plans != 1 else [step('read', 'fetch_tender_detail', args={'url': 'https://fixture.example/invented-list'})]
        if planned := planning_response(request, action='research' if plans == 0 else 'download',
                file_goal='public_files', steps=proposed_steps):
            payload = json.loads(body['messages'][1]['content'])
            if 'clarification_context' in payload:
                assert payload['clarification_context']['known_sources'][0]['source_url'] == page['source_url']
            plans += 1
            return planned
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [{
            'id': 'execute', 'type': 'function', 'function': {'name': 'execute_plan_steps',
            'arguments': json.dumps({'step_ids': ['read', 'plan']})}}]}}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    async def fetch(url):
        assert url == page['source_url']
        return page
    acquisition.fetch_tender_detail = fetch
    manager = BatchManager(store)
    manager.closed = True
    try:
        await Agent(acquisition, manager).run('可以', 'reply', session)
        task = store.get('reply')
        assert plans == 3
        assert task['spec']['action'] == 'download'
        assert task['status'] == 'scheduled', task
        assert task['batch_id']
        assert not any(log['tool'] == 'search_tenders' for log in store.logs(task_id='reply'))
    finally:
        await acquisition.close()


async def test_login_barrier_reports_without_asking_or_reading_login_page(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    monkeypatch.setenv('CONTEXT_COMPACT_THRESHOLD_TOKENS', '30000')
    store = Store(tmp_path)
    page = {'source_url': 'https://fixture.example/detail', 'title': '项目公告', 'page_type': 'detail',
        'text': '公开公告', 'attachments': [], 'document_access': [{'entry_id': 'access', 'title': '获取招标文件'}]}
    page['announcement_id'] = store.save('announcement', page)
    steps = [step('read', 'render_tender_page', args={'url': page['source_url']}),
        step('access', 'inspect_document_access', deps=['read'], args={
            'announcement_id': {'$ref': 'read.announcement_id'}, 'entry_id': 'access'})]
    rounds = 0
    def serve(request):
        nonlocal rounds
        if planned := planning_response(request, action='download', file_goal='tender_document', steps=steps):
            return planned
        rounds += 1
        assert rounds <= 2
        if rounds == 2:
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
                'content': '入口要求登录BOSP系统，没有其他可用项目，未下载文件。'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
        names = [item['function']['name'] for item in json.loads(request.content)['tools']]
        assert 'record_search_progress' not in names
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [{
            'id': 'execute', 'type': 'function', 'function': {'name': 'execute_plan_steps',
            'arguments': json.dumps({'step_ids': ['read', 'access']})}}]}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    async def render(url):
        return page
    async def inspect(announcement_id, entry_id):
        return {**page, 'document_access_status': 'requires_user_action',
            'access_error': {'message': '请先登录BOSP系统获取文件'}}
    acquisition.render_tender_page = render
    acquisition.inspect_document_access = inspect
    try:
        await Agent(acquisition).run('https://fixture.example/detail 帮我下载标书', 'task')
        task = store.get('task')
        assert task['status'] == 'failed', task
        assert 'BOSP' in task['answer']
        assert 'confirmation' not in task
        assert task['error_code'] == 'NO_FILES_DOWNLOADED'
        assert rounds == 2
        assert task['observed_pages'] == [page['announcement_id']]
        assert not task.get('batch_id')
        assert not any(log['tool'] == 'read_tool_result' for log in store.logs(task_id='task'))
    finally:
        await acquisition.close()


def test_confirmation_deadline_is_fail_closed_and_durable(tmp_path):
    store = Store(tmp_path)
    now = datetime.now(timezone.utc)
    state = {'message': '下载标书', 'session_id': 'owner', 'spec': {'action': 'download'}}
    ask(state, '正式标书还是公告附件？')
    state['confirmation'] = waiting('正式标书还是公告附件？', now)
    store.save('task', state, 'task')
    assert expire_task(store, store.get('task'), now + timedelta(seconds=179))['status'] == 'awaiting_clarification'
    result = expire_task(store, store.get('task'), now + timedelta(seconds=180))
    assert result['status'] == 'timed_out'
    assert result['error_code'] == 'CONFIRMATION_TIMEOUT'
    assert '没有任何文件成功下载' in result['answer']
    assert result['confirmation']['status'] == 'expired'
    assert pending_reply(store, 'owner') is None
    assert len(store.context_logs(task_id='task')) == 1


def test_optional_relaxation_timeout_keeps_original_authorization_and_scope(tmp_path):
    store = Store(tmp_path)
    state = {'message': '下载五个十月以前的标书', 'session_id': 'owner',
        'spec': {'action': 'download', 'questions': [], 'authorization_quote': '下载五个十月以前的标书',
            'file_goal': 'tender_document', 'fallback_file_goal': 'none', 'expected_count': 5,
            'criteria': [{'id': 'before_october', 'description': '十月以前'}]},
        'steps': [{'id': 'detail', 'status': 'completed', 'log_id': 'evidence'}],
        'usage': {'budget_consumed_tokens': 1000}}
    question = '是否允许公告替代标书？'
    ask(state, question, kind='relaxation')
    original = json.loads(json.dumps(state['spec']))
    store.save('task', state, 'task')
    result = expire_task(store, store.get('task'), datetime.now(timezone.utc) + timedelta(minutes=4))
    assert result['status'] == 'queued' and result['conservative_continuation']
    assert result['spec'] == original
    assert result['steps'][0]['status'] == 'completed'
    assert result['usage']['budget_consumed_tokens'] == 1000
    result['status'] = 'running'
    assert ask(result, question, kind='relaxation') is False
    assert ask(result, '换成公告行不行？', kind='relaxation') is False
    assert result['status'] == 'running'
    assert not result.get('error_code')


def test_runtime_required_strategy_question_cannot_revoke_resolved_download(tmp_path):
    store = Store(tmp_path)
    state = {'message': 'https://example.org/list 下载五个十月以前的标书',
        'spec': {'action': 'download', 'questions': [], 'authorization_quote': '下载五个十月以前的标书',
            'expected_count': 5, 'file_goal': 'tender_document', 'fallback_file_goal': 'none',
            'criteria': [{'id': 'date', 'description': '十月以前'}]}, 'steps': [{'id': 'read', 'status': 'completed'}]}
    ask(state, '继续向后翻页，还是更换来源？', kind='required')
    assert state['confirmation']['kind'] == 'relaxation'
    assert state['confirmation']['requested_kind'] == 'required'
    assert state['confirmation']['classification_reason']
    store.save('task', state, 'task')
    task = expire_task(store, store.get('task'), datetime.now(timezone.utc) + timedelta(minutes=4))
    assert task['status'] == 'queued' and task['conservative_continuation']
    assert task['spec'] == state['spec']
    assert task['steps'][0]['status'] == 'completed'


def test_essential_initial_ambiguity_remains_required():
    state = {'spec': {'action': 'download', 'authorization_quote': '帮我下载',
        'questions': ['下载哪个来源的什么文件？']}}
    ask(state, state['spec']['questions'][0], kind='required')
    assert state['confirmation']['kind'] == 'required'
    assert 'strict_spec' not in state['confirmation']


async def test_timeout_continuation_dispatches_once_and_retains_session_ownership(tmp_path, monkeypatch):
    import asyncio
    from bidding import api
    store = Store(tmp_path)
    session = store.create_session('scope')
    task_id = store.create_turn(session, '下载五个标书')
    store.release_session(session, task_id)
    task = store.get(task_id)
    task.update(status='queued', conservative_continuation=True)
    store.save('task', task, task_id)
    calls = []
    gate = asyncio.Event()
    async def run(message, ident, session_id):
        calls.append((message, ident, session_id))
        await gate.wait()
        store.release_session(session_id, ident)
    monkeypatch.setattr(api, 'store', store)
    monkeypatch.setattr(api.agent, 'run', run)
    runners = {}
    api.dispatch_conservative_continuation(store.get(task_id), runners)
    api.dispatch_conservative_continuation(store.get(task_id), runners)
    await asyncio.sleep(0)
    assert calls == [('下载五个标书', task_id, session)]
    assert not store.claim_existing_task(session, 'another-task')
    gate.set()
    await asyncio.gather(*list(runners.values()))


async def test_conservative_agent_continuation_does_not_replan_or_reset_token_usage(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    spec = {'action': 'research', 'goal': '按原日期查项目', 'file_goal': 'none', 'result_kind': 'pages',
        'criteria': [], 'expected_count': None, 'questions': [], 'steps': [], 'authorization_quote': ''}
    store.save('task', {'status': 'queued', 'conservative_continuation': True, 'spec': spec, 'steps': [],
        'events': [], 'denied_proposals': ['是否放宽日期？'], 'observed_pages': [],
        'usage': {'budget_consumed_tokens': 1000, 'input_tokens': 800, 'output_tokens': 200}}, 'task')
    client_class = httpx.AsyncClient
    bodies = []
    def serve(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '未找到符合原日期的项目。'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    try:
        await Agent(acquisition).run('按原日期查项目', 'task')
        result = store.get('task')
        assert result['spec'] == spec
        assert result['usage']['budget_consumed_tokens'] == 1120
        assert len(bodies) == 1
        assert '确认超时仅拒绝本次放宽' in bodies[0]['messages'][0]['content']
        assert all(log['purpose'] != 'task_plan' for log in store.model_logs(task_id='task'))
    finally:
        await acquisition.close()


async def test_initial_optional_question_cannot_replace_download_goal_with_answer(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    calls = []
    question = '是否只下载公开公告，或自行登录获取标书？'
    def serve(request):
        calls.append(json.loads(request.content))
        response = planning_response(request, action='answer' if len(calls) == 1 else 'download',
            file_goal='tender_document', expected_count=5, questions=[question],
            criteria=[{'id': 'date', 'description': '2026年10月以前', 'requires_expiry': False}])
        payload = response.json()
        spec = json.loads(payload['choices'][0]['message']['content'])
        if len(calls) > 1:
            spec['clarification_kind'] = 'relaxation'
        payload['choices'][0]['message']['content'] = json.dumps(spec)
        return httpx.Response(200, json=payload)
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    try:
        message = 'https://example.org/list 帮我下载五个10月份以前的标书文件'
        await Agent(acquisition).run(message, 'task')
        task = store.get('task')
        assert len(calls) == 2
        assert task['confirmation']['kind'] == 'relaxation'
        task = expire_task(store, task, datetime.now(timezone.utc) + timedelta(minutes=4))
        assert task['status'] == 'queued' and task['conservative_continuation']
        assert task['spec']['action'] == 'download' and task['spec']['questions'] == []
        assert task['spec']['file_goal'] == 'tender_document'
        assert task['spec']['fallback_file_goal'] == 'none'
        assert task['spec']['expected_count'] == 5
        assert task['spec']['authorization_quote'] == message
    finally:
        await acquisition.close()


@pytest.mark.parametrize('count,accepted,fallback', [(5, True, False), (6, False, False), (5, True, True), (6, False, True)])
async def test_verified_confirmation_chain_retains_quote_without_expanding_scope(tmp_path, monkeypatch, count, accepted, fallback):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    session = store.create_session('confirmation')
    original = '帮我下载五个近期的标书文件'
    store.save('task', {'session_id': session, 'message': original,
        'spec': {'action': 'download', 'result_kind': 'projects', 'file_goal': 'tender_document',
            'expected_count': 5, 'authorization_quote': original, 'fallback_file_goal': 'none'},
        'confirmation': {'status': 'answered', 'reply_task_id': 'reply'}}, 'parent')
    store.save('task', {'clarification_reply': {'task_id': 'parent', 'original_request': original,
        'question': '近期是否按最近发布日期排序？'}}, 'reply')
    def serve(request):
        response = planning_response(request, action='download', file_goal='tender_document', expected_count=count)
        if response:
            payload = response.json()
            spec = json.loads(payload['choices'][0]['message']['content'])
            spec['authorization_quote'] = original
            if fallback:
                spec.update(fallback_file_goal='announcement', fallback_authorization_quote='公告也可以')
            payload['choices'][0]['message']['content'] = json.dumps(spec)
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '尚未取得符合要求的文件。'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client())
    try:
        await Agent(acquisition).run('公告也可以' if fallback else '可以', 'reply', session)
        task = store.get('reply')
        assert bool(task.get('spec')) == accepted
        bindings = [row for row in store.context_logs(task_id='reply') if row['kind'] == 'authorization_binding']
        assert bool(bindings) == accepted
        assert not store.list_records('batch')
    finally:
        await acquisition.close()


async def test_expired_batch_cannot_be_confirmed_or_resumed(tmp_path):
    store = Store(tmp_path)
    manager = BatchManager(store)
    manager.closed = True
    page = store.save('announcement', {'source_url': 'https://fixture.example/list', 'page_type': 'listing',
        'entries': [], 'total_projects': 20, 'attachments': []})
    batch = manager.create(page, 'unspecified', 3, '下载', 'task', 'owner')
    batch['confirmation'] = waiting('确认范围', datetime.now(timezone.utc) - timedelta(minutes=4))
    store.save('batch', batch, batch['id'])
    with pytest.raises(ValueError):
        await manager.control(batch['id'], 'confirm', 'owner', 'count', 3)
    with pytest.raises(ValueError):
        await manager.control(batch['id'], 'resume', 'owner')
    assert batch_view(store, batch['id'])['status'] == 'cancelled'
    assert not store.items(batch['id'])


async def test_mid_execution_question_stops_remaining_same_round_tools(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    calls = [{'id': 'read', 'type': 'function', 'function': {'name': 'fetch_tender_detail',
        'arguments': json.dumps({'url': 'https://fixture.example/should-not-read'})}},
        {'id': 'ask', 'type': 'function', 'function': {'name': 'request_clarification',
        'arguments': json.dumps({'question': '下载正式标书还是公告？'})}}]
    client_class = httpx.AsyncClient
    def serve(request):
        return planning_response(request, action='download') or httpx.Response(200,
            json={'choices': [{'message': {'role': 'assistant', 'tool_calls': calls}}]})
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    async with client_class() as network:
        acquisition = Acquisition(store, network)
        await Agent(acquisition).run('帮我下载文件', 'task')
        task = store.get('task')
        assert task['status'] == 'awaiting_clarification'
        assert '3 分钟' in task['answer']
        assert task['confirmation']['status'] == 'pending'
        assert [log['tool'] for log in store.logs(task_id='task')] == ['request_clarification']
        assert not task.get('batch_id')
        await acquisition.close()


async def test_chat_reply_preserves_question_and_does_not_assume_authorization(tmp_path, monkeypatch):
    from bidding import api
    store = Store(tmp_path)
    session = store.create_session('test')
    original = {'message': '下载标书', 'session_id': session}
    ask(original, '允许下载公告吗？')
    store.save('task', original, 'original')
    monkeypatch.setattr(api, 'store', store)
    seen = []
    async def run(message, task_id, session_id):
        seen.append((message, store.get(task_id)['clarification_reply']))
        store.release_session(session_id, task_id)
    monkeypatch.setattr(api.agent, 'run', run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
        response = await client.post('/chat', json={'session_id': session, 'message': '不允许，先别下载'})
    assert response.status_code == 202
    assert seen == [('不允许，先别下载', {'task_id': 'original', 'original_request': '下载标书', 'question': '允许下载公告吗？'})]
    assert store.get('original')['confirmation']['status'] == 'answered'
    assert not store.list_records('batch')


async def test_expired_chat_reply_is_rejected_before_new_turn(tmp_path, monkeypatch):
    from bidding import api
    store = Store(tmp_path)
    session = store.create_session('test')
    state = {'status': 'awaiting_clarification', 'message': '下载', 'session_id': session,
        'confirmation': waiting('确认？', datetime.now(timezone.utc) - timedelta(minutes=4))}
    store.save('task', state, 'expired')
    monkeypatch.setattr(api, 'store', store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
        response = await client.post('/chat', json={'session_id': session, 'message': '同意'})
    assert response.status_code == 409
    assert store.get('expired')['error_code'] == 'CONFIRMATION_TIMEOUT'
    assert len(store.list_records('task')) == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
        response = await client.post('/chat', json={'session_id': session, 'message': '同意', 'confirmation_task_id': 'expired'})
    assert response.status_code == 409
    assert len(store.list_records('task')) == 1
