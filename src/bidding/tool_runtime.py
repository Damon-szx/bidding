import asyncio
import time
import os
import uuid

from bidding.retry import failure, retry_delay


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in ("password", "api_key", "token", "authorization", "secret") else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class ToolRuntime:
    def __init__(self, store, handlers, deadline=None, attempt_guard=None, timeout=None):
        self.store, self.handlers = store, handlers
        self.deadline, self.attempt_guard = deadline, attempt_guard
        self.timeout = timeout or float(os.getenv('TOOL_TIMEOUT_SECONDS', '90'))
        self.semaphore = asyncio.Semaphore(3)
        self.sites = {}
        self.cooldowns = {}

    async def call(self, name, arguments, source="agent", task_id=None, session_id=None, model_call_id=None):
        from urllib.parse import urlparse
        retry_safe = name in {'search_tenders', 'fetch_tender_detail', 'render_tender_page', 'discover_tender_files', 'download_tender_file', 'read_tool_result'}
        maximum = max(1, min(3, int(os.getenv('TOOL_MAX_ATTEMPTS', '3')))) if retry_safe else 1
        operation_id = uuid.uuid4().hex
        for attempt in range(1, maximum + 1):
            if self.attempt_guard:
                self.attempt_guard(name, arguments)
            remaining = self.deadline - time.monotonic() if self.deadline else self.timeout
            if remaining <= 0:
                raise asyncio.TimeoutError('Task time limit reached')
            log_id = self.store.start_tool(name, redact(arguments), source, task_id, session_id, model_call_id)
            started = time.perf_counter()
            detail = None
            try:
                site = urlparse(arguments.get('url', '')).hostname or 'local'
                limit = self.sites.setdefault(site, asyncio.Semaphore(2))
                async def invoke():
                    if not retry_safe:
                        return await self.handlers[name](**arguments)
                    async with self.semaphore, limit:
                        wait = self.cooldowns.get(site, 0) - time.monotonic()
                        if wait > 0:
                            await asyncio.sleep(wait)
                        return await self.handlers[name](**arguments)
                result = await asyncio.wait_for(invoke(), timeout=remaining if name in ('execute_plan_steps', 'delegate_agent') else min(self.timeout, remaining))
            except (Exception, asyncio.CancelledError) as exc:
                detail = failure(exc)
                result = {'error': type(exc).__name__, 'message': str(exc), 'failure': detail}
                if getattr(exc, 'evidence_repair', None):
                    result['evidence_repair'] = exc.evidence_repair
                self.store.finish_tool(log_id, redact(result), int((time.perf_counter() - started) * 1000), str(exc) or type(exc).__name__)
                exc.tool_log_id = log_id
                exc.failure = detail
                delay = retry_delay(exc, attempt, base=float(os.getenv('RETRY_BASE_SECONDS', '0.5')))
                if detail['category'] == 'rate_limited':
                    self.cooldowns[site] = time.monotonic() + delay
                    self.sites[site] = asyncio.Semaphore(1)
                retrying = not isinstance(exc, asyncio.CancelledError) and detail['retryable'] and attempt < maximum
                if self.deadline and delay >= self.deadline - time.monotonic():
                    retrying = False
                self.store.record_context('tool_attempt', {'tool': name, 'operation_id': operation_id,
                    'log_id': log_id, 'attempt': attempt, 'max_attempts': maximum, 'failure': detail,
                    'retrying': retrying, 'wait_seconds': delay if retrying else 0}, task_id, session_id)
                if not retrying:
                    raise
                await asyncio.sleep(delay)
                continue
            self.store.finish_tool(log_id, redact(result), int((time.perf_counter() - started) * 1000))
            self.store.record_context('tool_attempt', {'tool': name, 'operation_id': operation_id,
                'log_id': log_id, 'attempt': attempt, 'max_attempts': maximum, 'status': 'completed'}, task_id, session_id)
            return result, log_id
