"""Repositories for structured memories, preferences, and memory jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    GroupMemoryModel,
    MembershipModel,
    MemoryJobModel,
    PersonGroupMemoryModel,
    PersonMemoryModel,
    PersonPreferenceModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_group,
    _ensure_person,
    _event_record,
)
from qq_ai_bot.persistence.repository_records import (
    MemoryJobRecord,
    MemoryRecord,
    PreferenceRecord,
)

MemoryScope = Literal["person", "group", "person_group"]


class MemoryRepository:
    """Read and mutate bounded structured memories and preferences."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_person(
        self,
        user_id: str,
        *,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> tuple[MemoryRecord, ...]:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.list_person(
                    user_id,
                    limit=limit,
                    session=owned_session,
                )
        rows = (
            await session.scalars(
                select(PersonMemoryModel)
                .where(PersonMemoryModel.user_id == user_id)
                .order_by(
                    PersonMemoryModel.importance.desc(),
                    PersonMemoryModel.updated_at.desc(),
                )
                .limit(limit)
            )
        ).all()
        return tuple(self._project(row, "person") for row in rows)

    async def list_people(
        self,
        user_ids: tuple[str, ...],
        *,
        limit_per_user: int = 20,
    ) -> dict[str, tuple[MemoryRecord, ...]]:
        """Load bounded person memories for several QQ identities in one query."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        limit = max(1, limit_per_user)
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PersonMemoryModel)
                    .where(PersonMemoryModel.user_id.in_(unique_ids))
                    .order_by(
                        PersonMemoryModel.user_id,
                        PersonMemoryModel.importance.desc(),
                        PersonMemoryModel.updated_at.desc(),
                    )
                )
            ).all()
        grouped: dict[str, list[MemoryRecord]] = {user_id: [] for user_id in unique_ids}
        for row in rows:
            bucket = grouped[row.user_id]
            if len(bucket) < limit:
                bucket.append(self._project(row, "person"))
        return {user_id: tuple(records) for user_id, records in grouped.items()}

    async def list_group(self, group_id: str, *, limit: int = 100) -> tuple[MemoryRecord, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(GroupMemoryModel)
                    .where(GroupMemoryModel.group_id == group_id)
                    .order_by(
                        GroupMemoryModel.importance.desc(),
                        GroupMemoryModel.updated_at.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(self._project(row, "group") for row in rows)

    async def list_person_group(
        self, user_id: str, group_id: str, *, limit: int = 50
    ) -> tuple[MemoryRecord, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PersonGroupMemoryModel)
                    .where(
                        PersonGroupMemoryModel.user_id == user_id,
                        PersonGroupMemoryModel.group_id == group_id,
                    )
                    .order_by(
                        PersonGroupMemoryModel.importance.desc(),
                        PersonGroupMemoryModel.updated_at.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(self._project(row, "person_group") for row in rows)

    async def list_people_group(
        self,
        user_ids: tuple[str, ...],
        group_id: str,
        *,
        limit_per_user: int = 20,
    ) -> dict[str, tuple[MemoryRecord, ...]]:
        """Load one group's member memories for several people in one query."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        limit = max(1, limit_per_user)
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PersonGroupMemoryModel)
                    .where(
                        PersonGroupMemoryModel.user_id.in_(unique_ids),
                        PersonGroupMemoryModel.group_id == group_id,
                    )
                    .order_by(
                        PersonGroupMemoryModel.user_id,
                        PersonGroupMemoryModel.importance.desc(),
                        PersonGroupMemoryModel.updated_at.desc(),
                    )
                )
            ).all()
        grouped: dict[str, list[MemoryRecord]] = {user_id: [] for user_id in unique_ids}
        for row in rows:
            bucket = grouped[row.user_id]
            if len(bucket) < limit:
                bucket.append(self._project(row, "person_group"))
        return {user_id: tuple(records) for user_id, records in grouped.items()}

    @staticmethod
    def _project(row: Any, scope: MemoryScope) -> MemoryRecord:
        return MemoryRecord(
            id=row.id,
            memory_key=row.memory_key,
            category=row.category,
            content=row.content,
            importance=row.importance,
            source_type=row.source_type,
            updated_at=row.updated_at,
            user_id=getattr(row, "user_id", None),
            group_id=getattr(row, "group_id", None),
            subject_user_id=getattr(row, "subject_user_id", None),
        )

    async def upsert(
        self,
        *,
        scope: MemoryScope,
        memory_key: str,
        content: str,
        category: str = "fact",
        importance: int = 3,
        source_type: str = "automatic",
        source_event_id: int | None = None,
        user_id: str | None = None,
        group_id: str | None = None,
        subject_user_id: str | None = None,
        limit: int,
        session: AsyncSession | None = None,
    ) -> MemoryRecord:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.upsert(
                    scope=scope,
                    memory_key=memory_key,
                    content=content,
                    category=category,
                    importance=importance,
                    source_type=source_type,
                    source_event_id=source_event_id,
                    user_id=user_id,
                    group_id=group_id,
                    subject_user_id=subject_user_id,
                    limit=limit,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        model, filters, values = await self._scope_values(
            session,
            scope=scope,
            user_id=user_id,
            group_id=group_id,
            now=now,
        )
        existing = await session.scalar(
            select(model).where(*filters, model.memory_key == memory_key)
        )
        if existing is None:
            count = int(
                await session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
            )
            if count >= limit:
                oldest_automatic = await session.scalar(
                    select(model)
                    .where(*filters, model.source_type != "explicit")
                    .order_by(model.importance.asc(), model.updated_at.asc())
                    .limit(1)
                )
                if oldest_automatic is None:
                    raise ValueError("memory capacity is occupied by explicit memories")
                await session.delete(oldest_automatic)
            existing = model(
                **values,
                memory_key=memory_key,
                category=category,
                content=content[:4000],
                importance=max(1, min(5, importance)),
                source_type=source_type,
                source_event_id=source_event_id,
                created_at=now,
                updated_at=now,
            )
            if scope == "group":
                existing.subject_user_id = subject_user_id
            session.add(existing)
            await session.flush()
        elif not (existing.source_type == "explicit" and source_type != "explicit"):
            existing.category = category
            existing.content = content[:4000]
            existing.importance = max(1, min(5, importance))
            existing.source_type = source_type
            existing.source_event_id = source_event_id
            existing.updated_at = now
            if scope == "group":
                existing.subject_user_id = subject_user_id
        await self._trim(session, model, filters, limit)
        await session.flush()
        return self._project(existing, scope)

    async def _scope_values(
        self,
        session: AsyncSession,
        *,
        scope: MemoryScope,
        user_id: str | None,
        group_id: str | None,
        now: datetime,
    ) -> tuple[Any, tuple[Any, ...], dict[str, str]]:
        if scope == "person":
            if not user_id:
                raise ValueError("person memory requires user_id")
            await _ensure_person(session, user_id, now=now)
            return (
                PersonMemoryModel,
                (PersonMemoryModel.user_id == user_id,),
                {"user_id": user_id},
            )
        if scope == "group":
            if not group_id:
                raise ValueError("group memory requires group_id")
            await _ensure_group(session, group_id, now=now)
            return (
                GroupMemoryModel,
                (GroupMemoryModel.group_id == group_id,),
                {"group_id": group_id},
            )
        if not user_id or not group_id:
            raise ValueError("person-group memory requires user_id and group_id")
        await _ensure_person(session, user_id, now=now)
        await _ensure_group(session, group_id, now=now)
        membership = await session.get(MembershipModel, {"user_id": user_id, "group_id": group_id})
        if membership is None:
            session.add(
                MembershipModel(
                    user_id=user_id,
                    group_id=group_id,
                    group_card="",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            await session.flush()
        return (
            PersonGroupMemoryModel,
            (
                PersonGroupMemoryModel.user_id == user_id,
                PersonGroupMemoryModel.group_id == group_id,
            ),
            {"user_id": user_id, "group_id": group_id},
        )

    @staticmethod
    async def _trim(
        session: AsyncSession,
        model: Any,
        filters: tuple[Any, ...],
        limit: int,
    ) -> None:
        count = int(
            await session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
        )
        if count <= limit:
            return
        removable = (
            await session.scalars(
                select(model.id)
                .where(*filters, model.source_type != "explicit")
                .order_by(model.importance.asc(), model.updated_at.asc())
                .limit(count - limit)
            )
        ).all()
        if removable:
            await session.execute(delete(model).where(model.id.in_(removable)))

    async def update_explicit(
        self,
        memory_id: int,
        *,
        user_id: str,
        content: str,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.update_explicit(
                    memory_id,
                    user_id=user_id,
                    content=content,
                    session=owned_session,
                )
        row = await session.get(PersonMemoryModel, memory_id)
        if row is None or row.user_id != user_id:
            return False
        row.content = content
        row.source_type = "explicit"
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return True

    async def delete_person_memory(
        self,
        memory_id: int,
        *,
        user_id: str,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.delete_person_memory(
                    memory_id,
                    user_id=user_id,
                    session=owned_session,
                )
        result = await session.execute(
            delete(PersonMemoryModel).where(
                PersonMemoryModel.id == memory_id,
                PersonMemoryModel.user_id == user_id,
            )
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def prune_person_memories(
        self,
        *,
        user_id: str,
        max_importance: int,
        older_than: datetime,
        session: AsyncSession | None = None,
    ) -> int:
        """Delete stale automatic person memories matching one bounded rule."""

        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.prune_person_memories(
                    user_id=user_id,
                    max_importance=max_importance,
                    older_than=older_than,
                    session=owned_session,
                )
        result = await session.execute(
            delete(PersonMemoryModel).where(
                PersonMemoryModel.user_id == user_id,
                PersonMemoryModel.source_type != "explicit",
                PersonMemoryModel.importance <= max_importance,
                PersonMemoryModel.updated_at < older_than,
            )
        )
        return int(cast(CursorResult[Any], result).rowcount or 0)

    async def list_preferences(
        self,
        user_id: str,
        *,
        limit: int = 30,
        session: AsyncSession | None = None,
    ) -> tuple[PreferenceRecord, ...]:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.list_preferences(
                    user_id,
                    limit=limit,
                    session=owned_session,
                )
        rows = (
            await session.scalars(
                select(PersonPreferenceModel)
                .where(PersonPreferenceModel.user_id == user_id)
                .order_by(PersonPreferenceModel.updated_at.desc())
                .limit(limit)
            )
        ).all()
        return tuple(
            PreferenceRecord(
                id=row.id,
                key=row.preference_key,
                value=row.value,
                source_type=row.source_type,
                updated_at=row.updated_at,
            )
            for row in rows
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
    ) -> PreferenceRecord:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_preference(
                    user_id,
                    key,
                    value,
                    limit=limit,
                    source_type=source_type,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        await _ensure_person(session, user_id, now=now)
        statement = insert(PersonPreferenceModel).values(
            user_id=user_id,
            preference_key=key,
            value=value[:2000],
            source_type=source_type,
            created_at=now,
            updated_at=now,
        )
        update_statement = statement.on_conflict_do_update(
            index_elements=[
                PersonPreferenceModel.user_id,
                PersonPreferenceModel.preference_key,
            ],
            set_={
                "value": value[:2000],
                "source_type": source_type,
                "updated_at": now,
            },
            where=(
                PersonPreferenceModel.source_type != "explicit"
                if source_type != "explicit"
                else None
            ),
        )
        await session.execute(update_statement)
        stale = (
            await session.scalars(
                select(PersonPreferenceModel.id)
                .where(PersonPreferenceModel.user_id == user_id)
                .order_by(PersonPreferenceModel.updated_at.desc())
                .offset(limit)
            )
        ).all()
        if stale:
            await session.execute(
                delete(PersonPreferenceModel).where(PersonPreferenceModel.id.in_(stale))
            )
        row = await session.scalar(
            select(PersonPreferenceModel).where(
                PersonPreferenceModel.user_id == user_id,
                PersonPreferenceModel.preference_key == key,
            )
        )
        assert row is not None
        return PreferenceRecord(
            id=row.id,
            key=row.preference_key,
            value=row.value,
            source_type=row.source_type,
            updated_at=row.updated_at,
        )

    async def delete_preference(
        self,
        user_id: str,
        key: str,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.delete_preference(
                    user_id,
                    key,
                    session=owned_session,
                )
        result = await session.execute(
            delete(PersonPreferenceModel).where(
                PersonPreferenceModel.user_id == user_id,
                PersonPreferenceModel.preference_key == key,
            )
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def count_person(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.count_person(user_id, session=owned_session)
        value = await session.scalar(
            select(func.count())
            .select_from(PersonMemoryModel)
            .where(PersonMemoryModel.user_id == user_id)
        )
        return int(value or 0)


class MemoryJobRepository:
    """Durable queue with bounded retries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def enqueue(self, event_id: int) -> None:
        now = datetime.now(UTC)
        statement = insert(MemoryJobModel).values(
            event_id=event_id,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=[MemoryJobModel.event_id])
            )

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MemoryJobModel)
                .where(
                    MemoryJobModel.status == "pending",
                    MemoryJobModel.next_attempt_at <= datetime.now(UTC),
                )
            )
            return int(value or 0)

    async def claim(self, *, limit: int = 20) -> tuple[MemoryJobRecord, ...]:
        now = datetime.now(UTC)
        stale_processing = now - timedelta(minutes=5)
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(MemoryJobModel)
                    .where(
                        or_(
                            MemoryJobModel.status == "pending",
                            (
                                (MemoryJobModel.status == "processing")
                                & (MemoryJobModel.updated_at <= stale_processing)
                            ),
                        ),
                        MemoryJobModel.next_attempt_at <= now,
                    )
                    .order_by(MemoryJobModel.id)
                    .limit(limit)
                )
            ).all()
            result: list[MemoryJobRecord] = []
            for row in rows:
                event = await session.get(ChatEventModel, row.event_id)
                if event is None:
                    await session.delete(row)
                    continue
                row.status = "processing"
                row.updated_at = now
                result.append(
                    MemoryJobRecord(
                        job_id=row.id,
                        attempts=row.attempts,
                        event=_event_record(event),
                    )
                )
            return tuple(result)

    async def complete(self, job_ids: tuple[int, ...]) -> None:
        if not job_ids:
            return
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryJobModel)
                .where(MemoryJobModel.id.in_(job_ids))
                .values(status="done", updated_at=datetime.now(UTC), error_category=None)
            )

    async def fail(self, job_id: int, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(MemoryJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = "failed" if row.attempts >= 3 else "pending"
            row.next_attempt_at = now + timedelta(seconds=30 * row.attempts)
            row.updated_at = now
            row.error_category = error_category[:64]
