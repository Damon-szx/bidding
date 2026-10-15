import asyncio
import json

import httpx
import pytest

from bidding.acquisition import DownloadCapacityExceeded, GenericHTMLAdapter
from bidding.agent import Agent
from bidding.batch import BatchManager, batch_view, identity
from bidding.storage import Store
from model_fixtures import planning_response


@pytest.mark.parametrize('target_count', [3, 5])
async def test_authorized_fallback_downloads_real_local_files_without_relabeling_as_tenders(tmp_path, monkeypatch, target_count):
    from pathlib import Path
    from bidding.acquisition import Acquisition
    from bidding.eligibility import validate_assessment
    async def allow(url):
        pass
    monkeypatch.setattr('bidding.acquisition.validate_public_url', allow)
    store = Store(tmp_path)
    settings = {'names': {i: ['公告.pdf'] for i in range(1, target_count + 1)}}
    fixture = Fixture(store, settings)
    criteria = {'criteria': [], 'action': 'download', 'file_goal': 'tender_document',
        'fallback_file_goal': 'announcement', 'expected_count': target_count, 'file_formats': ['pdf']}
    targets = []
    for i in range(1, target_count + 1):
        page = await fixture.fetch_tender_detail(f'https://fixture.example/p/{i}')
        evaluation = validate_assessment(page, criteria, [], '允许的公告回退', files=[{
            'url': page['attachments'][0]['url'], 'document_type': 'announcement_document', 'quote': '公告.pdf'}])
        evaluation.update(fallback_used=True, fallback_basis='no_primary_access_observed')
        ident = store.save('assessment', {**evaluation, 'task_id': 'chat', 'announcement_id': page['announcement_id']})
        targets.append({**page, 'assessment_id': ident, 'eligibility': evaluation})
    class DownloadFixture(Fixture):
        def __init__(self, store):
            super().__init__(store, settings)
            self.acquisition = Acquisition(store, httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b'%PDF-1.7\n' + str(request.url).encode()))))
        async def download_tender_file(self, url, announcement_id, max_bytes):
            return await self.acquisition.download_tender_file(url, announcement_id, max_bytes)
        async def close(self):
            await self.acquisition.close()
    manager = BatchManager(store, DownloadFixture)
    manager.closed = True
    batch = manager.create(targets[0]['announcement_id'], 'count', target_count, f'下载{target_count}个，允许公告回退',
        'chat', None, targets=targets, criteria=criteria, require_documents=True)
    await manager.run(batch['id'])
    result = batch_view(store, batch['id'])
    assert result['counts']['downloaded'] == target_count, result
    files = store.list_records('file')
    assert len(files) == target_count
    assert all(Path(file['path']).read_bytes().startswith(b'%PDF-') for file in files)
    assert all(file['document_type'] == 'announcement_document' and file['fallback_used'] for file in files)


def source(store, total=1574, size=20, next_page=True):
    return store.save('announcement', {'source_url': 'https://fixture.example/list', 'page_type': 'listing',
        'entries': [{'entry_id': f'e{i}', 'title': f'项目{i}', 'url': f'https://fixture.example/p/{i}'} for i in range(size)],
        'pagination': [{'entry_id': 'next'}] if next_page else [], 'total_projects': total, 'attachments': []})


@pytest.mark.parametrize('scope,count,message,expected', [
    ('unspecified', 3, '下载这个列表的标书', 'awaiting_confirmation'),
    ('all', 3, '下载这个列表的标书', 'awaiting_confirmation'),
    ('count', 1574, '一共有1574个，请先下载3个', 'awaiting_confirmation'),
    ('all', 3, '全部下载', 'queued'),
    ('all', 3, '不要全部下载，先3个', 'awaiting_confirmation'),
    ('count', 3, '下载前3个项目', 'queued'),
    ('count', 10, '下载10个项目', 'queued'),
])
def test_scope_authorization(tmp_path, scope, count, message, expected):
    store = Store(tmp_path)
    manager = BatchManager(store)
    manager.closed = True
    result = manager.create(source(store), scope, count, message, 'chat', 'session')
    assert result['status'] == expected
    assert not store.items(result['id'])
    assert result['contract']['model_calls_in_workers'] == 0


