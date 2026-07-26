"""Transactional repositories for people, the event ledger, and structured memory."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode, ScopeType
from qq_ai_bot.domain.memories import GroupMemory, GroupMemoryUpsert
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.domain.relationships import (
    RelationshipEvaluation,
    RelationshipSnapshot,
    effective_trust,
    relationship_weight,
    stage_for_score,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    AgentActionModel,
    ChatEventModel,
    ContextResetModel,
    EmojiDescriptionModel,
    GroupMemoryModel,
    GroupModel,
    MediaAnalysisModel,
    MembershipModel,
    MemoryJobModel,
    PersonAliasModel,
    PersonGroupMemoryModel,
    PersonMemoryModel,
    PersonModel,
    PersonPreferenceModel,
    PersonRelationshipModel,
    ProcessedEventModel,
    RelationshipEventModel,
    RelationshipJobModel,
    RuntimeConfigOverrideModel,
    WebSearchRunModel,
    WebSearchSourceModel,
)
from qq_ai_bot.web.base import WebSearchError, normalize_public_url
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource

MemoryScope = Literal["person", "group", "person_group"]


@dataclass(frozen=True, slots=True)
class GroupSetting:
    """Domain projection of a group setting row."""

    group_id: str
    enabled: bool
    require_mention: bool
    conversation_mode: ConversationMode
    autonomous_enabled: bool = True
    name: str = ""


@dataclass(frozen=True, slots=True)
class PrivateUserSetting:
    """Domain projection of one private-chat access state."""

    user_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One permanent ledger event."""

    id: int
    bot_user_id: str
    platform_message_id: str
    scope_type: ScopeType
    sender_user_id: str
    direction: str
    content: str
    visual_summary: str
    segments: tuple[dict[str, Any], ...]
    occurred_at: datetime
    group_id: str | None = None
    private_peer_user_id: str | None = None
    reply_to_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class MediaAnalysisRecord:
    """A cached structured observation; it never contains source image bytes."""

    id: int
    source_event_id: int | None
    segment_index: int
    content_hash: str
    analysis_mode: str
    question_hash: str
    provider: str
    model: str
    prompt_version: str
    observation_json: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EmojiDescriptionRecord:
    """A persistent, reusable description of one stable QQ emoji identity."""

    id: int
    emoji_key: str
    analysis_mode: str
    question_hash: str
    provider: str
    model: str
    prompt_version: str
    description: str
    observation_json: str
    hit_count: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A memory projection shared by the three memory scopes."""

    id: int
    memory_key: str
    category: str
    content: str
    importance: int
    source_type: str
    updated_at: datetime
    user_id: str | None = None
    group_id: str | None = None
    subject_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreferenceRecord:
    """One interaction preference."""

    id: int
    key: str
    value: str
    source_type: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryJobRecord:
    """A claimed background memory job with its source event."""

    job_id: int
    attempts: int
    event: EventRecord


@dataclass(frozen=True, slots=True)
class RelationshipEventRecord:
    """One relationship change without duplicated chat content."""

    id: int
    user_id: str
    change_type: str
    affection_before: int
    affection_delta: int
    affection_after: int
    trust_before: int
    trust_delta: int
    trust_after: int
    reason_code: str
    confidence: float | None
    created_at: datetime
    source_event_id: int | None = None
    actor_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipJobRecord:
    """A claimed relationship job with bounded person-specific context."""

    job_id: int
    attempts: int
    user_id: str
    conversation_key: str
    trigger_event: EventRecord
    recent_events: tuple[EventRecord, ...]


async def _ensure_person(
    session: AsyncSession,
    user_id: str,
    *,
    nickname: str = "",
    is_bot: bool = False,
    now: datetime | None = None,
) -> PersonModel:
    timestamp = now or datetime.now(UTC)
    person = await session.get(PersonModel, user_id)
    if person is None:
        person = PersonModel(
            user_id=user_id,
            nickname=nickname,
            enabled=True,
            is_bot=is_bot,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        session.add(person)
        await session.flush()
    else:
        if nickname:
            person.nickname = nickname
        person.is_bot = person.is_bot or is_bot
        person.last_seen_at = timestamp
    return person


async def _ensure_relationship(
    session: AsyncSession,
    user_id: str,
    *,
    initial_affection: int = 50,
    initial_trust: int = 50,
    now: datetime | None = None,
) -> PersonRelationshipModel:
    timestamp = now or datetime.now(UTC)
    row = await session.get(PersonRelationshipModel, user_id)
    if row is None:
        row = PersonRelationshipModel(
            user_id=user_id,
            affection_score=initial_affection,
            trust_score=initial_trust,
            created_at=timestamp,
            updated_at=timestamp,
            last_automatic_change_at=None,
        )
        session.add(row)
        await session.flush()
    return row


async def _ensure_group(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    enabled: bool | None = None,
    now: datetime | None = None,
) -> GroupModel:
    timestamp = now or datetime.now(UTC)
    group = await session.get(GroupModel, group_id)
    if group is None:
        group = GroupModel(
            group_id=group_id,
            name=name,
            enabled=bool(enabled),
            require_mention=True,
            autonomous_enabled=True,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            updated_at=timestamp,
        )
        session.add(group)
        await session.flush()
    else:
        if name:
            group.name = name
        if enabled is not None:
            group.enabled = enabled
        group.last_seen_at = timestamp
        group.updated_at = timestamp
    return group


def _event_record(row: ChatEventModel) -> EventRecord:
    try:
        decoded = json.loads(row.segments_json)
    except json.JSONDecodeError:
        decoded = []
    segments = tuple(item for item in decoded if isinstance(item, dict))
    return EventRecord(
        id=row.id,
        bot_user_id=row.bot_user_id,
        platform_message_id=row.platform_message_id,
        scope_type=ScopeType(row.scope_type),
        sender_user_id=row.sender_user_id,
        direction=row.direction,
        content=row.content,
        visual_summary=row.visual_summary,
        segments=segments,
        occurred_at=row.occurred_at,
        group_id=row.group_id,
        private_peer_user_id=row.private_peer_user_id,
        reply_to_message_id=row.reply_to_message_id,
    )


def _relationship_snapshot(
    row: PersonRelationshipModel,
    *,
    trust_cap_offset: int,
) -> RelationshipSnapshot:
    usable_trust = effective_trust(
        row.affection_score,
        row.trust_score,
        cap_offset=trust_cap_offset,
    )
    return RelationshipSnapshot(
        user_id=row.user_id,
        affection_score=row.affection_score,
        trust_score=row.trust_score,
        effective_trust=usable_trust,
        relationship_weight=relationship_weight(row.affection_score, usable_trust),
        stage=stage_for_score(row.affection_score),
        updated_at=row.updated_at,
    )


def _relationship_event_record(row: RelationshipEventModel) -> RelationshipEventRecord:
    return RelationshipEventRecord(
        id=row.id,
        user_id=row.user_id,
        source_event_id=row.source_event_id,
        actor_user_id=row.actor_user_id,
        change_type=row.change_type,
        affection_before=row.affection_before,
        affection_delta=row.affection_delta,
        affection_after=row.affection_after,
        trust_before=row.trust_before,
        trust_delta=row.trust_delta,
        trust_after=row.trust_after,
        reason_code=row.reason_code,
        confidence=row.confidence,
        created_at=row.created_at,
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
                            "next_attempt_at": now,
                            "updated_at": now,
                            "error_category": None,
                        },
                    )
                )
            await session.execute(
                delete(PersonMemoryModel).where(
                    PersonMemoryModel.source_type != "explicit",
                    or_(
                        PersonMemoryModel.content.contains(user_id),
                        PersonMemoryModel.source_event_id.in_(attributable_event_ids),
                    ),
                )
            )
            await session.execute(
                delete(PersonGroupMemoryModel).where(
                    PersonGroupMemoryModel.source_type != "explicit",
                    or_(
                        PersonGroupMemoryModel.content.contains(user_id),
                        PersonGroupMemoryModel.source_event_id.in_(attributable_event_ids),
                    ),
                )
            )
            await session.execute(
                delete(GroupMemoryModel).where(
                    or_(
                        GroupMemoryModel.subject_user_id == user_id,
                        (
                            (GroupMemoryModel.source_type != "explicit")
                            & or_(
                                GroupMemoryModel.content.contains(user_id),
                                GroupMemoryModel.source_event_id.in_(attributable_event_ids),
                            )
                        ),
                    )
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


class EventLedgerRepository:
    """Append, query, search, and forget permanent raw chat events."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def append(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
        scope_type: ScopeType,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...] = (),
        group_id: str | None = None,
        private_peer_user_id: str | None = None,
        reply_to_message_id: str | None = None,
        occurred_at: datetime | None = None,
        sender_nickname: str = "",
        sender_is_bot: bool = False,
    ) -> tuple[EventRecord, bool]:
        """Insert idempotently and return the existing row on duplicate."""

        timestamp = occurred_at or datetime.now(UTC)
        observed_at = datetime.now(UTC)
        try:
            async with self._database.sessions() as session, session.begin():
                await _ensure_person(
                    session,
                    sender_user_id,
                    nickname=sender_nickname,
                    is_bot=sender_is_bot,
                    now=timestamp,
                )
                await _ensure_person(session, bot_user_id, is_bot=True, now=observed_at)
                if private_peer_user_id:
                    await _ensure_person(session, private_peer_user_id, now=timestamp)
                if group_id:
                    await _ensure_group(session, group_id, now=timestamp)
                row = ChatEventModel(
                    bot_user_id=bot_user_id,
                    platform_message_id=platform_message_id,
                    scope_type=scope_type.value,
                    group_id=group_id,
                    private_peer_user_id=private_peer_user_id,
                    sender_user_id=sender_user_id,
                    direction=direction,
                    content=content,
                    visual_summary="",
                    segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
                    reply_to_message_id=reply_to_message_id,
                    occurred_at=timestamp,
                    observed_at=observed_at,
                )
                session.add(row)
                await session.flush()
                record = _event_record(row)
            return record, True
        except IntegrityError:
            async with self._database.sessions() as session:
                existing_row = await session.scalar(
                    select(ChatEventModel).where(
                        ChatEventModel.bot_user_id == bot_user_id,
                        ChatEventModel.platform_message_id == platform_message_id,
                    )
                )
                if existing_row is None:
                    raise
                return _event_record(existing_row), False

    async def append_inbound(
        self, message: InboundMessage, *, bot_user_id: str
    ) -> tuple[EventRecord, bool]:
        peer = message.sender.user_id if message.scope_type is ScopeType.PRIVATE else None
        return await self.append(
            bot_user_id=bot_user_id,
            platform_message_id=message.message_id,
            scope_type=message.scope_type,
            group_id=message.group_id,
            private_peer_user_id=peer,
            sender_user_id=message.sender.user_id,
            direction="inbound",
            content=message.text,
            segments=message.segments,
            reply_to_message_id=message.reply_to_message_id,
            occurred_at=message.received_at,
            sender_nickname=message.sender.nickname,
            sender_is_bot=message.sender.is_bot,
        )

    async def find_by_platform_message(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
    ) -> EventRecord | None:
        """Return one exact locally observed event without widening its conversation scope."""

        async with self._database.sessions() as session:
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == bot_user_id,
                    ChatEventModel.platform_message_id == platform_message_id,
                )
            )
        return _event_record(row) if row is not None else None

    async def list_recent(
        self,
        *,
        scope_type: ScopeType,
        user_id: str,
        group_id: str | None,
        limit: int,
        since: datetime | None = None,
    ) -> tuple[EventRecord, ...]:
        query = select(ChatEventModel)
        if scope_type is ScopeType.GROUP:
            query = query.where(ChatEventModel.group_id == group_id)
        else:
            query = query.where(ChatEventModel.private_peer_user_id == user_id)
        if since is not None:
            query = query.where(ChatEventModel.occurred_at >= since)
        async with self._database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(
                            ChatEventModel.occurred_at.desc(), ChatEventModel.id.desc()
                        ).limit(limit)
                    )
                ).all()
            )
        rows.reverse()
        return tuple(_event_record(row) for row in rows)

    async def search(
        self,
        *,
        keyword: str,
        limit: int = 20,
        user_id: str | None = None,
        group_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> tuple[EventRecord, ...]:
        """Search with trigram FTS, falling back to bounded LIKE for short terms."""

        bounded_limit = max(1, min(limit, 100))
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": bounded_limit}
        if user_id:
            conditions.append(
                "(ce.sender_user_id = :user_id OR ce.private_peer_user_id = :user_id)"
            )
            params["user_id"] = user_id
        if group_id:
            conditions.append("ce.group_id = :group_id")
            params["group_id"] = group_id
        if after:
            conditions.append("ce.occurred_at >= :after")
            params["after"] = after
        if before:
            conditions.append("ce.occurred_at <= :before")
            params["before"] = before
        prefix = f" AND {' AND '.join(conditions)}" if conditions else ""
        stripped = keyword.strip()
        if len(stripped) >= 3:
            sql = text(
                """
                SELECT ce.* FROM chat_events AS ce
                JOIN chat_events_fts AS fts ON fts.rowid = ce.id
                WHERE chat_events_fts MATCH :keyword
                """
                + prefix
                + " ORDER BY ce.occurred_at DESC, ce.id DESC LIMIT :limit"
            )
            params["keyword"] = '"' + stripped.replace('"', '""') + '"'
        else:
            if not conditions:
                raise ValueError("short history searches require a QQ, group, or time bound")
            sql = text(
                "SELECT ce.* FROM chat_events AS ce WHERE ce.content LIKE :pattern"
                + prefix
                + " ORDER BY ce.occurred_at DESC, ce.id DESC LIMIT :limit"
            )
            params["pattern"] = f"%{stripped}%"
        async with self._database.sessions() as session:
            mappings = (await session.execute(sql, params)).mappings().all()
        records: list[EventRecord] = []
        for row in mappings:
            raw_occurred = row["occurred_at"]
            occurred = (
                datetime.fromisoformat(raw_occurred)
                if isinstance(raw_occurred, str)
                else raw_occurred
            )
            raw_segments = json.loads(str(row["segments_json"]))
            records.append(
                EventRecord(
                    id=int(row["id"]),
                    bot_user_id=str(row["bot_user_id"]),
                    platform_message_id=str(row["platform_message_id"]),
                    scope_type=ScopeType(str(row["scope_type"])),
                    sender_user_id=str(row["sender_user_id"]),
                    direction=str(row["direction"]),
                    content=str(row["content"]),
                    visual_summary=str(row["visual_summary"] or ""),
                    segments=tuple(raw_segments) if isinstance(raw_segments, list) else (),
                    occurred_at=occurred,
                    group_id=row["group_id"],
                    private_peer_user_id=row["private_peer_user_id"],
                    reply_to_message_id=row["reply_to_message_id"],
                )
            )
        return tuple(reversed(records))

    async def set_visual_summary(self, event_id: int, summary: str) -> bool:
        """Attach one compact derived observation to its immutable source event."""

        normalized = summary.strip()[:6000]
        lowered = normalized.casefold()
        if "data:image/" in lowered or "base64://" in lowered:
            raise ValueError("visual_summary must not contain image or Base64 payloads")
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == event_id)
                .values(visual_summary=normalized)
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def count_context(self, identity: ConversationIdentity) -> int:
        reset = await self.context_reset(identity)
        rows = await self.list_recent(
            scope_type=identity.scope_type,
            user_id=identity.user_id,
            group_id=identity.group_id,
            limit=100_000,
            since=reset,
        )
        return len(rows)

    async def set_context_reset(self, identity: ConversationIdentity) -> int:
        count = await self.count_context(identity)
        now = datetime.now(UTC)
        statement = insert(ContextResetModel).values(
            context_key=identity.key,
            user_id=identity.user_id,
            group_id=identity.group_id,
            reset_at=now,
        )
        async with self._database.sessions() as session, session.begin():
            await _ensure_person(session, identity.user_id, now=now)
            if identity.group_id:
                await _ensure_group(session, identity.group_id, now=now)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ContextResetModel.context_key],
                    set_={"reset_at": now},
                )
            )
        return count

    async def context_reset(self, identity: ConversationIdentity) -> datetime | None:
        async with self._database.sessions() as session:
            return cast(
                datetime | None,
                await session.scalar(
                    select(ContextResetModel.reset_at).where(
                        ContextResetModel.context_key == identity.key
                    )
                ),
            )


