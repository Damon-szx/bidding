const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  fs.mkdirSync('data/ui-checks', { recursive: true });
  const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let turns = 0;
  const history = [];
  let session = null;
  const taskReads = new Map();
  const logs = [{ id: 'log1', tool: 'fetch_tender_detail', source: 'agent', status: 'completed', duration_ms: 125,
    arguments: { url: 'https://example.org/bid' }, result: { title: '软件采购项目', attachments: [] } },
    { id: 'failed1', tool: 'navigate_tender_entry', source: 'agent', status: 'failed', duration_ms: 30,
      error: 'CancelledError', arguments: { announcement_id: 'page1', entry_id: 'entry1' }, result: { error: 'CancelledError' } }];
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let response;
    if (path === '/sessions') response = { sessions: session ? [session] : [] };
    else if (path === '/batches') response = { batches: [] };
    else if (path === '/sessions/session1') response = session;
    else if (path === '/sessions/session1/messages') response = { messages: history };
    else if (path === '/tool-calls') {
      if (turns) assert.match(url.searchParams.get('task_id'), /^task[12]$/);
      response = { tool_calls: turns ? logs : [] };
    }
    else if (path === '/model-calls') response = { model_calls: turns ? [{ id: 'model1', model: 'fixture', purpose: 'agent', status: 'completed', usage: { prompt_tokens: 120, completion_tokens: 40 }, duration_ms: 30 }] : [] };
    else if (path === '/context-events') response = { context_events: turns ? [
      { id: 'context1', kind: 'tool_result', payload: { mode: 'structured_projection', before_estimated_tokens: 5000, after_estimated_tokens: 800 } },
      { id: 'compress1', kind: 'compression_attempt', payload: { status: 'failed', stage: 'history_summary', exception: { type: 'ValidationError', message: 'pending field missing', traceback: 'fixture traceback\n' + 'diagnostic line\n'.repeat(120) } } },
      { id: 'recover1', kind: 'compression_recovery', payload: { status: 'completed', action: 'rebuild_from_durable_checkpoints', before_estimated_tokens: 35000, after_estimated_tokens: 9000, next_action: 'continue_with_evidence_reread' } },
    ] : [] };
    else if (path === '/chat') {
      const request = route.request().postDataJSON();
      if (turns) assert.equal(request.session_id, 'session1');
      turns++;
      const id = `task${turns}`;
      session = { id: 'session1', title: '查找软件采购项目', active_task_id: null };
      history.push({ task_id: id, payload: { role: 'user', content: request.message } },
        { task_id: id, payload: { role: 'assistant', content: 'Let me use fetch_tender_detail', tool_calls: [{ id: 'call-1', function: { name: 'fetch_tender_detail', arguments: '{}' } }] } },
        { task_id: id, payload: { role: 'assistant', content: turns === 1 ? 'I have enough information. Let me compile results.\n\n找到软件采购项目，公告来源已记录。\n\n| 项目 | 来源 |\n| --- | --- |\n| **软件项目** | [原始公告](https://example.org/bid) |\n\n<script>window.injected=true</script><img src=x onerror="window.injected=true">[危险链接](javascript:alert(1))' : '招标文件已下载。' } });
      response = { session_id: 'session1', task_id: id };
    } else if (path.startsWith('/tasks/task')) {
      const id = path.split('/').pop();
      const count = taskReads.get(id) || 0; taskReads.set(id, count + 1);
      response = { id, status: count ? 'completed' : 'running', progress: '核对公告原文' };
    }
    else return route.continue();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) });
  });
  await page.goto('http://127.0.0.1:8010/');
  await page.waitForFunction(() => document.querySelector('#task-status').textContent === '就绪');
  await page.screenshot({ path: 'data/ui-checks/desktop-empty.png' });
  assert.ok(await page.locator('svg').count() > 5, 'Local icons must render');
  await page.locator('#input').fill('查找软件采购项目');
  await page.locator('#send').click();
  await page.locator('.pending').getByText('核对公告原文', { exact: true }).waitFor();
  await page.getByText('找到软件采购项目，公告来源已记录。', { exact: true }).waitFor();
  assert.equal(await page.locator('#messages').getByText('Let me use fetch_tender_detail').count(), 0);
  assert.equal(await page.locator('#messages').getByText('I have enough information.', { exact: false }).count(), 0);
  assert.equal(await page.locator('#messages table').count(), 1);
  assert.equal(await page.locator('#messages strong').count(), 1);
  assert.equal(await page.locator('#messages script, #messages img, #messages a[href^="javascript:"]').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  await page.locator('#input').fill('下载刚才的招标文件');
  await page.locator('#send').click();
  await page.getByText('招标文件已下载。', { exact: true }).waitFor();
  await page.locator('#toggle-logs').click();
  await page.getByText('fetch_tender_detail', { exact: true }).click();
  await page.getByText('125 ms', { exact: false }).waitFor();
  await page.getByText('本轮 · 输入 120 · 输出 40 Token', { exact: true }).waitFor();
  await page.getByText('Tool · 读取公告或列表原文，提取项目、日期及附件信息', { exact: true }).waitFor();
  await page.getByText('执行时间线（含压缩与恢复）', { exact: true }).waitFor();
  await page.locator('[data-id="contextcompress1"] > summary').click();
  await page.getByText('fixture traceback', { exact: false }).waitFor();
  await page.locator('[data-id="contextrecover1"] > summary').click();
  await page.getByText('continue_with_evidence_reread', { exact: false }).waitFor();
  assert.equal(await page.locator('[data-id="failed1"]').evaluate(node => node.classList.contains('audit-failed')), true);
  assert.equal(await page.locator('[data-id="contextcompress1"] > summary').evaluate(node => getComputedStyle(node).color), 'rgb(180, 35, 24)');
  assert.equal(await page.locator('[data-id="contextrecover1"]').evaluate(node => node.classList.contains('audit-failed')), false);
  const reading = await page.evaluate(() => {
    const node = document.querySelector('[data-id="contextcompress1"]');
    node.scrollIntoView({ block: 'start' });
    const list = document.querySelector('#log-list'); list.scrollTop += 140;
    window.auditReadingNode = node;
    return { top: list.scrollTop, offset: node.getBoundingClientRect().top };
  });
  await page.evaluate(() => refreshLogs(generation));
  assert.equal(await page.evaluate(() => document.querySelector('[data-id="contextcompress1"]') === window.auditReadingNode), true);
  assert.ok(Math.abs(await page.locator('#log-list').evaluate(node => node.scrollTop) - reading.top) < 2);
  logs.unshift({ id: 'newlog', tool: 'read_tool_result', source: 'agent', status: 'completed', duration_ms: 1, arguments: {}, result: {} });
  await page.evaluate(() => refreshLogs(generation));
  assert.equal(await page.evaluate(() => document.querySelector('[data-id="contextcompress1"]') === window.auditReadingNode), true);
  assert.ok(Math.abs(await page.locator('[data-id="contextcompress1"]').evaluate(node => node.getBoundingClientRect().top) - reading.offset) < 2, 'New records preserve the visible reading anchor');
  const beforeWheel = await page.locator('#log-list').evaluate(node => node.scrollTop);
  await page.locator('#log-list').hover(); await page.mouse.wheel(0, 160);
  await page.waitForFunction(top => document.querySelector('#log-list').scrollTop > top + 30, beforeWheel);
  assert.equal(await page.locator('#audit-task').inputValue(), 'task2');
  await page.locator('#audit-task').selectOption('task1');
  await page.waitForFunction(() => document.querySelector('#log-list').textContent.includes('Let me use fetch_tender_detail') && !document.querySelector('#log-list').textContent.includes('招标文件已下载。'));
  await page.locator('#audit-task').selectOption('task2');
  assert.equal(await page.getByText('模型执行消息', { exact: true }).count(), 0);
  await page.locator('#log-list [data-id^="transcript"]').first().waitFor();
  await page.screenshot({ path: 'data/ui-checks/desktop-chat.png' });
  await page.reload();
  await page.getByText('招标文件已下载。', { exact: true }).waitFor();
  assert.equal(await page.locator('.message.user').count(), 2, 'Refresh must restore both turns');
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.locator('#close-logs').isVisible()) await page.locator('#close-logs').click();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.screenshot({ path: 'data/ui-checks/mobile-chat.png' });
  assert.equal(await page.locator('#composer').evaluate(node => node.getBoundingClientRect().bottom <= innerHeight), true, 'Composer remains inside the viewport');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'No horizontal overflow');
  await page.locator('#open-sidebar').click();
  assert.equal(await page.locator('#sidebar').isVisible(), true);
  await page.locator('#close-sidebar').click();
  await page.locator('#new-chat').evaluate(node => node.click());
  await page.locator('.empty').waitFor();
  assert.equal(await page.locator('.message').count(), 0);
  await page.route('**/chat', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '测试：服务暂不可用' }) }));
  await page.locator('#input').fill('测试失败');
  await page.locator('#send').click();
  await page.getByText('测试：服务暂不可用', { exact: true }).waitFor();
  assert.equal(await page.locator('#input').inputValue(), '测试失败');
  assert.deepEqual(errors, []);
  console.log('UI checks passed: multi-turn, logs, refresh, mobile, new conversation, request failure.');
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
