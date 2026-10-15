"""Validate model assessments against observed sources, not industry keywords."""
import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

TZ = ZoneInfo('Asia/Shanghai')


class Judgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion_id: str
    verdict: str = Field(pattern=r'^(match|mismatch|unknown)$')
    reason: str = Field(min_length=1, max_length=800)
    quotes: list[str] = Field(default_factory=list, max_length=4)


class Document(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str
    document_type: str = Field(pattern=r'^(tender_document|announcement_document|prequalification_notice|other_pdf|other_file|unknown)$')
    quote: str = Field(min_length=1, max_length=600)


def fingerprint(page):
    fields = {key: page.get(key) for key in ('source_url', 'title', 'project_name', 'text', 'published_at', 'purchaser')}
    if page.get('page_type') == 'listing':
        fields['entries'] = page.get('entries', [])
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized(value):
    return re.sub(r'\s+', '', str(value or ''))


class AssessmentEvidenceError(ValueError):
    def __init__(self, message, page, judgment):
        super().__init__(message)
        corpus = normalized(' '.join(str(page.get(key) or '') for key in
            ('title', 'project_name', 'text', 'purchaser', 'published_at')))
        self.evidence_repair = {
            'announcement_id': page.get('id') or page.get('announcement_id'),
            'criterion_id': judgment['criterion_id'],
            'invalid_quotes': [quote for quote in judgment['quotes'] if not quote.strip() or normalized(quote) not in corpus],
            'source_fields': {key: page.get(key) for key in ('title', 'published_at', 'source_url')},
            'source_text_excerpt': (page.get('text') or '')[:1800],
            'text_complete': len(page.get('text') or '') <= 1800 and not page.get('text_truncated'),
            'observed_attachments': [{key: item.get(key) for key in ('url', 'name')} for item in page.get('attachments', [])][:6],
            'next_action': 'correct_quotes_and_select_files_from_this_saved_page_without_navigation',
        }


def validate_assessment(page, spec, judgments, summary, files=None, valid_until=None, entry_id=None):
    source_page = page
    if spec.get('action') == 'research' and spec.get('file_goal') == 'none' and files:
        raise ValueError('Project-only assessment does not select files')
    if entry_id is not None:
        if page.get('page_type') != 'listing':
            navigation = page.get('navigation') or {}
            if page.get('page_type') == 'detail' and navigation.get('parent_announcement_id') and navigation.get('entry_id') == entry_id:
                entry_id = None
            else:
                raise ValueError('Detail-page assessment must omit entry_id; use the detail announcement_id and its original text. entry_id is only for an entry on a listing page.')
    if entry_id is not None:
        if page.get('page_type') != 'listing' or spec.get('action') != 'research' or spec.get('file_goal') != 'none':
            raise ValueError('Listing-entry assessment is only available for project-only research')
        entry = next((item for item in page.get('entries', []) if item.get('entry_id') == entry_id), None)
        if not entry or not entry.get('url') or not entry.get('title'):
            raise ValueError('Listing entry needs an observed project URL and title; navigate for missing evidence')
        page = {**entry, 'source_url': entry['url'], 'page_type': 'detail',
            'text': ' '.join(str(value) for key, value in entry.items() if key not in ('entry_id', 'url', 'selector'))}
    if page.get('page_type') != 'detail':
        raise ValueError('Assessment requires an observed project detail page, not a listing or search snippet')
    parsed = [Judgment.model_validate(item).model_dump() for item in judgments]
    expected = {item['id'] for item in spec['criteria']}
    if len(parsed) != len(expected) or {item['criterion_id'] for item in parsed} != expected:
        raise ValueError('Assess every task criterion exactly once')
    corpus = normalized(' '.join(str(page.get(key) or '') for key in ('title', 'project_name', 'text', 'purchaser', 'published_at')))
    for judgment in parsed:
        if judgment['verdict'] == 'match' and not judgment['quotes']:
            raise AssessmentEvidenceError('A match needs original-source evidence', page, judgment)
        if any(not quote.strip() or len(quote) > 600 or normalized(quote) not in corpus for quote in judgment['quotes']):
            raise AssessmentEvidenceError('Evidence quotation is not present in the observed source; current page '
                + str(source_page.get('id') or source_page.get('announcement_id') or '') + ': ' + str(page.get('title') or '')
                + '. Select quotes from this exact page; do not move another project quote here or navigate again if its evidence is saved.', page, judgment)
    documents = [Document.model_validate(item).model_dump() for item in files or []]
    attachments = {item['url']: item for item in page.get('attachments', [])}
    if len({item['url'] for item in documents}) != len(documents):
        raise ValueError('Duplicate document decisions')
    for document in documents:
        attachment = attachments.get(document['url'])
        if not attachment or normalized(document['quote']) not in corpus + normalized(attachment.get('name')):
            raise ValueError('Document decision must cite an observed attachment, not the detail-page URL. '
                + 'Use the exact attachments[].url (including browser-attachment: IDs) and its name as quote. Observed attachments: '
                + json.dumps([{'url': item['url'], 'name': item.get('name')} for item in page.get('attachments', [])], ensure_ascii=False)[:2000])
        document['attachment_key'] = attachment.get('href') or attachment.get('name') or attachment['url']
        if document['document_type'] == 'announcement_document' and spec.get('file_goal') == 'tender_document' and spec.get('fallback_file_goal') != 'announcement':
            raise ValueError('Announcement fallback was not authorized for this tender-document goal')
    if all(item['verdict'] == 'match' for item in parsed) and any(item.get('requires_expiry') for item in spec['criteria']) and not valid_until:
        raise ValueError('Time-sensitive matching needs a quoted expiry/deadline')
    if valid_until:
        expiry = datetime.fromisoformat(valid_until)
        if expiry.tzinfo is None:
            raise ValueError('valid_until requires an explicit timezone')
        dates = []
        for item in parsed:
            for quote in item['quotes']:
                for match in re.finditer(r'(\d{4})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})(?:日)?[T\s]*(\d{1,2})\s*[:时]\s*(\d{1,2})(?:\s*[:分]\s*(\d{1,2})(?:秒)?)?', quote):
                    dates.append(tuple(int(part) if part else 0 for part in match.groups()))
        if (expiry.year, expiry.month, expiry.day, expiry.hour, expiry.minute, expiry.second) not in dates:
            raise ValueError('Expiry must be supported by a quoted source date and time. Omit optional valid_until when no criterion requires expiry; otherwise cite the matching deadline in judgments.quotes.')
        if all(item['verdict'] == 'match' for item in parsed) and expiry <= datetime.now(TZ):
            raise ValueError('Expiry is already past; reassess the time-sensitive requirement')
    return {'eligible': all(item['verdict'] == 'match' for item in parsed), 'judgments': parsed,
        'summary': summary[:1500], 'source_url': page['source_url'], 'source_fingerprint': fingerprint(source_page),
        'evidence_kind': 'listing_entry' if entry_id is not None else 'detail', 'entry_id': entry_id,
        'title': page.get('project_name') or page.get('title'),
        'criteria': spec['criteria'], 'files': documents, 'valid_until': valid_until,
        'checked_at': datetime.now(TZ).isoformat()}


def validate_checkpoint(page, evaluation):
    if not evaluation or not evaluation.get('eligible'):
        raise ValueError('Project has no evidence-backed matching assessment')
    if fingerprint(page) != evaluation['source_fingerprint']:
        raise ValueError('公告原文已变化，需要模型重新核验，不自动下载')
    if evaluation.get('valid_until') and datetime.fromisoformat(evaluation['valid_until']) <= datetime.now(TZ):
        raise ValueError('原核验的有效期已过，需要重新核验')


def matching_answer(state):
    spec = state.get('spec', {})
    projects = state.get('qualified_projects', [])[:spec.get('expected_count') or 5]
    lines = [f'已依据公告原文核实匹配项目：{len(projects)} 个。']
    for page in projects:
        lines.append(f"- [{page.get('title') or '项目公告'}]({page['source_url']})：{page['eligibility']['summary']}")
    expected = spec.get('expected_count')
    if expected and len(projects) < expected:
        lines.append(f'尚缺 {expected - len(projects)} 个符合要求的项目，不使用未核实或不匹配的线索补数。')
    if spec.get('action') == 'download':
        if spec.get('file_goal') == 'tender_document':
            ready = sum(any(item.get('document_type') == 'tender_document' for item in page['eligibility'].get('files', [])) for page in projects)
            lines.append(f'完整标书文件已核实可选的项目：{ready} 个。项目条件符合不代表已取得标书。')
            if spec.get('fallback_file_goal') == 'announcement':
                fallback = sum(bool(page['eligibility'].get('fallback_used')) for page in projects)
                lines.append(f'按用户授权改选公告附件的项目：{fallback} 个；公告附件不是完整标书。')
        lines.append('已建立下载任务，尚未完成下载，实际文件及未完成项见任务记录。' if state.get('batch_id') else '尚未完成文件下载。')
        for barrier in state.get('document_access_barriers', [])[:5]:
            lines.append(f"文件获取受限：[{barrier.get('title') or '项目公告'}]({barrier['source_url']}) 的文件入口要求登录、验证码或其他用户操作，尚未取得可下载的完整标书。")
    if not projects:
        lines.append('暂未取得足够的原文证据，不能确认项目符合要求。')
    return '\n\n'.join(lines)
