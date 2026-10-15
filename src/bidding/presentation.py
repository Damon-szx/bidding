"""Business-facing wording; original transcripts remain in the audit store."""
import re

TOOL_LABELS = {
    'record_search_progress': '整理搜索线索',
    'request_search_extension': '评估补充检索',
    'review_search_strategy': '检查检索策略',
    'delegate_agent': '分配专业处理任务',
    'get_task_plan': '查看任务计划', 'update_task_plan': '调整任务计划',
    'execute_plan_steps': '执行任务步骤', 'request_clarification': '确认需求',
    'resume_task': '继续未完成任务', 'record_project_assessment': '核验项目条件',
    'plan_acquisition': '确定下载范围',
    'list_acquisition_batches': '查看下载任务',
    'control_acquisition_batch': '调整下载任务',
    "search_tenders": "检索招标信息",
    "fetch_tender_detail": "核对公告原文",
    "render_tender_page": "加载公告页面",
    "navigate_tender_entry": "查看项目详情",
    'scan_tender_listing': '初筛列表并翻页',
    "discover_tender_files": "查找招标文件",
    "inspect_document_access": "检查文件获取入口",
    "download_tender_file": "下载招标文件",
    "read_tool_result": "查阅完整资料",
}


def business_text(text):
    text = text or ""
    # Some providers prepend execution commentary to an otherwise Chinese answer.
    lines = text.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "")
    if re.match(r"(?:I (?:have|will|need|found)|I'll|Let me|Based on|Now I|The (?:search|direct))\b", first):
        start = next((index for index, line in enumerate(lines) if re.search(r"[\u4e00-\u9fff]", line)), None)
        if start is not None:
            text = "\n".join(lines[start:])
    for name, label in TOOL_LABELS.items():
        text = text.replace(name, label)
    text = re.sub(r"`?verified`?\s*[:：=为]+\s*`?false`?", "尚未核对公告原文", text)
    return text


def partial_answer(state):
    spec = state.get('spec', {})
    if (spec.get('action') in ('research', 'download') and (spec.get('result_kind') == 'projects' or spec.get('criteria'))) or state.get('qualified_projects'):
        from bidding.eligibility import matching_answer
        return matching_answer(state)
    if state.get('spec'):
        return '本次任务尚未完成，已保留步骤、资料和执行记录。可以继续未完成的步骤；暂不把搜索线索当作已核实成果。'
    candidates = [item for item in state.get("candidates", []) if item.get("source_kind") != "document"]
    if not candidates:
        return "本次任务未能完成，暂时没有可展示的项目结果。可以缩小查询范围后重试。"
    lines = ["本次查询尚未完成，先提供已找到的项目线索（尚未核对公告原文）："]
    for item in candidates[:5]:
        title = item.get("title") or "未命名项目"
        url = item.get("source_url")
        lines.append(f"- {title}" + (f"：{url}" if url else "（来源链接暂缺）"))
    return "\n\n".join(lines)
