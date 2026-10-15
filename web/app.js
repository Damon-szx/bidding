const $ = (id) => document.getElementById(id);
let sessionId = null;
let sessions = [];
let generation = 0;
let busy = false;
let ready = false;
let taskId = null;
let confirmationTaskId = null;
let confirmationTimer = null;
const terminal = new Set(['completed', 'failed', 'interrupted', 'cancelled', 'timed_out', 'budget_exceeded', 'scheduled', 'awaiting_confirmation', 'awaiting_clarification', 'partial_completed']);
const stateNames = { queued: '等待中', running: '处理中', completed: '已完成', failed: '失败', interrupted: '已中断', timed_out: '已超时', budget_exceeded: '预算已达上限', scheduled: '下载任务已创建', awaiting_confirmation: '待确认范围', partial_completed: '部分完成', paused: '已暂停', cancelled: '已取消', needs_input: '需人工处理', downloaded: '附件已下载', no_attachment: '未发现匹配附件', pending: '待处理' };
const toolLabels = { list_acquisition_batches: '查看下载任务', control_acquisition_batch: '调整下载任务', plan_acquisition: '确定下载范围', search_tenders: '检索招标信息', fetch_tender_detail: '核对公告原文', render_tender_page: '加载公告页面', navigate_tender_entry: '查看项目详情', discover_tender_files: '查找招标文件', download_tender_file: '下载招标文件', read_tool_result: '查阅完整资料' };
toolLabels.inspect_document_access = '检查文件获取入口';
Object.assign(stateNames, { awaiting_clarification: '待补充需求', blocked: '前置步骤未完成' });
Object.assign(toolLabels, { delegate_agent: '分配专业处理任务' });
Object.assign(toolLabels, { record_search_progress: '整理搜索线索', request_search_extension: '评估补充检索', review_search_strategy: '检查检索策略' });
Object.assign(toolLabels, { get_task_plan: '查看任务计划', update_task_plan: '调整任务计划', execute_plan_steps: '执行任务步骤', request_clarification: '确认需求', resume_task: '继续未完成任务', record_project_assessment: '核验项目条件' });
let batchSignature = '';
let refreshingBatches = false;
const expandedBatches = new Set();
const itemOffsets = new Map();
let logOffset = 0;
let refreshingLogs = false;
let auditTaskId = null;
let auditSnapshot = '';
let auditScope = '';
const toolDescriptions = {
  record_search_progress: '整理候选初筛、待核验线索和目标缺口（不等于原文核验通过）',
  request_search_extension: '主 Agent 有限追加搜索额度，最多三批，不突破总预算',
  review_search_strategy: '独立反思连续无进展时的新检索策略',
  delegate_agent: '分配有界子任务给搜索或页面与文件 Agent，使用独立上下文与共享预算',
  get_task_plan: '查看目标、完成条件、依赖关系与步骤状态',
  update_task_plan: '根据结果调整步骤，保留已完成检查点',
  execute_plan_steps: '调度就绪步骤并行执行，等待或阻塞依赖项',
  request_clarification: '向用户确认重要歧义，在回复前不继续执行',
  resume_task: '继续同一对话的未完成任务，复用已完成步骤',
  record_project_assessment: '记录模型对条件的判断并校验原文证据引用',
  fetch_tender_detail: '读取公告或列表原文，提取项目、日期及附件信息',
  search_tenders: '通过搜索服务发现候选项目和来源链接（尚未核实）',
  render_tender_page: '使用浏览器加载动态公告页面，识别项目入口及附件',
  navigate_tender_entry: '访问已发现的项目入口或下一页，保留来源关系',
  discover_tender_files: '从已读取公告中识别公开文件附件',
  inspect_document_access: '检查已观察到的文件获取按钮，报告登录、验证码或付费限制',
  download_tender_file: '下载所选附件并校验实际格式，保存本地文件',
  plan_acquisition: '确认项目范围并创建持久化下载任务',
  read_tool_result: '按字段或关键字查阅完整工具结果',
  list_acquisition_batches: '读取本对话下载任务及进度',
  control_acquisition_batch: '确认、暂停、继续、取消或重试下载任务',
};
function taskStatus(task) {
  if (task.status === 'budget_exceeded') {
    if (/cannot fit next/i.test(task.error || '')) return '剩余预算不足以执行下一次请求';
    if (/compression/i.test(task.error || '')) return '上下文压缩失败，已停止';
    if (/context/i.test(task.error || '')) return '上下文达到上限';
    if (/round|tool-call/i.test(task.error || '')) return '执行次数达到上限';
  }
  return stateNames[task.status];
}
function focusAudit(id) {
  auditTaskId = id; logOffset = 0;
  $('audit-task').value = id || '';
  renderRequestIdentity();
  toggleLogs(true);
}
function renderRequestIdentity() {
  $('audit-request').hidden = !auditTaskId;
  $('audit-request-id').textContent = auditTaskId || '';
}
$('copy-request-id').onclick = async () => {
  if (!auditTaskId) return;
  try { await navigator.clipboard.writeText(auditTaskId); }
  catch { notice('复制失败，请选择请求 ID 后手动复制。'); }
};
$('audit-task').onchange = () => focusAudit($('audit-task').value || null);

