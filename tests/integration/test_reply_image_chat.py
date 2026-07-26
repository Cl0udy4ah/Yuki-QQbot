"""Replied-image resolution and event-linked cache integration."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.vision.fake import FakeVisionProvider


def _png() -> str:
    output = io.BytesIO()
    Image.new("RGB", (6, 6), (80, 90, 100)).save(output, format="PNG")
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


def _attachment(file: str, *, source: str) -> MessageAttachment:
    return MessageAttachment(
        kind=AttachmentKind.IMAGE,
        label="image",
        segment_index=0,
        source=source,
        file=file,
    )


@pytest.mark.asyncio
async def test_reply_to_old_image_prefers_event_cache_before_expired_resource(database) -> None:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        vision_enabled=True,
        vision_provider="fake",
        vision_base_url="https://vision.invalid/v1",
        vision_api_key="test-key",
        vision_model="fake-vision",
    )
    llm = FakeLLMProvider(lambda _request: "我看到了同一张旧图片。")
    vision = FakeVisionProvider()
    harness = build_harness(database, settings, llm, vision_provider=vision)
    common = {
        "event_type": "message:private:friend",
        "scope_type": ScopeType.PRIVATE,
        "sender": SenderIdentity(user_id="1001"),
        "bot_user_id": "9999",
        "text": "这个呢",
    }

    first = InboundMessage(
        message_id="old-image",
        attachments=(_attachment(_png(), source="current"),),
        **common,
    )
    second = InboundMessage(
        message_id="reply-image",
        reply_to_message_id="old-image",
        reply_attachments=(_attachment("expired-napcat-file", source="reply"),),
        **common,
    )

    first_result = await harness.processor.handle(first, MemorySender())
    second_result = await harness.processor.handle(second, MemorySender())

    assert first_result.reason == "chat"
    assert second_result.reason == "chat"
    assert len(vision.requests) == 1


@pytest.mark.asyncio
async def test_reply_image_without_cache_reports_expired_resource_clearly(database) -> None:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        vision_enabled=True,
        vision_provider="fake",
        vision_base_url="https://vision.invalid/v1",
        vision_api_key="test-key",
        vision_model="fake-vision",
    )
    harness = build_harness(
        database,
        settings,
        FakeLLMProvider(lambda _request: "不应调用"),
        vision_provider=FakeVisionProvider(),
    )
    message = InboundMessage(
        message_id="reply-expired-no-cache",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        bot_user_id="9999",
        text="",
        reply_to_message_id="missing-old-image",
        reply_attachments=(_attachment("expired-napcat-file", source="reply"),),
    )
    sender = MemorySender()

    result = await harness.processor.handle(message, sender)

    assert result.reason == "vision_resource_unavailable"
    assert sender.messages[-1].text == "回复中的图片资源已过期或无法读取，请重新发送原图。"
