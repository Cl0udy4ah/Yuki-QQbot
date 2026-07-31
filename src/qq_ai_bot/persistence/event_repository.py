"""Repositories for the permanent event ledger and event state."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AgentActionModel,
    ChatEventModel,
    ContextResetModel,
    ProcessedEventModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_group,
    _ensure_person,
    _event_record,
)
from qq_ai_bot.persistence.repository_records import (
    EventRecord,
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
        origin: str = "user_message",
        automation_id: int | None = None,
        automation_run_id: int | None = None,
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
                    origin=origin[:32],
                    automation_id=automation_id,
                    automation_run_id=automation_run_id,
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

    async def list_before(
        self,
        event: EventRecord,
        *,
        limit: int,
    ) -> tuple[EventRecord, ...]:
        """Return only earlier events from the primary event's exact conversation."""

        query = select(ChatEventModel).where(
            ChatEventModel.bot_user_id == event.bot_user_id,
            or_(
                ChatEventModel.occurred_at < event.occurred_at,
                (
                    (ChatEventModel.occurred_at == event.occurred_at)
                    & (ChatEventModel.id < event.id)
                ),
            ),
        )
        if event.scope_type is ScopeType.GROUP:
            query = query.where(ChatEventModel.group_id == event.group_id)
        else:
            query = query.where(ChatEventModel.private_peer_user_id == event.private_peer_user_id)
        async with self._database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(
                            ChatEventModel.occurred_at.desc(),
                            ChatEventModel.id.desc(),
                        ).limit(max(1, limit))
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
                    origin=str(row["origin"] or "user_message"),
                    automation_id=row["automation_id"],
                    automation_run_id=row["automation_run_id"],
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
