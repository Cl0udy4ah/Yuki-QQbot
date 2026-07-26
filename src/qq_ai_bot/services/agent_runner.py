"""Reusable bounded Chat Completions tool loop for user and scheduled turns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.authority import DelegatedAuthority
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatTool, ToolCall
from qq_ai_bot.llm.base import LLMEmptyResponseError, LLMProvider
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.time.models import TimeContext


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

    def __init__(self, provider: LLMProvider, concurrency: ConcurrencyManager) -> None:
        self._provider = provider
        self._concurrency = concurrency

    async def run(
        self,
        initial_messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: AgentToolBackend | None,
    ) -> AgentRunResult:
        messages = list(initial_messages)
        calls_used = 0
        web_was_used = False
        for request_index in range(runtime.max_model_requests):
            definitions = (
                tools.definitions(runtime, web_was_used=web_was_used) if tools is not None else ()
            )
            response = await self._concurrency.run_llm(
                runtime.conversation_key,
                partial(
                    self._provider.complete,
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
            if not response.tool_calls:
                content = response.content
                if tools is not None:
                    content = tools.finalize(content, runtime)
                if not content.strip():
                    raise LLMEmptyResponseError("model returned no final answer")
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
