"""Full normalized-event to persisted-reply integration tests."""

from __future__ import annotations

import asyncio
import json

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_normalizer import group_event, private_event
from tests.unit.test_runtime_admin import admin_stack

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.admin.config_admin import ConfigAdminService


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
async def test_ordinary_natural_language_capability_question_calls_current_user_tool(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "get_my_capabilities" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="my-capabilities",
                        function=ToolFunction(
                            name="get_my_capabilities",
                            arguments=json.dumps({"mode": "summary"}),
                        ),
                    ),
                ),
            )
        assert "get_my_capabilities" in {tool.name for tool in request.tools}
        payload = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert payload["data"]["transient_internal_reference"] is True
        assert payload["data"]["do_not_copy_verbatim_to_user"] is True
        assert payload["data"]["counts"]["self_service_operations"] == 29
        return ChatResponse(
            content="你目前有 29 项本人自助能力，其中 14 项会修改本人数据；不能修改系统配置。",
            latency_seconds=0,
        )

    provider = FakeLLMProvider(responder)
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(
        normalize_event(
            private_event(
                Message("Yuki，我能修改什么？能改多少参数？"),
                message_id=104,
            )
        ),
        sender,
    )

    assert result.reason == "chat"
    assert calls == 2
    rendered = "\n".join(message.text for message in sender.messages)
    assert rendered == "你目前有 29 项本人自助能力，其中 14 项会修改本人数据；不能修改系统配置。"
    assert "transient_internal_reference" not in rendered
    events = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.PRIVATE,
        user_id="1001",
        group_id=None,
        limit=10,
    )
    persisted = "\n".join(event.content for event in events)
    assert "transient_internal_reference" not in persisted
    assert "permission_levels" not in persisted


@pytest.mark.asyncio
async def test_ordinary_capability_payload_echo_is_neither_sent_nor_persisted(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="my-capabilities-echo",
                        function=ToolFunction(
                            name="get_my_capabilities",
                            arguments=json.dumps({"mode": "summary"}),
                        ),
                    ),
                ),
            )
        payload = next(
            message.content or "{}"
            for message in reversed(request.messages)
            if message.role == "tool"
        )
        return ChatResponse(content=payload, latency_seconds=0)

    harness = build_harness(
        database,
        make_settings(database.url),
        FakeLLMProvider(responder),
    )
    sender = MemorySender()
    result = await harness.processor.handle(
        normalize_event(private_event(Message("Yuki，我能修改什么？"), message_id=105)),
        sender,
    )

    assert result.reason == "chat"
    rendered = "\n".join(message.text for message in sender.messages)
    assert "内部读取" in rendered
    assert "transient_internal_reference" not in rendered
    assert "do_not_copy_verbatim_to_user" not in rendered
    events = await EventLedgerRepository(database).list_recent(
        scope_type=ScopeType.PRIVATE,
        user_id="1001",
        group_id=None,
        limit=10,
    )
    persisted = "\n".join(event.content for event in events)
    assert "transient_internal_reference" not in persisted
    assert "do_not_copy_verbatim_to_user" not in persisted


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

    monkeypatch.setattr("qq_ai_bot.services.reply_sequence.asyncio.sleep", fake_sleep)
    provider = FakeLLMProvider(lambda _request: "第一句。第二句！")
    settings = make_settings(
        database.url,
        daily_chat_message_delay_min_seconds=3,
        daily_chat_message_delay_max_seconds=5,
    )
    harness = build_harness(database, settings, provider)
    harness.processor._chat._reply_sequence._random_uniform = fake_uniform
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


@pytest.mark.asyncio
async def test_natural_and_deterministic_config_entrypoints_share_runtime_instance(
    database: Database,
) -> None:
    calls = 0

    def responder(_request: object) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="natural-set",
                        function=ToolFunction(
                            name="admin_set_config",
                            arguments=json.dumps(
                                {
                                    "key": "planner.max_pending_messages",
                                    "value": 10,
                                    "scope_type": "global",
                                    "scope_id": "",
                                }
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="已立即改为 10。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    runtime, capabilities = admin_stack(database)
    harness.processor._runtime_config = runtime
    harness.processor._config_admin = ConfigAdminService(runtime)
    harness.processor._chat._runtime_config = runtime
    harness.processor._chat.set_admin_tools(capabilities)

    natural_sender = MemorySender()
    natural = normalize_event(
        private_event(
            Message("把每小时自动插话次数改成 10"),
            message_id=410,
            user_id=9000,
        )
    )
    natural_result = await harness.processor.handle(natural, natural_sender)
    assert natural_result.reason == "chat"

    command_sender = MemorySender()
    command = normalize_event(
        private_event(
            Message("/ai config get planner.max_pending_messages"),
            message_id=411,
            user_id=9000,
        )
    )
    command_result = await harness.processor.handle(command, command_sender)
    assert command_result.reason == "command_config"
    assert "10" in command_sender.messages[0].text
    assert harness.processor._config_admin._runtime_config is runtime
