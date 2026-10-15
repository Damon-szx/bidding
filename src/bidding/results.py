"""Stable evidence references for model-facing reads and page actions."""
from bidding.model import estimate_tokens
import hashlib
import json


def canonical_read(store, arguments, task_id, session_id=None):
    args = {'field': '', 'offset': 0, 'limit': 1000, 'contains': '', **arguments}
    requested = store.get_tool_log(args['log_id'])
    root = original_log(store, args['log_id'], task_id, session_id)
    field = args['field']
    # Model previews wrap source fields in data; only unwrap when the source does not.
    if isinstance(root.get('result'), dict) and 'data' not in root['result'] and field.startswith('data.'):
        field = field[5:]
    elif field == 'data' and isinstance(root.get('result'), dict) and 'data' not in root['result']:
        field = ''
    seen = set()
    while requested['tool'] == 'read_tool_result' and (field == 'value' or field.startswith('value.')):
        if requested['id'] in seen:
            raise ValueError('Cyclic field reference')
        seen.add(requested['id'])
        parent_field = (requested.get('result') or {}).get('field') or requested['arguments'].get('field', '')
        field = parent_field + field[5:]
        requested = store.get_tool_log((requested.get('result') or {}).get('original_result_id') or requested['arguments']['log_id'])
    args.update(log_id=root['id'], field=field)
    return args


def source_reference(root):
    result = root['result']
    return {'result_id': root['id'], 'announcement_id': result.get('announcement_id') if isinstance(result, dict) else None,
        'task_id': root.get('task_id'), 'session_id': root.get('session_id'),
        'source_url': result.get('source_url') if isinstance(result, dict) else None}


def slice_reference(result):
    key = [result['original_result_id'], result.get('field'), result.get('offset', 0),
        result.get('end_offset'), result.get('matched')]
    return hashlib.sha256(json.dumps(key, separators=(',', ':')).encode()).hexdigest()


def accessible(log, task_id, session_id):
    return log and (log['session_id'] == session_id if session_id else log['task_id'] == task_id)


def original_log(store, log_id, task_id, session_id):
    seen = set()
    for _ in range(16):
        if log_id in seen:
            raise ValueError('Cyclic evidence reference')
        seen.add(log_id)
        log = store.get_tool_log(log_id)
        if not accessible(log, task_id, session_id):
            raise ValueError('Result is not accessible in this conversation')
        if log['tool'] != 'read_tool_result':
            return log
        parent = (log.get('result') or {}).get('original_result_id') or log['arguments'].get('log_id')
        if not parent:
            raise ValueError('Read result has no original evidence reference')
        log_id = parent
    raise ValueError('Evidence reference chain is too deep')


def page_reference(store, value, task_id, session_id):
    record = store.get(value)
    if record and record.get('kind') == 'announcement':
        return value
    log = store.get_tool_log(value)
    if not log:
        return value
    log = original_log(store, value, task_id, session_id)
    if log['tool'] not in ('fetch_tender_detail', 'render_tender_page', 'navigate_tender_entry'):
        raise ValueError('This log is not a page result; use its actual announcement_id')
    page_id = (log.get('result') or {}).get('announcement_id')
    if not page_id:
        raise ValueError('Page result has no announcement_id')
    return page_id


