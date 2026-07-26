"""Image, image-emoji, and replied-media normalization tests."""

from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Reply, Sender

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.domain.messages import AttachmentKind


def _private_event(message: Message, *, message_id: int = 1) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=9999,
        post_type="message",
        sub_type="friend",
        user_id=1001,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=1001, nickname="tester"),
    )


def test_image_segment_preserves_all_safe_fields_and_order() -> None:
    image = MessageSegment(
        "image",
        {
            "file": "trusted-file-id",
            "url": "https://example.test/a.png?signature=secret",
            "summary": "[动画表情]",
            "sub_type": "1",
            "file_size": "1234",
            "key": "media-key",
            "emoji_id": "emoji-1",
            "emoji_package_id": "package-2",
        },
    )
    normalized = normalize_event(_private_event(Message([MessageSegment.text("看"), image])))

    assert normalized.text == "看"
    assert len(normalized.attachments) == 1
    attachment = normalized.attachments[0]
    assert attachment.kind is AttachmentKind.IMAGE
    assert attachment.segment_index == 1
    assert attachment.source == "current"
    assert attachment.file == "trusted-file-id"
    assert attachment.url == "https://example.test/a.png?signature=secret"
    assert attachment.summary == "[动画表情]"
    assert attachment.sub_type == "1"
    assert attachment.file_size == 1234
    assert attachment.key == "media-key"
    assert attachment.emoji_id == "emoji-1"
    assert attachment.emoji_package_id == "package-2"


def test_reply_image_is_separate_from_current_image_metadata() -> None:
    event = _private_event(Message("这个呢"), message_id=2)
    event.reply = Reply(
        time=1,
        message_type="private",
        message_id=8,
        real_id=8,
        sender=Sender(user_id=1002),
        message=Message(
            MessageSegment(
                "image",
                {"file": "reply-file", "summary": "旧图片"},
            )
        ),
    )

    normalized = normalize_event(event)

    assert normalized.attachments == ()
    assert normalized.reply_text is None
    assert len(normalized.reply_attachments) == 1
    assert normalized.reply_attachments[0].source == "reply"
    assert normalized.reply_attachments[0].file == "reply-file"
    assert normalized.reply_to_message_id == "8"
    assert normalized.reply_segments[0]["type"] == "image"


def test_unicode_emoji_stays_text_and_unknown_face_keeps_id() -> None:
    normalized = normalize_event(
        _private_event(Message([MessageSegment.text("🙂"), MessageSegment.face(987654)]))
    )

    assert normalized.text == "🙂[QQ表情：ID 987654]"
    assert normalized.attachments == ()


def test_inline_image_payload_is_available_for_turn_but_scrubbed_from_ledger_segments() -> None:
    image = MessageSegment(
        "image",
        {
            "file": "base64://file-secret",
            "url": "data:image/png;base64,url-secret",
            "base64": "raw-base64-secret",
        },
    )

    normalized = normalize_event(_private_event(Message(image)))

    assert normalized.attachments[0].file == "base64://file-secret"
    stored_data = normalized.segments[0]["data"]
    assert isinstance(stored_data, dict)
    assert set(stored_data.values()) == {"[inline-image-omitted]"}