class ConversationRepository:
    """Compatibility facade: conversation history is now a view over the event ledger."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._ledger = EventLedgerRepository(database)

    async def ensure(self, identity: ConversationIdentity) -> int:
        return 0

    async def add_message(
        self,
        identity: ConversationIdentity,
        *,
        role: str,
        content: str,
        platform_message_id: str | None = None,
    ) -> None:
        sender = identity.user_id if role == "user" else "compat-bot"
        await self._ledger.append(
            bot_user_id="compat-bot",
            platform_message_id=platform_message_id or f"compat-{uuid.uuid4()}",
            scope_type=identity.scope_type,
            sender_user_id=sender,
            direction="inbound" if role == "user" else "outbound",
            content=content,
            group_id=identity.group_id,
            private_peer_user_id=(
                identity.user_id if identity.scope_type is ScopeType.PRIVATE else None
            ),
            sender_is_bot=role == "assistant",
        )

    async def list_context(
        self,
        identity: ConversationIdentity,
        *,
        max_messages: int,
        max_characters: int,
    ) -> tuple[ChatMessage, ...]:
        reset = await self._ledger.context_reset(identity)
        rows = await self._ledger.list_recent(
            scope_type=identity.scope_type,
            user_id=identity.user_id,
            group_id=identity.group_id,
            limit=max_messages,
            since=reset,
        )
        selected: list[ChatMessage] = []
        used = 0
        for row in reversed(rows):
            remaining = max_characters - used
            if remaining <= 0:
                break
            content = row.content[-remaining:]
            selected.append(
                ChatMessage(
                    role="assistant" if row.direction == "outbound" else "user",
                    content=content,
                )
            )
            used += len(content)
        selected.reverse()
        return tuple(selected)

    async def count_messages(self, identity: ConversationIdentity) -> int:
        return await self._ledger.count_context(identity)

    async def clear(self, identity: ConversationIdentity) -> int:
        return await self._ledger.set_context_reset(identity)


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


class GroupMemoryRepository:
    """Compatibility facade over the new group-memory layer."""

    def __init__(self, database: Database) -> None:
        self._memories = MemoryRepository(database)
        self._database = database

    async def list_recent(self, group_id: str, *, limit: int) -> tuple[GroupMemory, ...]:
        rows = await self._memories.list_group(group_id, limit=limit)
        return tuple(
            GroupMemory(
                id=row.id,
                group_id=group_id,
                memory_key=row.memory_key,
                content=row.content,
                updated_at=row.updated_at,
            )
            for row in reversed(rows)
        )

    async def apply_updates(
        self,
        group_id: str,
        *,
        upserts: tuple[GroupMemoryUpsert, ...],
        delete_keys: tuple[str, ...],
        limit: int,
    ) -> None:
        if delete_keys:
            async with self._database.sessions() as session, session.begin():
                await session.execute(
                    delete(GroupMemoryModel).where(
                        GroupMemoryModel.group_id == group_id,
                        GroupMemoryModel.memory_key.in_(delete_keys),
                    )
                )
        for item in upserts:
            await self._memories.upsert(
                scope="group",
                group_id=group_id,
                memory_key=item.memory_key,
                content=item.content,
                limit=limit,
            )

    async def count(self, group_id: str) -> int:
        return len(await self._memories.list_group(group_id, limit=100_000))


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


class RelationshipRepository:
    """Persist bounded per-person affection and trust with a complete audit trail."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
        trust_cap_offset: int = 10,
        max_affection_auto_delta: int = 2,
        max_trust_auto_delta: int = 2,
    ) -> None:
        self._database = database
        self._initial_affection = initial_affection
        self._initial_trust = initial_trust
        self._trust_cap_offset = trust_cap_offset
        self._max_affection_auto_delta = max_affection_auto_delta
        self._max_trust_auto_delta = max_trust_auto_delta

    async def _ensure_row(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        now: datetime,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> PersonRelationshipModel:
        person = await session.get(PersonModel, user_id)
        if person is None:
            await _ensure_person(session, user_id, now=now)
        return await _ensure_relationship(
            session,
            user_id,
            initial_affection=(
                self._initial_affection if initial_affection is None else initial_affection
            ),
            initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
            now=now,
        )

    async def get_or_create(
        self,
        user_id: str,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self.get_or_create(
                    user_id,
                    initial_affection=initial_affection,
                    initial_trust=initial_trust,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        row = await self._ensure_row(
            session,
            user_id,
            now=now,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )
        await session.flush()
        return _relationship_snapshot(row, trust_cap_offset=self._trust_cap_offset)

    async def get(self, user_id: str) -> RelationshipSnapshot | None:
        async with self._database.sessions() as session:
            row = await session.get(PersonRelationshipModel, user_id)
            if row is None:
                return None
            return _relationship_snapshot(row, trust_cap_offset=self._trust_cap_offset)

    async def history(
        self,
        user_id: str,
        *,
        limit: int = 10,
    ) -> tuple[RelationshipEventRecord, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(RelationshipEventModel)
                    .where(RelationshipEventModel.user_id == user_id)
                    .order_by(
                        RelationshipEventModel.created_at.desc(),
                        RelationshipEventModel.id.desc(),
                    )
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
            return tuple(_relationship_event_record(row) for row in rows)

    async def apply_automatic(
        self,
        *,
        user_id: str,
        source_event_id: int,
        evaluation: RelationshipEvaluation,
        max_auto_delta: int | None = None,
        daily_positive_cap: int = 0,
        daily_negative_cap: int = 0,
    ) -> tuple[RelationshipSnapshot, bool]:
        """Apply one event once, with optional runtime daily caps (zero means unlimited)."""

        self._validate_automatic_evaluation(
            evaluation,
            maximum=max_auto_delta,
        )
        now = datetime.now(UTC)
        try:
            async with self._database.sessions() as session, session.begin():
                existing = await session.scalar(
                    select(RelationshipEventModel.id).where(
                        RelationshipEventModel.change_type == "automatic",
                        RelationshipEventModel.source_event_id == source_event_id,
                    )
                )
                row = await self._ensure_row(session, user_id, now=now)
                if existing is not None:
                    return (
                        _relationship_snapshot(
                            row,
                            trust_cap_offset=self._trust_cap_offset,
                        ),
                        False,
                    )
                source = await session.get(ChatEventModel, source_event_id)
                if (
                    source is None
                    or source.sender_user_id != user_id
                    or source.direction != "inbound"
                ):
                    raise ValueError("relationship source event does not belong to the user")

                effective_evaluation = evaluation
                if daily_positive_cap or daily_negative_cap:
                    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    daily_events = (
                        await session.scalars(
                            select(RelationshipEventModel).where(
                                RelationshipEventModel.user_id == user_id,
                                RelationshipEventModel.change_type == "automatic",
                                RelationshipEventModel.created_at >= day_start,
                            )
                        )
                    ).all()
                    affection_delta = self._apply_daily_cap(
                        evaluation.affection_delta,
                        positive_used=sum(max(0, item.affection_delta) for item in daily_events),
                        negative_used=sum(max(0, -item.affection_delta) for item in daily_events),
                        positive_cap=daily_positive_cap,
                        negative_cap=daily_negative_cap,
                    )
                    trust_delta = self._apply_daily_cap(
                        evaluation.trust_delta,
                        positive_used=sum(max(0, item.trust_delta) for item in daily_events),
                        negative_used=sum(max(0, -item.trust_delta) for item in daily_events),
                        positive_cap=daily_positive_cap,
                        negative_cap=daily_negative_cap,
                    )
                    effective_evaluation = RelationshipEvaluation(
                        affection_delta=affection_delta,
                        trust_delta=trust_delta,
                        reason_code=(
                            evaluation.reason_code if affection_delta or trust_delta else "neutral"
                        ),
                        confidence=evaluation.confidence,
                    )
                affection_before = row.affection_score
                trust_before = row.trust_score
                row.affection_score = max(
                    0,
                    min(100, affection_before + effective_evaluation.affection_delta),
                )
                row.trust_score = max(
                    0,
                    min(100, trust_before + effective_evaluation.trust_delta),
                )
                affection_delta = row.affection_score - affection_before
                trust_delta = row.trust_score - trust_before
                row.updated_at = now
                if affection_delta or trust_delta:
                    row.last_automatic_change_at = now
                session.add(
                    RelationshipEventModel(
                        user_id=user_id,
                        source_event_id=source_event_id,
                        actor_user_id=None,
                        change_type="automatic",
                        affection_before=affection_before,
                        affection_delta=affection_delta,
                        affection_after=row.affection_score,
                        trust_before=trust_before,
                        trust_delta=trust_delta,
                        trust_after=row.trust_score,
                        reason_code=effective_evaluation.reason_code[:64],
                        confidence=effective_evaluation.confidence,
                        created_at=now,
                    )
                )
                await session.flush()
                return (
                    _relationship_snapshot(
                        row,
                        trust_cap_offset=self._trust_cap_offset,
                    ),
                    True,
                )
        except IntegrityError:
            snapshot = await self.get_or_create(user_id)
            return snapshot, False

    def _validate_automatic_evaluation(
        self,
        evaluation: RelationshipEvaluation,
        *,
        maximum: int | None = None,
    ) -> None:
        affection_maximum = maximum or self._max_affection_auto_delta
        trust_maximum = maximum or self._max_trust_auto_delta
        for value, maximum, name in (
            (
                evaluation.affection_delta,
                affection_maximum,
                "affection_delta",
            ),
            (
                evaluation.trust_delta,
                trust_maximum,
                "trust_delta",
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or abs(value) > maximum:
                raise ValueError(f"{name} exceeds the configured automatic range")
        if not 0 <= evaluation.confidence <= 1:
            raise ValueError("confidence must be between zero and one")

    @staticmethod
    def _apply_daily_cap(
        delta: int,
        *,
        positive_used: int,
        negative_used: int,
        positive_cap: int,
        negative_cap: int,
    ) -> int:
        if delta > 0 and positive_cap > 0:
            return min(delta, max(0, positive_cap - positive_used))
        if delta < 0 and negative_cap > 0:
            return max(delta, -max(0, negative_cap - negative_used))
        return delta

    async def set_affection(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        score: int,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if not 0 <= score <= 100:
            raise ValueError("affection score must be between 0 and 100")
        return await self._apply_manual(
            user_id=user_id,
            actor_user_id=actor_user_id,
            affection_score=score,
            reason_code="manual_set_affection",
            session=session,
        )

    async def adjust_affection(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        delta: int,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if not -20 <= delta <= 20:
            raise ValueError("affection adjustment must be between -20 and 20")
        return await self._apply_manual(
            user_id=user_id,
            actor_user_id=actor_user_id,
            affection_delta=delta,
            reason_code="manual_adjust_affection",
            session=session,
        )

    async def set_trust(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        score: int,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if not 0 <= score <= 100:
            raise ValueError("trust score must be between 0 and 100")
        return await self._apply_manual(
            user_id=user_id,
            actor_user_id=actor_user_id,
            trust_score=score,
            reason_code="manual_set_trust",
            session=session,
        )

    async def _apply_manual(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        reason_code: str,
        affection_score: int | None = None,
        affection_delta: int = 0,
        trust_score: int | None = None,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if session is None:
            async with self._database.sessions() as owned_session, owned_session.begin():
                return await self._apply_manual(
                    user_id=user_id,
                    actor_user_id=actor_user_id,
                    reason_code=reason_code,
                    affection_score=affection_score,
                    affection_delta=affection_delta,
                    trust_score=trust_score,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        row = await self._ensure_row(session, user_id, now=now)
        affection_before = row.affection_score
        trust_before = row.trust_score
        row.affection_score = (
            affection_score
            if affection_score is not None
            else max(0, min(100, affection_before + affection_delta))
        )
        row.trust_score = trust_score if trust_score is not None else trust_before
        actual_affection_delta = row.affection_score - affection_before
        actual_trust_delta = row.trust_score - trust_before
        row.updated_at = now
        session.add(
            RelationshipEventModel(
                user_id=user_id,
                source_event_id=None,
                actor_user_id=actor_user_id,
                change_type="manual",
                affection_before=affection_before,
                affection_delta=actual_affection_delta,
                affection_after=row.affection_score,
                trust_before=trust_before,
                trust_delta=actual_trust_delta,
                trust_after=row.trust_score,
                reason_code=reason_code,
                confidence=None,
                created_at=now,
            )
        )
        await session.flush()
        return _relationship_snapshot(row, trust_cap_offset=self._trust_cap_offset)


class RelationshipJobRepository:
    """Restart-safe relationship queue with bounded retries and five-event context."""

    def __init__(self, database: Database, *, max_attempts: int = 3) -> None:
        self._database = database
        self._max_attempts = max_attempts

    async def enqueue(
        self,
        *,
        trigger_event_id: int,
        user_id: str,
        conversation_key: str,
    ) -> None:
        now = datetime.now(UTC)
        statement = insert(RelationshipJobModel).values(
            trigger_event_id=trigger_event_id,
            user_id=user_id,
            conversation_key=conversation_key,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            error_category=None,
            created_at=now,
            updated_at=now,
        )
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[RelationshipJobModel.trigger_event_id]
                )
            )

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(RelationshipJobModel)
                .where(
                    RelationshipJobModel.status == "pending",
                    RelationshipJobModel.next_attempt_at <= datetime.now(UTC),
                )
            )
            return int(value or 0)

    async def claim(self, *, limit: int = 10) -> tuple[RelationshipJobRecord, ...]:
        now = datetime.now(UTC)
        stale_processing = now - timedelta(minutes=5)
        async with self._database.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(RelationshipJobModel)
                    .where(
                        or_(
                            RelationshipJobModel.status == "pending",
                            (
                                (RelationshipJobModel.status == "processing")
                                & (RelationshipJobModel.updated_at <= stale_processing)
                            ),
                        ),
                        RelationshipJobModel.next_attempt_at <= now,
                    )
                    .order_by(RelationshipJobModel.id)
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
            result: list[RelationshipJobRecord] = []
            for row in rows:
                trigger = await session.get(ChatEventModel, row.trigger_event_id)
                if trigger is None:
                    await session.delete(row)
                    continue
                recent_query = select(ChatEventModel).where(
                    ChatEventModel.id <= trigger.id,
                )
                if trigger.scope_type == ScopeType.PRIVATE.value:
                    recent_query = recent_query.where(
                        ChatEventModel.private_peer_user_id == row.user_id,
                    )
                else:
                    recent_query = recent_query.where(
                        ChatEventModel.group_id == trigger.group_id,
                        ChatEventModel.sender_user_id == row.user_id,
                    )
                recent_rows = list(
                    (
                        await session.scalars(
                            recent_query.order_by(ChatEventModel.id.desc()).limit(5)
                        )
                    ).all()
                )
                recent_rows.reverse()
                row.status = "processing"
                row.updated_at = now
                result.append(
                    RelationshipJobRecord(
                        job_id=row.id,
                        attempts=row.attempts,
                        user_id=row.user_id,
                        conversation_key=row.conversation_key,
                        trigger_event=_event_record(trigger),
                        recent_events=tuple(_event_record(event) for event in recent_rows),
                    )
                )
            return tuple(result)

    async def complete(self, job_ids: tuple[int, ...]) -> None:
        if not job_ids:
            return
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(RelationshipJobModel)
                .where(RelationshipJobModel.id.in_(job_ids))
                .values(
                    status="completed",
                    updated_at=datetime.now(UTC),
                    error_category=None,
                )
            )

    async def fail(self, job_id: int, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(RelationshipJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = "failed" if row.attempts >= self._max_attempts else "pending"
            row.next_attempt_at = now + timedelta(seconds=30 * row.attempts)
            row.updated_at = now
            row.error_category = error_category[:64]


class AgentActionRepository:
    """Record safe metadata for generic OneBot actions."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        actor_user_id: str | None,
        action: str,
        success: bool,
        duration_seconds: float,
        error_category: str | None = None,
    ) -> None:
        async with self._database.sessions() as session, session.begin():
            session.add(
                AgentActionModel(
                    actor_user_id=actor_user_id,
                    action=action[:128],
                    success=success,
                    duration_seconds=duration_seconds,
                    error_category=error_category[:64] if error_category else None,
                    created_at=datetime.now(UTC),
                )
            )


class MediaAnalysisRepository:
    """Persist and reuse expiring structured visual observations."""

    _MODES = frozenset({"general", "meme", "ocr", "question"})

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find_cached(
        self,
        *,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> MediaAnalysisRecord | None:
        """Find one unexpired exact cache entry for a prepared media payload."""

        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(MediaAnalysisModel).where(
                    MediaAnalysisModel.content_hash == content_hash,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.question_hash == (question_hash or ""),
                    MediaAnalysisModel.provider == provider,
                    MediaAnalysisModel.model == model,
                    MediaAnalysisModel.prompt_version == prompt_version,
                    MediaAnalysisModel.expires_at > timestamp,
                )
            )
        return self._record(row) if row is not None else None

    async def get_cached(
        self,
        *,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> MediaAnalysisRecord | None:
        """Compatibility spelling for callers that use get-style repository APIs."""

        return await self.find_cached(
            content_hash=content_hash,
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            now=now,
        )

    async def find_for_event(
        self,
        source_event_id: int,
        segment_index: int,
        *,
        analysis_mode: str,
        question_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> MediaAnalysisRecord | None:
        """Find an unexpired observation attached to one exact ledger segment."""

        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(MediaAnalysisModel).where(
                    MediaAnalysisModel.source_event_id == source_event_id,
                    MediaAnalysisModel.segment_index == segment_index,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.question_hash == question_hash,
                    MediaAnalysisModel.provider == provider,
                    MediaAnalysisModel.model == model,
                    MediaAnalysisModel.prompt_version == prompt_version,
                    MediaAnalysisModel.expires_at > timestamp,
                )
            )
        return self._record(row) if row is not None else None

    async def save(
        self,
        *,
        source_event_id: int | None,
        segment_index: int,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        observation_json: str | dict[str, Any],
        expires_at: datetime,
        created_at: datetime | None = None,
    ) -> MediaAnalysisRecord:
        """Upsert a cache value while preserving its original event association."""

        self._validate_key(
            content_hash=content_hash,
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            segment_index=segment_index,
        )
        normalized_question_hash = question_hash or ""
        serialized = self._serialize_observation(observation_json)
        timestamp = created_at or datetime.now(UTC)
        values = {
            "source_event_id": source_event_id,
            "segment_index": segment_index,
            "content_hash": content_hash,
            "analysis_mode": analysis_mode,
            "question_hash": normalized_question_hash,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "observation_json": serialized,
            "created_at": timestamp,
            "expires_at": expires_at,
        }
        statement = insert(MediaAnalysisModel).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                MediaAnalysisModel.content_hash,
                MediaAnalysisModel.analysis_mode,
                MediaAnalysisModel.question_hash,
                MediaAnalysisModel.model,
                MediaAnalysisModel.prompt_version,
            ],
            set_={
                # A refresh may update the provider and observation, but a cache hit
                # never changes the source event that owns cascade deletion.
                "provider": provider,
                "observation_json": serialized,
                "created_at": timestamp,
                "expires_at": expires_at,
            },
        )
        async with self._database.sessions() as session, session.begin():
            await session.execute(statement)
            row = await session.scalar(
                select(MediaAnalysisModel).where(
                    MediaAnalysisModel.content_hash == content_hash,
                    MediaAnalysisModel.analysis_mode == analysis_mode,
                    MediaAnalysisModel.question_hash == normalized_question_hash,
                    MediaAnalysisModel.model == model,
                    MediaAnalysisModel.prompt_version == prompt_version,
                )
            )
            if row is None:  # pragma: no cover - guarded by the insert above
                raise RuntimeError("media analysis upsert did not return a row")
            return self._record(row)

    async def save_analysis(self, **values: Any) -> MediaAnalysisRecord:
        """Compatibility spelling for service-layer integrations."""

        return await self.save(**values)

    async def associate_event(
        self,
        analysis_id: int,
        *,
        source_event_id: int,
        segment_index: int,
    ) -> bool:
        """Attach an unowned cache row once; never move it between ledger events."""

        if segment_index < 0:
            raise ValueError("segment_index must be non-negative")
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(MediaAnalysisModel)
                .where(
                    MediaAnalysisModel.id == analysis_id,
                    MediaAnalysisModel.source_event_id.is_(None),
                )
                .values(
                    source_event_id=source_event_id,
                    segment_index=segment_index,
                )
            )
            return bool(cast(CursorResult[Any], result).rowcount)

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Delete cache rows whose expiry has been reached."""

        cutoff = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(MediaAnalysisModel).where(MediaAnalysisModel.expires_at <= cutoff)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)

    @classmethod
    def _validate_key(
        cls,
        *,
        content_hash: str,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        segment_index: int,
    ) -> None:
        if len(content_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest")
        try:
            int(content_hash, 16)
        except ValueError as exc:
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest") from exc
        if analysis_mode not in cls._MODES:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")
        if analysis_mode == "question" and not question_hash:
            raise ValueError("question analysis requires question_hash")
        if question_hash and len(question_hash) > 64:
            raise ValueError("question_hash must not exceed 64 characters")
        if segment_index < 0:
            raise ValueError("segment_index must be non-negative")
        if not provider or len(provider) > 32:
            raise ValueError("provider must contain at most 32 characters")
        if not model or len(model) > 128:
            raise ValueError("model must contain at most 128 characters")
        if not prompt_version or len(prompt_version) > 64:
            raise ValueError("prompt_version must contain at most 64 characters")

    @staticmethod
    def _serialize_observation(value: str | dict[str, Any]) -> str:
        return _serialize_visual_observation(value)

    @staticmethod
    def _record(row: MediaAnalysisModel) -> MediaAnalysisRecord:
        return MediaAnalysisRecord(
            id=row.id,
            source_event_id=row.source_event_id,
            segment_index=row.segment_index,
            content_hash=row.content_hash,
            analysis_mode=row.analysis_mode,
            question_hash=row.question_hash,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            observation_json=row.observation_json,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )


class EmojiDescriptionRepository:
    """Persist visual observations behind durable, exact emoji lookup keys."""

    _MODES = frozenset({"general", "meme", "ocr", "question"})

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find_first(
        self,
        emoji_keys: tuple[str, ...],
        *,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        now: datetime | None = None,
    ) -> EmojiDescriptionRecord | None:
        """Return the first exact key match and atomically record its reuse."""

        keys = self._validated_keys(emoji_keys)
        self._validate_lookup(
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if not keys:
            return None
        timestamp = now or datetime.now(UTC)
        normalized_question_hash = question_hash or ""
        async with self._database.sessions() as session, session.begin():
            for key in keys:
                row = await session.scalar(
                    select(EmojiDescriptionModel).where(
                        EmojiDescriptionModel.emoji_key == key,
                        EmojiDescriptionModel.analysis_mode == analysis_mode,
                        EmojiDescriptionModel.question_hash == normalized_question_hash,
                        EmojiDescriptionModel.provider == provider,
                        EmojiDescriptionModel.model == model,
                        EmojiDescriptionModel.prompt_version == prompt_version,
                    )
                )
                if row is None:
                    continue
                row.hit_count += 1
                row.last_used_at = timestamp
                await session.flush()
                return self._record(row)
        return None

    async def save_many(
        self,
        emoji_keys: tuple[str, ...],
        *,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
        observation_json: str | dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[EmojiDescriptionRecord, ...]:
        """Upsert one safe observation for every equivalent emoji identity key."""

        keys = self._validated_keys(emoji_keys)
        self._validate_lookup(
            analysis_mode=analysis_mode,
            question_hash=question_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if not keys:
            return ()
        serialized = _serialize_visual_observation(observation_json)
        parsed = json.loads(serialized)
        description_value = parsed.get("overall_description", "")
        description = description_value if isinstance(description_value, str) else ""
        description = " ".join(description.split())[:2000]
        timestamp = now or datetime.now(UTC)
        normalized_question_hash = question_hash or ""
        records: list[EmojiDescriptionRecord] = []
        async with self._database.sessions() as session, session.begin():
            for key in keys:
                values = {
                    "emoji_key": key,
                    "analysis_mode": analysis_mode,
                    "question_hash": normalized_question_hash,
                    "provider": provider,
                    "model": model,
                    "prompt_version": prompt_version,
                    "description": description,
                    "observation_json": serialized,
                    "hit_count": 0,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "last_used_at": timestamp,
                }
                statement = insert(EmojiDescriptionModel).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        EmojiDescriptionModel.emoji_key,
                        EmojiDescriptionModel.analysis_mode,
                        EmojiDescriptionModel.question_hash,
                        EmojiDescriptionModel.model,
                        EmojiDescriptionModel.prompt_version,
                    ],
                    set_={
                        "provider": provider,
                        "description": description,
                        "observation_json": serialized,
                        "updated_at": timestamp,
                        "last_used_at": timestamp,
                    },
                )
                await session.execute(statement)
                row = await session.scalar(
                    select(EmojiDescriptionModel).where(
                        EmojiDescriptionModel.emoji_key == key,
                        EmojiDescriptionModel.analysis_mode == analysis_mode,
                        EmojiDescriptionModel.question_hash == normalized_question_hash,
                        EmojiDescriptionModel.model == model,
                        EmojiDescriptionModel.prompt_version == prompt_version,
                    )
                )
                if row is None:  # pragma: no cover - guarded by the upsert above
                    raise RuntimeError("emoji description upsert did not return a row")
                records.append(self._record(row))
        return tuple(records)

    @classmethod
    def _validated_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        keys = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 255 for value in keys):
            raise ValueError("emoji_key must not exceed 255 characters")
        if any(not value.startswith(("package:", "emoji:", "file:", "content:")) for value in keys):
            raise ValueError("unsupported emoji_key namespace")
        return keys

    @classmethod
    def _validate_lookup(
        cls,
        *,
        analysis_mode: str,
        question_hash: str | None,
        provider: str,
        model: str,
        prompt_version: str,
    ) -> None:
        if analysis_mode not in cls._MODES:
            raise ValueError(f"unsupported analysis_mode: {analysis_mode}")
        if analysis_mode == "question" and not question_hash:
            raise ValueError("question analysis requires question_hash")
        if question_hash and len(question_hash) > 64:
            raise ValueError("question_hash must not exceed 64 characters")
        if not provider or len(provider) > 32:
            raise ValueError("provider must contain at most 32 characters")
        if not model or len(model) > 128:
            raise ValueError("model must contain at most 128 characters")
        if not prompt_version or len(prompt_version) > 64:
            raise ValueError("prompt_version must contain at most 64 characters")

    @staticmethod
    def _record(row: EmojiDescriptionModel) -> EmojiDescriptionRecord:
        return EmojiDescriptionRecord(
            id=row.id,
            emoji_key=row.emoji_key,
            analysis_mode=row.analysis_mode,
            question_hash=row.question_hash,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            description=row.description,
            observation_json=row.observation_json,
            hit_count=row.hit_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_used_at=row.last_used_at,
        )


def _serialize_visual_observation(value: str | dict[str, Any]) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value

    def contains_embedded_media(item: Any) -> bool:
        if isinstance(item, str):
            lowered = item.lstrip().lower()
            return lowered.startswith(("data:image/", "base64://"))
        if isinstance(item, dict):
            return any(contains_embedded_media(child) for child in item.values())
        if isinstance(item, list | tuple):
            return any(contains_embedded_media(child) for child in item)
        return False

    if contains_embedded_media(parsed):
        raise ValueError("observation_json must not contain image or Base64 payloads")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


class WebSearchSourceRepository:
    """Persist real source metadata with strict ConversationIdentity isolation."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save_response(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        provider: str,
        response: WebSearchResponse,
        max_runs: int,
    ) -> int:
        """Persist one successful tool run and prune older runs in this conversation."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            run = WebSearchRunModel(
                conversation_key=conversation_key[:255],
                trigger_message_id=trigger_message_id[:128],
                query=response.query[:400],
                provider=provider[:32],
                created_at=now,
                partial_failure=response.partial_failure,
            )
            session.add(run)
            await session.flush()
            seen: set[str] = set()
            ordinal = 0
            for source in response.sources:
                try:
                    normalized = normalize_public_url(source.url)
                except WebSearchError:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                ordinal += 1
                session.add(
                    WebSearchSourceModel(
                        run_id=run.id,
                        ordinal=ordinal,
                        title=" ".join(source.title.split())[:512],
                        url=normalized,
                        domain=source.domain[:255],
                        snippet=source.snippet[:1000],
                        published_at=source.published_at,
                        provider_score=source.provider_score,
                        created_at=now,
                    )
                )
            await session.flush()
            old_run_ids = (
                await session.scalars(
                    select(WebSearchRunModel.id)
                    .where(WebSearchRunModel.conversation_key == conversation_key[:255])
                    .order_by(WebSearchRunModel.created_at.desc(), WebSearchRunModel.id.desc())
                    .offset(max_runs)
                )
            ).all()
            if old_run_ids:
                await session.execute(
                    delete(WebSearchRunModel).where(WebSearchRunModel.id.in_(old_run_ids))
                )
            return run.id

    async def for_trigger(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
    ) -> tuple[WebSearchSource, ...]:
        """Return only sources used by this trigger in this exact conversation."""

        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(WebSearchSourceModel)
                    .join(
                        WebSearchRunModel,
                        WebSearchSourceModel.run_id == WebSearchRunModel.id,
                    )
                    .where(
                        WebSearchRunModel.conversation_key == conversation_key[:255],
                        WebSearchRunModel.trigger_message_id == trigger_message_id[:128],
                    )
                    .order_by(
                        WebSearchRunModel.created_at.asc(),
                        WebSearchRunModel.id.asc(),
                        WebSearchSourceModel.ordinal.asc(),
                    )
                )
            ).scalars()
            return tuple(self._source_record(row) for row in rows)

    async def latest(self, conversation_key: str) -> tuple[WebSearchSource, ...]:
        """Return sources from the latest successful run in one conversation."""

        async with self._database.sessions() as session:
            run_id = await session.scalar(
                select(WebSearchRunModel.id)
                .join(
                    WebSearchSourceModel,
                    WebSearchSourceModel.run_id == WebSearchRunModel.id,
                )
                .where(WebSearchRunModel.conversation_key == conversation_key[:255])
                .group_by(WebSearchRunModel.id)
                .order_by(WebSearchRunModel.created_at.desc(), WebSearchRunModel.id.desc())
                .limit(1)
            )
            if run_id is None:
                return ()
            rows = (
                await session.scalars(
                    select(WebSearchSourceModel)
                    .where(WebSearchSourceModel.run_id == run_id)
                    .order_by(WebSearchSourceModel.ordinal.asc())
                )
            ).all()
            return tuple(self._source_record(row) for row in rows)

    async def used_url_for_trigger(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        url: str,
    ) -> bool:
        """Return whether a prior web search in this turn produced this URL."""

        normalized = normalize_public_url(url)
        async with self._database.sessions() as session:
            count = await session.scalar(
                select(func.count(WebSearchSourceModel.id))
                .join(
                    WebSearchRunModel,
                    WebSearchSourceModel.run_id == WebSearchRunModel.id,
                )
                .where(
                    WebSearchRunModel.conversation_key == conversation_key[:255],
                    WebSearchRunModel.trigger_message_id == trigger_message_id[:128],
                    WebSearchSourceModel.url == normalized,
                )
            )
            return bool(count)

    async def cleanup_expired(
        self,
        *,
        retention_days: int,
        now: datetime | None = None,
    ) -> int:
        """Delete expired runs; source rows cascade through their foreign key."""

        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(WebSearchRunModel).where(WebSearchRunModel.created_at < cutoff)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)

    @staticmethod
    def _source_record(row: WebSearchSourceModel) -> WebSearchSource:
        return WebSearchSource(
            source_id=f"stored-{row.id}",
            title=row.title,
            url=row.url,
            domain=row.domain,
            snippet=row.snippet,
            relevant_content="",
            published_at=row.published_at,
            provider_score=row.provider_score,
        )


class ProcessedEventRepository:
    """Durable idempotency repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def claim(self, event_key: str, *, expires_at: datetime) -> bool:
        try:
            async with self._database.sessions() as session, session.begin():
                session.add(
                    ProcessedEventModel(
                        event_key=event_key,
                        processed_at=datetime.now(UTC),
                        expires_at=expires_at,
                    )
                )
            return True
        except IntegrityError:
            return False

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(ProcessedEventModel).where(ProcessedEventModel.expires_at <= cutoff)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)
