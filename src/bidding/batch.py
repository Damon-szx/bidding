"""Durable manifests with bounded, deterministic acquisition workers (single API process)."""
import asyncio
import hashlib
import os
import re
import time

from bidding.acquisition import Acquisition, DownloadCapacityExceeded
from bidding.tool_runtime import ToolRuntime
from bidding.file_policy import NonPDFFile, split_attachments, SUPPORTED_FORMATS
from bidding.eligibility import validate_checkpoint
from bidding.retry import failure
from bidding.confirmation import expire_batch, waiting as confirmation_waiting


class BatchLimit(ValueError):
    pass


def identity(entry):
    # Browser action IDs are ephemeral; use business identity when resuming.
    value = entry.get('url') or ((entry.get('project_number') or '') + '|' + entry.get('title', ''))
    return hashlib.sha256(value.encode()).hexdigest()


def batch_view(store, batch_id):
    batch = store.get(batch_id)
    if not batch or batch['kind'] != 'batch':
        raise ValueError('Batch not found')
    expire_batch(store, batch)
    counts = store.item_counts(batch_id)
    return {**batch, 'counts': counts, 'processed': sum(counts.get(key, 0) for key in
        ('downloaded', 'no_attachment', 'needs_input', 'failed')), 'status_url': '/batches/' + batch_id}


def scope_answer(batch):
    contract = batch['contract']
    if batch['status'] == 'awaiting_confirmation':
        total = contract.get('observed_total')
        size = f'页面显示约 {total} 个项目' if total else '该页面包含多个项目，完整数量尚未确认'
        filters = '筛选条件需要先核验具体项目；确认数量不会取消这些条件。' if contract.get('requested_filter') else ''
        formats = '、'.join(value.upper() for value in contract['file_formats'])
        return f'{size}。请在 3 分钟内确认下载范围：先处理列表前 3 个项目、指定数量，还是全量处理？超时默认不授权。尚未开始下载。{filters}允许格式：{formats}，公告附件不等同于完整招标文件。'
    target = '全量项目' if contract['scope'] == 'all' else f"列表前 {contract['count']} 个项目" if contract['page_type'] == 'listing' else '该项目'
    formats = '、'.join(value.upper() for value in contract['file_formats'])
    return f'已创建附件下载任务，范围：{target}，允许格式：{formats}（按页面当前顺序，不保证是发布时间排序）。执行进度和文件会显示在任务区域。任务尚未完成；无匹配附件、需人工处理和失败的项目将分别统计。'


