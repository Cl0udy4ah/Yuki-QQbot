"""Backend validation that prevents model-selected identity attribution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryEvidenceRelation,
    MemoryScopeType,
    MemorySourceType,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.subjects import SubjectResolver
from qq_ai_bot.memory.temporal import MemoryTemporalResolver
from qq_ai_bot.persistence.repository_records import EventRecord

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_EXPLICIT_MARKERS = ("记住", "记得", "别忘", "请保存", "加入记忆")


@dataclass(frozen=True, slots=True)
class ValidatedMemoryClaim:
    operation: MemoryClaimOperation
    fact: MemoryFactCreate
    evidence: MemoryEvidenceCreate
    subject_is_speaker: bool
    occurred_at: datetime


def normalize_memory_text(value: str, *, maximum: int) -> str:
    """Flatten untrusted model/user text before persistence and prompt use."""

    return " ".join(_CONTROL.sub(" ", value).split())[:maximum].strip()


class MemoryClaimValidator:
    """Turn a claim into trusted persistence input or reject it without guessing."""

    def __init__(
        self,
        resolver: SubjectResolver | None = None,
        temporal: MemoryTemporalResolver | None = None,
        *,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._resolver = resolver or SubjectResolver()
        self._temporal = temporal or MemoryTemporalResolver()
        self._timezone_name = timezone_name

    def validate(
        self,
        claim: MemoryClaim,
        event: EventRecord,
    ) -> tuple[MemoryFactCreate, MemoryEvidenceCreate] | None:
        validated = self.validate_claim(claim, event)
        if validated is None:
            return None
        return validated.fact, validated.evidence

    def validate_claim(
        self,
        claim: MemoryClaim,
        event: EventRecord,
    ) -> ValidatedMemoryClaim | None:
        resolved = self._resolver.resolve(
            event,
            subject_ref=claim.subject_ref,
            scope_type=claim.scope_type,
        )
        if resolved is None:
            return None
        key = normalize_memory_text(claim.memory_key, maximum=128)
        category = normalize_memory_text(claim.category, maximum=64)
        content = normalize_memory_text(claim.content, maximum=4000)
        if not key or not category or not content:
            return None
        subject_is_speaker = resolved.subject_user_id == event.sender_user_id
        is_third_party = bool(resolved.subject_user_id) and not subject_is_speaker
        if is_third_party and resolved.scope_type is not MemoryScopeType.PERSON_GROUP:
            return None
        if is_third_party and claim.operation in {
            MemoryClaimOperation.CORRECT,
            MemoryClaimOperation.RETRACT,
        }:
            return None
        source_type = claim.source_type
        if source_type is MemorySourceType.EXPLICIT and not any(
            marker in event.content for marker in _EXPLICIT_MARKERS
        ):
            source_type = MemorySourceType.AUTOMATIC
        if is_third_party:
            source_type = MemorySourceType.AUTOMATIC
            authority = MemoryAuthority.THIRD_PARTY
        elif source_type is MemorySourceType.EXPLICIT:
            authority = MemoryAuthority.EXPLICIT
        elif resolved.scope_type is MemoryScopeType.GROUP:
            authority = MemoryAuthority.GROUP_REPORT
        else:
            authority = MemoryAuthority.SELF_REPORT
        try:
            temporal = self._temporal.resolve(
                mode=claim.temporal_mode,
                valid_from=claim.valid_from,
                valid_until=claim.valid_until,
                occurred_at=event.occurred_at,
                timezone_name=self._timezone_name,
            )
        except ValueError:
            return None
        fact = MemoryFactCreate(
            scope_type=resolved.scope_type,
            subject_user_id=resolved.subject_user_id,
            group_id=resolved.group_id,
            kind=claim.kind,
            memory_key=key,
            category=category,
            content=content,
            importance=claim.importance,
            confidence=claim.confidence,
            source_type=source_type,
            authority=authority,
            valid_from=temporal.valid_from,
            valid_until=temporal.valid_until,
        )
        relation = self._evidence_relation(
            operation=claim.operation,
            authority=authority,
            source_type=source_type,
        )
        evidence = MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id=event.sender_user_id,
            relation=relation,
            confidence=claim.confidence,
            authority=authority,
            excerpt=normalize_memory_text(event.content, maximum=500),
        )
        return ValidatedMemoryClaim(
            operation=claim.operation,
            fact=fact,
            evidence=evidence,
            subject_is_speaker=subject_is_speaker,
            occurred_at=event.occurred_at,
        )

    @staticmethod
    def _evidence_relation(
        *,
        operation: MemoryClaimOperation,
        authority: MemoryAuthority,
        source_type: MemorySourceType,
    ) -> MemoryEvidenceRelation:
        if operation is MemoryClaimOperation.RETRACT:
            return MemoryEvidenceRelation.RETRACTION
        if operation is MemoryClaimOperation.CORRECT:
            return MemoryEvidenceRelation.CORRECTION
        if operation is MemoryClaimOperation.CONFIRM:
            return MemoryEvidenceRelation.CONFIRMATION
        if source_type is MemorySourceType.EXPLICIT:
            return MemoryEvidenceRelation.EXPLICIT_COMMAND
        if authority is MemoryAuthority.THIRD_PARTY:
            return MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
        if authority is MemoryAuthority.GROUP_REPORT:
            return MemoryEvidenceRelation.GROUP_STATEMENT
        return MemoryEvidenceRelation.SELF_STATEMENT
