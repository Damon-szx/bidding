import pytest
import json
import httpx

from bidding.listing import prescreen_entries, scan_listing
from bidding.storage import Store
from bidding.acquisition import Acquisition
from bidding.agent import Agent
from model_fixtures import planning_response


def entry(title, date, date_kind='published_at'):
    return {'entry_id': title, 'title': title, 'url': None, 'listed_date': date, 'date_kind': date_kind}


def test_list_dates_exclude_only_confirmed_publication_fields():
    page = {'id': 'page', 'entries': [entry('new', '2026-10-01'), entry('old', '2026-09-30'),
        entry('unknown', '2026-10-14', 'unknown'), entry('missing', None)]}
    selected, rejected = prescreen_entries(page, '2026-10-01')
    assert [item['title'] for item in rejected] == ['new']
    assert [item['screening'] for item in selected] == ['candidate', 'needs_evidence', 'needs_evidence']


async def test_scan_reaches_fourth_page_without_opening_irrelevant_details(tmp_path):
    store = Store(tmp_path)
    for index in range(1, 5):
        store.save('announcement', {'page_type': 'listing', 'entries': [entry('project-' + str(index),
            '2026-10-10' if index < 4 else '2026-09-30')],
            'pagination': [{'entry_id': 'next-' + str(index), 'title': 'Next', 'mechanism': 'click'}]}, str(index))
    calls = []
    async def advance(page_id, entry_id):
        assert entry_id == 'next-' + page_id
        calls.append((page_id, entry_id))
        return {'announcement_id': str(int(page_id) + 1)}
    result = await scan_listing(store, '1', advance, '2026-10-01', 5)
    assert len(calls) == 3 and len(result['pages']) == 4
    assert result['excluded_count'] == 3
    assert result['candidates'][0]['announcement_id'] == '4'
    assert result['stop_reason'] == 'candidates_ready'


async def test_repeated_page_and_page_limit_do_not_mean_source_exhausted(tmp_path):
    store = Store(tmp_path)
    store.save('announcement', {'page_type': 'listing', 'entries': [entry('new', '2026-10-10')],
        'pagination': [{'entry_id': 'next', 'title': 'Next'}]}, 'page')
    async def advance(page_id, entry_id):
        return {'announcement_id': page_id}
    result = await scan_listing(store, 'page', advance, '2026-10-01', 2)
    assert result['stop_reason'] == 'repeated_page'
    result = await scan_listing(store, 'page', advance, '2026-10-01', 1)
    assert result['stop_reason'] == 'page_limit'


async def test_unknown_rows_do_not_end_scan_or_count_as_matches(tmp_path):
    store = Store(tmp_path)
    store.save('announcement', {'page_type': 'listing', 'entries': [entry('unknown', '2026-10-14', 'unknown')],
        'pagination': [{'entry_id': 'next', 'title': 'Next'}]}, '1')
    store.save('announcement', {'page_type': 'listing', 'entries': [entry('old', '2026-09-30')], 'pagination': []}, '2')
    calls = []
    async def advance(page_id, entry_id):
        calls.append(page_id)
        return {'announcement_id': '2'}
    result = await scan_listing(store, '1', advance, '2026-10-01', 5)
    assert calls == ['1']
    assert result['candidate_count'] == 1 and result['unresolved_count'] == 1
    assert result['unresolved_candidates'][0]['title'] == 'unknown'
    assert result['candidates'][0]['title'] == 'old'


async def test_all_unknown_pages_are_bounded_not_falsely_ready(tmp_path):
    store = Store(tmp_path)
    for i in range(1, 4):
        store.save('announcement', {'page_type': 'listing', 'entries': [entry(str(i), '2026-10-14', 'unknown')],
            'pagination': [{'entry_id': 'next', 'title': 'Next'}]}, str(i))
    async def advance(page_id, entry_id):
        return {'announcement_id': str(int(page_id) + 1)}
    result = await scan_listing(store, '1', advance, '2026-10-01', 3)
    assert result['candidate_count'] == 0 and result['unresolved_count'] == 3
    assert result['stop_reason'] == 'page_limit'


async def test_agent_skips_known_bad_detail_and_scans_before_premature_final(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    store = Store(tmp_path)
    for i in range(1, 5):
        store.save('announcement', {'page_type': 'listing', 'source_url': 'https://example.org/list',
            'entries': [entry('entry-' + str(i), '2026-10-10' if i < 4 else '2026-09-30')],
            'pagination': [{'entry_id': 'next-' + str(i), 'title': 'Next'}], 'attachments': [], 'text': ''}, str(i))
    calls = []
    async def render(url):
        return {**store.get('1'), 'announcement_id': '1'}
    async def navigate(announcement_id, entry_id):
        assert entry_id == 'next-' + announcement_id
        calls.append(announcement_id)
        ident = str(int(announcement_id) + 1)
        return {**store.get(ident), 'announcement_id': ident}
    rounds = 0
    def serve(request):
        nonlocal rounds
        if response := planning_response(request, action='download', file_goal='tender_document', expected_count=5,
            criteria=[{'id': 'date', 'description': '发布日期早于2026-10-01', 'requires_expiry': False}]):
            return response
        rounds += 1
        operations = [('render_tender_page', {'url': 'https://example.org/list'}),
            ('navigate_tender_entry', {'announcement_id': '1', 'entry_id': 'entry-1'})]
        if rounds <= 2:
            name, arguments = operations[rounds - 1]
            answer = {'role': 'assistant', 'tool_calls': [{'id': str(rounds), 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
        else:
            answer = {'role': 'assistant', 'content': '当前页未找到匹配文件。'}
        return httpx.Response(200, json={'choices': [{'message': answer}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(serve)))
    acquisition = Acquisition(store, client())
    agent = Agent(acquisition)
    agent.runtime.handlers.update(render_tender_page=render, navigate_tender_entry=navigate)
    try:
        await agent.run('https://example.org/list 下载五个10月份以前的标书', 'task')
        assert calls == ['1', '2', '3']
        assert store.get('task')['listing_scan']['candidates'][0]['announcement_id'] == '4'
        assert any(log['result'].get('status') == 'excluded_by_list_evidence' for log in store.logs(task_id='task'))
    finally:
        await acquisition.close()
