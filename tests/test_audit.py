from sqlalchemy import delete
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
import httpx

from bidding.storage import Store, request_audit


def test_unified_audit_contains_question_and_full_tool_lifecycle_with_redaction(tmp_path):
    store = Store(tmp_path)
    session = store.create_session()
    task = store.create_turn(session, '下载三个标书')
    log = store.start_tool('render_tender_page', {'url': 'https://example.org', 'api_key': 'private'}, 'agent', task, session, 'call1')
    assert next(row for row in store.audit_logs(task) if row['event_type'] == 'tool')['status'] == 'running'
    store.finish_tool(log, {'announcement_id': 'page', 'text': '正文' * 10000, 'token': 'secret'}, 123)
    store.record_context('reference_repair', {'status': 'completed'}, task, session)
    store.record_context('plan_repair', {'error': 'invalid graph'}, task, session)
    store.save('task', {'message': '下载三个标书', 'session_id': session, 'status': 'completed', 'answer': '已完成'}, task)
    audit = store.audit_logs(task)
    tools = [row for row in audit if row['event_type'] == 'tool']
    assert len(tools) == 1
    row = tools[0]
    assert row['request_text'] == '下载三个标书'
    assert row['model_call_id'] == 'call1'
    assert row['arguments']['api_key'] == '[REDACTED]'
    assert row['result']['token'] == '[REDACTED]'
    assert len(row['result']['text']) == 20000
    assert row['status'] == 'completed' and row['duration_ms'] == 123
    assert any(row['event_type'] == 'request' for row in audit)
    assert any(row['event_type'] == 'context' and row['status'] == 'failed' and row['error'] == 'invalid graph' for row in audit)


def test_historical_audit_backfill_is_idempotent_and_session_scoped(tmp_path):
    store = Store(tmp_path)
    session = store.create_session()
    task = store.create_turn(session, '原始问题')
    log = store.start_tool('read_tool_result', {'log_id': 'source'}, 'agent', task, session)
    store.finish_tool(log, {'error': 'ValueError'}, 0, 'invalid field')
    with store.engine.begin() as con:
        con.execute(delete(request_audit))
    store.backfill_audit()
    first = store.audit_logs(task)
    store.backfill_audit()
    assert len(store.audit_logs(task)) == len(first)
    row = next(row for row in first if row['event_type'] == 'tool')
    assert row['error'] == 'invalid field' and row['request_text'] == '原始问题'
    assert not store.audit_logs(session_id='other')


def test_audit_schema_supports_mysql_long_text_and_json():
    ddl = str(CreateTable(request_audit).compile(dialect=mysql.dialect()))
    assert 'LONGTEXT' in ddl and 'JSON' in ddl


async def test_audit_api_filters_individual_user_request(tmp_path, monkeypatch):
    from bidding import api
    store = Store(tmp_path)
    monkeypatch.setattr(api, 'store', store)
    session = store.create_session()
    task = store.create_turn(session, '单个问题')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
        result = await client.get('/request-audit', params={'task_id': task})
        assert result.status_code == 200
        assert all(row['task_id'] == task and row['request_text'] == '单个问题' for row in result.json()['audit_events'])
        assert (await client.get('/request-audit', params={'session_id': 'other'})).json()['audit_events'] == []
