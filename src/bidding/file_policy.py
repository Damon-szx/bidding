"""Bounded document validation and safe, readable project filenames."""
import re
import stat
import struct
import zipfile
import io
from pathlib import Path
from urllib.parse import unquote, urlparse


class NonPDFFile(ValueError):
    pass


SUPPORTED_FORMATS = ('pdf', 'docx', 'zip')


def validate_office_member(content, extension):
    """Validate nested Office documents without extracting or executing their contents."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as office:
            entries = office.infolist()
            required = {'[Content_Types].xml', 'word/document.xml' if extension == '.docx' else 'xl/workbook.xml'}
            if not required <= {item.filename for item in entries}:
                raise ValueError('Archive Office member does not match its extension')
            if len(entries) > 1000 or sum(item.file_size for item in entries) > 200 * 1024**2:
                raise ValueError('Nested Office document exceeds safety limit')
            for item in entries:
                name = item.filename.replace('\\', '/').lower()
                if (name.startswith('/') or '..' in name.split('/') or re.match(r'^[a-z]:', name)
                        or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1):
                    raise ValueError('Unsafe or encrypted nested Office entry')
                if (name.endswith(('vbaproject.bin', '.exe', '.js', '.vbs', '.bat', '.cmd', '.ps1', '.com', '.dll'))
                        or '/embeddings/' in name or name.startswith(('xl/macrosheets/', 'xl/dialogsheets/'))):
                    raise ValueError('Active or embedded document content is not supported')
                if item.file_size > 100 * 1024**2 or item.file_size > max(1, item.compress_size) * 200:
                    raise ValueError('Nested Office expansion exceeds safety limit')
                with office.open(item) as source:
                    while source.read(65536):
                        pass
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Invalid nested Office document') from exc


def file_candidate(item, formats=SUPPORTED_FORMATS):
    values = [item.get('name') or '', unquote(urlparse(item.get('href') or item.get('url') or '').path)]
    suffixes = {Path(value.strip()).suffix.lower() for value in values}
    if suffixes & {'.' + value for value in formats}:
        return True
    known = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar', '.7z', '.exe', '.js', '.png', '.jpg', '.html', '.txt'}
    return not bool(suffixes & known)


def validate_document(path, formats):
    if not formats or set(formats) - set(SUPPORTED_FORMATS):
        raise ValueError('Unsupported requested file formats')
    with path.open('rb') as source:
        header = source.read(8)
    if header.startswith(b'%PDF-'):
        detected, members = 'pdf', []
    elif zipfile.is_zipfile(path):
        try:
            # Some portal ZIPs omit the central-directory UTF-8 flag for Chinese names.
            try:
                opened = zipfile.ZipFile(path, metadata_encoding='utf-8')
            except UnicodeDecodeError:
                opened = zipfile.ZipFile(path)
            with opened as archive:
                entries = archive.infolist()
                names = {entry.filename for entry in entries}
                detected = 'docx' if {'[Content_Types].xml', 'word/document.xml'} <= names else 'zip'
                if len(entries) > 1000 or sum(entry.file_size for entry in entries) > 200 * 1024**2:
                    raise ValueError('Archive exceeds entry or expanded-size limit')
                members = []
                for entry in entries:
                    with path.open('rb') as source:
                        source.seek(entry.header_offset)
                        local = source.read(30)
                        if len(local) != 30 or local[:4] != b'PK\x03\x04':
                            raise ValueError('Invalid archive local header')
                        flags = struct.unpack_from('<H', local, 6)[0]
                        raw_name = source.read(struct.unpack_from('<H', local, 26)[0])
                    encoding = 'utf-8' if flags & 0x800 else opened.metadata_encoding or 'cp437'
                    local_name = raw_name.decode(encoding)
                    if local_name != entry.orig_filename:
                        try:
                            original_bytes = entry.orig_filename.encode('cp437')
                        except UnicodeEncodeError:
                            raise ValueError('Archive filename mismatch') from None
                        candidates = []
                        for name_encoding in ('utf-8', 'gb18030'):
                            try:
                                candidates.append(original_bytes.decode(name_encoding))
                            except UnicodeDecodeError:
                                pass
                        if local_name not in candidates:
                            raise ValueError('Archive filename mismatch')
                        # Accept only an encoding roundtrip matching the local name; retain CRC checks.
                        entry.filename = entry.orig_filename = local_name
                    name = entry.filename.replace('\\', '/')
                    if name.startswith('/') or '..' in name.split('/') or re.match(r'^[A-Za-z]:', name) or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                        raise ValueError('Unsafe or encrypted archive entry')
                    if entry.file_size > 100 * 1024**2 or entry.file_size > max(1, entry.compress_size) * 200:
                        raise ValueError('Archive expansion exceeds safety limit')
                    if entry.is_dir():
                        continue
                    extension = Path(name).suffix.lower()
                    if detected == 'zip' and extension not in ('.pdf', '.docx', '.xlsx'):
                        raise ValueError('Archive contains unsupported files; only PDF/DOCX/XLSX document bundles are supported')
                    if detected == 'docx' and (name.lower().endswith(('vbaproject.bin', '.exe', '.js')) or name.lower().startswith('word/embeddings/')):
                        raise ValueError('Active or embedded document content is not supported')
                    with archive.open(entry) as content:
                        first = content.read(8)
                        if detected == 'zip' and not (first.startswith(b'%PDF-') if extension == '.pdf' else first.startswith(b'PK\x03\x04')):
                            raise ValueError('Archive document content does not match its extension')
                        if detected == 'zip' and extension in ('.docx', '.xlsx'):
                            validate_office_member(first + content.read(), extension)
                        else:
                            while content.read(65536):
                                pass
                    members.append({'name': name, 'size': entry.file_size})
                if detected == 'zip' and not members:
                    raise ValueError('Empty document archive')
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError('Invalid document archive') from exc
    else:
        raise NonPDFFile('Unsupported or invalid document content; not PDF, DOCX or ZIP')
    if detected not in formats:
        raise NonPDFFile('Actual file format ' + detected + ' does not match requested formats: ' + ', '.join(formats))
    return {'file_format': detected, 'archive_members': members[:100], 'archive_member_count': len(members),
        'size': path.stat().st_size, 'header_hex': header.hex()}


def pdf_candidate(item):
    known_other = {'.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar', '.7z', '.png', '.jpg', '.jpeg', '.txt', '.html'}
    values = [item.get('name') or '', unquote(urlparse(item.get('href') or item.get('url') or '').path)]
    suffixes = [Path(value.strip()).suffix.lower() for value in values]
    # An endpoint may use a misleading extension. Any explicit PDF label warrants content validation.
    if '.pdf' in suffixes:
        return True
    return not any(suffix in known_other for suffix in suffixes)


def split_attachments(items, formats=SUPPORTED_FORMATS):
    return ([item for item in items if file_candidate(item, formats)], [item for item in items if not file_candidate(item, formats)])


def project_filename(project, moment, extension='pdf'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(project or '招标项目'))
    name = re.sub(r'\s+', ' ', name).strip(' ._') or '招标项目'
    name = name.encode('utf-8')[:180].decode('utf-8', errors='ignore').rstrip(' ._')
    if extension not in SUPPORTED_FORMATS:
        raise ValueError('Unsupported filename extension')
    return name + '_' + moment.strftime('%Y%m%d_%H%M%S_%f') + '.' + extension
