"""Read-only UI regression against a running local service, with fixture API data."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright


async def main():
    output = Path('data/ui-checks')
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            task = {'id': 'audit-task', 'status': 'budget_exceeded',
                'error': 'Remaining task token budget cannot fit next model request',
                'answer': '未完成，已保存证据。', 'usage': {'remaining_tokens': 7556,
                    'last_request': {'estimated_input_tokens': 25495, 'reserved_output_tokens': 2048}}}
            events = [{'id': str(index), 'kind': 'budget_check', 'created_at': '2026-10-10T01:12:31Z',
                'payload': {'status': 'stopped', 'required_tokens': 27543}} for index in range(25)]
            fixtures = {'/sessions': {'sessions': [{'id': 'audit-session', 'title': '预算与恢复测试'}]},
                '/sessions/audit-session': {'id': 'audit-session', 'title': '预算与恢复测试', 'active_task_id': None},
                '/sessions/audit-session/messages': {'messages': [{'task_id': task['id'],
                    'payload': {'role': 'user', 'content': '下载三个项目的招标文件'}}]},
                '/tasks/audit-task': task, '/batches': {'batches': []}, '/tool-calls': {'tool_calls': []},
                '/model-calls': {'model_calls': [{'id': 'model', 'purpose': 'agent', 'model': 'fixture',
                    'status': 'completed', 'usage': {'prompt_tokens': 128917, 'completion_tokens': 13527}}]},
                '/context-events': {'context_events': events}}
            fixtures['/model-calls']['model_calls'][0]['started_at'] = '2026-10-10T01:12:20Z'
            fixtures['/sessions/audit-session/messages']['messages'].append({
                'id': 'reply', 'task_id': task['id'], 'created_at': '2026-10-10T01:12:21Z',
                'payload': {'role': 'assistant', 'content': '现在读取两个项目。', 'tool_calls': []}})
            fixtures['/tool-calls']['tool_calls'] = [{
                'id': ident, 'tool': tool, 'source': 'agent', 'status': 'completed',
                'started_at': '2026-10-10T01:12:22Z', 'duration_ms': 10,
                'arguments': {}, 'result': {}} for ident, tool in [
                    ('nav', 'navigate_tender_entry'), ('read', 'read_tool_result')]]
            events.extend([
                {'id': 'actor', 'kind': 'agent_step', 'created_at': '2026-10-10T01:12:23Z',
                 'payload': {'agent': 'page_files', 'agent_run_id': 'run12345', 'log_id': 'nav'}},
                {'id': 'wave', 'kind': 'execution_wave', 'created_at': '2026-10-10T01:12:24Z',
                 'payload': {'status': 'completed', 'wave_id': 'wave12345', 'step_ids': ['s1', 's2'],
                    'members': [{'log_id': item['id'], 'tool': item['tool']} for item in fixtures['/tool-calls']['tool_calls']]}}])
            async def route(request):
                value = fixtures.get(urlparse(request.request.url).path)
                if value is None:
                    await request.continue_()
                else:
                    await request.fulfill(content_type='application/json', body=json.dumps(value))
            await page.route('**/*', route)
            await page.goto('http://127.0.0.1:8010/')
            await page.get_by_role('button', name='预算与恢复测试', exact=True).click()
            await page.locator('#toggle-logs').click()
            await page.locator('#usage-summary').filter(has_text='剩余 7,556').wait_for()
            assert '25,495' in await page.locator('#usage-summary').inner_text()
            assert '2,048' in await page.locator('#usage-summary').inner_text()
            assert await page.locator('#audit-request-id').inner_text() == 'audit-task'
            await page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
            await page.get_by_role('button', name='复制请求 ID', exact=True).click()
            assert await page.evaluate('navigator.clipboard.readText()') == 'audit-task'
            await page.locator('#audit-task').select_option('')
            await page.locator('#audit-request').wait_for(state='hidden')
            await page.locator('#audit-task').select_option('audit-task')
            await page.locator('#audit-request').wait_for(state='visible')
            await page.locator('#log-list details').first.wait_for()
            ids = await page.locator('#log-list details').evaluate_all('(nodes) => nodes.map(node => node.dataset.id)')
            assert ids[:4] == ['modelmodel', 'transcriptreply', 'nav', 'read'], ids
            assert '页面/文件 Agent [run12345]' in await page.locator('[data-id="nav"] summary').inner_text()
            assert '导航组内串行' in await page.locator('[data-id="nav"] summary').inner_text()
            assert '并行调度' in await page.locator('[data-id="read"] summary').inner_text()
            assert await page.get_by_text('模型执行消息', exact=True).count() == 0
            await page.locator('#log-list').evaluate('(node) => { node.scrollTop = 350; }')
            position = await page.locator('#log-list').evaluate('(node) => node.scrollTop')
            await page.evaluate('refreshLogs(generation)')
            assert abs(await page.locator('#log-list').evaluate('(node) => node.scrollTop') - position) < 2
            await page.screenshot(path=str(output / 'audit-budget-desktop.png'))
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.screenshot(path=str(output / 'audit-budget-mobile.png'))
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            task.update(status='awaiting_clarification', answer='正式标书还是公告附件？请在3分钟内回复。', error=None,
                confirmation={'status': 'pending', 'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()})
            await page.evaluate('loadMessages(sessionId, generation)')
            assert await page.evaluate('confirmationTaskId') == 'audit-task'
            task.update(status='timed_out', error_code='CONFIRMATION_TIMEOUT',
                answer='确认超时：默认不授权，没有任何文件成功下载。')
            await page.locator('.message.error').filter(has_text='确认超时：默认不授权').wait_for()
            assert await page.evaluate('confirmationTaskId') is None
            task.update(status='failed', error_code='NO_FILES_DOWNLOADED', answer='下载失败：没有任何文件成功下载。原因：未找到符合要求的附件。')
            await page.evaluate('loadMessages(sessionId, generation)')
            await page.locator('.message.error').filter(has_text='下载失败：没有任何文件成功下载').wait_for()
            fixtures['/batches'] = {'batches': [{'id': 'historic-batch', 'chat_task_id': 'previous-request-id',
                'status': 'completed', 'contract': {'scope': 'count', 'count': 1},
                'counts': {'total': 1, 'downloaded': 1}, 'processed': 1, 'bytes_downloaded': 100,
                'reason': '已下载'}]}
            await page.evaluate('refreshBatches(generation)')
            assert await page.locator('.batch-origin code').inner_text() == 'previous-request-id'
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            print('Audit UI passed: confirmation binding/expiry, zero-file error, budget details, scroll stability, desktop/mobile, no JS errors.')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
