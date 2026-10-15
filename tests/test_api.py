import httpx

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.storage import Store


async def test_chat_session_history_failure_and_direct_tool_logs(tmp_path, monkeypatch):
    from bidding import api
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    store = Store(tmp_path)
    async with httpx.AsyncClient() as network:
        acquisition = Acquisition(store, network)
        monkeypatch.setattr(api, "store", store)
        monkeypatch.setattr(api, "agent", Agent(acquisition))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            home = await client.get("/")
            assert home.status_code == 200 and 'id="composer"' in home.text
            first = await client.post("/chat", json={"message": "Find a tender"})
            assert first.status_code == 202
            session_id, task_id = first.json()["session_id"], first.json()["task_id"]
            assert (await client.get("/sessions")).json()["sessions"][0]["id"] == session_id
            assert (await client.get("/sessions/" + session_id)).json()["title"] == "Find a tender"
            assert (await client.get("/tasks/" + task_id)).json()["status"] == "failed"
            assert store.session(session_id)["active_task_id"] is None
            second = await client.post("/chat", json={"session_id": session_id, "message": "Follow-up"})
            assert second.status_code == 202
            history = (await client.get("/sessions/" + session_id + "/messages")).json()["messages"]
            assert len(history) == 2
            assert history[0]["payload"]["content"] == "Find a tender"
            invalid = await client.post("/tools/discover_tender_files", json={"arguments": {"announcement_id": "missing"}})
            assert invalid.status_code == 422
            logs = (await client.get("/tool-calls")).json()["tool_calls"]
            assert logs[0]["arguments"] == {"announcement_id": "missing"}
            assert logs[0]["status"] == "failed"
            assert logs[0]["source"] == "api"
            assert (await client.post("/chat", json={"session_id": "unknown", "message": "Test"})).status_code == 404
            store.create_turn(session_id, "Busy task")
            assert (await client.post("/chat", json={"session_id": session_id, "message": "Overlapping"})).status_code == 409


def test_mysql_schema_supports_large_payloads_and_json():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable
    from bidding.storage import records, messages, tool_calls
    dialect = mysql.dialect()
    assert "LONGTEXT" in str(CreateTable(records).compile(dialect=dialect))
    assert "JSON" in str(CreateTable(messages).compile(dialect=dialect))
    assert "JSON" in str(CreateTable(tool_calls).compile(dialect=dialect))
