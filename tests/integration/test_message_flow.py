"""Full normalized-event to persisted-reply integration tests."""

from __future__ import annotations

import asyncio

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_normalizer import group_event, private_event

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.domain.conversations import ConversationIdentity
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database


@pytest.mark.asyncio
async def test_private_and_group_mention_end_to_end(database: Database) -> None:
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)

    private_sender = MemorySender()
    private = normalize_event(private_event(Message("private question"), message_id=101))
    private_result = await harness.processor.handle(private, private_sender)

    group_sender = MemorySender()
    group = normalize_event(
        group_event(
            Message([MessageSegment.at(9999), MessageSegment.text("group question")]),
            message_id=102,
        )
    )
    group_result = await harness.processor.handle(group, group_sender)

    assert private_result.reason == "chat" and group_result.reason == "chat"
    assert private_sender.messages[0].text.endswith("private question")
    assert group_sender.messages[0].text.endswith("group question")
    assert await harness.conversations.count_messages(ConversationIdentity.private("1001")) == 2
    assert (
        await harness.conversations.count_messages(ConversationIdentity.group("2001", "1001")) == 2
    )


@pytest.mark.asyncio
async def test_short_plain_chat_is_sent_as_one_message_per_sentence(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delay_bounds: list[tuple[float, float]] = []
    delays: list[float] = []

    def fake_uniform(minimum: float, maximum: float) -> float:
        delay_bounds.append((minimum, maximum))
        return 4.0

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("qq_ai_bot.services.chat.random.uniform", fake_uniform)
    monkeypatch.setattr("qq_ai_bot.services.chat.asyncio.sleep", fake_sleep)
    provider = FakeLLMProvider(lambda _request: "第一句。第二句！")
    settings = make_settings(
        database.url,
        daily_chat_message_delay_min_seconds=3,
        daily_chat_message_delay_max_seconds=5,
    )
    harness = build_harness(database, settings, provider)
    sender = MemorySender()
    event = normalize_event(private_event(Message("聊聊天"), message_id=103))

    result = await harness.processor.handle(event, sender)

    assert result.reason == "chat"
    assert result.sent_messages == 2
    assert [message.text for message in sender.messages] == ["第一句。", "第二句！"]
    assert delay_bounds == [(3.0, 5.0)]
    assert delays == [4.0]
    history = await harness.conversations.list_context(
        ConversationIdentity.private("1001"),
        max_messages=10,
        max_characters=1000,
    )
    assert [item.content for item in history[-2:]] == ["第一句。", "第二句！"]


@pytest.mark.asyncio
async def test_ten_concurrent_conversations_do_not_cross_context(database: Database) -> None:
    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    senders = [MemorySender() for _ in range(10)]
    messages = [
        normalize_event(
            private_event(
                Message(f"unique-{index}"),
                message_id=200 + index,
                user_id=1001 + index,
            )
        )
        for index in range(10)
    ]

    await asyncio.gather(
        *(
            harness.processor.handle(message, sender)
            for message, sender in zip(messages, senders, strict=True)
        )
    )

    assert len(provider.requests) == 10
    for index in range(10):
        identity = ConversationIdentity.private(str(1001 + index))
        history = await harness.conversations.list_context(
            identity, max_messages=10, max_characters=1000
        )
        contents = [item.content for item in history]
        expected = f"FakeLLM: [QQ {1001 + index}] unique-{index}"
        assert contents == [f"unique-{index}", expected]
        assert senders[index].messages[0].text == expected
