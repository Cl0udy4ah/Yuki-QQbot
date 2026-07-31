"""MCP result normalization regression coverage."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qq_ai_bot.capabilities.results import ToolResultBudgeter
from qq_ai_bot.mcp.result_normalizer import normalize_mcp_result


class _Content:
    def __init__(self, **payload: object) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, object]:
        assert mode == "json"
        assert exclude_none is True
        return self._payload


def test_successful_structured_result_drops_duplicated_text_fallback() -> None:
    structured = {"data": {"meals": {"1440": {"name": "麦辣鸡腿汉堡"}}}}
    raw = SimpleNamespace(
        structuredContent=structured,
        isError=False,
        content=(
            _Content(type="text", text="a very large backwards-compatible rendering"),
            _Content(type="image", data="base64-image", mimeType="image/png"),
        ),
    )

    result = normalize_mcp_result(raw, server_id="mcd", tool_name="query-meals")

    assert result.ok is True
    assert result.data == structured
    assert result.content == (
        {"type": "image", "data": "base64-image", "mimeType": "image/png"},
    )


def test_unstructured_or_error_text_content_is_preserved() -> None:
    plain = normalize_mcp_result(
        SimpleNamespace(
            structuredContent=None,
            isError=False,
            content=(_Content(type="text", text="plain result"),),
        ),
        server_id="example",
        tool_name="plain",
    )
    failed = normalize_mcp_result(
        SimpleNamespace(
            structuredContent={"code": "invalid"},
            isError=True,
            content=(_Content(type="text", text="useful remote error"),),
        ),
        server_id="example",
        tool_name="failed",
    )

    assert plain.content == ({"type": "text", "text": "plain result"},)
    assert failed.content == ({"type": "text", "text": "useful remote error"},)


def test_structured_mcp_error_preserves_bounded_reason_and_retryability() -> None:
    failed = normalize_mcp_result(
        SimpleNamespace(
            structuredContent=None,
            isError=True,
            content=(
                _Content(
                    type="text",
                    text=(
                        'Error executing tool get_album: {"error_code":"not_found",'
                        '"message":"NetEase resource was not found","retryable":false}'
                    ),
                ),
            ),
        ),
        server_id="netease_music",
        tool_name="get_album",
    )

    assert failed.ok is False
    assert failed.error_code == "mcp_tool_error"
    assert failed.public_message == "NetEase resource was not found"
    assert failed.retryable is False
    assert failed.metadata == {
        "mcp_is_error": True,
        "mcp_error_code": "not_found",
    }


@pytest.mark.asyncio
async def test_structured_menu_stays_within_budget_when_text_fallback_duplicates_it() -> None:
    structured = {
        "data": {
            "meals": {"1440": {"name": "麦辣鸡腿汉堡", "currentPrice": "23"}},
            "padding": "x" * 20_000,
        }
    }
    result = normalize_mcp_result(
        SimpleNamespace(
            structuredContent=structured,
            isError=False,
            content=(_Content(type="text", text="x" * 20_000),),
        ),
        server_id="mcd",
        tool_name="query-meals",
    )

    rendered = await ToolResultBudgeter(max_characters=32_000).render(result)
    payload = json.loads(rendered.text)

    assert rendered.truncated is False
    assert payload["data"]["data"]["meals"]["1440"]["name"] == "麦辣鸡腿汉堡"
