import asyncio
import json
import math
import os
import time
from contextvars import ContextVar
from bidding.model_providers import create_provider
from bidding.retry import failure, retry_delay


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(value):
    # Provider-neutral conservative estimate, not an exact model tokenizer.
    return math.ceil(len(encoded(value).encode("utf-8")) / 2)


class BudgetExceeded(ValueError):
    pass


class ModelClient:
    def __init__(self, client, store, task_id, session_id=None):
        self.client, self.store = client, store
        self.task_id, self.session_id = task_id, session_id
        self.provider = create_provider()
        self.model = self.provider.model
        self.base_url = self.provider.base_url
        self.thinking = self.provider.thinking
        self.charged = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown_calls = 0
        self.last_request = {}
        self._last_log = ContextVar('last_model_log', default=None)
        self.budget = int(os.getenv("TASK_TOKEN_BUDGET", "300000"))
        self.soft_budget = self.budget
        self.budget_mode = os.getenv('TASK_TOKEN_BUDGET_MODE', 'hard')
        if self.budget_mode not in ('hard', 'soft'):
            raise ValueError('TASK_TOKEN_BUDGET_MODE must be hard or soft')
        if self.budget_mode == 'soft':
            self.budget = max(self.soft_budget, int(os.getenv('TASK_TOKEN_HARD_LIMIT', '1000000')))
        self.soft_warning_sent = False
        self.budget_extension = 0
        self.hard_limit = int(os.getenv("CONTEXT_HARD_LIMIT_TOKENS", "32000"))

    @property
    def last_log_id(self):
        return self._last_log.get()

    @last_log_id.setter
    def last_log_id(self, value):
        self._last_log.set(value)

    def usage(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "unknown_usage_calls": self.unknown_calls, "budget_consumed_tokens": self.charged,
            "remaining_tokens": max(0, self.budget - self.charged), "last_request": self.last_request,
            "budget_tokens": self.budget, "budget_extension_tokens": self.budget_extension,
            "budget_mode": self.budget_mode, "soft_budget_tokens": self.soft_budget,
            "note": "Unknown usage is reserved using estimates; provider usage is authoritative when available."}

    async def call(self, messages, purpose="agent", tools=None, max_tokens=2048):
        maximum = max(1, min(3, int(os.getenv('MODEL_MAX_ATTEMPTS', '3'))))
        for attempt in range(1, maximum + 1):
            try:
                return await self._call_once(messages, purpose, tools, max_tokens)
            except Exception as exc:
                detail = failure(exc)
                if not detail['retryable'] or attempt == maximum:
                    raise
                delay = retry_delay(exc, attempt, base=float(os.getenv('RETRY_BASE_SECONDS', '0.5')))
                self.store.record_context('model_retry', {'purpose': purpose, 'attempt': attempt,
                    'failure': detail, 'wait_seconds': delay}, self.task_id, self.session_id)
                await asyncio.sleep(delay)

    async def _call_once(self, messages, purpose="agent", tools=None, max_tokens=2048):
        if not self.provider.api_key or not self.model:
            raise ValueError("Configure the selected model provider API key and model name; direct tools remain available")
        estimate = estimate_tokens({"messages": messages, "tools": tools or []})
        reservation = estimate + max_tokens
        self.last_request = {'estimated_input_tokens': estimate, 'reserved_output_tokens': max_tokens,
            'required_tokens': reservation, 'remaining_before_request': max(0, self.budget - self.charged)}
        if reservation > self.hard_limit:
            raise BudgetExceeded("Context exceeds configured limit; narrow the request or retrieve smaller result pages")
        if self.budget_mode == 'soft' and self.charged + reservation > self.soft_budget and not self.soft_warning_sent:
            self.soft_warning_sent = True
            self.store.record_context('budget_check', {'status': 'review_required', **self.last_request,
                'soft_budget_tokens': self.soft_budget, 'hard_budget_tokens': self.budget,
                'reason': 'soft_budget_requires_progress_review_not_immediate_stop'}, self.task_id, self.session_id)
        if self.budget_mode == 'hard' and self.charged + reservation > self.budget:
            shortfall = self.charged + reservation - self.budget
            allowance = min(max(0, int(os.getenv('TASK_TOKEN_GRACE_TOKENS', '15000'))), self.budget // 20)
            if not self.budget_extension and shortfall <= allowance:
                self.budget += allowance
                self.budget_extension = allowance
                self.last_request['remaining_before_request'] = max(0, self.budget - self.charged)
                self.store.record_context('budget_check', {'status': 'extended', **self.last_request,
                    'shortfall_tokens': shortfall, 'extension_tokens': allowance,
                    'reason': 'one_time_small_shortfall_grace'}, self.task_id, self.session_id)
        if self.charged + reservation > self.budget:
            self.store.record_context('budget_check', {'status': 'stopped', **self.last_request,
                'reason': 'remaining_budget_cannot_fit_next_request'}, self.task_id, self.session_id)
            raise BudgetExceeded("Remaining task token budget cannot fit next model request; partial results are retained")
        self.charged += reservation
        log_id = self.store.start_model(self.task_id, self.session_id, purpose, self.model, estimate)
        self.last_log_id = log_id
        start = time.perf_counter()
        usage = None
        error = None
        counted = False
        try:
            body = {"model": self.model, "messages": messages, "stream": False, "max_tokens": max_tokens}
            if tools:
                body["tools"] = tools
            body.update(self.provider.options())
            async with asyncio.timeout(float(os.getenv('MODEL_TIMEOUT_SECONDS', '90'))):
                response = await self.client.post(self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.provider.api_key}, json=body)
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage")
            if usage and isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
                self.input_tokens += usage["prompt_tokens"]
                self.output_tokens += usage["completion_tokens"]
                self.charged += usage["prompt_tokens"] + usage["completion_tokens"] - reservation
            else:
                self.unknown_calls += 1
            counted = True
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("Model output limit reached; response is incomplete")
            answer = choice["message"]
            if not answer.get("tool_calls") and not (answer.get("content") or "").strip():
                raise ValueError("Model returned an empty answer")
            return {**answer, '_usage': usage}
        except (Exception, asyncio.CancelledError) as exc:
            if not counted:
                self.unknown_calls += 1
            error = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            self.store.finish_model(log_id, usage, int((time.perf_counter() - start) * 1000), error)
