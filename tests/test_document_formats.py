import io
import struct
import zipfile

import httpx
import pytest

from bidding.acquisition import Acquisition
from bidding.agent import Agent
from bidding.batch import BatchManager
from bidding.file_policy import NonPDFFile, validate_document
from bidding.storage import Store
from bidding.model import encoded
from model_fixtures import planning_response


def archive_bytes(files, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def docx_bytes():
    return archive_bytes({'[Content_Types].xml': '<Types/>', 'word/document.xml': '<document/>'})


def test_public_notice_bundle_allows_valid_xlsx_schedules(tmp_path):
    path = tmp_path / 'notice.zip'
    xlsx = archive_bytes({'[Content_Types].xml': '<Types/>', 'xl/workbook.xml': '<workbook/>'})
    path.write_bytes(archive_bytes({'notice.docx': docx_bytes(), 'schedule.xlsx': xlsx}))
    result = validate_document(path, ['zip'])
    assert result['archive_member_count'] == 2
    assert result['file_format'] == 'zip'


@pytest.mark.parametrize('extra', [
    {'xl/vbaProject.bin': b'macro'}, {'xl/embeddings/object.bin': b'active'},
    {'xl/macrosheets/sheet1.xml': '<macro/>'}, {'../escape.xml': '<xml/>'},
])
def test_nested_office_active_content_is_rejected(tmp_path, extra):
    path = tmp_path / 'notice.zip'
    xlsx = archive_bytes({'[Content_Types].xml': '<Types/>', 'xl/workbook.xml': '<workbook/>', **extra})
    path.write_bytes(archive_bytes({'schedule.xlsx': xlsx}))
    with pytest.raises(ValueError):
        validate_document(path, ['zip'])


def test_nested_xlsx_extension_requires_workbook_structure(tmp_path):
    path = tmp_path / 'notice.zip'
    path.write_bytes(archive_bytes({'schedule.xlsx': docx_bytes()}))
    with pytest.raises(ValueError, match='extension'):
        validate_document(path, ['zip'])


@pytest.mark.parametrize('content,expected', [
    (b'%PDF-1.7\nfixture', 'pdf'),
    (docx_bytes(), 'docx'),
    (archive_bytes({'change.docx': docx_bytes()}), 'zip'),
])
async def test_download_preserves_detected_format_not_misleading_url(tmp_path, monkeypatch, content, expected):
    async def allow(url):
        pass
    monkeypatch.setattr('bidding.acquisition.validate_public_url', allow)
    store = Store(tmp_path)
    url = 'https://example.org/download'
    page = store.save('announcement', {'title': 'Project', 'source_url': 'https://example.org/detail',
        'attachments': [{'url': url, 'name': 'Requested document'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=content))) as client:
        acquisition = Acquisition(store, client)
        result = await acquisition.download_tender_file(url, page, allowed_formats=['pdf', 'docx', 'zip'])
        assert result['file_format'] == expected
        assert result['filename'].endswith('.' + expected)
        assert result['size'] == len(content)
        assert store.get(result['file_id'])['archive_member_count'] == result['archive_member_count']


def test_pdf_only_rejects_valid_zip_without_widening_request(tmp_path):
    path = tmp_path / 'bundle'
    path.write_bytes(archive_bytes({'change.docx': docx_bytes()}))
    with pytest.raises(NonPDFFile, match='does not match requested formats'):
        validate_document(path, ['pdf'])


@pytest.mark.parametrize('files', [
    {'../change.docx': docx_bytes()}, {'/change.docx': docx_bytes()},
    {'C:\\change.docx': docx_bytes()}, {'install.exe': b'MZ'},
    {'nested.zip': b'PK'}, {},
    {'[Content_Types].xml': '<Types/>', 'word/document.xml': '<document/>', 'word/vbaProject.bin': b'active'},
])
def test_unsafe_archives_are_rejected(tmp_path, files):
    path = tmp_path / 'bundle'
    path.write_bytes(archive_bytes(files))
    with pytest.raises(ValueError):
        validate_document(path, ['pdf', 'docx', 'zip'])


def test_archive_expansion_ratio_is_bounded(tmp_path):
    path = tmp_path / 'bundle'
    path.write_bytes(archive_bytes({'huge.pdf': b'0' * 1000000}, zipfile.ZIP_DEFLATED))
    with pytest.raises(ValueError, match='expansion'):
        validate_document(path, ['zip'])


def test_portal_zip_with_missing_central_utf8_flag_still_validates_crc_and_names(tmp_path):
    data = bytearray(archive_bytes({'变更公告.docx': docx_bytes()}))
    directory = data.rindex(b'PK\x01\x02')
    flags = struct.unpack_from('<H', data, directory + 8)[0]
    struct.pack_into('<H', data, directory + 8, flags & ~0x800)
    path = tmp_path / 'portal.zip'
    path.write_bytes(data)
    result = validate_document(path, ['zip'])
    assert result['archive_members'][0]['name'] == '变更公告.docx'
    assert result['file_format'] == 'zip'


@pytest.mark.parametrize('directory_encoding', ['mojibake', 'gbk'])
def test_portal_zip_with_mixed_directory_and_local_name_encoding(tmp_path, directory_encoding):
    name = '变更公告.docx'
    data = bytearray(archive_bytes({name: docx_bytes()}))
    directory = data.rindex(b'PK\x01\x02')
    old = name.encode('utf-8')
    garbled = old.decode('cp437').encode('utf-8') if directory_encoding == 'mojibake' else name.encode('gbk')
    size = struct.unpack_from('<H', data, directory + 28)[0]
    data[directory + 46:directory + 46 + size] = garbled
    struct.pack_into('<H', data, directory + 28, len(garbled))
    if directory_encoding == 'gbk':
        struct.pack_into('<H', data, directory + 8, 0)
    end = data.index(b'PK\x05\x06', directory)
    central_size = struct.unpack_from('<I', data, end + 12)[0]
    struct.pack_into('<I', data, end + 12, central_size + len(garbled) - len(old))
    path = tmp_path / 'portal.zip'
    path.write_bytes(data)
    assert validate_document(path, ['zip'])['archive_members'][0]['name'] == name


async def test_batch_downloads_only_selected_change_document(tmp_path, monkeypatch):
    async def allow(url):
        pass
    monkeypatch.setattr('bidding.acquisition.validate_public_url', allow)
    store = Store(tmp_path)
    base = 'https://example.org/'
    page = {'source_url': base + 'detail', 'page_type': 'detail', 'title': 'Project', 'attachments': [
        {'url': base + 'original.zip', 'name': 'Original announcement'},
        {'url': base + 'change.zip', 'name': 'Change announcement'},
        {'url': base + 'unrelated.pdf', 'name': 'Unrelated file'}]}
    page['announcement_id'] = store.save('announcement', page)
    requested = []
    def response(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=archive_bytes({'change.docx': docx_bytes()}))
    def factory(worker_store):
        acquisition = Acquisition(worker_store, httpx.AsyncClient(transport=httpx.MockTransport(response)))
        async def fetch(url):
            return page
        acquisition.fetch_tender_detail = fetch
        return acquisition
    manager = BatchManager(store, factory)
    manager.closed = True
    batch = manager.create(page['announcement_id'], 'count', 1, 'Download change notice only', 'task', None,
        criteria={'file_formats': ['pdf', 'docx', 'zip']}, attachment_urls=[base + 'change.zip'])
    await manager.run(batch['id'])
    assert requested == [base + 'change.zip']
    assert store.get(batch['id'])['status'] == 'completed'
    assert store.items(batch['id'])[0]['payload']['files'][0]['file_format'] == 'zip'
    assert len(store.list_records('file')) == 1


def test_batch_rejects_unobserved_attachment_selection(tmp_path):
    store = Store(tmp_path)
    page = store.save('announcement', {'source_url': 'https://example.org/detail', 'page_type': 'detail', 'attachments': []})
    manager = BatchManager(store)
    manager.closed = True
    with pytest.raises(ValueError, match='observed attachment'):
        manager.create(page, 'count', 1, 'Download', 'task', None, attachment_urls=['https://example.org/invented.zip'])


async def test_agent_requires_selection_instead_of_downloading_every_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY', 'fixture')
    monkeypatch.setenv('CONTEXT_COMPACT_THRESHOLD_TOKENS', '30000')
    store = Store(tmp_path)
    page = {'source_url': 'https://example.org/detail', 'page_type': 'detail', 'title': 'Project', 'attachments': [
        {'url': 'https://example.org/original.zip', 'name': 'Original announcement'},
        {'url': 'https://example.org/change.zip', 'name': 'Change announcement'}]}
    page['announcement_id'] = store.save('announcement', page)
    actions = iter([('fetch_tender_detail', {'url': page['source_url']}),
        ('plan_acquisition', {'announcement_id': page['announcement_id'], 'scope': 'count', 'count': 1}),
        ('plan_acquisition', {'announcement_id': page['announcement_id'], 'scope': 'count', 'count': 1,
            'attachment_urls': ['https://example.org/change.zip']})])
    def response(request):
        if result := planning_response(request, action='download', file_goal='public_files', file_formats=['pdf', 'docx', 'zip']):
            return result
        name, arguments = next(actions)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': encoded(arguments)}}]}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr('bidding.agent.httpx.AsyncClient', lambda **kwargs: client_class(transport=httpx.MockTransport(response)))
    acquisition = Acquisition(store, client_class())
    async def fetch(url):
        return page
    acquisition.fetch_tender_detail = fetch
    manager = BatchManager(store)
    manager.closed = True
    try:
        await Agent(acquisition, manager).run('仅下载此页面的变更公告附件，不限制格式', 'task')
        task = store.get('task')
        assert task['status'] == 'scheduled'
        contract = store.get(task['batch_id'])['contract']
        assert contract['selected_attachment_keys'] == ['Change announcement']
        assert contract['file_formats'] == ['pdf', 'docx', 'zip']
        logs = [log for log in store.logs('task') if log['tool'] == 'plan_acquisition']
        assert logs[0]['status'] == 'failed'
        assert 'Select observed attachment_urls' in logs[0]['error']
        assert logs[1]['status'] == 'completed'
    finally:
        await acquisition.close()
