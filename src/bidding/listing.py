"""Site-independent list identity and conservative publication-date prescreening."""
import hashlib
import json
from datetime import date


def listing_fingerprint(entries):
    rows = [{key: item.get(key) for key in ('title', 'url', 'project_number', 'listed_date')} for item in entries]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prescreen_entries(page, published_before=None):
    cutoff = date.fromisoformat(published_before) if published_before else None
    selected, rejected = [], []
    for entry in page.get('entries', []):
        row = {**entry, 'announcement_id': page['id'], 'screening': 'candidate' if not cutoff else 'needs_evidence'}
        try:
            value = date.fromisoformat(entry.get('listed_date') or '')
        except ValueError:
            value = None
        if cutoff and value and entry.get('date_kind') == 'published_at':
            row['screening'] = 'candidate' if value < cutoff else 'excluded'
            row['reason'] = 'Observed publication date compared with original cutoff'
        (rejected if row['screening'] == 'excluded' else selected).append(row)
    return selected, rejected


async def scan_listing(store, announcement_id, advance, published_before=None, max_pages=5):
    pages, candidates, unresolved, excluded, seen, row_keys = [], [], [], 0, set(), set()
    current_id = announcement_id
    reason = 'page_limit'
    for _ in range(max(1, min(10, max_pages))):
        page = store.get(current_id)
        if not page or page.get('page_type') != 'listing':
            raise ValueError('Scan requires an observed listing page')
        fingerprint = listing_fingerprint(page.get('entries', []))
        if fingerprint in seen:
            reason = 'repeated_page'
            break
        seen.add(fingerprint)
        selected, rejected = prescreen_entries(page, published_before)
        for row in [*selected, *rejected]:
            key = (row.get('url'), row.get('title'), row.get('project_number'), row.get('listed_date'))
            if key in row_keys:
                continue
            row_keys.add(key)
            if row['screening'] == 'excluded':
                excluded += 1
            elif row['screening'] == 'candidate':
                candidates.append(row)
            else:
                unresolved.append(row)
        pages.append({'announcement_id': current_id, 'fingerprint': fingerprint,
            'entry_count': len(page.get('entries', [])), 'excluded_count': len(rejected),
            'mechanisms': [item.get('mechanism', 'link' if item.get('url') else 'click') for item in page.get('pagination', [])]})
        if candidates:
            reason = 'candidates_ready'
            break
        controls = page.get('pagination', [])
        if not controls:
            reason = 'no_observed_next_action'
            break
        if len(controls) != 1:
            reason = 'ambiguous_next_action'
            break
        if len(pages) >= max(1, min(10, max_pages)):
            break
        result = await advance(current_id, controls[0]['entry_id'])
        if result.get('error'):
            return {'pages': pages, 'candidates': candidates[:30], 'unresolved_candidates': unresolved[:30], 'unresolved_count': len(unresolved), 'excluded_count': excluded,
                'stop_reason': 'pagination_failed', 'failure': result, 'announcement_id': current_id}
        current_id = result['announcement_id']
    return {'pages': pages, 'candidates': candidates[:30], 'candidate_count': len(candidates),
        'unresolved_candidates': unresolved[:30], 'unresolved_count': len(unresolved),
        'unresolved_complete': len(unresolved) <= 30,
        'candidates_complete': len(candidates) <= 30, 'excluded_count': excluded,
        'announcement_id': current_id, 'stop_reason': reason,
        'notice': 'candidates 为初筛通过，unresolved_candidates 为待补证，不得混计。日期字段未识别时继续有界翻页，不因未知条目就绪而提前结束；待补证行仍保留在原始结果中。字段只从本扫描结果读取；当前页面 entries/pagination 应读取对应页面日志。'}
