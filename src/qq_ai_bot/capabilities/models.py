"""Unified capability metadata shared by chat, automation, admin, and plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import ChatTool

if TYPE_CHECKING:
    from qq_ai_bot.capabilities.binding import ToolBinding


class CapabilityEffect(StrEnum):
    READ_STATE = "read_state"
    WRITE_STATE = "write_state"
    EXTERNAL_READ = "external_read"
    PLATFORM_SEND = "platform_send"
    PLATFORM_MUTATE = "platform_mutate"
    REPLY_EFFECT = "reply_effect"


class CapabilityRisk(StrEnum):
    READ = "read"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


class CapabilityTrustSource(StrEnum):
    CORE = "core"
    ADMIN = "admin"
    AUTOMATION = "automation"
    PLUGIN = "plugin"
    MCP = "mcp"


class CapabilityIdempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    CONDITIONAL = "conditional"
    NON_IDEMPOTENT = "non_idempotent"


class CapabilityExposure(StrEnum):
    """How a capability enters a model turn after backend policy checks."""

    PLANNED = "planned"
    DIRECT_ALWAYS = "direct_always"


CapabilityHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    canonical_name: str
    model_name: str
    group: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    effect: CapabilityEffect
    risk: CapabilityRisk
    trust_source: CapabilityTrustSource
    allowed_origins: frozenset[TurnOrigin]
    required_permissions: frozenset[str]
    uses_external_data: bool
    cancellable: bool
    idempotency: CapabilityIdempotency
    handler: CapabilityHandler | None = None
    provider_id: str = ""
    provider_tool_name: str = ""
    description: str = ""
    compact_description: str = ""
    tags: tuple[str, ...] = ()
    binding: ToolBinding | None = None
    parallel_safe: bool = False
    result_kind: str = "json"
    schema_version: str = "1"
    exposure: CapabilityExposure = CapabilityExposure.PLANNED
    additional_scopes: tuple[str, ...] = ()
    bundle_scopes: tuple[str, ...] = ()
    scope_summaries: tuple[tuple[str, str], ...] = ()
    provider_metadata: dict[str, Any] | None = None
    finalize_after_commit: bool = False

    @property
    def scope_id(self) -> str:
        """Return the dynamic Planner scope used by this capability."""

        return self.group

    @property
    def scope_ids(self) -> tuple[str, ...]:
        """Return every Planner scope without duplicates."""

        return tuple(dict.fromkeys((self.group, *self.additional_scopes)))

    def as_chat_tool(self, description: str | None = None) -> ChatTool:
        return ChatTool(
            name=self.model_name,
            description=description if description is not None else self.description,
            parameters=self.input_schema,
        )


@dataclass(frozen=True, slots=True)
class AuthorityContext:
    actor_user_id: str
    is_superuser: bool
    permissions: frozenset[str] = frozenset()
