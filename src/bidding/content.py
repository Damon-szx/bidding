"""Deterministic source-text blocks; no model summaries or business judgments."""
import hashlib
import json
import re


BLOCK_CHARS = 900
# Structural headings only. These labels never decide project eligibility.
HEADING = re.compile(r'^(?:第[一二三四五六七八九十百\d]+[章节部分]|[一二三四五六七八九十]+[、.．]|\d{1,2}[、.．](?!\d)|[（(][一二三四五六七八九十\d]+[）)])')


def index_content(page):
    """Return a fresh index over the exact saved text, preserving every character."""
    text = page.get('text') or ''
    if not isinstance(text, str):
        return dict(page)
    headings = set(page.get('content_headings', []))
    version = hashlib.sha256(json.dumps(['blocks-v1', BLOCK_CHARS, page.get('source_url'), text,
        sorted(headings)], ensure_ascii=False).encode()).hexdigest()[:20]
    ranges, start, cursor = [], 0, 0
    for line in text.splitlines(keepends=True):
        label = line.strip()
        heading = len(label) <= 80 and (label in headings or bool(HEADING.match(label)) or label.endswith(('：', ':')))
        if cursor > start and (heading or cursor + len(line) - start > BLOCK_CHARS):
            ranges.append((start, cursor))
            start = cursor
        while cursor + len(line) - start > BLOCK_CHARS:
            ranges.append((start, start + BLOCK_CHARS))
            start += BLOCK_CHARS
        cursor += len(line)
    if start < len(text):
        ranges.append((start, len(text)))
    blocks, directory = [], []
    for index, (start, end) in enumerate(ranges):
        value = text[start:end]
        first_line = next((line.strip() for line in value.splitlines() if line.strip()), '')
        block_id = f'b-{version}-{index + 1}'
        title = first_line[:60] or f'正文第 {index + 1} 段'
        block = {'block_id': block_id, 'title': title, 'start': start, 'end': end, 'text': value}
        blocks.append(block)
        directory.append({key: block[key] for key in ('block_id', 'title', 'start', 'end')})
    return {**page, 'content_version': version, 'content_blocks': blocks, 'content_directory': directory,
        'content_index_complete': not page.get('text_truncated', False)}


def indexed_source(value):
    """Legacy page logs get an in-memory index; audit records are never rewritten."""
    if isinstance(value, dict) and isinstance(value.get('text'), str):
        return index_content(value)
    return value
