"""Bounded public-page navigation; no arbitrary selectors or scripts from the agent."""
import asyncio
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
from bidding.file_policy import split_attachments
from bidding.content import index_content
from bidding.listing import listing_fingerprint


class DownloadCapacityExceeded(ValueError):
    pass


def document_rejection(dialogs):
    return next((dialog['message'] for dialog in dialogs
        if re.search(r'不在.*时间|已截止|已过期|获取.*截止|尚未开始|未到.*时间|失败|无权限|无权|拒绝', dialog['message'])), None)


class BrowserNavigator:
    def __init__(self, store, validator, parser):
        self.store, self.validate, self.parse = store, validator, parser
        self.playwright = self.browser = None
        self.sessions = {}
        self.start_lock = asyncio.Lock()
        self.capacity_lock = asyncio.Lock()
        self.download_lock = asyncio.Lock()
        self.dom_script = Path(__file__).with_name('browser_dom.js').read_text()
        self.timeout = max(5, min(90, float(os.getenv('BROWSER_TIMEOUT_SECONDS', '40'))))
        self.max_bytes = 100 * 1024 * 1024
        self.max_pages = max(2, min(100, int(os.getenv('BROWSER_MAX_PAGES', '32'))))
        self.download_dir = None

    async def start(self):
        async with self.start_lock:
            if self.browser:
                return
            self.playwright = await async_playwright().start()
            self.download_dir = tempfile.TemporaryDirectory(prefix='bidding-browser-')
            channel = os.getenv('BROWSER_CHANNEL', 'chrome' if sys.platform == 'darwin' else '')
            try:
                self.browser = await self.playwright.chromium.launch(headless=True,
                    downloads_path=self.download_dir.name, **({'channel': channel} if channel else {}))
            except BaseException:
                await self.playwright.stop()
                self.playwright = None
                self.download_dir.cleanup()
                raise

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        if self.download_dir:
            self.download_dir.cleanup()
        self.browser = self.playwright = None
        self.sessions.clear()

    async def new_session(self):
        async with self.capacity_lock:
            return await self._new_session()

    async def _new_session(self):
        await self.start()
        # Browser state expires; never store cookies in tool output or logs.
        now = time.monotonic()
        for ident, item in list(self.sessions.items()):
            if now - item['used'] > 900 and not item['lock'].locked():
                await item['context'].close()
                self.sessions.pop(ident, None)
        if len(self.sessions) >= 4:
            idle = [(ident, item) for ident, item in self.sessions.items() if not item['lock'].locked()]
            if not idle:
                raise ValueError('Browser session limit reached; retry after current operations finish')
            oldest, item = min(idle, key=lambda pair: pair[1]['used'])
            await item['context'].close()
            self.sessions.pop(oldest, None)
        context = await self.browser.new_context(accept_downloads=True, service_workers='block', viewport={'width': 1440, 'height': 1000})
        context.set_default_timeout(self.timeout * 1000)
        ident = uuid.uuid4().hex
        session = {'context': context, 'used': now, 'lock': asyncio.Lock(), 'pages': {}, 'network': [], 'dialogs': [], 'allow_download': False}
        self.sessions[ident] = session

        async def route_request(route):
            if route.request.url.startswith('blob:') and session['allow_download']:
                blob_origin = urlsplit(route.request.url[5:])
                origins = {(urlsplit(page.url).scheme, urlsplit(page.url).netloc) for page in session['pages'].values()}
                if (blob_origin.scheme, blob_origin.netloc) in origins:
                    await route.continue_()
                    return
            try:
                await self.validate(route.request.url)
            except Exception:
                if len(session['network']) < 200:
                    session['network'].append({'url': route.request.url, 'status': 'blocked'})
                await route.abort('blockedbyclient')
            else:
                await route.continue_()

        await context.route('**/*', route_request)
        await context.route_web_socket('**/*', lambda socket: socket.close())

        async def observe_dialog(dialog):
            session['dialogs'].append({'type': dialog.type, 'message': dialog.message[:2000]})
            session['dialogs'][:] = session['dialogs'][-32:]
            # Record before dismissing; never accept a confirmation or enter data.
            await dialog.dismiss()

        def observe(page):
            page.on('dialog', observe_dialog)
            page.on('response', lambda response: session['network'].append({'url': response.url, 'status': response.status}) if len(session['network']) < 200 else None)
            page.on('download', lambda download: download.cancel() if not session['allow_download'] else None)

        context.on('page', observe)
        return ident, session

    async def ready(self, page):
        await page.wait_for_load_state('domcontentloaded', timeout=self.timeout * 1000)
        try:
            await page.wait_for_function("""() => [...document.querySelectorAll('tbody tr')].some(e => e.innerText.trim()) ||
                [...document.querySelectorAll('input[type="password"]')].some(e => e.getClientRects().length > 0) ||
                [...document.querySelectorAll('a')].some(e => /\\.(pdf|docx?|zip)/i.test(e.innerText)) ||
                [...document.querySelectorAll('a, button, [role="link"]')].some(e => e.getClientRects().length > 0 &&
                    /^(获取招标文件|获取采购文件|领取招标文件|下载公告文件|下载变更公告文件)$/.test(e.innerText.trim())) ||
                [...document.querySelectorAll('main, article')].some(e => e.innerText.length > 400)""", timeout=min(self.timeout, 25) * 1000)
        except BrowserTimeout:
            pass

    async def snapshot(self, page, ident, session, parent=None, action=None):
        await self.validate(page.url)
        await self.ready(page)
        prefix = uuid.uuid4().hex
        observed = await page.evaluate(self.dom_script, prefix)
        html = await page.content()
        if len(html.encode()) > 5 * 1024 * 1024:
            raise ValueError('Rendered page exceeds 5 MB')
        result = self.parse(page.url, html)
        result.update(observed)
        result['attachments'], result['excluded_attachments'] = split_attachments(observed['attachments'])
        for attachment in result['attachments']:
            attachment['download_mode'] = 'direct_url' if attachment.get('href') else 'browser_click'
            attachment['requires_browser_session'] = True
        if observed['page_type'] == 'detail' and result.get('title'):
            start = result['text'].find(result['title'])
            if start >= 0:
                result['text'] = result['text'][start:]
        result['adapter'] += '+browser'
        result['browser_session_id'] = ident
        result['navigation'] = {'parent_announcement_id': parent, 'entry_id': action, 'resolved_url': page.url}
        if result.get('page_type') == 'listing':
            result['listing_fingerprint'] = listing_fingerprint(result['entries'])
            result['listing_path'] = {'root_url': page.url, 'actions': []}
        result['network'] = session['network'][-100:]
        result['render_status'] = 'ready' if observed['page_type'] != 'unresolved' else 'unresolved'
        result['warnings'] = ['浏览器渲染的公开页面；列表状态不是已核实投标截止时间，附件类型须核验。']
        # Re-index after rendered DOM and source-title trimming replace static text.
        result = index_content(result)
        result['announcement_id'] = self.store.save('announcement', result)
        session['pages'][result['announcement_id']] = page
        session['used'] = time.monotonic()
        return result

    async def read(self, url):
        await self.validate(url)
        ident, session = await self.new_session()
        try:
            async with session['lock']:
                page = await session['context'].new_page()
                await page.goto(url, wait_until='domcontentloaded')
                return await self.snapshot(page, ident, session)
        except BaseException:
            await session['context'].close()
            self.sessions.pop(ident, None)
            raise

    def lookup(self, record):
        ident = record.get('browser_session_id')
        session = self.sessions.get(ident)
        if not session or time.monotonic() - session['used'] > 900:
            raise ValueError('Browser session expired or service restarted; read the source page again')
        page = session['pages'].get(record['id'])
        if not page or page.is_closed():
            raise ValueError('Browser page no longer available; read the source page again')
        return ident, session, page

    async def action_page(self, record, session, page, item):
        if record.get('page_type') == 'listing':
            await self.restore_listing(record, page)
        if page.url != record['source_url']:
            await page.goto(record['source_url'], wait_until='domcontentloaded')
            await self.ready(page)
        selector = f'[data-bidding-ref="{item.get("entry_id") or item.get("attachment_id")}"]'
        locator = page.locator(selector)
        if not await locator.count():
            observed = await page.evaluate(self.dom_script, uuid.uuid4().hex)
            key = 'name' if 'attachment_id' in item else 'title'
            matches = [value for value in [*observed['entries'], *observed['pagination'], *observed['attachments'], *observed.get('document_access', [])]
                if value.get(key) == item.get(key) and (not item.get('project_number') or value.get('project_number') == item['project_number'])]
            if len(matches) != 1:
                raise ValueError('Page changed or action is ambiguous; read the source page again')
            ref = matches[0].get('entry_id') or matches[0].get('attachment_id')
            locator = page.locator(f'[data-bidding-ref="{ref}"]')
        return locator

    async def restore_listing(self, record, page):
        current = await page.evaluate(self.dom_script, uuid.uuid4().hex)
        expected = listing_fingerprint(record.get('entries', []))
        if page.url == record['source_url'] and listing_fingerprint(current['entries']) == expected:
            return
        path = record.get('listing_path') or {'root_url': record['source_url'], 'actions': []}
        if len(path['actions']) > 20:
            raise ValueError('Listing cursor restoration exceeds 20 observed actions; refresh or use another saved listing cursor')
        await page.goto(path['root_url'], wait_until='domcontentloaded')
        await self.ready(page)
        for action in path['actions']:
            before = await page.evaluate(self.dom_script, uuid.uuid4().hex)
            matches = [value for value in before['pagination'] if value.get('title') == action['title']]
            if len(matches) != 1:
                raise ValueError('Saved pagination action changed or is ambiguous')
            await page.locator(f'[data-bidding-ref="{matches[0]["entry_id"]}"]').click()
            await self.wait_listing_change(page, before)
        restored = await page.evaluate(self.dom_script, uuid.uuid4().hex)
        if listing_fingerprint(restored['entries']) != expected:
            raise ValueError('Listing cursor could not be restored; observed rows changed. Read source again rather than using stale IDs')

    async def wait_listing_change(self, page, before):
        deadline = time.monotonic() + self.timeout
        fingerprint = listing_fingerprint(before['entries'])
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            current = await page.evaluate(self.dom_script, uuid.uuid4().hex)
            if current['entries'] and listing_fingerprint(current['entries']) != fingerprint:
                return
        raise ValueError('Pagination produced no new observed entries; do not count this as a new page')

    async def inspect_document_access(self, record, item):
        ident, session, page = self.lookup(record)
        async with session['lock']:
            if item.get('url'):
                await self.validate(item['url'])
            locator = await self.action_page(record, session, page, item)
            before = set(session['context'].pages)
            session['dialogs'] = []
            await locator.click()
            await asyncio.sleep(1)
            opened = [value for value in session['context'].pages if value not in before]
            target = opened[-1] if opened else page
            await self.ready(target)
            text = (await target.locator('body').inner_text())[:6000]
            dialogs = list(session['dialogs'])
            access_text = '\n'.join([text, *(dialog['message'] for dialog in dialogs)])
            restricted = bool(await target.locator('input[type="password"]').count()) or bool(re.search(r'请先登录|请登录后|登录后.*获取|验证码|购买招标文件|支付费用', access_text))
            result = await self.snapshot(target, ident, session, record['id'], item['entry_id'])
            result['document_access_status'] = 'requires_user_action' if restricted else 'inspected'
            result['access_observation'] = text[:2000]
            result['access_dialogs'] = dialogs
            result['access_source'] = {'announcement_id': record['id'], 'source_url': record['source_url'],
                'entry_id': item['entry_id']}
            if restricted:
                message = next((dialog['message'] for dialog in dialogs if re.search(r'登录|验证码|购买|支付', dialog['message'])),
                    '文件入口要求登录、验证码或购买；未自动提交凭据或付款。')
                result['access_error'] = {'code': 'DOCUMENT_ACCESS_REQUIRES_USER_ACTION', 'message': message,
                    'source': 'javascript_dialog' if dialogs else 'page'}
            rejection = document_rejection(dialogs)
            if rejection:
                result['document_access_status'] = 'blocked'
                result['access_error'] = {'code': 'WEBSITE_REJECTED_DOCUMENT_ACCESS', 'message': rejection,
                    'source': 'javascript_dialog'}
            result['access_notice'] = ('已观察到登录、验证码或购买障碍；不要反复读取空附件数组，不自动提交凭据或付款。'
                if restricted else '入口已检查一次。只能下载已观察附件，此次检查不证明文件已下载。')
            if rejection:
                result['access_notice'] = '网站拒绝获取文件。将 access_error.message 原话作为已观察的网站提示告知用户，不得声称已下载或推断必须登录。'
            self.store.save('announcement', result, result['announcement_id'])
            return result

    async def navigate(self, record, item):
        ident, session, page = self.lookup(record)
        async with session['lock']:
            key = (record['id'], item['entry_id'])
            cached_id = session.setdefault('navigation_results', {}).get(key)
            if cached_id and (cached := self.store.get(cached_id)):
                return {**cached, 'announcement_id': cached_id, 'reused': True, 'reuse_reason': 'observed_navigation_checkpoint'}
            if item.get('kind') != 'pagination' and len(session['context'].pages) >= self.max_pages:
                # Saved evidence remains readable; retain pages with interactive file actions.
                for candidate in list(session['context'].pages):
                    if candidate == page:
                        continue
                    snapshots = [self.store.get(page_id) for page_id, saved in session['pages'].items() if saved == candidate]
                    if snapshots and all(saved and saved.get('page_type') == 'detail' and not saved.get('document_access')
                        and not any(file.get('url', '').startswith('browser-attachment:') for file in saved.get('attachments', [])) for saved in snapshots):
                        await candidate.close()
                        break
                if len(session['context'].pages) >= self.max_pages:
                    raise ValueError('Browser page capacity reached; reuse saved evidence or release interactive pages')
            if item.get('url'):
                await self.validate(item['url'])
                target = page if item.get('kind') == 'pagination' else await session['context'].new_page()
                await target.goto(item['url'], wait_until='domcontentloaded')
            else:
                locator = await self.action_page(record, session, page, item)
                before = page.url
                rows_before = await page.evaluate(self.dom_script, uuid.uuid4().hex) if item.get('kind') == 'pagination' else None
                if rows_before is not None:
                    locator = await self.action_page(record, session, page, item)
                popup = asyncio.get_running_loop().create_future()
                def opened(new_page):
                    if not popup.done():
                        popup.set_result(new_page)
                session['context'].on('page', opened)
                try:
                    await locator.click()
                    for _ in range(50):
                        if popup.done() or page.url != before or item.get('kind') == 'pagination':
                            break
                        await asyncio.sleep(0.1)
                    target = popup.result() if popup.done() else page
                    if target == page and page.url == before and item.get('kind') != 'pagination':
                        raise ValueError('Project click did not navigate; no detail page was confirmed')
                finally:
                    session['context'].remove_listener('page', opened)
                if target.url == 'about:blank':
                    await target.wait_for_url(lambda url: str(url) != 'about:blank')
                if rows_before is not None:
                    await self.wait_listing_change(target, rows_before)
            result = await self.snapshot(target, ident, session, record['id'], item['entry_id'])
            if item.get('kind') == 'pagination' and result.get('page_type') == 'listing':
                path = record.get('listing_path') or {'root_url': record['source_url'], 'actions': []}
                result['listing_path'] = {'root_url': path['root_url'], 'actions': [*path['actions'], {'title': item['title']}]}
                self.store.save('announcement', result, result['announcement_id'])
            if result.get('render_status') == 'ready':
                session['navigation_results'][key] = result['announcement_id']
            return result

    async def download(self, record, item, destination, max_bytes=None):
        _, session, page = self.lookup(record)
        async with self.download_lock, session['lock']:
            locator = await self.action_page(record, session, page, item)
            session['allow_download'] = True
            session['dialogs'] = []
            download = None
            try:
                async with page.expect_download(timeout=self.timeout * 1000) as event:
                    await locator.click()
                download = await event.value
                if urlsplit(download.url).scheme in ('http', 'https'):
                    await self.validate(download.url)
                saving = asyncio.create_task(download.save_as(destination))
                try:
                    deadline = time.monotonic() + self.timeout
                    while not saving.done():
                        size = sum(path.stat().st_size for path in Path(self.download_dir.name).iterdir() if path.is_file())
                        if size > min(self.max_bytes, max_bytes or self.max_bytes):
                            await download.cancel()
                            raise DownloadCapacityExceeded('Browser download exceeds remaining capacity or per-file limit')
                        if time.monotonic() > deadline:
                            await download.cancel()
                            raise ValueError('Browser download time limit reached')
                        await asyncio.sleep(0.1)
                    await saving
                finally:
                    if not saving.done():
                        saving.cancel()
                        await asyncio.gather(saving, return_exceptions=True)
                return {'resolved_url': download.url, 'suggested_filename': download.suggested_filename}
            except BrowserTimeout as exc:
                rejection = document_rejection(session['dialogs'])
                if rejection:
                    raise ValueError('网站拒绝下载，页面提示：' + rejection) from exc
                raise ValueError('No download was produced by the observed attachment; inspect the page again for authentication or changed content') from exc
            finally:
                session['allow_download'] = False
                if download:
                    await download.delete()
