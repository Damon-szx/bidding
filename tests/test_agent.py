import json

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.search import search_tenders
from bidding.storage import Store
from model_fixtures import planning_response


async def test_missing_search_provider_is_explicit(monkeypatch):
    monkeypatch.delenv("SEARCH_MCP_URL", raising=False)
    try:
        await search_tenders("software tender")
    except ValueError as exc:
        assert "SEARCH_MCP_URL" in str(exc)
    else:
        raise AssertionError("Search must not invent results")


async def test_unverified_tender_download_does_not_offer_queue_tool(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    requests = []
    def serve(request):
        planned = planning_response(request, action='download', file_goal='tender_document')
        if planned:
            return planned
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
            'content': '未取得可下载的正式标书。'}}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs:
        client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    async with client_class() as client:
        await Agent(Acquisition(store, client)).run('下载指定项目标书', 'download')
    assert requests
    assert 'plan_acquisition' not in {item['function']['name'] for item in requests[0]['tools']}
    assert not store.get('download').get('batch_id')


async def test_site_rejection_is_shown_in_final_download_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('REFLECTION_ENABLED', 'false')
    responses = [
        {'role': 'assistant', 'tool_calls': [{'id': 'inspect', 'type': 'function', 'function': {
            'name': 'inspect_document_access', 'arguments': json.dumps({'announcement_id': 'page', 'entry_id': 'entry'})}}]},
        {'role': 'assistant', 'content': '请提供具体项目页面或明确下载范围。'},
    ]
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(
        lambda request: planning_response(request, action='download', file_goal='tender_document') or
        httpx.Response(200, json={'choices': [{'message': responses.pop(0)}]}))))
    store = Store(tmp_path)
    store.save('announcement', {'title': '指定项目', 'source_url': 'https://example.org/tender'}, 'page')
    async with client_class() as client:
        agent = Agent(Acquisition(store, client))
        async def inspect(**arguments):
            return {'announcement_id': 'observed', 'page_type': 'detail', 'document_access_status': 'blocked',
                'access_error': {'code': 'WEBSITE_REJECTED_DOCUMENT_ACCESS', 'message': '不在文件获取时间内!'}}
        agent.runtime.handlers['inspect_document_access'] = inspect
        await agent.run('下载招标文件', 'blocked-download')
    result = store.get('blocked-download')
    assert result['status'] == 'failed'
    assert '不在文件获取时间内!' in result['answer']
    assert '请提供具体项目页面' not in result['answer']
    assert result['document_access_barriers'][0]['log_id']
    inspection = next(log for log in store.logs('blocked-download') if log['tool'] == 'inspect_document_access')
    assert inspection['result']['access_error']['message'] in result['answer']


async def test_conversation_uses_tools_and_records_results(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    monkeypatch.setenv("MODEL_NAME", "fixture-model")
    monkeypatch.setenv("MODEL_BASE_URL", "https://model.example/v1")
    responses = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "fetch_tender_detail", "arguments": json.dumps({"url": "https://example.org/bid"})}}]},
        {"role": "assistant", "content": "Announcement read; no public attachment found."},
    ]
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        if planned := planning_response(request):
            return planned
        return httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    async with client_class() as client:
        acquisition = Acquisition(store, client)
        async def fetch(url):
            return {"source_url": url, "attachments": [], "announcement_id": "fixture"}
        monkeypatch.setattr(acquisition, "fetch_tender_detail", fetch)
        await Agent(acquisition).run("Read https://example.org/bid", "task-1")
    result = store.get("task-1")
    assert result["status"] == "completed"
    assert result["events"][0]["tool"] == "fetch_tender_detail"
    assert requests[2]["messages"][-1]["role"] == "tool"
    assert requests[2]["messages"][-1]["tool_call_id"] == "call_1"
    assert result["progress"] == "核对公告原文"
    assert "当前北京时间日期" in requests[1]["messages"][0]["content"]


