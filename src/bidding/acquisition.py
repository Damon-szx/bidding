import asyncio
import hashlib
import ipaddress
import re
import socket
import tempfile
import json
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bidding.browser import BrowserNavigator, DownloadCapacityExceeded
from bidding.file_policy import NonPDFFile, file_candidate, project_filename, split_attachments, validate_document
from bidding.content import index_content


async def validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A public HTTP(S) URL without credentials is required")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private and local network targets are not supported")


class GenericHTMLAdapter:
    def parse(self, url, html):
        soup = BeautifulSoup(html, "html.parser")
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        title = soup.find("h1") or soup.title
        links = []
        for anchor in soup.select("a[href]"):
            href = anchor["href"].strip()
            if not href or (href.startswith("#") and not href.startswith("#/")):
                continue
            target = urljoin(url, href)
            if not urlparse(target).fragment.startswith('/'):
                target = urldefrag(target)[0]
            if target == url or target == urldefrag(url)[0]:
                continue
            if urlparse(target).scheme in ("http", "https"):
                links.append({"url": target, "name": anchor.get_text(" ", strip=True)[:300]})
        files = []
        for link in links:
            suffix = Path(urlparse(link["url"]).path).suffix.lower()
            if suffix in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z") or any(word in link["name"] for word in ("附件", "下载", "招标文件")):
                if link["url"] not in {item["url"] for item in files}:
                    files.append(link)
        entries = []
        for row in soup.select('tbody tr'):
            anchor = row.select_one('td a[href]')
            if anchor and any(word in row.get_text() for word in ('招标', '采购', '公告', '项目')):
                target = urljoin(url, anchor['href'])
                if urlparse(target).scheme in ('http', 'https') and target not in {item['url'] for item in files}:
                    headers = [cell.get_text(' ', strip=True) for cell in row.find_parent('table').select('thead th, thead td')]
                    date_index = next((i for i, value in enumerate(headers) if re.search(r'发布时间|发布日期|发布.*日期', value)), None)
                    cells = row.select('td')
                    observed_date = re.search(r'\d{4}-\d{2}-\d{2}', cells[date_index].get_text()) if date_index is not None and date_index < len(cells) else None
                    entries.append({'entry_id': 'link-' + str(len(entries) + 1), 'title': anchor.get_text(' ', strip=True), 'url': target,
                        'listed_date': observed_date[0] if observed_date else None, 'date_kind': 'published_at' if observed_date else 'unknown',
                        'row_text': row.get_text(' ', strip=True)[:1200]})
        if not entries and re.search(r'/(?:index|list)(?:\.|/|$)', urlparse(url).path):
            for link in links:
                if re.search(r'招标公告|采购公告|中标.*公告|成交.*公告', link['name']) and len(link['name']) > 10:
                    if link['url'] not in {item['url'] for item in entries}:
                        entries.append({'entry_id': 'link-' + str(len(entries) + 1), 'title': link['name'], 'url': link['url']})
        pagination = [{'entry_id': 'next-page', 'title': '下一页', 'url': urljoin(url, anchor['href']), 'kind': 'pagination'}
            for anchor in soup.select('a[href]') if anchor.get_text(strip=True) in ('下一页', '下页', 'Next')
            and urlparse(urljoin(url, anchor['href'])).scheme in ('http', 'https')][:1]
        full_text = soup.get_text("\n", strip=True)
        text = full_text[:60000]
        total = re.search(r'(?:共|总计)\s*([\d,]+)\s*(?:条|个|项)', text)
        accepted, excluded = split_attachments(files[:50])
        return {"source_url": url, "title": title.get_text(" ", strip=True) if title else None, "text": text, "text_truncated": len(full_text) > len(text), "project_number": None, "published_at": None, "adapter": "generic_html", "attachments": accepted, "excluded_attachments": excluded, "links": links[:100], "entries": entries[:30], "pagination": pagination, "total_projects": int(total.group(1).replace(',', '')) if total else None, "page_type": "listing" if entries else "detail" if title else "unresolved", "warnings": ["通用提取结果，公告字段及日期筛选须进一步核验。"]}


class CCGPAdapter(GenericHTMLAdapter):
    def parse(self, url, html):
        result = super().parse(url, html)
        soup = BeautifulSoup(html, "html.parser")
        published = soup.find("meta", attrs={"name": "PubDate"})
        result["published_at"] = published.get("content") if published else None
        result["published_timezone"] = "Asia/Shanghai"
        fields = {}
        for label in soup.select("td.title"):
            value = label.find_next_sibling("td")
            if value:
                fields[label.get_text(strip=True)] = value.get_text(" ", strip=True)
        result["purchaser"] = fields.get("采购单位")
        result["project_name"] = fields.get("采购项目名称")
        result["acquisition_location"] = fields.get("获取招标文件的地点")
        number = soup.select_one('[id="_notice_content_noticeProject-projectCode"]')
        result["project_number"] = number.get_text(strip=True) if number else None
        result["adapter"] = "ccgp_html"
        result["warnings"] = ["仅支持公开网页及附件入口，报名要求须核对公告原文。"]
        return result


