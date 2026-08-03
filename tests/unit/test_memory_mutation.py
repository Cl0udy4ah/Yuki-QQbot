"""Unified Memory V2 mutation service behavior tests."""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryProcessingSource,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryMutationReceiptModel
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    PeopleRepository,
)
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime


def _service(
    database: Database,
) -> tuple[
    MemoryMutationService,
    MemoryFactService,
    EventLedgerRepository,
    MemoryClaimProcessor,
]:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
        candidate_resolver=MemoryConflictCandidateResolver(repository),
        relation_classifier=cast(MemoryRelationClassifier, object()),
        resolution_policy=MemoryResolutionPolicy(),
    )
    return (
        MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=processor,
            ledger=ledger,
        ),
        facts,
        ledger,
        processor,
    )


async def _event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    sender_user_id: str,
    content: str,
    group_id: str | None = None,
    mentioned_user_ids: tuple[str, ...] = (),
    direction: str = "inbound",
    sender_is_bot: bool = False,
) -> EventRecord:
    segments = (
        {
            "type": "yuki_context",
            "data": {
                "mentioned_user_ids": list(mentioned_user_ids),
                "reply_sender_user_id": None,
            },
        },
    )
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=sender_user_id,
        direction=direction,
        content=content,
        segments=segments,
        group_id=group_id,
        private_peer_user_id=sender_user_id if group_id is None else None,
        sender_is_bot=sender_is_bot,
    )
    return event


def _context(event: EventRecord) -> MemoryMutationContext:
    return MemoryMutationContext(
        event=event,
        conversation_key=(
            f"group:{event.group_id}:user:{event.sender_user_id}"
            if event.group_id
            else f"private:{event.sender_user_id}"
        ),
        turn_origin="user_message",
        delegation_mode="main_agent",
        trigger_actor_user_id=event.sender_user_id,
        decision_actor_type=MemoryDecisionActorType.AGENT,
        decision_actor_id="yuki-main-agent",
        executed_by_bot_user_id=event.bot_user_id,
    )


@pytest.mark.asyncio
async def test_self_create_is_atomic_receipted_and_deduplicated(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="self-create",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="现在住在上海",
        memory_key="location:home",
        category="location",
        reason="用户明确要求记住当前住址",
        confidence=0.96,
    )

    first = await service.mutate(request, _context(event))
    second = await service.mutate(request, _context(event))

    assert first.ok
    assert first.applied_operation is MemoryMutationAppliedOperation.CREATE
    assert first.outcome is MemoryMutationOutcome.COMMITTED
    assert second.ok
    assert second.deduplicated
    assert second.mutation_id == first.mutation_id
    rows = await facts.list_person("1001", limit=20)
    assert len(rows) == 1
    assert rows[0].content == "现在住在上海"
    assert rows[0].authority is MemoryAuthority.EXPLICIT
    async with database.sessions() as session:
        receipt_count = int(
            await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) or 0
        )
    assert receipt_count == 1


@pytest.mark.asyncio
async def test_third_party_group_correction_commits_as_contested(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    await _event(
        ledger,
        message_id="member-observed",
        sender_user_id="2002",
        content="大家好",
        group_id="3001",
    )
    existing = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="location:home",
            category="location",
            content="住在北京",
            importance=3,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        )
    )
    event = await _event(
        ledger,
        message_id="third-party-correct",
        sender_user_id="1001",
        content="小明已经搬到上海了",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CORRECT,
        fact_id=existing.id,
        target=MemoryMutationTarget(
            subject_ref="mentioned_user",
            scope_type=MemoryScopeType.PERSON_GROUP,
        ),
        new_content="已经搬到上海",
        reason="当前群消息明确报告了新住址",
        confidence=0.8,
        expected_fact_state=MemoryStatus.ACTIVE,
    )

    result = await service.mutate(request, _context(event))

    assert result.ok
    assert result.applied_operation is MemoryMutationAppliedOperation.CONTEST
    assert result.outcome is MemoryMutationOutcome.COMMITTED_AS_CONTESTED
    assert result.old_fact_id == existing.id
    assert result.new_fact_id is not None
    old = await facts.get_fact(existing.id)
    alternative = await facts.get_fact(result.new_fact_id)
    assert old is not None
    assert old.status is MemoryStatus.ACTIVE
    assert old.conflict_state is MemoryConflictState.CONTESTED
    assert alternative is not None
    assert alternative.status is MemoryStatus.CONTESTED
    assert alternative.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_bot_event_cannot_become_user_memory_evidence(database: Database) -> None:
    service, _facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="bot-event",
        sender_user_id="8000",
        content="记住用户住在上海",
        direction="outbound",
        sender_is_bot=True,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="住在上海",
        memory_key="location:home",
        category="location",
        reason="不可信的 Bot 消息",
    )

    result = await service.mutate(request, _context(event))

    assert not result.ok
    assert result.reason_code == "untrusted_trigger_event"
    async with database.sessions() as session:
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                or 0
            )
            == 0
        )


