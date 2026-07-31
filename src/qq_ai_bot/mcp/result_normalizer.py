"""Convert MCP SDK content blocks into the unified kernel result."""

from __future__ import annotations

import json
from typing import Any

from qq_ai_bot.capabilities.results import ToolExecutionResult


def normalize_mcp_result(value: Any, *, server_id: str, tool_name: str) -> ToolExecutionResult:
    structured = getattr(value, "structuredContent", None)
    is_error = bool(getattr(value, "isError", False))
    content: list[dict[str, Any]] = []
    public_error = ""
    upstream_error_code = ""
    retryable = False
    for item in getattr(value, "content", ()):
        if hasattr(item, "model_dump"):
            dumped = item.model_dump(mode="json", exclude_none=True)
            normalized = dict(dumped) if isinstance(dumped, dict) else {"value": dumped}
        else:
            normalized = {"value": str(item)}
        # MCP servers commonly mirror structuredContent into a large text block for
        # backwards-compatible clients. Keeping both can double the payload and make
        # the result budgeter discard the useful structured data. On successful
        # structured results, retain only non-text blocks such as images/resources.
        if structured is not None and not is_error and normalized.get("type") == "text":
            continue
        if is_error and normalized.get("type") == "text":
            parsed_error = _parse_structured_error(normalized.get("text"))
            if parsed_error is not None:
                upstream_error_code, public_error, retryable = parsed_error
        content.append(normalized)
    return ToolExecutionResult(
        ok=not is_error,
        data=structured,
        content=tuple(content),
        error_code="mcp_tool_error" if is_error else None,
        public_message=(public_error or "MCP 工具返回错误") if is_error else None,
        retryable=retryable,
        mutation_committed=False if is_error else None,
        provider_id=f"mcp.{server_id}",
        tool_name=tool_name,
        metadata={
            "mcp_is_error": is_error,
            **({"mcp_error_code": upstream_error_code} if upstream_error_code else {}),
        },
    )


def _parse_structured_error(value: object) -> tuple[str, str, bool] | None:
    """Extract the bounded server error envelope used by MCP tool failures."""

    if not isinstance(value, str):
        return None
    opening = value.find("{")
    if opening < 0:
        return None
    try:
        payload = json.loads(value[opening:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    message = " ".join(str(payload.get("message", "")).split())[:500]
    if not message:
        return None
    error_code = str(payload.get("error_code", "")).strip()[:64]
    retryable = payload.get("retryable") is True
    return error_code, message, retryable
