"""Unified person-preference administration."""

from __future__ import annotations

import time

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.models import MemoryFact
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.services.admin.common import require_self_or_superuser


class PreferenceAdminService:
    """Manage a person's bounded interaction preferences."""

    def __init__(
        self,
        *,
        settings: Settings,
        memories: MemoryFactService,
        audit: AdminAuditService,
    ) -> None:
        self._settings = settings
        self._memories = memories
        self._audit = audit

    async def list_preferences(
        self,
        actor: AdminActor,
        target: str,
    ) -> tuple[MemoryFact, ...]:
        require_self_or_superuser(actor, target, self._settings)
        return await self._memories.list_preferences(
            target,
            limit=self._settings.preference_max_entries,
        )

    async def set_preference(
        self,
        actor: AdminActor,
        target: str,
        key: str,
        value: str,
    ) -> MemoryFact:
        require_self_or_superuser(actor, target, self._settings)
        normalized_key = key.strip()
        normalized_value = " ".join(value.split()).strip()
        if not normalized_key or not normalized_value:
            raise ValueError("偏好键和值不能为空")
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            existing = {
                row.key: row
                for row in await self._memories.list_preferences(
                    target,
                    limit=self._settings.preference_max_entries,
                    session=session,
                )
            }.get(normalized_key)
            row = await self._memories.set_preference(
                target,
                normalized_key,
                normalized_value,
                limit=self._settings.preference_max_entries,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="preference",
                operation="set",
                target_type="user",
                target_id=target,
                before={
                    "key": normalized_key,
                    "preference_value": existing.value if existing else None,
                },
                after={"key": normalized_key, "preference_value": normalized_value},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return row

    async def delete_preference(
        self,
        actor: AdminActor,
        target: str,
        key: str,
    ) -> bool:
        require_self_or_superuser(actor, target, self._settings)
        normalized_key = key.strip()
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            existing = {
                row.key: row
                for row in await self._memories.list_preferences(
                    target,
                    limit=self._settings.preference_max_entries,
                    session=session,
                )
            }.get(normalized_key)
            deleted = await self._memories.delete_preference(
                target,
                normalized_key,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="preference",
                operation="delete",
                target_type="user",
                target_id=target,
                before={
                    "key": normalized_key,
                    "preference_value": existing.value if existing else None,
                },
                after=None,
                success=deleted,
                error_category=None if deleted else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return deleted
