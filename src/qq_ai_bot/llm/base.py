"""Provider-neutral LLM interface and sanitized exceptions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse


class LLMError(RuntimeError):
    """Base error safe for categorization but not direct provider details."""


class LLMTimeoutError(LLMError):
    """The provider did not answer before the configured timeout."""


class LLMUnavailableError(LLMError):
    """The provider is temporarily or permanently unavailable."""


class LLMConfigurationError(LLMError):
    """Required provider configuration is missing."""


class LLMEmptyResponseError(LLMError):
    """The provider returned no usable text."""


class RetryableProviderError(LLMUnavailableError):
    """Internal marker for an explicit HTTP 5xx response."""


class LLMProvider(ABC):
    """Abstract chat-completion provider."""

    @abstractmethod
    async def complete(self, request: ChatRequest) -> ChatResponse:
        """Return one complete, non-streaming response."""

    async def close(self) -> None:
        """Release provider resources."""

        return None
