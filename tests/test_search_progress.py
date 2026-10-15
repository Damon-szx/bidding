import time
from types import SimpleNamespace

import pytest

from bidding.search_progress import SearchProgress
from bidding.storage import Store
from bidding.specialists import Specialists
from bidding.agent import TOOLS
from test_specialists import ScriptModel, call


def progress(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_MAX_SEARCH_CALLS', '1')
    monkeypatch.setenv('SEARCH_EXTENSION_CALLS', '1')
    state = {'spec': {'expected_count': 10}, 'candidates': []}
    return SearchProgress(Store(tmp_path), state, 'task', None, SimpleNamespace(budget=150000, charged=0), time.monotonic() + 600)


def test_prompt_candidate_set_is_bounded_without_losing_durable_data(tmp_path, monkeypatch):
    from bidding.model import estimate_tokens
    p = progress(tmp_path, monkeypatch)
    p.state['candidates'] = [{'source_url': f'https://example.org/{i}', 'title': '公告' * 500,
        'snippet': '无关长正文' * 2000} for i in range(100)]
    p.data['decisions'] = {item['source_url']: {'source_url': item['source_url'],
        'verdict': 'excluded', 'reason': '很长的排除理由' * 100} for item in p.state['candidates']}
    projected = p.candidate_view()
    assert len(projected['items']) <= 12 and projected['partial']
    assert estimate_tokens(projected) < 4000
    assert estimate_tokens(p.model_view()) < 4000
    assert len(p.state['candidates']) == len(p.data['decisions']) == 100


def round_result(p, query, verdict='worth_verifying'):
    p.consume(query)
    url = 'https://example.org/' + query
    p.state['candidates'].append({'source_url': url})
    p.checkpoint([{'source_url': url, 'verdict': verdict, 'reason': '模型初筛判断'}], '数量不足', 'verify')
    return url


def test_unverified_candidates_block_extension(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    url = round_result(p, 'q1')
    with pytest.raises(ValueError, match='处理现有'):
        p.extend('缺项目', '新来源', '数量不足')
    assert not p.can_search()
    p.state['qualified_projects'] = [{'source_url': url}]
    assert p.extend('缺项目', '新来源', '已核验但数量不足')['extensions'] == 1


def test_three_extensions_max_and_no_repeated_queries(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    for index in range(4):
        url = round_result(p, 'q' + str(index))
        p.state.setdefault('qualified_projects', []).append({'source_url': url})
        if index < 3:
            p.extend('缺项目', 'source' + str(index), '已有候选均已处理')
    with pytest.raises(ValueError, match='上限'):
        p.extend('缺项目', 'source4', '已有候选均已处理')
    assert p.data['limit'] == 4
    p.data['limit'] += 1
    with pytest.raises(ValueError, match='已执行'):
        p.consume('q0')


def test_no_progress_needs_reflection_and_short_budget_denied(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    round_result(p, 'q1', 'excluded')
    p.extend('缺项目', 'new source1', '线索均不匹配')
    round_result(p, 'q2', 'excluded')
    with pytest.raises(ValueError, match='反思'):
        p.extend('缺项目', 'new source2', '线索均不匹配')
    p.data['approved_strategy'] = 'new source2'
    p.model.charged = 149000
    with pytest.raises(ValueError, match='Token'):
        p.extend('缺项目', 'new source2', '线索均不匹配')
    p.model.charged = 0
    assert p.extend('缺项目', 'new source2', '评审通过的新路径')['extensions'] == 2
    assert p.data['no_progress_streak'] == 0


def test_matching_count_stops_search_not_download_goal(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    p.state['spec']['expected_count'] = 1
    url = round_result(p, 'q1')
    p.state['qualified_projects'] = [{'source_url': url}]
    assert not p.can_search()
    with pytest.raises(ValueError, match='已达到'):
        p.extend('文件未下载', 'more queries', '缺文件')


def test_checkpoint_rejects_invented_or_omitted_candidates(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    p.consume('q')
    p.state['candidates'] = [{'source_url': 'https://example.org/a'}]
    with pytest.raises(ValueError, match='整理所有'):
        p.checkpoint([], '未知', 'search')
    with pytest.raises(ValueError, match='observed'):
        p.checkpoint([{'source_url': 'https://invented.org', 'verdict': 'excluded', 'reason': '不相关'}], '缺项目', 'search')


@pytest.mark.parametrize('repair', [{'content': '已有线索待核验'}, ValueError('Model output limit reached; response is incomplete')])
async def test_truncated_specialist_partial_no_repeated_search(tmp_path, repair):
    model = ScriptModel([call('search_tenders', {'query': 'q'}), ValueError('Model output limit reached; response is incomplete'), repair])
    store, calls, state = Store(tmp_path), [], {}
    async def invoke(name, args, ident):
        calls.append(name)
        log = store.start_tool(name, args, 'agent', 'task')
        result = {'candidates': [{'source_url': 'https://example.org/a', 'title': '项目'}]}
        store.finish_tool(log, result, 1)
        return result, log
    agents = Specialists(model, store, 'task', None, state, TOOLS, invoke)
    result = await agents.delegate('search', '查找项目')
    assert result['status'] == 'partial_completed'
    assert len(result['evidence_ids']) == 1 and calls == ['search_tenders']
    assert state['specialist_checkpoints'][result['agent_run_id']] == result
    assert model.calls[-1][1]['purpose'] == 'specialist_handoff_repair'
    assert not model.calls[-1][1].get('tools')


async def test_subagent_search_removed_after_quota_exhausted(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    p.consume('q1')
    p.checkpoint([], '未发现线索', 'search')
    model = ScriptModel([{'content': '额度不足，交回主 Agent 评估补搜'}])
    agents = Specialists(model, p.store, 'task', None, p.state, TOOLS, None)
    agents.search_progress = p
    await agents.delegate('search', '继续查找')
    assert all(item['function']['name'] != 'search_tenders' for item in model.calls[0][1]['tools'])


def test_large_candidate_check_can_be_chunked(tmp_path, monkeypatch):
    p = progress(tmp_path, monkeypatch)
    p.consume('q')
    p.state['candidates'] = [{'source_url': 'https://example.org/' + str(index)} for index in range(30)]
    for index in range(30):
        result = p.checkpoint([{'source_url': 'https://example.org/' + str(index), 'verdict': 'excluded', 'reason': '初筛不相关'}], '仍缺项目', 'search')
        assert result['checked_calls'] == (1 if index == 29 else 0)
    assert not result['unclassified_candidates']
