"""Metadata-driven capability visibility policy."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityRisk,
)
from qq_ai_bot.planner.models import ToolMode, ToolSelection


@dataclass(frozen=True, slots=True)
class CapabilityPolicyContext:
    authority: AuthorityContext
    origin: TurnOrigin
    tool_selection: ToolSelection
    contains_images: bool = False
    web_was_used: bool = False
    conversation_open: bool = True


class CapabilityPolicyEngine:
    """Intersect backend descriptors with Planner and current-turn policy."""

    def visible(
        self,
        descriptors: tuple[CapabilityDescriptor, ...],
        context: CapabilityPolicyContext,
    ) -> tuple[CapabilityDescriptor, ...]:
        if not context.conversation_open or context.tool_selection.mode is ToolMode.NONE:
            return ()
        selected_groups = {group.value for group in context.tool_selection.groups}
        granted = set(context.authority.permissions)
        if context.authority.is_superuser:
            granted.add("superuser")
        visible: list[CapabilityDescriptor] = []
        for descriptor in descriptors:
            if descriptor.group not in selected_groups:
                continue
            if context.origin not in descriptor.allowed_origins:
                continue
            if not descriptor.required_permissions.issubset(granted):
                continue
            if context.tool_selection.mode is ToolMode.READ_ONLY and descriptor.effect not in {
                CapabilityEffect.READ_STATE,
                CapabilityEffect.EXTERNAL_READ,
            }:
                continue
            if (context.contains_images or context.web_was_used) and (
                descriptor.effect
                in {CapabilityEffect.WRITE_STATE, CapabilityEffect.PLATFORM_MUTATE}
                or descriptor.risk is CapabilityRisk.DESTRUCTIVE
            ):
                continue
            visible.append(descriptor)
        return tuple(visible)