@pytest.mark.asyncio
async def test_agent_tool_and_worker_share_one_claim_receipt(database: Database) -> None:
    service, facts, ledger, processor = _service(database)
    event = await _event(
        ledger,
        message_id="agent-worker-dedupe",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=facts,
        memory_mutations=service,
        actions=AgentActionRepository(database),
    )
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=event.platform_message_id,
        actor_user_id=event.sender_user_id,
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert "memory_change" in {tool.name for tool in tools.definitions(runtime)}
    response = json.loads(
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "create",
                    "target": {
                        "subject_ref": "current_speaker",
                        "scope_type": "person",
                    },
                    "new_content": "现在住在上海",
                    "memory_key": "location:home",
                    "category": "location",
                    "reason": "当前消息明确要求记忆",
                    "confidence": 0.96,
                },
                ensure_ascii=False,
            ),
            runtime,
        )
    )
    assert response["ok"]
    assert response["data"]["outcome"] == "committed"

    claim = MemoryClaim(
        operation=MemoryClaimOperation.ASSERT,
        subject_ref="speaker",
        scope_type=MemoryScopeType.PERSON,
        kind=MemoryKind.FACT,
        memory_key="location:home",
        category="location",
        content="现在住在上海",
        evidence_quote=event.content,
        importance=3,
        confidence=0.96,
        source_type=MemorySourceType.EXPLICIT,
    )
    validated = processor.validate(claim, event)
    assert validated is not None
    worker_result = await service.mutate_validated_claim(
        validated,
        MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=event),
        conversation_key="private:1001",
    )

    assert worker_result.ok
    assert worker_result.deduplicated
    assert worker_result.mutation_id == response["data"]["mutation_id"]
    assert len(await facts.list_person("1001", limit=20)) == 1


@pytest.mark.asyncio
async def test_self_correction_creates_a_new_version(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="version-original",
        sender_user_id="1001",
        content="记住我住在北京",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="住在北京",
            memory_key="location:home",
            category="location",
            reason="original_self_report",
        ),
        _context(original_event),
    )
    assert original.new_fact_id is not None
    correction_event = await _event(
        ledger,
        message_id="version-correction",
        sender_user_id="1001",
        content="我已经搬到上海了",
    )
    correction = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=original.new_fact_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="已经搬到上海",
            reason="current_self_correction",
            expected_fact_state=MemoryStatus.ACTIVE,
        ),
        _context(correction_event),
    )

    assert correction.ok
    assert correction.applied_operation is MemoryMutationAppliedOperation.CORRECT
    assert correction.new_fact_id not in {None, original.new_fact_id}
    old = await facts.get_fact(original.new_fact_id)
    new = await facts.get_fact(correction.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert new is not None and new.status is MemoryStatus.ACTIVE
    assert new.supersedes_id == old.id


@pytest.mark.asyncio
async def test_group_member_can_create_group_and_third_party_group_memory(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    await _event(
        ledger,
        message_id="mentioned-member-presence",
        sender_user_id="2002",
        content="大家好",
        group_id="3001",
    )
    event = await _event(
        ledger,
        message_id="open-group-write",
        sender_user_id="1001",
        content="这个群每周五聚会，小明负责摄影",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    group_result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_group",
                scope_type=MemoryScopeType.GROUP,
            ),
            new_content="每周五聚会",
            memory_key="activity:weekly",
            category="activity",
            reason="group_member_report",
            evidence_quote="这个群每周五聚会",
        ),
        _context(event),
    )
    person_group_result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="负责摄影",
            memory_key="role:photography",
            category="role",
            reason="third_party_group_report",
            evidence_quote="小明负责摄影",
        ),
        _context(event),
    )

    assert group_result.ok and group_result.new_fact_id is not None
    assert person_group_result.ok and person_group_result.new_fact_id is not None
    group_fact = await facts.get_fact(group_result.new_fact_id)
    person_group_fact = await facts.get_fact(person_group_result.new_fact_id)
    assert group_fact is not None and group_fact.authority is MemoryAuthority.GROUP_REPORT
    assert person_group_fact is not None
    assert person_group_fact.authority is MemoryAuthority.THIRD_PARTY
    assert person_group_fact.subject_user_id == "2002"
    assert person_group_fact.group_id == "3001"


