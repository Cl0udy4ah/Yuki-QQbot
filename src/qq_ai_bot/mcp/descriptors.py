"""Translate discovered MCP metadata into unified Tool Kernel descriptors."""

from __future__ import annotations

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.models import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.models import MCPToolMetadata


def descriptor_from_mcp_tool(
    manager: MCPManager,
    item: MCPToolMetadata,
) -> CapabilityDescriptor:
    """Build one descriptor; server annotations remain descriptive, not authoritative."""

    from qq_ai_bot.mcp.binding import MCPToolBinding

    annotations = item.annotations
    read_only = bool(annotations.get("readOnlyHint", False))
    destructive = bool(annotations.get("destructiveHint", False))
    idempotent_hint = annotations.get("idempotentHint")
    idempotent = read_only if idempotent_hint is None else bool(idempotent_hint)
    finalize_after_commit = bool(annotations.get("finalizeAfterCommit", False))
    config = manager.server_config(item.server_id)
    if config is None:
        raise ValueError(f"unknown MCP server: {item.server_id}")
    base_scope = config.yuki.scope or f"mcp.{item.server_id}"
    bundles = tuple(
        bundle
        for bundle in config.yuki.tool_bundles.values()
        if item.remote_tool_name in bundle.include_tools
    )
    bundle_scopes = tuple(bundle.scope for bundle in bundles)
    return CapabilityDescriptor(
        canonical_name=f"mcp:{item.server_id}:{item.remote_tool_name}",
        model_name=item.model_name,
        group=base_scope,
        additional_scopes=bundle_scopes,
        bundle_scopes=bundle_scopes,
        scope_summaries=tuple((bundle.scope, bundle.summary) for bundle in bundles),
        input_schema=item.input_schema,
        output_schema=item.output_schema or {"type": "object"},
        effect=CapabilityEffect.EXTERNAL_READ if read_only else CapabilityEffect.WRITE_STATE,
        risk=(
            CapabilityRisk.DESTRUCTIVE
            if destructive
            else CapabilityRisk.READ
            if read_only
            else CapabilityRisk.MUTATE
        ),
        trust_source=CapabilityTrustSource.MCP,
        allowed_origins=frozenset(TurnOrigin),
        required_permissions=frozenset(),
        uses_external_data=True,
        cancellable=True,
        idempotency=(
            CapabilityIdempotency.IDEMPOTENT if idempotent else CapabilityIdempotency.CONDITIONAL
        ),
        provider_id=f"mcp.{item.server_id}",
        provider_tool_name=item.remote_tool_name,
        description=item.description,
        compact_description=item.compact_description,
        tags=tuple(
            dict.fromkeys(
                (
                    *config.yuki.tags,
                    *(
                        name
                        for name, bundle in config.yuki.tool_bundles.items()
                        if bundle in bundles
                    ),
                )
            )
        ),
        binding=MCPToolBinding(manager, item.server_id, item.remote_tool_name),
        parallel_safe=read_only,
        result_kind="mcp_content",
        schema_version=item.metadata_hash,
        provider_metadata={"mcp_annotations": dict(annotations)},
        finalize_after_commit=finalize_after_commit,
    )
