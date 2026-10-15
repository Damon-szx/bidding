import json
from datetime import datetime, timedelta

import httpx
import pytest

from bidding.acquisition import Acquisition, GenericHTMLAdapter
from bidding.agent import Agent
from bidding.batch import BatchManager
from bidding.eligibility import TZ, validate_assessment, validate_checkpoint
from bidding.presentation import partial_answer
from bidding.storage import Store
from model_fixtures import planning_response

CRITERIA = [{'id': 'industry', 'description': '用户要求的行业'}, {'id': 'type', 'description': '正式招标而非中标结果'}]
SPEC = {'criteria': CRITERIA, 'expected_count': 3, 'action': 'download', 'file_goal': 'tender_document'}


def page(title='医疗设备采购招标公告'):
    return {'source_url': 'https://example.org/tender', 'title': title, 'page_type': 'detail',
        'text': title + ' 项目采购医疗设备，公告日期2026-10-09，投标截止2026-11-03 08:30:00。',
        'attachments': [{'name': '完整招标文件.pdf', 'url': 'https://example.org/tender.pdf'}]}


def judgments(value):
    return [{'criterion_id': item['id'], 'verdict': 'match', 'reason': '原文符合该条件', 'quotes': [value['title']]} for item in CRITERIA]


def assessment(value):
    return validate_assessment(value, SPEC, judgments(value), '已核实',
        [{'url': value['attachments'][0]['url'], 'document_type': 'tender_document', 'quote': '完整招标文件.pdf'}])


def test_model_evidence_works_for_arbitrary_industry_not_keyword_list():
    value = page()
    assert assessment(value)['eligible']
    checked = judgments(value)
    checked[0].update(verdict='unknown', quotes=[], reason='证据不足')
    assert not validate_assessment(value, SPEC, checked, '不足')['eligible']


def test_listing_project_assessment_uses_only_selected_entry_evidence():
    listing = {'page_type': 'listing', 'source_url': 'https://example.org/list', 'entries': [
        {'entry_id': 'first', 'title': '医疗设备采购招标公告', 'url': 'https://example.org/first', 'published_at': '2026-10-10'},
        {'entry_id': 'second', 'title': '其他项目', 'url': 'https://example.org/second'}]}
    spec = {**SPEC, 'action': 'research', 'file_goal': 'none'}
    checked = judgments(listing['entries'][0])
    result = validate_assessment(listing, spec, checked, '符合', entry_id='first')
    assert result['eligible'] and result['source_url'].endswith('/first')
    assert result['evidence_kind'] == 'listing_entry'
    checked[0]['quotes'] = ['其他项目']
    with pytest.raises(ValueError, match='not present'):
        validate_assessment(listing, spec, checked, '不能借用其他行', entry_id='first')
    with pytest.raises(ValueError, match='only available'):
        validate_assessment(listing, SPEC, checked, '下载仍需详情', entry_id='first')
    with pytest.raises(ValueError, match='observed project URL'):
        validate_assessment(listing, spec, checked, 'missing', entry_id='missing')


def test_fabricated_evidence_missing_criteria_search_snippets_rejected():
    value = page()
    checked = judgments(value)
    checked[0]['quotes'] = ['不存在的公司资质']
    with pytest.raises(ValueError, match='not present'):
        validate_assessment(value, SPEC, checked, '匹配')
    with pytest.raises(ValueError, match='exactly once'):
        validate_assessment(value, SPEC, [], '匹配')
    with pytest.raises(ValueError, match='detail'):
        validate_assessment({**value, 'page_type': 'listing'}, SPEC, judgments(value), '匹配')


def test_wrong_source_quote_returns_exact_repair_context_not_a_fake_match():
    from bidding.eligibility import AssessmentEvidenceError
    value = {**page(), 'id': 'detail', 'published_at': '2026-10-09'}
    checked = judgments(value)
    checked[0]['quotes'] = ['列表整行 项目编号 状态 2026-10-09']
    with pytest.raises(AssessmentEvidenceError) as raised:
        validate_assessment(value, SPEC, checked, '匹配')
    repair = raised.value.evidence_repair
    assert repair['criterion_id'] == 'industry'
    assert repair['invalid_quotes'] == checked[0]['quotes']
    assert repair['source_fields']['published_at'] == '2026-10-09'
    assert repair['source_text_excerpt'] == value['text']
    assert repair['observed_attachments'][0]['url'] == value['attachments'][0]['url']
    assert repair['text_complete'] is True