async function refreshBatches(token = generation) {
  if (!sessionId || refreshingBatches) return;
  refreshingBatches = true;
  const owner = sessionId;
  try {
    const { batches } = await api(`/batches?session_id=${encodeURIComponent(owner)}`);
    if (token !== generation || owner !== sessionId) return;
    const signature = JSON.stringify(batches.map(({ elapsed_seconds, ...batch }) => batch));
    if (signature === batchSignature || $('batch-area').contains(document.activeElement)) return;
    batchSignature = signature;
    $('batch-area').hidden = !batches.length;
    $('batch-list').replaceChildren();
    for (const batch of batches) {
      const node = document.createElement('article'); node.className = 'batch-task'; node.dataset.batchId = batch.id;
      const heading = document.createElement('div'); heading.className = 'batch-heading';
      const title = document.createElement('strong'); title.textContent = batch.status === 'awaiting_confirmation' ? '下载范围待确认' : batch.contract.scope === 'all' ? '全量附件下载' : `${batch.contract.count} 个项目附件下载`;
      const status = document.createElement('span'); status.textContent = stateNames[batch.status] || '待处理';
      heading.append(title, status); node.append(heading);
      if (batch.chat_task_id) {
        const origin = document.createElement('div'); origin.className = 'batch-meta batch-origin';
        origin.append(document.createTextNode('所属请求 ID：'));
        const code = document.createElement('code'); code.textContent = batch.chat_task_id; origin.append(code);
        const audit = document.createElement('button'); audit.type = 'button'; audit.className = 'icon-button';
        audit.title = '查看所属请求的执行记录'; audit.setAttribute('aria-label', audit.title);
        audit.innerHTML = '<i data-lucide="list-tree"></i>'; audit.onclick = () => focusAudit(batch.chat_task_id);
        origin.append(audit); node.append(origin);
      }
      const meta = document.createElement('div'); meta.className = 'batch-meta';
      const counts = batch.counts;
      meta.textContent = `已发现 ${counts.total} 个 · 已处理 ${batch.processed} 个 · 已下载 ${counts.downloaded || 0} 个项目 · 无匹配附件 ${counts.no_attachment || 0} 个 · 需人工 ${counts.needs_input || 0} 个 · 失败 ${counts.failed || 0} 个 · ${(batch.bytes_downloaded / 1024 / 1024).toFixed(1)} MB`;
      node.append(meta);
      if (batch.reason) { const reason = document.createElement('div'); reason.className = 'batch-reason' + (batch.status === 'completed' ? ' success' : ''); reason.textContent = batch.reason; node.append(reason); }
      const actions = document.createElement('div'); actions.className = 'batch-actions'; node.append(actions);
      async function control(action, extra = {}) {
        try {
          await api(`/batches/${batch.id}/control`, { method: 'POST', body: JSON.stringify({ session_id: owner, action, ...extra }) });
          if (owner === sessionId) { document.activeElement?.blur(); batchSignature = ''; await refreshBatches(token); }
        } catch (error) { notice(error.message); }
      }
      function actionButton(action, label, icon) {
        const button = document.createElement('button'); button.className = 'icon-button'; button.title = label; button.setAttribute('aria-label', label);
        button.innerHTML = `<i data-lucide="${icon}"></i>`;
        button.onclick = async () => { button.disabled = true; await control(action); button.disabled = false; };
        actions.append(button);
      }
      if (batch.status === 'awaiting_confirmation') {
        const form = document.createElement('form'); form.className = 'batch-confirm';
        const select = document.createElement('select'); select.setAttribute('aria-label', '下载范围');
        for (const [value, text] of [['count', '指定项目数量'], ['all', '全部项目']]) { const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option); }
        const input = document.createElement('input'); input.type = 'number'; input.min = '1'; input.max = '5000'; input.value = '3'; input.required = true; input.setAttribute('aria-label', '项目数量');
        select.onchange = () => { input.hidden = select.value === 'all'; input.required = select.value === 'count'; };
        const button = document.createElement('button'); button.type = 'submit'; button.textContent = '确认下载';
        form.onsubmit = async event => { event.preventDefault(); button.disabled = true; await control('confirm', { scope: select.value, count: select.value === 'count' ? Number(input.value) : null }); button.disabled = false; };
        form.append(select, input, button); actions.append(form);
      }
      if (['running', 'queued'].includes(batch.status)) actionButton('pause', '暂停任务', 'pause');
      if (['paused', 'partial_completed', 'failed'].includes(batch.status)) actionButton('resume', '继续未完成任务', 'play');
      if (counts.failed && ['paused', 'partial_completed', 'needs_input', 'failed', 'completed'].includes(batch.status)) actionButton('retry_failed', '重试失败项目', 'rotate-cw');
      if (!['completed', 'cancelled'].includes(batch.status)) actionButton('cancel', '取消任务', 'square');
      const manifest = document.createElement('a'); manifest.className = 'icon-button'; manifest.href = `/batches/${batch.id}/manifest`; manifest.title = '下载完整项目清单'; manifest.setAttribute('aria-label', manifest.title); manifest.innerHTML = '<i data-lucide="download"></i>'; actions.append(manifest);
      const details = document.createElement('details'); details.className = 'batch-files'; details.open = expandedBatches.has(batch.id);
      const summary = document.createElement('summary'); summary.textContent = '项目及文件'; details.append(summary);
      const results = document.createElement('div'); details.append(results); node.append(details);
      async function loadItems() {
        const limit = 50, offset = itemOffsets.get(batch.id) || 0;
        try {
          const { items } = await api(`/batches/${batch.id}/items?limit=${limit}&offset=${offset}`);
          if (token !== generation) return;
          results.replaceChildren();
          for (const item of items) {
            const row = document.createElement('div'); row.className = 'batch-project';
            const name = document.createElement('div'); name.textContent = `${item.payload.entry.title} · ${stateNames[item.status] || '待处理'}`; row.append(name);
            for (const file of item.payload.files || []) {
              if (!/^\/files\/[a-f0-9]{32}$/.test(file.local_download_url)) continue;
              const link = document.createElement('a'); link.href = file.local_download_url; link.textContent = file.filename; link.target = '_blank'; link.rel = 'noopener'; row.append(link);
            }
            if (item.status === 'failed') { const error = document.createElement('div'); error.textContent = '获取失败，详情见开发者记录。'; row.append(error); }
            results.append(row);
          }
          if (counts.total > limit || offset) {
            const controls = document.createElement('div'); controls.className = 'batch-actions';
            const caption = document.createElement('span'); caption.textContent = `项目 ${offset + 1}–${offset + items.length} / ${counts.total}`; controls.append(caption);
            for (const [delta, icon, title] of [[-limit, 'chevron-left', '上一批项目'], [limit, 'chevron-right', '下一批项目']]) {
              const button = document.createElement('button'); button.className = 'icon-button'; button.title = title; button.setAttribute('aria-label', title); button.innerHTML = `<i data-lucide="${icon}"></i>`;
              button.disabled = delta < 0 ? !offset : offset + limit >= counts.total;
              button.onclick = () => { itemOffsets.set(batch.id, Math.max(0, offset + delta)); loadItems(); };
              controls.append(button);
            }
            results.append(controls); icons();
          }
        } catch (error) { notice(error.message); }
      }
      details.ontoggle = () => { if (details.open) { expandedBatches.add(batch.id); loadItems(); } else expandedBatches.delete(batch.id); };
      $('batch-list').append(node);
      if (details.open) await loadItems();
    }
    icons();
  } catch (error) { if (token === generation) notice('下载任务加载失败：' + error.message); }
  finally { refreshingBatches = false; }
}

