"""Backend-owned source rendering and model-output sanitization tests."""

from __future__ import annotations

from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.web.models import WebSearchSource


def source(
    url: str = "https://example.com/article",
    *,
    title: str = "真实页面",
) -> WebSearchSource:
    return WebSearchSource(
        source_id="source-1",
        title=title,
        url=url,
        domain="example.com",
        snippet="摘要",
        relevant_content="正文",
    )


def test_hidden_mode_removes_real_url_citations_and_trailing_source_section() -> None:
    text = (
        "这是联网后的正文[1]，普通链接 https://other.example/keep 应保留。\n\n"
        "来源：\n1. 真实页面\nhttps://example.com/article"
    )

    rendered = SourceRenderer().sanitize_model_text(text, (source(),))

    assert rendered == "这是联网后的正文，普通链接 https://other.example/keep 应保留。"


def test_model_markdown_link_to_real_source_keeps_label_but_not_url() -> None:
    rendered = SourceRenderer().sanitize_model_text(
        "请看[真实页面](https://example.com/article)，内容很清楚。",
        (source(),),
    )

    assert rendered == "请看真实页面，内容很清楚。"


def test_renderer_only_emits_real_deduplicated_sources() -> None:
    sources = (
        source(),
        source("https://example.com/article#fragment", title="重复页面"),
        source("https://example.org/other", title="第二页"),
    )

    rendered = SourceRenderer().render(sources)

    assert rendered == (
        "来源：\n"
        "1. 真实页面\n"
        "   https://example.com/article\n"
        "2. 第二页\n"
        "   https://example.org/other"
    )


def test_renderer_never_emits_more_than_five_sources() -> None:
    sources = tuple(
        source(f"https://example.com/{index}", title=f"页面 {index}") for index in range(8)
    )
    rendered = SourceRenderer().render(sources)
    assert "5. 页面 4" in rendered
    assert "页面 5" not in rendered
