"""Restart-safe bounded background memory reflection behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import delete, select
from tests.conftest import make_settings

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.mutation.models import MemoryDecisionActorType
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.reflection.models import (
    MemoryReflectionCandidate,
    MemoryReflectionIssue,
)
from qq_ai_bot.memory.reflection.repository import MemoryReflectionRepository
from qq_ai_bot.memory.reflection.worker import MemoryReflectionWorker
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MembershipModel, MemoryMutationReceiptModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord


def _components(
    database: Database,
) -> tuple[
    Settings,
    MemoryFactService,
    EventLedgerRepository,
    MemoryMutationService,
    MemoryReflectionRepository,
    MemoryReflectionWorker,
]:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        memory_maintenance_enabled=True,
        memory_maintenance_batch_limit=20,
        memory_maintenance_interval_seconds=60,
    )
    fact_repository = MemoryFactRepository(database)
    facts = MemoryFactService(fact_repository)
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
        candidate_resolver=MemoryConflictCandidateResolver(fact_repository),
        relation_classifier=cast(MemoryRelationClassifier, object()),
        resolution_policy=MemoryResolutionPolicy(),
    )
    mutations = MemoryMutationService(
        settings=settings,
        facts=facts,
        processor=processor,
        ledger=ledger,
    )
    repository = MemoryReflectionRepository(database)
    worker = MemoryReflectionWorker(
        settings=settings,
        repository=repository,
        facts=facts,
        mutations=mutations,
    )
    return settings, facts, ledger, mutations, repository, worker


async def _event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    sender_user_id: str,
    content: str,
    group_id: str | None = None,
) -> EventRecord:
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=sender_user_id,
        direction="inbound",
        content=content,
        group_id=group_id,
        private_peer_user_id=sender_user_id if group_id is None else None,
    )
    return event


def _evidence(event: EventRecord, *, authority: MemoryAuthority) -> MemoryEvidenceCreate:
    return MemoryEvidenceCreate(
        event_id=event.id,
        source_speaker_user_id=event.sender_user_id,
        relation=(
            MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
            if authority is MemoryAuthority.THIRD_PARTY
            else MemoryEvidenceRelation.SELF_STATEMENT
        ),
        confidence=0.9,
        authority=authority,
        excerpt=event.content,
    )


@pytest.mark.asyncio
async def test_reflection_worker_merges_exact_duplicates_through_mutation_service(
    database: Database,
) -> None:
    _settings, facts, ledger, _mutations, repository, worker = _components(database)
    event = await _event(
        ledger,
        message_id="reflection-duplicate",
        sender_user_id="1001",
        content="我喜欢爵士乐",
    )
    first = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="music:jazz",
            category="music",
            content="喜欢爵士乐",
            confidence=0.8,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=_evidence(event, authority=MemoryAuthority.SELF_REPORT),
    )
    second = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="preference:music:jazz",
            category="music",
            content="喜欢爵士乐",
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=_evidence(event, authority=MemoryAuthority.SELF_REPORT),
    )

    assert await worker.process_once() == 1

    refreshed = (await facts.get_fact(first.id), await facts.get_fact(second.id))
    assert {fact.status for fact in refreshed if fact is not None} == {
        MemoryStatus.ACTIVE,
        MemoryStatus.SUPERSEDED,
    }
    jobs = await repository.discover(limit=20)
    assert not any(item.issue_type is MemoryReflectionIssue.DUPLICATE for item in jobs)
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
    assert receipt is not None
    assert receipt.decision_actor_type == MemoryDecisionActorType.REFLECTION.value
    assert receipt.applied_operation == "merge"
    assert receipt.outcome == "committed"


@pytest.mark.asyncio
async def test_reflection_worker_contests_person_group_attribution_anomaly(
    database: Database,
) -> None:
    _settings, facts, ledger, _mutations, repository, worker = _components(database)
    await _event(
        ledger,
        message_id="known-person",
        sender_user_id="2002",
        content="你好",
    )
    event = await _event(
        ledger,
        message_id="bad-attribution",
        sender_user_id="1001",
        group_id="3001",
        content="小明喜欢摄影",
    )
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.PREFERENCE,
            memory_key="hobby:photography",
            category="hobby",
            content="喜欢摄影",
            confidence=0.75,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        ),
        evidence=_evidence(event, authority=MemoryAuthority.THIRD_PARTY),
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            delete(MembershipModel).where(
                MembershipModel.user_id == "2002",
                MembershipModel.group_id == "3001",
            )
        )

    candidates = await repository.discover(limit=20)
    assert MemoryReflectionCandidate(MemoryReflectionIssue.ATTRIBUTION, fact.id) in candidates
    assert await worker.process_once() == 1

    refreshed = await facts.get_fact(fact.id)
    assert refreshed is not None
    assert refreshed.status is MemoryStatus.ACTIVE
    assert refreshed.conflict_state is MemoryConflictState.CONTESTED


@pytest.mark.asyncio
async def test_reflection_jobs_retry_to_terminal_failure_and_recover_stale_claim(
    database: Database,
) -> None:
    _settings, facts, ledger, _mutations, repository, _worker = _components(database)
    event = await _event(
        ledger,
        message_id="reflection-retry",
        sender_user_id="1001",
        content="测试任务恢复",
    )
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.FACT,
            memory_key="test:recovery",
            category="test",
            content="测试任务恢复",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=_evidence(event, authority=MemoryAuthority.SELF_REPORT),
    )
    started = datetime(2026, 8, 2, 12, tzinfo=UTC)
    await repository.enqueue(
        (MemoryReflectionCandidate(MemoryReflectionIssue.CONTESTED, fact.id),),
        max_attempts=2,
        now=started,
    )
    first_claim = await repository.claim(limit=1, now=started)
    assert len(first_claim) == 1
    assert await repository.fail(first_claim[0].id, "synthetic", now=started)
    pending = await repository.get(first_claim[0].id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.next_attempt_at == started + timedelta(seconds=30)

    second_claim = await repository.claim(
        limit=1,
        now=started + timedelta(seconds=30),
    )
    assert len(second_claim) == 1
    assert await repository.fail(
        second_claim[0].id,
        "synthetic_again",
        now=started + timedelta(seconds=30),
    )
    exhausted = await repository.get(second_claim[0].id)
    assert exhausted is not None
    assert exhausted.status == "failed"
    assert exhausted.attempts == 2
    assert exhausted.error_category == "synthetic_again"

    await repository.enqueue(
        (MemoryReflectionCandidate(MemoryReflectionIssue.ATTRIBUTION, fact.id),),
        now=started,
    )
    stale_claim = await repository.claim(limit=1, now=started)
    assert len(stale_claim) == 1
    recovered_at = started + timedelta(minutes=5)
    assert await repository.recover_stale(before=started, now=recovered_at) == 1
    recovered = await repository.get(stale_claim[0].id)
    assert recovered is not None
    assert recovered.status == "pending"
    assert recovered.claimed_at is None
    assert recovered.next_attempt_at == recovered_at
    assert recovered.error_category == "worker_recovered"

    await repository.enqueue(
        (
            MemoryReflectionCandidate(
                MemoryReflectionIssue.DUPLICATE,
                fact.id,
                fact.id,
            ),
        ),
        max_attempts=1,
        now=started,
    )
    final_claim = await repository.claim(limit=1, now=started)
    assert len(final_claim) == 1
    assert await repository.recover_stale(before=started, now=recovered_at) == 1
    terminal = await repository.get(final_claim[0].id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.claimed_at is None
    assert terminal.error_category == "worker_recovery_exhausted"