def test_small_listing_default_and_single_project(tmp_path):
    store = Store(tmp_path)
    manager = BatchManager(store)
    manager.closed = True
    small = manager.create(source(store, 3, 3, False), 'unspecified', 3, '下载标书', 'one', None)
    assert small['status'] == 'queued' and small['contract']['count'] == 3
    detail = store.save('announcement', {'source_url': 'https://fixture.example/p/1', 'page_type': 'detail', 'attachments': []})
    result = manager.create(detail, 'all', 5000, '全部附件下载', 'two', None)
    assert result['contract']['count'] == 1


class Fixture:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    async def close(self):
        pass

    async def fetch_tender_detail(self, url):
        if '/list' in url:
            index = int(url.rsplit('/', 1)[-1]) if url.rsplit('/', 1)[-1].isdigit() else 0
            return self.page(index)
        number = int(url.rsplit('/', 1)[-1])
        names = self.settings.get('names', {number: ['招标文件.pdf']}).get(number, [])
        result = {'source_url': url, 'title': f'项目{number}', 'page_type': 'detail',
            'attachments': [{'url': f'https://fixture.example/file/{number}/{name}', 'name': name} for name in names],
            'text': self.settings.get('text', {}).get(number, '')}
        result['announcement_id'] = self.store.save('announcement', result)
        return result

    def page(self, index):
        pages = self.settings.get('pages', [[1, 2], [2, 3, 4]])
        entries = [{'entry_id': f'e{n}', 'title': f'项目{n}', 'url': f'https://fixture.example/p/{n}'} for n in pages[index]]
        page = {'source_url': f'https://fixture.example/list/{index}', 'index': index, 'page_type': 'listing', 'entries': entries,
            'pagination': [{'entry_id': 'next'}] if index < len(pages) - 1 else [], 'attachments': [], 'total_projects': self.settings.get('total')}
        page['announcement_id'] = self.store.save('announcement', page)
        return page

    async def navigate_tender_entry(self, announcement_id, entry_id):
        page = self.store.get(announcement_id)
        return self.page(page['index'] + 1)

    async def download_tender_file(self, url, announcement_id, max_bytes):
        self.settings.setdefault('calls', []).append(url)
        if self.settings.get('block'):
            self.settings['entered'].set()
            await self.settings['block'].wait()
        if url in self.settings.get('fail', []):
            raise ValueError('fixture download failure')
        if max_bytes < 10:
            raise DownloadCapacityExceeded('fixture capacity')
        file_id = self.store.save('file', {'filename': url.rsplit('/', 1)[-1], 'size': 10})
        return {'file_id': file_id, 'size': 10, 'filename': url.rsplit('/', 1)[-1], 'local_download_url': '/files/' + file_id}


def setup(tmp_path, settings=None, scope='all', count=3):
    settings = settings if settings is not None else {}
    store = Store(tmp_path)
    manager = BatchManager(store, lambda store: Fixture(store, settings))
    manager.closed = True
    batch = manager.create(source(store, settings.get('total', 4)), scope, count, '全部下载' if scope == 'all' else f'下载{count}个项目', 'chat', 'session')
    return store, manager, batch['id']


async def test_batch_dedup_outcomes_trace_and_zero_model_calls(tmp_path):
    settings = {'names': {1: ['招标文件.pdf'], 2: [], 3: [], 4: ['资格预审公告.pdf']}, 'text': {3: '请先登录后下载'}}
    store, manager, batch_id = setup(tmp_path, settings)
    await manager.run(batch_id)
    batch = batch_view(store, batch_id)
    assert batch['status'] == 'needs_input'
    assert batch['counts'] == {'downloaded': 2, 'no_attachment': 1, 'needs_input': 1, 'total': 4}
    assert batch['enumeration_complete'] and batch['bytes_downloaded'] == 20
    logs = store.logs(task_id='chat')
    assert all(log['source'] == 'batch' and log['session_id'] == 'session' for log in logs)
    assert len([log for log in logs if log['tool'] == 'download_tender_file']) == 2
    assert store.model_logs(task_id='chat') == []


