"""Unified explicit person-memory administration."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.repositories import MemoryRecord, MemoryRepository
from qq_ai_bot.services.admin.common import require_self_or_superuser


class MemoryAdminService:
    """Manage explicit memories and bounded automatic-memory retention rules."""

    def __init__(
        self,
        *,
        settings: Settings,
        memories: MemoryRepository,
        audit: AdminAuditService,
    ) -> None:
        self._settings = settings
        self._memories = memories
        self._audit = audit

    async def list_memories(
        self,
        actor: AdminActor,
        target: str,
    ) -> tuple[MemoryRecord, ...]:
        require_self_or_superuser(actor, target, self._settings)
        return await self._memories.list_person(
            target,
            limit=self._settings.person_memory_max_entries,
        )

    async def add_memory(
        self,
        actor: AdminActor,
        target: str,
        content: str,
    ) -> MemoryRecord:
        require_self_or_superuser(actor, target, self._settings)
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            if (
                await self._memories.count_person(target, session=session)
                >= self._settings.person_memory_max_entries
            ):
                raise ValueError("人物记忆已达到上限，请先删除或合并旧记忆")
            row = await self._memories.upsert(
                scope="person",
                user_id=target,
                memory_key=f"explicit-{uuid.uuid4()}",
                content=normalized,
                category="explicit",
                importance=5,
                source_type="explicit",
                limit=self._settings.person_memory_max_entries,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="add",
                target_type="user",
                target_id=target,
                before=None,
                after={"memory_id": row.id, "content": normalized},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return row

    async def update_memory(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
        content: str,
    ) -> bool:
        require_self_or_superuser(actor, target, self._settings)
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            before = next(
                (
                    row
                    for row in await self._memories.list_person(
                        target,
                        limit=self._settings.person_memory_max_entries,
                        session=session,
                    )
                    if row.id == memory_id
                ),
                None,
            )
            updated = await self._memories.update_explicit(
                memory_id,
                user_id=target,
                content=normalized,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="update",
                target_type="user",
                target_id=target,
                before={"memory_id": memory_id, "content": before.content if before else None},
                after={"memory_id": memory_id, "content": normalized},
                success=updated,
                error_category=None if updated else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return updated

    async def delete_memory(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
    ) -> bool:
        require_self_or_superuser(actor, target, self._settings)
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            before = next(
                (
                    row
                    for row in await self._memories.list_person(
                        target,
                        limit=self._settings.person_memory_max_entries,
                        session=session,
                    )
                    if row.id == memory_id
                ),
                None,
            )
            deleted = await self._memories.delete_person_memory(
                memory_id,
                user_id=target,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="delete",
                target_type="user",
                target_id=target,
                before={"memory_id": memory_id, "content": before.content if before else None},
                after=None,
                success=deleted,
                error_category=None if deleted else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return deleted

    async def prune_memories(
        self,
        actor: AdminActor,
        target: str,
        *,
        max_importance: int,
        older_than_days: int,
    ) -> int:
        """Atomically prune stale low-importance automatic person memories."""

        require_self_or_superuser(actor, target, self._settings)
        if not 1 <= max_importance <= 5:
            raise ValueError("max_importance 必须在 1～5")
        if not 1 <= older_than_days <= 3650:
            raise ValueError("older_than_days 必须在 1～3650")
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            deleted = await self._memories.prune_person_memories(
                user_id=target,
                max_importance=max_importance,
                older_than=cutoff,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="prune",
                target_type="user",
                target_id=target,
                before=None,
                after={
                    "max_importance": max_importance,
                    "older_than_days": older_than_days,
                    "deleted_count": deleted,
                },
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return deleted
