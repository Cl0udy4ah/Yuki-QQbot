"""Execute model requests through explicit tasks and profiles."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.model_runtime.models import ModelCapability, ModelTask, StructuredOutputMode
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.routes import ModelRouter


class ModelCompleter(Protocol):
    """Small compatibility boundary for injected test providers."""

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


class ModelExecutor(Protocol):
    """Business-facing task executor contract."""

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse: ...

    def model_name(self, task: ModelTask) -> str: ...

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode: ...


class LegacyTaskModelExecutor:
    """Adapt an injected test provider without leaking it into business services."""

    def __init__(self, provider: ModelCompleter, *, model: str = "fake") -> None:
        self._provider = provider
        self._model = model

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        del task
        return await self._provider.complete(request)

    def model_name(self, task: ModelTask) -> str:
        del task
        return self._model

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON


def require_model_executor(
    model_executor: ModelExecutor | None,
    *,
    provider: ModelCompleter | None = None,
    model: str = "fake",
) -> ModelExecutor:
    """Normalize old test injection at one migration boundary."""

    if model_executor is not None:
        return model_executor
    if provider is None:
        raise TypeError("model_executor is required")
    return LegacyTaskModelExecutor(provider, model=model)


class TaskModelExecutor:
    """The only main-model entry point used by business services."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        pool: ModelClientPool,
        invocations: ModelInvocationRepository | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive when configured")
        self._router = router
        self._pool = pool
        self._invocations = invocations
        self._semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )

    @property
    def router(self) -> ModelRouter:
        return self._router

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        required: set[ModelCapability] = set()
        if request.tools and not request.structured_output:
            required.add(ModelCapability.TOOLS)
        if request.structured_output or request.response_format is not None:
            required.add(ModelCapability.STRUCTURED_OUTPUT)
        if request.thinking_enabled or request.reasoning_effort is not None:
            required.add(ModelCapability.REASONING)
        _route, profile = self._router.route(task, required_capabilities=frozenset(required))
        provider = self._pool.get(profile)
        thinking_enabled = (
            profile.thinking_enabled
            if request.thinking_enabled is None
            else request.thinking_enabled
        )
        normalized = ChatRequest(
            messages=request.messages,
            model=profile.model,
            temperature=(
                profile.default_temperature if request.temperature is None else request.temperature
            ),
            max_output_tokens=(
                profile.default_max_output_tokens
                if request.max_output_tokens is None
                else request.max_output_tokens
            ),
            thinking_enabled=thinking_enabled,
            reasoning_effort=(
                (request.reasoning_effort or profile.reasoning_effort) if thinking_enabled else None
            ),
            tools=request.tools,
            tool_choice=request.tool_choice,
            response_format=request.response_format,
            structured_output=request.structured_output,
        )
        started = time.perf_counter()
        try:
            if self._semaphore is None:
                response = await provider.complete(normalized)
            else:
                async with self._semaphore:
                    response = await provider.complete(normalized)
        except Exception as exc:
            if self._invocations is not None:
                await self._invocations.record(
                    task=task,
                    profile_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    success=False,
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    cached_prompt_tokens=None,
                    latency_seconds=time.perf_counter() - started,
                    error_category=type(exc).__name__,
                )
            raise
        if self._invocations is not None:
            await self._invocations.record(
                task=task,
                profile_id=profile.id,
                provider=profile.provider,
                model=profile.model,
                success=True,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                cached_prompt_tokens=response.cached_prompt_tokens,
                latency_seconds=response.latency_seconds,
                error_category=None,
            )
        return response

    def profile_id(self, task: ModelTask) -> str:
        route, _profile = self._router.route(task)
        return route.profile_id

    def model_name(self, task: ModelTask) -> str:
        _route, profile = self._router.route(task)
        return profile.model

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        _route, profile = self._router.route(task)
        return profile.structured_output_mode

    async def close(self) -> None:
        await self._pool.close()