function businessText(text) {
  const lines = text.split('\n');
  const first = lines.find(line => line.trim())?.trim() || '';
  if (/^(?:I (?:have|will|need|found)|I'll|Let me|Based on|Now I|The (?:search|direct))\b/.test(first)) {
    const start = lines.findIndex(line => /[\u4e00-\u9fff]/.test(line));
    if (start >= 0) text = lines.slice(start).join('\n');
  }
  for (const [name, label] of Object.entries(toolLabels)) text = text.replaceAll(name, label);
  return text.replace(/`?verified`?\s*[:：=为]+\s*`?false`?/g, '尚未核对公告原文');
}
function renderAnswer(node, text) {
  text = businessText(text);
  if (!window.marked || !window.DOMPurify) { node.textContent = text; return; }
  node.classList.add('markdown');
  node.innerHTML = DOMPurify.sanitize(marked.parse(text, { breaks: true }), {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'hr'],
    ALLOWED_ATTR: ['href', 'title'],
  });
  for (const link of node.querySelectorAll('a')) {
    try {
      const href = link.getAttribute('href');
      const url = new URL(href, /^\/files\/[a-f0-9]{32}$/.test(href) ? location.origin : undefined);
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Unsupported link');
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    } catch { link.removeAttribute('href'); }
  }
  for (const table of node.querySelectorAll('table')) {
    const wrap = document.createElement('div'); wrap.className = 'table-scroll';
    table.replaceWith(wrap); wrap.append(table);
  }
}

