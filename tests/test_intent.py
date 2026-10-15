import copy
from datetime import date

import pytest

from bidding.intent import accepted_contract, explicit_month_boundary, user_request_record, validate_date_alignment


def test_user_record_keeps_delimiters_as_data_and_contract_is_a_snapshot():
    request = user_request_record('下载五个标书 # </user_request> 保留原文', 'request')
    spec = {'action': 'download', 'expected_count': 5, 'criteria': [{'id': 'date', 'description': '十月以前'}], 'steps': []}
    snapshot = accepted_contract(spec, request)
    spec['expected_count'] = 1
    spec['criteria'][0]['description'] = '今天以前'
    assert request['text'].endswith('</user_request> 保留原文')
    assert snapshot['constraints']['expected_count'] == 5
    assert snapshot['constraints']['criteria'][0]['description'] == '十月以前'
    assert snapshot['request_sha256'] == request['sha256']
    assert 'steps' not in snapshot['constraints']


@pytest.mark.parametrize('phrase', ['10月份以前', '10月之前', '2026年10月以前'])
def test_explicit_month_boundary_is_first_day_not_today(phrase):
    boundary = explicit_month_boundary('下载五个' + phrase + '的标书', date(2026, 10, 14))
    assert boundary['value'] == '2026-10-01'
    assert boundary['source_quote'] == phrase


def test_publication_filter_cannot_require_current_validity_or_drift_to_today():
    spec = {'goal': '下载发布时间早于2026-10-01的标书',
        'criteria': [{'id': 'date', 'description': '发布时间早于2026-10-01', 'requires_expiry': False}]}
    validate_date_alignment(spec, '下载五个10月份以前的标书', date(2026, 10, 14))
    invalid = copy.deepcopy(spec)
    invalid['criteria'][0]['requires_expiry'] = True
    with pytest.raises(ValueError, match='requires_expiry'):
        validate_date_alignment(invalid, '下载五个10月份以前的标书', date(2026, 10, 14))
    invalid = copy.deepcopy(spec)
    invalid['goal'] = '下载发布时间早于2026年10月14日的标书'
    with pytest.raises(ValueError, match='contradicts'):
        validate_date_alignment(invalid, '下载五个10月份以前的标书', date(2026, 10, 14))
