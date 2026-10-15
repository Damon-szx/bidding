(prefix) => {
  const visible = el => el.getClientRects().length > 0;
  const text = el => (el?.innerText || '').replace(/\s+/g, ' ').trim();
  const absolute = el => {
    const href = el.getAttribute('href');
    if (!href || href === '#' || /^(javascript|data|blob):/i.test(href)) return null;
    try { const url = new URL(href, location.href); return /^https?:$/.test(url.protocol) ? url.href : null; } catch { return null; }
  };
  let index = 0;
  const identify = el => { const id = `${prefix}-${index++}`; el.setAttribute('data-bidding-ref', id); return id; };
  const entries = [], attachments = [], pagination = [], document_access = [];
  const filePattern = /\.(pdf|docx?|xlsx?|zip|rar|7z)(?:$|[?\s])/i;
  const seen = new Set();
  for (const row of document.querySelectorAll('tbody tr, .el-table__row, .ant-table-row, .ivu-table-row')) {
    if (!visible(row)) continue;
    const cells = [...row.querySelectorAll('td')];
    if (!cells.length) continue;
    const title = text(cells[0]);
    const table = row.closest('table');
    const isProjectTable = /项目名称|公告名称|招标项目|采购项目/.test(text(table?.querySelector('thead')));
    const link = cells[0].querySelector('a[href], [role="link"]');
    if (!isProjectTable && !row.classList.contains('cur') && !(link && /招标|采购|公告|项目/.test(title))) continue;
    if (!title || filePattern.test(title)) continue;
    const action = link || row;
    const headerScope = row.closest('.el-table, .ant-table, .ivu-table, [role="grid"]') || table;
    let headers = [...(headerScope?.querySelectorAll('thead th, thead td, [role="columnheader"]') || [])].map(text);
    if (!headers.length && table) {
      const headerRow = [...table.querySelectorAll('tr')].find(candidate => {
        const labels = [...candidate.querySelectorAll('th,td')].map(text);
        return labels.some(value => /^(项目名称|公告名称|招标项目|采购项目)$/.test(value)) &&
          labels.some(value => /^(发布时间|发布日期|发布.*日期)$/.test(value));
      });
      if (headerRow) headers = [...headerRow.querySelectorAll('th,td')].map(text);
    }
    const dateColumn = headers.findIndex(value => /发布时间|发布日期|发布.*日期/.test(value));
    const key = title + '|' + text(cells[1]);
    if (seen.has(key)) continue;
    seen.add(key);
    entries.push({entry_id: identify(action), title, url: absolute(action),
      project_number: text(cells[1]) || null, project_status: text(cells[2]) || null,
      listed_date: text(cells[dateColumn >= 0 ? dateColumn : 3]).match(/\d{4}-\d{2}-\d{2}/)?.[0] || null,
      date_kind: dateColumn >= 0 ? 'published_at' : 'unknown', row_text: text(row).slice(0, 1200)});
    if (entries.length >= 30) break;
  }
  if (!entries.length) {
    for (const link of document.querySelectorAll('main a[href], article a[href], .list a[href], .news-list a[href]')) {
      const title = text(link), url = absolute(link);
      if (!visible(link) || !url || title.length < 8 || !/招标|采购|公告|项目/.test(title) || filePattern.test(url) || seen.has(url)) continue;
      seen.add(url);
      entries.push({entry_id: identify(link), title, url, project_number: null, project_status: null, listed_date: null});
      if (entries.length >= 30) break;
    }
  }
  for (const link of document.querySelectorAll('a, button, [role="link"]')) {
    const name = text(link), url = absolute(link);
    if (!visible(link) || link.closest('header, nav, aside, footer') || !name || name.length > 300 || /下载专区|操作说明|演示视频/.test(name)) continue;
    if (/^(获取招标文件|获取采购文件|领取招标文件|下载公告文件|下载变更公告文件|下载投标邀请书)$/.test(name)) {
      const ref = identify(link);
      document_access.push({entry_id: ref, title: name, url, kind: 'document_access'});
      if (/^下载(?:(?:变更)?公告文件|投标邀请书)$/.test(name)) {
        attachments.push({attachment_id: ref, url: `browser-attachment:${ref}`, href: url, name,
          document_type: 'unclassified_attachment', content_verified: false});
      }
      continue;
    }
    if (!(filePattern.test(name) || (url && filePattern.test(url)) || link.hasAttribute('download') || /附件下载|下载附件|下载招标文件/.test(name))) continue;
    const key = url || name;
    if (attachments.some(item => (item.href || item.name) === key)) continue;
    const ref = identify(link);
    attachments.push({attachment_id: ref, url: `browser-attachment:${ref}`, href: url, name,
      document_type: /资格预审公告/.test(name) ? 'prequalification_notice' : /招标文件/.test(name) ? 'tender_document' : 'unclassified_attachment'});
    if (attachments.length >= 30) break;
  }
  for (const el of document.querySelectorAll('.pagination a, .pagination button, .pagination li, .page a, .page button, .el-pagination .btn-next, .ivu-page-next, .ant-pagination-next, [aria-label="Next page"], a[rel="next"], button, [role="button"]')) {
    if (!visible(el) || el.hasAttribute('disabled') || /disabled/.test(el.className) || el.getAttribute('aria-disabled') === 'true') continue;
    if (/^(下一页|下页|next(?: page)?|>|›|»|加载更多|查看更多|load more|show more)$/i.test(text(el)) || el.getAttribute('aria-label') === 'Next page' || el.matches('.btn-next, .ivu-page-next, .ant-pagination-next, [rel="next"]')) {
      pagination.push({entry_id: identify(el), title: text(el) || '下一页', url: absolute(el), kind: 'pagination',
        mechanism: /加载更多|查看更多|load more|show more/i.test(text(el)) ? 'load_more' : absolute(el) ? 'link' : 'click'});
      break;
    }
  }
  const total = document.body.innerText.match(/(?:共|总计)\s*([\d,]+)\s*(?:条|个|项)/);
  return {entries, attachments, pagination, document_access,
    text_truncated: document.body.innerText.length > 60000,
    total_projects: total ? Number(total[1].replaceAll(',', '')) : null,
    page_type: entries.length ? 'listing' : attachments.length || /\/doc\//.test(location.hash) || document.querySelector('h1, article, main h2') ? 'detail' : 'unresolved',
    text: document.body.innerText.slice(0, 60000)};
}
