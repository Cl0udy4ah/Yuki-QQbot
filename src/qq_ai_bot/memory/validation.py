"""Backend validation that prevents model-selected identity attribution."""

from __future__ import annotations

import re

from qq_ai_bot.memory.enums import MemoryEvidenceRelation, MemorySourceType
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.subjects import SubjectResolver
from qq_ai_bot.persistence.repository_records import EventRecord

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def normalize_memory_text(value: str, *, maximum: int) -> str:
    """Flatten untrusted model/user text before persistence and prompt use."""

    return " ".join(_CONTROL.sub(" ", value).split())[:maximum].strip()


class MemoryClaimValidator:
    """Turn a claim into trusted persistence input or reject it without guessing."""

    def __init__(self, resolver: SubjectResolver | None = None) -> None:
        self._resolver = resolver or SubjectResolver()

    def validate(
        self,
        claim: MemoryClaim,
        event: EventRecord,
    ) -> tuple[MemoryFactCreate, MemoryEvidenceCreate] | None:
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
            source_type=claim.source_type,
        )
        relation = (
            MemoryEvidenceRelation.EXPLICIT_COMMAND
            if claim.source_type is MemorySourceType.EXPLICIT
            else MemoryEvidenceRelation.SELF_STATEMENT
        )
        evidence = MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id=event.sender_user_id,
            relation=relation,
            excerpt=normalize_memory_text(event.content, maximum=500),
        )
        return fact, evidence
