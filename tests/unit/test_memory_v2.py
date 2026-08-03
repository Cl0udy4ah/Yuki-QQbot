"""Identity, lifecycle, queue, and context contracts for Memory V2."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.memory.enums import (
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectResolver
from qq_ai_bot.memory.validation import MemoryClaimValidator
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.concurrency import ConcurrencyManager


def _event(
    *,
    event_id: int = 1,
    sender_user_id: str = "1001",
    scope_type: ScopeType = ScopeType.PRIVATE,
    group_id: str | None = None,
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"event-{event_id}",
        scope_type=scope_type,
        sender_user_id=sender_user_id,
        direction="inbound",
        content="我准备考研",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id=group_id,
        private_peer_user_id=sender_user_id if scope_type is ScopeType.PRIVATE else None,
    )


def _claim(**overrides: object) -> MemoryClaim:
    values: dict[str, object] = {
        "subject_ref": "speaker",
        "scope_type": "person",
        "kind": "fact",
        "memory_key": "education:plan",
        "category": "education",
        "content": "准备考研",
        "evidence_quote": "我准备考研",
        "importance": 4,
        "confidence": 0.9,
        "source_type": "automatic",
    }
    values.update(overrides)
    return MemoryClaim.model_validate(values)


def test_extraction_schema_rejects_model_selected_identity_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryClaim.model_validate({**_claim().model_dump(), "user_id": "2002"})
    with pytest.raises(ValidationError):
        MemoryClaim.model_validate({**_claim().model_dump(), "source_event_id": 999})


def test_subject_resolver_only_allows_primary_speaker_and_current_group() -> None:
    private = _event()
    group = _event(scope_type=ScopeType.GROUP, group_id="3001")

    assert [item.subject_ref for item in SubjectResolver.available(private)] == ["speaker"]
    assert [item.subject_ref for item in SubjectResolver.available(group)] == [
        "speaker",
        "group",
    ]
    assert (
        SubjectResolver.resolve(
            private,
            subject_ref="group",
            scope_type=MemoryScopeType.GROUP,
        )
        is None
    )
    assert (
        SubjectResolver.resolve(
            group,
            subject_ref="李四",
            scope_type=MemoryScopeType.PERSON,
        )
        is None
    )


def test_validator_owns_event_and_speaker_identity() -> None:
    event = _event(event_id=42, sender_user_id="1001")
    validated = MemoryClaimValidator().validate(_claim(), event)
    assert validated is not None
    fact, evidence = validated
    assert fact.subject_user_id == "1001"
    assert fact.group_id is None
    assert evidence.event_id == 42
    assert evidence.source_speaker_user_id == "1001"


def test_validator_rejects_unknown_subject_and_private_group_claims() -> None:
    event = _event()
    validator = MemoryClaimValidator()
    assert validator.validate(_claim(subject_ref="other_person"), event) is None
    assert (
        validator.validate(
            _claim(subject_ref="group", scope_type="group"),
            event,
        )
        is None
    )


def test_validator_rejects_context_only_or_semantically_different_claims() -> None:
    event = _event()
    validator = MemoryClaimValidator()
    assert validator.validate(_claim(evidence_quote="上下文里有人准备考研"), event) is None
    assert (
        validator.validate(
            _claim(content="准备出国", evidence_quote="我准备考研"),
            event,
        )
        is None
    )


@pytest.mark.parametrize(
    ("text", "content"),
    [
        ("江环是魅魔", "江环是魅魔"),
        ("廉政这爱好倒是挺稳定的，六年前到现在都没变", "廉政的爱好很稳定"),
    ],
)
def test_validator_rejects_named_other_misattributed_to_speaker(
    text: str,
    content: str,
) -> None:
    event = replace(_event(scope_type=ScopeType.GROUP, group_id="3001"), content=text)

    assert (
        MemoryClaimValidator().validate(
            _claim(
                scope_type="person_group",
                content=content,
                evidence_quote=text,
            ),
            event,
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    ["我喜欢猫娘", "最近喜欢猫娘", "爱好是摄影", "大家叫我队长"],
)
def test_validator_keeps_first_person_and_subjectless_self_reports(text: str) -> None:
    event = replace(_event(scope_type=ScopeType.GROUP, group_id="3001"), content=text)

    assert (
        MemoryClaimValidator().validate(
            _claim(
                scope_type="person",
                content=text,
                evidence_quote=text,
            ),
            event,
        )
        is not None
    )


def test_interaction_preferences_are_not_stored_as_person_facts() -> None:
    event = _event()
    event = replace(event, content="以后回复我时请简短一点")
    validated = MemoryClaimValidator().validate_claim(
        _claim(
            content="回复时简短一点",
            evidence_quote="以后回复我时请简短一点",
        ),
        event,
    )
    assert validated is not None
    assert validated.fact.kind is MemoryKind.PREFERENCE


async def _append_event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    user_id: str = "1001",
    content: str = "我准备考研",
    group_id: str | None = None,
    direction: str = "inbound",
    sender_is_bot: bool = False,
) -> EventRecord:
    row, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=user_id,
        direction=direction,
        content=content,
        group_id=group_id,
        private_peer_user_id=None if group_id else user_id,
        sender_is_bot=sender_is_bot,
    )
    return row


def _fact(
    *,
    content: str,
    memory_key: str = "education:plan",
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
    user_id: str | None = "1001",
    group_id: str | None = None,
    scope_type: MemoryScopeType = MemoryScopeType.PERSON,
    kind: MemoryKind = MemoryKind.FACT,
) -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=scope_type,
        subject_user_id=user_id,
        group_id=group_id,
        kind=kind,
        memory_key=memory_key,
        category="test",
        content=content,
        importance=4,
        confidence=0.9,
        source_type=source_type,
    )


@pytest.mark.asyncio
async def test_same_fact_reuses_active_row_and_accumulates_evidence(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    first_event = await _append_event(ledger, message_id="fact-1")
    second_event = await _append_event(ledger, message_id="fact-2")
    service = MemoryFactService(MemoryFactRepository(database))

    first = await service.remember(
        _fact(content="准备考研"),
        evidence=MemoryEvidenceCreate(
            event_id=first_event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="我准备考研",
        ),
    )
    repeated = await service.remember(
        _fact(content="  准备考研\n"),
        evidence=MemoryEvidenceCreate(
            event_id=second_event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="还是准备考研",
        ),
    )

    assert repeated.id == first.id
    assert repeated.evidence_count == 2
    assert len(await service.list_person("1001")) == 1


@pytest.mark.asyncio
async def test_changed_fact_supersedes_old_but_automatic_cannot_replace_explicit(
    database: Database,
) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    first = await service.remember(_fact(content="准备考研"))
    changed = await service.remember(_fact(content="决定直接工作"))

    assert changed.id != first.id
    assert changed.supersedes_id == first.id
    old = await repository.get_fact(first.id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED

    explicit = await service.remember(
        _fact(
            content="只喝红茶",
            memory_key="drink:preference",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    rejected = await service.remember(_fact(content="喜欢咖啡", memory_key="drink:preference"))
    assert rejected.id == explicit.id
    assert rejected.content == "只喝红茶"


@pytest.mark.asyncio
async def test_fact_and_evidence_write_rolls_back_as_one_transaction(database: Database) -> None:
    service = MemoryFactService(MemoryFactRepository(database))
    with pytest.raises(IntegrityError):
        await service.remember(
            _fact(content="事务测试"),
            evidence=MemoryEvidenceCreate(
                event_id=999_999,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.SELF_STATEMENT,
                excerpt="不存在的事件",
            ),
        )
    assert not await service.list_person("1001")


@pytest.mark.asyncio
async def test_jobs_accept_only_real_inbound_non_bot_events(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    inbound = await _append_event(ledger, message_id="job-inbound")
    outbound = await _append_event(
        ledger,
        message_id="job-outbound",
        user_id="8000",
        direction="outbound",
        sender_is_bot=True,
    )
    bot_inbound = await _append_event(
        ledger,
        message_id="job-bot",
        user_id="7000",
        sender_is_bot=True,
    )
    blank = await _append_event(ledger, message_id="job-blank", content="   ")
    jobs = MemoryJobRepository(database)

    assert await jobs.enqueue(inbound.id, "private:1001")
    assert not await jobs.enqueue(inbound.id, "private:1001")
    assert not await jobs.enqueue(outbound.id, "private:1001")
    assert not await jobs.enqueue(bot_inbound.id, "private:7000")
    assert not await jobs.enqueue(blank.id, "private:1001")


class _PerEventProvider(LLMProvider):
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        payload = json.loads(request.messages[-1].content or "{}")
        self.inputs.append(payload)
        content = str(payload["primary_event"]["content"])
        return ChatResponse(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "subject_ref": "speaker",
                            "scope_type": "person",
                            "kind": "fact",
                            "memory_key": "primary-event",
                            "category": "test",
                            "content": content,
                            "evidence_quote": content,
                            "importance": 3,
                            "confidence": 0.9,
                            "source_type": "automatic",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            latency_seconds=0,
        )


@pytest.mark.asyncio
async def test_worker_extracts_and_commits_each_event_independently(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    first = await _append_event(
        ledger,
        message_id="worker-1",
        user_id="1001",
        content="第一个人的事实",
    )
    second = await _append_event(
        ledger,
        message_id="worker-2",
        user_id="1002",
        content="第二个人的事实",
    )
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(first.id, "private:1001")
    assert await jobs.enqueue(second.id, "private:1002")
    facts = MemoryFactService(MemoryFactRepository(database))
    provider = _PerEventProvider()
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_events=20),
        jobs=jobs,
        facts=facts,
        ledger=ledger,
        provider=provider,
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 2
    assert len(provider.inputs) == 2
    assert [row.content for row in await facts.list_person("1001")] == ["第一个人的事实"]
    assert [row.content for row in await facts.list_person("1002")] == ["第二个人的事实"]
    assert all(len(item["available_subjects"]) == 1 for item in provider.inputs)


class _CancelledProvider(LLMProvider):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_worker_propagates_cancellation(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="worker-cancel")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    worker = MemoryWorker(
        settings=make_settings(database.url),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        provider=_CancelledProvider(),
        concurrency=ConcurrencyManager(1),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.process_once()


@pytest.mark.asyncio
async def test_context_keeps_facts_in_current_entity_blocks_only(database: Database) -> None:
    memories = MemoryFactService(MemoryFactRepository(database))
    await memories.remember(_fact(content="只属于当前人物"))
    await memories.remember(
        _fact(
            content="只属于当前群",
            memory_key="group:topic",
            user_id=None,
            group_id="2001",
            scope_type=MemoryScopeType.GROUP,
        )
    )
    await memories.remember(
        _fact(
            content="当前群内称呼",
            memory_key="member:alias",
            group_id="2001",
            scope_type=MemoryScopeType.PERSON_GROUP,
        )
    )
    await memories.remember(
        _fact(
            content="另一个群的秘密",
            memory_key="member:other",
            group_id="2002",
            scope_type=MemoryScopeType.PERSON_GROUP,
        )
    )
    await memories.remember(_fact(content="另一个人的秘密", memory_key="other", user_id="1002"))
    harness = build_harness(database, make_settings(database.url, max_context_characters=20_000))
    await harness.groups.set_enabled("2001", True)
    message = InboundMessage(
        message_id="memory-context",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前用户"),
        text="只属于当前人物，只属于当前群，当前群内称呼",
        group_id="2001",
        mentioned_user_ids=("1002",),
        mentions_bot=True,
        bot_user_id="8000",
    )
    await harness.processor.handle(message, MemorySender())
    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    envelope = next(
        item.content or ""
        for item in request.messages
        if item.role == "system" and '"id":"context.people_and_scene"' in (item.content or "")
    )
    items = json.loads(envelope[envelope.index("[") :])
    context = next(item["data"] for item in items if item["id"] == "context.people_and_scene")
    blocks = {item["id"]: item["data"] for item in context["items"]}

    assert [item["content"] for item in blocks["current_person"]["facts"]] == ["只属于当前人物"]
    assert [item["content"] for item in blocks["current_person_in_group"]["facts"]] == [
        "当前群内称呼"
    ]
    assert [item["content"] for item in blocks["current_group"]["facts"]] == ["只属于当前群"]
    related = blocks["related_person.0"]
    assert set(related) == {"user_id", "display_name", "group_card"}
    assert "另一个人的秘密" not in envelope


@pytest.mark.asyncio
async def test_context_limits_mentioned_member_facts_to_current_group_block(
    database: Database,
) -> None:
    memories = MemoryFactService(MemoryFactRepository(database))
    person_fact = await memories.remember(
        _fact(content="小李喜欢水彩绘画", memory_key="hobby:painting", user_id="1002")
    )
    group_fact = await memories.remember(
        _fact(
            content="小李在本群负责美术",
            memory_key="role:artist",
            user_id="1002",
            group_id="2001",
            scope_type=MemoryScopeType.PERSON_GROUP,
        )
    )
    harness = build_harness(database, make_settings(database.url, max_context_characters=20_000))
    await harness.groups.set_enabled("2001", True)
    await harness.profiles.upsert(
        user_id="1002",
        nickname="小李",
        group_id="2001",
        group_card="画师小李",
    )
    message = InboundMessage(
        message_id="referenced-memory-context",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前用户"),
        text="小李喜欢水彩绘画，也在本群负责美术吗",
        group_id="2001",
        mentioned_user_ids=("1002",),
        mentions_bot=True,
        bot_user_id="8000",
    )
    await harness.processor.handle(message, MemorySender())
    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    envelope = next(
        item.content or ""
        for item in request.messages
        if item.role == "system" and '"id":"context.people_and_scene"' in (item.content or "")
    )
    items = json.loads(envelope[envelope.index("[") :])
    context = next(item["data"] for item in items if item["id"] == "context.people_and_scene")
    blocks = {item["id"]: item["data"] for item in context["items"]}
    referenced = blocks["referenced_person.0"]

    assert referenced["user_id"] == "1002"
    assert referenced["person_facts"] == []
    assert [fact["fact_id"] for fact in referenced["group_facts"]] == [group_fact.id]
    assert person_fact.id not in {
        fact["fact_id"]
        for values in (referenced["person_facts"], referenced["group_facts"])
        for fact in values
    }
    assert blocks["current_person"]["facts"] == []
    assert "另一个群的秘密" not in envelope
