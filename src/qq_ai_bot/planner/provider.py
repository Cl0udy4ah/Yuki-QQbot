"""Planner provider contract and constrained LLM-backed implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable
from typing import Any, Protocol

from pydantic import ValidationError

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannerDecision,
    PlannerInput,
    PlannerReasonCode,
    ToolMode,
    TurnPlan,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.prompt import build_planner_messages
from qq_ai_bot.services.prompt_registry import PromptRegistry, PromptTarget

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class PlannerProviderError(RuntimeError):
    """Sanitized base exception for Planner-only failures."""


class PlannerInterruptedError(PlannerProviderError):
    """The current Planner request was superseded by a newer real message."""


class PlannerTimeoutError(PlannerProviderError):
    """The planning-only request exceeded its independent timeout."""


class PlannerResponseError(PlannerProviderError):
    """The model did not return one valid TurnPlan object."""


class PlannerProvider(Protocol):
    """Provider-neutral Planner interface used by later orchestration."""

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        cancellation: asyncio.Event | None = None,
    ) -> TurnPlan: ...


def extract_json_object(content: str) -> dict[str, Any]:
    """Extract one JSON object from raw text or a fenced JSON block."""

    candidates = [content.strip()]
    candidates.extend(match.strip() for match in _JSON_FENCE.findall(content))
    balanced = _first_balanced_object(content)
    if balanced is not None:
        candidates.append(balanced)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise PlannerResponseError("planner returned no valid JSON object")


def constrain_turn_plan(
    payload: dict[str, Any],
    planner_input: PlannerInput,
    *,
    hard_max_messages: int = 10,
    max_wait_seconds: float = 60,
) -> TurnPlan:
    """Apply monotonic backend clipping before strict Pydantic validation."""

    if not 1 <= hard_max_messages <= 20:
        raise ValueError("hard_max_messages must be between 1 and 20")
    if not 0 <= max_wait_seconds <= 300:
        raise ValueError("max_wait_seconds must be between 0 and 300")
    constrained = dict(payload)
    desired = constrained.get("desired_messages")
    if isinstance(desired, int) and not isinstance(desired, bool):
        constrained["desired_messages"] = max(1, min(hard_max_messages, desired))
    wait = constrained.get("wait_seconds")
    if isinstance(wait, int | float) and not isinstance(wait, bool):
        constrained["wait_seconds"] = max(0.0, min(max_wait_seconds, float(wait)))
    targets = constrained.get("target_user_ids")
    if isinstance(targets, list | tuple):
        known = set(planner_input.known_target_user_ids)
        filtered: list[str] = []
        for target in targets:
            if isinstance(target, str) and target in known and target not in filtered:
                filtered.append(target)
            if len(filtered) >= 5:
                break
        constrained["target_user_ids"] = filtered
    reply_to_message_id = constrained.get("reply_to_message_id")
    if reply_to_message_id is not None and (
        not isinstance(reply_to_message_id, str)
        or not reply_to_message_id.isdigit()
        or reply_to_message_id not in planner_input.known_message_ids
    ):
        constrained["reply_to_message_id"] = None
    plan = TurnPlan.model_validate(constrained)
    updates: dict[str, object] = {}
    if plan.decision is not PlannerDecision.WAIT and plan.wait_seconds:
        updates["wait_seconds"] = 0.0
    if planner_input.visual_input_present and plan.tool_mode is ToolMode.INHERIT:
        updates["tool_mode"] = ToolMode.READ_ONLY
    return plan.model_copy(update=updates) if updates else plan


def parse_turn_plan(
    content: str,
    planner_input: PlannerInput,
    *,
    hard_max_messages: int = 10,
    max_wait_seconds: float = 60,
) -> TurnPlan:
    """Extract, clip, and strictly validate a model response exactly once."""

    try:
        return constrain_turn_plan(
            extract_json_object(content),
            planner_input,
            hard_max_messages=hard_max_messages,
            max_wait_seconds=max_wait_seconds,
        )
    except ValidationError as exc:
        raise PlannerResponseError("planner returned an invalid TurnPlan") from exc


def deterministic_fallback_plan(planner_input: PlannerInput) -> TurnPlan:
    """Return a deterministic fallback without consulting a model.

    Autonomous turns only reach the provider after the necessity gate admits
    them. Replying here keeps a temporary planner-format failure from turning
    an otherwise relevant group turn into permanent silence.
    """

    explicitly_triggered = (
        planner_input.scope_type is ScopeType.PRIVATE
        or planner_input.mentions_bot
        or planner_input.reply_target_is_bot
    )
    should_reply = explicitly_triggered or planner_input.necessity.should_enter_planner
    return TurnPlan(
        decision=PlannerDecision.REPLY if should_reply else PlannerDecision.SILENT,
        intent=(
            "回应当前真实发送者的消息"
            if should_reply
            else "Planner 失败且本轮未通过发言门槛，保持沉默"
        ),
        target_user_ids=(planner_input.current_sender_user_id,) if should_reply else (),
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        tool_mode=(ToolMode.READ_ONLY if planner_input.visual_input_present else ToolMode.INHERIT),
        wait_seconds=0.0,
        confidence=0.0,
        reason_code=PlannerReasonCode.PLANNER_FALLBACK,
        planner_note="deterministic fallback after planner failure",
    )


class LLMPlannerProvider:
    """Request one tool-free TurnPlan from the current main LLM provider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
        hard_max_messages: int | None = None,
        max_wait_seconds: float | None = None,
        fallback_on_error: bool = True,
        observability: PlannerObservability | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if hard_max_messages is not None and not 1 <= hard_max_messages <= 20:
            raise ValueError("hard_max_messages must be between 1 and 20")
        if max_wait_seconds is not None and not 0 <= max_wait_seconds <= 300:
            raise ValueError("max_wait_seconds must be between 0 and 300")
        self._provider = provider
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._hard_max_messages = hard_max_messages
        self._max_wait_seconds = max_wait_seconds
        self._fallback_on_error = fallback_on_error
        self._observability = observability
        self._prompt_registry = prompt_registry

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        cancellation: asyncio.Event | None = None,
    ) -> TurnPlan:
        """Plan once; invalid output is never retried and may safely fall back."""

        started = time.perf_counter()
        token = (
            self._observability.request_started(
                conversation_key=planner_input.conversation_key,
                sender_user_id=planner_input.current_sender_user_id,
                group_id=planner_input.current_group_id,
            )
            if self._observability is not None
            else None
        )
        try:
            self._raise_if_cancelled(cancellation)
            planner_runtime = runtime.planner
            timeout_seconds = planner_runtime.timeout_seconds or self._timeout_seconds or 20.0
            hard_max_messages = (
                runtime.reply.plan_hard_max_messages or self._hard_max_messages or 10
            )
            max_wait_seconds = (
                planner_runtime.max_wait_seconds
                if planner_runtime.max_wait_seconds is not None
                else (self._max_wait_seconds or 60.0)
            )
            planner_messages = list(
                build_planner_messages(
                    planner_input,
                    preferred_messages=planner_runtime.preferred_messages,
                    hard_max_messages=hard_max_messages,
                )
            )
            if self._prompt_registry is not None:
                plugin_messages = self._prompt_registry.render(target=PromptTarget.PLANNER)
                planner_messages[1:1] = [
                    ChatMessage(role="system", content=content) for content in plugin_messages
                ]
            response = await _await_with_cancellation(
                self._provider.complete(
                    ChatRequest(
                        messages=tuple(planner_messages),
                        model=planner_runtime.model or self._model or runtime.llm.model or "fake",
                        temperature=planner_runtime.temperature,
                        max_output_tokens=planner_runtime.max_output_tokens,
                        thinking_enabled=False,
                        tools=(),
                        tool_choice=None,
                    )
                ),
                cancellation=cancellation,
                timeout_seconds=timeout_seconds,
            )
            self._raise_if_cancelled(cancellation)
            if response.tool_calls:
                raise PlannerResponseError("planner attempted a tool call")
            plan = parse_turn_plan(
                response.content,
                planner_input,
                hard_max_messages=hard_max_messages,
                max_wait_seconds=max_wait_seconds,
            )
        except PlannerInterruptedError:
            if self._observability is not None and token is not None:
                self._observability.request_interrupted(
                    token,
                    latency_seconds=time.perf_counter() - started,
                )
            raise
        except asyncio.CancelledError:
            if self._observability is not None and token is not None:
                self._observability.request_interrupted(
                    token,
                    latency_seconds=time.perf_counter() - started,
                )
            raise
        except Exception as exc:
            if not self._fallback_on_error:
                if self._observability is not None and token is not None:
                    self._observability.request_failed(
                        token,
                        latency_seconds=time.perf_counter() - started,
                        error_category=type(exc).__name__,
                    )
                raise
            logger.warning("planner_fallback error_category=%s", type(exc).__name__)
            plan = deterministic_fallback_plan(planner_input)
            if self._observability is not None and token is not None:
                self._observability.request_finished(
                    token,
                    plan=plan,
                    latency_seconds=time.perf_counter() - started,
                    fallback=True,
                )
            return plan
        if self._observability is not None and token is not None:
            self._observability.request_finished(
                token,
                plan=plan,
                latency_seconds=time.perf_counter() - started,
            )
        return plan

    @staticmethod
    def _raise_if_cancelled(cancellation: asyncio.Event | None) -> None:
        if cancellation is not None and cancellation.is_set():
            raise PlannerInterruptedError("planner request superseded")


async def _await_with_cancellation[T](
    awaitable: Awaitable[T],
    *,
    cancellation: asyncio.Event | None,
    timeout_seconds: float,
) -> T:
    operation = asyncio.ensure_future(awaitable)
    cancellation_waiter: asyncio.Task[bool] | None = None
    try:
        waiters: set[asyncio.Future[Any]] = {operation}
        if cancellation is not None:
            if cancellation.is_set():
                raise PlannerInterruptedError("planner request superseded")
            cancellation_waiter = asyncio.create_task(cancellation.wait())
            waiters.add(cancellation_waiter)
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_waiter is not None and cancellation_waiter in done:
            raise PlannerInterruptedError("planner request superseded")
        if operation not in done:
            raise PlannerTimeoutError("planner request timed out")
        return await operation
    finally:
        if cancellation_waiter is not None and not cancellation_waiter.done():
            cancellation_waiter.cancel()
        if not operation.done():
            operation.cancel()
        pending = tuple(
            task
            for task in (operation, cancellation_waiter)
            if task is not None and not task.done()
        )
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _first_balanced_object(content: str) -> str | None:
    start = content.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(content)):
            character = content[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return content[start : index + 1]
        start = content.find("{", start + 1)
    return None
