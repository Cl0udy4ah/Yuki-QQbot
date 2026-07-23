"""OneBot event normalization and unsupported-content tests."""

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Reply, Sender

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import AttachmentKind


def private_event(
    message: Message, *, message_id: int = 1, user_id: int = 1001
) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=9999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=user_id, nickname="tester"),
    )


def group_event(message: Message, *, message_id: int = 2) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=9999,
        post_type="message",
        sub_type="normal",
        user_id=1001,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=1001, nickname="tester", card="card"),
        group_id=2001,
    )


def test_private_text_and_group_mention_normalize() -> None:
    private = normalize_event(private_event(Message("hello")))
    group = normalize_event(
        group_event(Message([MessageSegment.at(9999), MessageSegment.text(" question")]))
    )
    assert private.scope_type is ScopeType.PRIVATE and private.text == "hello"
    assert private.sender.nickname == "tester" and not private.sender.group_card
    assert group.scope_type is ScopeType.GROUP and group.mentions_bot
    assert group.text == "question" and group.group_id == "2001"
    assert group.sender.nickname == "tester" and group.sender.group_card == "card"


def test_group_mention_uses_original_message_after_nonebot_strips_at() -> None:
    original = Message([MessageSegment.at(9999), MessageSegment.text(" question")])
    event = group_event(Message("question"))
    event.original_message = original
    event.to_me = True

    normalized = normalize_event(event)

    assert normalized.mentions_bot
    assert normalized.text == "question"


def test_reply_text_and_face_placeholder_are_supported() -> None:
    message = Message([MessageSegment.at(9999), MessageSegment.face(14), MessageSegment.text("ok")])
    event = group_event(message)
    event.reply = Reply(
        time=1,
        message_type="group",
        message_id=8,
        real_id=8,
        sender=Sender(user_id=1002),
        message=Message("quoted"),
    )
    normalized = normalize_event(event)
    assert normalized.reply_text == "quoted"
    assert "[QQ表情:14]" in normalized.text


def test_unsupported_attachment_is_metadata_only() -> None:
    normalized = normalize_event(
        private_event(Message(MessageSegment.image("https://invalid.test/a")))
    )
    assert not normalized.text
    assert normalized.attachments[0].kind is AttachmentKind.IMAGE
    assert normalized.attachments[0].label == "image"
