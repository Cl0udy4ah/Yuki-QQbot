"""Bounded shared group memory extraction and isolation tests."""

from __future__ import annotations

import pytest
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import MemoryRepository


def group_message(
    text: str,
    *,
    message_id: str,
    group_id: str = "2001",
    user_id: str = "1001",
    nickname: str = "发言者",
    mentioned_user_ids: tuple[str, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id=user_id, nickname=nickname),
        text=text,
        group_id=group_id,
        mentions_bot=True,
        mentioned_user_ids=mentioned_user_ids,
    )


@pytest.mark.asyncio
async def test_repository_updates_and_hard_limits_each_group(database: Database) -> None:
    repository = MemoryRepository(database)
    for index in range(35):
        await repository.upsert(
            scope="group",
            group_id="2001",
            memory_key=f"fact_{index}",
            content=f"事实 {index}",
            limit=30,
        )
    await repository.upsert(
        scope="group",
        group_id="2002",
        memory_key="other_group",
        content="另一个群",
        limit=30,
    )

    first_group = await repository.list_group("2001", limit=30)
    second_group = await repository.list_group("2002", limit=30)
    assert len(first_group) == 30
    assert all(memory.group_id == "2001" for memory in first_group)
    assert [memory.content for memory in second_group] == ["另一个群"]

    existing_key = first_group[-1].memory_key
    await repository.upsert(
        scope="group",
        group_id="2001",
        memory_key=existing_key,
        content="更新后的事实",
        limit=30,
    )
    updated = await repository.list_group("2001", limit=30)
    assert len(updated) == 30
    assert any(
        item.memory_key == existing_key and item.content == "更新后的事实" for item in updated
    )


@pytest.mark.asyncio
async def test_triggered_group_chat_queues_memory_instead_of_extracting_synchronously(
    database: Database,
) -> None:
    extraction_count = 0

    def responder(request: object) -> str:
        nonlocal extraction_count
        messages = request.messages  # type: ignore[attr-defined]
        if "群聊共享记忆提取器" in messages[0].content:
            extraction_count += 1
            name = "小明" if extraction_count == 1 else "老明"
            return (
                '{"upserts":[{"key":"member:mentioned:name","content":'
                f'"被提及成员在本群叫{name}"'
                '}],"delete_keys":[]}'
            )
        return "知道了。"

    provider = FakeLLMProvider(responder)
    harness = build_harness(database, make_settings(database.url), provider)
    first = group_message(
        "[提及成员1]叫小明",
        message_id="memory-first",
        mentioned_user_ids=("12345678",),
    )
    first_result = await harness.processor.handle(first, MemorySender())

    assert first_result.reason == "chat"
    memories = await MemoryRepository(database).list_group("2001", limit=30)
    assert not memories
    assert len(provider.requests) == 1

    second = group_message(
        "[提及成员1]现在叫老明",
        message_id="memory-second",
        mentioned_user_ids=("12345678",),
    )
    await harness.processor.handle(second, MemorySender())
    memories = await MemoryRepository(database).list_group("2001", limit=30)
    assert not memories
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_group_memories_never_cross_group_or_private_scope(database: Database) -> None:
    def responder(request: object) -> str:
        messages = request.messages  # type: ignore[attr-defined]
        if "群聊共享记忆提取器" in messages[0].content:
            return '{"upserts":[{"key":"group:topic","content":"一群喜欢猫"}],"delete_keys":[]}'
        return "回复。"

    provider = FakeLLMProvider(responder)
    harness = build_harness(database, make_settings(database.url), provider)
    await harness.processor.handle(
        group_message("我们喜欢猫", message_id="group-one"),
        MemorySender(),
    )
    await MemoryRepository(database).upsert(
        scope="group",
        group_id="2001",
        memory_key="group:topic",
        content="一群喜欢猫",
        limit=100,
    )

    await harness.processor.handle(
        group_message("另一个群", message_id="group-two", group_id="2002"),
        MemorySender(),
    )
    second_group_main = provider.requests[1]
    assert all(
        "一群喜欢猫" not in (message.content or "") for message in second_group_main.messages
    )

    private = InboundMessage(
        message_id="private-no-memory",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname="私聊用户"),
        text="私聊",
    )
    await harness.processor.handle(private, MemorySender())
    private_request = provider.requests[2]
    assert all("一群喜欢猫" not in (message.content or "") for message in private_request.messages)


@pytest.mark.asyncio
async def test_untriggered_group_chat_is_observed_without_synchronous_extraction(
    database: Database,
) -> None:
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    message = group_message("普通群聊", message_id="not-triggered")
    message = InboundMessage(
        message_id=message.message_id,
        event_type=message.event_type,
        scope_type=message.scope_type,
        sender=message.sender,
        text=message.text,
        group_id=message.group_id,
        mentions_bot=False,
    )

    result = await harness.processor.handle(message, MemorySender())

    assert not result.handled
    assert not provider.requests
    assert not await MemoryRepository(database).list_group("2001", limit=1)


@pytest.mark.asyncio
async def test_invalid_extractor_output_does_not_break_chat(database: Database) -> None:
    provider = FakeLLMProvider(lambda _request: "不是 JSON")
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()

    result = await harness.processor.handle(
        group_message("聊聊天", message_id="invalid-memory"),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[0].text == "不是 JSON"
    assert not await MemoryRepository(database).list_group("2001", limit=1)
