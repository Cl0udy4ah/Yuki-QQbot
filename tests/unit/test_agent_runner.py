"""Agent final-answer recovery and reply-effect visibility tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    InboundMessage,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.emoji.models import EmojiPlacement, EmojiReplyMode, PendingReplyEffect
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.chat import ChatService, _ChatAgentBackend
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.speech.reply_effect import PendingVoiceReplyEffect
from qq_ai_bot.time.models import TimeContext


class EmptyAfterToolProvider(LLMProvider):
    """Return a tool call, an empty final answer, then a usable retry."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="voice-1",
                        function=ToolFunction(name="send_voice", arguments="{}"),
                    ),
                ),
            )
        if len(self.requests) == 2:
            return ChatResponse(content="", latency_seconds=0)
        assert "最终回复正文为空" in (request.messages[-1].content or "")
        return ChatResponse(content="好呀，我用语音和你说。", latency_seconds=0)


@dataclass(slots=True)
class VoiceEffectBackend:
    effects: list[str] = field(default_factory=list)

    def definitions(
        self,
        runtime: AgentRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        return (
            ChatTool(
                name="send_voice",
                description="queue voice",
                parameters={"type": "object", "properties": {}},
            ),
        )

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del calls, runtime

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        del arguments_json, runtime
        self.effects.append(name)
        return json.dumps({"ok": True, "queued": True})

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        del runtime
        return content

    def exhausted(self, runtime: AgentRuntime) -> str:
        del runtime
        return "exhausted"

    def has_visible_effects(self) -> bool:
        # A queued voice needs model text and cannot complete a turn by itself.
        return False


class VisibleEffectBackend(VoiceEffectBackend):
    """Represent a Planner-owned media effect that can stand without text."""

    def definitions(
        self,
        runtime: AgentRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        return ()

    def has_visible_effects(self) -> bool:
        return True


class NativeThenLocalProvider(LLMProvider):
    """Return a native event and a local call in the same model response."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                native_tool_events=(
                    NativeToolEvent(
                        tool_type=NativeToolType.WEB_SEARCH,
                        call_id="native-1",
                        status=NativeToolStatus.COMPLETED,
                        action_type="search",
                    ),
                ),
                tool_calls=(
                    ToolCall(
                        id="local-1",
                        function=ToolFunction(name="send_voice", arguments="{}"),
                    ),
                ),
            )
        return ChatResponse(content="isolated", latency_seconds=0)


class NativeIsolationBackend(VoiceEffectBackend):
    native_web_used = False

    def mark_native_web_used(self) -> None:
        self.native_web_used = True

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        assert self.native_web_used
        return await super().execute(name, arguments_json, runtime)


def _agent_runtime() -> AgentRuntime:
    now = datetime.now(UTC)
    config = cast(
        RuntimeConfigSnapshot,
        SimpleNamespace(
            llm=SimpleNamespace(
                model="test-model",
                temperature=0.1,
                max_output_tokens=256,
                thinking_enabled=False,
            )
        ),
    )
    return AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="private:1001",
        current_group_id=None,
        bot_user_id="8000",
        gateway=None,
        runtime_config=config,
        current_time=TimeContext(utc=now, local=now, timezone="Asia/Shanghai"),
        allowed_capabilities=frozenset({"send_voice"}),
        max_tool_calls=5,
        max_model_requests=6,
    )


@pytest.mark.asyncio
async def test_empty_final_answer_after_voice_tool_is_retried() -> None:
    provider = EmptyAfterToolProvider()
    backend = VoiceEffectBackend()
    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="发条语音吧"),),
        _agent_runtime(),
        backend,
    )

    assert result.text == "好呀，我用语音和你说。"
    assert result.model_requests == 3
    assert backend.effects == ["send_voice"]


@pytest.mark.asyncio
async def test_empty_model_response_is_valid_when_planner_has_visible_media() -> None:
    provider = FakeLLMProvider(lambda _request: "   ")

    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="发个表情"),),
        _agent_runtime(),
        VisibleEffectBackend(),
    )

    assert result.text == ""
    assert result.model_requests == 1
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_native_web_state_is_marked_before_same_response_local_calls() -> None:
    backend = NativeIsolationBackend()
    result = await AgentRunner(
        NativeThenLocalProvider(),
        ConcurrencyManager(1),
    ).run((ChatMessage(role="user", content="search then act"),), _agent_runtime(), backend)

    assert result.text == "isolated"
    assert result.web_was_used
    assert backend.native_web_used


def test_voice_effect_cannot_complete_chat_without_text() -> None:
    inbound = InboundMessage(
        message_id="voice-visibility",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="发条语音吧",
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[PendingVoiceReplyEffect()],
    )
    backend = _ChatAgentBackend(cast(ChatService, object()), runtime)

    assert not backend.has_visible_effects()

    emoji_runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[
            PendingReplyEffect(
                mode=EmojiReplyMode.EMOJI_ONLY,
                placement=EmojiPlacement.ONLY,
                goal="回应用户",
                source="agent",
            )
        ],
    )
    emoji_backend = _ChatAgentBackend(cast(ChatService, object()), emoji_runtime)
    assert emoji_backend.has_visible_effects()

    preferred_emoji_runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[
            PendingReplyEffect(
                mode=EmojiReplyMode.PREFERRED,
                placement=EmojiPlacement.AFTER_TEXT,
                goal="回应用户",
                source="planner",
            )
        ],
    )
    preferred_emoji_backend = _ChatAgentBackend(
        cast(ChatService, object()),
        preferred_emoji_runtime,
    )
    assert preferred_emoji_backend.has_visible_effects()

    optional_emoji_runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        reply_effects=[
            PendingReplyEffect(
                mode=EmojiReplyMode.OPTIONAL,
                placement=EmojiPlacement.AFTER_TEXT,
                goal="回应用户",
                source="agent",
            )
        ],
    )
    optional_emoji_backend = _ChatAgentBackend(
        cast(ChatService, object()),
        optional_emoji_runtime,
    )
    assert not optional_emoji_backend.has_visible_effects()
