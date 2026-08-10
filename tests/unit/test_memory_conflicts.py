"""Memory V2 Phase 4 conflict, evidence, and lifecycle contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryResolutionAction,
    MemoryScopeType,
    MemorySemanticRelation,
    MemorySourceType,
    MemoryStatus,
    MemoryTemporalMode,
)
from qq_ai_bot.memory.evidence import MemoryEvidencePolicy
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.lifecycle import MemoryLifecycleConfig, MemoryLifecyclePolicy
from qq_ai_bot.memory.models import (
    CandidateRelation,
    MemoryEvidenceCreate,
    MemoryFactCreate,
)
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectResolver
from qq_ai_bot.memory.temporal import MemoryTemporalResolver
from qq_ai_bot.memory.validation import MemoryClaimValidator
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository


async def _group_event(
    database: Database,
    *,
    message_id: str,
    content: str,
    sender_user_id: str = "1001",
    mentioned_user_ids: tuple[str, ...] = (),
    reply_sender_user_id: str | None = None,
):
    ledger = EventLedgerRepository(database)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP,
        sender_user_id=sender_user_id,
        direction="inbound",
        content=content,
        group_id="3001",
        segments=(
            {
                "type": "yuki_context",
                "data": {
                    "mentioned_user_ids": list(mentioned_user_ids),
                    "reply_sender_user_id": reply_sender_user_id,
                },
            },
        ),
    )
    return replace(
        event,
        mentioned_user_ids=mentioned_user_ids,
        reply_sender_user_id=reply_sender_user_id,
    )


def _claim(**overrides: object) -> MemoryClaim:
    values: dict[str, object] = {
        "operation": "assert",
        "subject_ref": "speaker",
        "scope_type": "person",
        "kind": "fact",
        "memory_key": "profile:city",
        "category": "profile",
        "content": "现在住在上海",
        "evidence_quote": "现在住在上海",
        "importance": 3,
        "confidence": 0.8,
        "source_type": "automatic",
        "temporal_mode": "persistent",
    }
    values.update(overrides)
    return MemoryClaim.model_validate(values)


def test_subject_resolver_only_exposes_trusted_mentions_and_reply_author() -> None:
    from qq_ai_bot.persistence.repository_records import EventRecord

    event = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="subject-1",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="我觉得他喜欢红茶",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id="3001",
        mentioned_user_ids=("1002", "8000", "1002"),
        reply_sender_user_id="1003",
    )

    available = SubjectResolver.available(event)
    assert [item.subject_ref for item in available] == [
        "speaker",
        "group",
        "mentioned_1",
        "reply_author",
    ]
    mentioned = SubjectResolver.resolve(
        event,
        subject_ref="mentioned_1",
        scope_type=MemoryScopeType.PERSON_GROUP,
    )
    replied = SubjectResolver.resolve(
        event,
        subject_ref="reply_author",
        scope_type=MemoryScopeType.PERSON_GROUP,
    )
    assert mentioned is not None and mentioned.subject_user_id == "1002"
    assert replied is not None and replied.subject_user_id == "1003"
    assert (
        SubjectResolver.resolve(
            event,
            subject_ref="mentioned_1",
            scope_type=MemoryScopeType.PERSON,
        )
        is None
    )
    assert (
        MemoryClaimValidator().validate_claim(
            _claim(
                operation="retract",
                subject_ref="mentioned_1",
                scope_type="person_group",
            ),
            event,
        )
        is None
    )


def test_temporal_resolver_uses_trusted_event_clock_and_rejects_invalid_ranges() -> None:
    resolver = MemoryTemporalResolver()
    event_time = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    episode = resolver.resolve(
        mode=MemoryTemporalMode.EPISODE,
        valid_from=None,
        valid_until=None,
        occurred_at=event_time,
    )
    assert episode.valid_from == event_time
    with pytest.raises(ValueError, match="valid_until"):
        resolver.resolve(
            mode=MemoryTemporalMode.TEMPORARY,
            valid_from=None,
            valid_until=None,
            occurred_at=event_time,
        )
    with pytest.raises(ValueError, match="after"):
        resolver.resolve(
            mode=MemoryTemporalMode.TEMPORARY,
            valid_from="2026-08-03T00:00:00+08:00",
            valid_until="2026-08-02T00:00:00+08:00",
            occurred_at=event_time,
        )


def test_evidence_aggregation_is_monotonic_and_authority_capped() -> None:
    policy = MemoryEvidencePolicy()
    first = MemoryEvidenceCreate(
        event_id=1,
        source_speaker_user_id="1001",
        relation=MemoryEvidenceRelation.THIRD_PARTY_STATEMENT,
        confidence=0.8,
        authority=MemoryAuthority.THIRD_PARTY,
        excerpt="听说他住上海",
    )
    second = first.model_copy(update={"event_id": 2})
    one = policy.aggregate((first,), authority=MemoryAuthority.THIRD_PARTY)
    two = policy.aggregate((first, second), authority=MemoryAuthority.THIRD_PARTY)
    assert 0 < one < two <= policy.authority_cap(MemoryAuthority.THIRD_PARTY)


@pytest.mark.asyncio
async def test_third_party_contradiction_becomes_bounded_contest(database: Database) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    original = await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="1002",
            group_id="3001",
            memory_key="profile:city",
            category="profile",
            content="住在福州",
            confidence=0.6,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        )
    )
    event = await _group_event(
        database,
        message_id="third-party-conflict",
        content="你现在住上海",
        mentioned_user_ids=("1002",),
    )
    validated = MemoryClaimValidator().validate_claim(
        _claim(
            subject_ref="mentioned_1",
            scope_type="person_group",
            content="住在上海",
            evidence_quote="你现在住上海",
            subject_basis="addressed_second_person",
        ),
        event,
    )
    assert validated is not None
    assert validated.fact.authority is MemoryAuthority.THIRD_PARTY
    candidates = await MemoryConflictCandidateResolver(repository).resolve(validated.fact)
    assert [candidate.fact.id for candidate in candidates] == [original.id]
    plan = MemoryResolutionPolicy().resolve(
        validated,
        candidates,
        (
            CandidateRelation(
                candidate_ref=candidates[0].candidate_ref,
                relation=MemorySemanticRelation.CONTRADICTS,
                confidence=0.9,
            ),
        ),
    )
    assert plan.action is MemoryResolutionAction.CONTEST

    created = await service.apply_claim(validated, candidates=candidates, plan=plan)
    assert created is not None
    assert created.status is MemoryStatus.CONTESTED
    assert created.conflict_state is MemoryConflictState.CONTESTED
    existing = await repository.get_fact(original.id)
    assert existing is not None
    assert existing.status is MemoryStatus.ACTIVE
    assert existing.conflict_state is MemoryConflictState.CONTESTED
    assert [row.relation_type.value for row in await repository.list_relations(created.id)] == [
        "contradicts"
    ]

    confirmation_event = await _group_event(
        database,
        message_id="subject-confirmation",
        content="我确认自己住在上海",
        sender_user_id="1002",
    )
    confirmation = MemoryClaimValidator().validate_claim(
        _claim(
            operation="confirm",
            subject_ref="speaker",
            scope_type="person_group",
            content="住在上海",
            evidence_quote="我确认自己住在上海",
        ),
        confirmation_event,
    )
    assert confirmation is not None
    candidates = await MemoryConflictCandidateResolver(repository).resolve(confirmation.fact)
    confirmation_plan = MemoryResolutionPolicy().resolve(confirmation, candidates)
    assert confirmation_plan.action is MemoryResolutionAction.MERGE_EVIDENCE
    confirmed = await service.apply_claim(
        confirmation,
        candidates=candidates,
        plan=confirmation_plan,
    )
    assert confirmed is not None
    assert confirmed.authority is MemoryAuthority.SELF_REPORT
    assert confirmed.status is MemoryStatus.ACTIVE
    assert confirmed.conflict_state is MemoryConflictState.CLEAR
    selected = await repository.get_fact(created.id)
    rejected = await repository.get_fact(original.id)
    assert selected is not None and selected.status is MemoryStatus.ACTIVE
    assert selected.conflict_state is MemoryConflictState.CLEAR
    assert rejected is not None and rejected.status is MemoryStatus.INVALIDATED


@pytest.mark.asyncio
async def test_admin_resolution_promotes_contested_fact_atomically(database: Database) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    original = await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="profile:city",
            category="profile",
            content="住在福州",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    async with repository.transaction() as session:
        row = await repository.create_fact(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="1001",
                memory_key="profile:city",
                category="profile",
                content="住在上海",
                source_type=MemorySourceType.AUTOMATIC,
                status=MemoryStatus.CONTESTED,
                conflict_state=MemoryConflictState.CONTESTED,
            ),
            normalized_content="住在上海",
            supersedes_id=None,
            session=session,
        )
        await repository.record_created(
            row.id,
            status=MemoryStatus.CONTESTED,
            conflict_state=MemoryConflictState.CONTESTED,
            reason_code="test_contest",
            source_event_id=None,
            actor_user_id=None,
            session=session,
        )
        await repository.add_relation(
            source_fact_id=row.id,
            target_fact_id=original.id,
            relation_type=MemoryFactRelationType.CONTRADICTS,
            confidence=0.9,
            source_event_id=None,
            session=session,
        )
        contested_id = row.id

    assert (
        await service.resolve_conflicts(
            contested_id,
            (original.id,),
            actor_user_id="9000",
        )
        == 1
    )
    selected = await repository.get_fact(contested_id)
    rejected = await repository.get_fact(original.id)
    assert selected is not None and selected.status is MemoryStatus.ACTIVE
    assert selected.conflict_state is MemoryConflictState.CLEAR
    assert rejected is not None and rejected.status is MemoryStatus.INVALIDATED


@pytest.mark.asyncio
async def test_subject_confirmation_does_not_override_explicit_counterpart(
    database: Database,
) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    explicit = await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="1002",
            group_id="3001",
            memory_key="profile:city",
            category="profile",
            content="住在福州",
            source_type=MemorySourceType.EXPLICIT,
            authority=MemoryAuthority.EXPLICIT,
        )
    )
    assert await service.contest_fact(explicit.id, reason_code="test_contest")
    async with repository.transaction() as session:
        row = await repository.create_fact(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON_GROUP,
                subject_user_id="1002",
                group_id="3001",
                memory_key="profile:city",
                category="profile",
                content="住在上海",
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.THIRD_PARTY,
                status=MemoryStatus.CONTESTED,
                conflict_state=MemoryConflictState.CONTESTED,
            ),
            normalized_content="住在上海",
            supersedes_id=None,
            session=session,
        )
        await repository.record_created(
            row.id,
            status=MemoryStatus.CONTESTED,
            conflict_state=MemoryConflictState.CONTESTED,
            reason_code="test_contest",
            source_event_id=None,
            actor_user_id=None,
            session=session,
        )
        await repository.add_relation(
            source_fact_id=row.id,
            target_fact_id=explicit.id,
            relation_type=MemoryFactRelationType.CONTRADICTS,
            confidence=0.9,
            source_event_id=None,
            session=session,
        )
        contested_id = row.id
    event = await _group_event(
        database,
        message_id="explicit-protection-confirmation",
        content="我确认自己住在上海",
        sender_user_id="1002",
    )
    confirmed = await service.confirm_fact(
        contested_id,
        MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1002",
            relation=MemoryEvidenceRelation.CONFIRMATION,
            confidence=1.0,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我确认自己住在上海",
        ),
        confirmed_at=event.occurred_at,
    )
    protected = await repository.get_fact(explicit.id)
    assert confirmed is not None
    assert confirmed.authority is MemoryAuthority.SELF_REPORT
    assert confirmed.status is MemoryStatus.CONTESTED
    assert confirmed.conflict_state is MemoryConflictState.CONTESTED
    assert protected is not None and protected.status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_superseded_history_is_not_a_live_conflict_candidate(database: Database) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    original = await service.add_explicit_person("1001", "住在福州", limit=100)
    corrected = await service.correct_fact(
        original.id,
        content="住在上海",
        actor_user_id="1001",
    )
    assert corrected is not None
    candidates = await MemoryConflictCandidateResolver(repository).resolve(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="explicit-mismatch",
            category="explicit",
            content="住在福州",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    assert {row.fact.id for row in candidates} == {corrected.id}


@pytest.mark.asyncio
async def test_explicit_correction_creates_version_and_retraction_is_reversible(
    database: Database,
) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    original = await service.add_explicit_person("1001", "住在福州", limit=100)
    corrected = await service.correct_fact(
        original.id,
        content="住在上海",
        actor_user_id="1001",
    )
    assert corrected is not None and corrected.id != original.id
    assert corrected.supersedes_id == original.id
    old = await repository.get_fact(original.id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED

    assert await service.invalidate_fact(
        corrected.id,
        reason=MemoryInvalidationReason.USER_RETRACTED,
        actor_user_id="1001",
    )
    invalidated = await repository.get_fact(corrected.id)
    assert invalidated is not None
    assert invalidated.status is MemoryStatus.INVALIDATED
    assert invalidated.invalidated_reason is MemoryInvalidationReason.USER_RETRACTED
    restored = await service.restore_fact(corrected.id, actor_user_id="1001")
    assert restored is not None and restored.status is MemoryStatus.ACTIVE
    assert restored.invalidated_reason is None
    assert [row.action.value for row in await repository.list_state_events(corrected.id)] == [
        "created",
        "invalidated",
        "restored",
    ]


def test_lifecycle_protects_explicit_from_staleness_but_honors_deadline() -> None:
    from qq_ai_bot.memory.models import MemoryFact

    now = datetime.now(UTC)
    base = MemoryFact(
        id=1,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        kind="fact",
        memory_key="profile:city",
        category="profile",
        content="住在上海",
        normalized_content="住在上海",
        importance=1,
        confidence=0.2,
        source_type=MemorySourceType.AUTOMATIC,
        authority=MemoryAuthority.SELF_REPORT,
        status=MemoryStatus.ACTIVE,
        created_at=now - timedelta(days=100),
        updated_at=now - timedelta(days=100),
        last_confirmed_at=now - timedelta(days=100),
    )
    config = MemoryLifecycleConfig(
        automatic_stale_days=30,
        third_party_stale_days=14,
        contested_stale_days=7,
        stale_max_importance=2,
        stale_max_confidence=0.5,
    )
    policy = MemoryLifecyclePolicy()
    assert policy.reason(base, now=now, config=config) is MemoryInvalidationReason.STALE
    explicit = base.model_copy(
        update={
            "source_type": MemorySourceType.EXPLICIT,
            "authority": MemoryAuthority.EXPLICIT,
        }
    )
    assert policy.reason(explicit, now=now, config=config) is None
    expired_explicit = explicit.model_copy(
        update={"valid_until": now - timedelta(seconds=1)}
    )
    assert (
        policy.reason(expired_explicit, now=now, config=config)
        is MemoryInvalidationReason.EXPIRED
    )
    important = base.model_copy(update={"importance": 5})
    assert policy.reason(important, now=now, config=config) is None


def test_operation_enum_is_closed() -> None:
    assert {item.value for item in MemoryClaimOperation} == {
        "assert",
        "confirm",
        "correct",
        "retract",
    }
