import json
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, insert, select, update, func
from sqlalchemy.dialects.mysql import LONGTEXT


def now():
    return datetime.now(timezone.utc).isoformat()


def context_audit_values(payload):
    error = payload.get('error') or payload.get('exception')
    failed = bool(error or payload.get('failure'))
    return {'status': payload.get('status') or ('retrying' if payload.get('retrying') else 'failed' if failed else 'completed'),
        'error': json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else error}


metadata = MetaData()
records = Table("records", metadata,
    Column("id", String(64), primary_key=True), Column("kind", String(32), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql"), nullable=False), Column("updated_at", String(40), nullable=False))
sessions = Table("chat_sessions", metadata,
    Column("id", String(64), primary_key=True), Column("title", Text, nullable=False),
    Column("active_task_id", String(64)), Column("created_at", String(40), nullable=False))
messages = Table("chat_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(64), index=True, nullable=False), Column("task_id", String(64), index=True, nullable=False),
    Column("payload", JSON, nullable=False), Column("created_at", String(40), nullable=False))
tool_calls = Table("tool_calls", metadata,
    Column("id", String(64), primary_key=True), Column("session_id", String(64), index=True),
    Column("task_id", String(64), index=True), Column("model_call_id", String(128)),
    Column("source", String(16), nullable=False), Column("tool", String(128), nullable=False),
    Column("arguments", JSON, nullable=False), Column("result", JSON), Column("status", String(20), nullable=False),
    Column("error", Text), Column("started_at", String(40), nullable=False),
    Column("finished_at", String(40)), Column("duration_ms", Integer))
model_calls = Table("model_calls", metadata,
    Column("id", String(64), primary_key=True), Column("session_id", String(64), index=True),
    Column("task_id", String(64), index=True), Column("purpose", String(32), nullable=False),
    Column("model", String(128), nullable=False), Column("estimated_input_tokens", Integer, nullable=False),
    Column("usage", JSON), Column("status", String(20), nullable=False), Column("error", Text),
    Column("started_at", String(40), nullable=False), Column("finished_at", String(40)), Column("duration_ms", Integer))
context_events = Table("context_events", metadata,
    Column("id", String(64), primary_key=True), Column("session_id", String(64), index=True),
    Column("task_id", String(64), index=True), Column("kind", String(32), nullable=False),
    Column("payload", JSON, nullable=False), Column("created_at", String(40), nullable=False))
context_snapshots = Table("context_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("session_id", String(64), index=True, nullable=False),
    Column("task_id", String(64), nullable=False), Column("through_message_id", Integer, nullable=False),
    Column("frames", JSON, nullable=False), Column("created_at", String(40), nullable=False))
batch_items = Table("batch_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", String(64), index=True, nullable=False),
    Column("identity", String(64), nullable=False), Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, default=0), Column("payload", JSON, nullable=False),
    UniqueConstraint("batch_id", "identity"))
request_audit = Table('request_audit', metadata,
    Column('event_id', String(128), primary_key=True), Column('event_type', String(32), nullable=False),
    Column('session_id', String(64), index=True), Column('task_id', String(64), index=True),
    Column('request_text', Text().with_variant(LONGTEXT(), 'mysql')),
    Column('tool', String(128)), Column('model_call_id', String(128)),
    Column('arguments', JSON), Column('result', JSON), Column('details', JSON),
    Column('status', String(32), nullable=False), Column('error', Text().with_variant(LONGTEXT(), 'mysql')),
    Column('duration_ms', Integer), Column('created_at', String(40), nullable=False), Column('updated_at', String(40), nullable=False))


class SessionBusy(ValueError):
    pass