async def test_no_files_does_not_satisfy_download_goal(tmp_path):
    store, manager, batch_id = setup(tmp_path, {'names': {1: [], 2: [], 3: [], 4: []}}, scope='count', count=3)
    await manager.run(batch_id)
    batch = batch_view(store, batch_id)
    assert batch['status'] == 'partial_completed'
    assert batch['counts']['total'] == 3 and batch['counts']['no_attachment'] == 3
    assert batch['error_code'] == 'NO_FILES_DOWNLOADED'
    assert '下载失败：没有任何文件成功下载' in batch['reason']


async def test_batch_downloads_pdf_only(tmp_path):
    settings = {'pages': [[1, 2]], 'total': 2, 'names': {1: ['招标文件.zip'], 2: ['公告.pdf', '技术标.docx']}}
    store, manager, batch_id = setup(tmp_path, settings)
    await manager.run(batch_id)
    result = batch_view(store, batch_id)
    assert result['status'] == 'partial_completed' and result['counts']['no_attachment'] == 1
    assert len(settings['calls']) == 1 and settings['calls'][0].endswith('.pdf')
    assert result['contract']['file_formats'] == ['pdf']
    assert len(store.items(batch_id)[0]['payload']['excluded_attachments']) == 1


async def test_retry_only_failed_and_keep_downloaded_files(tmp_path):
    failed_url = 'https://fixture.example/file/2/招标文件.pdf'
    settings = {'fail': [failed_url]}
    store, manager, batch_id = setup(tmp_path, settings)
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert settings['calls'].count(failed_url) == 1
    before = len(settings['calls'])
    settings['fail'] = []
    await manager.control(batch_id, 'retry_failed', 'session')
    await manager.run(batch_id)
    assert len(settings['calls']) == before + 1
    assert batch_view(store, batch_id)['status'] == 'completed'
    assert batch_view(store, batch_id)['bytes_downloaded'] == 40


async def test_capacity_is_partial_and_checkpoint_retained(tmp_path):
    store, manager, batch_id = setup(tmp_path)
    batch = store.get(batch_id)
    batch['contract']['max_bytes'] = 15
    store.save('batch', batch, batch_id)
    await manager.run(batch_id)
    result = batch_view(store, batch_id)
    assert result['status'] == 'partial_completed'
    assert result['bytes_downloaded'] == 10 and result['counts']['downloaded'] == 1
    assert result['counts'].get('pending')


async def test_pause_resume_cancel_and_restart(tmp_path):
    settings = {'block': asyncio.Event(), 'entered': asyncio.Event()}
    store, manager, batch_id = setup(tmp_path, settings)
    runner = asyncio.create_task(manager.run(batch_id))
    manager.runners[batch_id] = runner
    await asyncio.wait_for(settings['entered'].wait(), 3)
    paused = await manager.control(batch_id, 'pause', 'session')
    assert paused['status'] == 'paused'
    assert not paused['counts'].get('running')
    settings['block'].set()
    await manager.control(batch_id, 'resume', 'session')
    manager.recover()
    assert batch_view(store, batch_id)['status'] == 'paused'
    await manager.control(batch_id, 'resume', 'session')
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'completed'
    other = manager.create(source(store), 'unspecified', 3, '下载', 'other', 'session')
    cancelled = await manager.control(other['id'], 'cancel', 'session')
    assert cancelled['status'] == 'cancelled'
    with pytest.raises(ValueError):
        await manager.control(other['id'], 'resume', 'session')


async def test_confirmation_ownership_and_unknown_total(tmp_path):
    store = Store(tmp_path)
    manager = BatchManager(store)
    manager.closed = True
    result = manager.create(source(store, None, 1, False), 'unspecified', 3, '下载', 'chat', 'owner')
    assert result['status'] == 'awaiting_confirmation'
    with pytest.raises(ValueError, match='belong'):
        await manager.control(result['id'], 'confirm', 'other', 'all')
    result = await manager.control(result['id'], 'confirm', 'owner', 'count', 3)
    assert result['status'] == 'queued' and result['contract']['count'] == 3


