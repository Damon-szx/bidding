import hashlib
import base64
import io
import os
import zipfile
import asyncio

import httpx
import pytest

from bidding.acquisition import Acquisition, GenericHTMLAdapter, SGCCAdapter
from bidding.context import compact_preview
from bidding.storage import Store


@pytest.mark.skipif(not os.getenv('RUN_BROWSER_TESTS'), reason='Requires installed Chrome/Chromium')
@pytest.mark.parametrize('load_more', [False, True])
@pytest.mark.parametrize('header_without_thead', [False, True])
async def test_js_listing_cursor_restores_after_detail_then_advances(tmp_path, monkeypatch, load_more, header_without_thead):
    acquisition = Acquisition(Store(tmp_path))
    browser = acquisition.browser
    async def validate(url):
        assert url.startswith('https://fixture.example/')
    browser.validate = validate
    original_session = browser.new_session
    async def fixture_session():
        ident, session = await original_session()
        async def fixture(route):
            body = '<h1>项目详情</h1><article>' + ('项目原文 ' * 100) + '</article>' if '/detail' in route.request.url else '''<h1>项目列表</h1>
                <table><thead><tr><th>项目名称</th><th>编号</th><th>状态</th><th>发布日期</th></tr></thead><tbody></tbody></table>
                <button onclick="n++;render()">Next page</button>
                <script>let n=1;function render(){document.querySelector('tbody').innerHTML=
                `<tr><td><a href="javascript:void(0)" onclick="location.href='/fixture/detail'">采购项目${n}</a></td><td>P${n}</td><td>公开</td><td>2026-09-30</td></tr>`;}render();</script>'''
            if load_more and '/detail' not in route.request.url:
                body = body.replace('Next page', 'Load more').replace('n++;render()', 'n++;more()')
                body += '''<script>function more(){document.querySelector('tbody').insertAdjacentHTML('beforeend',
                    `<tr><td><a href="javascript:void(0)" onclick="location.href='/fixture/detail'">采购项目${n}</a></td><td>P${n}</td><td>公开</td><td>2026-09-30</td></tr>`);}</script>'''
            if '/detail' not in route.request.url:
                body = body.replace('<tbody>', '<tbody class="data">').replace("querySelector('tbody')", "querySelector('tbody.data')")
                if header_without_thead:
                    body = body.replace('<thead>', '').replace('</thead>', '')
            await route.fulfill(content_type='text/html; charset=utf-8', body=body)
        await session['context'].route('https://fixture.example/fixture/**', fixture)
        return ident, session
    monkeypatch.setattr(browser, 'new_session', fixture_session)
    try:
        first = await acquisition.render_tender_page('https://fixture.example/fixture/list')
        assert first['entries'][0]['date_kind'] == 'published_at'
        second = await acquisition.navigate_tender_entry(first['announcement_id'], first['pagination'][0]['entry_id'])
        assert second['entries'][-1]['title'] == '采购项目2'
        assert second['pagination'][0]['mechanism'] == ('load_more' if load_more else 'click')
        detail = await acquisition.navigate_tender_entry(second['announcement_id'], second['entries'][-1]['entry_id'])
        assert detail['page_type'] == 'detail'
        third = await acquisition.navigate_tender_entry(second['announcement_id'], second['pagination'][0]['entry_id'])
        assert third['entries'][-1]['title'] == '采购项目3'
        assert len(third['listing_path']['actions']) == 2
    finally:
        await acquisition.close()


def test_spa_links_and_static_project_entries():
    result = GenericHTMLAdapter().parse('https://example.org/portal/#/list', '<table><tbody><tr><td><a href="#/doc/123">电力采购项目公告</a></td></tr></tbody></table>')
    assert result['links'][0]['url'] == 'https://example.org/portal/#/doc/123'
    assert result['entries'][0]['url'] == 'https://example.org/portal/#/doc/123'
    assert result['page_type'] == 'listing'


