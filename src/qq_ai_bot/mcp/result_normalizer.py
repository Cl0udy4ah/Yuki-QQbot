"""Convert MCP SDK content blocks into the unified kernel result."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.capabilities.results import ToolExecutionResult


def normalize_mcp_result(value: Any, *, server_id: str, tool_name: str) -> ToolExecutionResult:
    content: list[dict[str, Any]] = []
    for item in getattr(value, "content", ()):
        if hasattr(item, "model_dump"):
            dumped = item.model_dump(mode="json", exclude_none=True)
            content.append(dict(dumped) if isinstance(dumped, dict) else {"value": dumped})
        else:
            content.append({"value": str(item)})
    structured = getattr(value, "structuredContent", None)
    is_error = bool(getattr(value, "isError", False))
    return ToolExecutionResult(
        ok=not is_error,
        data=structured,
        content=tuple(content),
        error_code="mcp_tool_error" if is_error else None,
        public_message="MCP 工具返回错误" if is_error else None,
        retryable=False,
        mutation_committed=False,
        provider_id=f"mcp.{server_id}",
        tool_name=tool_name,
    )
