"""Repositories for people, memberships, groups, and private access."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.conversations import ConversationMode, ScopeType
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    ChatEventModel,
    GroupModel,
    MembershipModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryJobModel,
    PersonAliasModel,
    PersonModel,
    RuntimeConfigOverrideModel,
    WebSearchRunModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_group,
    _ensure_person,
    _ensure_relationship,
)
from qq_ai_bot.persistence.repository_records import (
    GroupSetting,
    PrivateUserSetting,
)


class PeopleRepository:
    """Keep one global person and exact per-group memberships."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
    ) -> None:
        self._database = database
        self._initial_affection = initial_affection
        self._initial_trust = initial_trust

    async def observe(
        self,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        group_name: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        is_bot: bool = False,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> None:
        """Update current values and retain historical aliases."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            person = await _ensure_person(
                session,
                user_id,
                nickname=nickname if nickname_known else "",
                is_bot=is_bot,
                now=now,
            )
            if not is_bot:
                await _ensure_relationship(
                    session,
                    user_id,
                    initial_affection=(
                        self._initial_affection if initial_affection is None else initial_affection
                    ),
                    initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
                    now=now,
                )
            if nickname_known:
                person.nickname = nickname
            if nickname:
                await self._upsert_alias(session, user_id, "", nickname, "nickname", now)
            if group_id is None:
                return
            existing_group = await session.get(GroupModel, group_id)
            await _ensure_group(
                session,
                group_id,
                name=group_name,
                enabled=True if existing_group is None else None,
                now=now,
            )
            membership = await session.get(
                MembershipModel, {"user_id": user_id, "group_id": group_id}
            )
            if membership is None:
                membership = MembershipModel(
                    user_id=user_id,
                    group_id=group_id,
                    group_card=group_card if group_card_known else "",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(membership)
            else:
                if group_card_known:
                    membership.group_card = group_card
                membership.last_seen_at = now
            if group_card:
                await self._upsert_alias(session, user_id, group_id, group_card, "group_card", now)

    @staticmethod
    async def _upsert_alias(
        session: AsyncSession,
        user_id: str,
        group_scope: str,
        alias: str,
        alias_type: str,
        now: datetime,
    ) -> None:
        statement = insert(PersonAliasModel).values(
            user_id=user_id,
            group_scope=group_scope,
            alias=alias,
            alias_type=alias_type,
            first_seen_at=now,
            last_seen_at=now,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    PersonAliasModel.user_id,
                    PersonAliasModel.group_scope,
                    PersonAliasModel.alias,
                ],
                set_={"alias_type": alias_type, "last_seen_at": now},
            )
        )

    async def get(self, *, user_id: str, group_id: str | None = None) -> UserProfileSnapshot | None:
        async with self._database.sessions() as session:
            person = await session.get(PersonModel, user_id)
            if person is None:
                return None
            card = ""
            if group_id is not None:
                membership = await session.get(
                    MembershipModel, {"user_id": user_id, "group_id": group_id}
                )
                if membership is not None:
                    card = membership.group_card
            return UserProfileSnapshot(
                user_id=person.user_id,
                scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                nickname=person.nickname,
                group_id=group_id,
                group_card=card,
            )

    async def get_many(
        self,
        user_ids: tuple[str, ...],
        *,
        group_id: str | None = None,
    ) -> dict[str, UserProfileSnapshot]:
        """Load several people and their current-group cards in one query."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        async with self._database.sessions() as session:
            statement = select(PersonModel, MembershipModel.group_card).outerjoin(
                MembershipModel,
                and_(
                    MembershipModel.user_id == PersonModel.user_id,
                    MembershipModel.group_id == group_id,
                ),
            )
            rows = (
                await session.execute(statement.where(PersonModel.user_id.in_(unique_ids)))
            ).all()
        return {
            person.user_id: UserProfileSnapshot(
                user_id=person.user_id,
                scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
                nickname=person.nickname,
                group_id=group_id,
                group_card=group_card or "",
            )
            for person, group_card in rows
        }

    async def aliases(self, user_id: str, *, limit: int = 20) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            values = (
                await session.scalars(
                    select(PersonAliasModel.alias)
                    .where(PersonAliasModel.user_id == user_id)
                    .order_by(PersonAliasModel.last_seen_at.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(dict.fromkeys(values))

    async def membership_count(self, user_id: str) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MembershipModel)
                .where(MembershipModel.user_id == user_id)
            )
            return int(value or 0)

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(
                    user_id,
                    enabled,
                    initial_affection=initial_affection,
                    initial_trust=initial_trust,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        person = await _ensure_person(session, user_id, now=now)
        await _ensure_relationship(
            session,
            user_id,
            initial_affection=(
                self._initial_affection if initial_affection is None else initial_affection
            ),
            initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
            now=now,
        )
        person.enabled = enabled
        await session.flush()
        return PrivateUserSetting(user_id=user_id, enabled=enabled)

    async def get_enabled(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting | None:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get_enabled(user_id, session=owned_session)
        person = await session.get(PersonModel, user_id)
        if person is None:
            return None
        return PrivateUserSetting(user_id=user_id, enabled=person.enabled)

    async def delete_person(self, user_id: str, *, marker: str = "[已删除用户]") -> bool:
        """Delete all attributable data and redact exact QQ text elsewhere."""

        async with self._database.sessions() as session, session.begin():
            person = await session.get(PersonModel, user_id)
            if person is None:
                return False
            attributable_event_ids = (
                await session.scalars(
                    select(ChatEventModel.id).where(
                        or_(
                            ChatEventModel.sender_user_id == user_id,
                            ChatEventModel.private_peer_user_id == user_id,
                        )
                    )
                )
            ).all()
            remaining = (
                await session.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.sender_user_id != user_id,
                        or_(
                            ChatEventModel.private_peer_user_id.is_(None),
                            ChatEventModel.private_peer_user_id != user_id,
                        ),
                        or_(
                            ChatEventModel.content.contains(user_id),
                            ChatEventModel.visual_summary.contains(user_id),
                            ChatEventModel.segments_json.contains(user_id),
                        ),
                    )
                )
            ).all()
            for event in remaining:
                event.content = event.content.replace(user_id, marker)
                event.visual_summary = event.visual_summary.replace(user_id, marker)
                event.segments_json = event.segments_json.replace(user_id, marker)
                now = datetime.now(UTC)
                job_statement = insert(MemoryJobModel).values(
                    event_id=event.id,
                    conversation_key=(
                        f"group:{event.group_id}:user:{event.sender_user_id}"
                        if event.group_id
                        else f"private:{event.private_peer_user_id or event.sender_user_id}"
                    ),
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                    error_category=None,
                )
                await session.execute(
                    job_statement.on_conflict_do_update(
                        index_elements=[MemoryJobModel.event_id],
                        set_={
                            "status": "pending",
                            "attempts": 0,
                            "conversation_key": (
                                f"group:{event.group_id}:user:{event.sender_user_id}"
                                if event.group_id
                                else f"private:{event.private_peer_user_id or event.sender_user_id}"
                            ),
                            "next_attempt_at": now,
                            "updated_at": now,
                            "error_category": None,
                        },
                    )
                )
            affected_fact_ids = select(MemoryEvidenceModel.fact_id).where(
                MemoryEvidenceModel.event_id.in_(attributable_event_ids)
            )
            await session.execute(
                delete(MemoryFactModel).where(
                    MemoryFactModel.source_type != "explicit",
                    or_(
                        MemoryFactModel.content.contains(user_id),
                        MemoryFactModel.id.in_(affected_fact_ids),
                    ),
                )
            )
            await session.execute(
                delete(WebSearchRunModel).where(
                    or_(
                        WebSearchRunModel.conversation_key == f"private:{user_id}",
                        WebSearchRunModel.conversation_key.like(
                            f"group:%:user:{user_id}",
                        ),
                    )
                )
            )
            await session.execute(
                delete(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.scope_type == "user",
                    RuntimeConfigOverrideModel.scope_id == user_id,
                )
            )
            await session.execute(
                update(RuntimeConfigOverrideModel)
                .where(RuntimeConfigOverrideModel.updated_by == user_id)
                .values(updated_by=marker)
            )
            audit_rows = (
                await session.scalars(
                    select(AdminOperationEventModel).where(
                        or_(
                            AdminOperationEventModel.actor_user_id == user_id,
                            AdminOperationEventModel.target_id == user_id,
                            AdminOperationEventModel.conversation_key.contains(user_id),
                            AdminOperationEventModel.before_json.contains(user_id),
                            AdminOperationEventModel.after_json.contains(user_id),
                        )
                    )
                )
            ).all()
            for audit in audit_rows:
                if audit.actor_user_id == user_id:
                    audit.actor_user_id = marker
                if audit.target_id == user_id:
                    audit.target_id = marker
                audit.conversation_key = audit.conversation_key.replace(user_id, marker)
                audit.before_json = audit.before_json.replace(user_id, marker)
                audit.after_json = audit.after_json.replace(user_id, marker)
            await session.delete(person)
            return True


class UserProfileRepository(PeopleRepository):
    """Backward-compatible name used by identity services."""

    async def upsert(
        self,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> None:
        await self.observe(
            user_id=user_id,
            nickname=nickname,
            group_id=group_id,
            group_card=group_card,
            nickname_known=nickname_known,
            group_card_known=group_card_known,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )

    async def delete_user(self, user_id: str) -> bool:
        return await self.delete_person(user_id)


class GroupSettingsRepository:
    """Persist group observation and autonomous participation settings."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self,
        group_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting | None:
        if session is None:
            async with self._database.sessions() as owned_session:
                return await self.get(group_id, session=owned_session)
        row = await session.get(GroupModel, group_id)
        if row is None:
            return None
        return GroupSetting(
            group_id=group_id,
            enabled=row.enabled,
            require_mention=row.require_mention,
            conversation_mode=ConversationMode.SHARED,
            autonomous_enabled=row.autonomous_enabled,
            name=row.name,
        )

    async def set_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_enabled(group_id, enabled, session=owned_session)
        now = datetime.now(UTC)
        row = await _ensure_group(session, group_id, enabled=enabled, now=now)
        await session.flush()
        return GroupSetting(
            group_id=group_id,
            enabled=enabled,
            require_mention=row.require_mention,
            conversation_mode=ConversationMode.SHARED,
            autonomous_enabled=row.autonomous_enabled,
            name=row.name,
        )

    async def set_autonomous_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        session: AsyncSession | None = None,
    ) -> GroupSetting:
        """Update only the group's autonomous participation switch."""

        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.set_autonomous_enabled(
                    group_id,
                    enabled,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        row = await _ensure_group(session, group_id, now=now)
        row.autonomous_enabled = enabled
        row.updated_at = now
        await session.flush()
        return GroupSetting(
            group_id=group_id,
            enabled=row.enabled,
            require_mention=row.require_mention,
            conversation_mode=ConversationMode.SHARED,
            autonomous_enabled=enabled,
            name=row.name,
        )

    async def observe(
        self,
        group_id: str,
        *,
        name: str = "",
        enabled_if_new: bool = False,
    ) -> GroupSetting:
        """Create an observed group without overwriting an existing access switch."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            existing = await session.get(GroupModel, group_id)
            row = await _ensure_group(
                session,
                group_id,
                name=name,
                enabled=enabled_if_new if existing is None else None,
                now=now,
            )
        return GroupSetting(
            group_id=group_id,
            enabled=row.enabled,
            require_mention=row.require_mention,
            conversation_mode=ConversationMode.SHARED,
            autonomous_enabled=row.autonomous_enabled,
            name=row.name,
        )


class PrivateUserSettingsRepository:
    """Private chats are allowed unless the person's row explicitly disables them."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
    ) -> None:
        self._people = PeopleRepository(
            database,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )

    async def get(
        self,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting | None:
        return await self._people.get_enabled(user_id, session=session)

    async def set_enabled(
        self,
        user_id: str,
        enabled: bool,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> PrivateUserSetting:
        return await self._people.set_enabled(
            user_id,
            enabled,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
            session=session,
        )
