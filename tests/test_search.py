import json

from bidding.search import normalize_results
from bidding.context import compact_preview, ContextManager
from bidding.presentation import business_text, partial_answer
from bidding.storage import Store


def test_baidu_records_preserve_trailing_urls_and_order():
    raw = "details:\nTitle:电网采购计划\nContent:" + "正文" * 10000 + "\nURL:https://example.org/plan\n\ndetails:\nTitle:储能招标\nContent:报名平台 https://login.example.org\nURL:https://example.org/tender"
    items = normalize_results([{"type": "text", "text": raw}])
    assert [item["source_url"] for item in items] == ["https://example.org/plan", "https://example.org/tender"]
    assert items[1]["candidate_id"] == "candidate-2"
    assert len(items[0]["snippet"]) == 700


def test_structured_and_json_text_search_deduplicate():
    record = {"title": "项目", "url": "https://example.org/bid", "date": "2026-10-08", "content": "内容"}
    items = normalize_results({"results": [record, {"text": json.dumps({"references": [record]})}]})
    assert len(items) == 1
    assert items[0]["published_at"] == "2026-10-08"


def test_missing_source_does_not_use_link_from_body():
    items = normalize_results("Title:项目\nContent:登录 https://example.org/login 获取文件")
    assert items[0]["source_url"] is None


async def test_search_projection_skips_model_summary_and_keeps_sources(tmp_path):
    class Model:
        async def call(self, *args, **kwargs):
            raise AssertionError("Search records should not need a model summary")
    store = Store(tmp_path)
    manager = ContextManager(store, Model(), "task")
    content = "Title:项目\nContent:" + "正文" * 10000 + "\nURL:https://example.org/source"
    raw = {"query": "电力", "candidates": normalize_results(content), "content": content}
    reduced = await manager.reduce_tool("search_tenders", raw, "log")
    assert reduced["candidates"][0]["source_url"] == "https://example.org/source"
    assert "content" not in reduced
    restored = compact_preview(raw, "log", 2000)
    assert restored["candidates"][0]["source_url"] == "https://example.org/source"
    assert raw["content"] == content


def test_business_text_and_partial_results():
    assert "fetch_tender_detail" not in business_text("用 fetch_tender_detail 核对 verified=false")
    text = partial_answer({"candidates": [{"title": "电力项目", "source_url": "https://example.org"}]})
    assert "电力项目" in text and "https://example.org" in text and "尚未核对" in text
    assert business_text("I have enough information. Let me compile results.\n\nBased on my search...\n\n---\n\n以下是项目结果。") == "以下是项目结果。"
