"""Reusable bounded Chat Completions tool loop for user and scheduled turns."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from typing import Protocol, cast

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.authority import DelegatedAuthority
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatTool,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolEvent,
    ProviderContinuation,
    ResponseCitation,
    ToolCall,
)
from qq_ai_bot.llm.base import (
    LLMEmptyResponseError,
    LLMIncompleteResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.native_tool_binder import NativeToolBinder
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.web.models import WebMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    delegated_authority: DelegatedAuthority | None
    conversation_key: str
    current_group_id: str | None
    bot_user_id: str
    gateway: object | None
    runtime_config: RuntimeConfigSnapshot
    current_time: TimeContext
    allowed_capabilities: frozenset[str]
    max_tool_calls: int
    max_model_requests: int
    force_tavily_fallback: bool = False


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    text: str
    tool_calls_used: int
    model_requests: int
    web_was_used: bool
    native_tool_events: tuple[NativeToolEvent, ...] = ()
    citations: tuple[ResponseCitation, ...] = ()
    response_status: ModelResponseStatus = ModelResponseStatus.COMPLETED


class AgentToolBackend(Protocol):
    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]: ...

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None: ...

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str: ...

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool: ...

    def finalize(self, content: str, runtime: AgentRuntime) -> str: ...

    def exhausted(self, runtime: AgentRuntime) -> str: ...


class AgentRunner:
    """Execute a provider-neutral bounded tool loop without fabricating inbound events."""

    def __init__(
        self,
        model_executor: ModelExecutor | ModelCompleter,
        concurrency: ConcurrencyManager,
        *,
        task: ModelTask = ModelTask.CHAT_AGENT,
    ) -> None:
        if callable(getattr(model_executor, "execute", None)):
            self._models = cast(ModelExecutor, model_executor)
        else:
            self._models = require_model_executor(
                None,
                provider=cast(ModelCompleter, model_executor),
            )
        self._concurrency = concurrency
        self._task = task
        self._tool_coordinator = ToolInvocationCoordinator()
        self._native_tools = NativeToolBinder()

    async def run(
        self,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> AgentRunResult:
        messages = list(initial_messages)
        calls_used = 0
        web_was_used = False
        empty_retries = 0
        continuation: ProviderContinuation | None = None
        pending_function_outputs: tuple[FunctionCallOutput, ...] = ()
        native_events: list[NativeToolEvent] = []
        citations: list[ResponseCitation] = []
        response_status = ModelResponseStatus.COMPLETED
        incomplete_recovery_used = False
        tavily_fallback = runtime.force_tavily_fallback
        if tavily_fallback and tools is not None:
            enable_fallback = getattr(tools, "enable_native_web_fallback", None)
            if callable(enable_fallback):
                enable_fallback()
        for request_index in range(runtime.max_model_requests):
            definitions = (
                tools.definitions(runtime, web_was_used=web_was_used) if tools is not None else ()
            )
            web_config = getattr(runtime.runtime_config, "web", None)
            try:
                web_mode = WebMode(getattr(web_config, "mode", WebMode.DISABLED.value))
            except ValueError:
                web_mode = WebMode.DISABLED
            native_definitions = self._native_tools.bind(
                protocol=self._models.protocol(self._task),
                capabilities=self._models.capabilities(self._task),
                allowed_capabilities=runtime.allowed_capabilities,
                web_mode=web_mode,
                web_was_used=web_was_used,
            )
            if tavily_fallback:
                native_definitions = ()
            if native_definitions:
                definitions = tuple(
                    item for item in definitions if item.name not in {"web_search", "read_webpage"}
                )
            if incomplete_recovery_used:
                definitions = ()
                native_definitions = ()
            try:
                response = await self._concurrency.run_llm(
                    runtime.conversation_key,
                    partial(
                        self._models.execute,
                        self._task,
                        ChatRequest(
                            messages=tuple(messages),
                            model=runtime.runtime_config.llm.model or "fake",
                            temperature=runtime.runtime_config.llm.temperature,
                            max_output_tokens=runtime.runtime_config.llm.max_output_tokens,
                            thinking_enabled=runtime.runtime_config.llm.thinking_enabled,
                            tools=definitions,
                            tool_choice=("auto" if definitions or native_definitions else None),
                            native_tools=native_definitions,
                            continuation=continuation,
                            function_outputs=pending_function_outputs,
                        ),
                    ),
                )
            except (LLMTimeoutError, LLMUnavailableError) as exc:
                if self._enable_tavily_fallback(
                    tools=tools,
                    web_mode=web_mode,
                    native_was_offered=(
                        bool(native_definitions) and not isinstance(exc, LLMRateLimitError)
                    ),
                    fallback_used=tavily_fallback,
                    has_request_budget=request_index + 1 < runtime.max_model_requests,
                    reason=type(exc).__name__,
                ):
                    tavily_fallback = True
                    continue
                raise
            except LLMEmptyResponseError:
                if self._enable_tavily_fallback(
                    tools=tools,
                    web_mode=web_mode,
                    native_was_offered=bool(native_definitions),
                    fallback_used=tavily_fallback,
                    has_request_budget=request_index + 1 < runtime.max_model_requests,
                    reason="empty_response",
                ):
                    tavily_fallback = True
                    continue
                has_visible_effects = bool(
                    tools is not None
                    and callable(getattr(tools, "has_visible_effects", None))
                    and tools.has_visible_effects()  # type: ignore[attr-defined]
                )
                if has_visible_effects:
                    return AgentRunResult(
                        text="",
                        tool_calls_used=calls_used,
                        model_requests=request_index + 1,
                        web_was_used=web_was_used,
                        native_tool_events=tuple(native_events),
                        citations=tuple(citations),
                        response_status=response_status,
                    )
                if empty_retries >= 2 or request_index + 1 >= runtime.max_model_requests:
                    raise
                empty_retries += 1
                logger.warning(
                    "agent_empty_response_retry retry=%d tool_calls_used=%d",
                    empty_retries,
                    calls_used,
                )
                messages.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "上一次模型请求返回了空内容。请继续当前同一轮任务：如果已有工具"
                            "结果，先核对结果再给出简短、真实的最终答复；如果任务尚未完成，"
                            "继续调用必要工具。不得声称未成功的操作已经完成。"
                        ),
                    )
                )
                continue
            pending_function_outputs = ()
            native_events.extend(response.native_tool_events)
            citations.extend(response.citations)
            response_status = response.status
            if response.native_tool_events:
                web_was_used = True
                mark_native_web = getattr(tools, "mark_native_web_used", None)
                if callable(mark_native_web):
                    mark_native_web()
            if response.continuation is not None:
                continuation = response.continuation
            if response.status is ModelResponseStatus.INCOMPLETE:
                if incomplete_recovery_used or request_index + 1 >= runtime.max_model_requests:
                    raise LLMIncompleteResponseError(
                        "provider response remained incomplete after bounded recovery"
                    )
                incomplete_recovery_used = True
                messages.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "上一响应未完整结束。只根据本轮已有结果给出简短最终答复；"
                            "不要重复任何已经完成的原生搜索或本地工具调用。"
                        ),
                    )
                )
                logger.warning(
                    "agent_incomplete_response_recovery reason=%s",
                    response.incomplete_reason or "unknown",
                )
                continue
            if not response.tool_calls:
                content = response.content
                if tools is not None:
                    content = tools.finalize(content, runtime)
                has_visible_effects = bool(
                    tools is not None
                    and callable(getattr(tools, "has_visible_effects", None))
                    and tools.has_visible_effects()  # type: ignore[attr-defined]
                )
                if not content.strip() and not has_visible_effects:
                    if empty_retries >= 2 or request_index + 1 >= runtime.max_model_requests:
                        raise LLMEmptyResponseError("model returned no final answer")
                    empty_retries += 1
                    logger.warning(
                        "agent_empty_final_retry retry=%d tool_calls_used=%d",
                        empty_retries,
                        calls_used,
                    )
                    messages.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "你已经完成了本轮所需的工具调用，但最终回复正文为空。"
                                "请根据已有工具结果生成实际要发送给用户的简短正文；"
                                "不要重复已经成功的工具调用，也不要只描述发送模式。"
                            ),
                        )
                    )
                    continue
                return AgentRunResult(
                    text=content,
                    tool_calls_used=calls_used,
                    model_requests=request_index + 1,
                    web_was_used=web_was_used,
                    native_tool_events=tuple(native_events),
                    citations=tuple(citations),
                    response_status=response_status,
                )
            responses_path = response.continuation is not None
            if not responses_path:
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=response.tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )
            if tools is not None:
                tools.begin_batch(response.tool_calls, runtime)
            tooling = getattr(runtime.runtime_config, "tooling", None)
            coordinated = await self._tool_coordinator.execute_batch(
                response.tool_calls,
                tools,
                runtime,
                remaining_calls=max(0, runtime.max_tool_calls - calls_used),
                max_parallel_calls=tooling.max_parallel_calls if tooling is not None else 1,
            )
            batch, executed = coordinated.calls, coordinated.executed_count
            calls_used += executed
            for call, result, _was_executed in batch:
                try:
                    outcome = json.loads(result)
                except json.JSONDecodeError:
                    outcome = {}
                logger.info(
                    "agent_tool_complete tool=%s ok=%s error=%s",
                    call.function.name,
                    outcome.get("ok") if isinstance(outcome, dict) else None,
                    (
                        outcome.get("error") or outcome.get("error_code")
                        if isinstance(outcome, dict)
                        else None
                    ),
                )
                if responses_path:
                    pending_function_outputs = (
                        *pending_function_outputs,
                        FunctionCallOutput(call_id=call.id, output=result),
                    )
                else:
                    messages.append(ChatMessage(role="tool", content=result, tool_call_id=call.id))
            if tools is not None:
                effect_probe = getattr(tools, "did_use_web", None)
                if callable(effect_probe) and effect_probe():
                    web_was_used = True
        exhausted = (
            tools.exhausted(runtime) if tools is not None else "工具调用次数过多，Agent 已停止。"
        )
        return AgentRunResult(
            text=exhausted,
            tool_calls_used=calls_used,
            model_requests=runtime.max_model_requests,
            web_was_used=web_was_used,
            native_tool_events=tuple(native_events),
            citations=tuple(citations),
            response_status=response_status,
        )

    @staticmethod
    def _enable_tavily_fallback(
        *,
        tools: AgentToolBackend | None,
        web_mode: WebMode,
        native_was_offered: bool,
        fallback_used: bool,
        has_request_budget: bool,
        reason: str,
    ) -> bool:
        if (
            tools is None
            or web_mode is not WebMode.NATIVE_WITH_TAVILY_FALLBACK
            or not native_was_offered
            or fallback_used
            or not has_request_budget
        ):
            return False
        enable = getattr(tools, "enable_native_web_fallback", None)
        if not callable(enable):
            return False
        enable()
        logger.warning(
            "web_provider_fallback from_provider=deepseek_native to_provider=tavily "
            "reason_category=%s",
            reason,
        )
        return True