def read_result(store, log_id, task_id, session_id=None, field='', offset=0, limit=1000, contains='', token_limit=2000):
    if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 1000:
        raise ValueError('offset must be nonnegative and limit must be 1..1000')
    args = canonical_read(store, {'log_id': log_id, 'field': field, 'offset': offset, 'limit': limit, 'contains': contains}, task_id, session_id)
    field = args['field']
    root = original_log(store, log_id, task_id, session_id)
    value = root['result']
    fields = list(value) if isinstance(value, dict) else []
    base = {'original_result_id': root['id'], 'source_ref': source_reference(root), 'field': field,
        'notice': '使用相同 field 和 next_offset 继续读取 original_result_id。偏移量始终对应原始来源，不对应当前切片。'}
    base['slice_id'] = slice_reference({**base, 'offset': offset})
    if not field and isinstance(value, dict):
        return {**base, 'original_fields': fields, 'content_complete': True}
    try:
        for part in field.split('.') if field else []:
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f'Original field {field!r} does not exist on source log {root["id"]}. Available original fields: {fields}' + '。请使用此来源日志及列出的字段之一。') from exc
    if isinstance(value, str):
        limit = min(limit, max(1, token_limit // 2))
        if contains:
            match = value.find(contains, offset)
            if match < 0:
                return {**base, 'matched': False, 'notice': '此字段未找到关键词，不代表整个来源都没有相关内容。'}
            offset = max(offset, match - min(150, limit // 4))
        page = value[offset:offset + limit]
    elif isinstance(value, list):
        page = value[offset:offset + min(limit, 20)]
    else:
        if estimate_tokens({**base, 'value': value}) > token_limit:
            return {**base, 'value_omitted': True,
                'available_fields': list(value)[:30] if isinstance(value, dict) else [],
                'notice': '字段值过大，请从 original_result_id 读取具体子字段。'}
        return {**base, 'value': value, 'content_complete': True,
            'notice': '字段完整，请复用，不必继续读取此字段。'}
    while len(page) > 1 and estimate_tokens({**base, 'value': page}) > token_limit - 300:
        page = page[:-1]
    end = offset + len(page)
    result = {**base, 'value': page, 'offset': offset, 'end_offset': end, 'next_offset': end if end < len(value) else None, 'total': len(value)}
    result['content_complete'] = offset == 0 and end == len(value)
    if end == len(value):
        result['notice'] = '已到字段末尾。空数组是完整观察结果，不要重复读取。'
    if estimate_tokens(result) > token_limit:
        result.update(value=[], end_offset=offset, next_offset=offset, preview_only=True, content_complete=False,
            notice='单项过大，请从 original_result_id 读取具体子字段；偏移量仍对应原始来源。')
    result['slice_id'] = slice_reference(result)
    return result


def covered_read(store, arguments, log_ids, task_id, session_id=None):
    """Only complete a read checkpoint when observed ranges cover its exact source/field."""
    args = canonical_read(store, arguments, task_id, session_id)
    if args['contains']:
        return None
    root = original_log(store, args['log_id'], task_id, session_id)
    value = root['result']
    for part in args['field'].split('.') if args['field'] else []:
        value = value[int(part)] if isinstance(value, list) else value[part]
    ranges, used = [], []
    for ident in log_ids:
        log = store.get_tool_log(ident)
        if not accessible(log, task_id, session_id) or log['tool'] != 'read_tool_result' or log['status'] != 'completed':
            continue
        result = log.get('result') or {}
        if result.get('original_result_id') != root['id'] or result.get('field') != args['field'] or result.get('preview_only') or result.get('value_omitted'):
            continue
        if not isinstance(value, (list, str)):
            if result.get('content_complete'):
                return {**result, 'reused': True, 'covered_by_log_ids': [ident]}
            continue
        if 'value' not in result or not isinstance(result['value'], type(value)):
            continue
        start = result.get('offset', 0)
        end = start + len(result['value'])
        if result['value'] != value[start:end]:
            continue
        ranges.append((start, end))
        used.append(ident)
    if not isinstance(value, (list, str)):
        return None
    start = args['offset']
    if start > len(value):
        return None
    end = min(len(value), start + args['limit']) if 'limit' in arguments else len(value)
    cursor = start
    for left, right in sorted(ranges):
        if left <= cursor:
            cursor = max(cursor, right)
    if not ranges or cursor < end:
        return None
    result = {'original_result_id': root['id'], 'source_ref': source_reference(root), 'field': args['field'],
        'value': value[start:end], 'offset': start, 'end_offset': end, 'total': len(value),
        'next_offset': end if end < len(value) else None, 'content_complete': start == 0 and end == len(value),
        'reused': True, 'covered_by_log_ids': used}
    result['slice_id'] = slice_reference(result)
    return result
