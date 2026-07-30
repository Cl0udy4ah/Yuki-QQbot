"""Tool Kernel binding for one remote MCP tool."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.mcp.manager import MCPManager


@dataclass(frozen=True, slots=True)
class MCPToolBinding:
    manager: MCPManager
    server_id: str
    remote_tool_name: str

    async def invoke(
        self,
        arguments: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        return await self.manager.call_tool(
            self.server_id,
            self.remote_tool_name,
            arguments,
            conversation_key=context.conversation_key,
            record_invocation=False,
        )
