import json
from types import SimpleNamespace

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.batch import BatchManager
from bidding.storage import Store
from model_fixtures import planning_response
from test_planning import step
from test_eligibility import CRITERIA, page, judgments
from bidding.confirmation import ask


@pytest.mark.parametrize('placeholder_assessments', [False, True])
@pytest.mark.parametrize('clarification_reply', [False, True])
async def test_three_project_workflow_repairs_log_id_without_read_or_replan_loop(tmp_path, monkeypatch, placeholder_assessments, clarification_reply):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    # This scripted transport covers business calls; summary behavior has separate fixtures.
    monkeypatch.setenv('CONTEXT_COMPACT_THRESHOLD_TOKENS', '30000')
    async def fixture_url(url):
        assert url.startswith('https://example.org/')
    monkeypatch.setattr('bidding.acquisition.validate_public_url', fixture_url)
    store = Store(tmp_path)
    session = store.create_session()
    request_text = '从https://example.org/#/list下载3个项目的招标文件'
    task_id = store.create_turn(session, request_text)
    if clarification_reply:
        original_request = request_text
        request_text = '从列表页挑选，允许继续'
        original_task = {'session_id': session, 'message': original_request,
            'spec': {'action': 'download', 'result_kind': 'projects', 'goal': original_request,
                'expected_count': 3, 'file_goal': 'tender_document'}}
        ask(original_task, '从列表页挑选还是指定项目？')
        store.save('task', original_task, 'original')
        store.release_session(session, task_id)
    listing = {'page_type': 'listing', 'source_url': 'https://example.org/#/list',
        'entries': [{'entry_id': str(index), 'title': f'项目{index}', 'project_status': '正在招标'} for index in range(3)], 'attachments': []}
    listing['announcement_id'] = store.save('announcement', listing)
    details = [page(f'项目{index}招标公告') for index in range(3)]
    for index, value in enumerate(details):
        value['source_url'] = f'https://example.org/{index}'
        value['announcement_id'] = store.save('announcement', value)
    steps = [step('s1', 'render_tender_page', args={'url': listing['source_url']})] + [
        step(f'p{i}', 'navigate_tender_entry', deps=['s1'], args={
            'announcement_id': {'$ref': 's1.announcement_id'}, 'entry_id': {'$ref': f's1.entries.{i}.entry_id'}}) for i in range(3)]
    calls = 0
    if placeholder_assessments:
        steps += [step(f'a{i}', 'record_project_assessment', deps=[f'p{i}'], args={
            'announcement_id': {'$ref': f'p{i}.announcement_id'}, 'judgments': [], 'summary': '待原文核验'}) for i in range(3)]
    def serve(request):
        nonlocal calls
        draft_steps = [step('bad-resume', 'resume_task', args={'task_id': 'original'}), *steps] if clarification_reply else steps
        if planned := planning_response(request, action='resume' if clarification_reply else 'download', file_goal='tender_document', expected_count=3, criteria=CRITERIA, steps=draft_steps, files_per_project=1):
            return planned
        calls += 1
        if calls == 1:
            tools = [('execute_plan_steps', {'step_ids': ['s1']})]
        elif calls == 2:
            render_log = next(log for log in store.logs(task_id=task_id) if log['tool'] == 'render_tender_page')
            tools = [('navigate_tender_entry', {'announcement_id': render_log['id'], 'entry_id': '0'}),
                ('execute_plan_steps', {'step_ids': ['p1', 'p2']})]
        elif calls == 3:
            tools = [('record_project_assessment', {'announcement_id': value['announcement_id'],
                'judgments': judgments(value), 'summary': '符合用户条件', 'files': [{
                    'url': value['attachments'][0]['url'], 'document_type': 'tender_document', 'quote': '完整招标文件.pdf'}]}) for value in details]
        else:
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '完成核验，创建下载任务。'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
        answer = {'role': 'assistant', 'tool_calls': [{'id': f'{calls}-{i}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(tools)]}
        return httpx.Response(200, json={'choices': [{'message': answer}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client_class())
    async def render(url):
        return listing
    async def navigate(announcement_id, entry_id):
        assert announcement_id == listing['announcement_id']
        return details[int(entry_id)]
    acquisition.render_tender_page = render
    acquisition.navigate_tender_entry = navigate
    def worker_factory(worker_store):
        worker = Acquisition(worker_store, client_class(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, content=b'%PDF-1.4\nfixture\n%%EOF', headers={'content-type': 'application/pdf'}))))
        async def fetch(url):
            return next(value for value in details if value['source_url'] == url)
        worker.fetch_tender_detail = fetch
        return worker
    manager = BatchManager(store, worker_factory)
    manager.closed = True
    try:
        if clarification_reply:
            from bidding import api
            async def execute_reply(message, ident, session_id):
                nonlocal task_id
                task_id = ident
                await Agent(acquisition, manager).run(message, ident, session_id)
            monkeypatch.setattr(api, 'store', store)
            monkeypatch.setattr(api, 'agent', SimpleNamespace(run=execute_reply))
            async with client_class(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as web:
                response = await web.post('/chat', json={'session_id': session, 'message': request_text})
            assert response.status_code == 202, response.text
            assert response.json()['task_id'] == task_id
            assert store.get('original')['confirmation']['reply_task_id'] == task_id
        else:
            await Agent(acquisition, manager).run(request_text, task_id, session)
        task = store.get(task_id)
        assert task['status'] == 'scheduled', task
        assert task['spec']['action'] == 'download'
        if clarification_reply:
            assert task['continued_from'] == 'original'
            assert not any(item['tool'] == 'resume_task' for item in task['steps'])
            assert not any(log['tool'] == 'resume_task' for log in store.logs(task_id=task_id))
        assert len(store.get(task['batch_id'])['contract']['targets']) == 3
        assert store.get(task['batch_id'])['contract']['files_per_project'] == 1
        assert calls == 4
        logs = store.logs(task_id=task_id)
        assert not any(log['status'] == 'failed' for log in logs)
        assert not any(log['tool'] == 'read_tool_result' for log in logs)
        assert len([log for log in logs if log['tool'] == 'navigate_tender_entry']) == 3
        assert any(event['kind'] == 'reference_repair' for event in store.context_logs(task_id=task_id))
        if not clarification_reply:
            assert all(row['request_text'] == request_text for row in store.audit_logs(task_id))
        if placeholder_assessments:
            assert any(event['kind'] == 'plan_repair' and event['payload'].get('action') == 'defer_evidence_dependent_nodes'
                for event in store.context_logs(task_id=task_id))
            assert not any(item['id'] in ('a0', 'a1', 'a2') for item in task['steps'])
        await manager.run(task['batch_id'])
        batch = store.get(task['batch_id'])
        assert batch['status'] == 'completed', batch
        assert len([item for item in store.items(batch['id']) if item['status'] == 'downloaded']) == 3
        assert len(store.list_records('file')) == 3
    finally:
        await acquisition.close()
