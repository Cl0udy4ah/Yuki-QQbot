"""Deterministic subject aliases derived from one trusted ledger event."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.extraction import AvailableSubject
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    scope_type: MemoryScopeType
    subject_user_id: str | None
    group_id: str | None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None


class SubjectResolver:
    """Map model-visible aliases to backend-owned QQ and group identifiers."""

    @staticmethod
    def available(event: EventRecord) -> tuple[AvailableSubject, ...]:
        scopes = [MemoryScopeType.PERSON]
        if event.scope_type is ScopeType.GROUP:
            scopes.append(MemoryScopeType.PERSON_GROUP)
        subjects = [
            AvailableSubject(
                subject_ref="speaker",
                display_label="当前发送者",
                allowed_scopes=tuple(scopes),
                relation_to_speaker="self",
            )
        ]
        if event.scope_type is ScopeType.GROUP and event.group_id:
            subjects.append(
                AvailableSubject(
                    subject_ref="group",
                    display_label="当前群",
                    allowed_scopes=(MemoryScopeType.GROUP,),
                    relation_to_speaker="current_group",
                )
            )
            seen = {event.sender_user_id, event.bot_user_id, ""}
            mention_number = 0
            for user_id in event.mentioned_user_ids:
                if user_id in seen:
                    continue
                seen.add(user_id)
                mention_number += 1
                subjects.append(
                    AvailableSubject(
                        subject_ref=f"mentioned_{mention_number}",
                        display_label=f"被提及成员{mention_number}",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="mentioned_member",
                    )
                )
            reply_author = event.reply_sender_user_id or ""
            if reply_author not in seen:
                subjects.append(
                    AvailableSubject(
                        subject_ref="reply_author",
                        display_label="回复消息作者",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="reply_author",
                    )
                )
        return tuple(subjects)

    @staticmethod
    def resolve(
        event: EventRecord,
        *,
        subject_ref: str,
        scope_type: MemoryScopeType,
    ) -> ResolvedSubject | None:
        if subject_ref == "self" and scope_type is MemoryScopeType.SELF:
            return ResolvedSubject(scope_type, None, None)
        if subject_ref == "speaker":
            if scope_type is MemoryScopeType.PERSON:
                return ResolvedSubject(scope_type, event.sender_user_id, None)
            if (
                scope_type is MemoryScopeType.PERSON_GROUP
                and event.scope_type is ScopeType.GROUP
                and event.group_id
            ):
                return ResolvedSubject(scope_type, event.sender_user_id, event.group_id)
            return None
        if (
            subject_ref == "group"
            and scope_type is MemoryScopeType.GROUP
            and event.scope_type is ScopeType.GROUP
            and event.group_id
        ):
            return ResolvedSubject(scope_type, None, event.group_id)
        if (
            scope_type is MemoryScopeType.PERSON_GROUP
            and event.scope_type is ScopeType.GROUP
            and event.group_id
        ):
            available = SubjectResolver.available(event)
            subject = next((item for item in available if item.subject_ref == subject_ref), None)
            if subject is None:
                return None
            if subject_ref.startswith("mentioned_"):
                position = int(subject_ref.removeprefix("mentioned_")) - 1
                candidates = tuple(
                    dict.fromkeys(
                        item
                        for item in event.mentioned_user_ids
                        if item and item not in {event.sender_user_id, event.bot_user_id}
                    )
                )
                if 0 <= position < len(candidates):
                    return ResolvedSubject(scope_type, candidates[position], event.group_id)
            if subject_ref == "reply_author" and event.reply_sender_user_id:
                return ResolvedSubject(scope_type, event.reply_sender_user_id, event.group_id)
        return None
