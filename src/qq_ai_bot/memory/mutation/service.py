"""Unified, auditable, and idempotent orchestration for Memory V2 writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryProcessingSource,
    MemoryResolutionAction,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    MemoryTemporalMode,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import (
    MemoryCandidate,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryResolutionPlan,
)
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationResult,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.mutation.repository import MemoryMutationReceiptRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import ResolvedSubject, SubjectResolver
from qq_ai_bot.memory.temporal import MemoryTemporalResolver
from qq_ai_bot.memory.validation import (
    ValidatedMemoryClaim,
    event_requests_explicit_memory,
    normalize_memory_text,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord

logger = logging.getLogger(__name__)


class MemoryMutationRejected(ValueError):
    """A stable policy or request rejection safe to return to the main Agent."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class _PreparedMutation:
    request: MemoryMutationRequest
    context: MemoryMutationContext
    subject_ref: str
    target: ResolvedSubject
    fact: MemoryFact | None
    merge_fact: MemoryFact | None
    evidence: MemoryEvidenceCreate
    claim: ValidatedMemoryClaim | None
    idempotency_key: str
    claim_fingerprint: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class _AppliedMutation:
    operation: MemoryMutationAppliedOperation
    outcome: MemoryMutationOutcome
    old_fact_id: int | None
    new_fact_id: int | None
    reason_code: str


