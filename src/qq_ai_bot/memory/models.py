"""Strict provider-neutral domain objects for Memory V2."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.memory.enums import (
    MemoryEvidenceRelation,
    MemoryJobStatus,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.persistence.repository_records import EventRecord


class _MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryFact(_MemoryModel):
    id: int = Field(gt=0)
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    kind: MemoryKind
    memory_key: str
    category: str
    content: str
    normalized_content: str
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    source_type: MemorySourceType
    status: MemoryStatus
    supersedes_id: int | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)

    @property
    def user_id(self) -> str | None:
        """Compatibility projection used by Plugin API v1."""

        return self.subject_user_id

    @property
    def key(self) -> str:
        return self.memory_key

    @property
    def value(self) -> str:
        return self.content


class MemoryEvidence(_MemoryModel):
    id: int = Field(gt=0)
    fact_id: int = Field(gt=0)
    event_id: int = Field(gt=0)
    source_speaker_user_id: str
    relation: MemoryEvidenceRelation
    excerpt: str
    created_at: datetime


class MemoryEvidenceCreate(_MemoryModel):
    event_id: int = Field(gt=0)
    source_speaker_user_id: str
    relation: MemoryEvidenceRelation
    excerpt: str


class MemoryFactCreate(_MemoryModel):
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    kind: MemoryKind = MemoryKind.FACT
    memory_key: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4000)
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_type: MemorySourceType
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _validate_scope(self) -> MemoryFactCreate:
        if self.scope_type is MemoryScopeType.PERSON:
            valid = bool(self.subject_user_id) and self.group_id is None
        elif self.scope_type is MemoryScopeType.PERSON_GROUP:
            valid = bool(self.subject_user_id) and bool(self.group_id)
        else:
            valid = self.subject_user_id is None and bool(self.group_id)
        if not valid:
            raise ValueError("memory fact identity does not match its scope")
        return self


class MemoryFactQuery(_MemoryModel):
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    kind: MemoryKind | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE

    @model_validator(mode="after")
    def _validate_scope(self) -> MemoryFactQuery:
        MemoryFactCreate(
            scope_type=self.scope_type,
            subject_user_id=self.subject_user_id,
            group_id=self.group_id,
            kind=self.kind or MemoryKind.FACT,
            memory_key="query",
            category="query",
            content="query",
            source_type=MemorySourceType.AUTOMATIC,
        )
        return self


class MemoryJob(_MemoryModel):
    id: int = Field(gt=0)
    event_id: int = Field(gt=0)
    conversation_key: str
    status: MemoryJobStatus
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    error_category: str | None = None
    event: EventRecord


class MemoryContextBlock(_MemoryModel):
    entity: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    facts: tuple[MemoryFact, ...] = ()
