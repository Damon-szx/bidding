import copy

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.content import BLOCK_CHARS, index_content
from bidding.context import page_preview
from bidding.model import estimate_tokens
from bidding.results import canonical_read, covered_read, read_result
from bidding.storage import Store


def log_page(store, page, task='task'):
    ident = store.start_tool('fetch_tender_detail', {}, 'agent', task)
    store.finish_tool(ident, page, 1)
    return ident


def long_page():
    return {'page_type': 'detail', 'source_url': 'https://example.org/tender', 'title': '测试项目',
        'text': '一、项目概况\n' + '项目范围。' * 700 + '\n二、招标文件获取方式\n文件获取截止时间：2026-10-20 10:00。\n',
        'attachments': [{'url': 'https://example.org/tender.pdf', 'name': '招标文件.pdf'}]}


@pytest.mark.parametrize('text', ['', '一、范围\r\n正文\n\n二、获取方式\n末尾\n', '长行' * 10000, '😀汉字\t\r\n' * 1000])
def test_blocks_preserve_every_source_character_and_bind_versions(text):
    source = {'text': text, 'source_url': 'https://example.org/page'}
    page = index_content(source)
    assert ''.join(block['text'] for block in page['content_blocks']) == text
    for block in page['content_blocks']:
        assert text[block['start']:block['end']] == block['text']
        assert len(block['text']) <= BLOCK_CHARS
    assert index_content(page) == page
    assert index_content({**source, 'text': text + '变化'})['content_version'] != page['content_version']
    assert index_content({**source, 'source_url': 'https://example.org/other'})['content_version'] != page['content_version']
    assert 'content_blocks' not in source


def test_observed_html_headings_are_bound_to_block_layout():
    source = {'text': '项目\n介绍\n文件获取方式\n凭证\n', 'content_headings': ['文件获取方式']}
    indexed = index_content(source)
    assert any(block['title'] == '文件获取方式' for block in indexed['content_directory'])
    assert index_content({**source, 'content_headings': []})['content_version'] != indexed['content_version']


def test_long_page_preview_is_directory_and_legacy_is_reversible(monkeypatch):
    page = index_content(long_page())
    before = copy.deepcopy(page)
    projected = page_preview(page, 'source', 2000)
    assert projected['data']['text'] == ''
    assert not projected['data']['text_complete']
    assert projected['data']['content_directory']
    assert 'content_blocks' not in projected['data']
    assert projected['data']['attachments'] == page['attachments']
    assert estimate_tokens(projected) <= 2000
    monkeypatch.setenv('PAGE_CONTENT_MODE', 'legacy')
    legacy = page_preview(page, 'source', 2000)
    assert legacy['data']['text']
    assert 'content_directory' not in legacy['data']
    assert page == before


def test_block_reads_old_logs_without_mutation_and_rejects_wrong_source(tmp_path):
    store = Store(tmp_path)
    raw = long_page()
    source = log_page(store, raw)
    indexed = index_content(raw)
    block = next(block for block in indexed['content_blocks'] if '截止时间' in block['text'])
    result = read_result(store, source, 'task', block_id=block['block_id'])
    assert result['value'] == block['text']
    assert result['content_complete']
    assert result['block_ref']['content_version'] == indexed['content_version']
    assert result['block_ref']['start'] == block['start']
    assert estimate_tokens(result) <= 2000
    assert store.get_tool_log(source)['result'] == raw
    # Existing exact field/keyword reads still work without an index in storage.
    assert '截止时间' in read_result(store, source, 'task', field='text', contains='截止时间')['value']
    with pytest.raises(ValueError, match='not accessible'):
        read_result(store, source, 'other-task', block_id=block['block_id'])
    other = log_page(store, {**raw, 'text': raw['text'] + '内容变化'})
    with pytest.raises(ValueError, match='页面版本'):
        read_result(store, other, 'task', block_id=block['block_id'])
    with pytest.raises(ValueError, match='混用'):
        read_result(store, source, 'task', field='attachments', block_id=block['block_id'])


