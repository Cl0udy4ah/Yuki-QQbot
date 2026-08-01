"""OneBot outbound sender behavior tests."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from qq_ai_bot.adapters.onebot.sender import (
    OneBotSender,
    OneBotSendError,
    parse_onebot_send_receipt,
)
from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage


@pytest.mark.asyncio
async def test_sender_emits_plain_text_without_reply_segment() -> None:
    bot = AsyncMock()
    bot.send.return_value = 10001
    event = object()
    sender = OneBotSender(cast(Bot, bot), cast(MessageEvent, event))

    receipt = await sender.send(OutboundMessage(text="普通消息"))

    bot.send.assert_awaited_once()
    call = bot.send.await_args
    assert call.kwargs["event"] is event
    payload = call.kwargs["message"]
    assert isinstance(payload, MessageSegment)
    assert payload.type == "text"
    assert payload.data == {"text": "普通消息"}
    assert receipt.platform_message_id == "10001"


@pytest.mark.asyncio
async def test_sender_can_quote_one_validated_message() -> None:
    bot = AsyncMock()
    bot.send.return_value = {"message_id": 10002}
    event = object()
    sender = OneBotSender(cast(Bot, bot), cast(MessageEvent, event))

    await sender.send(OutboundMessage(text="这条回答指向你。", reply_to_message_id="12345"))

    payload = bot.send.await_args.kwargs["message"]
    assert isinstance(payload, Message)
    assert [(segment.type, segment.data) for segment in payload] == [
        ("reply", {"id": "12345"}),
        ("text", {"text": "这条回答指向你。"}),
    ]


@pytest.mark.asyncio
async def test_sender_encodes_local_audio_only_at_onebot_boundary(tmp_path: Path) -> None:
    audio = b"RIFF-local-wave"
    path = tmp_path / "voice.wav"
    path.write_bytes(audio)
    bot = AsyncMock()
    bot.send.return_value = {"id": "10003"}
    event = object()
    sender = OneBotSender(cast(Bot, bot), cast(MessageEvent, event))

    await sender.send(
        OutboundMessage(
            media=(
                OutboundMedia(
                    kind=AttachmentKind.AUDIO,
                    mime_type="audio/wav",
                    local_path=str(path),
                ),
            )
        )
    )

    payload = bot.send.await_args.kwargs["message"]
    assert isinstance(payload, Message)
    assert [segment.type for segment in payload] == ["record"]
    assert payload[0].data["file"] == "base64://" + base64.b64encode(audio).decode("ascii")


class _ReceiptObject:
    def __init__(self, message_id: object) -> None:
        self.message_id = message_id


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (123, "123"),
        ("456", "456"),
        ({"message_id": 789}, "789"),
        ({"message_id": 0}, "0"),
        ({"id": "901"}, "901"),
        (_ReceiptObject(234), "234"),
    ),
)
def test_onebot_receipt_parser_accepts_supported_message_ids(
    value: object,
    expected: str,
) -> None:
    assert parse_onebot_send_receipt(value).platform_message_id == expected


@pytest.mark.parametrize("value", (None, "", "  ", {}, {"message_id": ""}, True))
def test_onebot_receipt_parser_rejects_missing_or_empty_ids(value: object) -> None:
    with pytest.raises(OneBotSendError):
        parse_onebot_send_receipt(value)
