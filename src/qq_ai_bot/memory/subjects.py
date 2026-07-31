"""Deterministic subject aliases derived from one trusted ledger event."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryScopeType
from qq_ai_bot.memory.extraction import AvailableSubject
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    scope_type: MemoryScopeType
    subject_user_id: str | None
    group_id: str | None


class SubjectResolver:
    """Map model-visible aliases to backend-owned QQ and group identifiers."""

    @staticmethod
    def available(event: EventRecord) -> tuple[AvailableSubject, ...]:
        scopes = [MemoryScopeType.PERSON]
        if event.scope_type is ScopeType.GROUP:
            scopes.append(MemoryScopeType.PERSON_GROUP)
        subjects = [AvailableSubject(subject_ref="speaker", allowed_scopes=tuple(scopes))]
        if event.scope_type is ScopeType.GROUP and event.group_id:
            subjects.append(
                AvailableSubject(
                    subject_ref="group",
                    allowed_scopes=(MemoryScopeType.GROUP,),
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
        return None
