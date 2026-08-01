"""Strict provider-neutral domain objects for Memory V2."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryJobStatus,
    MemoryKind,
    MemoryResolutionAction,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySemanticRelation,
    MemorySourceType,
    MemoryStateAction,
    MemoryStatus,
    MemoryTargetRole,
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
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT
    status: MemoryStatus
    conflict_state: MemoryConflictState = MemoryConflictState.CLEAR
    supersedes_id: int | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_confirmed_at: datetime = Field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    invalidated_reason: MemoryInvalidationReason | None = None
    last_used_at: datetime | None = None
    evidence_count: int = Field(default=0, ge=0)

    @field_validator(
        "valid_from",
        "valid_until",
        "created_at",
        "updated_at",
        "last_confirmed_at",
        "last_used_at",
        mode="after",
    )
    @classmethod
    def _normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> MemoryFact:
        _validate_fact_lifecycle(
            status=self.status,
            conflict_state=self.conflict_state,
            invalidated_reason=self.invalidated_reason,
        )
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_from > self.valid_until
        ):
            raise ValueError("memory valid_from must not be after valid_until")
        return self

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
    confidence: float = Field(ge=0, le=1)
    authority: MemoryAuthority
    excerpt: str
    created_at: datetime


class MemoryEvidenceCreate(_MemoryModel):
    event_id: int = Field(gt=0)
    source_speaker_user_id: str
    relation: MemoryEvidenceRelation
    confidence: float = Field(default=1.0, ge=0, le=1)
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT
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
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT
    status: MemoryStatus = MemoryStatus.ACTIVE
    conflict_state: MemoryConflictState = MemoryConflictState.CLEAR
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("valid_from", "valid_until", mode="after")
    @classmethod
    def _normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

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
        _validate_fact_lifecycle(
            status=self.status,
            conflict_state=self.conflict_state,
            invalidated_reason=None,
        )
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_from > self.valid_until
        ):
            raise ValueError("memory valid_from must not be after valid_until")
        return self


class MemoryFactRelation(_MemoryModel):
    id: int = Field(gt=0)
    source_fact_id: int = Field(gt=0)
    target_fact_id: int = Field(gt=0)
    relation_type: MemoryFactRelationType
    confidence: float = Field(ge=0, le=1)
    source_event_id: int | None = Field(default=None, gt=0)
    created_at: datetime


class MemoryFactStateEvent(_MemoryModel):
    id: int = Field(gt=0)
    fact_id: int = Field(gt=0)
    action: MemoryStateAction
    from_status: MemoryStatus | None = None
    to_status: MemoryStatus | None = None
    from_conflict_state: MemoryConflictState | None = None
    to_conflict_state: MemoryConflictState | None = None
    reason_code: str
    source_event_id: int | None = Field(default=None, gt=0)
    actor_user_id: str | None = None
    created_at: datetime


class MemoryCandidate(_MemoryModel):
    candidate_ref: str = Field(pattern=r"^candidate_[1-9][0-9]*$")
    fact: MemoryFact
    exact_key: bool = False
    exact_content: bool = False
    relevance: float = 0


class CandidateRelation(_MemoryModel):
    candidate_ref: str = Field(pattern=r"^candidate_[1-9][0-9]*$")
    relation: MemorySemanticRelation
    confidence: float = Field(ge=0, le=1)


class MemoryRelationClassification(_MemoryModel):
    relations: tuple[CandidateRelation, ...] = ()


class MemoryResolutionPlan(_MemoryModel):
    action: MemoryResolutionAction
    existing_fact_id: int | None = Field(default=None, gt=0)
    new_fact_status: MemoryStatus | None = None
    new_conflict_state: MemoryConflictState | None = None
    existing_status: MemoryStatus | None = None
    existing_conflict_state: MemoryConflictState | None = None
    relation_types: tuple[MemoryFactRelationType, ...] = ()
    reason_code: str
    append_evidence: bool = True
    create_new_fact: bool = False


class MemoryConsistencyHealth(_MemoryModel):
    active_slot_conflicts: int = Field(ge=0)
    contested_fact_count: int = Field(ge=0)
    active_contested_count: int = Field(ge=0)
    orphan_relation_count: int = Field(ge=0)
    cross_target_relation_count: int = Field(ge=0)
    orphan_state_event_count: int = Field(ge=0)
    invalidated_without_reason_count: int = Field(ge=0)
    superseded_without_chain_count: int = Field(ge=0)
    evidence_authority_mismatch_count: int = Field(ge=0)
    expired_active_count: int = Field(ge=0)
    stale_backlog_count: int = Field(ge=0)
    classifier_recent_errors: int = Field(ge=0)
    maintenance_last_success_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return not any(
            (
                self.active_slot_conflicts,
                self.orphan_relation_count,
                self.cross_target_relation_count,
                self.orphan_state_event_count,
                self.invalidated_without_reason_count,
                self.superseded_without_chain_count,
                self.expired_active_count,
            )
        )


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


class MemoryEntityTarget(_MemoryModel):
    role: MemoryTargetRole
    scope_type: MemoryScopeType
    subject_user_id: str | None = None
    group_id: str | None = None
    block_id: str

    @model_validator(mode="after")
    def _validate_scope(self) -> MemoryEntityTarget:
        MemoryFactQuery(
            scope_type=self.scope_type,
            subject_user_id=self.subject_user_id,
            group_id=self.group_id,
        )
        expected = {
            MemoryTargetRole.CURRENT_PERSON: MemoryScopeType.PERSON,
            MemoryTargetRole.CURRENT_PERSON_GROUP: MemoryScopeType.PERSON_GROUP,
            MemoryTargetRole.CURRENT_GROUP: MemoryScopeType.GROUP,
            MemoryTargetRole.REFERENCED_PERSON: MemoryScopeType.PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP: MemoryScopeType.PERSON_GROUP,
        }[self.role]
        if self.scope_type is not expected:
            raise ValueError("memory target role does not match its scope")
        return self


class MemoryQuery(_MemoryModel):
    text: str
    normalized_text: str
    mode: MemoryRetrievalMode
    targets: tuple[MemoryEntityTarget, ...]
    kinds: tuple[MemoryKind, ...] = ()
    candidate_limit: int = Field(gt=0)
    limit_per_target: int = Field(gt=0)
    always_on_explicit_preference_limit: int = Field(ge=0)
    query_term_limit: int = Field(gt=0)
    short_query_fallback_enabled: bool = True
    semantic_enabled: bool = True
    semantic_candidate_limit: int = Field(default=50, gt=0)
    semantic_min_similarity: float = Field(default=0.35, ge=-1, le=1)
    hybrid_lexical_weight: float = Field(default=1.0, ge=0)
    hybrid_semantic_weight: float = Field(default=1.0, ge=0)
    hybrid_rrf_k: int = Field(default=60, gt=0)


class MemoryLexicalCandidate(_MemoryModel):
    fact_id: int = Field(gt=0)
    target: MemoryEntityTarget
    fts_rank: float
    exact_match: bool = False
    matched_terms: tuple[str, ...] = ()


class MemoryRetrievalHit(_MemoryModel):
    fact: MemoryFact
    target: MemoryEntityTarget
    rank: int = Field(gt=0)
    lexical_score: float | None = None
    semantic_score: float | None = None
    fusion_score: float = 0
    lexical_rank: int | None = Field(default=None, gt=0)
    semantic_rank: int | None = Field(default=None, gt=0)
    sources: tuple[str, ...] = ()
    exact_match: bool = False
    matched_terms: tuple[str, ...] = ()
    selection_reason: str


class MemoryRetrievalBlock(_MemoryModel):
    target: MemoryEntityTarget
    hits: tuple[MemoryRetrievalHit, ...] = ()


class MemoryRetrievalResult(_MemoryModel):
    blocks: tuple[MemoryRetrievalBlock, ...]
    hits: tuple[MemoryRetrievalHit, ...]
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    query_hash: str
    mode: MemoryRetrievalMode
    semantic_status: str = "disabled"
    semantic_degraded: bool = False
    embedding_profile: str | None = None


class MemoryIndexHealth(_MemoryModel):
    fact_count: int = Field(ge=0)
    indexed_row_count: int = Field(ge=0)
    missing_row_count: int = Field(ge=0)
    orphan_row_count: int = Field(ge=0)

    @property
    def healthy(self) -> bool:
        return self.missing_row_count == 0 and self.orphan_row_count == 0


def _validate_fact_lifecycle(
    *,
    status: MemoryStatus,
    conflict_state: MemoryConflictState,
    invalidated_reason: MemoryInvalidationReason | None,
) -> None:
    if status is MemoryStatus.CONTESTED and conflict_state is not MemoryConflictState.CONTESTED:
        raise ValueError("contested memory status requires contested conflict state")
    if status is MemoryStatus.INVALIDATED and invalidated_reason is None:
        raise ValueError("invalidated memory fact requires an invalidation reason")
    if status is not MemoryStatus.INVALIDATED and invalidated_reason is not None:
        raise ValueError("invalidation reason is only valid for invalidated memory facts")
