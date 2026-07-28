"""OneBot outbound sender behavior tests."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from qq_ai_bot.adapters.onebot.sender import OneBotSender
from qq_ai_bot.domain.messages import OutboundMessage


@pytest.mark.asyncio
async def test_sender_emits_plain_text_without_reply_segment() -> None:
    bot = AsyncMock()
    event = object()
    sender = OneBotSender(cast(Bot, bot), cast(MessageEvent, event))

    await sender.send(OutboundMessage(text="普通消息"))

    bot.send.assert_awaited_once()
    call = bot.send.await_args
    assert call.kwargs["event"] is event
    payload = call.kwargs["message"]
    assert isinstance(payload, MessageSegment)
    assert payload.type == "text"
    assert payload.data == {"text": "普通消息"}


@pytest.mark.asyncio
async def test_sender_can_quote_one_validated_message() -> None:
    bot = AsyncMock()
    event = object()
    sender = OneBotSender(cast(Bot, bot), cast(MessageEvent, event))

    await sender.send(OutboundMessage(text="这条回答指向你。", reply_to_message_id="12345"))

    payload = bot.send.await_args.kwargs["message"]
    assert isinstance(payload, Message)
    assert [(segment.type, segment.data) for segment in payload] == [
        ("reply", {"id": "12345"}),
        ("text", {"text": "这条回答指向你。"}),
    ]
