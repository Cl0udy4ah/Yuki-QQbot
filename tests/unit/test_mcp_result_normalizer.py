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
    assert result.content == ({"type": "image", "data": "base64-image", "mimeType": "image/png"},)


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


def test_single_json_text_is_promoted_redacted_and_keeps_non_text_blocks() -> None:
    raw = SimpleNamespace(
        structuredContent=None,
        isError=False,
        content=(
            _Content(
                type="text",
                text=json.dumps(
                    [
                        {
                            "id": 12,
                            "title": "Ars Technica",
                            "password": "feed-password",
                            "auth": {
                                "accessToken": "access-secret",
                                "cookie": "session-secret",
                            },
                        }
                    ]
                ),
            ),
            _Content(type="image", data="base64-image", mimeType="image/png"),
        ),
    )

    result = normalize_mcp_result(raw, server_id="miniflux", tool_name="get_feeds")

    assert result.data == [
        {
            "id": 12,
            "title": "Ars Technica",
            "password": "[redacted]",
            "auth": {
                "accessToken": "[redacted]",
                "cookie": "[redacted]",
            },
        }
    ]
    assert result.content == (
        {"type": "image", "data": "base64-image", "mimeType": "image/png"},
    )


def test_plain_and_error_text_redact_inline_credentials_without_json_promotion() -> None:
    plain = normalize_mcp_result(
        SimpleNamespace(
            structuredContent=None,
            isError=False,
            content=(
                _Content(
                    type="text",
                    text="Authorization: Bearer access-secret; password=hunter2",
                ),
            ),
        ),
        server_id="example",
        tool_name="plain",
    )
    scalar_json = normalize_mcp_result(
        SimpleNamespace(
            structuredContent=None,
            isError=False,
            content=(_Content(type="text", text="42"),),
        ),
        server_id="example",
        tool_name="scalar",
    )

    rendered = json.dumps(plain.model_payload(), ensure_ascii=False)
    assert "access-secret" not in rendered
    assert "hunter2" not in rendered
    assert rendered.count("[redacted]") == 2
    assert scalar_json.data is None
    assert scalar_json.content == ({"type": "text", "text": "42"},)


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
