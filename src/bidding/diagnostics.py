import re
import traceback


def safe_text(value):
    text = str(value)
    text = re.sub(r'(?i)(Bearer\s+|api[_-]?key[=:]\s*)[^\s\"\']+', r'\1[REDACTED]', text)
    return re.sub(r'(?:sk-[A-Za-z0-9_-]+|bce-v3/[^\s\"\']+)', '[REDACTED]', text)


def exception_detail(exc):
    detail = {'type': type(exc).__name__, 'message': safe_text(exc),
        'traceback': safe_text(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-12000:]}
    if hasattr(exc, 'errors'):
        detail['validation_errors'] = [{key: value for key, value in item.items() if key in ('loc', 'type', 'msg')}
            for item in exc.errors()]
    return detail
