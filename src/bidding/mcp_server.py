from mcp.server.fastmcp import FastMCP

from bidding.api import agent, store

mcp = FastMCP("bidding-acquisition")


@mcp.tool()
async def search_tenders(query: str):
    """通过已配置的搜索服务查找招标候选来源；query 为中文检索式。"""
    result, _ = await agent.runtime.call("search_tenders", {"query": query}, source="mcp")
    return result


@mcp.tool()
async def fetch_tender_detail(url: str):
    """读取并保存公开公告或列表页面，url 须为完整来源网址。"""
    result, _ = await agent.runtime.call("fetch_tender_detail", {"url": url}, source="mcp")
    return result


@mcp.tool()
async def render_tender_page(url: str):
    """在浏览器中渲染公开页面，保留网址中的前端路由。"""
    result, _ = await agent.runtime.call('render_tender_page', {'url': url}, source='mcp')
    return result


@mcp.tool()
async def navigate_tender_entry(announcement_id: str, entry_id: str):
    """使用同一已观察页面的 announcement_id 和 entry_id 打开项目或分页入口。"""
    result, _ = await agent.runtime.call('navigate_tender_entry', {'announcement_id': announcement_id, 'entry_id': entry_id}, source='mcp')
    return result


@mcp.tool()
async def discover_tender_files(announcement_id: str):
    """从已保存公告中发现附件候选，announcement_id 为页面 ID。"""
    result, _ = await agent.runtime.call("discover_tender_files", {"announcement_id": announcement_id}, source="mcp")
    return result


@mcp.tool()
async def download_tender_file(url: str, announcement_id: str):
    """下载公告中已观察的附件并校验文件；url 须原样使用发现结果，保留公告来源。"""
    result, _ = await agent.runtime.call("download_tender_file", {"url": url, "announcement_id": announcement_id}, source="mcp")
    return result


@mcp.tool()
async def get_acquisition_task(task_id: str):
    """查询已观察任务 ID 对应的采集状态和结果。"""
    result, _ = await agent.runtime.call("get_acquisition_task", {"task_id": task_id}, source="mcp")
    return result


if __name__ == "__main__":
    mcp.run()
