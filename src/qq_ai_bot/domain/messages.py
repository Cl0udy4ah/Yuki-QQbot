"""Message models shared by adapters and business services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType


class AttachmentKind(StrEnum):
    """Attachment kinds intentionally not downloaded by the MVP."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    FORWARD = "forward"
    CARD = "card"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MessageAttachment:
    """Safe metadata for unsupported content; never contains downloaded data."""

    kind: AttachmentKind
    label: str


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    """Platform-neutral sender identity."""

    user_id: str
    display_name: str = ""
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Normalized inbound message consumed by policies and chat services."""

    message_id: str
    event_type: str
    scope_type: ScopeType
    sender: SenderIdentity
    text: str
    raw_text: str = ""
    group_id: str | None = None
    mentions_bot: bool = False
    is_self_message: bool = False
    reply_text: str | None = None
    attachments: tuple[MessageAttachment, ...] = ()
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def conversation(self, *, shared_group: bool = False) -> ConversationIdentity:
        """Build the conversation identity for this message."""

        if self.scope_type is ScopeType.PRIVATE:
            return ConversationIdentity.private(self.sender.user_id)
        if self.group_id is None:
            raise ValueError("group message is missing group_id")
        from qq_ai_bot.domain.conversations import ConversationMode

        mode = ConversationMode.SHARED if shared_group else ConversationMode.PER_USER
        return ConversationIdentity.group(self.group_id, self.sender.user_id, mode)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Transport-independent outbound text."""

    text: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single role/content pair sent to a chat completion API."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Provider-independent chat request."""

    messages: tuple[ChatMessage, ...]
    model: str
    temperature: float
    max_output_tokens: int
    thinking_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Validated provider response."""

    content: str
    latency_seconds: float
    provider_request_id: str | None = None
