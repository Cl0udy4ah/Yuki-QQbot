"""Stable request, actor, receipt, and result contracts for memory mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, MemoryStatus
from qq_ai_bot.persistence.repository_records import EventRecord


class MemoryMutationOperation(StrEnum):
    CREATE = "create"
    CORRECT = "correct"
    INVALIDATE = "invalidate"
    RESTORE = "restore"
    CONTEST = "contest"
    MERGE = "merge"
    REASSIGN = "reassign"
    UPDATE_METADATA = "update_metadata"


class MemoryMutationAppliedOperation(StrEnum):
    CREATE = "create"
    CORRECT = "correct"
    INVALIDATE = "invalidate"
    RESTORE = "restore"
    CONTEST = "contest"
    MERGE = "merge"
    REASSIGN = "reassign"
    UPDATE_METADATA = "update_metadata"
    MERGE_EVIDENCE = "merge_evidence"
    NOOP = "noop"


class MemoryMutationOutcome(StrEnum):
    PROCESSING = "processing"
    COMMITTED = "committed"
    COMMITTED_AS_CONTESTED = "committed_as_contested"
    DEDUPLICATED = "deduplicated"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"


class MemoryDecisionActorType(StrEnum):
    AGENT = "agent"
    WORKER = "worker"
    COMMAND = "command"
    ADMIN = "admin"
    PLUGIN = "plugin"
    REFLECTION = "reflection"
    SYSTEM = "system"


class SelfMemoryVisibilityMode(StrEnum):
    CURRENT_SCOPE = "current_scope"
    GLOBAL = "global"


SELF_MEMORY_CATEGORIES: tuple[str, ...] = (
    "self_fact",
    "self_preference",
    "self_episode",
    "self_reflection",
    "self_principle",
)


class _MutationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryMutationTarget(_MutationModel):
    """A model-safe alias and scope; it never carries raw QQ or group IDs."""

    subject_ref: str = Field(min_length=1, max_length=32)
    scope_type: MemoryScopeType


class MemoryMutationRequest(_MutationModel):
    """One requested semantic operation from any Memory V2 write entrypoint."""

    operation: MemoryMutationOperation
    fact_id: int | None = Field(default=None, ge=1)
    merge_fact_id: int | None = Field(default=None, ge=1)
    target: MemoryMutationTarget | None = None
    visibility: SelfMemoryVisibilityMode | None = None
    new_content: str | None = Field(default=None, max_length=4000)
    memory_key: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    kind: MemoryKind | None = None
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.9, ge=0, le=1)
    importance: int | None = Field(default=None, ge=1, le=5)
    evidence_refs: tuple[str, ...] = ("current_event",)
    evidence_quote: str | None = Field(default=None, max_length=500)
    expected_fact_state: MemoryStatus | None = None
    valid_from: str | None = Field(default=None, max_length=64)
    valid_until: str | None = Field(default=None, max_length=64)


@dataclass(frozen=True, slots=True)
class MemoryMutationContext:
    """Trusted scene and actor provenance that model arguments cannot override."""

    event: EventRecord
    conversation_key: str
    turn_origin: str
    delegation_mode: str
    trigger_actor_user_id: str
    decision_actor_type: MemoryDecisionActorType
    decision_actor_id: str | None
    executed_by_bot_user_id: str
    actor_is_superuser: bool = False


@dataclass(frozen=True, slots=True)
class MemoryMutationReceipt:
    id: int
    mutation_id: str
    idempotency_key: str
    claim_fingerprint: str
    target_fingerprint: str
    trigger_event_id: int
    conversation_key: str
    current_group_id: str | None
    turn_origin: str
    delegation_mode: str
    trigger_actor_user_id: str
    decision_actor_type: MemoryDecisionActorType
    decision_actor_id: str | None
    executed_by_bot_user_id: str | None
    requested_operation: MemoryMutationOperation
    applied_operation: MemoryMutationAppliedOperation
    old_fact_id: int | None
    new_fact_id: int | None
    outcome: MemoryMutationOutcome
    reason_code: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryMutationResult:
    ok: bool
    mutation_id: str | None
    requested_operation: MemoryMutationOperation
    applied_operation: MemoryMutationAppliedOperation
    outcome: MemoryMutationOutcome
    old_fact_id: int | None = None
    new_fact_id: int | None = None
    reason_code: str = ""
    deduplicated: bool = False

    @classmethod
    def from_receipt(
        cls,
        receipt: MemoryMutationReceipt,
        *,
        deduplicated: bool,
        requested_operation: MemoryMutationOperation | None = None,
    ) -> MemoryMutationResult:
        return cls(
            ok=receipt.outcome
            in {
                MemoryMutationOutcome.COMMITTED,
                MemoryMutationOutcome.COMMITTED_AS_CONTESTED,
                MemoryMutationOutcome.DEDUPLICATED,
            },
            mutation_id=receipt.mutation_id,
            requested_operation=requested_operation or receipt.requested_operation,
            applied_operation=receipt.applied_operation,
            outcome=(MemoryMutationOutcome.DEDUPLICATED if deduplicated else receipt.outcome),
            old_fact_id=receipt.old_fact_id,
            new_fact_id=receipt.new_fact_id,
            reason_code=receipt.reason_code,
            deduplicated=deduplicated,
        )
