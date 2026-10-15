import json
import os
import re
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def public_link(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    return value.strip() if parsed.scheme in ("http", "https") and parsed.hostname else None


def normalize_results(content):
    """Parse provider records before shortening bodies, keeping source URLs intact."""
    candidates = []

    def add(title, url, body, date=None):
        url = public_link(url)
        if not title and not url:
            return
        title = str(title or "")
        kind = "unknown"
        candidates.append({"title": title, "source_url": url,
            "published_at": date, "snippet": str(body or "")[:700],
            "source_site": urlsplit(url).hostname if url else None,
            "information_type": kind, "verification_status": "search_only",
            "source_kind": "document" if url and re.search(r"\.(pdf|docx?|xlsx?|zip)(?:$|\?)", url, re.I) else "page"})

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("title") and (value.get("url") or value.get("source_url")):
                add(value["title"], value.get("url") or value.get("source_url"),
                    value.get("snippet") or value.get("content") or value.get("description"),
                    value.get("published_at") or value.get("published_date") or value.get("date"))
            else:
                for key in ("results", "references", "data", "content", "text", "items"):
                    if key in value:
                        visit(value[key])
        elif isinstance(value, str):
            try:
                nested = json.loads(value)
            except ValueError:
                nested = None
            if isinstance(nested, (dict, list)):
                visit(nested)
                return
            # Baidu's text response uses explicit Title/Content/URL record labels.
            for block in re.split(r"(?m)^\s*Title[:：]", value)[1:]:
                title, _, rest = block.partition("\n")
                sources = re.findall(r"(?m)^\s*URL[:：]\s*(https?://\S+)", rest)
                body = re.sub(r"(?m)^\s*(?:URL[:：].*|details:\s*)$", "", rest)
                body = re.sub(r"^\s*Content[:：]", "", body).strip()
                add(title.strip(), sources[-1] if sources else None, body)

    visit(content)
    unique = []
    seen = set()
    for item in candidates:
        key = item["source_url"] or item["title"]
        if key not in seen:
            seen.add(key)
            unique.append({"candidate_id": "candidate-" + str(len(unique) + 1), **item})
    return unique


async def search_tenders(query):
    url = os.getenv("SEARCH_MCP_URL")
    if not url:
        raise ValueError("SEARCH_MCP_URL is not configured; configure a search MCP or supply an announcement URL")
    token = os.getenv("SEARCH_MCP_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else None
    async with streamablehttp_client(url, headers=headers, timeout=30) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(os.getenv("SEARCH_MCP_TOOL", "web_search"), {os.getenv("SEARCH_MCP_QUERY_FIELD", "query"): query})
            if result.isError:
                raise ValueError("Search MCP returned an error")
            structured = getattr(result, "structuredContent", None)
            content = structured or [{"type": item.type, "text": getattr(item, "text", "")} for item in result.content if item.type == "text"]
            # Search services use different schemas; retain their actual content for the agent.
            return {"query": query, "provider": "mcp", "candidates": normalize_results(content),
                "content": json.loads(json.dumps(content)), "verified": False}
