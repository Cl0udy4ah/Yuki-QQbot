"""Transactional repositories that enforce conversation isolation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ConversationModel,
    GroupSettingModel,
    MessageModel,
    ProcessedEventModel,
)


@dataclass(frozen=True, slots=True)
class GroupSetting:
    """Domain projection of a group setting row."""

    group_id: str
    enabled: bool
    require_mention: bool
    conversation_mode: ConversationMode


class ConversationRepository:
    """Read and mutate only explicitly identified conversations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def _get_or_create_in_session(
        self, session: object, identity: ConversationIdentity
    ) -> ConversationModel:
        from sqlalchemy.ext.asyncio import AsyncSession

        if not isinstance(session, AsyncSession):
            raise TypeError("session must be AsyncSession")
        result = await session.execute(
            select(ConversationModel).where(ConversationModel.conversation_key == identity.key)
        )
        conversation = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if conversation is None:
            conversation = ConversationModel(
                conversation_key=identity.key,
                scope_type=identity.scope_type.value,
                group_id=identity.group_id,
                user_id=identity.user_id,
                mode=identity.mode.value,
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
            session.add(conversation)
            await session.flush()
        return conversation

    async def ensure(self, identity: ConversationIdentity) -> int:
        """Create the conversation when absent and return its database id."""

        async with self._database.sessions() as session, session.begin():
            conversation = await self._get_or_create_in_session(session, identity)
            return conversation.id

    async def add_message(
        self,
        identity: ConversationIdentity,
        *,
        role: str,
        content: str,
        platform_message_id: str | None = None,
    ) -> None:
        """Append a message in one explicit transaction."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            conversation = await self._get_or_create_in_session(session, identity)
            session.add(
                MessageModel(
                    conversation_id=conversation.id,
                    role=role,
                    content=content,
                    platform_message_id=platform_message_id,
                    created_at=now,
                    character_count=len(content),
                )
            )
            conversation.updated_at = now
            conversation.last_active_at = now

    async def list_context(
        self,
        identity: ConversationIdentity,
        *,
        max_messages: int,
        max_characters: int,
    ) -> tuple[ChatMessage, ...]:
        """Return system messages plus the newest ordinary messages within limits."""

        async with self._database.sessions() as session:
            conversation_id = await session.scalar(
                select(ConversationModel.id).where(
                    ConversationModel.conversation_key == identity.key
                )
            )
            if conversation_id is None:
                return ()
            rows = (
                await session.scalars(
                    select(MessageModel)
                    .where(MessageModel.conversation_id == conversation_id)
                    .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
                )
            ).all()

        systems = [
            ChatMessage(role=row.role, content=row.content) for row in rows if row.role == "system"
        ]
        ordinary = [row for row in rows if row.role != "system"]
        selected: list[ChatMessage] = []
        used_characters = sum(len(message.content) for message in systems)
        for row in reversed(ordinary):
            remaining = max_characters - used_characters
            if len(selected) >= max_messages or remaining <= 0:
                break
            content = row.content
            if len(content) > remaining:
                if not selected:
                    selected.append(ChatMessage(role=row.role, content=content[-remaining:]))
                break
            selected.append(ChatMessage(role=row.role, content=content))
            used_characters += len(content)
        selected.reverse()
        return tuple([*systems, *selected])

    async def count_messages(self, identity: ConversationIdentity) -> int:
        """Count messages in exactly one conversation."""

        async with self._database.sessions() as session:
            count = await session.scalar(
                select(func.count(MessageModel.id))
                .join(ConversationModel)
                .where(ConversationModel.conversation_key == identity.key)
            )
            return int(count or 0)

    async def clear(self, identity: ConversationIdentity) -> int:
        """Transactionally delete only the selected conversation's messages."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            conversation = await session.scalar(
                select(ConversationModel).where(ConversationModel.conversation_key == identity.key)
            )
            if conversation is None:
                return 0
            count = await session.scalar(
                select(func.count(MessageModel.id)).where(
                    MessageModel.conversation_id == conversation.id
                )
            )
            await session.execute(
                delete(MessageModel).where(MessageModel.conversation_id == conversation.id)
            )
            conversation.updated_at = now
            conversation.last_active_at = now
            return int(count or 0)


class GroupSettingsRepository:
    """Persist dynamic group enablement and future sharing mode."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, group_id: str) -> GroupSetting | None:
        """Fetch a group override."""

        async with self._database.sessions() as session:
            row = await session.get(GroupSettingModel, group_id)
            if row is None:
                return None
            return GroupSetting(
                group_id=row.group_id,
                enabled=row.enabled,
                require_mention=row.require_mention,
                conversation_mode=ConversationMode(row.conversation_mode),
            )

    async def set_enabled(self, group_id: str, enabled: bool) -> GroupSetting:
        """Upsert a group's enabled state in one transaction."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(GroupSettingModel, group_id)
            if row is None:
                row = GroupSettingModel(
                    group_id=group_id,
                    enabled=enabled,
                    require_mention=True,
                    conversation_mode=ConversationMode.PER_USER.value,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.updated_at = now
        return GroupSetting(
            group_id=group_id,
            enabled=enabled,
            require_mention=row.require_mention,
            conversation_mode=ConversationMode(row.conversation_mode),
        )


class ProcessedEventRepository:
    """Durable idempotency repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def claim(self, event_key: str, *, expires_at: datetime) -> bool:
        """Atomically claim an event; return false when it was already claimed."""

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
        """Delete expired idempotency rows."""

        cutoff = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            count = await session.scalar(
                select(func.count(ProcessedEventModel.event_key)).where(
                    ProcessedEventModel.expires_at <= cutoff
                )
            )
            await session.execute(
                delete(ProcessedEventModel).where(ProcessedEventModel.expires_at <= cutoff)
            )
            return int(count or 0)
