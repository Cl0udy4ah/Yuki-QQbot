"""One event eligibility policy shared by live extraction and historical rebuild."""

from __future__ import annotations

from sqlalchemy import exists, func, not_, or_, select

from qq_ai_bot.memory.enums import MemoryJobStatus
from qq_ai_bot.persistence.models import ChatEventModel, MemoryJobModel, PersonModel
from qq_ai_bot.persistence.repository_records import EventRecord


class MemoryEventEligibilityPolicy:
    """Keep domain and SQL event eligibility intentionally equivalent."""

    allowed_origins = frozenset({"user_message", "onebot_history"})

    def is_eligible(self, event: EventRecord, *, sender_is_bot: bool = False) -> bool:
        return bool(
            event.direction == "inbound"
            and not sender_is_bot
            and event.sender_user_id != event.bot_user_id
            and event.content.strip()
            and event.origin in self.allowed_origins
            and event.scope_type.value in {"private", "group"}
            and (event.scope_type.value != "group" or event.group_id)
            and (event.scope_type.value != "private" or event.private_peer_user_id)
        )

    def sql_conditions(self, *, include_failed_live_jobs: bool) -> tuple[object, ...]:
        excluded = [
            MemoryJobStatus.DONE.value,
            MemoryJobStatus.PENDING.value,
            MemoryJobStatus.PROCESSING.value,
        ]
        if not include_failed_live_jobs:
            excluded.append(MemoryJobStatus.FAILED.value)
        receipt = exists(
            select(MemoryJobModel.id).where(
                MemoryJobModel.event_id == ChatEventModel.id,
                MemoryJobModel.status.in_(excluded),
            )
        )
        bot_sender = exists(
            select(PersonModel.user_id).where(
                PersonModel.user_id == ChatEventModel.sender_user_id,
                PersonModel.is_bot.is_(True),
            )
        )
        return (
            ChatEventModel.direction == "inbound",
            ChatEventModel.sender_user_id != ChatEventModel.bot_user_id,
            not_(bot_sender),
            func.length(func.trim(ChatEventModel.content)) > 0,
            ChatEventModel.origin.in_(tuple(self.allowed_origins)),
            ChatEventModel.scope_type.in_(("private", "group")),
            or_(ChatEventModel.scope_type != "group", ChatEventModel.group_id.is_not(None)),
            or_(
                ChatEventModel.scope_type != "private",
                ChatEventModel.private_peer_user_id.is_not(None),
            ),
            not_(receipt),
        )
