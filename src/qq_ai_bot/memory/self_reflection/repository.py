"""Restart-safe episode cursors and receipts for low-frequency self-reflection."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.self_reflection.models import (
    SelfReflectionEpisode,
    SelfReflectionState,
    StoredToolReceipt,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record

_HIGH_VALUE = re.compile(
    r"(?:你.{0,6}(?:说错|弄错|记错|理解错)|第一次(?:测试|成功|完成)|终于(?:成功|修好)|"
    r"以后应该|下次记得|我对你|你让我|别再|纠正一下|其实不是)"
)


def conversation_key_hash(
    scope_type: ScopeType,
    *,
    group_id: str | None,
    private_peer_user_id: str | None,
) -> str:
    key = (
        f"group:{group_id}" if scope_type is ScopeType.GROUP else f"private:{private_peer_user_id}"
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class SelfReflectionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def scan_new_events(self, *, limit: int = 5000) -> int:
        """Accumulate only post-deployment events; first startup establishes a baseline."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
            if runtime is None:
                maximum = int(await session.scalar(select(func.max(ChatEventModel.id))) or 0)
                session.add(
                    MemorySelfReflectionRuntimeModel(
                        id=1,
                        last_scanned_event_id=maximum,
                        updated_at=now,
                    )
                )
                return 0
            rows = (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.id > runtime.last_scanned_event_id,
                        ChatEventModel.event_kind == "message",
                        ChatEventModel.direction.in_(("inbound", "outbound")),
                    )
                    .order_by(ChatEventModel.id.asc())
                    .limit(max(1, limit))
                )
            ).all()
            for row in rows:
                peer = row.private_peer_user_id or (
                    row.sender_user_id if row.direction == "inbound" else None
                )
                if row.scope_type == ScopeType.PRIVATE.value and not peer:
                    continue
                key_hash = conversation_key_hash(
                    ScopeType(row.scope_type),
                    group_id=row.group_id,
                    private_peer_user_id=peer,
                )
                state = await session.scalar(
                    select(MemorySelfReflectionStateModel).where(
                        MemorySelfReflectionStateModel.conversation_key_hash == key_hash
                    )
                )
                content = row.content.strip()
                if state is None:
                    state = MemorySelfReflectionStateModel(
                        conversation_key_hash=key_hash,
                        bot_user_id=row.bot_user_id,
                        scope_type=row.scope_type,
                        group_id=row.group_id,
                        private_peer_user_id=peer,
                        last_event_id=runtime.last_scanned_event_id,
                        latest_event_id=row.id,
                        pending_events=0,
                        pending_characters=0,
                        pending_since=row.occurred_at,
                        has_yuki_reply=False,
                        has_tool_result=False,
                        high_value_signal=False,
                        updated_at=now,
                    )
                    session.add(state)
                if content:
                    state.pending_events += 1
                    state.pending_characters += len(content)
                    state.pending_since = state.pending_since or row.occurred_at
                state.latest_event_id = row.id
                state.has_yuki_reply = state.has_yuki_reply or (
                    row.direction == "outbound" and row.sender_user_id == row.bot_user_id
                )
                state.high_value_signal = state.high_value_signal or bool(
                    row.direction == "inbound" and _HIGH_VALUE.search(content)
                )
                state.updated_at = now
            if rows:
                runtime.last_scanned_event_id = rows[-1].id
                runtime.updated_at = now
            return len(rows)

    async def claim_due(
        self,
        *,
        scheduled_slot: str,
        local_date: str,
        event_threshold: int,
        character_threshold: int,
        max_wait_seconds: float,
        max_sessions: int,
        max_daily_calls: int,
        max_events: int,
        max_characters: int,
    ) -> tuple[SelfReflectionEpisode, ...]:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            used = int(
                await session.scalar(
                    select(func.count(MemorySelfReflectionRunModel.id)).where(
                        MemorySelfReflectionRunModel.scheduled_slot.like(f"{local_date}:%")
                    )
                )
                or 0
            )
            available = min(max_sessions, max(0, max_daily_calls - used))
            if available <= 0:
                return ()
            waited_before = now - timedelta(seconds=max_wait_seconds)
            states = (
                await session.scalars(
                    select(MemorySelfReflectionStateModel)
                    .where(
                        MemorySelfReflectionStateModel.pending_events > 0,
                        or_(
                            MemorySelfReflectionStateModel.pending_events >= event_threshold,
                            MemorySelfReflectionStateModel.pending_characters
                            >= character_threshold,
                            MemorySelfReflectionStateModel.pending_since <= waited_before,
                            MemorySelfReflectionStateModel.high_value_signal.is_(True),
                        ),
                    )
                    .order_by(
                        MemorySelfReflectionStateModel.high_value_signal.desc(),
                        MemorySelfReflectionStateModel.pending_since.asc(),
                    )
                    .limit(available * 3)
                )
            ).all()
            claimed: list[SelfReflectionEpisode] = []
            for row in states:
                has_tool = bool(
                    await session.scalar(
                        select(MemoryToolReceiptModel.id).where(
                            MemoryToolReceiptModel.conversation_key_hash
                            == row.conversation_key_hash,
                            MemoryToolReceiptModel.trigger_event_id > row.last_event_id,
                            MemoryToolReceiptModel.expires_at > now,
                        )
                    )
                )
                if not (row.has_yuki_reply or row.has_tool_result or has_tool):
                    continue
                event_query = select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == row.bot_user_id,
                    ChatEventModel.id > row.last_event_id,
                    ChatEventModel.id <= row.latest_event_id,
                    ChatEventModel.event_kind == "message",
                )
                if row.scope_type == ScopeType.GROUP.value:
                    event_query = event_query.where(ChatEventModel.group_id == row.group_id)
                else:
                    event_query = event_query.where(
                        ChatEventModel.private_peer_user_id == row.private_peer_user_id
                    )
                event_rows = list(
                    (
                        await session.scalars(
                            event_query.order_by(ChatEventModel.id.desc()).limit(max_events)
                        )
                    ).all()
                )
                event_rows.reverse()
                while event_rows and sum(len(item.content) for item in event_rows) > max_characters:
                    event_rows.pop(0)
                if not event_rows:
                    continue
                reason = (
                    "high_value"
                    if row.high_value_signal
                    else "event_count"
                    if row.pending_events >= event_threshold
                    else "characters"
                    if row.pending_characters >= character_threshold
                    else "max_wait"
                )
                run_id = await session.scalar(
                    insert(MemorySelfReflectionRunModel)
                    .values(
                        conversation_key_hash=row.conversation_key_hash,
                        scheduled_slot=scheduled_slot,
                        trigger_reason=reason,
                        first_event_id=event_rows[0].id,
                        last_event_id=event_rows[-1].id,
                        status="processing",
                        proposal_count=0,
                        committed_count=0,
                        started_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            MemorySelfReflectionRunModel.conversation_key_hash,
                            MemorySelfReflectionRunModel.scheduled_slot,
                        ]
                    )
                    .returning(MemorySelfReflectionRunModel.id)
                )
                if run_id is None:
                    continue
                claimed.append(
                    SelfReflectionEpisode(
                        state=self._state(row, has_tool=has_tool),
                        events=tuple(_event_record(item) for item in event_rows),
                        trigger_reason=reason,
                        scheduled_slot=scheduled_slot,
                        run_id=run_id,
                    )
                )
                if len(claimed) >= available:
                    break
            return tuple(claimed)

    async def health_snapshot(
        self,
        *,
        local_date: str,
    ) -> tuple[int, int, str | None, datetime | None]:
        """Return content-free pending and execution statistics."""

        async with self._database.sessions() as session:
            pending = int(
                await session.scalar(
                    select(func.count(MemorySelfReflectionStateModel.id)).where(
                        MemorySelfReflectionStateModel.pending_events > 0
                    )
                )
                or 0
            )
            daily_calls = int(
                await session.scalar(
                    select(func.count(MemorySelfReflectionRunModel.id)).where(
                        MemorySelfReflectionRunModel.scheduled_slot.like(f"{local_date}:%")
                    )
                )
                or 0
            )
            last_run = await session.scalar(
                select(MemorySelfReflectionRunModel)
                .order_by(MemorySelfReflectionRunModel.started_at.desc())
                .limit(1)
            )
        return (
            pending,
            daily_calls,
            last_run.status if last_run is not None else None,
            last_run.completed_at if last_run is not None else None,
        )

    async def tool_receipts(
        self,
        episode: SelfReflectionEpisode,
        *,
        limit: int = 8,
    ) -> tuple[StoredToolReceipt, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(MemoryToolReceiptModel)
                    .where(
                        MemoryToolReceiptModel.conversation_key_hash
                        == episode.state.conversation_key_hash,
                        MemoryToolReceiptModel.trigger_event_id >= episode.events[0].id,
                        MemoryToolReceiptModel.trigger_event_id <= episode.events[-1].id,
                        MemoryToolReceiptModel.expires_at > datetime.now(UTC),
                    )
                    .order_by(MemoryToolReceiptModel.created_at.asc())
                    .limit(max(1, limit))
                )
            ).all()
        return tuple(
            StoredToolReceipt(
                id=row.id,
                trigger_event_id=row.trigger_event_id,
                tool_name=row.tool_name,
                success=row.success,
                result_excerpt=row.result_excerpt,
            )
            for row in rows
        )

    async def complete(
        self,
        episode: SelfReflectionEpisode,
        *,
        proposals: int,
        committed: int,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemorySelfReflectionRunModel)
                .where(MemorySelfReflectionRunModel.id == episode.run_id)
                .values(
                    status="completed",
                    proposal_count=proposals,
                    committed_count=committed,
                    completed_at=now,
                )
            )
            await session.execute(
                update(MemorySelfReflectionStateModel)
                .where(MemorySelfReflectionStateModel.id == episode.state.id)
                .values(
                    last_event_id=episode.events[-1].id,
                    pending_events=0,
                    pending_characters=0,
                    pending_since=None,
                    has_yuki_reply=False,
                    has_tool_result=False,
                    high_value_signal=False,
                    updated_at=now,
                )
            )

    async def fail(self, run_id: int, error_category: str) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(MemorySelfReflectionRunModel)
                .where(MemorySelfReflectionRunModel.id == run_id)
                .values(
                    status="failed",
                    error_category=error_category[:64],
                    completed_at=datetime.now(UTC),
                )
            )

    async def cleanup_receipts(self) -> int:
        from sqlalchemy import delete

        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(MemoryToolReceiptModel).where(
                    MemoryToolReceiptModel.expires_at <= datetime.now(UTC)
                )
            )
            return int(cast(CursorResult[object], result).rowcount)

    @staticmethod
    def _state(row: MemorySelfReflectionStateModel, *, has_tool: bool) -> SelfReflectionState:
        return SelfReflectionState(
            id=row.id,
            conversation_key_hash=row.conversation_key_hash,
            bot_user_id=row.bot_user_id,
            scope_type=ScopeType(row.scope_type),
            group_id=row.group_id,
            private_peer_user_id=row.private_peer_user_id,
            last_event_id=row.last_event_id,
            latest_event_id=row.latest_event_id,
            pending_events=row.pending_events,
            pending_characters=row.pending_characters,
            pending_since=row.pending_since,
            has_yuki_reply=row.has_yuki_reply,
            has_tool_result=row.has_tool_result or has_tool,
            high_value_signal=row.high_value_signal,
        )
