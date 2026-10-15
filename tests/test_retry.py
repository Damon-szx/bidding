import asyncio
import time

import httpx
import pytest

from bidding.retry import failure, retry_delay
from bidding.storage import Store
from bidding.tool_runtime import ToolRuntime


async def test_transient_errors_try_three_times_and_log_every_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('RETRY_BASE_SECONDS', '0')
    store = Store(tmp_path)
    attempts = 0
    async def handler(url):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError('temporary')
        return {'title': 'result'}
    result, log_id = await ToolRuntime(store, {'fetch_tender_detail': handler}).call('fetch_tender_detail', {'url': 'https://example.org'}, task_id='task', model_call_id='call')
    assert attempts == 3 and result['title'] == 'result'
    logs = store.logs(task_id='task')
    assert [item['status'] for item in logs] == ['failed', 'failed', 'completed']
    assert all(item['model_call_id'] == 'call' for item in logs)
    events = store.context_logs(task_id='task')
    assert [item['payload']['attempt'] for item in events] == [1, 2, 3]
    assert len({item['payload']['operation_id'] for item in events}) == 1


@pytest.mark.parametrize('code,expected', [(401, 'access_required'), (403, 'access_required'), (404, 'source_changed')])
async def test_permanent_http_failures_are_not_blindly_retried(tmp_path, code, expected):
    store = Store(tmp_path)
    async def handler(url):
        httpx.Response(code, request=httpx.Request('GET', url)).raise_for_status()
    with pytest.raises(httpx.HTTPStatusError):
        await ToolRuntime(store, {'fetch_tender_detail': handler}).call('fetch_tender_detail', {'url': 'https://example.org'})
    assert len(store.logs()) == 1
    assert store.logs()[0]['result']['failure']['category'] == expected


def test_rate_limit_honors_retry_after():
    response = httpx.Response(429, headers={'Retry-After': '42'}, request=httpx.Request('GET', 'https://example.org'))
    error = httpx.HTTPStatusError('limited', request=response.request, response=response)
    assert failure(error)['retryable']
    assert retry_delay(error, 1) == 42


def test_cancellation_and_invalid_plan_are_not_transport_retries():
    assert failure(asyncio.CancelledError())['category'] == 'cancelled'
    assert failure(ValueError('invalid planned argument'))['recovery'] == 'repair_arguments'
    assert not failure(ValueError('invalid planned argument'))['retryable']


async def test_non_idempotent_navigation_and_invalid_arguments_not_retried(tmp_path):
    store = Store(tmp_path)
    async def handler():
        raise httpx.ConnectError('navigation outcome uncertain')
    with pytest.raises(httpx.ConnectError):
        await ToolRuntime(store, {'navigate_tender_entry': handler}).call('navigate_tender_entry', {})
    assert len(store.logs()) == 1
    with pytest.raises(TypeError):
        await ToolRuntime(store, {'fetch_tender_detail': handler}).call('fetch_tender_detail', {'wrong': True})
    assert store.logs()[-1]['result']['failure']['category'] == 'invalid_arguments'


async def test_retry_wait_cannot_exceed_task_deadline(tmp_path):
    store = Store(tmp_path)
    async def handler(url):
        httpx.Response(429, headers={'Retry-After': '60'}, request=httpx.Request('GET', url)).raise_for_status()
    with pytest.raises(httpx.HTTPStatusError):
        await ToolRuntime(store, {'fetch_tender_detail': handler}, deadline=time.monotonic() + 0.1).call('fetch_tender_detail', {'url': 'https://example.org'})
    assert len(store.logs()) == 1
    assert store.context_logs()[0]['payload']['retrying'] is False
