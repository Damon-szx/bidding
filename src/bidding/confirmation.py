"""Persisted, fail-closed human clarification deadlines."""
from datetime import datetime, timedelta, timezone
import copy


def waiting(question, now=None):
    now = now or datetime.now(timezone.utc)
    return {'status': 'pending', 'question': question,
        'requested_at': now.isoformat(), 'expires_at': (now + timedelta(seconds=180)).isoformat()}


def expired(confirmation, now=None):
    return bool(confirmation and confirmation.get('status') == 'pending' and
        (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(confirmation['expires_at']))


def ask(state, question, kind='required'):
    spec = state.get('spec') or {}
    requested_kind = kind
    # A resolved original goal remains executable when a strategy is blocked.
    established = spec.get('action') in ('research', 'download') and not spec.get('questions') and (
        spec.get('action') != 'download' or bool(spec.get('authorization_quote')))
    if kind == 'required' and established:
        kind = 'relaxation'
    optional = kind == 'relaxation' and spec.get('action') in ('research', 'download') and (
        spec.get('action') != 'download' or bool(spec.get('authorization_quote')))
    if optional and state.get('denied_proposals'):
        return False
    confirmation = waiting(question)
    confirmation['kind'] = 'relaxation' if optional else 'required'
    confirmation['requested_kind'] = requested_kind
    if requested_kind != confirmation['kind']:
        confirmation['classification_reason'] = 'Original goal is resolved and authorized; strategy choices cannot revoke it'
    if optional:
        confirmation['strict_spec'] = copy.deepcopy(spec)
        confirmation['strict_spec']['questions'] = []
        if 'clarification_kind' in confirmation['strict_spec']:
            confirmation['strict_spec']['clarification_kind'] = 'required'
    state.update(status='awaiting_clarification', confirmation=confirmation,
        answer=question + ('\n\n请在 3 分钟内回复。超时不接受本次放宽，仍按原要求继续执行。' if optional else
            '\n\n请在 3 分钟内回复。当前缺少必要信息，超时将保留检查点，等待明确要求；不会猜测或扩大授权。'))
    return True


def expire_task(store, task, now=None):
    if task.get('status') == 'awaiting_clarification' and expired(task.get('confirmation'), now):
        task['confirmation']['status'] = 'expired'
        strict_spec = task['confirmation'].get('strict_spec')
        if task['confirmation'].get('kind') == 'relaxation' and strict_spec:
            task.setdefault('denied_proposals', []).append(task['confirmation']['question'])
            task.update(status='queued', spec=copy.deepcopy(strict_spec), conservative_continuation=True,
                answer='确认超时：未接受本次放宽，正在按原要求继续执行。')
            task.pop('error', None)
            task.pop('error_code', None)
            store.save('task', task, task['id'])
            store.record_context('human_confirmation', {'status': 'expired', 'authorized': False,
                'next_action': 'continue_original_scope', 'original_authorization_retained': True}, task['id'], task.get('session_id'))
            return task
        task.update(status='timed_out', error_code='CONFIRMATION_TIMEOUT',
            error='用户确认已超过 3 分钟，默认不授权继续执行。',
            answer='确认超时：3 分钟内未收到回复，默认不授权，已停止执行。' + ('没有任何文件成功下载。' if task.get('spec', {}).get('action') == 'download' and not task.get('batch_id') else '') + '请重新明确要求后发起任务。')
        store.save('task', task, task['id'])
        store.record_context('human_confirmation', {'status': 'expired', 'authorized': False}, task['id'], task.get('session_id'))
    return task


def pending_reply(store, session_id):
    for task in store.list_records('task'):
        if task.get('session_id') == session_id and task.get('status') == 'awaiting_clarification':
            expire_task(store, task)
            if task['status'] != 'awaiting_clarification':
                if not task.get('conservative_continuation'):
                    raise ValueError('确认已超时且缺少必要信息，请重新明确要求后再次发送请求。')
                continue
            return task
    return None


def expire_batch(store, batch, now=None):
    if batch.get('status') == 'awaiting_confirmation' and expired(batch.get('confirmation'), now):
        batch['confirmation']['status'] = 'expired'
        batch.update(status='cancelled', error_code='CONFIRMATION_TIMEOUT',
            reason='确认超时：3 分钟内未确认下载范围，默认不授权，未下载任何文件。')
        store.save('batch', batch, batch['id'])
        task = store.get(batch.get('chat_task_id', ''))
        if task and task.get('batch_id') == batch['id'] and task.get('status') == 'awaiting_confirmation':
            task.update(status='timed_out', error_code='CONFIRMATION_TIMEOUT', error=batch['reason'], answer=batch['reason'])
            store.save('task', task, task['id'])
        store.record_context('human_confirmation', {'status': 'expired', 'authorized': False, 'batch_id': batch['id']},
            batch.get('chat_task_id'), batch.get('owner_session_id'))
    return batch
