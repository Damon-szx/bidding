"""Bounded transport recovery; business failures must go back to the planner."""
import asyncio
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


def failure(exc):
    if isinstance(exc, BaseExceptionGroup):
        details = [failure(item) for item in exc.exceptions]
        return next((item for item in details if item['retryable']), details[0])
    code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if isinstance(exc, asyncio.CancelledError):
        category, retryable, recovery = 'cancelled', False, 'preserve_checkpoints_and_report_cancellation_source'
    elif isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException, httpx.NetworkError, ConnectionError)):
        category, retryable, recovery = 'transient', True, 'retry_with_backoff'
    elif code == 429:
        category, retryable, recovery = 'rate_limited', True, 'wait_and_reduce_concurrency'
    elif code in (500, 502, 503, 504):
        category, retryable, recovery = 'transient', True, 'retry_with_backoff'
    elif code in (401, 403):
        category, retryable, recovery = 'access_required', False, 'request_human_help'
    elif code in (404, 410):
        category, retryable, recovery = 'source_changed', False, 'find_another_source'
    elif isinstance(exc, (TypeError, KeyError, ValueError)):
        category, retryable, recovery = 'invalid_arguments', False, 'repair_arguments'
    else:
        category, retryable, recovery = 'non_retryable', False, 'inspect_error_and_replan'
    return {'category': category, 'retryable': retryable, 'recovery': recovery, 'http_status': code}


def retry_delay(exc, attempt, base=0.5, maximum=30):
    if isinstance(exc, httpx.HTTPStatusError):
        header = exc.response.headers.get('Retry-After')
        if header:
            try:
                return max(0, float(header))
            except ValueError:
                try:
                    date = parsedate_to_datetime(header)
                    return max(0, (date - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
    return min(maximum, base * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
