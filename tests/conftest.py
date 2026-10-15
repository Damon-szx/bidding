import pytest


@pytest.fixture(autouse=True)
def legacy_reflection_boundary(monkeypatch, request):
    # API imports must never connect unit tests to the user's configured database.
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.setenv('MODEL_PROVIDER', '')
    for prefix in ('DEEPSEEK', 'QWEN'):
        for suffix in ('API_KEY', 'NAME', 'BASE_URL', 'THINKING'):
            monkeypatch.delenv(f'{prefix}_{suffix}', raising=False)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    # Legacy transport scripts cover the producer loop; specialist tests opt in.
    if request.module.__name__.split('.')[-1] != 'test_specialists':
        monkeypatch.setenv('REFLECTION_ENABLED', 'false')