def test_partial_blocks_paginate_and_cache_only_covered_ranges(tmp_path):
    store = Store(tmp_path)
    page = index_content(long_page())
    source = log_page(store, page)
    block = page['content_blocks'][0]
    args = {'log_id': source, 'block_id': block['block_id']}
    first = read_result(store, source, 'task', block_id=block['block_id'], limit=5)
    read = store.start_tool('read_tool_result', {**args, 'limit': 5}, 'agent', 'task')
    store.finish_tool(read, first, 1)
    assert first['next_offset'] == 5
    assert covered_read(store, args, [read], 'task') is None
    rest = read_result(store, read, 'task', block_id=block['block_id'], offset=first['next_offset'])
    assert first['value'] + rest['value'] == block['text']
    ident = store.start_tool('read_tool_result', {**args, 'offset': 5}, 'agent', 'task')
    store.finish_tool(ident, rest, 1)
    cached = covered_read(store, args, [read, ident], 'task')
    assert cached['value'] == block['text']
    assert cached['block_ref']['block_id'] == block['block_id']
    assert cached['content_scope'] == 'block'
    assert canonical_read(store, args, 'task')['field'] == 'content_blocks.0.text'


async def test_static_acquisition_indexes_real_headings_before_saving(tmp_path, monkeypatch):
    async def validate(url):
        pass
    monkeypatch.setattr('bidding.acquisition.validate_public_url', validate)
    body = '<h1>招标公告</h1><p>' + '项目说明' * 500 + '</p><h2>招标文件获取方式</h2><p>请获取公开文件</p>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, text=body, headers={'content-type': 'text/html'}))) as client:
        store = Store(tmp_path)
        acquisition = Acquisition(store, client)
        page = await acquisition.fetch_tender_detail('https://example.org/page')
        assert ''.join(block['text'] for block in page['content_blocks']) == page['text']
        assert any(item['title'] == '招标文件获取方式' for item in page['content_directory'])
        assert store.get(page['announcement_id'])['content_version'] == page['content_version']


async def test_agent_can_read_selected_block_through_existing_tool(tmp_path, monkeypatch):
    import json
    from bidding.agent import Agent
    from model_fixtures import planning_response
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    raw = index_content(long_page())
    block = raw['content_blocks'][-1]
    store = Store(tmp_path)
    phase = 0
    def serve(request):
        nonlocal phase
        if planned := planning_response(request):
            return planned
        body = json.loads(request.content)
        phase += 1
        if phase == 1:
            answer = {'tool_calls': [{'id': 'fetch', 'type': 'function', 'function': {
                'name': 'fetch_tender_detail', 'arguments': json.dumps({'url': raw['source_url']})}}]}
        elif phase == 2:
            page_log = next(log for log in store.logs('task') if log['tool'] == 'fetch_tender_detail')
            assert 'content_directory' in body['messages'][-1]['content']
            answer = {'tool_calls': [{'id': 'block', 'type': 'function', 'function': {
                'name': 'read_tool_result', 'arguments': json.dumps({'log_id': page_log['id'], 'block_id': block['block_id']})}}]}
        else:
            assert '截止时间' in body['messages'][-1]['content']
            answer = {'content': '已读取文件获取截止时间，未执行下载。'}
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', **answer}}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs:
        client_class(transport=httpx.MockTransport(serve)))
    async with client_class() as client:
        acquisition = Acquisition(store, client)
        async def fetch(url):
            ident = store.save('announcement', raw)
            return {**raw, 'announcement_id': ident}
        acquisition.fetch_tender_detail = fetch
        await Agent(acquisition).run('读取公告中的文件获取截止时间', 'task')
    logs = [log for log in store.logs('task') if log['tool'] == 'read_tool_result']
    assert len(logs) == 1
    assert logs[0]['result']['value'] == block['text']
    assert logs[0]['result']['block_ref']['block_id'] == block['block_id']
    assert store.get('task')['status'] == 'completed'


def test_truncated_source_never_claims_full_document_index():
    page = index_content({**long_page(), 'text_truncated': True})
    assert not page['content_index_complete']
    assert not page_preview(page, 'log', 2000)['data']['content_index_complete']
