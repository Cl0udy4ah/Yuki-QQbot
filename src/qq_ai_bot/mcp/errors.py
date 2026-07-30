"""Stable, secret-free diagnostics for MCP transport failures."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class MCPErrorDetails:
    code: str
    public_message: str
    retryable: bool


def classify_mcp_exception(exc: BaseException) -> MCPErrorDetails:
    """Collapse SDK task groups and HTTP errors into operator-facing categories."""

    errors = tuple(_walk_exceptions(exc))
    status: int | None = None
    for item in errors:
        status = _http_status(item)
        if status is not None:
            break
    if status in {401, 403}:
        return MCPErrorDetails(
            "mcp_authentication_failed",
            "MCP 鉴权失败，请检查 Token 是否有效",
            False,
        )
    if status == 429:
        return MCPErrorDetails(
            "mcp_rate_limited",
            "MCP 服务请求过于频繁，请稍后重试",
            True,
        )
    if status is not None and status >= 500:
        return MCPErrorDetails(
            "mcp_server_unavailable",
            "MCP 服务暂时不可用",
            True,
        )
    if status is not None:
        return MCPErrorDetails(
            f"mcp_http_{status}",
            "MCP 服务拒绝了请求",
            False,
        )
    if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in errors):
        return MCPErrorDetails("mcp_timeout", "MCP 工具调用超时", True)
    if any(isinstance(item, (OSError, httpx.TransportError)) for item in errors):
        return MCPErrorDetails("mcp_transport_unavailable", "MCP 服务暂时不可用", True)
    return MCPErrorDetails(type(exc).__name__, "MCP 工具暂时不可用", False)


def _walk_exceptions(exc: BaseException) -> Iterator[BaseException]:
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None
