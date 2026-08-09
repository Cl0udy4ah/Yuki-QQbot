"""MCP result normalization regression coverage."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.mcp.result_normalizer import normalize_mcp_result


class _Content:
    def __init__(self, **payload: object) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, object]:
        assert mode == "json"
        assert exclude_none is True
        return self._payload


class _Artifacts:
    def __init__(self) -> None:
        self.content = ""

    async def write_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        content: str,
        media_type: str,
        retention_seconds: int | None = None,
    ) -> str:
        assert provider_id == "mcp.miniflux"
        assert tool_name == "get_feeds"
        assert media_type == "application/json"
        assert retention_seconds is None
        self.content = content
        return "artifact-1"

    def configure_retention(self, retention_seconds: int) -> None:
        del retention_seconds

    async def read(
        self,
        handle_id: str,
        *,
        offset: int = 0,
        limit: int = 8000,
        query: str = "",
    ) -> dict[str, object] | None:
        del handle_id, offset, limit, query
        return None


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


@pytest.mark.asyncio
async def test_promoted_large_json_keeps_real_counts_preview_and_redacted_artifact() -> None:
    feeds = [
        {
            "id": index,
            "title": f"Feed {index}",
            "category": {"id": 9, "title": "Technology"},
            "password": f"secret-{index}",
            "description": "x" * 2000,
        }
        for index in range(27)
    ]
    result = normalize_mcp_result(
        SimpleNamespace(
            structuredContent=None,
            isError=False,
            content=(_Content(type="text", text=json.dumps(feeds)),),
        ),
        server_id="miniflux",
        tool_name="get_feeds",
    )
    artifacts = _Artifacts()

    rendered = await ToolResultBudgeter(
        max_characters=1800,
        artifacts=artifacts,
    ).render(result)
    payload = json.loads(rendered.text)
    artifact_payload = json.loads(artifacts.content)

    assert rendered.truncated is True
    assert rendered.artifact_id == "artifact-1"
    assert len(rendered.text) <= 1800
    assert payload["data"]["total_items"] == 27
    assert payload["data"]["shown_items"] == 5
    assert payload["data"]["items"][0]["title"] == "Feed 0"
    assert payload["data"]["items"][4]["title"] == "Feed 4"
    assert len(artifact_payload["data"]) == 27
    assert artifact_payload["data"][0]["password"] == "[redacted]"
    assert "secret-0" not in artifacts.content


@pytest.mark.asyncio
async def test_bounded_list_preview_uses_compact_record_projection() -> None:
    feeds = [
        {
            "id": index,
            "user_id": 1,
            "feed_url": f"https://example.com/{index}.xml",
            "site_url": "https://example.com",
            "title": f"Feed {index}",
            "description": "x" * 2000,
            "disabled": False,
            "password": "[redacted]",
            "category": {"id": 9, "title": "Technology", "user_id": 1},
            "ignore_http_cache": False,
        }
        for index in range(27)
    ]
    result = ToolExecutionResult(
        ok=True,
        data=feeds,
        provider_id="mcp.miniflux",
        tool_name="get_feeds",
    )

    rendered = await ToolResultBudgeter(max_characters=8000).render(result)
    payload = json.loads(rendered.text)
    preview = payload["data"]

    assert preview["total_items"] == 27
    assert preview["shown_items"] == 5
    assert [item["title"] for item in preview["items"]] == [
        "Feed 0",
        "Feed 1",
        "Feed 2",
        "Feed 3",
        "Feed 4",
    ]
    assert "description" not in preview["items"][0]
    assert "password" not in preview["items"][0]
    assert "ignore_http_cache" not in preview["items"][0]


@pytest.mark.asyncio
async def test_bounded_structured_result_preserves_upstream_total_and_page_size() -> None:
    result = ToolExecutionResult(
        ok=True,
        data={
            "total": 1612,
            "entries": [
                {
                    "id": index,
                    "title": f"Article {index}",
                    "status": "unread",
                    "content": "x" * 2000,
                }
                for index in range(10)
            ],
        },
        provider_id="mcp.miniflux",
        tool_name="get_entries",
    )

    rendered = await ToolResultBudgeter(max_characters=1400).render(result)
    payload = json.loads(rendered.text)

    assert payload["data"]["total"] == 1612
    assert payload["data"]["entries"]["total_items"] == 10
    assert payload["data"]["entries"]["shown_items"] == 5
    assert payload["data"]["entries"]["items"][0]["title"] == "Article 0"


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
