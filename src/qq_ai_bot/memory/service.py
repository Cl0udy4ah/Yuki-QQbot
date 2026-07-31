"""Transactional Memory V2 fact lifecycle rules."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.enums import (
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import (
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactQuery,
)
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.validation import normalize_memory_text


class MemoryFactService:
    """Apply deduplication, versioning, explicit protection, and evidence atomically."""

    def __init__(self, repository: MemoryFactRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> MemoryFactRepository:
        return self._repository

    async def remember(
        self,
        fact: MemoryFactCreate,
        *,
        evidence: MemoryEvidenceCreate | None = None,
        limit: int | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.remember(
                    fact,
                    evidence=evidence,
                    limit=limit,
                    session=owned,
                )
        content = normalize_memory_text(fact.content, maximum=4000)
        key = normalize_memory_text(fact.memory_key, maximum=128)
        category = normalize_memory_text(fact.category, maximum=64)
        if not content or not key or not category:
            raise ValueError("memory fact content, key, and category cannot be empty")
        normalized = content.casefold()
        prepared = fact.model_copy(
            update={"content": content, "memory_key": key, "category": category}
        )
        existing = await self._repository.find_active(prepared, session=session)
        if existing is not None and existing.normalized_content == normalized:
            await self._repository.refresh_fact(
                existing.id,
                importance=prepared.importance,
                confidence=prepared.confidence,
                session=session,
            )
            if evidence is not None:
                await self._repository.add_evidence(existing.id, evidence, session=session)
            result = await self._repository.get_fact(existing.id, session=session)
            assert result is not None
            return result
        if (
            existing is not None
            and existing.source_type == MemorySourceType.EXPLICIT.value
            and prepared.source_type is not MemorySourceType.EXPLICIT
        ):
            result = await self._repository.get_fact(existing.id, session=session)
            assert result is not None
            return result
        if existing is None and limit is not None:
            query = MemoryFactQuery(
                scope_type=prepared.scope_type,
                subject_user_id=prepared.subject_user_id,
                group_id=prepared.group_id,
            )
            if not await self._repository.make_room(query, limit=limit, session=session):
                raise ValueError("memory capacity is occupied by explicit facts")
        supersedes_id = existing.id if existing is not None else None
        if existing is not None:
            await self._repository.set_status(
                existing.id,
                MemoryStatus.SUPERSEDED,
                session=session,
            )
        created = await self._repository.create_fact(
            prepared,
            normalized_content=normalized,
            supersedes_id=supersedes_id,
            session=session,
        )
        if evidence is not None:
            await self._repository.add_evidence(created.id, evidence, session=session)
        result = await self._repository.get_fact(created.id, session=session)
        assert result is not None
        return result

    async def list_person(
        self,
        user_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(scope_type=MemoryScopeType.PERSON, subject_user_id=user_id),
            limit=limit,
            session=session,
        )

    async def list_group(
        self,
        group_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(scope_type=MemoryScopeType.GROUP, group_id=group_id),
            limit=limit,
            session=session,
        )

    async def list_person_group(
        self,
        user_id: str,
        group_id: str,
        *,
        limit: int = 50,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON_GROUP,
                subject_user_id=user_id,
                group_id=group_id,
            ),
            limit=limit,
            session=session,
        )

    async def list_preferences(
        self,
        user_id: str,
        *,
        limit: int = 30,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        return await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.PREFERENCE,
            ),
            limit=limit,
            session=session,
        )

    async def count_person(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        return await self._repository.count_active(
            MemoryFactQuery(scope_type=MemoryScopeType.PERSON, subject_user_id=user_id),
            session=session,
        )

    async def add_explicit_person(
        self,
        user_id: str,
        content: str,
        *,
        limit: int,
        memory_key: str | None = None,
        evidence: MemoryEvidenceCreate | None = None,
        session: AsyncSession | None = None,
    ) -> MemoryFact:
        return await self.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.FACT,
                memory_key=memory_key or f"explicit-{uuid.uuid4()}",
                category="explicit",
                content=content,
                importance=5,
                confidence=1,
                source_type=MemorySourceType.EXPLICIT,
            ),
            evidence=evidence,
            limit=limit,
            session=session,
        )

    async def update_explicit_person(
        self,
        fact_id: int,
        *,
        user_id: str,
        content: str,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.update_explicit_person(
                    fact_id,
                    user_id=user_id,
                    content=content,
                    session=owned,
                )
        current = await self._repository.get_fact(fact_id, session=session)
        if (
            current is None
            or current.status is not MemoryStatus.ACTIVE
            or current.scope_type is not MemoryScopeType.PERSON
            or current.subject_user_id != user_id
        ):
            return None
        return await self.remember(
            MemoryFactCreate(
                scope_type=current.scope_type,
                subject_user_id=user_id,
                kind=current.kind,
                memory_key=current.memory_key,
                category=current.category,
                content=content,
                importance=current.importance,
                confidence=1,
                source_type=MemorySourceType.EXPLICIT,
                valid_from=current.valid_from,
                valid_until=current.valid_until,
            ),
            session=session,
        )

    async def invalidate_person(
        self,
        fact_id: int,
        *,
        user_id: str,
        session: AsyncSession | None = None,
    ) -> bool:
        return await self._repository.invalidate(
            fact_id,
            subject_user_id=user_id,
            session=session,
        )

    async def set_preference(
        self,
        user_id: str,
        key: str,
        value: str,
        *,
        limit: int = 30,
        source_type: str = "explicit",
        session: AsyncSession | None = None,
    ) -> MemoryFact:
        return await self.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.PREFERENCE,
                memory_key=key,
                category="preference",
                content=value,
                importance=4,
                confidence=1,
                source_type=MemorySourceType(source_type),
            ),
            limit=limit,
            session=session,
        )

    async def delete_preference(
        self,
        user_id: str,
        key: str,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._repository.transaction() as owned:
                return await self.delete_preference(user_id, key, session=owned)
        rows = await self._repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                kind=MemoryKind.PREFERENCE,
            ),
            limit=100_000,
            session=session,
        )
        row = next((item for item in rows if item.memory_key == key), None)
        return bool(
            row
            and await self._repository.invalidate(
                row.id,
                subject_user_id=user_id,
                session=session,
            )
        )

    async def prune_person_memories(
        self,
        *,
        user_id: str,
        max_importance: int,
        older_than: datetime,
        session: AsyncSession,
    ) -> int:
        return await self._repository.prune_person(
            user_id=user_id,
            max_importance=max_importance,
            older_than=older_than,
            session=session,
        )

    async def list_evidence(self, fact_id: int, *, limit: int = 100) -> tuple[MemoryEvidence, ...]:
        return await self._repository.list_evidence(fact_id, limit=limit)

    async def mark_used(self, fact_ids: tuple[int, ...]) -> int:
        """Mark only facts that survived final context budgeting."""

        return await self._repository.mark_used(fact_ids)
