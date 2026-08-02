"""Backend validation that prevents model-selected identity attribution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryEvidenceRelation,
    MemoryKind,
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
_INTERACTION_MARKERS = (
    "回复",
    "回答",
    "称呼",
    "语音",
    "表情",
    "格式",
    "简短",
    "句号",
    "引用",
    "yuki",
    "机器人",
)


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


def event_requests_explicit_memory(value: str) -> bool:
    """Return whether the trusted event explicitly asks Yuki to retain a memory."""

    return any(marker in value for marker in _EXPLICIT_MARKERS)


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
        if not event.content.strip():
            return None
        raw_quote = claim.evidence_quote.strip()
        if not raw_quote or raw_quote not in event.content:
            return None
        quote = normalize_memory_text(raw_quote, maximum=500)
        source = normalize_memory_text(event.content, maximum=4000)
        if not quote or quote not in source:
            return None
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
        if not self._semantically_anchored(content, quote):
            return None
        kind = claim.kind
        lowered = f"{key} {category} {content} {quote}".casefold()
        if any(marker in lowered for marker in _INTERACTION_MARKERS):
            kind = MemoryKind.PREFERENCE
        subject_is_speaker = resolved.subject_user_id == event.sender_user_id
        is_third_party = bool(resolved.subject_user_id) and not subject_is_speaker
        if is_third_party and resolved.scope_type is not MemoryScopeType.PERSON_GROUP:
            return None
        source_type = claim.source_type
        if source_type is MemorySourceType.EXPLICIT and not event_requests_explicit_memory(
            event.content
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
            kind=kind,
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
            excerpt=quote,
        )
        return ValidatedMemoryClaim(
            operation=claim.operation,
            fact=fact,
            evidence=evidence,
            subject_is_speaker=subject_is_speaker,
            occurred_at=event.occurred_at,
        )

    @staticmethod
    def _semantically_anchored(content: str, quote: str) -> bool:
        """Conservatively reject claims derived from context instead of this event."""

        def compact(value: str) -> str:
            return "".join(char.casefold() for char in value if char.isalnum())

        claim_text = compact(content)
        evidence_text = compact(quote)
        if not claim_text or not evidence_text:
            return False
        if claim_text in evidence_text or evidence_text in claim_text:
            return True
        is_cjk = any("\u4e00" <= char <= "\u9fff" for char in evidence_text)
        if is_cjk:
            cjk_claim = "".join(char for char in claim_text if "\u4e00" <= char <= "\u9fff")
            if len(cjk_claim) >= 2 and cjk_claim[-2:] not in evidence_text:
                return False
        width = 2 if is_cjk else 3
        if len(claim_text) < width or len(evidence_text) < width:
            return False
        claim_grams = {
            claim_text[index : index + width] for index in range(len(claim_text) - width + 1)
        }
        evidence_grams = {
            evidence_text[index : index + width] for index in range(len(evidence_text) - width + 1)
        }
        overlap = len(claim_grams & evidence_grams)
        required_overlap = 1 if is_cjk else 2
        return overlap >= required_overlap and overlap / min(
            len(claim_grams), len(evidence_grams)
        ) >= (0.2 if is_cjk else 0.3)

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
        if source_type is MemorySourceType.REBUILD:
            return MemoryEvidenceRelation.REBUILD
        if source_type is MemorySourceType.EXPLICIT:
            return MemoryEvidenceRelation.EXPLICIT_COMMAND
        if authority is MemoryAuthority.THIRD_PARTY:
            return MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
        if authority is MemoryAuthority.GROUP_REPORT:
            return MemoryEvidenceRelation.GROUP_STATEMENT
        return MemoryEvidenceRelation.SELF_STATEMENT
