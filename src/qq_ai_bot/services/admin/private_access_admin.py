"""Unified private-chat access administration."""

from __future__ import annotations

import time

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.repositories import (
    PrivateUserSetting,
    PrivateUserSettingsRepository,
)
from qq_ai_bot.services.admin.common import require_real_superuser


class PrivateAccessAdminService:
    """Block or restore private access without allowing superuser lockout."""

    def __init__(
        self,
        *,
        settings: Settings,
        private_users: PrivateUserSettingsRepository,
        audit: AdminAuditService,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._private_users = private_users
        self._audit = audit
        self._runtime_config = runtime_config

    async def enable_user(
        self,
        actor: AdminActor,
        target_user_id: str,
    ) -> PrivateUserSetting:
        return await self._set(actor, target_user_id, True)

    async def disable_user(
        self,
        actor: AdminActor,
        target_user_id: str,
    ) -> PrivateUserSetting:
        if target_user_id in self._settings.superusers:
            raise ValueError("不能关闭超级用户的私聊权限。")
        return await self._set(actor, target_user_id, False)

    async def _set(
        self,
        actor: AdminActor,
        target_user_id: str,
        enabled: bool,
    ) -> PrivateUserSetting:
        require_real_superuser(actor, self._settings)
        started = time.perf_counter()
        initial_affection: int | None = None
        initial_trust: int | None = None
        if self._runtime_config is not None:
            runtime = await self._runtime_config.snapshot(user_id=target_user_id)
            initial_affection = runtime.relationship.initial_affection
            initial_trust = runtime.relationship.initial_trust
        async with self._audit.transaction() as session:
            before = await self._private_users.get(target_user_id, session=session)
            after = await self._private_users.set_enabled(
                target_user_id,
                enabled,
                initial_affection=initial_affection,
                initial_trust=initial_trust,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="private_access",
                operation="enable" if enabled else "disable",
                target_type="user",
                target_id=target_user_id,
                before={"enabled": before.enabled if before else None},
                after={"enabled": enabled},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return after