def test_announcements_cannot_replace_tenders_without_explicit_fallback_contract():
    value = page()
    documents = [{'url': value['attachments'][0]['url'], 'document_type': 'announcement_document', 'quote': '完整招标文件.pdf'}]
    with pytest.raises(ValueError, match='not authorized'):
        validate_assessment(value, SPEC, judgments(value), '公告代替', documents)
    assert validate_assessment(value, {**SPEC, 'fallback_file_goal': 'announcement'}, judgments(value), '允许回退', documents)['files'][0]['document_type'] == 'announcement_document'


def test_detail_assessment_only_recovers_entry_with_exact_navigation_provenance():
    value = {**page(), 'navigation': {'parent_announcement_id': 'listing', 'entry_id': 'first'}}
    result = validate_assessment(value, SPEC, judgments(value), '匹配', entry_id='first')
    assert result['evidence_kind'] == 'detail' and result['entry_id'] is None
    with pytest.raises(ValueError, match='must omit entry_id'):
        validate_assessment(value, SPEC, judgments(value), '匹配', entry_id='other')
    with pytest.raises(ValueError, match='must omit entry_id'):
        validate_assessment(page(), SPEC, judgments(value), '匹配', entry_id='first')


def test_changed_page_and_expired_assessment_require_model_reassessment():
    value = page()
    checked = assessment(value)
    with pytest.raises(ValueError, match='变化'):
        validate_checkpoint({**value, 'title': '中标结果公告'}, checked)
    checked['valid_until'] = (datetime.now(TZ) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match='有效期'):
        validate_checkpoint(value, checked)


def test_failure_answer_never_includes_raw_unverified_candidates():
    result = partial_answer({'spec': SPEC, 'candidates': [{'title': '供水中标公告', 'source_url': 'https://example.org/water'}]})
    assert '供水' not in result and '0 个' in result
    assert '尚未完成文件下载' in result


def test_static_list_anchors_are_discovered_without_table():
    parsed = GenericHTMLAdapter().parse('https://example.org/zbgg/index.jhtml', '<title>公告列表</title><a href="/zbgg/123.jhtml">电网设备采购项目招标公告</a>')
    assert parsed['page_type'] == 'listing'


async def test_agent_uses_model_judgments_and_schedules_three_targets(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    store = Store(tmp_path)
    values = []
    for index in range(3):
        value = {**page(f'医疗设备项目{index}招标公告'), 'source_url': f'https://example.org/{index}'}
        value['announcement_id'] = store.save('announcement', value)
        values.append(value)
    responses = [{'role': 'assistant', 'tool_calls': [{'id': f'read{i}', 'type': 'function', 'function': {'name': 'fetch_tender_detail', 'arguments': json.dumps({'url': value['source_url']})}} for i, value in enumerate(values)]},
        {'role': 'assistant', 'tool_calls': [{'id': f'check{i}', 'type': 'function', 'function': {'name': 'record_project_assessment', 'arguments': json.dumps({'announcement_id': value['announcement_id'], 'judgments': judgments(value), 'summary': '原文符合条件', 'files': [{'url': 'https://example.org/tender.pdf', 'document_type': 'tender_document', 'quote': '完整招标文件.pdf'}]})}} for i, value in enumerate(values)]},
        {'role': 'assistant', 'content': '整理完成'}]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(lambda request: planning_response(request, action='download', file_goal='tender_document', expected_count=3, criteria=CRITERIA) or httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]}))))
    acquisition = Acquisition(store, client_class())
    async def fetch(url):
        return next(value for value in values if value['source_url'] == url)
    acquisition.fetch_tender_detail = fetch
    manager = BatchManager(store)
    manager.closed = True
    await Agent(acquisition, manager).run('找三个医疗项目并下载完整标书', 'task')
    task = store.get('task')
    assert task['status'] == 'scheduled', task
    batch = store.get(task['batch_id'])
    assert len(batch['contract']['targets']) == 3
    assert batch['contract']['require_documents']
    assert len(store.list_records('assessment')) == 3
    assert '尚未完成下载' in task['answer']
    await acquisition.close()


