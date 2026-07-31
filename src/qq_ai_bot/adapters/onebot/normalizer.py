"""Convert OneBot v11 events into transport-independent domain messages."""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Protocol

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from qq_ai_bot.adapters.onebot.card_parser import parse_card_segment
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


class FaceNameResolver(Protocol):
    """Small adapter boundary used to keep normalization deterministic."""

    def resolve(self, face_id: str | int) -> str:
        """Return a readable name, or an ``ID <value>`` fallback."""


@lru_cache(maxsize=1)
def _default_face_resolver() -> FaceNameResolver:
    from qq_ai_bot.services.qq_face_resolver import QQFaceResolver

    return QQFaceResolver()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _attachment_from_segment(
    segment: MessageSegment,
    *,
    segment_index: int,
    source: str,
    summary: str | None = None,
    url: str | None = None,
) -> MessageAttachment:
    data = segment.data
    return MessageAttachment(
        kind=_ATTACHMENT_TYPES.get(segment.type, AttachmentKind.UNKNOWN),
        label=segment.type,
        segment_index=segment_index,
        source=source,
        file=_optional_string(data.get("file")),
        url=url or _optional_string(data.get("url")),
        summary=summary or _optional_string(data.get("summary")),
        sub_type=_optional_string(data.get("sub_type")),
        file_size=_optional_integer(data.get("file_size")),
        emoji_id=_optional_string(data.get("emoji_id")),
        emoji_package_id=_optional_string(data.get("emoji_package_id")),
        key=_optional_string(data.get("key")),
    )


def _extract_segments(
    segments: Iterable[MessageSegment],
    *,
    self_id: str,
    source: str = "current",
    face_resolver: FaceNameResolver | None = None,
) -> tuple[str, bool, tuple[MessageAttachment, ...], tuple[str, ...]]:
    text_parts: list[str] = []
    mentions_bot = False
    attachments: list[MessageAttachment] = []
    mentioned_user_ids: list[str] = []
    resolver = face_resolver or _default_face_resolver()
    for segment_index, segment in enumerate(segments):
        segment_type = segment.type
        data = segment.data
        if segment_type == "text":
            text_parts.append(str(data.get("text", "")))
        elif segment_type == "at":
            target = str(data.get("qq", ""))
            if target == self_id:
                mentions_bot = True
            elif target == "all":
                text_parts.append("[提及全体成员]")
            elif target.isdecimal():
                if target not in mentioned_user_ids:
                    mentioned_user_ids.append(target)
                index = mentioned_user_ids.index(target) + 1
                text_parts.append(f"[提及成员{index}]")
        elif segment_type == "face":
            face_id = str(data.get("id", "未知"))
            text_parts.append(f"[QQ表情：{resolver.resolve(face_id)}]")
        elif segment_type == "reply":
            continue
        else:
            card = parse_card_segment(segment_type, data)
            if card is not None:
                text_parts.append(card.text)
            attachments.append(
                _attachment_from_segment(
                    segment,
                    segment_index=segment_index,
                    source=source,
                    summary=card.summary if card is not None else None,
                    url=card.url if card is not None else None,
                )
            )
    return (
        sanitize_input("".join(text_parts)),
        mentions_bot,
        tuple(attachments),
        tuple(mentioned_user_ids),
    )


def _reply_text(
    reply_message: Message | None,
    *,
    self_id: str,
    face_resolver: FaceNameResolver | None = None,
) -> str | None:
    if reply_message is None:
        return None
    text, _, _, _ = _extract_segments(
        reply_message,
        self_id=self_id,
        source="reply",
        face_resolver=face_resolver,
    )
    return text or None


def _json_value(value: Any) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return str(value)


def _serialize_segments(message: Message) -> tuple[dict[str, object], ...]:
    """Preserve safe media/message metadata without downloading payloads."""

    serialized: list[dict[str, object]] = []
    for segment in message:
        data = dict(segment.data)
        if segment.type == "image":
            for key in ("file", "url", "base64"):
                value = data.get(key)
                if key == "base64" and value is not None:
                    data[key] = "[inline-image-omitted]"
                elif isinstance(value, str) and value.lstrip().casefold().startswith(
                    ("base64://", "data:image/")
                ):
                    data[key] = "[inline-image-omitted]"
        serialized.append(
            {
                "type": segment.type,
                "data": _json_value(data),
            }
        )
    return tuple(serialized)


def normalize_event(
    event: MessageEvent,
    *,
    ignored_bot_users: frozenset[str] = frozenset(),
    face_resolver: FaceNameResolver | None = None,
) -> InboundMessage:
    """Normalize a private or group OneBot message without downloading attachments."""

    self_id = str(event.self_id)
    text, _, attachments, mentioned_user_ids = _extract_segments(
        event.message,
        self_id=self_id,
        face_resolver=face_resolver,
    )
    _, mentions_bot, _, original_mentioned_user_ids = _extract_segments(
        event.original_message,
        self_id=self_id,
        face_resolver=face_resolver,
    )
    if not mentioned_user_ids:
        mentioned_user_ids = original_mentioned_user_ids
    sender_user_id = str(event.sender.user_id or event.user_id)
    reply_message = event.reply.message if event.reply is not None else None
    reply_attachments: tuple[MessageAttachment, ...] = ()
    if reply_message is not None:
        _, _, reply_attachments, _ = _extract_segments(
            reply_message,
            self_id=self_id,
            source="reply",
            face_resolver=face_resolver,
        )

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
        bot_user_id=self_id,
        raw_text=event.raw_message,
        group_id=group_id,
        mentions_bot=mentions_bot,
        is_self_message=sender_user_id == self_id,
        reply_text=_reply_text(
            reply_message,
            self_id=self_id,
            face_resolver=face_resolver,
        ),
        mentioned_user_ids=mentioned_user_ids,
        attachments=attachments,
        segments=_serialize_segments(event.original_message),
        reply_attachments=reply_attachments,
        reply_segments=_serialize_segments(reply_message) if reply_message is not None else (),
        reply_to_message_id=(
            str(event.reply.message_id)
            if event.reply is not None and event.reply.message_id is not None
            else None
        ),
        reply_sender_user_id=(
            str(event.reply.sender.user_id)
            if event.reply is not None and event.reply.sender.user_id is not None
            else None
        ),
    )
