"""Context budgets, batch projections, and SQLite worker-safety tests."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import MemoryRepository, PeopleRepository


@pytest.mark.asyncio
async def test_context_assembler_enforces_one_dynamic_character_budget(
    database: Database,
) -> None:
    settings = make_settings(database.url, max_context_characters=1200)
    harness = build_harness(database, settings)
    memories = MemoryRepository(database)
    for index in range(30):
        await memories.upsert(
            scope="person",
            user_id="1001",
            memory_key=f"person-{index}",
            content=f"人物事实 {index} " + "很长的内容" * 80,
            importance=5 if index == 0 else 1,
            limit=100,
        )
        await memories.upsert(
            scope="group",
            group_id="2001",
            memory_key=f"group-{index}",
            content=f"群事实 {index} " + "另一段很长的内容" * 80,
            importance=5 if index == 0 else 1,
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
        if item.role == "system" and "人物中心记忆与当前 QQ 场景元数据" in (item.content or "")
    )
    metadata = request.messages[metadata_index].content or ""
    payload_text = metadata.split("\n", 1)[1]
    payload = json.loads(payload_text)
    history_characters = sum(
        len(item.content or "") for item in request.messages[metadata_index + 1 :]
    )

    assert len(payload_text) <= settings.max_context_characters * 55 // 100
    assert len(payload_text) + history_characters <= settings.max_context_characters
    assert request.messages[-1].content == "[QQ 1001] 请根据已有信息简短回答"
    assert payload["current_person"]["user_id"] == "1001"
    assert len(payload["current_person"]["memories"]) < 30
    assert len(payload["group_memories"]) < 30


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
