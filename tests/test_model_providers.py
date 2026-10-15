import asyncio
import json

import httpx
import pytest

from bidding.model import ModelClient
from bidding.model_providers import create_provider
from bidding.storage import Store


@pytest.mark.parametrize('provider', ['qwen', 'deepseek'])
async def test_provider_request_and_tool_response(tmp_path, monkeypatch, provider):
    monkeypatch.setenv('MODEL_PROVIDER', provider)
    monkeypatch.setenv('MODEL_API_KEY', 'legacy-deepseek')
    monkeypatch.setenv('MODEL_NAME', 'deepseek-flash')
    monkeypatch.setenv('MODEL_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.setenv('QWEN_API_KEY', 'fixture-qwen')
    monkeypatch.setenv('QWEN_THINKING', 'disabled')
    monkeypatch.setenv('MODEL_THINKING', 'disabled')
    tool = {'id': 'one', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}

    def serve(request):
        body = json.loads(request.content)
        assert body['tools'][0]['function']['name'] == 'lookup'
        assert body['stream'] is False
        if provider == 'qwen':
            assert str(request.url) == 'https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions'
            assert request.headers['authorization'] == 'Bearer fixture-qwen'
            assert body['model'] == 'qwen3.7-flash'
            assert body['enable_thinking'] is False
            assert 'thinking' not in body
        else:
            assert request.url.host == 'api.deepseek.com'
            assert request.headers['authorization'] == 'Bearer legacy-deepseek'
            assert body['thinking'] == {'type': 'disabled'}
            assert 'enable_thinking' not in body
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [tool]}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        model = ModelClient(client, Store(tmp_path), 'task')
        answer = await model.call([{'role': 'user', 'content': 'lookup'}],
            tools=[{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}])
        assert answer['tool_calls'] == [tool]
        assert model.usage()['input_tokens'] == 20


async def test_absolute_timeout_finishes_log(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_PROVIDER', 'qwen')
    monkeypatch.setenv('QWEN_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_TIMEOUT_SECONDS', '0.01')
    monkeypatch.setenv('MODEL_MAX_ATTEMPTS', '1')
    async def serve(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    store = Store(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(TimeoutError):
            await ModelClient(client, store, 'task').call([{'role': 'user', 'content': 'hello'}])
    log = store.model_logs(task_id='task')[0]
    assert log['status'] != 'running'
    assert log['finished_at']


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv('MODEL_PROVIDER', 'typo')
    with pytest.raises(ValueError, match='MODEL_PROVIDER'):
        create_provider()


def test_qwen_does_not_inherit_deepseek_key(monkeypatch):
    monkeypatch.setenv('MODEL_PROVIDER', 'qwen')
    monkeypatch.setenv('MODEL_API_KEY', 'deepseek-only')
    monkeypatch.delenv('QWEN_API_KEY', raising=False)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    assert not create_provider().api_key
