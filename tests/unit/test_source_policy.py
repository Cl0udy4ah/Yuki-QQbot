"""Source display intent classification tests."""

from __future__ import annotations

import pytest

from qq_ai_bot.services.source_policy import SourceDisplayPolicy


@pytest.mark.parametrize(
    "text",
    [
        "给出来源",
        "请附上出处",
        "原文链接是什么",
        "参考资料有哪些",
        "给我网址",
        "从哪里查到的",
        "有证据吗",
        "请联网搜索并附上来源",
        "sources",
        "citation",
        "reference links",
        "继续解释并给出来源",
    ],
)
def test_requested_recognizes_explicit_source_requests(text: str) -> None:
    assert SourceDisplayPolicy().requested(text)


@pytest.mark.parametrize(
    "text",
    [
        "不要显示来源",
        "不用给链接",
        "帮我分析这个网站",
        "网站为什么打不开",
        "来源这个词是什么意思",
        "最近 DeepSeek 有什么更新？",
        "Please answer without citations",
    ],
)
def test_requested_rejects_negation_meta_language_and_normal_web_questions(text: str) -> None:
    assert not SourceDisplayPolicy().requested(text)


@pytest.mark.parametrize(
    "text",
    ["来源呢", "链接", "出处？", "把网址发我", "从哪查的", "show me the sources"],
)
def test_standalone_request_recognizes_short_followups(text: str) -> None:
    assert SourceDisplayPolicy().standalone_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "继续解释并给出来源",
        "不要显示来源",
        "来源这个词是什么意思",
        "帮我打开链接并分析为什么网站打不开",
    ],
)
def test_standalone_request_rejects_compound_or_negative_requests(text: str) -> None:
    assert not SourceDisplayPolicy().standalone_request(text)
