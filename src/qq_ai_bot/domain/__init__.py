"""Transport-independent domain models."""

from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode, ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    InboundMessage,
    MessageAttachment,
    OutboundMessage,
    SenderIdentity,
)

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ConversationIdentity",
    "ConversationMode",
    "InboundMessage",
    "MessageAttachment",
    "OutboundMessage",
    "ScopeType",
    "SenderIdentity",
]
