import asyncio
import json

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.model import ModelClient, BudgetExceeded
from bidding.storage import Store
from test_context import MEMORY
from model_fixtures import planning_response


@pytest.fixture(autouse=True)
def hard_budget_default(monkeypatch):
    monkeypatch.setenv('TASK_TOKEN_BUDGET_MODE', 'hard')


async def test_soft_budget_continues_but_never_crosses_hard_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    monkeypatch.setenv('TASK_TOKEN_BUDGET_MODE', 'soft')
    monkeypatch.setenv('TASK_TOKEN_BUDGET', '300000')
    monkeypatch.setenv('TASK_TOKEN_HARD_LIMIT', '1000000')
    store = Store(tmp_path)
    requests = []
    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        model = ModelClient(client, store, 'task')
        model.charged = 299950
        await model.call([{'role': 'user', 'content': 'Question'}], max_tokens=100)
        assert model.charged > model.soft_budget and model.budget_extension == 0
        model.charged = 999950
        with pytest.raises(BudgetExceeded):
            await model.call([{'role': 'user', 'content': 'Question'}], max_tokens=100)
    assert len(requests) == 1
    assert store.context_logs()[0]['payload']['status'] == 'review_required'


async def test_agent_summarizes_then_reads_original_and_accounts_all_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture")
    monkeypatch.setenv("MODEL_NAME", "fixture")
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.deepseek.com")
    original = {"announcement_id": "ann1", "text": "OriginalText" * 3000}
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, "Read the exact text")
    requests, main_calls = [], 0
    def serve(request):
        nonlocal main_calls
        body = json.loads(request.content)
        requests.append(body)
        if planned := planning_response(request):
            return planned
        if not body.get("tools"):
            answer = {"role": "assistant", "content": json.dumps(MEMORY)}
        else:
            main_calls += 1
            if main_calls == 1:
                name, arguments = "fetch_tender_detail", {"url": "https://example.org/bid"}
            elif main_calls == 2:
                projected = json.loads(body["messages"][-1]["content"])
                assert len(body["messages"][-1]["content"]) < len(json.dumps(original))
                name, arguments = "read_tool_result", {"log_id": projected["full_result_id"], "field": "text", "offset": 5, "limit": 20}
            else:
                projected = json.loads(body["messages"][-1]["content"])
                assert projected["data"]["value"] == original["text"][5:25]
                answer = {"role": "assistant", "content": "Exact text retrieved"}
            if main_calls <= 2:
                answer = {"role": "assistant", "content": None, "tool_calls": [{"id": f"call{main_calls}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        return httpx.Response(200, json={"choices": [{"message": answer}], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    async with client_class() as client:
        acquisition = Acquisition(store, client)
        async def fetch(url):
            return original
        monkeypatch.setattr(acquisition, "fetch_tender_detail", fetch)
        await Agent(acquisition).run("Read the exact text", task_id, session_id)
    task = store.get(task_id)
    assert task["status"] == "completed", task
    assert store.logs(task_id=task_id)[0]["result"] == original
    assert task["usage"]["input_tokens"] == len(requests) * 100
    assert task["usage"]["output_tokens"] == len(requests) * 20
    assert all(request["thinking"] == {"type": "disabled"} for request in requests)
    assert any(log["purpose"] == "tool_summary" for log in store.model_logs(task_id=task_id))
    assert any(original["text"] in (row["payload"].get("content") or "") for row in store.conversation_messages(session_id))


async def test_budget_stops_before_network_call_and_releases_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture")
    monkeypatch.setenv("MODEL_NAME", "fixture")
    monkeypatch.setenv("TASK_TOKEN_BUDGET", "10")
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, "Question")
    async with httpx.AsyncClient() as client:
        await Agent(Acquisition(store, client)).run("Question", task_id, session_id)
    assert store.get(task_id)["status"] == "budget_exceeded"
    assert store.model_logs(task_id=task_id) == []
    assert store.session(session_id)["active_task_id"] is None


async def test_next_request_reservation_explains_early_budget_stop(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    store = Store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('Must stop before network'))) as client:
        model = ModelClient(client, store, 'task')
        model.budget, model.charged = 1000, 950
        with pytest.raises(BudgetExceeded, match='cannot fit next'):
            await model.call([{'role': 'user', 'content': 'Question'}], max_tokens=100)
        usage = model.usage()
    assert usage['remaining_tokens'] == 50
    assert usage['last_request']['reserved_output_tokens'] == 100
    assert usage['budget_consumed_tokens'] == 950
    assert not store.model_logs()
    assert store.context_logs()[0]['payload']['reason'] == 'remaining_budget_cannot_fit_next_request'


async def test_task_timeout_logs_model_cancellation(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture")
    monkeypatch.setenv("MODEL_NAME", "fixture")
    monkeypatch.setenv("TASK_TIMEOUT_SECONDS", "0.02")
    async def serve(request):
        await asyncio.sleep(1)
        return httpx.Response(200)
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, "Question")
    async with client_class() as client:
        await Agent(Acquisition(store, client)).run("Question", task_id, session_id)
    assert store.get(task_id)["status"] == "timed_out"
    assert store.model_logs(task_id=task_id)[0]["status"] == "failed"
    assert store.get(task_id)["usage"]["unknown_usage_calls"] == 1
    assert store.session(session_id)["active_task_id"] is None


async def test_small_budget_shortfall_gets_one_bounded_extension(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    monkeypatch.setenv('TASK_TOKEN_GRACE_TOKENS', '15000')
    store = Store(tmp_path)
    requests = []
    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        model = ModelClient(client, store, 'task')
        model.charged = 299950
        await model.call([{'role': 'user', 'content': 'Question'}], max_tokens=100)
        assert model.budget == 315000
        assert model.usage()['budget_extension_tokens'] == 15000
        model.charged = 314950
        with pytest.raises(BudgetExceeded, match='cannot fit next'):
            await model.call([{'role': 'user', 'content': 'Question'}], max_tokens=100)
    assert len(requests) == 1
    assert store.context_logs()[0]['payload']['status'] == 'extended'


async def test_grace_never_bypasses_context_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('No network'))) as client:
        model = ModelClient(client, Store(tmp_path), 'task')
        model.charged, model.hard_limit = 299950, 50
        with pytest.raises(BudgetExceeded, match='Context exceeds'):
            await model.call([{'role': 'user', 'content': 'Question'}], max_tokens=100)
        assert model.budget_extension == 0


@pytest.mark.parametrize("status", [200, 500])
async def test_absent_usage_never_counted_as_zero_or_twice(tmp_path, monkeypatch, status):
    monkeypatch.setenv('MODEL_MAX_ATTEMPTS', '1')
    monkeypatch.setenv("MODEL_API_KEY", "fixture")
    monkeypatch.setenv("MODEL_NAME", "fixture")
    store = Store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status,
            json={"choices": [{"message": {"role": "assistant", "content": "Answer"}}]}))) as client:
        model = ModelClient(client, store, "task")
        if status == 200:
            await model.call([{"role": "user", "content": "Question"}])
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await model.call([{"role": "user", "content": "Question"}])
    assert model.unknown_calls == 1
    assert model.charged > 0
    assert store.model_logs()[0]["usage"] is None
