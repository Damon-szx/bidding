from contextlib import asynccontextmanager
import os
import asyncio
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.config import DATA, ROOT
from bidding.search import search_tenders
from bidding.storage import SessionBusy, Store
from bidding.batch import batch_view
from bidding.confirmation import expire_task, expire_batch, pending_reply

store = Store(DATA, os.getenv("DATABASE_URL") or None)
acquisition = Acquisition(store)
agent = Agent(acquisition)


def dispatch_conservative_continuation(task, runners):
    if not task.get('conservative_continuation') or task.get('status') not in ('queued', 'interrupted'):
        return
    task_id, session_id = task['id'], task.get('session_id')
    if task_id in runners or task_id in agent.running or not session_id:
        return
    if not store.claim_existing_task(session_id, task_id):
        return
    runner = asyncio.create_task(agent.run(task['message'], task_id, session_id))
    runners[task_id] = runner
    runner.add_done_callback(lambda completed: runners.pop(task_id, None))


@asynccontextmanager
async def lifespan(app):
    store.recover_tasks()
    agent.batches.recover()
    continuations = {}
    async def expire_confirmations():
        while True:
            for task in store.list_records('task'):
                expired_task = expire_task(store, task)
                dispatch_conservative_continuation(expired_task, continuations)
            for batch in store.list_records('batch'):
                expire_batch(store, batch)
            await asyncio.sleep(5)
    expiration = asyncio.create_task(expire_confirmations())
    yield
    expiration.cancel()
    await asyncio.gather(expiration, return_exceptions=True)
    for runner in list(continuations.values()):
        runner.cancel()
    await asyncio.gather(*list(continuations.values()), return_exceptions=True)
    await agent.batches.close()
    await acquisition.close()
    store.engine.dispose()