def test_sgcc_detail_metadata():
    result = SGCCAdapter().parse('https://ecp.sgcc.com.cn/ecp2.0/portal/#/doc/doc-spec/123_2018032700290425', '<h1>北京电力监理项目</h1><p>【项目编号：DLF1A260036】</p><p>2026-09-30</p>')
    assert result['project_number'] == 'DLF1A260036'
    assert result['published_at'] == '2026-09-30'
    assert result['information_type'] == 'prequalification_notice'


def test_browser_projection_preserves_entry_and_attachment_actions():
    raw = {'browser_session_id': 'private-session', 'announcement_id': 'list', 'source_url': 'https://example.org/#/list',
        'page_type': 'listing', 'entries': [{'entry_id': 'ref1', 'title': '电力项目', 'url': None}],
        'attachments': [{'url': 'browser-attachment:ref2', 'name': '资格预审.pdf'}],
        'network': [{'url': 'x' * 30000}], 'text': '导航' * 10000}
    reduced = compact_preview(raw, 'log', 2000)
    assert reduced['data']['entries'][0]['entry_id'] == 'ref1'
    assert reduced['data']['attachments'][0]['url'] == 'browser-attachment:ref2'
    assert 'network' not in reduced['data'] and 'browser_session_id' not in reduced['data']


async def test_hash_route_auto_renders_without_static_request(tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail('Hash route should render'))) as client:
        acquisition = Acquisition(Store(tmp_path), client)
        async def render(url):
            return {'source_url': url, 'page_type': 'listing'}
        monkeypatch.setattr(acquisition, 'render_tender_page', render)
        result = await acquisition.fetch_tender_detail('https://example.org/#/list')
        assert result['page_type'] == 'listing'


async def test_unobserved_entry_and_attachment_rejected(tmp_path):
    acquisition = Acquisition(Store(tmp_path))
    ident = acquisition.store.save('announcement', {'source_url': 'https://example.org', 'entries': [], 'attachments': []})
    try:
        with pytest.raises(ValueError, match='observed'):
            await acquisition.navigate_tender_entry(ident, 'invented')
        with pytest.raises(ValueError, match='discovered'):
            await acquisition.download_tender_file('browser-attachment:invented', ident)
    finally:
        await acquisition.close()


@pytest.mark.skipif(not os.getenv('RUN_BROWSER_TESTS'), reason='Requires installed Chrome/Chromium')
async def test_expired_primary_does_not_block_javascript_announcement_download(tmp_path, monkeypatch):
    acquisition = Acquisition(Store(tmp_path))
    browser = acquisition.browser
    async def validate(url):
        assert url.startswith('https://fixture.example/')
    browser.validate = validate
    original_session = browser.new_session
    async def fixture_session():
        ident, session = await original_session()
        async def fixture(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body='''<h1>招标公告</h1>
                <button onclick="alert('不在文件获取时间内!')">获取招标文件</button>
                <a href="javascript:void(0)" onclick="const a=document.createElement('a');
                a.href=window.URL.createObjectURL(new Blob(['%PDF-1.7\\nannouncement fixture']));
                a.download='announcement.pdf';a.click()">下载公告文件</a>
                <a href="javascript:void(0)">下载投标邀请书</a>''')
        await session['context'].route('https://fixture.example/fixture/**', fixture)
        return ident, session
    monkeypatch.setattr(browser, 'new_session', fixture_session)
    try:
        page = await acquisition.render_tender_page('https://fixture.example/fixture/detail')
        primary = next(item for item in page['document_access'] if item['title'] == '获取招标文件')
        attachment = next(item for item in page['attachments'] if item['name'] == '下载公告文件')
        assert attachment['download_mode'] == 'browser_click' and not attachment['href']
        assert any(item['name'] == '下载投标邀请书' for item in page['attachments'])
        access = await acquisition.inspect_document_access(page['announcement_id'], primary['entry_id'])
        assert access['document_access_status'] == 'blocked'
        browser.timeout = 5
        saved = await acquisition.download_tender_file(attachment['url'], page['announcement_id'])
        from pathlib import Path
        assert Path(saved['path']).read_bytes().startswith(b'%PDF-1.7')
    finally:
        await acquisition.close()


