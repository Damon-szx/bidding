const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  fs.mkdirSync('data/ui-checks', { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const batch = { id: 'batch1', status: 'awaiting_confirmation', contract: { scope: 'count', count: 3 },
      counts: { total: 0 }, processed: 0, bytes_downloaded: 0 };
    const actions = [];
    await page.route('**/*', async route => {
      const path = new URL(route.request().url()).pathname;
      let body;
      if (path === '/sessions') body = { sessions: [{ id: 'session1', title: '下载国家电网项目附件' }] };
      else if (path === '/sessions/session1') body = { id: 'session1', title: '下载国家电网项目附件', active_task_id: null };
      else if (path === '/sessions/session1/messages') body = { messages: [{ task_id: 'task1', payload: { role: 'user', content: '下载这个列表的标书' } }] };
      else if (path === '/tasks/task1') body = { id: 'task1', status: 'awaiting_confirmation', answer: '页面显示约 1574 个项目，请确认下载范围，尚未开始下载。' };
      else if (path === '/batches') body = { batches: [batch] };
      else if (path === '/batches/batch1/items') body = { items: [{ id: 1, status: 'downloaded', payload: { entry: { title: '电力监理项目资格预审公告' }, files: [{ filename: '资格预审公告.pdf', local_download_url: '/files/' + 'a'.repeat(32) }] } }] };
      else if (path === '/batches/batch1/control') {
        const request = route.request().postDataJSON(); actions.push(request);
        assert.equal(request.session_id, 'session1');
        if (request.action === 'confirm') { assert.equal(request.count, 12); batch.contract.count = request.count; batch.status = 'running'; batch.counts.total = 12; }
        else if (request.action === 'pause') batch.status = 'paused';
        else if (request.action === 'resume') { batch.status = 'partial_completed'; batch.counts.failed = 1; batch.reason = '部分项目失败'; }
        else if (request.action === 'retry_failed') { batch.status = 'running'; batch.counts.failed = 0; }
        else if (request.action === 'cancel') batch.status = 'cancelled';
        body = batch;
      }
      else if (path === '/tool-calls') body = { tool_calls: [] };
      else if (path === '/model-calls') body = { model_calls: [] };
      else if (path === '/context-events') body = { context_events: [] };
      else return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto('http://127.0.0.1:8010/');
    await page.getByRole('button', { name: '下载国家电网项目附件', exact: true }).click();
    await page.getByRole('button', { name: '确认下载', exact: true }).waitFor();
    await page.getByRole('spinbutton', { name: '项目数量' }).fill('12');
    await page.screenshot({ path: 'data/ui-checks/batch-confirm-desktop.png' });
    await page.getByRole('button', { name: '确认下载', exact: true }).click();
    await page.getByRole('button', { name: '暂停任务' }).click();
    await page.getByRole('button', { name: '继续未完成任务' }).click();
    await page.getByRole('button', { name: '重试失败项目' }).click();
    await page.getByText('项目及文件', { exact: true }).click();
    await page.getByRole('link', { name: '资格预审公告.pdf' }).waitFor();
    assert.equal(await page.getByRole('link', { name: '资格预审公告.pdf' }).getAttribute('href'), '/files/' + 'a'.repeat(32));
    await page.reload();
    await page.getByRole('button', { name: '暂停任务' }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: 'data/ui-checks/batch-progress-mobile.png' });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('#composer').evaluate(node => node.getBoundingClientRect().bottom <= innerHeight), true);
    await page.getByRole('button', { name: '取消任务' }).click();
    await page.locator('#batch-area').getByText('已取消', { exact: true }).waitFor();
    assert.deepEqual(actions.map(item => item.action), ['confirm', 'pause', 'resume', 'retry_failed', 'cancel']);
    assert.deepEqual(errors, []);
    console.log('Batch UI checks passed: confirmation, controls, files, reload and mobile.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
