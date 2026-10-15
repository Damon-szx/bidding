from bidding.context import ContextManager
from bidding.storage import Store
from test_context import SummaryModel


def test_unfinished_turn_restores_goal_and_logs_without_broken_tool_messages(tmp_path):
    store = Store(tmp_path)
    session_id = store.create_session()
    failed_id = store.create_turn(session_id, "Download the second project")
    store.add_message(session_id, failed_id, {"role": "assistant", "tool_calls": [{"id": "unfinished"}], "content": None})
    log_id = store.start_tool("download_tender_file", {"announcement_id": "ann2"}, "agent", failed_id, session_id)
    store.finish_tool(log_id, {"error": "Timeout"}, 10, "Timeout")
    store.save("task", {"status": "timed_out", "message": "Download the second project", "error": "Deadline reached"}, failed_id)
    store.release_session(session_id, failed_id)
    next_id = store.create_turn(session_id, "Retry the previous task")
    context = ContextManager(store, SummaryModel(), next_id, session_id)
    context.restore("Retry the previous task")
    assert "Download the second project" in context.frames[0]["message"]["content"]
    assert log_id in context.frames[0]["message"]["content"]
    assert not any(frame["message"].get("tool_calls") for frame in context.frames)
    assert context.frames[-1]["message"]["content"] == "Retry the previous task"
