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
        assert contents == [f"unique-{index}", f"FakeLLM: unique-{index}"]
        assert senders[index].messages[0].text == f"FakeLLM: unique-{index}"