@pytest.mark.asyncio
async def test_reassign_is_one_atomic_versioned_group_operation(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="reassign-original",
        sender_user_id="1001",
        content="我喜欢摄影",
        group_id="3001",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="喜欢摄影",
            memory_key="hobby:photography",
            category="hobby",
            reason="initial_attribution",
        ),
        _context(original_event),
    )
    assert original.new_fact_id is not None
    event = await _event(
        ledger,
        message_id="reassign-correction",
        sender_user_id="1001",
        content="刚才那条其实说的是小明喜欢摄影",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.REASSIGN,
            fact_id=original.new_fact_id,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            reason="misattributed_subject",
        ),
        _context(event),
    )

    assert result.ok
    assert result.applied_operation is MemoryMutationAppliedOperation.REASSIGN
    assert result.new_fact_id is not None
    old = await facts.get_fact(original.new_fact_id)
    reassigned = await facts.get_fact(result.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert reassigned is not None and reassigned.status is MemoryStatus.ACTIVE
    assert reassigned.subject_user_id == "2002"
    assert reassigned.group_id == "3001"
    assert reassigned.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_commit_once(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="concurrent-dedupe",
        sender_user_id="1001",
        content="记住我喜欢爵士乐",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="喜欢爵士乐",
        memory_key="music:jazz",
        category="music",
        reason="concurrent_same_request",
    )

    first, second = await asyncio.gather(
        service.mutate(request, _context(event)),
        service.mutate(request, _context(event)),
    )

    assert {first.deduplicated, second.deduplicated} == {False, True}
    assert first.mutation_id == second.mutation_id
    assert len(await facts.list_person("1001", limit=20)) == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_fact(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="rollback-receipt",
        sender_user_id="1001",
        content="记住我喜欢蓝色",
    )

    async def fail_finalize(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("forced receipt failure")

    monkeypatch.setattr(service._receipts, "finalize", fail_finalize)
    with pytest.raises(RuntimeError, match="forced receipt failure"):
        await service.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=MemoryScopeType.PERSON,
                ),
                new_content="喜欢蓝色",
                memory_key="color:favorite",
                category="preference",
                reason="rollback_test",
            ),
            _context(event),
        )

    assert await facts.list_person("1001", limit=20) == ()
    async with database.sessions() as session:
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                or 0
            )
            == 0
        )


@pytest.mark.asyncio
async def test_embedding_schedule_failure_keeps_committed_fact(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="embedding-failure",
        sender_user_id="1001",
        content="记住我喜欢绿色",
    )

    async def fail_embedding(_fact_id: int) -> None:
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(facts, "schedule_embedding", fail_embedding)
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="喜欢绿色",
            memory_key="color:favorite",
            category="preference",
            reason="embedding_failure_test",
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    assert await facts.get_fact(result.new_fact_id) is not None


@pytest.mark.asyncio
async def test_reflection_uses_existing_user_evidence_without_claim_collision(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="reflection-source",
        sender_user_id="1001",
        content="我暂时住在上海",
    )
    created = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="暂时住在上海",
            memory_key="location:temporary",
            category="location",
            reason="temporary_self_report",
        ),
        _context(event),
    )
    assert created.new_fact_id is not None
    fact = await facts.get_fact(created.new_fact_id)
    assert fact is not None
    reflected = await service.mutate_reflection(
        fact,
        operation=MemoryMutationOperation.INVALIDATE,
        reason=MemoryInvalidationReason.STALE,
    )

    assert reflected.ok
    assert reflected.applied_operation is MemoryMutationAppliedOperation.INVALIDATE
    invalidated = await facts.get_fact(fact.id)
    assert invalidated is not None
    assert invalidated.status is MemoryStatus.INVALIDATED
    assert invalidated.invalidated_reason is MemoryInvalidationReason.STALE