async def test_repeated_tool_call_is_logged_but_not_executed(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    responses = [{"role": "assistant", "content": "Let me inspect", "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {"name": "fetch_tender_detail", "arguments": json.dumps({"url": "https://example.org/bid"})}}]} for i in range(3)]
    responses.append({"role": "assistant", "content": "已通过 fetch_tender_detail 核对公告。"})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(lambda request: planning_response(request) or httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]}))))
    store = Store(tmp_path)
    executed = []
    async with client_class() as client:
        acquisition = Acquisition(store, client)
        async def fetch(url):
            executed.append(url)
            return {"source_url": url}
        monkeypatch.setattr(acquisition, "fetch_tender_detail", fetch)
        await Agent(acquisition).run("查看项目", "repeat-task")
    assert len(executed) == 2
    assert len(store.logs(task_id="repeat-task")) == 3
    assert '相同操作' in store.get("repeat-task")["events"][-1]["output"]["data"]["message"]
    assert "fetch_tender_detail" not in store.get("repeat-task")["answer"]


async def test_unrelated_question_does_not_trigger_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    monkeypatch.setenv("MODEL_NAME", "fixture-model")
    client_class = httpx.AsyncClient
    response = {"choices": [{"message": {"role": "assistant", "content": "Supplier scoring is not implemented yet."}}]}
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(lambda request: planning_response(request, action='answer') or httpx.Response(200, json=response))))
    store = Store(tmp_path)
    async with client_class() as client:
        await Agent(Acquisition(store, client)).run("Score this supplier", "task-2")
    assert store.get("task-2")["events"] == []


@pytest.mark.parametrize("rounds,searches", [(2, 6), (16, 1)])
async def test_last_round_is_reserved_for_final_answer(tmp_path, monkeypatch, rounds, searches):
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    monkeypatch.setenv("AGENT_MAX_ROUNDS", str(rounds))
    monkeypatch.setenv("AGENT_MAX_SEARCH_CALLS", str(searches))
    requests = []
    responses = [
        {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "search_tenders", "arguments": '{"query":"电力"}'}}]},
        {"role": "assistant", "content": "已找到电力项目，来源尚待核对。"},
    ]
    def serve(request):
        requests.append(json.loads(request.content))
        if planned := planning_response(request):
            return planned
        return httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    async with client_class() as client:
        agent = Agent(Acquisition(store, client))
        async def search(query):
            return {"query": query, "candidates": [{"title": "电力项目", "source_url": "https://example.org/bid"}]}
        agent.runtime.handlers["search_tenders"] = search
        await agent.run("查询电力项目", "final-round")
    assert not requests[-1].get("tools") if rounds == 2 else all(item['function']['name'] != 'search_tenders' for item in requests[-1]['tools'])
    assert store.get("final-round")["status"] == "completed"


async def test_original_field_navigation_and_keyword_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    store = Store(tmp_path)
    log_id = store.start_tool("fetch_tender_detail", {}, "agent", "reading", None, "old-call")
    store.finish_tool(log_id, {"text": "导航" * 3000 + "提交截止时间：2026年10月20日", "source_url": "https://example.org"}, 0)
    responses = []
    for index, args in enumerate([{"log_id": log_id}, {"log_id": log_id, "field": "data.text"}, {"log_id": log_id, "field": "text", "contains": "截止", "limit": 300}]):
        responses.append({"role": "assistant", "tool_calls": [{"id": f"read{index}", "type": "function", "function": {"name": "read_tool_result", "arguments": json.dumps(args)}}]})
    responses.append({"role": "assistant", "content": "已找到截止时间。"})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(lambda request: planning_response(request) or httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]}))))
    async with client_class() as client:
        await Agent(Acquisition(store, client)).run("查看截止时间", "reading")
    outputs = [event["output"]["data"] for event in store.get("reading")["events"]]
    assert outputs[0]["original_fields"] == ["text", "source_url"]
    assert outputs[1]['field'] == 'text'
    assert outputs[1]['original_result_id'] == log_id
    assert "2026年10月20日" in outputs[2]["value"]