class Store:
    def __init__(self, root: Path, database_url=None):
        self.root = root
        self.file_lock = threading.Lock()
        (root / "files").mkdir(parents=True, exist_ok=True)
        self.db = root / "bidding.sqlite3"
        self.engine = create_engine(database_url or "sqlite:///" + str(self.db), pool_pre_ping=True, hide_parameters=True)
        metadata.create_all(self.engine)

    def _audit(self, con, event_type, event_id, task_id=None, session_id=None, **values):
        from bidding.tool_runtime import redact
        raw = con.execute(select(records.c.payload).where(records.c.id == task_id)).scalar() if task_id else None
        task = json.loads(raw) if raw else {}
        stamp = now()
        values = redact({'event_type': event_type, 'task_id': task_id,
            'session_id': session_id or task.get('session_id'), 'request_text': task.get('message'),
            'status': 'completed', **values, 'updated_at': stamp})
        if con.execute(select(request_audit.c.event_id).where(request_audit.c.event_id == event_id)).first():
            con.execute(update(request_audit).where(request_audit.c.event_id == event_id).values(**values))
        else:
            con.execute(insert(request_audit).values(event_id=event_id, created_at=stamp, **values))

    def audit_logs(self, task_id=None, session_id=None, offset=0, limit=100):
        query = select(request_audit).order_by(request_audit.c.created_at, request_audit.c.event_id)
        if task_id:
            query = query.where(request_audit.c.task_id == task_id)
        if session_id:
            query = query.where(request_audit.c.session_id == session_id)
        with self.engine.connect() as con:
            return [dict(row) for row in con.execute(query.offset(offset).limit(limit)).mappings()]

    def backfill_audit(self):
        counts = {}
        for table, event_type in ((records, 'request'), (tool_calls, 'tool'), (model_calls, 'model'),
                (context_events, 'context'), (messages, 'message')):
            offset = 0
            while True:
                query = select(table).order_by(table.c.id).offset(offset).limit(100)
                if table is records:
                    query = query.where(records.c.kind == 'task')
                with self.engine.begin() as con:
                    rows = con.execute(query).mappings().all()
                    for row in rows:
                        if table is records:
                            task = json.loads(row['payload'])
                            self._audit(con, event_type, 'request:' + row['id'], row['id'], task.get('session_id'),
                                arguments={'message': task.get('message')}, status=task.get('status', 'unknown'),
                                result={'answer': task.get('answer'), 'usage': task.get('usage')}, error=task.get('error'))
                            first_message_time = con.execute(select(messages.c.created_at).where(messages.c.task_id == row['id'])
                                .order_by(messages.c.id).limit(1)).scalar()
                        else:
                            values = {key: row[key] for key in ('tool', 'model_call_id', 'arguments', 'result', 'status', 'error', 'duration_ms') if key in row}
                            if table is model_calls:
                                values.update(result={'usage': row['usage']}, details={'purpose': row['purpose'], 'model': row['model']})
                            elif table is context_events:
                                values.update(result=row['payload'], details={'kind': row['kind']}, **context_audit_values(row['payload']))
                            elif table is messages:
                                values.update(result=row['payload'], details={'role': row['payload'].get('role')})
                            self._audit(con, event_type, event_type + ':' + str(row['id']), row['task_id'], row['session_id'], **values)
                        con.execute(update(request_audit).where(request_audit.c.event_id == event_type + ':' + str(row['id']))
                            .values(created_at=(first_message_time if table is records else None)
                                or row.get('started_at') or row.get('created_at') or row.get('updated_at')))
                counts[event_type] = counts.get(event_type, 0) + len(rows)
                if len(rows) < 100:
                    break
                offset += len(rows)
        return counts

    def save(self, kind, payload, record_id=None):
        record_id = record_id or uuid.uuid4().hex
        with self.engine.begin() as con:
            values = dict(kind=kind, payload=json.dumps(payload, ensure_ascii=False), updated_at=now())
            if con.execute(select(records.c.id).where(records.c.id == record_id)).first():
                con.execute(update(records).where(records.c.id == record_id).values(**values))
            else:
                con.execute(insert(records).values(id=record_id, **values))
            if kind == 'task':
                self._audit(con, 'task', 'task:' + record_id, record_id, payload.get('session_id'),
                    status=payload.get('status', 'queued'), error=payload.get('error'),
                    result={'answer': payload.get('answer'), 'usage': payload.get('usage'), 'batch_id': payload.get('batch_id')})
                self._audit(con, 'request', 'request:' + record_id, record_id, payload.get('session_id'),
                    arguments={'message': payload.get('message')}, status=payload.get('status', 'queued'), error=payload.get('error'),
                    result={'answer': payload.get('answer'), 'usage': payload.get('usage'), 'batch_id': payload.get('batch_id')})
        return record_id

    def get(self, record_id):
        with self.engine.connect() as con:
            row = con.execute(select(records).where(records.c.id == record_id)).mappings().first()
        return {"id": record_id, "kind": row["kind"], **json.loads(row["payload"])} if row else None

    def list_records(self, kind):
        with self.engine.connect() as con:
            rows = con.execute(select(records).where(records.c.kind == kind).order_by(records.c.updated_at.desc())).mappings().all()
        return [{"id": row["id"], "kind": kind, **json.loads(row["payload"])} for row in rows]

    def enqueue_item(self, batch_id, identity, payload):
        with self.engine.begin() as con:
            if con.execute(select(batch_items.c.id).where(batch_items.c.batch_id == batch_id, batch_items.c.identity == identity)).first():
                return False
            con.execute(insert(batch_items).values(batch_id=batch_id, identity=identity, status="pending", attempts=0, payload=payload))
        return True

    def items(self, batch_id, offset=0, limit=100):
        with self.engine.connect() as con:
            rows = con.execute(select(batch_items).where(batch_items.c.batch_id == batch_id).order_by(batch_items.c.id).offset(offset).limit(limit)).mappings().all()
        return [dict(row) for row in rows]

    def claim_item(self, batch_id):
        with self.engine.begin() as con:
            row = con.execute(select(batch_items).where(batch_items.c.batch_id == batch_id, batch_items.c.status == "pending").order_by(batch_items.c.id).limit(1)).mappings().first()
            if not row:
                return None
            claimed = con.execute(update(batch_items).where(batch_items.c.id == row["id"], batch_items.c.status == "pending").values(status="running", attempts=row["attempts"] + 1))
            return {**dict(row), "status": "running", "attempts": row["attempts"] + 1} if claimed.rowcount else None

    def update_item(self, item_id, status, payload):
        with self.engine.begin() as con:
            con.execute(update(batch_items).where(batch_items.c.id == item_id).values(status=status, payload=payload))

    def checkpoint_batch_file(self, batch_id, batch, item_id, payload):
        # Keep cumulative byte accounting and the per-file resume checkpoint in one transaction.
        with self.engine.begin() as con:
            con.execute(update(batch_items).where(batch_items.c.id == item_id).values(status='running', payload=payload))
            con.execute(update(records).where(records.c.id == batch_id).values(
                payload=json.dumps(batch, ensure_ascii=False), updated_at=now()))

    def reset_items(self, batch_id, statuses, reset_attempts=False):
        with self.engine.begin() as con:
            values = {"status": "pending"}
            if reset_attempts:
                values["attempts"] = 0
            con.execute(update(batch_items).where(batch_items.c.batch_id == batch_id, batch_items.c.status.in_(statuses)).values(**values))

    def item_counts(self, batch_id):
        with self.engine.connect() as con:
            rows = con.execute(select(batch_items.c.status, func.count()).where(batch_items.c.batch_id == batch_id).group_by(batch_items.c.status)).all()
        counts = dict(rows)
        return {**counts, "total": sum(counts.values())}

    def create_session(self, title="New conversation"):
        session_id = uuid.uuid4().hex
        with self.engine.begin() as con:
            con.execute(insert(sessions).values(id=session_id, title=title, created_at=now()))
        return session_id

    def session(self, session_id):
        with self.engine.connect() as con:
            row = con.execute(select(sessions).where(sessions.c.id == session_id)).mappings().first()
        return dict(row) if row else None

    def list_sessions(self, offset=0, limit=100):
        with self.engine.connect() as con:
            rows = con.execute(select(sessions).order_by(sessions.c.created_at.desc()).offset(offset).limit(limit)).mappings().all()
        return [dict(row) for row in rows]

    def create_turn(self, session_id, message):
        task_id = uuid.uuid4().hex
        with self.engine.begin() as con:
            claimed = con.execute(update(sessions).where(sessions.c.id == session_id, sessions.c.active_task_id.is_(None)).values(active_task_id=task_id))
            if not claimed.rowcount:
                if not con.execute(select(sessions.c.id).where(sessions.c.id == session_id)).first():
                    raise ValueError("Session not found")
                raise SessionBusy("This session already has an active task")
            payload = {"status": "queued", "message": message, "session_id": session_id, "events": []}
            con.execute(insert(records).values(id=task_id, kind="task", payload=json.dumps(payload, ensure_ascii=False), updated_at=now()))
            con.execute(insert(messages).values(session_id=session_id, task_id=task_id, payload={"role": "user", "content": message}, created_at=now()))
            self._audit(con, 'request', 'request:' + task_id, task_id, session_id, status='queued', arguments={'message': message})
        return task_id

    def release_session(self, session_id, task_id):
        with self.engine.begin() as con:
            con.execute(update(sessions).where(sessions.c.id == session_id, sessions.c.active_task_id == task_id).values(active_task_id=None))

    def claim_existing_task(self, session_id, task_id):
        with self.engine.begin() as con:
            claimed = con.execute(update(sessions).where(sessions.c.id == session_id,
                sessions.c.active_task_id.is_(None)).values(active_task_id=task_id))
            return bool(claimed.rowcount)

    def add_message(self, session_id, task_id, payload):
        with self.engine.begin() as con:
            result = con.execute(insert(messages).values(session_id=session_id, task_id=task_id, payload=payload, created_at=now()))
            self._audit(con, 'message', 'message:' + str(result.inserted_primary_key[0]), task_id, session_id,
                result=payload, details={'role': payload.get('role')})
            return result.inserted_primary_key[0]

    def history(self, session_id, current_task_id=None):
        return [row["payload"] for row in self.history_entries(session_id, current_task_id)]

    def history_entries(self, session_id, current_task_id=None, after_id=0, include_failed=False):
        with self.engine.connect() as con:
            rows = con.execute(select(messages).where(messages.c.session_id == session_id, messages.c.id > after_id).order_by(messages.c.id)).mappings().all()
            task_ids = {row["task_id"] for row in rows}
            completed = set()
            states = {}
            if task_ids:
                tasks = con.execute(select(records.c.id, records.c.payload).where(records.c.id.in_(task_ids))).all()
                states = {task_id: json.loads(raw).get("status") for task_id, raw in tasks}
                allowed = {"completed", "awaiting_confirmation", "scheduled", "partial_completed"}
                if include_failed:
                    allowed.update({"failed", "interrupted", "timed_out", "budget_exceeded"})
                completed = {task_id for task_id, state in states.items() if state in allowed}
        # Keep interrupted turns in audit history, but exclude incomplete tool sequences from model context.
        return [{**dict(row), "task_status": states.get(row["task_id"])} for row in rows if row["task_id"] in completed or row["task_id"] == current_task_id]

    def get_tool_log(self, log_id):
        with self.engine.connect() as con:
            row = con.execute(select(tool_calls).where(tool_calls.c.id == log_id)).mappings().first()
        return dict(row) if row else None

    def find_tool_log(self, task_id, model_call_id):
        with self.engine.connect() as con:
            row = con.execute(select(tool_calls).where(tool_calls.c.task_id == task_id,
                tool_calls.c.model_call_id == model_call_id).order_by(tool_calls.c.started_at.desc()).limit(1)).mappings().first()
        return dict(row) if row else None

    def start_model(self, task_id, session_id, purpose, model, estimated_input_tokens):
        log_id = uuid.uuid4().hex
        with self.engine.begin() as con:
            con.execute(insert(model_calls).values(id=log_id, task_id=task_id, session_id=session_id,
                purpose=purpose, model=model, estimated_input_tokens=estimated_input_tokens, status="running", started_at=now()))
            self._audit(con, 'model', 'model:' + log_id, task_id, session_id, status='running',
                details={'purpose': purpose, 'model': model, 'estimated_input_tokens': estimated_input_tokens})
        return log_id

    def finish_model(self, log_id, usage, duration_ms, error=None):
        with self.engine.begin() as con:
            con.execute(update(model_calls).where(model_calls.c.id == log_id).values(usage=usage,
                duration_ms=duration_ms, status="failed" if error else "completed", error=error, finished_at=now()))
            row = con.execute(select(model_calls).where(model_calls.c.id == log_id)).mappings().first()
            if row:
                self._audit(con, 'model', 'model:' + log_id, row['task_id'], row['session_id'],
                    status='failed' if error else 'completed', error=error, duration_ms=duration_ms, result={'usage': usage})

    def model_logs(self, task_id=None, session_id=None, offset=0, limit=100):
        query = select(model_calls).order_by(model_calls.c.started_at, model_calls.c.id).offset(offset).limit(limit)
        if task_id:
            query = query.where(model_calls.c.task_id == task_id)
        if session_id:
            query = query.where(model_calls.c.session_id == session_id)
        with self.engine.connect() as con:
            return [dict(row) for row in con.execute(query).mappings()]

    def record_context(self, kind, payload, task_id, session_id=None):
        event_id = uuid.uuid4().hex
        with self.engine.begin() as con:
            con.execute(insert(context_events).values(id=event_id, kind=kind, payload=payload,
                task_id=task_id, session_id=session_id, created_at=now()))
            self._audit(con, 'context', 'context:' + event_id, task_id, session_id,
                details={'kind': kind}, result=payload, **context_audit_values(payload))
        return event_id

    def context_logs(self, task_id=None, session_id=None, offset=0, limit=100):
        query = select(context_events).order_by(context_events.c.created_at, context_events.c.id).offset(offset).limit(limit)
        if task_id:
            query = query.where(context_events.c.task_id == task_id)
        if session_id:
            query = query.where(context_events.c.session_id == session_id)
        with self.engine.connect() as con:
            return [dict(row) for row in con.execute(query).mappings()]

    def save_snapshot(self, session_id, task_id, through_message_id, frames):
        with self.engine.begin() as con:
            con.execute(insert(context_snapshots).values(session_id=session_id, task_id=task_id,
                through_message_id=through_message_id, frames=frames, created_at=now()))

    def latest_snapshot(self, session_id):
        with self.engine.connect() as con:
            rows = con.execute(select(context_snapshots).where(context_snapshots.c.session_id == session_id).order_by(context_snapshots.c.id.desc())).mappings()
            for row in rows:
                raw = con.execute(select(records.c.payload).where(records.c.id == row["task_id"])).scalar()
                if raw and json.loads(raw).get("status") == "completed":
                    return dict(row)
        return None

    def conversation_messages(self, session_id, offset=0, limit=100):
        with self.engine.connect() as con:
            rows = con.execute(select(messages).where(messages.c.session_id == session_id).order_by(messages.c.id).offset(offset).limit(limit)).mappings().all()
        return [dict(row) for row in rows]

    def start_tool(self, name, arguments, source, task_id=None, session_id=None, model_call_id=None):
        log_id = uuid.uuid4().hex
        with self.engine.begin() as con:
            con.execute(insert(tool_calls).values(id=log_id, tool=name, arguments=arguments, source=source,
                task_id=task_id, session_id=session_id, model_call_id=model_call_id, status="running", started_at=now()))
            self._audit(con, 'tool', 'tool:' + log_id, task_id, session_id, tool=name,
                model_call_id=model_call_id, arguments=arguments, status='running', details={'source': source})
        return log_id

    def finish_tool(self, log_id, result, duration_ms, error=None):
        with self.engine.begin() as con:
            con.execute(update(tool_calls).where(tool_calls.c.id == log_id).values(result=result,
                duration_ms=duration_ms, error=error, status="failed" if error else "completed", finished_at=now()))
            row = con.execute(select(tool_calls).where(tool_calls.c.id == log_id)).mappings().first()
            if row:
                self._audit(con, 'tool', 'tool:' + log_id, row['task_id'], row['session_id'],
                    result=result, duration_ms=duration_ms, error=error, status='failed' if error else 'completed')

    def logs(self, task_id=None, session_id=None, offset=0, limit=100, newest_first=False, include_results=True):
        fields = list(tool_calls.c) if include_results else [column for column in tool_calls.c if column.name != 'result']
        order = (tool_calls.c.started_at.desc(), tool_calls.c.id.desc()) if newest_first else (tool_calls.c.started_at, tool_calls.c.id)
        query = select(*fields).order_by(*order).offset(offset).limit(limit)
        if task_id:
            query = query.where(tool_calls.c.task_id == task_id)
        if session_id:
            query = query.where(tool_calls.c.session_id == session_id)
        with self.engine.connect() as con:
            return [dict(row) for row in con.execute(query).mappings()]

    def recover_tasks(self):
        with self.engine.connect() as con:
            rows = con.execute(select(records.c.id, records.c.payload).where(records.c.kind == "task")).all()
        for record_id, raw in rows:
            task = json.loads(raw)
            if task["status"] in ("queued", "running"):
                for step in task.get('steps', []):
                    if step['status'] == 'running':
                        step.update(status='pending', error='服务重启，执行结果未确认，请从检查点继续')
                self.save("task", {**task, "status": "interrupted", "error": "Process restarted; completed checkpoints retained for authorized resume."}, record_id)
            if task.get("session_id"):
                self.release_session(task["session_id"], record_id)
        with self.engine.begin() as con:
            con.execute(update(tool_calls).where(tool_calls.c.status == "running").values(status="interrupted", finished_at=now(), error="Process restarted during tool call"))
            con.execute(update(model_calls).where(model_calls.c.status == "running").values(status="interrupted", finished_at=now(), error="Process restarted during model call"))
            con.execute(update(request_audit).where(request_audit.c.status == 'running', request_audit.c.event_type.in_(['tool', 'model']))
                .values(status='interrupted', updated_at=now(), error='Process restarted during call'))
