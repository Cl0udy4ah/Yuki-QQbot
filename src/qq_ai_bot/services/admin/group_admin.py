"""Unified group access and autonomous-participation administration."""

from __future__ import annotations

import time

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor, ConfigChangeResult
from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.repositories import GroupSetting, GroupSettingsRepository
from qq_ai_bot.services.admin.common import require_real_superuser


class GroupAdminService:
    """Manage group admission and group-scoped runtime settings."""

    def __init__(
        self,
        *,
        settings: Settings,
        groups: GroupSettingsRepository,
        runtime_config: RuntimeConfigService,
        audit: AdminAuditService,
    ) -> None:
        self._settings = settings
        self._groups = groups
        self._runtime_config = runtime_config
        self._audit = audit

    async def enable_current_group(
        self,
        actor: AdminActor,
        group_id: str,
    ) -> GroupSetting:
        return await self._set_enabled(actor, group_id, True)

    async def disable_current_group(
        self,
        actor: AdminActor,
        group_id: str,
    ) -> GroupSetting:
        return await self._set_enabled(actor, group_id, False)

    async def set_autonomous_enabled(
        self,
        actor: AdminActor,
        group_id: str,
        enabled: bool,
    ) -> GroupSetting:
        require_real_superuser(actor, self._settings)
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            before = await self._groups.get(group_id, session=session)
            after = await self._groups.set_autonomous_enabled(
                group_id,
                enabled,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="group",
                operation="set_autonomous_enabled",
                target_type="group",
                target_id=group_id,
                before={"autonomous_enabled": before.autonomous_enabled if before else None},
                after={"autonomous_enabled": enabled},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return after

    async def set_group_config(
        self,
        actor: AdminActor,
        group_id: str,
        key: str,
        value: object,
    ) -> ConfigChangeResult:
        require_real_superuser(actor, self._settings)
        return await self._runtime_config.set_override(
            key,
            value,
            scope_type="group",
            scope_id=group_id,
            actor_user_id=actor.user_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )

    async def _set_enabled(
        self,
        actor: AdminActor,
        group_id: str,
        enabled: bool,
    ) -> GroupSetting:
        require_real_superuser(actor, self._settings)
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            before = await self._groups.get(group_id, session=session)
            after = await self._groups.set_enabled(
                group_id,
                enabled,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="group",
                operation="enable" if enabled else "disable",
                target_type="group",
                target_id=group_id,
                before={"enabled": before.enabled if before else None},
                after={"enabled": enabled},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return after
