import asyncio
import json

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.storage import SessionBusy, Store
from bidding.tool_runtime import ToolRuntime
from model_fixtures import planning_response


async def test_two_turns_restore_history_after_store_reopened(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture")
    monkeypatch.setenv("MODEL_NAME", "fixture")
    sent = []
    def serve(request):
        if planned := planning_response(request, action='answer'):
            return planned
        sent.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Remembered answer"}}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bidding.agent.httpx.AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(serve)))
    store = Store(tmp_path)
    session_id = store.create_session()
    async with client_class() as client:
        task_id = store.create_turn(session_id, "First message")
        await Agent(Acquisition(store, client)).run("First message", task_id, session_id)
        store.engine.dispose()
        reopened = Store(tmp_path)
        task_id = reopened.create_turn(session_id, "Follow-up")
        await Agent(Acquisition(reopened, client)).run("Follow-up", task_id, session_id)
    assert len(sent[1][1:]) == 2
    assert '任务交接' in sent[1][1]['content']
    assert 'First message' in sent[1][1]['content'] and 'Remembered answer' in sent[1][1]['content']
    assert sent[1][-1]['content'] == 'Follow-up'
    assert len(reopened.conversation_messages(session_id)) == 4
    assert reopened.session(session_id)["active_task_id"] is None


def test_session_claim_prevents_overlapping_turns(tmp_path):
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, "One")
    with pytest.raises(SessionBusy):
        Store(tmp_path).create_turn(session_id, "Two")
    assert len(store.conversation_messages(session_id)) == 1
    store.release_session(session_id, task_id)
    store.create_turn(session_id, "Two")


def test_interrupted_history_kept_for_audit_not_model_context(tmp_path):
    store = Store(tmp_path)
    session_id = store.create_session()
    task_id = store.create_turn(session_id, "One")
    store.add_message(session_id, task_id, {"role": "assistant", "content": None, "tool_calls": [{"id": "unfinished"}]})
    log_id = store.start_tool("download", {"url": "https://example.org"}, "agent", task_id, session_id)
    store.recover_tasks()
    assert store.history(session_id) == []
    assert len(store.conversation_messages(session_id)) == 2
    assert store.logs(session_id=session_id)[0]["id"] == log_id
    assert store.logs(session_id=session_id)[0]["status"] == "interrupted"
    assert store.session(session_id)["active_task_id"] is None


@pytest.mark.parametrize("fails", [False, True])
async def test_tool_parameters_result_error_and_timing_are_durable(tmp_path, fails):
    store = Store(tmp_path)
    async def handler(**kwargs):
        assert store.logs()[0]["status"] == "running"
        if fails:
            raise ValueError("fixture failure")
        return {"files": ["bid.pdf"]}
    runtime = ToolRuntime(store, {"fetch": handler})
    if fails:
        with pytest.raises(ValueError):
            await runtime.call("fetch", {"url": "https://example.org", "token": "hidden"}, source="mcp")
    else:
        await runtime.call("fetch", {"url": "https://example.org", "token": "hidden"}, source="api")
    log = Store(tmp_path).logs()[0]
    assert log["arguments"] == {"url": "https://example.org", "token": "[REDACTED]"}
    assert log["duration_ms"] >= 0
    assert log["finished_at"]
    assert log["status"] == ("failed" if fails else "completed")
    if fails:
        assert log['result']['error'] == 'ValueError'
        assert log['result']['failure']['retryable'] is False
    else:
        assert log['result'] == {'files': ['bid.pdf']}


async def test_cancelled_tool_is_logged(tmp_path):
    store = Store(tmp_path)
    async def handler():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await ToolRuntime(store, {"cancel": handler}).call("cancel", {})
    assert store.logs()[0]["status"] == "failed"
