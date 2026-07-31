"""Deterministic `/ai mcp` management and diagnostics."""

from __future__ import annotations

import json

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.results import ToolArtifactWriter, ToolResultBudgeter
from qq_ai_bot.mcp.binding import MCPPolicyRuntime, MCPToolBinding
from qq_ai_bot.mcp.errors import classify_mcp_exception
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPHealthSnapshot

_MUTATING = frozenset({"refresh", "reconnect", "enable", "disable", "doctor", "call"})


class MCPCommandHandler:
    def __init__(
        self,
        manager: MCPManager,
        *,
        result_max_characters: int = 8000,
        artifacts: ToolArtifactWriter | None = None,
        artifact_retention_seconds: int | None = None,
    ) -> None:
        if result_max_characters <= 0:
            raise ValueError("MCP command result budget must be positive")
        self._manager = manager
        self._result_max = result_max_characters
        self._artifacts = artifacts
        self._artifact_retention_seconds = artifact_retention_seconds

    def health(self) -> MCPHealthSnapshot:
        return self._manager.health()

    async def execute(self, argument: str, *, is_superuser: bool) -> str:
        parts = argument.strip().split(maxsplit=3)
        operation = parts[0].casefold() if parts else "list"
        if operation in _MUTATING and not is_superuser:
            return "权限不足：该 MCP 命令仅限超级管理员"
        if operation == "list":
            statuses = await self._manager.statuses()
            if not statuses:
                return "当前没有配置 MCP Server"
            return "\n".join(
                f"- {item.server_id}: {item.status}，工具 {item.configured_tools} 个"
                for item in statuses
            )
        if len(parts) < 2 and operation not in {"search"}:
            return "格式：/ai mcp <操作> <server_id>"
        if operation == "search":
            query = argument.partition(" ")[2].strip()
            tools = self._manager.search_tools(query)
            return (
                "\n".join(
                    f"- {item.server_id}/{item.remote_tool_name}: {item.compact_description}"
                    for item in tools
                )
                or "没有匹配的 MCP 工具"
            )
        server_id = parts[1]
        try:
            if operation == "show":
                return json.dumps(
                    self._manager.display_config(server_id),
                    ensure_ascii=False,
                    indent=2,
                )
            if operation == "status":
                status = await self._manager.status(server_id)
                return json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2)
            if operation == "tools":
                tools = self._manager.search_tools("", server_id=server_id)
                return (
                    "\n".join(
                        f"- {item.remote_tool_name}: {item.compact_description}" for item in tools
                    )
                    or "该 Server 尚无缓存工具；可由超级管理员执行 refresh"
                )
            if operation == "refresh":
                tools = await self._manager.refresh(server_id)
                return f"已刷新 {server_id}：{len(tools)} 个工具"
            if operation == "reconnect":
                tools = await self._manager.reconnect(server_id)
                return f"已重连 {server_id}：{len(tools)} 个工具"
            if operation in {"enable", "disable"}:
                await self._manager.set_enabled(server_id, operation == "enable")
                label = "启用" if operation == "enable" else "停用"
                return f"已{label} {server_id}"
            if operation == "doctor":
                tools = await self._manager.reconnect(server_id)
                status = await self._manager.status(server_id)
                return (
                    f"MCP 诊断通过：{server_id}，协议 {status.protocol_version or '未知'}，"
                    f"工具 {len(tools)} 个"
                )
            if operation == "call":
                if len(parts) < 4:
                    return "格式：/ai mcp call <server_id> <tool_name> <JSON>"
                tool_name = parts[2]
                raw = json.loads(parts[3])
                if not isinstance(raw, dict):
                    return "MCP 调用参数必须是 JSON 对象"
                runtime = MCPPolicyRuntime(
                    origin=TurnOrigin.USER_MESSAGE,
                    actor_user_id="deterministic-superuser",
                    actor_is_superuser=True,
                )
                result = await MCPToolBinding(
                    self._manager,
                    server_id,
                    tool_name,
                    record_invocation=True,
                ).invoke(
                    {str(key): value for key, value in raw.items()},
                    ToolInvocationContext(
                        runtime=runtime,
                        conversation_key="deterministic-command",
                        actor_user_id=runtime.actor_user_id,
                    ),
                )
                rendered = await ToolResultBudgeter(
                    max_characters=self._result_max,
                    artifacts=self._artifacts,
                    artifact_retention_seconds=self._artifact_retention_seconds,
                ).render(result)
                return rendered.text
        except json.JSONDecodeError:
            return "MCP 调用参数不是有效 JSON"
        except Exception as exc:
            failure = classify_mcp_exception(exc)
            return f"MCP 操作失败：{failure.public_message}"
        return "未知 MCP 操作"
