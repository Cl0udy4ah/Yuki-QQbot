"""Reusable OpenAI-compatible Chat Completions client."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMProvider,
    LLMTimeoutError,
    LLMUnavailableError,
    RetryableProviderError,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """Non-streaming provider with bounded retries for transient failures only."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._owns_client = client is None
        timeout = httpx.Timeout(
            connect=min(timeout_seconds, 10.0),
            read=timeout_seconds,
            write=timeout_seconds,
            pool=min(timeout_seconds, 10.0),
        )
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if not self._api_key or not request.model:
            raise LLMConfigurationError("LLM is not configured")

        started = time.perf_counter()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries + 1),
                wait=wait_random_exponential(multiplier=0.25, max=2),
                retry=retry_if_exception_type(
                    (httpx.ConnectError, httpx.TimeoutException, RetryableProviderError)
                ),
                reraise=True,
            ):
                with attempt:
                    response = await self._post(request)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM request timed out") from exc
        except (httpx.ConnectError, RetryableProviderError) as exc:
            raise LLMUnavailableError("LLM is temporarily unavailable") from exc

        latency = time.perf_counter() - started
        logger.info("llm_request_complete latency_seconds=%.3f success=true", latency)
        content, request_id = self._parse_response(response)
        return ChatResponse(
            content=content,
            latency_seconds=latency,
            provider_request_id=request_id,
        )

    async def _post(self, request: ChatRequest) -> httpx.Response:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.thinking_enabled is not None:
            payload["thinking"] = {"type": "enabled" if request.thinking_enabled else "disabled"}
        response = await self._client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        if response.status_code >= 500:
            raise RetryableProviderError("provider returned a server error")
        if response.status_code >= 400:
            raise LLMError(f"provider rejected request with HTTP {response.status_code}")
        return response

    @staticmethod
    def _parse_response(response: httpx.Response) -> tuple[str, str | None]:
        try:
            payload: dict[str, Any] = response.json()
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise LLMEmptyResponseError("provider returned no choices")
            first = choices[0]
            if not isinstance(first, dict):
                raise LLMEmptyResponseError("provider returned an invalid choice")
            message = first.get("message")
            if not isinstance(message, dict):
                raise LLMEmptyResponseError("provider returned no message")
            raw_content = message.get("content")
            if not isinstance(raw_content, str) or not raw_content.strip():
                raise LLMEmptyResponseError("provider returned empty content")
            request_id = payload.get("id")
            return raw_content.strip(), request_id if isinstance(request_id, str) else None
        except ValueError as exc:
            raise LLMError("provider returned invalid JSON") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
