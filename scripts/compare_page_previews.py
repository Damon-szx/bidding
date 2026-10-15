"""Compare first-page model inputs against the preserved Git baseline, offline."""
import argparse
import json
import subprocess
from pathlib import Path

from bidding.content import index_content
from bidding.context import page_preview
from bidding.model import estimate_tokens

BASELINE = 'baseline-before-page-blocks-20261015'


def compare(pages):
    source = subprocess.check_output(['git', 'show', BASELINE + ':src/bidding/context.py'], text=True)
    namespace = {'__name__': 'baseline_context'}
    exec(compile(source, 'baseline_context.py', 'exec'), namespace)
    old_preview = namespace['page_preview']
    results = []
    for page in pages:
        old = estimate_tokens(old_preview(page, 'comparison-source', 2000))
        new = estimate_tokens(page_preview(index_content(page), 'comparison-source', 2000))
        results.append({'page_type': page.get('page_type'), 'text_characters': len(page.get('text', '')),
            'baseline_preview_estimated_tokens': old, 'blocks_preview_estimated_tokens': new,
            'reduction_percent': round((old - new) / old * 100, 1)})
    return results


def main():
    parser = argparse.ArgumentParser(description='离线比较页面首次预览；不调用模型或外部网站。')
    parser.add_argument('--tools-json', type=Path, help='本机 /tool-calls 响应 JSON，不会复制到仓库')
    args = parser.parse_args()
    pages = []
    if args.tools_json:
        rows = json.loads(args.tools_json.read_text())['tool_calls']
        pages = [row['result'] for row in rows if isinstance(row.get('result'), dict)
            and row['result'].get('page_type') and 'text' in row['result']]
    synthetic = {'source_url': 'https://example.org/tender', 'page_type': 'detail', 'title': '测试招标公告',
        'text': '一、项目概况\n' + '服务内容及实施要求。' * 160 + '\n二、投标人资格要求\n' + '资格条件由原文核验。' * 80
            + '\n三、招标文件获取方式\n文件获取截止时间：2026-10-20 10:00。\n四、联系方式\n联系人及地址。',
        'attachments': [{'name': '招标文件.pdf', 'url': 'https://example.org/tender.pdf'}]}
    report = {'measurement': '首次页面预览的本地保守估算，不是供应商实际 Token，也不代表整轮耗时或调用次数',
        'baseline_ref': BASELINE, 'recorded_pages': compare(pages), 'synthetic_long_detail': compare([synthetic])[0]}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