@pytest.mark.parametrize('final_review', ['disabled', 'pass', 'budget'])
async def test_project_only_agent_verifies_five_listing_entries_without_navigation_or_files(tmp_path, monkeypatch, final_review):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false' if final_review == 'disabled' else 'true')
    async def review(self, proposed_answer, phase='final_result'):
        from bidding.model import BudgetExceeded
        assert phase == 'final_result'
        assert '已依据公告原文核实匹配项目：5 个' in proposed_answer
        if final_review == 'budget':
            raise BudgetExceeded('fixture budget')
        return {'verdict': 'pass', 'summary': '符合用户目标', 'issues': [], 'next_actions': []}
    monkeypatch.setattr('bidding.agent.Specialists.review', review)
    store = Store(tmp_path)
    entries = [{'entry_id': str(index), 'title': f'医疗设备项目{index}招标公告',
        'url': f'https://example.org/{index}', 'published_at': '2026-10-10'} for index in range(5)]
    listing = {'page_type': 'listing', 'source_url': 'https://example.org/list', 'entries': entries, 'attachments': [], 'text': '公告列表'}
    listing['announcement_id'] = store.save('announcement', listing)
    responses = [
        {'role': 'assistant', 'tool_calls': [{'id': 'list', 'type': 'function', 'function': {
            'name': 'fetch_tender_detail', 'arguments': json.dumps({'url': listing['source_url']})}}]},
        {'role': 'assistant', 'tool_calls': [{'id': 'check' + entry['entry_id'], 'type': 'function', 'function': {
            'name': 'record_project_assessment', 'arguments': json.dumps({'announcement_id': listing['announcement_id'],
                'entry_id': entry['entry_id'], 'judgments': judgments(entry), 'summary': '正式项目，原文符合条件'})}} for entry in entries]},
        {'role': 'assistant', 'content': '已完成五个项目核验'}]
    client_class = httpx.AsyncClient
    def respond(request):
        planned = planning_response(request, expected_count=5, criteria=CRITERIA)
        if planned:
            return planned
        body = json.loads(request.content)
        assert len(responses) > 1, 'Verified goal must finalize without another main Agent call'
        names = {item['function']['name'] for item in body.get('tools', [])}
        assert not names & {'discover_tender_files', 'inspect_document_access', 'download_tender_file', 'plan_acquisition'}
        return httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]})
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(respond)))
    acquisition = Acquisition(store, client_class())
    async def fetch(url):
        return listing
    acquisition.fetch_tender_detail = fetch
    try:
        await Agent(acquisition).run('找五个正式项目，不下载', 'task')
        task = store.get('task')
        assert task['status'] == ('partial_completed' if final_review == 'budget' else 'completed'), task
        if final_review == 'budget':
            assert '最终独立复核未完成' in task['answer']
        assert len(task['qualified_projects']) == 5
        assert len(store.list_records('assessment')) == 5
        assert not task.get('batch_id')
        assert {log['tool'] for log in store.logs(task_id='task')} == {'fetch_tender_detail', 'record_project_assessment'}
    finally:
        await acquisition.close()


def test_batch_refuses_unscoped_or_changed_assessment(tmp_path):
    store = Store(tmp_path)
    manager = BatchManager(store)
    manager.closed = True
    value = page()
    value['announcement_id'] = store.save('announcement', value)
    assessment_id = store.save('assessment', {**assessment(value), 'task_id': 'task', 'announcement_id': value['announcement_id']})
    selected = {**value, 'assessment_id': assessment_id}
    batch = manager.create(value['announcement_id'], 'count', 1, '下载', 'task', None, targets=[selected], criteria=SPEC, require_documents=True)
    assert batch['contract']['requested_count'] == 3
    with pytest.raises(ValueError, match='核验记录'):
        manager.create(value['announcement_id'], 'count', 1, '下载', 'another', None, targets=[selected], criteria=SPEC)
    store.save('announcement', {**value, 'title': '变化'}, value['announcement_id'])
    with pytest.raises(ValueError, match='变化'):
        manager.create(value['announcement_id'], 'count', 1, '下载', 'task', None, targets=[selected], criteria=SPEC)
