"""Conversation-safe chat orchestration independent of OneBot."""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage, OutboundMessage
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.persistence.repositories import ConversationRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.renderer import clean_model_output, split_qq_message


class OutboundSender(Protocol):
    """Adapter-provided sender used by the business layer."""

    async def send(self, message: OutboundMessage) -> None:
        """Send one message or raise on failure."""


class ChatService:
    """Persist user input, call the model, send, then persist successful output."""

    def __init__(
        self,
        *,
        settings: Settings,
        conversations: ConversationRepository,
        provider: LLMProvider,
        concurrency: ConcurrencyManager,
    ) -> None:
        self._settings = settings
        self._conversations = conversations
        self._provider = provider
        self._concurrency = concurrency

    async def respond(
        self,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        content: str,
        sender: OutboundSender,
    ) -> int:
        """Run one ordered conversation turn and return sent chunk count."""

        async with self._concurrency.conversation(identity.key):
            await self._conversations.add_message(
                identity,
                role="user",
                content=content,
                platform_message_id=inbound.message_id,
            )
            history = await self._conversations.list_context(
                identity,
                max_messages=self._settings.max_context_messages,
                max_characters=self._settings.max_context_characters,
            )
            messages = history
            if not history or history[0].role != "system":
                messages = (
                    ChatMessage(role="system", content=self._settings.system_prompt),
                    *history,
                )
            request = ChatRequest(
                messages=messages,
                model=self._settings.llm_model or "fake",
                temperature=self._settings.llm_temperature,
                max_output_tokens=self._settings.llm_max_output_tokens,
                thinking_enabled=self._settings.llm_thinking_enabled,
            )
            response = await self._concurrency.run_llm(
                identity.key,
                lambda: self._provider.complete(request),
            )
            rendered = clean_model_output(
                response.content,
                max_characters=self._settings.max_output_characters,
            )
            chunks = split_qq_message(rendered, limit=self._settings.max_qq_message_chars)
            for chunk in chunks:
                await sender.send(OutboundMessage(text=chunk))
            await self._conversations.add_message(identity, role="assistant", content=rendered)
            return len(chunks)