async def test_missing_next_page_and_shortfall_not_completed(tmp_path):
    store, manager, batch_id = setup(tmp_path, {'pages': [[1, 2]], 'total': 1574})
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert not batch_view(store, batch_id)['enumeration_complete']
    store, manager, batch_id = setup(tmp_path / 'count', {'pages': [[1, 2]]}, 'count', 3)
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert batch_view(store, batch_id)['scope_shortfall'] == 1
    store, manager, batch_id = setup(tmp_path / 'unknown', {'pages': [[1, 2]], 'total': None})
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert '不能确认全量' in batch_view(store, batch_id)['reason']


def test_log_metadata_pagination_preserves_full_original(tmp_path):
    store = Store(tmp_path)
    for index in range(3):
        log_id = store.start_tool('fetch_tender_detail', {'index': index}, 'batch', 'chat', 'session')
        store.finish_tool(log_id, {'text': '原始网页' * 2000}, 3)
    rows = store.logs(session_id='session', limit=1, newest_first=True, include_results=False)
    assert len(rows) == 1 and 'result' not in rows[0]
    assert rows[0]['arguments']['index'] == 2
    assert len(store.get_tool_log(rows[0]['id'])['result']['text']) == 8000
    assert store.logs(session_id='session', offset=1, limit=1, newest_first=True)[0]['arguments']['index'] == 1


async def test_project_limit_never_claims_all_complete(tmp_path):
    store, manager, batch_id = setup(tmp_path, {'pages': [[1, 2, 3]], 'total': 3})
    batch = store.get(batch_id)
    batch['contract']['max_projects'] = 2
    store.save('batch', batch, batch_id)
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert not batch_view(store, batch_id)['enumeration_complete']


async def test_per_file_checkpoint_skips_successful_attachment_on_retry(tmp_path):
    failed_url = 'https://fixture.example/file/1/附件二.pdf'
    settings = {'pages': [[1]], 'total': 1, 'names': {1: ['附件一.pdf', '附件二.pdf']}, 'fail': [failed_url]}
    store, manager, batch_id = setup(tmp_path, settings)
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['bytes_downloaded'] == 10
    assert len(store.items(batch_id)[0]['payload']['files']) == 1
    settings['fail'] = []
    await manager.control(batch_id, 'retry_failed', 'session')
    await manager.run(batch_id)
    assert settings['calls'].count('https://fixture.example/file/1/附件一.pdf') == 1
    assert batch_view(store, batch_id)['bytes_downloaded'] == 20


async def test_repeated_pagination_and_timeout_are_partial(tmp_path):
    store, manager, batch_id = setup(tmp_path, {'pages': [[1, 2], [1, 2]], 'total': 4})
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert '分页未推进' in batch_view(store, batch_id)['reason']
    settings = {'block': asyncio.Event(), 'entered': asyncio.Event()}
    store, manager, batch_id = setup(tmp_path / 'timeout', settings)
    batch = store.get(batch_id)
    batch['contract']['timeout_seconds'] = 0.05
    store.save('batch', batch, batch_id)
    await manager.run(batch_id)
    assert batch_view(store, batch_id)['status'] == 'partial_completed'
    assert not batch_view(store, batch_id)['counts'].get('running')


async def test_batch_api_scope_controls_items_and_manifest(tmp_path, monkeypatch):
    from bidding import api
    store, manager, batch_id = setup(tmp_path)
    monkeypatch.setattr(api, 'store', store)
    class AgentStub:
        batches = manager
    monkeypatch.setattr(api, 'agent', AgentStub())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
        assert len((await client.get('/batches?session_id=session')).json()['batches']) == 1
        assert (await client.get('/batches?session_id=other')).json()['batches'] == []
        assert (await client.post(f'/batches/{batch_id}/control', json={'session_id': 'other', 'action': 'cancel'})).status_code == 409
        assert (await client.get('/batches/missing')).status_code == 404
        await manager.run(batch_id)
        assert (await client.get(f'/batches/{batch_id}/items?limit=1')).json()['items'][0]['status'] == 'downloaded'
        response = await client.get(f'/batches/{batch_id}/manifest')
        assert response.headers['content-disposition'].startswith('attachment;')
        assert len(response.json()['projects']) == 4
        assert 'files' in response.json()['projects'][0]


