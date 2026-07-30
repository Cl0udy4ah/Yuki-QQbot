"""Expose cached MCP metadata as Tool Kernel descriptors."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.mcp.binding import MCPToolBinding
from qq_ai_bot.mcp.gateway import MCPGatewayBinding
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.planner.models import ToolScopeSummary


class MCPToolProvider:
    provider_id = "mcp"

    def __init__(
        self,
        manager: MCPManager,
        *,
        gateway_enabled: bool,
        selection_mode: str,
    ) -> None:
        self._manager = manager
        self._gateway_enabled = gateway_enabled
        self._selection_mode = selection_mode

    def descriptors(self, context: Any) -> tuple[CapabilityDescriptor, ...]:
        runtime = getattr(context, "runtime_config", None)
        mcp = getattr(runtime, "mcp", None)
        enabled = mcp.enabled if mcp is not None else self._manager.enabled
        self._manager.configure_runtime(
            enabled=enabled,
            metadata_cache_ttl_seconds=(mcp.metadata_cache_ttl_seconds if mcp else None),
            connect_timeout_seconds=(mcp.connect_timeout_seconds if mcp else None),
            request_timeout_seconds=(mcp.request_timeout_seconds if mcp else None),
            max_parallel_calls=(mcp.max_parallel_calls if mcp else None),
        )
        if not enabled:
            return ()
        selection_mode = mcp.tool_selection_mode if mcp is not None else self._selection_mode
        gateway_enabled = mcp.gateway_enabled if mcp is not None else self._gateway_enabled
        descriptors: list[CapabilityDescriptor] = []
        if selection_mode != "gateway":
            descriptors.extend(self._descriptor(item) for item in self._manager.cached_tools)
        if gateway_enabled and self._manager.configured_server_ids:
            descriptors.append(self._gateway_descriptor())
        return tuple(descriptors)

    async def prepare_scopes(self, scopes: tuple[str, ...], context: Any) -> None:
        """Discover only selected lazy servers before constructing Agent schemas."""

        runtime = getattr(context, "runtime_config", None)
        mcp = getattr(runtime, "mcp", None)
        enabled = mcp.enabled if mcp is not None else self._manager.enabled
        self._manager.configure_runtime(
            enabled=enabled,
            metadata_cache_ttl_seconds=(mcp.metadata_cache_ttl_seconds if mcp else None),
            connect_timeout_seconds=(mcp.connect_timeout_seconds if mcp else None),
            request_timeout_seconds=(mcp.request_timeout_seconds if mcp else None),
            max_parallel_calls=(mcp.max_parallel_calls if mcp else None),
        )
        if not enabled:
            return
        for server_id in self._manager.configured_server_ids:
            config = self._manager.server_config(server_id)
            assert config is not None
            scope = config.yuki.scope or f"mcp.{server_id}"
            if scopes and scope not in scopes and "mcp" not in scopes:
                continue
            try:
                await self._manager.ensure_metadata(server_id)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                continue

    def scope_summaries(self, runtime: Any | None = None) -> tuple[ToolScopeSummary, ...]:
        """Expose compact config metadata without connecting lazy servers."""

        mcp = getattr(runtime, "mcp", None)
        enabled = mcp.enabled if mcp is not None else self._manager.enabled
        if not enabled:
            return ()
        gateway_enabled = mcp.gateway_enabled if mcp is not None else self._gateway_enabled
        summaries: list[ToolScopeSummary] = []
        for server_id in self._manager.configured_server_ids:
            config = self._manager.server_config(server_id)
            assert config is not None
            scope = config.yuki.scope or f"mcp.{server_id}"
            summaries.append(
                ToolScopeSummary(
                    scope_id=scope,
                    parent=scope.rpartition(".")[0] or None,
                    display_name=server_id,
                    description=config.yuki.summary or f"MCP Server {server_id}",
                    tool_count=sum(
                        item.server_id == server_id for item in self._manager.cached_tools
                    ),
                    provider_ids=(f"mcp.{server_id}",),
                    tags=config.yuki.tags,
                )
            )
        if gateway_enabled and summaries:
            summaries.append(
                ToolScopeSummary(
                    scope_id="mcp",
                    parent=None,
                    display_name="MCP",
                    description="MCP 工具目录与按需调用网关",
                    tool_count=1,
                    provider_ids=("mcp.gateway",),
                    tags=("mcp",),
                )
            )
        return tuple(summaries)

    def _descriptor(self, item: Any) -> CapabilityDescriptor:
        annotations = item.annotations
        read_only = bool(annotations.get("readOnlyHint", False))
        idempotent_hint = annotations.get("idempotentHint")
        idempotent = read_only if idempotent_hint is None else bool(idempotent_hint)
        config = self._manager.server_config(item.server_id)
        assert config is not None
        scope = config.yuki.scope or f"mcp.{item.server_id}"
        return CapabilityDescriptor(
            canonical_name=f"mcp:{item.server_id}:{item.remote_tool_name}",
            model_name=item.model_name,
            group=scope,
            input_schema=item.input_schema,
            output_schema=item.output_schema or {"type": "object"},
            effect=CapabilityEffect.EXTERNAL_READ if read_only else CapabilityEffect.WRITE_STATE,
            risk=CapabilityRisk.READ if read_only else CapabilityRisk.MUTATE,
            trust_source=CapabilityTrustSource.MCP,
            allowed_origins=frozenset(TurnOrigin),
            required_permissions=frozenset(),
            uses_external_data=True,
            cancellable=True,
            idempotency=(
                CapabilityIdempotency.IDEMPOTENT
                if idempotent
                else CapabilityIdempotency.CONDITIONAL
            ),
            provider_id=f"mcp.{item.server_id}",
            provider_tool_name=item.remote_tool_name,
            description=item.description,
            compact_description=item.compact_description,
            tags=tuple(config.yuki.tags),
            binding=MCPToolBinding(self._manager, item.server_id, item.remote_tool_name),
            parallel_safe=read_only,
            result_kind="mcp_content",
            schema_version=item.metadata_hash,
        )

    def _gateway_descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            canonical_name="mcp.gateway",
            model_name="mcp_gateway",
            group="mcp",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["search", "describe", "call"]},
                    "search": {"type": "string"},
                    "describe": {"type": "string"},
                    "tool": {"type": "string"},
                    "server": {"type": "string"},
                    "query": {"type": "string"},
                    "server_id": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect=CapabilityEffect.EXTERNAL_READ,
            risk=CapabilityRisk.READ,
            trust_source=CapabilityTrustSource.MCP,
            allowed_origins=frozenset(TurnOrigin),
            required_permissions=frozenset(),
            uses_external_data=True,
            cancellable=True,
            idempotency=CapabilityIdempotency.CONDITIONAL,
            provider_id="mcp.gateway",
            provider_tool_name="mcp_gateway",
            description="搜索、描述或调用已配置 MCP Server 的工具",
            compact_description="MCP 工具目录与调用网关",
            tags=("mcp", "gateway"),
            binding=MCPGatewayBinding(self._manager),
            parallel_safe=False,
        )

    async def refresh(self, *, force: bool = False) -> None:
        for server_id in self._manager.configured_server_ids:
            try:
                await self._manager.refresh(server_id, force=force)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                continue

    async def close(self) -> None:
        await self._manager.close()