async def test_file_endpoint_serves_only_saved_files(tmp_path, monkeypatch):
    import bidding.api as api
    store = Store(tmp_path)
    monkeypatch.setattr(api, 'store', store)
    path = tmp_path / 'files' / 'fixture.pdf'
    path.write_bytes(b'%PDF-1.7\nfixture')
    ident = store.save('file', {'path': str(path), 'filename': '资格预审公告.pdf'})
    outside = tmp_path / 'outside.pdf'
    outside.write_bytes(b'%PDF-1.7\nprivate')
    invalid = store.save('file', {'path': str(outside)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
        result = await client.get('/files/' + ident)
        assert result.content.startswith(b'%PDF-')
        assert result.headers['content-disposition'].startswith('attachment;')
        assert result.headers['x-content-type-options'] == 'nosniff'
        assert (await client.get('/files/' + invalid)).status_code == 404
        assert (await client.get('/files/missing')).status_code == 404


@pytest.mark.skipif(not os.getenv('RUN_BROWSER_TESTS'), reason='Requires installed Chrome/Chromium')
async def test_render_popup_download_pagination_and_network_guard(tmp_path, monkeypatch):
    acquisition = Acquisition(Store(tmp_path))
    browser = acquisition.browser
    async def validate(url):
        if not url.startswith('https://fixture.example/'):
            raise ValueError('Private network blocked')
    browser.validate = validate
    original_session = browser.new_session
    list_html = '''<html><title>招标列表</title><body><iframe src="http://127.0.0.1/secret"></iframe>
      <div id="root"></div><script>setTimeout(()=>{document.querySelector('#root').innerHTML =
      `<table><thead><tr><th>项目名称</th><th>项目编号</th><th>项目状态</th><th>创建时间</th></tr></thead><tbody>
      <tr class="cur" onclick="window.open('/fixture/detail','_blank')"><td>电力监理项目</td><td>D1</td><td>正在招标</td><td>2026-09-30</td></tr></tbody></table>
      <div class="pagination"><button onclick="document.querySelector('tbody td').innerText='第二页电力项目'">下一页</button></div>`},50);</script></body></html>'''
    detail_html = '''<html><h1>电力监理项目</h1><p>项目编号：D1</p><a href="javascript:void(0)" onclick="const a=document.createElement('a');a.href=window.URL.createObjectURL(new Blob(['%PDF-1.7\\nfixture']));a.download='资格预审公告.pdf';a.click()">资格预审公告.pdf</a></html>'''
    async def session_with_fixture():
        ident, session = await original_session()
        async def fixture(route):
            await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=detail_html if '/detail' in route.request.url else list_html)
        await session['context'].route('https://fixture.example/fixture/**', fixture)
        return ident, session
    monkeypatch.setattr(browser, 'new_session', session_with_fixture)
    try:
        listing = await acquisition.render_tender_page('https://fixture.example/fixture/list')
        assert 'raw_path' not in listing and 'screenshot_path' not in listing
        assert list((tmp_path / 'files').iterdir()) == []
        assert listing['page_type'] == 'listing'
        assert listing['entries'][0]['url'] is None
        assert any(item['status'] == 'blocked' for item in listing['network'])
        assert listing['pagination']
        detail = await acquisition.navigate_tender_entry(listing['announcement_id'], listing['entries'][0]['entry_id'])
        assert detail['source_url'].endswith('/fixture/detail')
        page_count = len(browser.sessions[listing['browser_session_id']]['context'].pages)
        repeated = await acquisition.navigate_tender_entry(listing['announcement_id'], listing['entries'][0]['entry_id'])
        assert repeated['announcement_id'] == detail['announcement_id'] and repeated['reused']
        assert len(browser.sessions[listing['browser_session_id']]['context'].pages) == page_count
        files = await acquisition.discover_tender_files(detail['announcement_id'])
        assert files['files'][0]['url'].startswith('browser-attachment:')
        result = await acquisition.download_tender_file(files['files'][0]['url'], detail['announcement_id'])
        assert result['sha256'] == hashlib.sha256(b'%PDF-1.7\nfixture').hexdigest()
        assert result['document_type'] == 'prequalification_notice'
        assert result['navigation']['parent_announcement_id'] == listing['announcement_id']
        result2 = await acquisition.download_tender_file(files['files'][0]['url'], detail['announcement_id'])
        assert result2['deduplicated']
        next_page = await acquisition.navigate_tender_entry(listing['announcement_id'], listing['pagination'][0]['entry_id'])
        assert next_page['entries'][0]['title'] == '第二页电力项目'
        assert [path.suffix for path in (tmp_path / 'files').iterdir()] == ['.pdf']
        await browser.close()
        with pytest.raises(ValueError, match='expired'):
            browser.lookup(acquisition.store.get(detail['announcement_id']))
    finally:
        await acquisition.close()


@pytest.mark.skipif(not os.getenv('RUN_BROWSER_TESTS'), reason='Requires installed Chrome/Chromium')
@pytest.mark.parametrize('format', ['pdf', 'zip'])
async def test_public_announcement_button_is_downloadable_without_access_inspection(tmp_path, monkeypatch, format):
    acquisition = Acquisition(Store(tmp_path))
    browser = acquisition.browser
    async def validate(url):
        assert url.startswith('https://fixture.example/')
    browser.validate = validate
    original_session = browser.new_session
    content = b'%PDF-1.7\nfixture'
    if format == 'zip':
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('announcement.pdf', content)
        content = output.getvalue()
    payload = base64.b64encode(content).decode()
    async def fixture_session():
        ident, session = await original_session()
        async def fixture(route):
            body = f'''<html><h1>招标公告</h1><button onclick="const a=document.createElement('a');
                a.href=window.URL.createObjectURL(new Blob([Uint8Array.from(atob('{payload}'),c=>c.charCodeAt(0))]));
                a.download='announcement.{format}';a.click()">下载公告文件</button></html>'''
            await route.fulfill(content_type='text/html; charset=utf-8', body=body)
        await session['context'].route('https://fixture.example/fixture/**', fixture)
        return ident, session
    monkeypatch.setattr(browser, 'new_session', fixture_session)
    try:
        page = await acquisition.render_tender_page('https://fixture.example/fixture/detail')
        attachment = page['attachments'][0]
        assert attachment['document_type'] == 'unclassified_attachment'
        assert attachment['content_verified'] is False
        assert attachment['attachment_id'] == page['document_access'][0]['entry_id']
        result = await acquisition.download_tender_file(attachment['url'], page['announcement_id'], allowed_formats=[format])
        assert result['sha256'] == hashlib.sha256(content).hexdigest()
        assert result['file_format'] == format
        assert result['document_type'] != 'tender_document'
    finally:
        await acquisition.close()


@pytest.mark.skipif(not os.getenv('RUN_BROWSER_TESTS'), reason='Requires installed Chrome/Chromium')
@pytest.mark.parametrize('login_alert', [False, True])
async def test_document_access_button_reports_login_barrier_without_submitting(tmp_path, monkeypatch, login_alert):
    acquisition = Acquisition(Store(tmp_path))
    browser = acquisition.browser
    async def validate(url):
        assert url.startswith('https://fixture.example/')
    browser.validate = validate
    original_session = browser.new_session
    requests = []
    async def fixture_session():
        ident, session = await original_session()
        async def fixture(route):
            requests.append(route.request.method)
            body = '<h1>登录</h1><input type="password"><button>提交</button>' if '/login' in route.request.url else '<h1>招标公告</h1><button onclick="location.href=\'/fixture/login\'">获取招标文件</button>'
            if login_alert:
                body = '<h1>招标公告</h1><button onclick="alert(\'请先登录BOSP系统获取文件\')">获取招标文件</button>'
            await route.fulfill(content_type='text/html; charset=utf-8', body=body)
        await session['context'].route('https://fixture.example/fixture/**', fixture)
        return ident, session
    monkeypatch.setattr(browser, 'new_session', fixture_session)
    try:
        page = await acquisition.render_tender_page('https://fixture.example/fixture/detail')
        assert not page['attachments']
        assert page['document_access'][0]['title'] == '获取招标文件'
        result = await asyncio.wait_for(acquisition.inspect_document_access(
            page['announcement_id'], page['document_access'][0]['entry_id']), timeout=8)
        assert result['document_access_status'] == 'requires_user_action'
        assert result['source_url'].endswith('/detail' if login_alert else '/login')
        assert result['access_source']['announcement_id'] == page['announcement_id']
        assert result['access_error']['code'] == 'DOCUMENT_ACCESS_REQUIRES_USER_ACTION'
        if login_alert:
            assert result['access_error']['message'] == '请先登录BOSP系统获取文件'
        assert set(requests) == {'GET'}
        assert not list((tmp_path / 'files').iterdir())
    finally:
        await acquisition.close()


@pytest.mark.skipif(not os.getenv('RUN_BROWSER_TESTS'), reason='Requires installed Chrome/Chromium')
async def test_document_access_alert_is_logged_and_survives_projection(tmp_path, monkeypatch):
    acquisition = Acquisition(Store(tmp_path))
    browser = acquisition.browser
    async def validate(url):
        assert url.startswith('https://fixture.example/')
    browser.validate = validate
    original_session = browser.new_session
    async def fixture_session():
        ident, session = await original_session()
        async def fixture(route):
            await route.fulfill(content_type='text/html; charset=utf-8', body='''<h1>招标公告</h1>
                <button onclick="alert('不在文件获取时间内!')">获取招标文件</button>''')
        await session['context'].route('https://fixture.example/fixture/**', fixture)
        return ident, session
    monkeypatch.setattr(browser, 'new_session', fixture_session)
    try:
        page = await acquisition.render_tender_page('https://fixture.example/fixture/detail')
        result = await acquisition.inspect_document_access(page['announcement_id'], page['document_access'][0]['entry_id'])
        assert result['document_access_status'] == 'blocked'
        assert result['access_error']['message'] == '不在文件获取时间内!'
        assert result['access_dialogs'] == [{'type': 'alert', 'message': '不在文件获取时间内!'}]
        assert acquisition.store.get(result['announcement_id'])['access_error'] == result['access_error']
        projected = compact_preview(result, 'source-log', 2000)
        assert projected['data']['access_error'] == result['access_error']
        assert projected['data']['access_dialogs'] == result['access_dialogs']
        assert not list((tmp_path / 'files').iterdir())
        browser.timeout = 0.2
        record = acquisition.store.get(result['announcement_id'])
        with pytest.raises(ValueError, match='网站拒绝下载，页面提示：不在文件获取时间内!'):
            await browser.download(record, result['document_access'][0], str(tmp_path / 'rejected.pdf'))
        # A later successful inspection must not inherit a previous alert.
        _, session, target = browser.lookup(acquisition.store.get(result['announcement_id']))
        await target.evaluate("document.querySelector('button').onclick = () => {}")
        clean = await acquisition.inspect_document_access(result['announcement_id'], result['document_access'][0]['entry_id'])
        assert clean['access_dialogs'] == []
        assert 'access_error' not in clean
    finally:
        await acquisition.close()