function icons() { window.lucide?.createIcons(); }
function remember(id) { try { if (id) localStorage.setItem('bidding.session', id); else localStorage.removeItem('bidding.session'); } catch {} }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers }, signal: AbortSignal.timeout(20000) });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败 (${response.status})`);
  return body;
}
function notice(text = '') { $('notice-text').textContent = text; $('notice').hidden = !text; }
function setBusy(value, status = '') {
  busy = value;
  $('cancel-task').hidden = !value || !taskId;
  $('task-status').textContent = status || (value ? '处理中' : '就绪');
  $('composer-status').textContent = value ? '任务执行中' : '招标采集';
  $('send').disabled = !ready || busy || !$('input').value.trim();
}
function empty() {
  $('messages').replaceChildren();
  const box = document.createElement('div'); box.className = 'empty';
  box.innerHTML = '<span class="empty-icon"><i data-lucide="messages-square"></i></span><h1>新对话</h1>';
  $('messages').append(box); icons();
}
function message(role, text, error = false, executionId = null) {
  $('messages').querySelector('.empty')?.remove();
  const row = document.createElement('article'); row.className = `message ${role}${error ? ' error' : ''}`;
  row.innerHTML = `<span class="avatar"><i data-lucide="${role === 'user' ? 'user-round' : 'files'}"></i></span><div class="message-body"><div class="message-label"></div><div class="message-text"></div></div>`;
  row.querySelector('.message-label').textContent = role === 'user' ? '你' : '招标助手';
  if (executionId) {
    const button = document.createElement('button'); button.className = 'icon-button audit-link';
    button.title = '查看本轮执行记录'; button.setAttribute('aria-label', '查看本轮执行记录');
    button.innerHTML = '<i data-lucide="list-tree"></i>'; button.onclick = () => focusAudit(executionId);
    row.querySelector('.message-label').append(button);
  }
  if (role === 'assistant' && !error) renderAnswer(row.querySelector('.message-text'), text);
  else row.querySelector('.message-text').textContent = text;
  $('messages').append(row); icons();
}
function scrollEnd() { $('messages').scrollTop = $('messages').scrollHeight; }
function pending(show, text = '正在处理…') {
  $('messages').querySelector('.pending')?.remove();
  if (show) { const node = document.createElement('div'); node.className = 'pending'; node.innerHTML = '<span class="spinner"></span><span></span>'; node.lastChild.textContent = text; $('messages').append(node); }
}
function sidebar(open) { $('sidebar').classList.toggle('open', open); $('scrim').hidden = !open; }
function renderSessions() {
  $('sessions').replaceChildren();
  for (const item of sessions) {
    const node = document.createElement('button'); node.className = 'session' + (item.id === sessionId ? ' active' : '');
    node.innerHTML = '<i data-lucide="message-square"></i><span></span>';
    node.querySelector('span').textContent = item.title;
    node.title = item.title;
    node.onclick = () => { sidebar(false); selectSession(item.id); };
    $('sessions').append(node);
  }
  icons();
}
async function loadSessions(more = false) {
  const offset = more ? sessions.length : 0;
  const response = await api(`/sessions?offset=${offset}&limit=100`);
  sessions = more ? [...sessions, ...response.sessions] : response.sessions;
  $('more-sessions').hidden = response.sessions.length < 100;
  renderSessions();
}
async function allItems(path, key) {
  let items = [], offset = 0;
  while (true) {
    const response = await api(`${path}${path.includes('?') ? '&' : '?'}offset=${offset}&limit=500`);
    items.push(...response[key]);
    if (response[key].length < 500) return items;
    offset += 500;
  }
}
function auditFailed(item) {
  const data = item.payload || item;
  if (['completed', 'started', 'running'].includes(data.status)) return false;
  return data.status === 'failed' || Boolean(data.error || data.exception || data.failure);
}
function auditTimeline(logs, models, events, transcript) {
  const actors = { agent: '主 Agent', task_plan: '规划 Agent', reflection: '反思 Agent', search: '搜索 Agent', page_files: '页面/文件 Agent' };
  const actorName = value => actors[value] || (value?.startsWith('specialist_') ? actorName(value.slice(11)) : value?.startsWith('history_summary') ? '上下文管理' : value?.startsWith('reflection') ? '反思 Agent' : value || '执行器（未注明 Agent）');
  const links = new Map();
  const groups = new Map();
  for (const event of events) {
    const p = event.payload || {};
    if (p.agent) {
      const actor = actorName(p.agent) + (p.agent_run_id ? ` [${p.agent_run_id.slice(0, 8)}]` : '');
      for (const id of [p.log_id, p.model_call_id, p.tool_call_id, ...(p.tool_calls || []).map(call => call.id)].filter(Boolean)) links.set(id, actor);
    }
    if (event.kind === 'execution_wave') {
      for (const member of p.members || []) groups.set(member.log_id, { ...p, member });
    }
  }
  const items = [
    ...logs.map(item => ({ ...item, auditKind: 'tool' })),
    ...models.map(item => ({ ...item, auditKind: 'model' })),
    ...events.map(item => ({ ...item, auditKind: 'context' })),
    ...transcript.map((row, index) => ({ ...row, id: row.id || `${row.task_id}-${index}`, auditKind: 'transcript', model: '模型回复', purpose: row.payload.tool_calls?.map(call => call.function.name).join('、') || '最终回答' })),
  ];
  for (const item of items) {
    item.auditTime = item.started_at || item.created_at;
    item.auditActor = links.get(item.id) || links.get(item.model_call_id) || (item.auditKind === 'model' ? actorName(item.purpose) : item.auditKind === 'transcript' ? '主 Agent' : item.payload?.agent ? actorName(item.payload.agent) : item.auditKind === 'tool' && item.source === 'agent' ? '主 Agent' : item.auditKind === 'context' && /summary|compression/.test(item.kind) ? '上下文管理' : '执行器');
    const group = groups.get(item.id);
    item.auditParallel = group ? `调度组 ${group.wave_id.slice(0, 8)} · ${group.member.tool === 'navigate_tender_entry' ? '导航组内串行' : '并行调度'} · 同组节点 ${group.step_ids.join('、')}` : '';
  }
  return items.sort((a, b) => (Date.parse(a.auditTime) || Infinity) - (Date.parse(b.auditTime) || Infinity));
}
function auditPosition() {
  const list = $('log-list');
  const top = list.getBoundingClientRect().top;
  const anchor = [...list.querySelectorAll('details')].find(node => node.getBoundingClientRect().bottom > top);
  return { top: list.scrollTop, id: anchor?.dataset.id, offset: anchor ? anchor.getBoundingClientRect().top - top : 0 };
}
function restoreAuditPosition(position) {
  const list = $('log-list');
  const anchor = [...list.querySelectorAll('details')].find(node => node.dataset.id === position.id);
  list.scrollTop = position.top;
  if (anchor) list.scrollTop += anchor.getBoundingClientRect().top - list.getBoundingClientRect().top - position.offset;
}
function renderLogs(logs, previous = new Map()) {
  const opened = new Set([...$('log-list').querySelectorAll('details[open]')].map(node => node.dataset.id));
  $('log-list').replaceChildren();
  if (!logs.length) { const node = document.createElement('p'); node.className = 'log-empty'; node.textContent = '暂无调用记录'; $('log-list').append(node); }
  for (const log of logs) {
    const signature = JSON.stringify(log);
    const existing = previous.get(log.id);
    if (existing?.dataset.signature === signature) { $('log-list').append(existing); continue; }
    const node = document.createElement('details'); node.className = 'tool-log'; node.dataset.id = log.id; node.open = opened.has(log.id);
    node.dataset.signature = signature;
    node.classList.toggle('audit-failed', auditFailed(log));
    const title = document.createElement('summary');
    const name = document.createElement('span'); name.textContent = log.tool;
    const meaning = document.createElement('span'); meaning.className = 'log-meta'; meaning.textContent = ' · ' + (toolLabels[log.tool] || '任务操作');
    title.append(name, meaning); node.append(title);
    const description = document.createElement('div'); description.className = 'log-meta';
    description.textContent = `Tool · ${toolDescriptions[log.tool] || toolLabels[log.tool] || '执行任务操作'}`; node.append(description);
    const meta = document.createElement('div'); meta.className = 'log-meta'; meta.textContent = `${stateNames[log.status] || log.status} · ${log.duration_ms == null ? '—' : log.duration_ms + ' ms'} · ${log.source}`; node.append(meta);
    let loaded = false;
    async function loadResult() {
      if (loaded || !node.open) return;
      loaded = true;
      try {
        const full = Object.hasOwn(log, 'result') ? log : await api(`/tool-calls/${log.id}`);
        node.classList.toggle('audit-failed', auditFailed(full) || Boolean(full.result?.error));
        for (const [label, value] of [['参数', full.arguments], ['结果', full.result], ['错误', full.error]]) {
          if (value == null) continue;
          const caption = document.createElement('div'); caption.className = 'log-caption'; caption.textContent = label;
          const content = document.createElement('pre'); content.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
          node.append(caption, content);
        }
      } catch (error) { loaded = false; notice(error.message); }
    }
    node.ontoggle = loadResult;
    $('log-list').append(node);
    if (node.open) loadResult();
  }
}
async function refreshLogs(token) {
  renderRequestIdentity();
  if (refreshingLogs) return;
  refreshingLogs = true;
  try {
  const selectedTask = auditTaskId;
  const selectedSession = sessionId;
  const selectedOffset = logOffset;
  const query = `?session_id=${encodeURIComponent(sessionId)}` + (selectedTask ? `&task_id=${encodeURIComponent(selectedTask)}` : '');
  const [logs, models, events, auditTask] = sessionId ? await Promise.all([
    allItems(`/tool-calls${query}&include_results=false`, 'tool_calls'), allItems('/model-calls' + query, 'model_calls'),
    allItems('/context-events' + query, 'context_events'), selectedTask ? api(`/tasks/${selectedTask}`) : null]) : [[], [], [], null];
  if (token !== generation || selectedTask !== auditTaskId) return;
  const rows = sessionId ? await allItems(`/sessions/${sessionId}/messages`, 'messages') : [];
  if (token !== generation || selectedTask !== auditTaskId || selectedSession !== sessionId || selectedOffset !== logOffset) return;
  const transcript = rows.filter(row => row.payload.role === 'assistant' && (!selectedTask || row.task_id === selectedTask));
  const scope = JSON.stringify([selectedSession, selectedTask, selectedOffset]);
  const snapshot = JSON.stringify([scope, logs, models, events, transcript, auditTask?.usage]);
  if (snapshot === auditSnapshot) return;
  const position = scope === auditScope ? auditPosition() : { top: 0 };
  const previous = scope === auditScope ? new Map([...$('log-list').querySelectorAll('details')].map(node => [node.dataset.id, node])) : new Map();
  const opened = new Set([...$('log-list').querySelectorAll('details[open]')].map(node => node.dataset.id));
  renderLogs(logs, previous);
  let input = 0, output = 0, unknown = 0;
  for (const item of models) {
    if (Number.isInteger(item.usage?.prompt_tokens) && Number.isInteger(item.usage?.completion_tokens)) {
      input += item.usage.prompt_tokens; output += item.usage.completion_tokens;
    } else unknown++;
  }
  $('usage-summary').textContent = models.length ? `${selectedTask ? '本轮' : '本对话'} · 输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} Token${unknown ? ` · ${unknown} 次用量未知` : ''}` : '暂无 Token 记录';
  const usage = auditTask?.usage;
  if (Number.isInteger(usage?.remaining_tokens)) {
    const request = usage.last_request || {};
    $('usage-summary').textContent += ` · 剩余 ${usage.remaining_tokens.toLocaleString()} Token`;
    if (Number.isInteger(request.estimated_input_tokens)) {
      $('usage-summary').textContent += ` · 最近请求输入估算 ${request.estimated_input_tokens.toLocaleString()} · 输出预留 ${request.reserved_output_tokens.toLocaleString()} Token`;
    }
  }
  function appendAudit(label, items, kind) {
    if (!items.length) return;
    const heading = document.createElement('h2'); heading.className = 'audit-title'; heading.textContent = label; $('log-list').append(heading);
    for (const item of items) {
      const displayKind = item.auditKind || kind;
      if (displayKind === 'tool') {
        const toolNode = toolNodes.get(item.id);
        if (toolNode) $('log-list').append(toolNode);
        continue;
      }
      const ident = displayKind + item.id;
      const signature = JSON.stringify(item);
      const existing = previous.get(ident);
      if (existing?.dataset.signature === signature) { $('log-list').append(existing); continue; }
      const node = document.createElement('details'); node.className = 'tool-log'; node.dataset.id = displayKind + item.id; node.open = opened.has(node.dataset.id);
      node.dataset.signature = signature;
      node.classList.toggle('audit-failed', auditFailed(item));
      const summary = document.createElement('summary');
      summary.textContent = displayKind === 'context' ? ({ compression_attempt: '压缩尝试与诊断', compression_recovery: '压缩恢复与验证', agent_route: '专业 Agent 分工', agent_step: '专业 Agent 执行', reflection: '独立结果评审', history_summary: '历史摘要', candidate_validation: '模型项目核验', batch_plan: '下载范围计划', tool_result: '工具结果压缩', task_plan: '意图与任务计划', step_update: '步骤状态更新', plan_update: '任务重新规划', task_resume: '任务断点恢复', tool_attempt: '工具尝试与重试', model_retry: '模型请求重试', plan_repair: '计划格式修正' }[item.kind] || item.kind) : `${item.model} · ${item.purpose}`;
      if (displayKind === 'context') {
        summary.textContent = ({ assessment_binding: '核验步骤绑定', execution_progress: '业务进展检查', execution_recovery: '无进展恢复评审', execution_repair: '修复提案校验与执行', task_handoff: '任务增量交接摘要' }[item.kind] || summary.textContent);
        summary.textContent += ` · ${item.payload.status || (item.payload.retrying ? '重试中' : '')}${item.payload.stage ? ' · ' + item.payload.stage : ''}${item.payload.agent ? ' · ' + item.payload.agent : ''}${item.payload.exception ? ' · ' + item.payload.exception.type + ': ' + item.payload.exception.message.slice(0, 160) : ''}`;
      }
      const eventLabels = { execution_advice: '反思建议（未执行，交由主 Agent）', execution_wave: '并行调度组结果', reference_repair: '页面与日志引用修正', goal_checkpoint: '目标进度与证据缺口', budget_check: '下一次请求预算检查', search_budget: '搜索额度使用', search_checkpoint: '搜索自检与目标缺口', search_extension: '追加搜索额度', search_strategy_review: '检索策略评审', agent_recovery: '子 Agent 交接恢复' };
      if (displayKind === 'context' && eventLabels[item.kind]) summary.textContent = eventLabels[item.kind] + ' · ' + (item.payload.status || '') + (item.payload.exception ? ' · ' + item.payload.exception.type + ': ' + item.payload.exception.message.slice(0, 160) : '');
      if (displayKind === 'transcript') summary.textContent = `模型回复 · ${item.purpose}`;
      const pre = document.createElement('pre'); pre.textContent = JSON.stringify(['context', 'transcript'].includes(displayKind) ? item.payload : { id: item.id, status: item.status, usage: item.usage, duration_ms: item.duration_ms, error: item.error }, null, 2);
      node.append(summary, pre); $('log-list').append(node);
    }
  }
  const toolNodes = new Map([...$('log-list').querySelectorAll('details')].map(node => [node.dataset.id, node]));
  $('log-list').replaceChildren();
  const timeline = auditTimeline(logs, models, events, transcript);
  appendAudit('执行时间线（含压缩与恢复）', timeline, 'timeline');
  timeline.forEach((item, index) => {
    const id = item.auditKind === 'tool' ? item.id : item.auditKind + item.id;
    const node = [...$('log-list').querySelectorAll('details')].find(node => node.dataset.id === id);
    if (!node) return;
    let trace = node.querySelector('.audit-trace');
    if (!trace) { trace = document.createElement('span'); trace.className = 'log-meta audit-trace'; node.querySelector('summary').prepend(trace, document.createElement('br')); }
    const time = item.auditTime ? new Date(item.auditTime).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '时间未记录';
    trace.textContent = `#${index + 1} · ${time} · ${item.auditActor}${item.auditParallel ? ' · ' + item.auditParallel : ''}`;
  });
  restoreAuditPosition(position);
  auditScope = scope;
  auditSnapshot = snapshot;
  } finally { refreshingLogs = false; }
}
async function loadMessages(id, token) {
  const rows = await allItems(`/sessions/${id}/messages`, 'messages');
  const ids = [...new Set(rows.map(row => row.task_id))];
  const tasks = await Promise.all(ids.map(id => api(`/tasks/${id}`)));
  if (token !== generation) return;
  const byId = new Map(tasks.map(task => [task.id, task]));
  confirmationTaskId = tasks.at(-1)?.status === 'awaiting_clarification' ? tasks.at(-1).id : null;
  clearTimeout(confirmationTimer);
  const confirmationDeadline = tasks.at(-1)?.confirmation?.expires_at;
  if (confirmationTaskId && confirmationDeadline) {
    confirmationTimer = setTimeout(async () => {
      if (token !== generation) return;
      try { await loadMessages(id, token); } catch (error) { console.error(error); }
    }, Math.max(0, Date.parse(confirmationDeadline) - Date.now()) + 1000);
  }
  const chosen = auditTaskId && ids.includes(auditTaskId) ? auditTaskId : ids.at(-1) || null;
  $('audit-task').replaceChildren(new Option('全部请求', ''));
  for (const task of tasks) {
    const request = task.message || rows.find(row => row.task_id === task.id && row.payload.role === 'user')?.payload.content || task.id;
    $('audit-task').append(new Option(request.slice(0, 80), task.id));
  }
  auditTaskId = chosen; $('audit-task').value = chosen || '';
  $('messages').replaceChildren();
  let lastTask = null;
  function appendResult(id) {
    const task = byId.get(id);
    if (!task || !terminal.has(task.status)) return;
    const fallback = rows.filter(row => row.task_id === id && row.payload.role === 'assistant' && !row.payload.tool_calls?.length).at(-1)?.payload.content;
    if (task.answer || fallback) message('assistant', task.answer || fallback, Boolean(task.error_code), id);
    if (!['completed', 'scheduled', 'awaiting_confirmation', 'awaiting_clarification'].includes(task.status)) message('assistant', `${taskStatus(task)}，已保留本次执行记录。`, true, id);
  }
  for (const row of rows) {
    if (lastTask && row.task_id !== lastTask) appendResult(lastTask);
    lastTask = row.task_id;
    const item = row.payload;
    if (item.role === 'user' && item.content) message(item.role, item.content, false, row.task_id);
  }
  if (lastTask) appendResult(lastTask);
  if (!$('messages').children.length) empty();
  scrollEnd();
}
async function poll(id, token) {
  taskId = id;
  try {
    while (token === generation) {
      const task = await api(`/tasks/${id}`);
      if (token !== generation) return;
      setBusy(true, taskStatus(task) || task.status);
      await refreshLogs(token);
      if (token !== generation) return;
      if (terminal.has(task.status)) {
        await loadMessages(sessionId, token);
        await refreshBatches(token);
        if (token !== generation) return;
        taskId = null; setBusy(false, taskStatus(task));
        await loadSessions();
        return;
      }
      pending(true, toolLabels[task.progress] || task.progress || '正在整理招标信息…');
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  } catch (error) { if (token === generation) { notice('连接中断：' + error.message); setBusy(true, '连接中断'); } }
}
async function selectSession(id) {
  const token = ++generation; ready = false; sessionId = id; taskId = null; remember(id); notice(); setBusy(false); renderSessions();
  batchSignature = ''; $('batch-area').hidden = true; $('batch-list').replaceChildren();
  logOffset = 0; auditTaskId = null;
  try {
    const session = await api(`/sessions/${id}`);
    if (token !== generation) return;
    $('conversation-title').textContent = session.title;
    await loadMessages(id, token); await refreshLogs(token);
    await refreshBatches(token);
    if (token !== generation) return;
    ready = true; setBusy(false);
    if (session.active_task_id) { setBusy(true); pending(true); await poll(session.active_task_id, token); }
  } catch (error) { if (token === generation) { notice(error.message); ready = false; setBusy(false, '加载失败'); } }
}
function newChat() {
  confirmationTaskId = null;
  clearTimeout(confirmationTimer);
  auditSnapshot = ''; auditScope = '';
  generation++; sessionId = null; taskId = null; auditTaskId = null; $('audit-task').replaceChildren(new Option('全部请求', '')); ready = true; batchSignature = ''; $('batch-area').hidden = true; $('batch-list').replaceChildren(); remember(null); $('input').value = ''; notice(); $('conversation-title').textContent = '新对话'; empty(); renderLogs([]); $('usage-summary').textContent = '暂无 Token 记录'; setBusy(false); renderSessions(); sidebar(false); $('input').focus();
}
$('composer').addEventListener('submit', async event => {
  event.preventDefault(); const text = $('input').value.trim(); if (!text || busy || !ready) return;
  const token = generation; setBusy(true, '提交中'); notice();
  try {
    const request = { message: text }; if (sessionId) request.session_id = sessionId;
    if (confirmationTaskId) request.confirmation_task_id = confirmationTaskId;
    const result = await api('/chat', { method: 'POST', body: JSON.stringify(request) });
    if (token !== generation) { await loadSessions(); return; }
    sessionId = result.session_id; remember(sessionId); $('conversation-title').textContent = sessions.find(item => item.id === sessionId)?.title || text;
    auditTaskId = result.task_id; logOffset = 0;
    $('audit-task').append(new Option(text.slice(0, 80), result.task_id)); $('audit-task').value = result.task_id;
    $('input').value = ''; $('input').style.height = ''; message('user', text, false, result.task_id); pending(true); scrollEnd();
    await loadSessions(); await poll(result.task_id, token);
  } catch (error) { if (token === generation) { notice(error.message); setBusy(false, '提交失败'); } }
});
$('input').addEventListener('input', () => { $('input').style.height = 'auto'; $('input').style.height = Math.min($('input').scrollHeight, 170) + 'px'; setBusy(busy); });
$('input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } });
$('new-chat').onclick = newChat;
$('more-sessions').onclick = () => loadSessions(true).catch(error => notice(error.message));
$('open-sidebar').onclick = () => sidebar(true);
$('close-sidebar').onclick = $('scrim').onclick = () => sidebar(false);
function toggleLogs(open) { $('logs').hidden = !open; $('toggle-logs').setAttribute('aria-expanded', String(open)); if (open) refreshLogs(generation).catch(error => notice(error.message)); }
$('toggle-logs').onclick = () => toggleLogs($('logs').hidden);
$('close-logs').onclick = () => toggleLogs(false);
$('cancel-task').onclick = async () => {
  if (!taskId || !sessionId) return;
  try {
    $('cancel-task').disabled = true;
    await api(`/tasks/${taskId}/cancel`, { method: 'POST', body: JSON.stringify({ session_id: sessionId }) });
  } catch (error) { notice(error.message); }
  finally { $('cancel-task').disabled = false; }
};
$('retry').onclick = async () => { notice(); try { await loadSessions(); if (sessionId) await selectSession(sessionId); else { ready = true; setBusy(false); } } catch (error) { notice(error.message); } };
document.addEventListener('keydown', event => { if (event.key === 'Escape') { sidebar(false); toggleLogs(false); } });
async function init() {
  empty(); renderLogs([]);
  try { await loadSessions(); let saved; try { saved = localStorage.getItem('bidding.session'); } catch {} if (saved) await selectSession(saved); else { ready = true; setBusy(false); } }
  catch (error) { notice('无法连接服务：' + error.message); }
}
setInterval(() => { if (sessionId && ready && !document.hidden) { refreshBatches(); if (!$('logs').hidden) refreshLogs(generation).catch(error => notice(error.message)); } }, 3000);
icons(); init();
