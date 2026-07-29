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
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatTool, ToolCall
from qq_ai_bot.llm.base import LLMEmptyResponseError
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.time.models import TimeContext

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


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    text: str
    tool_calls_used: int
    model_requests: int
    web_was_used: bool


class AgentToolBackend(Protocol):
    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]: ...

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None: ...

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str: ...

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
        for request_index in range(runtime.max_model_requests):
            definitions = (
                tools.definitions(runtime, web_was_used=web_was_used) if tools is not None else ()
            )
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
                            tool_choice="auto" if definitions else None,
                        ),
                    ),
                )
            except LLMEmptyResponseError:
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
                )
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
            for call in response.tool_calls:
                if calls_used >= runtime.max_tool_calls:
                    result = json.dumps(
                        {"ok": False, "error": "tool_limit_exceeded"},
                        ensure_ascii=False,
                    )
                elif tools is None:
                    result = json.dumps(
                        {"ok": False, "error": "tools_unavailable"},
                        ensure_ascii=False,
                    )
                else:
                    result = await tools.execute(
                        call.function.name,
                        call.function.arguments,
                        runtime,
                    )
                    calls_used += 1
                    if call.function.name in {
                        "web.search",
                        "web.read_page",
                        "web_search",
                        "read_webpage",
                        "web__search",
                        "web__read_page",
                    }:
                        web_was_used = True
                    try:
                        outcome = json.loads(result)
                    except json.JSONDecodeError:
                        outcome = {}
                    logger.info(
                        "agent_tool_complete tool=%s ok=%s error=%s",
                        call.function.name,
                        outcome.get("ok") if isinstance(outcome, dict) else None,
                        outcome.get("error") if isinstance(outcome, dict) else None,
                    )
                messages.append(ChatMessage(role="tool", content=result, tool_call_id=call.id))
        exhausted = (
            tools.exhausted(runtime) if tools is not None else "工具调用次数过多，Agent 已停止。"
        )
        return AgentRunResult(
            text=exhausted,
            tool_calls_used=calls_used,
            model_requests=runtime.max_model_requests,
            web_was_used=web_was_used,
        )
