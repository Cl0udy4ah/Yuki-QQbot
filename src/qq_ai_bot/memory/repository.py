"""Persistence-only repositories for Memory V2 facts, evidence, and jobs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.enums import MemoryJobStatus, MemoryStatus
from qq_ai_bot.memory.models import (
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactQuery,
    MemoryJob,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MembershipModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryJobModel,
    PersonModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_group,
    _ensure_person,
    _event_record,
)


class MemoryFactRepository:
    """Store and query facts without extraction or prompt logic."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._database.sessions() as session, session.begin():
            yield session

    async def list_facts(
        self,
        query: MemoryFactQuery,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryFact, ...]:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.list_facts(query, limit=limit, session=owned)
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == query.status.value,
        ]
        if query.subject_user_id is None:
            conditions.append(MemoryFactModel.subject_user_id.is_(None))
        else:
            conditions.append(MemoryFactModel.subject_user_id == query.subject_user_id)
        if query.group_id is None:
            conditions.append(MemoryFactModel.group_id.is_(None))
        else:
            conditions.append(MemoryFactModel.group_id == query.group_id)
        if query.kind is not None:
            conditions.append(MemoryFactModel.kind == query.kind.value)
        statement = (
            select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
            .outerjoin(MemoryEvidenceModel, MemoryEvidenceModel.fact_id == MemoryFactModel.id)
            .where(*conditions)
            .group_by(MemoryFactModel.id)
            .order_by(MemoryFactModel.importance.desc(), MemoryFactModel.updated_at.desc())
            .limit(max(1, limit))
        )
        rows = (await session.execute(statement)).all()
        return tuple(self._project_fact(row, int(evidence_count)) for row, evidence_count in rows)

    async def get_fact(
        self,
        fact_id: int,
        *,
        session: AsyncSession | None = None,
    ) -> MemoryFact | None:
        if session is None:
            async with self._database.sessions() as owned:
                return await self.get_fact(fact_id, session=owned)
        result = (
            await session.execute(
                select(MemoryFactModel, func.count(MemoryEvidenceModel.id))
                .outerjoin(
                    MemoryEvidenceModel,
                    MemoryEvidenceModel.fact_id == MemoryFactModel.id,
                )
                .where(MemoryFactModel.id == fact_id)
                .group_by(MemoryFactModel.id)
            )
        ).first()
        return self._project_fact(result[0], int(result[1])) if result else None

    async def find_active(
        self,
        fact: MemoryFactCreate,
        *,
        session: AsyncSession,
    ) -> MemoryFactModel | None:
        return cast(
            MemoryFactModel | None,
            await session.scalar(
                select(MemoryFactModel).where(
                    MemoryFactModel.scope_type == fact.scope_type.value,
                    MemoryFactModel.subject_user_id == fact.subject_user_id,
                    MemoryFactModel.group_id == fact.group_id,
                    MemoryFactModel.kind == fact.kind.value,
                    MemoryFactModel.memory_key == fact.memory_key,
                    MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                )
            ),
        )

    async def create_fact(
        self,
        fact: MemoryFactCreate,
        *,
        normalized_content: str,
        supersedes_id: int | None,
        session: AsyncSession,
    ) -> MemoryFactModel:
        now = datetime.now(UTC)
        if fact.subject_user_id:
            await _ensure_person(session, fact.subject_user_id, now=now)
        if fact.group_id:
            await _ensure_group(session, fact.group_id, now=now)
        if fact.subject_user_id and fact.group_id:
            membership = await session.get(
                MembershipModel,
                {"user_id": fact.subject_user_id, "group_id": fact.group_id},
            )
            if membership is None:
                session.add(
                    MembershipModel(
                        user_id=fact.subject_user_id,
                        group_id=fact.group_id,
                        group_card="",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
        row = MemoryFactModel(
            scope_type=fact.scope_type.value,
            subject_user_id=fact.subject_user_id,
            group_id=fact.group_id,
            kind=fact.kind.value,
            memory_key=fact.memory_key,
            category=fact.category,
            content=fact.content,
            normalized_content=normalized_content,
            importance=fact.importance,
            confidence=fact.confidence,
            source_type=fact.source_type.value,
            status=MemoryStatus.ACTIVE.value,
            supersedes_id=supersedes_id,
            valid_from=fact.valid_from,
            valid_until=fact.valid_until,
            created_at=now,
            updated_at=now,
            last_used_at=None,
        )
        session.add(row)
        await session.flush()
        return row

    async def set_status(
        self,
        fact_id: int,
        status: MemoryStatus,
        *,
        session: AsyncSession,
    ) -> None:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(status=status.value, updated_at=datetime.now(UTC))
        )

    async def refresh_fact(
        self,
        fact_id: int,
        *,
        importance: int,
        confidence: float,
        session: AsyncSession,
    ) -> None:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(
                importance=func.max(MemoryFactModel.importance, importance),
                confidence=func.max(MemoryFactModel.confidence, confidence),
                updated_at=datetime.now(UTC),
            )
        )

    async def add_evidence(
        self,
        fact_id: int,
        evidence: MemoryEvidenceCreate,
        *,
        session: AsyncSession,
    ) -> None:
        statement = insert(MemoryEvidenceModel).values(
            fact_id=fact_id,
            event_id=evidence.event_id,
            source_speaker_user_id=evidence.source_speaker_user_id,
            relation=evidence.relation.value,
            excerpt=evidence.excerpt[:500],
            created_at=datetime.now(UTC),
        )
        await session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[MemoryEvidenceModel.fact_id, MemoryEvidenceModel.event_id]
            )
        )

    async def list_evidence(self, fact_id: int, *, limit: int = 100) -> tuple[MemoryEvidence, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.fact_id == fact_id)
                    .order_by(MemoryEvidenceModel.created_at.desc())
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(
            MemoryEvidence(
                id=row.id,
                fact_id=row.fact_id,
                event_id=row.event_id,
                source_speaker_user_id=row.source_speaker_user_id,
                relation=row.relation,
                excerpt=row.excerpt,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def count_active(
        self,
        query: MemoryFactQuery,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        return len(await self.list_facts(query, limit=100_000, session=session))

    async def make_room(
        self,
        query: MemoryFactQuery,
        *,
        limit: int,
        session: AsyncSession,
    ) -> bool:
        """Invalidate the least useful automatic fact when a scope is full."""

        if await self.count_active(query, session=session) < max(1, limit):
            return True
        conditions = [
            MemoryFactModel.scope_type == query.scope_type.value,
            MemoryFactModel.status == MemoryStatus.ACTIVE.value,
            MemoryFactModel.source_type != "explicit",
        ]
        conditions.append(
            MemoryFactModel.subject_user_id.is_(None)
            if query.subject_user_id is None
            else MemoryFactModel.subject_user_id == query.subject_user_id
        )
        conditions.append(
            MemoryFactModel.group_id.is_(None)
            if query.group_id is None
            else MemoryFactModel.group_id == query.group_id
        )
        row = await session.scalar(
            select(MemoryFactModel)
            .where(*conditions)
            .order_by(MemoryFactModel.importance.asc(), MemoryFactModel.updated_at.asc())
            .limit(1)
        )
        if row is None:
            return False
        row.status = MemoryStatus.INVALIDATED.value
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return True

    async def invalidate(
        self,
        fact_id: int,
        *,
        subject_user_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self.transaction() as owned:
                return await self.invalidate(
                    fact_id,
                    subject_user_id=subject_user_id,
                    session=owned,
                )
        conditions = [
            MemoryFactModel.id == fact_id,
            MemoryFactModel.status == MemoryStatus.ACTIVE.value,
        ]
        if subject_user_id is not None:
            conditions.append(MemoryFactModel.subject_user_id == subject_user_id)
        result = await session.execute(
            update(MemoryFactModel)
            .where(*conditions)
            .values(status=MemoryStatus.INVALIDATED.value, updated_at=datetime.now(UTC))
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def prune_person(
        self,
        *,
        user_id: str,
        max_importance: int,
        older_than: datetime,
        session: AsyncSession,
    ) -> int:
        result = await session.execute(
            update(MemoryFactModel)
            .where(
                MemoryFactModel.scope_type == "person",
                MemoryFactModel.subject_user_id == user_id,
                MemoryFactModel.status == MemoryStatus.ACTIVE.value,
                MemoryFactModel.source_type != "explicit",
                MemoryFactModel.importance <= max_importance,
                MemoryFactModel.updated_at < older_than,
            )
            .values(status=MemoryStatus.INVALIDATED.value, updated_at=datetime.now(UTC))
        )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def delete_orphaned_automatic_facts(
        self,
        *,
        event_ids: tuple[int, ...],
        exact_text: str,
        session: AsyncSession,
    ) -> None:
        evidence_fact_ids = select(MemoryEvidenceModel.fact_id).where(
            MemoryEvidenceModel.event_id.in_(event_ids)
        )
        await session.execute(
            delete(MemoryFactModel).where(
                MemoryFactModel.source_type != "explicit",
                or_(
                    MemoryFactModel.content.contains(exact_text),
                    MemoryFactModel.id.in_(evidence_fact_ids),
                ),
            )
        )

    @staticmethod
    def _project_fact(row: MemoryFactModel, evidence_count: int = 0) -> MemoryFact:
        return MemoryFact(
            id=row.id,
            scope_type=row.scope_type,
            subject_user_id=row.subject_user_id,
            group_id=row.group_id,
            kind=row.kind,
            memory_key=row.memory_key,
            category=row.category,
            content=row.content,
            normalized_content=row.normalized_content,
            importance=row.importance,
            confidence=row.confidence,
            source_type=row.source_type,
            status=row.status,
            supersedes_id=row.supersedes_id,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_used_at=row.last_used_at,
            evidence_count=evidence_count,
        )


class MemoryJobRepository:
    """Durable one-event extraction queue with bounded retries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def enqueue(self, event_id: int, conversation_key: str) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            event = await session.get(ChatEventModel, event_id)
            if event is None or event.direction != "inbound":
                return False
            sender = await session.get(PersonModel, event.sender_user_id)
            if sender is None or sender.is_bot:
                return False
            statement = insert(MemoryJobModel).values(
                event_id=event_id,
                conversation_key=conversation_key[:255],
                status=MemoryJobStatus.PENDING.value,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                error_category=None,
            )
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=[MemoryJobModel.event_id])
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(
                    MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                    MemoryJobModel.next_attempt_at <= datetime.now(UTC),
                )
            )
        return int(value or 0)

    async def claim(self, *, limit: int = 20) -> tuple[MemoryJob, ...]:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=5)
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(MemoryJobModel)
                    .where(
                        or_(
                            MemoryJobModel.status == MemoryJobStatus.PENDING.value,
                            (
                                (MemoryJobModel.status == MemoryJobStatus.PROCESSING.value)
                                & (MemoryJobModel.updated_at <= stale)
                            ),
                        ),
                        MemoryJobModel.next_attempt_at <= now,
                    )
                    .order_by(MemoryJobModel.id)
                    .limit(max(1, limit))
                )
            ).all()
            jobs: list[MemoryJob] = []
            for row in rows:
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    await session.delete(row)
                    continue
                row.status = MemoryJobStatus.PROCESSING.value
                row.updated_at = now
                jobs.append(
                    MemoryJob(
                        id=row.id,
                        event_id=row.event_id,
                        conversation_key=row.conversation_key,
                        status=row.status,
                        attempts=row.attempts,
                        next_attempt_at=row.next_attempt_at,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        error_category=row.error_category,
                        event=_event_record(event),
                    )
                )
            return tuple(jobs)

    async def complete(self, job_id: int) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryJobModel)
                .where(MemoryJobModel.id == job_id)
                .values(
                    status=MemoryJobStatus.DONE.value,
                    updated_at=datetime.now(UTC),
                    error_category=None,
                )
            )

    async def fail(self, job_id: int, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = (
                MemoryJobStatus.FAILED.value if row.attempts >= 3 else MemoryJobStatus.PENDING.value
            )
            row.next_attempt_at = now + timedelta(seconds=30 * row.attempts)
            row.updated_at = now
            row.error_category = error_category[:64]