app = FastAPI(title="Bidding Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    confirmation_task_id: str | None = Field(default=None, min_length=1, max_length=64)


class SessionRequest(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=200)


class ToolRequest(BaseModel):
    arguments: dict


class TaskControl(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class BatchControl(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    action: str
    scope: str | None = None
    count: int | None = Field(default=None, ge=1, le=5000)


@app.get('/batches')
async def list_batches(session_id: str):
    return {'batches': [batch_view(store, batch['id']) for batch in store.list_records('batch')
        if batch.get('owner_session_id') == session_id]}


@app.get('/batches/{batch_id}')
async def get_batch(batch_id: str):
    try:
        return batch_view(store, batch_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get('/batches/{batch_id}/items')
async def get_batch_items(batch_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    await get_batch(batch_id)
    return {'items': store.items(batch_id, offset, limit)}


@app.post('/batches/{batch_id}/control')
async def control_batch(batch_id: str, request: BatchControl):
    try:
        return await agent.batches.control(batch_id, request.action, request.session_id, request.scope, request.count)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/batches/{batch_id}/manifest')
async def batch_manifest(batch_id: str):
    batch = await get_batch(batch_id)
    items = store.items(batch_id, limit=5000)
    return JSONResponse({'status': batch['status'], 'contract': batch['contract'], 'counts': batch['counts'],
        'projects': [{'title': item['payload']['entry']['title'], 'status': item['status'],
            'source_url': item['payload'].get('source_url') or item['payload']['entry'].get('url'),
            'files': [{key: file.get(key) for key in ('filename', 'document_type', 'local_download_url', 'size', 'sha256')}
                for file in item['payload'].get('files', [])]} for item in items]},
        headers={'Content-Disposition': f'attachment; filename="bidding-{batch_id}.json"'})


@app.post("/chat", status_code=202)
async def chat(request: ChatRequest, background: BackgroundTasks):
    session_id = request.session_id or store.create_session(request.message[:100])
    try:
        if request.confirmation_task_id:
            pending = store.get(request.confirmation_task_id)
            if not pending or pending.get('kind') != 'task' or pending.get('session_id') != session_id:
                raise ValueError('确认请求不属于当前会话。')
            expire_task(store, pending)
            if pending.get('status') != 'awaiting_clarification':
                raise ValueError('确认已失效或超时，默认不授权继续。请重新明确要求后发起任务。')
        else:
            pending = pending_reply(store, session_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        task_id = store.create_turn(session_id, request.message)
    except SessionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if pending:
        task = store.get(task_id)
        task['clarification_reply'] = {'task_id': pending['id'], 'original_request': pending['message'],
            'question': pending['confirmation']['question']}
        store.save('task', task, task_id)
        pending['confirmation'].update(status='answered', reply_task_id=task_id)
        pending.update(status='completed', answer=pending['answer'] + '\n\n已收到补充回复，按新的请求重新判断授权及下载目标；这不代表文件已下载。')
        store.save('task', pending, pending['id'])
        store.record_context('human_confirmation', {'status': 'answered', 'reply_task_id': task_id}, pending['id'], session_id)
    background.add_task(agent.run, request.message, task_id, session_id)
    return {"task_id": task_id, "session_id": session_id, "status": "queued", "status_url": "/tasks/" + task_id}


@app.post("/sessions", status_code=201)
async def create_session(request: SessionRequest):
    return {"session_id": store.create_session(request.title)}


@app.get("/sessions")
async def list_sessions(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return {"sessions": store.list_sessions(offset, limit)}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    result = store.session(session_id)
    if not result:
        raise HTTPException(404, "Session not found")
    return result


@app.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    if not store.session(session_id):
        raise HTTPException(404, "Session not found")
    return {"session_id": session_id, "messages": store.conversation_messages(session_id, offset, limit)}


@app.get("/tool-calls")
async def get_tool_calls(task_id: str | None = None, session_id: str | None = None,
                         offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                         newest_first: bool = False, include_results: bool = True):
    return {"tool_calls": store.logs(task_id, session_id, offset, limit, newest_first, include_results)}


@app.get('/tool-calls/{log_id}')
async def get_tool_call(log_id: str):
    log = store.get_tool_log(log_id)
    if not log:
        raise HTTPException(404, 'Tool call not found')
    return log


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    result = store.get(task_id)
    if not result or result["kind"] != "task":
        raise HTTPException(404, "Task not found")
    return expire_task(store, result)


@app.post('/tasks/{task_id}/cancel')
async def cancel_task(task_id: str, request: TaskControl):
    try:
        return await agent.cancel(task_id, request.session_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/model-calls")
async def get_model_calls(task_id: str | None = None, session_id: str | None = None,
                          offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return {"model_calls": store.model_logs(task_id, session_id, offset, limit)}


@app.get("/context-events")
async def get_context_events(task_id: str | None = None, session_id: str | None = None,
                             offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return {"context_events": store.context_logs(task_id, session_id, offset, limit)}


@app.get('/request-audit')
async def get_request_audit(task_id: str | None = None, session_id: str | None = None,
                            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return {'audit_events': store.audit_logs(task_id, session_id, offset, limit)}


@app.post("/tools/{name}")
async def call_tool(name: str, request: ToolRequest):
    if name not in agent.runtime.handlers:
        raise HTTPException(404, "Unknown tool")
    try:
        result, log_id = await agent.runtime.call(name, request.arguments, source="api")
        return {"result": result, "log_id": log_id}
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get('/files/{file_id}')
async def downloaded_file(file_id: str):
    record = store.get(file_id)
    if not record or record['kind'] != 'file':
        raise HTTPException(404, 'File not found')
    path = Path(record['path']).resolve()
    if not path.is_relative_to((store.root / 'files').resolve()) or not path.is_file():
        raise HTTPException(404, 'File not found')
    return FileResponse(path, filename=record.get('filename') or path.name, content_disposition_type='attachment',
        headers={'X-Content-Type-Options': 'nosniff'})
