import pytest

from bidding.context import ContextManager
from bidding.model import estimate_tokens
from bidding.results import read_result, page_reference
from bidding.storage import Store
from test_context import SummaryModel


def logged(store, tool, result, args=None, task='task', session=None):
    ident = store.start_tool(tool, args or {}, 'agent', task, session)
    store.finish_tool(ident, result, 1)
    return ident


def test_page_log_alias_resolves_to_actual_page_and_preserves_access_boundary(tmp_path):
    store = Store(tmp_path)
    page = store.save('announcement', {'entries': []})
    log = logged(store, 'render_tender_page', {'announcement_id': page})
    assert page_reference(store, log, 'task', None) == page
    assert page_reference(store, page, 'task', None) == page
    with pytest.raises(ValueError, match='not accessible'):
        page_reference(store, log, 'other', None)


def test_preview_data_alias_does_not_override_real_source_field(tmp_path):
    store = Store(tmp_path)
    root = logged(store, 'render_tender_page', {'text': 'original', 'entries': [1, 2]})
    assert read_result(store, root, 'task', field='data.text')['value'] == 'original'
    assert read_result(store, root, 'task', field='data')['original_fields'] == ['text', 'entries']
    real = logged(store, 'custom', {'data': {'text': 'real nested data'}, 'text': 'outer'})
    assert read_result(store, real, 'task', field='data.text')['value'] == 'real nested data'


def test_pagination_stays_on_original_result_even_when_model_uses_read_log(tmp_path):
    store = Store(tmp_path)
    entries = [{'entry_id': str(index), 'title': '项目' * 50} for index in range(20)]
    root = logged(store, 'render_tender_page', {'entries': entries})
    first = read_result(store, root, 'task', field='entries', limit=20, token_limit=1000)
    assert 0 < len(first['value']) < 20
    assert first['next_offset'] == len(first['value'])
    wrapper = logged(store, 'read_tool_result', first, {'log_id': root, 'field': 'entries'})
    second = read_result(store, wrapper, 'task', field='value', offset=16, limit=4)
    assert second['original_result_id'] == root
    assert second['value'] == entries[16:20]
    assert second['field'] == 'entries'
    assert second['next_offset'] is None
    assert estimate_tokens(first) <= 1000


def test_legacy_nested_reads_resolve_to_original_field(tmp_path):
    store = Store(tmp_path)
    root = logged(store, 'render_tender_page', {'entries': list(range(20))})
    first = logged(store, 'read_tool_result', {'original_result_id': root, 'field': 'entries', 'value': list(range(20))}, {'log_id': root})
    second = logged(store, 'read_tool_result', {'original_result_id': first, 'field': 'value', 'value': list(range(8, 20))}, {'log_id': first})
    result = read_result(store, second, 'task', field='value', offset=16, limit=4)
    assert result['value'] == [16, 17, 18, 19]
    assert result['original_result_id'] == root


def test_large_subfield_returns_reference_instead_of_unbounded_dictionary(tmp_path):
    store = Store(tmp_path)
    root = logged(store, 'fetch_tender_detail', {'network': {'body': 'x' * 100000}})
    result = read_result(store, root, 'task', field='network', token_limit=500)
    assert result['value_omitted'] and result['available_fields'] == ['body']
    assert estimate_tokens(result) < 500


async def test_read_and_scheduler_projections_never_hide_page_identity(tmp_path):
    store = Store(tmp_path)
    context = ContextManager(store, SummaryModel(), 'task')
    read = {'original_result_id': 'root', 'field': 'entries', 'value': [1], 'offset': 0, 'next_offset': None}
    projected = await context.reduce_tool('read_tool_result', read, 'read-log')
    assert projected['full_result_id'] == 'root'
    assert projected['read_call_log_id'] == 'read-log'
    value = {'outputs': [{'step_id': 's1', 'log_id': 'page-log', 'result': {
        'data': {'announcement_id': 'page', 'text': 'body' * 10000}}}], 'steps': ['large plan'] * 100}
    result = await context.reduce_tool('execute_plan_steps', value, 'scheduler-log')
    assert result['outputs'][0]['announcement_id'] == 'page'
    assert result['outputs'][0]['result_log_id'] == 'page-log'
    assert 'steps' not in result
    assert estimate_tokens(result) <= context.tool_limit