@pytest.mark.asyncio
async def test_merge_metadata_contest_invalidate_and_restore_operations(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)

    async def create(message_id: str, text: str, key: str, content: str) -> int:
        event = await _event(
            ledger,
            message_id=message_id,
            sender_user_id="1001",
            content=text,
        )
        result = await service.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=MemoryScopeType.PERSON,
                ),
                new_content=content,
                memory_key=key,
                category="music",
                reason="operation_fixture",
            ),
            _context(event),
        )
        assert result.new_fact_id is not None
        return result.new_fact_id

    source_id = await create(
        "merge-source",
        "我喜欢 Jazz",
        "music:jazz",
        "喜欢 Jazz",
    )
    target_id = await create(
        "merge-target",
        "我喜欢爵士乐",
        "music:favorite",
        "喜欢爵士乐",
    )
    merge_event = await _event(
        ledger,
        message_id="merge-operation",
        sender_user_id="1001",
        content="这两条其实是同一个音乐偏好",
    )
    merged = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.MERGE,
            fact_id=source_id,
            merge_fact_id=target_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="equivalent_music_preferences",
        ),
        _context(merge_event),
    )
    assert merged.ok
    assert merged.applied_operation is MemoryMutationAppliedOperation.MERGE
    assert (await facts.get_fact(source_id)).status is MemoryStatus.SUPERSEDED  # type: ignore[union-attr]

    metadata_event = await _event(
        ledger,
        message_id="metadata-operation",
        sender_user_id="1001",
        content="把它归类为音乐偏好，重要度四级",
    )
    metadata = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.UPDATE_METADATA,
            fact_id=target_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            category="preference",
            kind=MemoryKind.PREFERENCE,
            importance=4,
            reason="metadata_reclassification",
        ),
        _context(metadata_event),
    )
    assert metadata.ok and metadata.new_fact_id is not None
    current_id = metadata.new_fact_id
    current = await facts.get_fact(current_id)
    assert current is not None
    assert current.kind is MemoryKind.PREFERENCE
    assert current.category == "preference"
    assert current.supersedes_id == target_id

    contest_event = await _event(
        ledger,
        message_id="contest-operation",
        sender_user_id="1001",
        content="这条记忆需要先标为有争议",
    )
    contested = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CONTEST,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_review",
        ),
        _context(contest_event),
    )
    assert contested.ok
    assert contested.applied_operation is MemoryMutationAppliedOperation.CONTEST
    assert (await facts.get_fact(current_id)).conflict_state is (  # type: ignore[union-attr]
        MemoryConflictState.CONTESTED
    )

    invalidate_event = await _event(
        ledger,
        message_id="invalidate-operation",
        sender_user_id="1001",
        content="撤销这条音乐偏好记忆",
    )
    invalidated = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_retracted",
        ),
        _context(invalidate_event),
    )
    assert invalidated.ok
    assert (await facts.get_fact(current_id)).status is MemoryStatus.INVALIDATED  # type: ignore[union-attr]

    restore_event = await _event(
        ledger,
        message_id="restore-operation",
        sender_user_id="1001",
        content="恢复这条音乐偏好记忆",
    )
    restored = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.RESTORE,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_restore",
        ),
        _context(restore_event),
    )
    assert restored.ok
    assert restored.applied_operation is MemoryMutationAppliedOperation.RESTORE
    assert (await facts.get_fact(current_id)).status is MemoryStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mentioned_member_read_is_limited_to_current_group_person_group(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    del service
    current_group_event = await _event(
        ledger,
        message_id="member-in-group",
        sender_user_id="2002",
        content="我喜欢天文",
        group_id="3001",
    )
    global_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="private:secret",
            category="private",
            content="跨群私人事实",
            importance=5,
            confidence=1,
            source_type=MemorySourceType.EXPLICIT,
            authority=MemoryAuthority.EXPLICIT,
        )
    )
    group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="role:photographer",
            category="role",
            content="在本群负责摄影",
            importance=3,
            confidence=0.8,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        )
    )
    projected_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="hobby:astronomy",
            category="hobby",
            content="喜欢天文",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=current_group_event.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=0.9,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我喜欢天文",
        ),
    )
    other_group_event = await _event(
        ledger,
        message_id="member-other-group",
        sender_user_id="2002",
        content="我喜欢围棋",
        group_id="3002",
    )
    other_group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="hobby:go",
            category="hobby",
            content="喜欢围棋",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=other_group_event.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=0.9,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我喜欢围棋",
        ),
    )
    inbound = InboundMessage(
        message_id="member-read",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001"),
        text="小明在这个群负责什么",
        bot_user_id="8000",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    tools = AgentToolService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        memories=facts,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="1001",
        current_group_id="3001",
        mentioned_user_ids=("2002",),
    )
    definition = next(
        tool for tool in tools.definitions(runtime) if tool.name == "get_person_memories"
    )
    properties = definition.parameters["properties"]
    assert definition.parameters["required"] == []
    assert set(properties) >= {"subject_ref", "display_name", "user_id"}  # type: ignore[arg-type]
    assert "mentioned_user_1" in properties["subject_ref"]["enum"]  # type: ignore[index]

    by_reference = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"subject_ref": "mentioned_user_1"}),
            runtime,
        )
    )
    reference_ids = {row["fact_id"] for row in by_reference["data"]["memories"]}
    assert by_reference["data"]["resolved_by"] == "subject_ref"
    assert by_reference["data"]["subject_ref"] == "mentioned_user_1"
    assert group_fact.id in reference_ids
    assert projected_fact.id in reference_ids
    assert global_fact.id not in reference_ids
    assert other_group_fact.id not in reference_ids
    projected_row = next(
        row for row in by_reference["data"]["memories"] if row["fact_id"] == projected_fact.id
    )
    assert projected_row["access_scope"] == "same_group_evidence_projection"
    assert projected_row["read_only"] is True

    listed = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"user_id": "2002"}),
            runtime,
        )
    )
    visible_ids = {row["fact_id"] for row in listed["data"]["memories"]}
    assert group_fact.id in visible_ids
    assert projected_fact.id in visible_ids
    assert global_fact.id not in visible_ids
    assert other_group_fact.id not in visible_ids
    queried = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"subject_ref": "mentioned_user_1", "query": "天文"}),
            runtime,
        )
    )
    assert projected_fact.id in {row["fact_id"] for row in queried["data"]["memories"]}
    group_lookup = json.loads(
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": group_fact.id}),
            runtime,
        )
    )
    global_lookup = json.loads(
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": global_fact.id}),
            runtime,
        )
    )
    projected_lookup = json.loads(
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": projected_fact.id}),
            runtime,
        )
    )
    assert group_lookup["ok"]
    assert not global_lookup["ok"]
    assert not projected_lookup["ok"]


