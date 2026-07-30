"""Catalog gateway for large or initially-lazy MCP tool sets."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.mcp.binding import MCPToolBinding
from qq_ai_bot.mcp.manager import MCPManager


@dataclass(frozen=True, slots=True)
class MCPGatewayBinding:
    manager: MCPManager

    async def invoke(
        self,
        arguments: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        operation = str(arguments.get("operation", ""))
        if not operation:
            operation = (
                "call"
                if arguments.get("tool") or arguments.get("tool_name")
                else "describe"
                if arguments.get("describe")
                else "search"
            )
        server_id = str(arguments.get("server", arguments.get("server_id", "")))
        if operation == "search":
            query = str(arguments.get("search", arguments.get("query", "")))
            items = self.manager.search_tools(query, server_id=server_id or None)
            return ToolExecutionResult(
                ok=True,
                data=[
                    {
                        "server_id": item.server_id,
                        "tool_name": item.remote_tool_name,
                        "description": item.compact_description,
                    }
                    for item in items
                ],
                provider_id="mcp.gateway",
                tool_name="mcp_gateway",
            )
        tool_name = str(
            arguments.get("tool", arguments.get("tool_name", arguments.get("describe", "")))
        )
        if operation == "describe":
            if server_id and self.manager.describe_tool(server_id, tool_name) is None:
                try:
                    await self.manager.ensure_metadata(server_id)
                except (OSError, RuntimeError, TimeoutError, ValueError):
                    pass
            item = self.manager.describe_tool(server_id, tool_name)
            if item is None:
                return ToolExecutionResult(
                    ok=False,
                    error_code="unknown_mcp_tool",
                    public_message="未找到 MCP 工具",
                    provider_id="mcp.gateway",
                    tool_name="mcp_gateway",
                )
            return ToolExecutionResult(
                ok=True,
                data=item.model_dump(mode="json"),
                provider_id="mcp.gateway",
                tool_name="mcp_gateway",
            )
        if operation == "call":
            raw_arguments = arguments.get("arguments", {})
            if not isinstance(raw_arguments, dict):
                return ToolExecutionResult(
                    ok=False,
                    error_code="invalid_arguments",
                    public_message="MCP arguments 必须是对象",
                    provider_id="mcp.gateway",
                    tool_name="mcp_gateway",
                )
            return await MCPToolBinding(self.manager, server_id, tool_name).invoke(
                {str(key): value for key, value in raw_arguments.items()},
                context,
            )
        return ToolExecutionResult(
            ok=False,
            error_code="unknown_gateway_operation",
            public_message="未知 MCP gateway 操作",
            provider_id="mcp.gateway",
            tool_name="mcp_gateway",
        )