class SGCCAdapter(GenericHTMLAdapter):
    def parse(self, url, html):
        result = super().parse(url, html)
        text = result['text']
        number = re.search(r'项目编号[：:]\s*([^】\n]+)', text)
        date = re.search(r'\b\d{4}-\d{2}-\d{2}\b', text)
        result.update(adapter='sgcc_ecp', project_number=number.group(1).strip() if number else None,
            published_at=date.group(0) if date and '/doc/' in url else None,
            published_timezone='Asia/Shanghai', information_type='prequalification_notice' if url.endswith('_2018032700290425') else 'unknown')
        return result


class Acquisition:
    def __init__(self, store, client=None):
        self.store = store
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False, headers={"User-Agent": "BiddingAgent/0.1"})
        self.adapter = GenericHTMLAdapter()
        self.adapters = {"www.ccgp.gov.cn": CCGPAdapter(), "ccgp.gov.cn": CCGPAdapter(), 'ecp.sgcc.com.cn': SGCCAdapter()}
        self.browser = BrowserNavigator(store, validate_public_url, self.parse_page)

    def parse_page(self, url, html):
        result = self.adapters.get(urlparse(url).hostname, self.adapter).parse(url, html)
        soup = BeautifulSoup(html, 'html.parser')
        result['content_headings'] = list(dict.fromkeys(node.get_text(' ', strip=True)
            for node in soup.select('h1,h2,h3,h4,h5,h6')))[:100]
        return index_content(result)

    async def close(self):
        await self.browser.close()
        await self.client.aclose()

    async def open(self, url, referer=None, cookie_provider=None):
        for _ in range(6):
            await validate_public_url(url)
            headers = {"Referer": referer} if referer else {}
            if cookie_provider:
                headers['Cookie'] = await cookie_provider(url)
            request = self.client.build_request("GET", url, headers=headers)
            response = await self.client.send(request, stream=True)
            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise ValueError("Redirect without Location")
                url = urljoin(url, location)
                continue
            try:
                response.raise_for_status()
            except Exception:
                await response.aclose()
                raise
            return response
        raise ValueError("Too many redirects")

    async def fetch_tender_detail(self, url):
        if urlparse(url).fragment.startswith('/'):
            return await self.render_tender_page(url)
        response = await self.open(url)
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 5 * 1024 * 1024:
                    raise ValueError("Page exceeds 5 MB")
            if "html" not in response.headers.get("content-type", "").lower() and not bytes(body).lstrip().startswith((b"<", b"<!")):
                raise ValueError("URL is not an HTML announcement page")
            html = bytes(body).decode(response.encoding or "utf-8", errors="replace")
            result = self.parse_page(str(response.url), html)
            if not result['attachments'] and not result.get('excluded_attachments') and not result['entries'] and (len(result['text'].strip()) < 150 or any(marker in html.lower() for marker in ('<app-root', 'id="__next"', '<router-view', 'id="app"', 'id="root"'))):
                await response.aclose()
                return await self.render_tender_page(str(response.url))
            result["announcement_id"] = self.store.save("announcement", result)
            return result
        finally:
            await response.aclose()

    async def render_tender_page(self, url):
        return await asyncio.wait_for(self.browser.read(url), timeout=self.browser.timeout + 35)

    async def navigate_tender_entry(self, announcement_id, entry_id):
        record = self.store.get(announcement_id)
        if not record or record['kind'] != 'announcement':
            raise ValueError('Unknown source-page record')
        item = next((item for item in [*record.get('entries', []), *record.get('pagination', [])] if item['entry_id'] == entry_id), None)
        if not item:
            raise ValueError('Entry must be observed on this page first')
        if record.get('browser_session_id'):
            return await asyncio.wait_for(self.browser.navigate(record, item), timeout=self.browser.timeout + 35)
        result = await self.fetch_tender_detail(item['url'])
        result['navigation'] = {'parent_announcement_id': announcement_id, 'entry_id': entry_id, 'resolved_url': result['source_url']}
        self.store.save('announcement', result, result['announcement_id'])
        return result

    async def discover_tender_files(self, announcement_id):
        announcement = self.store.get(announcement_id)
        if not announcement or announcement["kind"] != "announcement":
            raise ValueError("Unknown announcement_id; fetch the announcement first")
        files, excluded = split_attachments(announcement["attachments"])
        return {"source_url": announcement["source_url"], "files": files, 'excluded_attachments': [*announcement.get('excluded_attachments', []), *excluded],
            'document_access': announcement.get('document_access', []), 'content_complete': True,
            'next_action': 'assess_observed_files' if files else 'inspect_document_access_or_change_candidate',
            "status": "found" if files else "no_public_pdf_found"}

    async def inspect_document_access(self, announcement_id, entry_id):
        record = self.store.get(announcement_id)
        item = next((value for value in (record or {}).get('document_access', []) if value['entry_id'] == entry_id), None)
        if not record or not item or not record.get('browser_session_id'):
            raise ValueError('File access must use an observed browser document_access entry')
        return await asyncio.wait_for(self.browser.inspect_document_access(record, item), timeout=self.browser.timeout + 35)

    async def download_tender_file(self, url, announcement_id, max_bytes=100 * 1024 * 1024, allowed_formats=None):
        allowed_formats = allowed_formats or ['pdf']
        max_bytes = max(1, min(100 * 1024 * 1024, int(max_bytes)))
        announcement = self.store.get(announcement_id)
        if not announcement or announcement["kind"] != "announcement":
            raise ValueError("Unknown announcement_id")
        item = next((item for item in announcement['attachments'] if item['url'] == url), None)
        if not item:
            raise ValueError("Attachment must be discovered on this announcement first")
        if not file_candidate(item, allowed_formats):
            raise NonPDFFile('Attachment does not match requested file formats')
        if url.startswith('browser-attachment:') and not item.get('href'):
            with tempfile.NamedTemporaryFile(dir=self.store.root / 'files', delete=False) as out:
                path = Path(out.name)
            try:
                metadata = await asyncio.wait_for(self.browser.download(announcement, item, str(path), max_bytes=max_bytes), timeout=self.browser.timeout * 2 + 5)
                if path.stat().st_size > max_bytes:
                    raise DownloadCapacityExceeded('Download capacity limit reached')
                return await asyncio.to_thread(self.finish_file, path, url, announcement, metadata['resolved_url'], metadata['suggested_filename'], item, allowed_formats)
            finally:
                path.unlink(missing_ok=True)
        requested_url = item.get('href') or url
        async def cookies(target):
            _, session, _ = self.browser.lookup(announcement)
            values = await session['context'].cookies([target])
            return '; '.join(cookie['name'] + '=' + cookie['value'] for cookie in values)
        response = await self.open(requested_url, announcement["source_url"], cookies if url.startswith('browser-attachment:') else None)
        tmp = None
        try:
            size = 0
            prefix = bytearray()
            with tempfile.NamedTemporaryFile(dir=self.store.root / "files", delete=False) as out:
                tmp = Path(out.name)
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise DownloadCapacityExceeded("Attachment exceeds remaining download capacity")
                    if len(prefix) < 5:
                        prefix.extend(chunk[:5 - len(prefix)])
                    if allowed_formats == ['pdf'] and len(prefix) == 5 and prefix != b'%PDF-':
                        raise NonPDFFile('Non-PDF or invalid file content; only PDF is saved')
                    out.write(chunk)
            return await asyncio.to_thread(self.finish_file, tmp, url, announcement, str(response.url), item.get('name') or Path(urlparse(requested_url).path).name, item, allowed_formats)
        finally:
            await response.aclose()
            if tmp and tmp.exists():
                tmp.unlink()

    def finish_file(self, tmp, url, announcement, resolved_url, filename, item, allowed_formats=None):
        size = tmp.stat().st_size
        if size > 100 * 1024 * 1024:
            raise ValueError('Attachment exceeds 100 MB')
        digest = hashlib.sha256()
        with tmp.open('rb') as source:
            prefix = source.read(512)
            digest.update(prefix)
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        try:
            validation = validate_document(tmp, allowed_formats or ['pdf'])
        except ValueError as exc:
            raise type(exc)(f'{exc}; filename={str(filename)[:160]}, bytes={size}, header_hex={prefix[:8].hex()}') from exc
        checksum = digest.hexdigest()
        project = announcement.get('project_name') or announcement.get('title') or announcement.get('project_number') or '招标项目'
        asset_id = hashlib.sha256(json.dumps([announcement['source_url'], project, checksum], ensure_ascii=False).encode()).hexdigest()
        with self.store.file_lock:
            asset = self.store.get(asset_id)
            target = Path(asset['path']) if asset and asset['kind'] == 'file_asset' else None
            reused = bool(target and target.resolve().is_relative_to((self.store.root / 'files').resolve()) and target.is_file())
            if reused:
                downloaded_at = asset['downloaded_at']
                tmp.unlink()
            else:
                moment = datetime.now(ZoneInfo('Asia/Shanghai'))
                while True:
                    target = self.store.root / 'files' / project_filename(project, moment, validation['file_format'])
                    try:
                        destination = target.open('xb')
                        break
                    except FileExistsError:
                        moment += timedelta(microseconds=1)
                try:
                    with destination, tmp.open('rb') as source:
                        shutil.copyfileobj(source, destination)
                    downloaded_at = moment.isoformat()
                    self.store.save('file_asset', {'path': str(target), 'downloaded_at': downloaded_at, 'sha256': checksum}, asset_id)
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
                tmp.unlink()
        result = {**validation, 'source_url': announcement['source_url'], 'download_url': url, 'resolved_url': resolved_url,
            'filename': target.name, 'original_filename': filename, 'project_name': project, 'downloaded_at': downloaded_at,
            'document_type': item.get('document_type', 'unclassified_attachment'),
            'path': str(target), 'sha256': checksum, 'size': size, 'deduplicated': reused,
            'announcement_id': announcement['id'], 'navigation': announcement.get('navigation')}
        result['file_id'] = self.store.save('file', result)
        result['local_download_url'] = '/files/' + result['file_id']
        self.store.save('file', result, result['file_id'])
        return result