@pytest.mark.asyncio
async def test_manual_qq_and_exact_name_lookup_stay_inside_current_group(
    database: Database,
) -> None:
    _service_unused, facts, ledger, _processor = _service(database)
    people = PeopleRepository(database)
    await people.observe(
        user_id="2002",
        nickname="查无此人",
        group_id="3001",
        group_card="摄影师",
    )
    group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="role:photographer",
            category="role",
            content="在本群负责摄影",
            importance=3,
            confidence=0.8,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        )
    )
    inbound = InboundMessage(
        message_id="manual-member-read",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001"),
        text="查一下摄影师的记忆",
        bot_user_id="8000",
        group_id="3001",
    )
    tools = AgentToolService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        memories=facts,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="1001",
        current_group_id="3001",
    )

    by_qq = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"user_id": "2002"}),
            runtime,
        )
    )
    by_name = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"display_name": "摄影师"}),
            runtime,
        )
    )
    assert by_qq["ok"] and by_qq["data"]["resolved_by"] == "user_id"
    assert by_name["ok"] and by_name["data"]["resolved_by"] == "display_name"
    assert {row["fact_id"] for row in by_qq["data"]["memories"]} == {group_fact.id}
    assert {row["fact_id"] for row in by_name["data"]["memories"]} == {group_fact.id}

    nonmember = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"user_id": "9999"}),
            runtime,
        )
    )
    assert not nonmember["ok"] and nonmember["error"] == "permission_denied"

    await people.observe(
        user_id="2003",
        nickname="另一个人",
        group_id="3001",
        group_card="摄影师",
    )
    ambiguous = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"display_name": "摄影师"}),
            runtime,
        )
    )
    assert not ambiguous["ok"] and ambiguous["error"] == "ambiguous_person"


@pytest.mark.asyncio
async def test_deterministic_memory_admin_uses_unified_mutation_receipt(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="command-memory-add",
        sender_user_id="1001",
        content="/ai memory add 我喜欢天文",
    )
    admin = MemoryAdminService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=service,
        ledger=ledger,
    )
    row = await admin.add_memory(
        AdminActor(
            user_id="1001",
            is_superuser=False,
            trigger_message_id=event.platform_message_id,
            conversation_key="private:1001",
            current_message_text=event.content,
            bot_user_id=event.bot_user_id,
            decision_actor_type="command",
        ),
        "1001",
        "我喜欢天文",
    )

    assert row.content == "我喜欢天文"
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
    assert receipt is not None
    assert receipt.trigger_event_id == event.id
    assert receipt.decision_actor_type == "command"
    assert receipt.new_fact_id == row.id
