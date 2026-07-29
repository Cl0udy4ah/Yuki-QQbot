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
    """Transient event media reference; payload fields are never persisted verbatim."""

    kind: AttachmentKind
    label: str
    segment_index: int = 0
    source: str = "current"
    file: str | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    summary: str | None = field(default=None, repr=False)
    sub_type: str | None = None
    file_size: int | None = None
    emoji_id: str | None = None
    emoji_package_id: str | None = None
    key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    """Platform-neutral sender identity."""

    user_id: str
    nickname: str = ""
    group_card: str = ""
    is_bot: bool = False

    @property
    def display_name(self) -> str:
        """Return the event-provided display name without database fallback."""

        return self.group_card or self.nickname


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Normalized inbound message consumed by policies and chat services."""

    message_id: str
    event_type: str
    scope_type: ScopeType
    sender: SenderIdentity
    text: str
    bot_user_id: str = ""
    raw_text: str = ""
    group_id: str | None = None
    mentions_bot: bool = False
    is_self_message: bool = False
    reply_text: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    attachments: tuple[MessageAttachment, ...] = ()
    segments: tuple[dict[str, object], ...] = ()
    reply_attachments: tuple[MessageAttachment, ...] = ()
    reply_segments: tuple[dict[str, object], ...] = ()
    reply_to_message_id: str | None = None
    reply_sender_user_id: str | None = None
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
class OutboundMedia:
    """Ephemeral outbound media bytes with ledger-safe descriptive metadata."""

    kind: AttachmentKind
    content: bytes = field(default=b"", repr=False)
    mime_type: str = "application/octet-stream"
    summary: str = ""
    emoji_id: str | None = None
    animated: bool = False
    local_path: str | None = field(default=None, repr=False)
    spoken_text: str = field(default="", repr=False)
    generation_id: int | None = None
    voice_profile_id: str | None = None
    voice_reference_key: str | None = None
    voice_language: str | None = None
    duration_milliseconds: int | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Transport-independent text and optional ephemeral media."""

    text: str = ""
    reply_to_message_id: str | None = None
    media: tuple[OutboundMedia, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A message sent to a chat completion API, including tool-call turns."""

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFunction:
    """A provider-neutral function tool call."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider-neutral tool call."""

    id: str
    function: ToolFunction
    type: str = "function"


@dataclass(frozen=True, slots=True)
class ChatTool:
    """A JSON-schema function tool exposed to the model."""

    name: str
    description: str
    parameters: dict[str, object]


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Provider-independent chat request."""

    messages: tuple[ChatMessage, ...]
    model: str = ""
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_enabled: bool | None = None
    tools: tuple[ChatTool, ...] = ()
    tool_choice: str | None = None
    response_format: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Validated provider response."""

    content: str
    latency_seconds: float
    provider_request_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
