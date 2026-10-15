import hashlib
import asyncio
import re
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from bidding.acquisition import Acquisition, CCGPAdapter, GenericHTMLAdapter, validate_public_url
from bidding.storage import Store
from bidding.file_policy import NonPDFFile, pdf_candidate, project_filename


def test_relative_attachment_and_deduplication():
    result = GenericHTMLAdapter().parse("https://example.org/bid/1", '<h1>Project</h1><a href="../files/bid.pdf">Document</a><a href="../files/bid.pdf">Again</a><script>ignored()</script>')
    assert result["attachments"] == [{"url": "https://example.org/files/bid.pdf", "name": "Document"}]
    assert result["published_at"] is None
    assert "ignored" not in result["text"]


def test_empty_links_not_treated_as_files():
    result = GenericHTMLAdapter().parse("https://example.org/bid", '<a href="">招标文件</a><a href="#">附件下载</a><a href="/bid#files">附件</a>')
    assert result["attachments"] == []


def test_ccgp_published_date_and_purchaser():
    result = CCGPAdapter().parse("https://www.ccgp.gov.cn/bid", '<meta name="PubDate" content="2026-09-25 11:28"><h1>Bid</h1><table><tr><td class="title">采购单位</td><td>Purchaser</td></tr></table>')
    assert result["published_at"] == "2026-09-25 11:28"
    assert result["purchaser"] == "Purchaser"
    assert result["adapter"] == "ccgp_html"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a", "http://user:password@example.org"])
async def test_private_targets_rejected(url):
    with pytest.raises(ValueError):
        await validate_public_url(url)


async def test_download_and_provenance(tmp_path, monkeypatch):
    async def allow(url):
        pass

    monkeypatch.setattr("bidding.acquisition.validate_public_url", allow)
    pdf = b"%PDF-1.7\nfixture"
    def serve(request):
        if request.url.path == "/bid":
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, text='<h1>Bid</h1><a href="/attachment.pdf">File</a>')
        return httpx.Response(200, content=pdf)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        store = Store(tmp_path)
        acquisition = Acquisition(store, client)
        announcement = await acquisition.fetch_tender_detail("https://example.org/bid")
        assert 'raw_path' not in announcement
        assert list((tmp_path / 'files').iterdir()) == []
        assert (await acquisition.discover_tender_files(announcement["announcement_id"]))["status"] == "found"
        first = await acquisition.download_tender_file("https://example.org/attachment.pdf", announcement["announcement_id"])
        second = await acquisition.download_tender_file("https://example.org/attachment.pdf", announcement["announcement_id"])
        assert first["sha256"] == hashlib.sha256(pdf).hexdigest()
        assert second["deduplicated"] is True
        assert first["file_id"] != second["file_id"]
        assert store.get(first["file_id"])["source_url"] == "https://example.org/bid"
        assert re.fullmatch(r'Bid_\d{8}_\d{6}_\d{6}\.pdf', first['filename'])
        assert Path(first['path']).name == first['filename']
        assert first['original_filename'] == 'File'
        assert first['path'] == second['path']
        assert len(list((tmp_path / 'files').iterdir())) == 1


