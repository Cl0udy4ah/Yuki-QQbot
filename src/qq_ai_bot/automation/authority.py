"""Explicit authority snapshots for user and superuser automations."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from qq_ai_bot.automation.models import StrictModel, TurnOrigin
from qq_ai_bot.config import Settings

if TYPE_CHECKING:
    from qq_ai_bot.automation.registry import AutomationCapabilityRegistry


class PermissionLevel(StrEnum):
    USER = "user"
    SUPERUSER = "superuser"


class DelegatedAuthority(StrictModel):
    creator_user_id: str
    bot_user_id: str
    created_from_message_id: str
    created_at: str
    permission_level: PermissionLevel
    granted_capabilities: tuple[str, ...]
    capability_schema_versions: dict[str, int]
    authority_version: int = 1
    origin: TurnOrigin = TurnOrigin.SCHEDULED_AUTOMATION
    current_group_id: str | None = None


class AuthorityContext(StrictModel):
    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    bot_user_id: str
    delegated_authority: DelegatedAuthority | None = None
    allowed_capabilities: frozenset[str] = Field(default_factory=frozenset)


def permission_for(settings: Settings, user_id: str) -> PermissionLevel:
    return PermissionLevel.SUPERUSER if user_id in settings.superusers else PermissionLevel.USER


def effective_delegated_capabilities(
    authority: DelegatedAuthority,
    *,
    settings: Settings,
    registry: AutomationCapabilityRegistry,
) -> frozenset[str]:
    """Intersect the immutable grant with current registry and creator permission."""

    current_permission = permission_for(settings, authority.creator_user_id)
    if authority.permission_level is PermissionLevel.SUPERUSER and (
        current_permission is not PermissionLevel.SUPERUSER
    ):
        return frozenset()
    allowed: set[str] = set()
    for name in authority.granted_capabilities:
        definition = registry.get(name)
        if definition is None:
            continue
        if authority.capability_schema_versions.get(name) != definition.schema_version:
            continue
        if not definition.permits(current_permission):
            continue
        if TurnOrigin.SCHEDULED_AUTOMATION not in definition.allowed_origins:
            continue
        allowed.add(name)
    return frozenset(allowed)
