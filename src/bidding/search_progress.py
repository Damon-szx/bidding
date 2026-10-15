import os
import time

from pydantic import BaseModel, ConfigDict, Field
from bidding.model import estimate_tokens


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_url: str
    verdict: str = Field(pattern='^(worth_verifying|excluded)$')
    reason: str = Field(min_length=1, max_length=500)


class SearchProgress:
    def __init__(self, store, state, task_id, session_id, model, deadline):
        self.store, self.state, self.task_id, self.session_id = store, state, task_id, session_id
        self.model, self.deadline = model, deadline
        self.data = state.setdefault('search_progress', {'calls': 0, 'limit': max(1, int(os.getenv('AGENT_MAX_SEARCH_CALLS', '6'))),
            'extensions': 0, 'queries': [], 'decisions': {}, 'checked_calls': 0, 'no_progress_streak': 0})

    def save(self, kind, payload):
        self.store.record_context(kind, payload, self.task_id, self.session_id)
        self.store.save('task', self.state, self.task_id)

    def view(self):
        decided = {item['source_url'] for key in ('qualified_projects', 'rejected_projects') for item in self.state.get(key, [])}
        pending = [url for url, value in self.data['decisions'].items()
            if value['verdict'] == 'worth_verifying' and url not in decided]
        unclassified = [item['source_url'] for item in self.state.get('candidates', [])
            if item.get('source_url') and item['source_url'] not in self.data['decisions']]
        return {**self.data, 'remaining_searches': max(0, self.data['limit'] - self.data['calls']),
            'unclassified_candidates': unclassified,
            'pending_verification': pending, 'verified_count': len(self.state.get('qualified_projects', [])),
            'expected_count': self.state.get('spec', {}).get('expected_count'),
            'candidate_count': len(self.state.get('candidates', []))}

    def can_search(self):
        view = self.view()
        enough = view['expected_count'] and view['verified_count'] >= view['expected_count']
        return not enough and not self.data.get('partial_check') and self.data['calls'] < self.data['limit'] and self.data['calls'] - self.data['checked_calls'] < 2 and not view['pending_verification'] and (self.data['no_progress_streak'] < 2 or bool(self.data.get('approved_strategy')))

    def model_view(self):
        """Bound prompt data without discarding durable candidates or search decisions."""
        view = self.view()
        view['decisions'] = {url: {**item, 'reason': item.get('reason', '')[:160]}
            for url, item in list(view['decisions'].items())[-12:]}
        view['unclassified_count'] = len(view['unclassified_candidates'])
        view['pending_verification_count'] = len(view['pending_verification'])
        view['unclassified_candidates'] = view['unclassified_candidates'][:12]
        view['pending_verification'] = view['pending_verification'][:12]
        view['gap'] = str(view.get('gap', ''))[:500]
        view['queries'] = view['queries'][-6:]
        while estimate_tokens(view) > 3500 and view['decisions']:
            view['decisions'].pop(next(iter(view['decisions'])))
        return view

    def candidate_view(self):
        pending = set(self.view()['pending_verification'])
        candidates = sorted(self.state.get('candidates', []), key=lambda item:
            0 if item.get('source_url') in pending else 2 if item.get('source_url') in self.data['decisions'] else 1)
        result = []
        for item in candidates:
            projected = {key: item.get(key) for key in ('candidate_id', 'source_url', 'published_at')}
            projected.update(title=str(item.get('title', ''))[:200], snippet=str(item.get('snippet', ''))[:300])
            if len(result) >= 12 or estimate_tokens([*result, projected]) > 3500:
                break
            result.append(projected)
        return {'items': result, 'total': len(candidates), 'partial': len(result) < len(candidates),
            'notice': '这里只是有界候选工作集，不是完整搜索结果。其余候选保留在原始搜索日志，按需读取。'}

    def consume(self, query):
        if self.deadline - time.monotonic() < 30 or self.model.budget - self.model.charged < int(os.getenv('SEARCH_RESERVED_TOKENS', '8000')):
            raise ValueError('保留核验和交付预算，不再开展新搜索。')
        if not self.can_search():
            raise ValueError('先整理本轮线索并核验待处理候选；额度耗尽时由主 Agent 申请有界补搜。')
        normalized = ' '.join(query.lower().split())
        if normalized in self.data['queries']:
            raise ValueError('检索式已执行，请复用原结果或改变策略，不重复消耗额度。')
        self.data['queries'].append(normalized)
        self.data['calls'] += 1
        self.save('search_budget', {'status': 'consumed', **self.view()})

    def checkpoint(self, decisions, gap, next_action):
        if next_action not in ('verify', 'search', 'stop') or not isinstance(gap, str) or len(gap) > 2000:
            raise ValueError('Invalid search checkpoint')
        known = {item.get('source_url') for item in self.state.get('candidates', []) if item.get('source_url')}
        parsed = [CandidateDecision.model_validate(item).model_dump() for item in decisions]
        urls = [item['source_url'] for item in parsed]
        if len(urls) != len(set(urls)) or any(url not in known for url in urls):
            raise ValueError('Decisions must reference distinct observed candidate URLs')
        merged = {**self.data['decisions'], **{item['source_url']: item for item in parsed}}
        useful = self.data.get('batch_useful', False) or any(item['verdict'] == 'worth_verifying'
            and self.data['decisions'].get(item['source_url'], {}).get('verdict') != 'worth_verifying' for item in parsed)
        missing = known - set(merged)
        if missing and not parsed:
            raise ValueError('请整理所有尚未判断的候选；可以增量提交，但不能将遗漏线索当作不存在。')
        if missing:
            self.data.update(decisions=merged, batch_useful=useful, partial_check=True, gap=gap, next_action='verify')
            self.save('search_checkpoint', {'status': 'partial_completed', **self.view()})
            return self.view()
        if self.data['checked_calls'] != self.data['calls']:
            self.data['no_progress_streak'] = 0 if useful else self.data['no_progress_streak'] + 1
        self.data['batch_useful'] = False
        self.data.update(decisions=merged, partial_check=False, checked_calls=self.data['calls'], gap=gap, next_action=next_action)
        self.save('search_checkpoint', {'status': 'completed', **self.view()})
        return self.view()

    def extend(self, gap, strategy, reason):
        view = self.view()
        count = self.state.get('spec', {}).get('expected_count')
        if count and view['verified_count'] >= count:
            raise ValueError('已达到核验项目数量，不追加搜索额度；继续处理文件或交付。')
        if view['remaining_searches'] or view['pending_verification'] or self.data['checked_calls'] != self.data['calls']:
            raise ValueError('先整理并处理现有候选，不能提前追加额度。')
        if not all(isinstance(value, str) and value.strip() and len(value) <= 2000 for value in (gap, strategy, reason)):
            raise ValueError('追加申请须说明目标缺口、新策略和现有线索为何不足。')
        strategies = self.data.setdefault('extension_strategies', [])
        if strategy.strip() in strategies or self.data['extensions'] >= min(3, max(0, int(os.getenv('SEARCH_MAX_EXTENSIONS', '3')))):
            raise ValueError('追加上限已达或策略重复，请整理未完成项。')
        if self.data['no_progress_streak'] >= 2 and self.data.get('approved_strategy') != strategy:
            raise ValueError('连续检索没有新增值得核验的线索，需要反思后再决定是否改变策略。')
        if self.deadline - time.monotonic() < 30 or self.model.budget - self.model.charged < int(os.getenv('SEARCH_RESERVED_TOKENS', '8000')):
            raise ValueError('剩余时间或 Token 不足，保留核验、恢复和交付预算，不再追加。')
        self.data['extensions'] += 1
        self.data['limit'] += max(1, min(4, int(os.getenv('SEARCH_EXTENSION_CALLS', '2'))))
        strategies.append(strategy.strip())
        if self.data.pop('approved_strategy', None) == strategy:
            self.data['no_progress_streak'] = 0
        self.save('search_extension', {'status': 'approved', 'gap': gap, 'strategy': strategy, 'reason': reason, **self.view()})
        return self.view()