class BatchManager:
    def __init__(self, store, factory=Acquisition):
        self.store, self.factory = store, factory
        self.scheduler = None
        self.runners = {}
        self.closed = False
        self.control_lock = asyncio.Lock()

    def create(self, page_id, scope, count, message, task_id, session_id, targets=None, criteria=None, require_documents=False, attachment_urls=None):
        if targets:
            for target in targets:
                original = self.store.get(target['announcement_id'])
                assessment = self.store.get(target.get('assessment_id', ''))
                if not original or not assessment or assessment['kind'] != 'assessment' or assessment.get('task_id') != task_id or assessment.get('announcement_id') != target['announcement_id']:
                    raise ValueError('下载项目没有本任务的原文核验记录')
                validate_checkpoint(original, assessment)
                target['eligibility'] = assessment
        page = self.store.get(page_id)
        if not page or page['kind'] != 'announcement':
            raise ValueError('Read the source page first')
        if scope not in ('unspecified', 'count', 'all'):
            raise ValueError('Unknown scope')
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5000:
            raise ValueError('count must be 1..5000')
        task = self.store.get(task_id)
        if task and task.get('batch_id'):
            return batch_view(self.store, task['batch_id'])
        listing = page.get('page_type') == 'listing' or bool(page.get('entries'))
        if not listing and page.get('page_type') == 'unresolved':
            raise ValueError('No project or listing was confirmed; inspect the page before scheduling')
        total = page.get('total_projects')
        entries = page.get('entries', [])
        formats = (criteria or {}).get('file_formats', ['pdf'])
        if not formats or set(formats) - set(SUPPORTED_FORMATS):
            raise ValueError('Unsupported requested file formats')
        selected_keys = None
        if attachment_urls is not None:
            observed = {item['url']: item for item in page.get('attachments', [])}
            if listing or not attachment_urls or len(set(attachment_urls)) != len(attachment_urls) or not set(attachment_urls) <= observed.keys():
                raise ValueError('Select unique observed attachment URLs from this detail page')
            selected_keys = [observed[url].get('href') or observed[url].get('name') or url for url in attachment_urls]
        large = listing and (bool(page.get('pagination')) or total is None or total > 5 or len(entries) > 5)
        explicit_all = bool(re.search(r'全部|全量|所有项目|所有标|全站|\ball\b', message, re.I)) and not bool(re.search(r'不要全部|不[要必用]?全量|不用全部|只.*(?:个|条)', message))
        # A model's proposed large scope is not user authorization. Otherwise require UI confirmation.
        explicit_count = bool(re.search(rf'(?:下载|处理|获取|前|最近)\s*(?:最新)?\s*{count}\s*(?:个|条|项|份)', message)) or (count <= 5 and any(word in message for word in
            {1: ('一个', '一条', '第一个'), 2: ('两个', '两条'), 3: ('三个', '三条'), 4: ('四个', '四条'), 5: ('五个', '五条')}[count]))
        waiting = listing and ((scope == 'unspecified' and large) or
            (scope == 'all' and not explicit_all) or (scope == 'count' and count > 5 and not explicit_count))
        requested_filter = bool(listing and criteria and criteria.get('criteria'))
        if requested_filter:
            # Workers do not silently ignore semantic filters that have not been implemented.
            waiting = True
        if not listing:
            scope, count = 'count', 1
        elif scope == 'unspecified':
            scope, count = 'count', 3
        contract = {'goal': 'download_tender_document' if require_documents else 'download_public_files', 'file_formats': formats,
            'selected_attachment_keys': selected_keys, 'scope': scope, 'count': count,
            'source_url': page['source_url'], 'page_type': 'listing' if listing else 'detail',
            'observed_total': total, 'order': 'source_page_order', 'filter': None,
            'requested_filter': message if requested_filter else None,
            'completion': 'Every scoped project has a recorded outcome; downloaded means validated files, not verified full tender documents.',
            'max_projects': 5000, 'max_pages': 200, 'max_files_per_project': 30,
            'concurrency': max(1, min(3, int(os.getenv('BATCH_CONCURRENCY', '2')))),
            'timeout_seconds': max(10, min(86400, int(os.getenv('BATCH_TIMEOUT_SECONDS', '1800')))),
            'max_bytes': max(1, min(10 * 1024**3, int(os.getenv('BATCH_MAX_BYTES', str(2 * 1024**3))))),
            'max_attempts': 3, 'model_calls_in_workers': 0, 'require_documents': require_documents,
            'fallback_file_goal': (criteria or {}).get('fallback_file_goal', 'none'),
            'files_per_project': (criteria or {}).get('files_per_project')}
        if targets:
            contract.update(targets=[{'title': item['title'], 'url': item['source_url'], 'assessment': item['eligibility']} for item in targets],
                criteria=criteria, count=len(targets), requested_count=criteria.get('expected_count') or len(targets))
        payload = {'status': 'awaiting_confirmation' if waiting else 'queued', 'contract': contract,
            'owner_session_id': session_id, 'chat_task_id': task_id, 'enumeration_complete': False,
            'discovered': 0, 'bytes_downloaded': 0, 'elapsed_seconds': 0, 'pages_scanned': 0,
            'control_events': [], 'reason': '当前批量流程不自动筛选地区或日期；确认后按页面顺序处理。' if requested_filter else None}
        if waiting:
            payload['confirmation'] = confirmation_waiting('请确认下载范围')
        batch_id = self.store.save('batch', payload)
        if task:
            self.store.save('task', {**task, 'batch_id': batch_id, 'contract': contract}, task_id)
        self.store.record_context('batch_plan', {'batch_id': batch_id, 'contract': contract, 'status': payload['status']}, task_id, session_id)
        if not waiting:
            self.ensure_scheduler()
        return batch_view(self.store, batch_id)

    def ensure_scheduler(self):
        if not self.closed and (not self.scheduler or self.scheduler.done()):
            self.scheduler = asyncio.create_task(self.pump())

    async def pump(self):
        while not self.closed:
            # One active batch bounds cross-job load on the same website.
            queued = [batch for batch in self.store.list_records('batch') if batch['status'] == 'queued']
            if queued:
                batch_id = queued[-1]['id']
                runner = asyncio.create_task(self.run(batch_id))
                self.runners[batch_id] = runner
                try:
                    await runner
                except asyncio.CancelledError:
                    if self.closed:
                        break
                finally:
                    self.runners.pop(batch_id, None)
            else:
                await asyncio.sleep(0.5)

    def recover(self):
        for batch in self.store.list_records('batch'):
            if batch['status'] in ('running', 'queued'):
                self.store.reset_items(batch['id'], ['running'])
                self.store.save('batch', {**batch, 'status': 'paused', 'reason': '服务重启，进度已保留，请继续任务。'}, batch['id'])

    async def close(self):
        self.closed = True
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)

    async def control(self, batch_id, action, session_id, scope=None, count=None):
        async with self.control_lock:
            batch = batch_view(self.store, batch_id)
            if batch.get('owner_session_id') != session_id:
                raise ValueError('Task does not belong to this conversation')
            state = batch['status']
            if action == 'confirm':
                if state != 'awaiting_confirmation' or scope not in ('all', 'count'):
                    raise ValueError('Task is not awaiting a valid scope confirmation')
                if batch['contract'].get('requested_filter'):
                    raise ValueError('筛选条件尚未核验，需要先确定匹配的项目页面，不能跳过条件。')
                if scope == 'count' and (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5000):
                    raise ValueError('count must be 1..5000')
                batch['contract'].update(scope=scope, count=count if scope == 'count' else batch['contract']['count'])
                if batch.get('confirmation'):
                    batch['confirmation']['status'] = 'answered'
                batch['status'] = 'queued'
            elif action in ('pause', 'cancel'):
                allowed = ('queued', 'running') if action == 'pause' else ('awaiting_confirmation', 'queued', 'running', 'paused', 'partial_completed', 'needs_input', 'failed')
                if state not in allowed:
                    raise ValueError('This action is not available in the current state')
                runner = self.runners.get(batch_id)
                if runner:
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                batch = batch_view(self.store, batch_id)
                batch['status'] = 'paused' if action == 'pause' else 'cancelled'
            elif action in ('resume', 'retry_failed'):
                if state not in ('paused', 'partial_completed', 'needs_input', 'failed', 'completed'):
                    raise ValueError('Task cannot be resumed in this state')
                if action == 'retry_failed':
                    if not self.store.item_counts(batch_id).get('failed'):
                        raise ValueError('No failed projects to retry')
                    self.store.reset_items(batch_id, ['failed'], reset_attempts=True)
                elif state == 'completed' or state == 'needs_input':
                    raise ValueError('No resumable work; retry failed items or resolve the manual requirements')
                # Time is renewed explicitly; download capacity remains a cumulative cap.
                batch.update(status='queued', elapsed_seconds=0, reason=None)
            else:
                raise ValueError('Unknown task action')
            batch.setdefault('control_events', []).append({'action': action, 'scope': scope, 'count': count, 'time': time.time()})
            self.store.save('batch', batch, batch_id)
            self.store.record_context('batch_control', {'batch_id': batch_id, 'action': action, 'scope': scope, 'count': count}, batch['chat_task_id'], session_id)
            self.ensure_scheduler()
            return batch_view(self.store, batch_id)

    async def run(self, batch_id):
        batch = self.store.get(batch_id)
        if not batch or batch['status'] != 'queued':
            return
        batch.update(status='running', reason=None)
        self.store.save('batch', batch, batch_id)
        started = time.monotonic()
        enumeration_done = asyncio.Event()
        byte_lock = asyncio.Lock()
        clients = []
        jobs = []
        contract = batch['contract']

        def save(item_checkpoint=None, **values):
            batch.update(values)
            batch['elapsed_seconds'] = round(time.monotonic() - started, 2)
            if item_checkpoint:
                self.store.checkpoint_batch_file(batch_id, batch, *item_checkpoint)
            else:
                self.store.save('batch', batch, batch_id)

        def runtime(acquisition):
            return ToolRuntime(self.store, {name: getattr(acquisition, name) for name in
                ('fetch_tender_detail', 'navigate_tender_entry', 'download_tender_file')},
                deadline=started + contract['timeout_seconds'], timeout=150)

        async def call(rt, name, **args):
            result, _ = await asyncio.wait_for(rt.call(name, args, 'batch', batch['chat_task_id'], batch.get('owner_session_id')), timeout=150)
            return result

        async def next_page(rt, page, acquisition):
            action = next(iter(page.get('pagination', [])), None)
            if not action:
                return None
            following = await call(rt, 'navigate_tender_entry', announcement_id=page['announcement_id'], entry_id=action['entry_id'])
            browser = getattr(acquisition, 'browser', None)
            if browser and page.get('browser_session_id'):
                _, session, previous_tab = browser.lookup(self.store.get(page['announcement_id']))
                _, _, next_tab = browser.lookup(self.store.get(following['announcement_id']))
                if previous_tab != next_tab:
                    await previous_tab.close()
                    session['pages'].pop(page['announcement_id'], None)
            return following

        async def enumerate_projects():
            acquisition = self.factory(self.store)
            clients.append(acquisition)
            rt = runtime(acquisition)
            try:
                if batch['enumeration_complete']:
                    return
                if contract.get('targets'):
                    for entry in contract['targets']:
                        self.store.enqueue_item(batch_id, identity(entry), {'entry': entry, 'page_index': 0, 'files': []})
                    save(discovered=self.store.item_counts(batch_id)['total'], enumeration_complete=True)
                    return
                page = await call(rt, 'fetch_tender_detail', url=contract['source_url'])
                seen_pages = set()
                target = contract['max_projects'] if contract['scope'] == 'all' else contract['count']
                for page_index in range(contract['max_pages']):
                    entries = page.get('entries', []) if contract['page_type'] == 'listing' else [{'title': page.get('title') or '项目', 'url': page['source_url'], 'project_number': page.get('project_number')}]
                    fingerprint = tuple(identity(entry) for entry in entries)
                    if not fingerprint or fingerprint in seen_pages:
                        raise BatchLimit('列表为空或分页未推进，无法确认范围完整性。')
                    seen_pages.add(fingerprint)
                    unscanned_entries = False
                    for entry in entries:
                        if self.store.item_counts(batch_id)['total'] >= target:
                            unscanned_entries = True
                            break
                        self.store.enqueue_item(batch_id, identity(entry), {'entry': entry, 'page_index': page_index, 'files': []})
                    counts = self.store.item_counts(batch_id)
                    save(discovered=counts['total'], pages_scanned=page_index + 1)
                    if counts['total'] >= target:
                        if contract['scope'] == 'all' and (page.get('pagination') or unscanned_entries):
                            raise BatchLimit('达到项目数量上限，未遍历全部项目。')
                        save(enumeration_complete=True)
                        return
                    following = await next_page(rt, page, acquisition)
                    if following is None:
                        observed_total = page.get('total_projects') or contract.get('observed_total')
                        if contract['scope'] == 'all' and not observed_total:
                            raise BatchLimit('未能核实列表总数或末页，不能确认全量项目已遍历。')
                        if observed_total and counts['total'] < observed_total and contract['scope'] == 'all':
                            raise BatchLimit('页面显示的总数大于已发现项目数，但未识别下一页。')
                        if contract['scope'] == 'count' and counts['total'] < target:
                            save(scope_shortfall=target - counts['total'])
                        save(enumeration_complete=True)
                        return
                    page = following
                raise BatchLimit('达到分页上限，未遍历全部项目。')
            finally:
                enumeration_done.set()

        async def worker():
            acquisition = self.factory(self.store)
            clients.append(acquisition)
            rt = runtime(acquisition)
            cached_page, cached_index = None, -1
            while True:
                item = self.store.claim_item(batch_id)
                if not item:
                    if enumeration_done.is_set():
                        return
                    await asyncio.sleep(0.1)
                    continue
                payload = item['payload']
                try:
                    entry = payload['entry']
                    if entry.get('url'):
                        detail = await call(rt, 'fetch_tender_detail', url=entry['url'])
                    else:
                        index = payload['page_index']
                        if cached_page is None or cached_index > index:
                            cached_page = await call(rt, 'fetch_tender_detail', url=contract['source_url'])
                            cached_index = 0
                        while cached_index < index:
                            cached_page = await next_page(rt, cached_page, acquisition)
                            if not cached_page:
                                raise ValueError('Source pagination changed; project could not be located')
                            cached_index += 1
                        matches = [value for value in cached_page.get('entries', []) if identity(value) == identity(entry)]
                        if len(matches) != 1:
                            raise ValueError('Project changed or is ambiguous; not downloading another project')
                        detail = await call(rt, 'navigate_tender_entry', announcement_id=cached_page['announcement_id'], entry_id=matches[0]['entry_id'])
                    payload.update(source_url=detail['source_url'], announcement_id=detail['announcement_id'])
                    if detail.get('page_type') in ('listing', 'unresolved'):
                        raise ValueError('Project entry did not resolve to a confirmed detail')
                    if entry.get('assessment'):
                        try:
                            validate_checkpoint(detail, entry['assessment'])
                        except ValueError as exc:
                            self.store.update_item(item['id'], 'needs_input', {**payload, 'error': str(exc)})
                            continue
                        payload['eligibility'] = entry['assessment']
                    attachments, excluded = split_attachments(detail.get('attachments', []), contract['file_formats'])
                    if contract.get('selected_attachment_keys') is not None:
                        keys = set(contract['selected_attachment_keys'])
                        attachments = [attachment for attachment in attachments if (attachment.get('href') or attachment.get('name') or attachment['url']) in keys]
                        if len(attachments) != len(keys):
                            self.store.update_item(item['id'], 'needs_input', {**payload, 'error': '已选择的附件入口缺失、格式不符或发生变化，需重新选择'})
                            continue
                    if contract.get('require_documents') or contract.get('files_per_project') or entry.get('assessment', {}).get('files'):
                        selected = [document for document in entry.get('assessment', {}).get('files', []) if document['document_type'] == 'tender_document'
                            or (contract.get('fallback_file_goal') == 'announcement' and entry.get('assessment', {}).get('fallback_used')
                                and document['document_type'] == 'announcement_document')
                            or (not contract.get('require_documents') and document['document_type'] in ('other_pdf', 'other_file', 'announcement_document', 'prequalification_notice'))]
                        keys = {document['attachment_key'] for document in selected}
                        attachments = [attachment for attachment in attachments if (attachment.get('href') or attachment.get('name') or attachment['url']) in keys]
                        if len(attachments) != len(keys):
                            self.store.update_item(item['id'], 'needs_input', {**payload, 'error': '已核验的标书附件入口已变化，需重新核验'})
                            continue
                        if contract.get('files_per_project') and len(attachments) != contract['files_per_project']:
                            self.store.update_item(item['id'], 'needs_input', {**payload, 'error': '每个项目的已核验文件数量不符合用户要求'})
                            continue
                    payload['excluded_attachments'] = [*detail.get('excluded_attachments', []), *excluded]
                    attachment_keys = [attachment.get('href') or (attachment.get('name') if attachment['url'].startswith('browser-attachment:') else attachment['url']) for attachment in attachments]
                    if len(attachment_keys) != len(set(attachment_keys)):
                        raise ValueError('Attachment identities are ambiguous; manual review required')
                    completed_files = {file['attachment_key'] for file in payload['files']}
                    if len(attachments) > contract['max_files_per_project']:
                        raise BatchLimit('单项目附件数超过限额，需缩小范围。')
                    for attachment in attachments:
                        key = attachment.get('href') or (attachment.get('name') if attachment['url'].startswith('browser-attachment:') else attachment['url'])
                        if key in completed_files:
                            continue
                        async with byte_lock:
                            remaining = contract['max_bytes'] - batch['bytes_downloaded']
                            if remaining <= 0:
                                raise BatchLimit('累计下载容量已达上限。')
                            try:
                                options = {'allowed_formats': contract['file_formats']} if contract['file_formats'] != ['pdf'] else {}
                                file = await call(rt, 'download_tender_file', url=attachment['url'], announcement_id=detail['announcement_id'], max_bytes=min(100 * 1024**2, remaining), **options)
                            except NonPDFFile as exc:
                                payload['excluded_attachments'].append({**attachment, 'reason': str(exc)})
                                self.store.update_item(item['id'], 'running', payload)
                                continue
                            selected_key = attachment.get('href') or attachment.get('name') or attachment['url']
                            selected_document = next((document for document in entry.get('assessment', {}).get('files', [])
                                if document['attachment_key'] == selected_key), None)
                            if selected_document:
                                file['document_type'] = selected_document['document_type']
                                file['fallback_used'] = bool(entry.get('assessment', {}).get('fallback_used'))
                                self.store.save('file', file, file['file_id'])
                            payload['files'].append({**file, 'attachment_key': key})
                            save(bytes_downloaded=batch['bytes_downloaded'] + file['size'], item_checkpoint=(item['id'], payload))
                    text = detail.get('text', '')
                    manual = not attachments and bool(re.search(r'请先登录|登录后下载|购买招标文件|支付.*文件|验证码', text))
                    status = 'downloaded' if payload['files'] else 'needs_input' if manual else 'failed' if attachments else 'no_attachment'
                    error = '所选附件未通过实际格式校验，详情见附件拒收记录；不代表页面没有附件' if attachments and not payload['files'] else None
                    if contract.get('files_per_project') and len(payload['files']) != contract['files_per_project']:
                        status, error = 'needs_input', '实际通过 PDF 校验的文件数量不足用户约定，不能标记下载完成'
                    self.store.update_item(item['id'], status, {**payload, 'error': error})
                    save()
                    # A click can create a new tab for every project; retain only the worker's listing tab.
                    browser = getattr(acquisition, 'browser', None)
                    if browser and detail.get('browser_session_id'):
                        try:
                            _, browser_session, detail_page = browser.lookup(self.store.get(detail['announcement_id']))
                            if not cached_page or detail['announcement_id'] != cached_page['announcement_id']:
                                await detail_page.close()
                                browser_session['pages'].pop(detail['announcement_id'], None)
                        except ValueError:
                            pass
                except BatchLimit:
                    self.store.update_item(item['id'], 'pending', payload)
                    raise
                except DownloadCapacityExceeded as exc:
                    self.store.update_item(item['id'], 'pending', payload)
                    raise BatchLimit('下载容量或单文件大小达到限制，已保留进度。') from exc
                except asyncio.CancelledError:
                    self.store.update_item(item['id'], 'pending', payload)
                    raise
                except Exception as exc:
                    payload['error'] = str(exc)
                    payload['failure'] = getattr(exc, 'failure', failure(exc))
                    self.store.update_item(item['id'], 'needs_input' if payload['failure']['category'] == 'access_required' else 'failed', payload)
                    cached_page, cached_index = None, -1

        try:
            jobs = [asyncio.create_task(enumerate_projects()), *[asyncio.create_task(worker()) for _ in range(contract['concurrency'])]]
            await asyncio.wait_for(asyncio.gather(*jobs), timeout=contract['timeout_seconds'])
            counts = self.store.item_counts(batch_id)
            requested = contract.get('requested_count') or contract['count']
            if counts.get('needs_input') and not counts.get('failed'):
                save(status='needs_input', reason='部分项目需要人工处理或模型重新核验，未完成下载目标。')
            elif counts.get('failed') or batch.get('scope_shortfall') or counts.get('no_attachment') or (contract['scope'] == 'count' and counts.get('downloaded', 0) < requested):
                save(status='partial_completed', reason='部分项目失败或不足约定数量，请查看明细。')
            else:
                save(status='completed', reason='约定范围内项目已处理；成功下载与无公开 PDF 分别统计。')
        except asyncio.CancelledError:
            save(status='paused', reason='任务已暂停，进度已保留。')
            raise
        except (BatchLimit, asyncio.TimeoutError) as exc:
            save(status='partial_completed', reason=str(exc) or '执行时间已达上限，进度已保留。')
        except Exception as exc:
            save(status='partial_completed', reason='任务未完成：' + str(exc))
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for acquisition in clients:
                await acquisition.close()
            self.store.reset_items(batch_id, ['running'])
            if batch['status'] not in ('paused', 'queued', 'running') and not self.store.item_counts(batch_id).get('downloaded'):
                batch['error_code'] = 'NO_FILES_DOWNLOADED'
                batch['reason'] = '下载失败：没有任何文件成功下载。原因：' + (batch.get('reason') or '未找到符合要求的可下载附件。')
            save()
