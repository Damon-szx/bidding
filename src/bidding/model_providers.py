"""Provider strategies for the shared Chat Completions transport."""
import os
from urllib.parse import urlparse


class ChatProvider:
    name = 'openai_compatible'
    prefix = ''
    default_url = 'https://api.deepseek.com'
    default_model = ''

    def __init__(self):
        def setting(suffix, default=''):
            specific = os.getenv(f'{self.prefix}_{suffix}') if self.prefix else None
            return specific if specific is not None else os.getenv(f'MODEL_{suffix}', default)
        self.model = setting('NAME', self.default_model)
        self.base_url = setting('BASE_URL', self.default_url).rstrip('/')
        self.api_key = setting('API_KEY')
        self.thinking = setting('THINKING', 'disabled')
        if self.thinking not in ('', 'enabled', 'disabled'):
            raise ValueError('Model thinking must be enabled or disabled')

    def options(self):
        return {}


class DeepSeekProvider(ChatProvider):
    name = 'deepseek'
    prefix = 'DEEPSEEK'

    def options(self):
        return {'thinking': {'type': self.thinking}} if self.thinking else {}


class QwenProvider(ChatProvider):
    name = 'qwen'
    prefix = 'QWEN'
    default_url = 'https://maas.qianwenaiapi.com/compatible-mode/v1'
    default_model = 'qwen3.7-flash'

    def __init__(self):
        # A newly selected Qwen channel must not inherit a DeepSeek endpoint/key.
        self.model = os.getenv('QWEN_NAME', self.default_model)
        self.base_url = os.getenv('QWEN_BASE_URL', self.default_url).rstrip('/')
        self.api_key = os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY', '')
        self.thinking = os.getenv('QWEN_THINKING', 'disabled')
        if self.thinking not in ('enabled', 'disabled'):
            raise ValueError('QWEN_THINKING must be enabled or disabled')

    def options(self):
        return {'enable_thinking': self.thinking == 'enabled'}


def create_provider():
    name = os.getenv('MODEL_PROVIDER', '').strip().lower()
    if not name:
        host = urlparse(os.getenv('MODEL_BASE_URL', 'https://api.deepseek.com')).hostname
        name = 'deepseek' if host == 'api.deepseek.com' else 'openai_compatible'
    providers = {'deepseek': DeepSeekProvider, 'qwen': QwenProvider,
                 'openai_compatible': ChatProvider}
    if name not in providers:
        raise ValueError('MODEL_PROVIDER must be deepseek, qwen or openai_compatible')
    return providers[name]()