def test_manifest_identity_not_browser_ref_and_static_total(tmp_path):
    assert identity({'title': '电力项目', 'project_number': 'D1', 'entry_id': 'old'}) == identity({'title': '电力项目', 'project_number': 'D1', 'entry_id': 'new'})
    page = GenericHTMLAdapter().parse('https://fixture.example/list', '<p>共 1,574 条</p><a href="/page2">下一页</a>')
    assert page['total_projects'] == 1574 and page['pagination'][0]['url'].endswith('/page2')


async def test_agent_premature_answer_is_replaced_by_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    store = Store(tmp_path)
    page_id = source(store)
    manager = BatchManager(store)
    manager.closed = True
    responses = [
        {'role': 'assistant', 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {'name': 'fetch_tender_detail', 'arguments': '{"url":"https://fixture.example/list"}'}}]},
        {'role': 'assistant', 'content': '所有文件已经下载完成。'},
    ]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda req: planning_response(req, action='download', file_goal='public_pdf') or httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]}))))
    acquisition = Fixture(store, {})
    async def fetch(url):
        return {**store.get(page_id), 'announcement_id': page_id}
    acquisition.fetch_tender_detail = fetch
    acquisition.render_tender_page = fetch
    acquisition.discover_tender_files = fetch
    await Agent(acquisition, manager).run('帮我下载这个列表的标书', 'chat')
    result = store.get('chat')
    assert result['status'] == 'awaiting_confirmation'
    assert '1574' in result['answer'] and '尚未开始下载' in result['answer']
    assert not store.items(result['batch_id'])
    assert not any(log['tool'] == 'download_tender_file' for log in store.logs())


async def test_agent_cannot_upgrade_confirmation_to_all(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    store = Store(tmp_path)
    session_id = store.create_session('test')
    manager = BatchManager(store)
    manager.closed = True
    batch = manager.create(source(store), 'unspecified', 3, '下载', 'old', session_id)
    task_id = store.create_turn(session_id, '确认下载前3个项目')
    responses = [
        {'role': 'assistant', 'tool_calls': [{'id': 'control', 'type': 'function', 'function': {'name': 'control_acquisition_batch',
            'arguments': json.dumps({'batch_id': batch['id'], 'action': 'confirm', 'scope': 'all'})}}]},
        {'role': 'assistant', 'content': '请确认范围。'},
    ]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda req: planning_response(req, action='control', control_action='confirm') or httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]}))))
    fixture = Fixture(store, {})
    fixture.render_tender_page = fixture.fetch_tender_detail
    fixture.discover_tender_files = fixture.fetch_tender_detail
    await Agent(fixture, manager).run('确认下载前3个项目', task_id, session_id)
    assert batch_view(store, batch['id'])['status'] == 'awaiting_confirmation'
    assert '没有确认全量' in store.logs(task_id=task_id)[0]['error']


async def test_hypothetical_request_cannot_schedule_download(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    store = Store(tmp_path)
    manager = BatchManager(store)
    manager.closed = True
    page_id = source(store)
    responses = [
        {'role': 'assistant', 'tool_calls': [{'id': 'plan', 'type': 'function', 'function': {'name': 'plan_acquisition',
            'arguments': json.dumps({'announcement_id': page_id, 'scope': 'all'})}}]},
        {'role': 'assistant', 'content': '这里需要先确认范围。'},
    ]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda req: planning_response(req, action='answer') or httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]}))))
    fixture = Fixture(store, {})
    fixture.render_tender_page = fixture.fetch_tender_detail
    fixture.discover_tender_files = fixture.fetch_tender_detail
    await Agent(fixture, manager).run('假设用户说全部下载，你如何处理，先说思路', 'chat')
    assert store.list_records('batch') == []
    assert store.logs(task_id='chat')[0]['status'] == 'failed'
