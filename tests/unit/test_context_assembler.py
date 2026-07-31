"""Context budgets, batch projections, and SQLite worker-safety tests."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import PeopleRepository
from qq_ai_bot.services.context_assembler import ContextAssembler


@pytest.mark.asyncio
async def test_context_assembler_enforces_one_dynamic_character_budget(
    database: Database,
) -> None:
    settings = make_settings(database.url, max_context_characters=1200)
    harness = build_harness(database, settings)
    memories = MemoryFactService(MemoryFactRepository(database))
    for index in range(30):
        await memories.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="1001",
                kind="fact",
                category="fact",
                source_type=MemorySourceType.AUTOMATIC,
                confidence=0.9,
                memory_key=f"person-{index}",
                content=f"人物事实 {index} " + "很长的内容" * 80,
                importance=5 if index == 0 else 1,
            ),
            limit=100,
        )
        await memories.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.GROUP,
                group_id="2001",
                kind="fact",
                category="fact",
                source_type=MemorySourceType.AUTOMATIC,
                confidence=0.9,
                memory_key=f"group-{index}",
                content=f"群事实 {index} " + "另一段很长的内容" * 80,
                importance=5 if index == 0 else 1,
            ),
            limit=100,
        )
    await harness.groups.set_enabled("2001", True)

    message = InboundMessage(
        message_id="bounded-context",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="测试用户", group_card="测试名片"),
        text="请根据已有信息简短回答",
        group_id="2001",
        mentions_bot=True,
        bot_user_id="9999",
    )
    await harness.processor.handle(message, MemorySender())

    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    metadata_index = next(
        index
        for index, item in enumerate(request.messages)
        if item.role == "system" and '"id":"context.people_and_scene"' in (item.content or "")
    )
    envelope = request.messages[metadata_index].content or ""
    envelope_items = json.loads(envelope[envelope.index("[") :])
    context_item = next(item for item in envelope_items if item["id"] == "context.people_and_scene")
    payload = context_item["data"]
    payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload_items = {item["id"]: item["data"] for item in payload["items"]}
    history_characters = sum(
        len(item.content or "") for item in request.messages[metadata_index + 1 :]
    )

    assert len(payload_text) <= settings.max_context_characters * 55 // 100
    assert len(payload_text) + history_characters <= settings.max_context_characters
    assert request.messages[-1].content == "[QQ 1001] 请根据已有信息简短回答"
    assert payload_items["current_person"]["user_id"] == "1001"
    assert len(payload_items["current_person"]["facts"]) < 30
    assert len(payload_items["current_group"]["facts"]) < 30
    assert not any(key.startswith("person_memory.") for key in payload_items)
    assert not any(key.startswith("current_group.fact.") for key in payload_items)


@pytest.mark.asyncio
async def test_related_people_batch_queries_keep_group_cards_isolated(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(
        user_id="1001",
        nickname="甲",
        group_id="2001",
        group_card="一群名片",
    )
    await people.observe(
        user_id="1001",
        nickname="甲",
        group_id="2002",
        group_card="二群名片",
    )
    await people.observe(
        user_id="1002",
        nickname="乙",
        group_id="2001",
        group_card="乙的一群名片",
    )

    first_group = await people.get_many(("1001", "1002"), group_id="2001")
    second_group = await people.get_many(("1001", "1002"), group_id="2002")

    assert first_group["1001"].display_name == "一群名片"
    assert first_group["1002"].display_name == "乙的一群名片"
    assert second_group["1001"].display_name == "二群名片"
    assert second_group["1002"].display_name == "乙"


@pytest.mark.asyncio
async def test_sqlite_connections_enable_wal_and_bounded_busy_wait(database: Database) -> None:
    async with database.sessions() as session:
        journal_mode = await session.scalar(text("PRAGMA journal_mode"))
        busy_timeout = await session.scalar(text("PRAGMA busy_timeout"))
        foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))

    assert str(journal_mode).casefold() == "wal"
    assert int(busy_timeout or 0) == 5000
    assert int(foreign_keys or 0) == 1


@pytest.mark.asyncio
async def test_only_facts_surviving_context_budget_are_marked_used(database: Database) -> None:
    repository = MemoryFactRepository(database)
    memories = MemoryFactService(repository)
    selected = await memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            category="profile",
            source_type=MemorySourceType.AUTOMATIC,
            confidence=0.9,
            memory_key="selected",
            content="短事实",
            importance=5,
        )
    )
    omitted = await memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind="fact",
            category="profile",
            source_type=MemorySourceType.AUTOMATIC,
            confidence=0.9,
            memory_key="omitted",
            content="不会进入预算的事实" * 200,
            importance=1,
        )
    )
    context = {
        "current_person": {
            "user_id": "1001",
            "nickname": "测试",
            "display_name": "测试",
            "facts": [
                {"fact_id": selected.id, "content": selected.content, "importance": 5},
                {"fact_id": omitted.id, "content": omitted.content, "importance": 1},
            ],
        },
        "scene": {"type": "private", "group_id": None},
    }
    contributions = ContextAssembler._context_contributions(context)
    required_cost = sum(item.cost for item in contributions if item.required)
    selected_cost = next(item.cost for item in contributions if item.id == "person_memory.0")
    _, fact_ids = ContextAssembler._fit_metadata(
        context,
        required_cost + selected_cost,
    )
    await memories.mark_used(fact_ids)

    selected_row = await repository.get_fact(selected.id)
    omitted_row = await repository.get_fact(omitted.id)
    assert fact_ids == (selected.id,)
    assert selected_row is not None and selected_row.last_used_at is not None
    assert omitted_row is not None and omitted_row.last_used_at is None
