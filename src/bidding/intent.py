"""Separate human requests, accepted constraints, and disposable execution memory."""
import copy
import hashlib
import json
import re
from datetime import date


def user_request_record(message, request_id):
    return {'source': 'user', 'request_id': request_id, 'text': message,
        'sha256': hashlib.sha256(message.encode('utf-8')).hexdigest()}


def explicit_month_boundary(message, today=None):
    match = re.search(r'(?:(\d{4})年\s*)?(\d{1,2})\s*月(?:份)?\s*(?:以前|之前)', message)
    if not match or not 1 <= int(match[2]) <= 12:
        return None
    year = int(match[1] or (today or date.today()).year)
    return {'source_quote': match[0], 'field': 'month_boundary', 'operator': 'lt',
        'value': date(year, int(match[2]), 1).isoformat(),
        'year_basis': 'explicit' if match[1] else 'current_year_assumption'}


def validate_date_alignment(spec, message, today=None):
    if spec.get('action', 'download') not in ('download', 'research'):
        return
    boundary = explicit_month_boundary(message, today)
    if not boundary:
        return
    publication = [item for item in spec.get('criteria', [])
        if re.search(r'发布时间|发布日期|发布.*日期', item['description'])]
    for item in publication:
        if item.get('requires_expiry'):
            raise ValueError('Publication-date filtering is not ongoing validity: requires_expiry must be false. '
                + json.dumps(boundary, ensure_ascii=False))
    for text in [spec.get('goal', ''), *(item['description'] for item in publication)]:
        for value in re.finditer(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日)?', text):
            observed = date(*map(int, value.groups())).isoformat()
            if observed != boundary['value']:
                raise ValueError('Plan date contradicts the original month boundary; do not substitute today. '
                    + json.dumps(boundary, ensure_ascii=False))


def accepted_contract(spec, request, today=None):
    return {'source': 'accepted_user_constraints', 'version': 1, 'request_id': request['request_id'],
        'request_sha256': request['sha256'],
        'constraints': copy.deepcopy({key: value for key, value in spec.items()
            if key not in ('steps', 'questions', 'clarification_kind')}),
        'date_boundary': explicit_month_boundary(request['text'], today),
        'update_policy': 'Only a new human request may change scope; summaries and assessments are not authorization'}


def authority_packet(task):
    return {'user_request': task.get('user_request'), 'related_user_requests': task.get('related_user_requests', []),
        'task_constraints': task.get('intent_contract'),
        'execution_state': {'confirmation_status': (task.get('confirmation') or {}).get('status'),
            'denied_proposals': task.get('denied_proposals', [])}}
