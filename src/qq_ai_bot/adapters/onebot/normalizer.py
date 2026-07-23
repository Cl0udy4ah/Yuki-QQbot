"""Convert OneBot v11 events into transport-independent domain messages."""

from __future__ import annotations

from collections.abc import Iterable

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.services.renderer import sanitize_input

_ATTACHMENT_TYPES: dict[str, AttachmentKind] = {
    "image": AttachmentKind.IMAGE,
    "record": AttachmentKind.AUDIO,
    "video": AttachmentKind.VIDEO,
    "file": AttachmentKind.FILE,
    "forward": AttachmentKind.FORWARD,
    "node": AttachmentKind.FORWARD,
    "xml": AttachmentKind.CARD,
    "json": AttachmentKind.CARD,
}


def _extract_segments(
    segments: Iterable[MessageSegment],
    *,
    self_id: str,
) -> tuple[str, bool, tuple[MessageAttachment, ...], tuple[str, ...]]:
    text_parts: list[str] = []
    mentions_bot = False
    attachments: list[MessageAttachment] = []
    mentioned_user_ids: list[str] = []
    for segment in segments:
        segment_type = segment.type
        data = segment.data
        if segment_type == "text":
            text_parts.append(str(data.get("text", "")))
        elif segment_type == "at":
            target = str(data.get("qq", ""))
            if target == self_id:
                mentions_bot = True
            elif target:
                if target not in mentioned_user_ids:
                    mentioned_user_ids.append(target)
                index = mentioned_user_ids.index(target) + 1
                text_parts.append(f"[提及成员{index}]")
        elif segment_type == "face":
            face_id = str(data.get("id", "未知"))
            text_parts.append(f"[QQ表情:{face_id}]")
        elif segment_type == "reply":
            continue
        else:
            kind = _ATTACHMENT_TYPES.get(segment_type, AttachmentKind.UNKNOWN)
            attachments.append(MessageAttachment(kind=kind, label=segment_type))
    return (
        sanitize_input("".join(text_parts)),
        mentions_bot,
        tuple(attachments),
        tuple(mentioned_user_ids),
    )


def _reply_text(reply_message: Message | None, *, self_id: str) -> str | None:
    if reply_message is None:
        return None
    text, _, _, _ = _extract_segments(reply_message, self_id=self_id)
    return text or None


def normalize_event(
    event: MessageEvent,
    *,
    ignored_bot_users: frozenset[str] = frozenset(),
) -> InboundMessage:
    """Normalize a private or group OneBot message without downloading attachments."""

    self_id = str(event.self_id)
    text, _, attachments, mentioned_user_ids = _extract_segments(
        event.message,
        self_id=self_id,
    )
    _, mentions_bot, _, original_mentioned_user_ids = _extract_segments(
        event.original_message,
        self_id=self_id,
    )
    if not mentioned_user_ids:
        mentioned_user_ids = original_mentioned_user_ids
    sender_user_id = str(event.sender.user_id or event.user_id)
    reply_message = event.reply.message if event.reply is not None else None

    if isinstance(event, GroupMessageEvent):
        scope = ScopeType.GROUP
        group_id: str | None = str(event.group_id)
    elif isinstance(event, PrivateMessageEvent):
        scope = ScopeType.PRIVATE
        group_id = None
    else:
        raise TypeError(f"unsupported OneBot event: {type(event).__name__}")

    return InboundMessage(
        message_id=str(event.message_id),
        event_type=f"message:{event.message_type}:{event.sub_type}",
        scope_type=scope,
        sender=SenderIdentity(
            user_id=sender_user_id,
            nickname=event.sender.nickname or "",
            group_card=event.sender.card or "",
            is_bot=sender_user_id in ignored_bot_users,
        ),
        text=text,
        raw_text=event.raw_message,
        group_id=group_id,
        mentions_bot=mentions_bot,
        is_self_message=sender_user_id == self_id,
        reply_text=_reply_text(reply_message, self_id=self_id),
        mentioned_user_ids=mentioned_user_ids,
        attachments=attachments,
    )