async def test_html_disguised_as_pdf_rejected_and_cleaned(tmp_path, monkeypatch):
    async def allow(url):
        pass

    monkeypatch.setattr("bidding.acquisition.validate_public_url", allow)
    store = Store(tmp_path)
    announcement_id = store.save("announcement", {"source_url": "https://example.org/bid", "attachments": [{"url": "https://example.org/file.pdf"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>Login</html>"))) as client:
        with pytest.raises(ValueError, match="invalid file"):
            await Acquisition(store, client).download_tender_file("https://example.org/file.pdf", announcement_id)
    assert list((tmp_path / "files").iterdir()) == []


async def test_redirect_to_private_target_rejected(tmp_path, monkeypatch):
    seen = []
    async def validate(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("Private target")
    monkeypatch.setattr("bidding.acquisition.validate_public_url", validate)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}))) as client:
        with pytest.raises(ValueError, match="Private"):
            await Acquisition(Store(tmp_path), client).open("https://example.org/bid")
    assert len(seen) == 2


def test_restart_marks_unfinished_task(tmp_path):
    store = Store(tmp_path)
    task_id = store.save("task", {"status": "running"})
    store.recover_tasks()
    assert store.get(task_id)["status"] == "interrupted"


def test_pdf_filter_and_safe_project_filename():
    result = GenericHTMLAdapter().parse('https://example.org/bid', '<h1>电力项目</h1><a href="/f.pdf">公告.pdf</a><a href="/f.docx">标书.docx</a><a href="/f.zip">附件.zip</a><a href="/download?id=1">附件下载</a>')
    assert [item['name'] for item in result['attachments']] == ['公告.pdf', '标书.docx', '附件.zip', '附件下载']
    assert result['excluded_attachments'] == []
    assert pdf_candidate({'name': '公告.pdf', 'url': 'https://example.org/download.html'})
    filename = project_filename('../电力/项目:*?\x00' + '长' * 200, datetime(2026, 10, 9, 12, 30))
    assert '/' not in filename and '\x00' not in filename and len(filename.encode()) < 255
    assert filename.endswith('_20261009_123000_000000.pdf')


async def test_known_non_pdf_rejected_before_network(tmp_path):
    store = Store(tmp_path)
    ident = store.save('announcement', {'source_url': 'https://example.org/bid', 'attachments': [{'url': 'https://example.org/file.zip', 'name': '招标文件.zip'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('Non-PDF must not be downloaded'))) as client:
        acquisition = Acquisition(store, client)
        assert (await acquisition.discover_tender_files(ident))['status'] == 'found'
        with pytest.raises(NonPDFFile):
            await acquisition.download_tender_file('https://example.org/file.zip', ident)
    assert list((tmp_path / 'files').iterdir()) == []


async def test_archive_disguised_as_pdf_not_saved(tmp_path, monkeypatch):
    async def allow(url):
        pass
    monkeypatch.setattr('bidding.acquisition.validate_public_url', allow)
    store = Store(tmp_path)
    ident = store.save('announcement', {'title': '项目', 'source_url': 'https://example.org/bid', 'attachments': [{'url': 'https://example.org/file.pdf'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'PK\x03\x04archive'))) as client:
        with pytest.raises(NonPDFFile):
            await Acquisition(store, client).download_tender_file('https://example.org/file.pdf', ident)
    assert list((tmp_path / 'files').iterdir()) == []


async def test_concurrent_download_dedup_and_project_isolation(tmp_path, monkeypatch):
    async def allow(url):
        pass
    monkeypatch.setattr('bidding.acquisition.validate_public_url', allow)
    store = Store(tmp_path)
    def page(title, url):
        return store.save('announcement', {'title': title, 'source_url': url, 'attachments': [{'url': 'https://example.org/file.pdf', 'name': '标书.pdf'}]})
    first_id = page('电力项目甲', 'https://example.org/a')
    second_id = page('电力项目乙', 'https://example.org/b')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'%PDF-1.7\nfixture'))) as client:
        acquisition = Acquisition(store, client)
        results = await asyncio.gather(*[acquisition.download_tender_file('https://example.org/file.pdf', first_id) for _ in range(2)])
        other = await acquisition.download_tender_file('https://example.org/file.pdf', second_id)
    assert results[0]['path'] == results[1]['path']
    assert sum(result['deduplicated'] for result in results) == 1
    assert other['path'] != results[0]['path']
    assert other['filename'].startswith('电力项目乙_')
    assert len(list((tmp_path / 'files').glob('*.pdf'))) == 2


async def test_same_timestamp_never_overwrites_other_attachment(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 12, 30, tzinfo=tz)
    monkeypatch.setattr('bidding.acquisition.datetime', FixedDatetime)
    store = Store(tmp_path)
    acquisition = Acquisition(store)
    announcement = {'id': 'parent', 'title': '项目', 'source_url': 'https://example.org/bid'}
    results = []
    for index in range(2):
        temporary = tmp_path / 'files' / f'temp{index}'
        temporary.write_bytes(b'%PDF-1.7\n' + str(index).encode())
        results.append(acquisition.finish_file(temporary, 'https://example.org/file.pdf', announcement, 'https://example.org/file.pdf', '标书.pdf', {}))
    assert results[0]['filename'].endswith('000000.pdf')
    assert results[1]['filename'].endswith('000001.pdf')
    assert Path(results[0]['path']).read_bytes() != Path(results[1]['path']).read_bytes()
    await acquisition.close()
