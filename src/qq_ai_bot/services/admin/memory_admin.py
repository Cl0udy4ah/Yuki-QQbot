"""Unified explicit person-memory administration."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import MemoryRetrievalMode, MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryIndexHealth,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.services.admin.common import require_self_or_superuser


class MemoryAdminService:
    """Manage explicit memories and bounded automatic-memory retention rules."""

    def __init__(
        self,
        *,
        settings: Settings,
        memories: MemoryFactService,
        audit: AdminAuditService,
        memory_context: MemoryContextService | None = None,
        memory_index: SQLiteMemoryFTSIndex | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._memories = memories
        self._audit = audit
        database = memories.repository.database
        self._memory_index = memory_index or SQLiteMemoryFTSIndex(database)
        self._memory_context = memory_context or MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
            retriever=MemoryRetriever(
                repository=memories.repository,
                lexical_index=self._memory_index,
            ),
            facts=memories,
        )
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )

    async def list_memories(
        self,
        actor: AdminActor,
        target: str,
    ) -> tuple[MemoryFact, ...]:
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
        *,
        evidence: MemoryEvidenceCreate | None = None,
    ) -> MemoryFact:
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
            row = await self._memories.add_explicit_person(
                target,
                normalized,
                limit=self._settings.person_memory_max_entries,
                evidence=evidence,
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
            updated_row = await self._memories.update_explicit_person(
                memory_id,
                user_id=target,
                content=normalized,
                session=session,
            )
            updated = updated_row is not None
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
            deleted = await self._memories.invalidate_person(
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

    async def list_evidence(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
    ) -> tuple[MemoryEvidence, ...]:
        require_self_or_superuser(actor, target, self._settings)
        fact = next(
            (
                row
                for row in await self._memories.list_person(
                    target,
                    limit=self._settings.person_memory_max_entries,
                )
                if row.id == memory_id
            ),
            None,
        )
        if fact is None:
            return ()
        return await self._memories.list_evidence(memory_id)

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

    async def search_person(
        self,
        actor: AdminActor,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> MemoryRetrievalResult:
        require_self_or_superuser(actor, user_id, self._settings)
        runtime = await self._runtime_config.snapshot(user_id=user_id)
        target = MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_PERSON,
            scope_type=MemoryScopeType.PERSON,
            subject_user_id=user_id,
            block_id="admin_person",
        )
        return await self._memory_context.search(
            text=query,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(target,),
            runtime=runtime,
            limit=limit,
        )

    async def search_group(
        self,
        actor: AdminActor,
        group_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> MemoryRetrievalResult:
        if not actor.is_superuser or actor.user_id not in self._settings.superusers:
            raise PermissionError("只有超级管理员可以诊断群记忆")
        runtime = await self._runtime_config.snapshot(group_id=group_id)
        target = MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_GROUP,
            scope_type=MemoryScopeType.GROUP,
            group_id=group_id,
            block_id="admin_group",
        )
        return await self._memory_context.search(
            text=query,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(target,),
            runtime=runtime,
            limit=limit,
        )

    async def index_status(self, actor: AdminActor) -> MemoryIndexHealth:
        self._require_superuser(actor)
        return await self._memory_index.health()

    async def rebuild_index(self, actor: AdminActor) -> MemoryIndexHealth:
        self._require_superuser(actor)
        started = time.perf_counter()
        health = await self._memory_index.rebuild()
        await self._audit.record(
            actor=actor,
            capability="memory",
            operation="index_rebuild",
            target_type="derived_index",
            target_id="memory_facts_fts",
            before=None,
            after=health.model_dump(),
            success=True,
            duration_seconds=time.perf_counter() - started,
        )
        return health

    def _require_superuser(self, actor: AdminActor) -> None:
        if not actor.is_superuser or actor.user_id not in self._settings.superusers:
            raise PermissionError("只有超级管理员可以诊断记忆索引")