class MemoryMutationService:
    """The only supported orchestration boundary for durable Memory V2 changes."""

    def __init__(
        self,
        *,
        settings: Settings,
        facts: MemoryFactService,
        processor: MemoryClaimProcessor,
        ledger: EventLedgerRepository,
        receipts: MemoryMutationReceiptRepository | None = None,
        subject_resolver: SubjectResolver | None = None,
        temporal_resolver: MemoryTemporalResolver | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._processor = processor
        self._ledger = ledger
        self._receipts = receipts or MemoryMutationReceiptRepository(facts.repository.database)
        self._subjects = subject_resolver or SubjectResolver()
        self._temporal = temporal_resolver or MemoryTemporalResolver()
        self._lock = asyncio.Lock()

    async def mutate(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
    ) -> MemoryMutationResult:
        """Validate, resolve, commit, and receipt one requested mutation."""

        try:
            prepared = await self._prepare(request, context)
        except MemoryMutationRejected as exc:
            return self._rejected(request.operation, exc.reason_code)
        return await self._commit_prepared(prepared)

    async def mutate_resolved(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        target: ResolvedSubject,
    ) -> MemoryMutationResult:
        """Apply a trusted command/admin/plugin target through the same boundary."""

        try:
            prepared = await self._prepare(request, context, target_override=target)
        except MemoryMutationRejected as exc:
            return self._rejected(request.operation, exc.reason_code)
        return await self._commit_prepared(prepared)

    async def _commit_prepared(
        self,
        prepared: _PreparedMutation,
    ) -> MemoryMutationResult:
        request = prepared.request
        context = prepared.context
        async with self._lock:
            existing = await self._receipts.find(
                idempotency_key=prepared.idempotency_key,
                claim_fingerprint=prepared.claim_fingerprint,
            )
            if existing is not None:
                return MemoryMutationResult.from_receipt(
                    existing,
                    deduplicated=True,
                    requested_operation=request.operation,
                )
            try:
                async with self._facts.repository.transaction() as session:
                    duplicate = await self._receipts.find(
                        idempotency_key=prepared.idempotency_key,
                        claim_fingerprint=prepared.claim_fingerprint,
                        session=session,
                    )
                    if duplicate is not None:
                        return MemoryMutationResult.from_receipt(
                            duplicate,
                            deduplicated=True,
                            requested_operation=request.operation,
                        )
                    reserved = await self._receipts.reserve(
                        mutation_id=str(uuid.uuid4()),
                        idempotency_key=prepared.idempotency_key,
                        claim_fingerprint=prepared.claim_fingerprint,
                        target_fingerprint=prepared.target_fingerprint,
                        trigger_event_id=context.event.id,
                        conversation_key=context.conversation_key,
                        current_group_id=context.event.group_id,
                        turn_origin=context.turn_origin,
                        delegation_mode=context.delegation_mode,
                        trigger_actor_user_id=context.trigger_actor_user_id,
                        decision_actor_type=context.decision_actor_type,
                        decision_actor_id=context.decision_actor_id,
                        executed_by_bot_user_id=context.executed_by_bot_user_id,
                        requested_operation=request.operation,
                        created_at=datetime.now(UTC),
                        session=session,
                    )
                    applied = await self._apply(prepared, session=session)
                    receipt = await self._receipts.finalize(
                        reserved.id,
                        applied_operation=applied.operation,
                        old_fact_id=applied.old_fact_id,
                        new_fact_id=applied.new_fact_id,
                        outcome=applied.outcome,
                        reason_code=applied.reason_code,
                        session=session,
                    )
            except IntegrityError:
                duplicate = await self._receipts.find(
                    idempotency_key=prepared.idempotency_key,
                    claim_fingerprint=prepared.claim_fingerprint,
                )
                if duplicate is None:
                    raise
                return MemoryMutationResult.from_receipt(
                    duplicate,
                    deduplicated=True,
                    requested_operation=request.operation,
                )
        await self._schedule_embedding_after_commit(receipt.new_fact_id)
        return MemoryMutationResult.from_receipt(receipt, deduplicated=False)

    async def mutate_validated_claim(
        self,
        claim: ValidatedMemoryClaim,
        processing_context: MemoryProcessingContext,
        *,
        conversation_key: str,
    ) -> MemoryMutationResult:
        """Commit one Worker claim through the same receipt and transaction boundary."""

        event = processing_context.event
        operation = self._claim_requested_operation(claim.operation)
        if (
            event.direction != "inbound"
            or event.sender_user_id == event.bot_user_id
            or await self._ledger.sender_is_bot(event.sender_user_id)
            or not self._validated_claim_matches_event(claim, event)
        ):
            return self._rejected(operation, "untrusted_trigger_event")
        target_payload = {
            "scope_type": claim.fact.scope_type.value,
            "subject_user_id": claim.fact.subject_user_id,
            "group_id": claim.fact.group_id,
        }
        common = {
            "event_id": event.id,
            "target": target_payload,
            "memory_key": normalize_memory_text(claim.fact.memory_key, maximum=128),
            "content": normalize_memory_text(
                claim.fact.content,
                maximum=4000,
            ).casefold(),
        }
        claim_fingerprint = _fingerprint(common)
        idempotency_key = _fingerprint({**common, "operation": operation.value})
        target_fingerprint = _fingerprint(target_payload)
        async with self._lock:
            existing = await self._receipts.find(
                idempotency_key=idempotency_key,
                claim_fingerprint=claim_fingerprint,
            )
            if existing is not None:
                return MemoryMutationResult.from_receipt(
                    existing,
                    deduplicated=True,
                    requested_operation=operation,
                )
            try:
                async with self._facts.repository.transaction() as session:
                    duplicate = await self._receipts.find(
                        idempotency_key=idempotency_key,
                        claim_fingerprint=claim_fingerprint,
                        session=session,
                    )
                    if duplicate is not None:
                        return MemoryMutationResult.from_receipt(
                            duplicate,
                            deduplicated=True,
                            requested_operation=operation,
                        )
                    reserved = await self._receipts.reserve(
                        mutation_id=str(uuid.uuid4()),
                        idempotency_key=idempotency_key,
                        claim_fingerprint=claim_fingerprint,
                        target_fingerprint=target_fingerprint,
                        trigger_event_id=event.id,
                        conversation_key=conversation_key,
                        current_group_id=event.group_id,
                        turn_origin=event.origin,
                        delegation_mode="automatic_extraction",
                        trigger_actor_user_id=event.sender_user_id,
                        decision_actor_type=MemoryDecisionActorType.WORKER,
                        decision_actor_id="memory_worker",
                        executed_by_bot_user_id=event.bot_user_id,
                        requested_operation=operation,
                        created_at=datetime.now(UTC),
                        session=session,
                    )
                    processed = await self._processor.process(
                        claim,
                        processing_context,
                        session=session,
                    )
                    applied = self._claim_applied(
                        action=processed.action,
                        fact_id=processed.fact_id,
                        reason_code=processed.reason_code,
                    )
                    receipt = await self._receipts.finalize(
                        reserved.id,
                        applied_operation=applied.operation,
                        old_fact_id=applied.old_fact_id,
                        new_fact_id=applied.new_fact_id,
                        outcome=applied.outcome,
                        reason_code=applied.reason_code,
                        session=session,
                    )
            except IntegrityError:
                duplicate = await self._receipts.find(
                    idempotency_key=idempotency_key,
                    claim_fingerprint=claim_fingerprint,
                )
                if duplicate is None:
                    raise
                return MemoryMutationResult.from_receipt(
                    duplicate,
                    deduplicated=True,
                    requested_operation=operation,
                )
        await self._schedule_embedding_after_commit(receipt.new_fact_id)
        return MemoryMutationResult.from_receipt(receipt, deduplicated=False)

    async def mutate_reflection(
        self,
        fact: MemoryFact,
        *,
        operation: MemoryMutationOperation,
        reason: MemoryInvalidationReason,
    ) -> MemoryMutationResult:
        """Apply one bounded background-governance decision using existing evidence."""

        evidence_rows = await self._facts.list_evidence(fact.id, limit=20)
        evidence = next((row for row in evidence_rows if row.event_id is not None), None)
        if evidence is None or evidence.event_id is None:
            return self._rejected(operation, "reflection_evidence_not_found")
        event = await self._ledger.get_event(evidence.event_id)
        if event is None:
            return self._rejected(operation, "reflection_trigger_event_not_found")
        quote = (
            evidence.excerpt
            if evidence.excerpt and evidence.excerpt in event.content
            else event.content[:500]
        )
        return await self.mutate_resolved(
            MemoryMutationRequest(
                operation=operation,
                fact_id=fact.id,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=fact.scope_type,
                ),
                reason=reason.value,
                evidence_quote=quote,
            ),
            MemoryMutationContext(
                event=event,
                conversation_key=(
                    f"group:{fact.group_id}:reflection"
                    if fact.group_id is not None
                    else f"private:{fact.subject_user_id}:reflection"
                ),
                turn_origin="memory_reflection",
                delegation_mode="bounded_background_reflection",
                trigger_actor_user_id=event.sender_user_id,
                decision_actor_type=MemoryDecisionActorType.REFLECTION,
                decision_actor_id="memory_maintenance",
                executed_by_bot_user_id=event.bot_user_id,
            ),
            target=ResolvedSubject(
                fact.scope_type,
                fact.subject_user_id,
                fact.group_id,
            ),
        )

    async def _prepare(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        target_override: ResolvedSubject | None = None,
    ) -> _PreparedMutation:
        event = context.event
        if (
            event.direction != "inbound"
            or event.sender_user_id != context.trigger_actor_user_id
            or event.bot_user_id != context.executed_by_bot_user_id
            or event.sender_user_id == event.bot_user_id
            or await self._ledger.sender_is_bot(event.sender_user_id)
        ):
            raise MemoryMutationRejected("untrusted_trigger_event")
        if tuple(dict.fromkeys(request.evidence_refs)) != ("current_event",):
            raise MemoryMutationRejected("unsupported_evidence_reference")
        if request.target is None:
            raise MemoryMutationRejected("target_required")
        subject_ref = self._normalize_subject_ref(request.target.subject_ref, event)
        target = target_override or self._subjects.resolve(
            event,
            subject_ref=subject_ref,
            scope_type=request.target.scope_type,
        )
        if target is None:
            raise MemoryMutationRejected("target_not_available_in_current_event")
        if request.target.scope_type is not target.scope_type:
            raise MemoryMutationRejected("target_scope_mismatch")
        self._authorize(request.operation, target, context)
        fact = await self._load_fact(request.fact_id)
        merge_fact = await self._load_fact(request.merge_fact_id)
        self._validate_fact_requirements(request, target, fact, merge_fact, context)
        quote = self._evidence_quote(request, event.content)
        authority, source_type = self._provenance(target, context)
        evidence = MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id=event.sender_user_id,
            relation=self._evidence_relation(request.operation, authority, source_type),
            confidence=request.confidence,
            authority=authority,
            excerpt=quote,
        )
        claim = self._validated_claim(
            request,
            context,
            subject_ref=subject_ref,
            fact=fact,
            source_type=source_type,
            evidence=evidence,
            target_override=target if target_override is not None else None,
        )
        content = normalize_memory_text(
            request.new_content or (fact.content if fact is not None else ""),
            maximum=4000,
        )
        key = normalize_memory_text(
            request.memory_key or (fact.memory_key if fact is not None else ""),
            maximum=128,
        )
        target_payload = {
            "scope_type": target.scope_type.value,
            "subject_user_id": target.subject_user_id,
            "group_id": target.group_id,
        }
        target_fingerprint = _fingerprint(target_payload)
        common = {
            "event_id": event.id,
            "target": target_payload,
            "memory_key": key,
            "content": content.casefold(),
        }
        if context.decision_actor_type in {
            MemoryDecisionActorType.REFLECTION,
            MemoryDecisionActorType.SYSTEM,
        }:
            common["decision_namespace"] = context.decision_actor_type.value
        claim_fingerprint = _fingerprint(common)
        idempotency_key = _fingerprint(
            {
                **common,
                "operation": request.operation.value,
                "fact_id": request.fact_id,
                "merge_fact_id": request.merge_fact_id,
            }
        )
        return _PreparedMutation(
            request=request,
            context=context,
            subject_ref=subject_ref,
            target=target,
            fact=fact,
            merge_fact=merge_fact,
            evidence=evidence,
            claim=claim,
            idempotency_key=idempotency_key,
            claim_fingerprint=claim_fingerprint,
            target_fingerprint=target_fingerprint,
        )

    async def _apply(
        self,
        prepared: _PreparedMutation,
        *,
        session: AsyncSession,
    ) -> _AppliedMutation:
        operation = prepared.request.operation
        if operation in {MemoryMutationOperation.CREATE, MemoryMutationOperation.CORRECT}:
            claim = prepared.claim
            if claim is None:
                raise MemoryMutationRejected("validated_claim_required")
            processed = await self._processor.process(
                claim,
                MemoryProcessingContext(
                    source=MemoryProcessingSource.LIVE,
                    event=prepared.context.event,
                ),
                session=session,
            )
            return self._claim_result(
                prepared, processed.action, processed.fact_id, processed.reason_code
            )
        if operation is MemoryMutationOperation.CONTEST and prepared.request.new_content:
            claim = prepared.claim
            fact = prepared.fact
            if claim is None or fact is None:
                raise MemoryMutationRejected("contest_fact_required")
            result = await self._facts.apply_claim(
                claim,
                candidates=(
                    MemoryCandidate(
                        candidate_ref="candidate_1",
                        fact=fact,
                        exact_key=True,
                    ),
                ),
                plan=MemoryResolutionPlan(
                    action=MemoryResolutionAction.CONTEST,
                    existing_fact_id=fact.id,
                    new_fact_status=MemoryStatus.CONTESTED,
                    new_conflict_state=MemoryConflictState.CONTESTED,
                    existing_status=MemoryStatus.ACTIVE,
                    existing_conflict_state=MemoryConflictState.CONTESTED,
                    relation_types=(MemoryFactRelationType.CONTRADICTS,),
                    reason_code="agent_requested_contest",
                    append_evidence=True,
                    create_new_fact=True,
                ),
                limit=self._scope_limit(claim.fact.scope_type),
                session=session,
            )
            return _AppliedMutation(
                MemoryMutationAppliedOperation.CONTEST,
                MemoryMutationOutcome.COMMITTED_AS_CONTESTED,
                fact.id,
                result.id if result is not None else None,
                "agent_requested_contest",
            )
        if operation is MemoryMutationOperation.CONTEST:
            fact = self._required_fact(prepared)
            changed = await self._facts.contest_fact(
                fact.id,
                reason_code="agent_requested_contest",
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.CONTEST,
                changed=changed,
                old_fact_id=fact.id,
                new_fact_id=fact.id,
                reason_code="agent_requested_contest",
                contested=True,
            )
        if operation is MemoryMutationOperation.INVALIDATE:
            fact = self._required_fact(prepared)
            invalidation_reason = self._invalidation_reason(prepared)
            changed = await self._facts.invalidate_fact(
                fact.id,
                reason=invalidation_reason,
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.INVALIDATE,
                changed=changed,
                old_fact_id=fact.id,
                new_fact_id=None,
                reason_code=invalidation_reason.value,
            )
        if operation is MemoryMutationOperation.RESTORE:
            fact = self._required_fact(prepared)
            restored = await self._facts.restore_fact(
                fact.id,
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                confirmed_at=prepared.context.event.occurred_at,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.RESTORE,
                changed=restored is not None,
                old_fact_id=fact.id,
                new_fact_id=restored.id if restored is not None else None,
                reason_code="explicit_restore",
            )
        if operation is MemoryMutationOperation.MERGE:
            fact = self._required_fact(prepared)
            merge_fact = prepared.merge_fact
            if merge_fact is None:
                raise MemoryMutationRejected("merge_fact_required")
            merged = await self._facts.merge_facts(
                fact.id,
                merge_fact.id,
                actor_user_id=prepared.context.trigger_actor_user_id,
                evidence=prepared.evidence,
                confirmed_at=prepared.context.event.occurred_at,
                session=session,
            )
            return self._direct_result(
                operation=MemoryMutationAppliedOperation.MERGE,
                changed=merged is not None,
                old_fact_id=fact.id,
                new_fact_id=merged.id if merged is not None else None,
                reason_code="merged",
            )
        if operation in {
            MemoryMutationOperation.REASSIGN,
            MemoryMutationOperation.UPDATE_METADATA,
        }:
            return await self._version(prepared, session=session)
        raise MemoryMutationRejected("unsupported_operation")

    async def _version(
        self,
        prepared: _PreparedMutation,
        *,
        session: AsyncSession,
    ) -> _AppliedMutation:
        fact = self._required_fact(prepared)
        request = prepared.request
        reassign = request.operation is MemoryMutationOperation.REASSIGN
        target = (
            prepared.target
            if reassign
            else ResolvedSubject(
                fact.scope_type,
                fact.subject_user_id,
                fact.group_id,
            )
        )
        authority, source_type = self._provenance(target, prepared.context)
        temporal = None
        if request.valid_from is not None or request.valid_until is not None:
            temporal = self._temporal.resolve(
                mode=(
                    MemoryTemporalMode.TEMPORARY
                    if request.valid_until is not None
                    else MemoryTemporalMode.PERSISTENT
                ),
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                occurred_at=prepared.context.event.occurred_at,
                timezone_name=self._settings.default_timezone,
            )
        replacement = MemoryFactCreate(
            scope_type=target.scope_type,
            subject_user_id=target.subject_user_id,
            group_id=target.group_id,
            kind=request.kind or fact.kind,
            memory_key=normalize_memory_text(
                request.memory_key or fact.memory_key,
                maximum=128,
            ),
            category=normalize_memory_text(request.category or fact.category, maximum=64),
            content=normalize_memory_text(
                request.new_content or fact.content,
                maximum=4000,
            ),
            importance=request.importance or fact.importance,
            confidence=request.confidence,
            source_type=source_type,
            authority=authority,
            valid_from=temporal.valid_from if temporal is not None else fact.valid_from,
            valid_until=temporal.valid_until if temporal is not None else fact.valid_until,
        )
        versioned = await self._facts.version_fact(
            fact.id,
            replacement=replacement,
            evidence=prepared.evidence,
            actor_user_id=prepared.context.trigger_actor_user_id,
            reason_code="memory_reassigned" if reassign else "metadata_updated",
            limit=self._scope_limit(replacement.scope_type),
            copy_existing_evidence=True,
            copied_evidence_authority=authority if reassign else None,
            confirmed_at=prepared.context.event.occurred_at,
            session=session,
        )
        operation = (
            MemoryMutationAppliedOperation.REASSIGN
            if reassign
            else MemoryMutationAppliedOperation.UPDATE_METADATA
        )
        return self._direct_result(
            operation=operation,
            changed=versioned is not None,
            old_fact_id=fact.id,
            new_fact_id=versioned.id if versioned is not None else None,
            reason_code="memory_reassigned" if reassign else "metadata_updated",
        )

    def _validated_claim(
        self,
        request: MemoryMutationRequest,
        context: MemoryMutationContext,
        *,
        subject_ref: str,
        fact: MemoryFact | None,
        source_type: MemorySourceType,
        evidence: MemoryEvidenceCreate,
        target_override: ResolvedSubject | None,
    ) -> ValidatedMemoryClaim | None:
        if request.operation not in {
            MemoryMutationOperation.CREATE,
            MemoryMutationOperation.CORRECT,
            MemoryMutationOperation.CONTEST,
        }:
            return None
        if (
            request.operation is MemoryMutationOperation.CONTEST
            and request.new_content is None
        ):
            return None
        content = normalize_memory_text(request.new_content or "", maximum=4000)
        key = normalize_memory_text(
            request.memory_key or (fact.memory_key if fact is not None else ""),
            maximum=128,
        )
        category = normalize_memory_text(
            request.category or (fact.category if fact is not None else ""),
            maximum=64,
        )
        if not content or not key or not category:
            raise MemoryMutationRejected("memory_content_key_and_category_required")
        target = request.target
        if target is None:
            raise MemoryMutationRejected("target_required")
        quote = self._evidence_quote(request, context.event.content)
        claim = MemoryClaim(
            operation=(
                MemoryClaimOperation.ASSERT
                if request.operation is MemoryMutationOperation.CREATE
                else MemoryClaimOperation.CORRECT
            ),
            subject_ref=subject_ref,
            scope_type=target.scope_type,
            kind=request.kind or (fact.kind if fact is not None else MemoryKind.FACT),
            memory_key=key,
            category=category,
            content=content,
            evidence_quote=quote,
            importance=request.importance or (fact.importance if fact is not None else 3),
            confidence=request.confidence,
            source_type=source_type,
            temporal_mode=(
                MemoryTemporalMode.TEMPORARY
                if request.valid_until is not None
                else MemoryTemporalMode.PERSISTENT
            ),
            valid_from=request.valid_from,
            valid_until=request.valid_until,
        )
        if target_override is not None:
            try:
                temporal = self._temporal.resolve(
                    mode=claim.temporal_mode,
                    valid_from=claim.valid_from,
                    valid_until=claim.valid_until,
                    occurred_at=context.event.occurred_at,
                    timezone_name=self._settings.default_timezone,
                )
            except ValueError as exc:
                raise MemoryMutationRejected("invalid_memory_temporal_range") from exc
            authority, _source_type = self._provenance(target_override, context)
            return ValidatedMemoryClaim(
                operation=claim.operation,
                fact=MemoryFactCreate(
                    scope_type=target_override.scope_type,
                    subject_user_id=target_override.subject_user_id,
                    group_id=target_override.group_id,
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
                ),
                evidence=evidence,
                subject_is_speaker=(
                    target_override.subject_user_id == context.event.sender_user_id
                ),
                occurred_at=context.event.occurred_at,
            )
        validated = self._processor.validate(claim, context.event)
        if validated is None:
            raise MemoryMutationRejected("claim_not_supported_by_current_event")
        return validated

    async def _load_fact(self, fact_id: int | None) -> MemoryFact | None:
        if fact_id is None:
            return None
        fact = await self._facts.get_fact(fact_id)
        if fact is None:
            raise MemoryMutationRejected("memory_fact_not_found")
        return fact

    @staticmethod
    def _validate_fact_requirements(
        request: MemoryMutationRequest,
        target: ResolvedSubject,
        fact: MemoryFact | None,
        merge_fact: MemoryFact | None,
        context: MemoryMutationContext,
    ) -> None:
        if request.operation is MemoryMutationOperation.CREATE:
            if fact is not None or merge_fact is not None:
                raise MemoryMutationRejected("create_does_not_accept_fact_id")
            return
        if fact is None:
            raise MemoryMutationRejected("fact_id_required")
        if (
            request.expected_fact_state is not None
            and fact.status is not request.expected_fact_state
        ):
            raise MemoryMutationRejected("expected_fact_state_mismatch")
        fact_target = (fact.scope_type, fact.subject_user_id, fact.group_id)
        requested_target = (target.scope_type, target.subject_user_id, target.group_id)
        if (
            request.operation is not MemoryMutationOperation.REASSIGN
            and fact_target != requested_target
        ):
            raise MemoryMutationRejected("fact_target_mismatch")
        if request.operation is MemoryMutationOperation.REASSIGN:
            if (
                context.event.group_id is None
                or fact.scope_type is not MemoryScopeType.PERSON_GROUP
                or fact.group_id != context.event.group_id
                or target.scope_type is not MemoryScopeType.PERSON_GROUP
                or target.group_id != context.event.group_id
            ):
                raise MemoryMutationRejected("reassign_must_remain_in_current_group")
            if (
                not context.actor_is_superuser
                and fact.subject_user_id != context.trigger_actor_user_id
                and fact.authority in {MemoryAuthority.EXPLICIT, MemoryAuthority.SELF_REPORT}
            ):
                raise MemoryMutationRejected("third_party_cannot_reassign_subject_owned_fact")
        if request.operation is MemoryMutationOperation.MERGE:
            if merge_fact is None:
                raise MemoryMutationRejected("merge_fact_required")
            merge_target = (
                merge_fact.scope_type,
                merge_fact.subject_user_id,
                merge_fact.group_id,
            )
            if merge_target != requested_target:
                raise MemoryMutationRejected("merge_target_mismatch")
        elif merge_fact is not None:
            raise MemoryMutationRejected("merge_fact_id_only_valid_for_merge")

    @staticmethod
    def _authorize(
        operation: MemoryMutationOperation,
        target: ResolvedSubject,
        context: MemoryMutationContext,
    ) -> None:
        if context.actor_is_superuser or context.decision_actor_type in {
            MemoryDecisionActorType.REFLECTION,
            MemoryDecisionActorType.SYSTEM,
        }:
            return
        if target.subject_user_id == context.trigger_actor_user_id:
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.INVALIDATE,
                MemoryMutationOperation.RESTORE,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
                MemoryMutationOperation.UPDATE_METADATA,
            }
        elif target.scope_type is MemoryScopeType.GROUP:
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.INVALIDATE,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
                MemoryMutationOperation.UPDATE_METADATA,
            }
        else:
            allowed = {
                MemoryMutationOperation.CREATE,
                MemoryMutationOperation.CORRECT,
                MemoryMutationOperation.CONTEST,
                MemoryMutationOperation.MERGE,
                MemoryMutationOperation.REASSIGN,
            }
        if operation not in allowed:
            raise MemoryMutationRejected("operation_not_allowed_for_target")

    @staticmethod
    def _normalize_subject_ref(subject_ref: str, event: EventRecord) -> str:
        normalized = subject_ref.strip().casefold()
        aliases = {
            "current_speaker": "speaker",
            "current_group": "group",
            "replied_message_author": "reply_author",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized == "mentioned_user":
            available = tuple(
                item
                for item in SubjectResolver.available(event)
                if item.subject_ref.startswith("mentioned_")
            )
            if len(available) != 1:
                raise MemoryMutationRejected("mentioned_user_is_ambiguous")
            return available[0].subject_ref
        if normalized.startswith("mentioned_user_"):
            return "mentioned_" + normalized.removeprefix("mentioned_user_")
        return normalized

    @staticmethod
    def _evidence_quote(request: MemoryMutationRequest, event_content: str) -> str:
        source = normalize_memory_text(event_content, maximum=4000)
        if not source:
            raise MemoryMutationRejected("empty_trigger_event")
        if request.evidence_quote is None:
            if len(source) > 500:
                raise MemoryMutationRejected("evidence_quote_required_for_long_event")
            return source
        quote = normalize_memory_text(request.evidence_quote, maximum=500)
        if not quote or quote not in source:
            raise MemoryMutationRejected("evidence_quote_not_in_current_event")
        return quote

    @staticmethod
    def _provenance(
        target: ResolvedSubject,
        context: MemoryMutationContext,
    ) -> tuple[MemoryAuthority, MemorySourceType]:
        if target.subject_user_id and target.subject_user_id != context.trigger_actor_user_id:
            return MemoryAuthority.THIRD_PARTY, MemorySourceType.AUTOMATIC
        if target.scope_type is MemoryScopeType.GROUP:
            return MemoryAuthority.GROUP_REPORT, MemorySourceType.AUTOMATIC
        if event_requests_explicit_memory(context.event.content):
            return MemoryAuthority.EXPLICIT, MemorySourceType.EXPLICIT
        return MemoryAuthority.SELF_REPORT, MemorySourceType.AUTOMATIC

    @staticmethod
    def _evidence_relation(
        operation: MemoryMutationOperation,
        authority: MemoryAuthority,
        source_type: MemorySourceType,
    ) -> MemoryEvidenceRelation:
        if operation is MemoryMutationOperation.INVALIDATE:
            return MemoryEvidenceRelation.RETRACTION
        if operation in {
            MemoryMutationOperation.CORRECT,
            MemoryMutationOperation.CONTEST,
            MemoryMutationOperation.REASSIGN,
            MemoryMutationOperation.UPDATE_METADATA,
        }:
            return MemoryEvidenceRelation.CORRECTION
        if operation in {MemoryMutationOperation.RESTORE, MemoryMutationOperation.MERGE}:
            return MemoryEvidenceRelation.CONFIRMATION
        if source_type is MemorySourceType.EXPLICIT:
            return MemoryEvidenceRelation.EXPLICIT_COMMAND
        if authority is MemoryAuthority.THIRD_PARTY:
            return MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
        if authority is MemoryAuthority.GROUP_REPORT:
            return MemoryEvidenceRelation.GROUP_STATEMENT
        return MemoryEvidenceRelation.SELF_STATEMENT

    def _scope_limit(self, scope_type: MemoryScopeType) -> int:
        if scope_type is MemoryScopeType.PERSON:
            return self._settings.person_memory_max_entries
        if scope_type is MemoryScopeType.GROUP:
            return self._settings.group_memory_max_entries
        return self._settings.person_group_memory_max_entries

    @staticmethod
    def _invalidation_reason(prepared: _PreparedMutation) -> MemoryInvalidationReason:
        context = prepared.context
        requested = prepared.request.reason
        if context.decision_actor_type is MemoryDecisionActorType.PLUGIN:
            return MemoryInvalidationReason.PLUGIN_EXPLICIT_INVALIDATION
        if context.actor_is_superuser:
            try:
                return MemoryInvalidationReason(requested)
            except ValueError:
                return MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
        if context.decision_actor_type in {
            MemoryDecisionActorType.REFLECTION,
            MemoryDecisionActorType.SYSTEM,
        }:
            try:
                return MemoryInvalidationReason(requested)
            except ValueError:
                return MemoryInvalidationReason.STALE
        return MemoryInvalidationReason.USER_RETRACTED

    @staticmethod
    def _required_fact(prepared: _PreparedMutation) -> MemoryFact:
        if prepared.fact is None:
            raise MemoryMutationRejected("fact_id_required")
        return prepared.fact

    @staticmethod
    def _claim_result(
        prepared: _PreparedMutation,
        action: MemoryResolutionAction,
        fact_id: int | None,
        reason_code: str,
    ) -> _AppliedMutation:
        return MemoryMutationService._claim_applied(
            action=action,
            fact_id=fact_id,
            reason_code=reason_code,
            old_fact_id=prepared.fact.id if prepared.fact is not None else None,
        )

    @staticmethod
    def _claim_applied(
        *,
        action: MemoryResolutionAction,
        fact_id: int | None,
        reason_code: str,
        old_fact_id: int | None = None,
    ) -> _AppliedMutation:
        if action is MemoryResolutionAction.CREATE:
            applied = MemoryMutationAppliedOperation.CREATE
            outcome = MemoryMutationOutcome.COMMITTED
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.MERGE_EVIDENCE:
            applied = MemoryMutationAppliedOperation.MERGE_EVIDENCE
            outcome = MemoryMutationOutcome.COMMITTED
            old_fact_id = fact_id
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.SUPERSEDE:
            applied = MemoryMutationAppliedOperation.CORRECT
            outcome = MemoryMutationOutcome.COMMITTED
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.CONTEST:
            applied = MemoryMutationAppliedOperation.CONTEST
            outcome = MemoryMutationOutcome.COMMITTED_AS_CONTESTED
            new_fact_id = fact_id
        elif action is MemoryResolutionAction.INVALIDATE:
            applied = MemoryMutationAppliedOperation.INVALIDATE
            outcome = MemoryMutationOutcome.COMMITTED
            old_fact_id = fact_id or old_fact_id
            new_fact_id = None
        else:
            applied = MemoryMutationAppliedOperation.NOOP
            outcome = MemoryMutationOutcome.NO_CHANGE
            new_fact_id = None
        return _AppliedMutation(applied, outcome, old_fact_id, new_fact_id, reason_code)

    @staticmethod
    def _claim_requested_operation(
        operation: MemoryClaimOperation,
    ) -> MemoryMutationOperation:
        if operation is MemoryClaimOperation.CORRECT:
            return MemoryMutationOperation.CORRECT
        if operation is MemoryClaimOperation.RETRACT:
            return MemoryMutationOperation.INVALIDATE
        return MemoryMutationOperation.CREATE

    @staticmethod
    def _validated_claim_matches_event(
        claim: ValidatedMemoryClaim,
        event: EventRecord,
    ) -> bool:
        if (
            claim.evidence.event_id != event.id
            or claim.evidence.source_speaker_user_id != event.sender_user_id
        ):
            return False
        fact = claim.fact
        if fact.scope_type is MemoryScopeType.PERSON:
            return fact.subject_user_id == event.sender_user_id and fact.group_id is None
        if fact.scope_type is MemoryScopeType.GROUP:
            return fact.subject_user_id is None and fact.group_id == event.group_id
        referenced = {*event.mentioned_user_ids}
        if event.reply_sender_user_id:
            referenced.add(event.reply_sender_user_id)
        return bool(
            fact.scope_type is MemoryScopeType.PERSON_GROUP
            and fact.group_id is not None
            and fact.group_id == event.group_id
            and fact.subject_user_id != event.bot_user_id
            and fact.subject_user_id in {event.sender_user_id, *referenced}
        )

    @staticmethod
    def _direct_result(
        *,
        operation: MemoryMutationAppliedOperation,
        changed: bool,
        old_fact_id: int | None,
        new_fact_id: int | None,
        reason_code: str,
        contested: bool = False,
    ) -> _AppliedMutation:
        return _AppliedMutation(
            operation if changed else MemoryMutationAppliedOperation.NOOP,
            (
                MemoryMutationOutcome.COMMITTED_AS_CONTESTED
                if changed and contested
                else MemoryMutationOutcome.COMMITTED
                if changed
                else MemoryMutationOutcome.NO_CHANGE
            ),
            old_fact_id,
            new_fact_id if changed else None,
            reason_code if changed else "no_state_change",
        )

    async def _schedule_embedding_after_commit(self, fact_id: int | None) -> None:
        if fact_id is None:
            return
        fact = await self._facts.get_fact(fact_id)
        if fact is None or fact.status is not MemoryStatus.ACTIVE:
            return
        try:
            await self._facts.schedule_embedding(fact_id)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "memory_mutation_embedding_schedule_failed fact_id=%d category=%s",
                fact_id,
                type(exc).__name__,
            )

    @staticmethod
    def _rejected(
        operation: MemoryMutationOperation,
        reason_code: str,
    ) -> MemoryMutationResult:
        return MemoryMutationResult(
            ok=False,
            mutation_id=None,
            requested_operation=operation,
            applied_operation=MemoryMutationAppliedOperation.NOOP,
            outcome=MemoryMutationOutcome.REJECTED,
            reason_code=reason_code,
        )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
