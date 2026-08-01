"""Dynamic Tool Kernel request gateway tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolCandidateSelector,
    ToolKernelMetrics,
    ToolProviderRegistry,
)
from qq_ai_bot.capabilities.request import REQUEST_TOOLS_NAME, match_requestable_tools
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatTool,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.planner.models import ToolMode
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.chat import _ChatAgentBackend


def _tool(name: str, description: str) -> ChatTool:
    return ChatTool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )


def _registry(calls: list[str]) -> ToolProviderRegistry:
    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        calls.append(name)
        return {"ok": True, "data": {"called": name}}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="plugin",
            source=CapabilityTrustSource.PLUGIN,
            definitions=lambda _runtime: (
                _tool("album_share", "搜索并发送网易云专辑卡片"),
                _tool("song_share", "搜索并发送网易云单曲；也可从刚才专辑抽一首"),
            ),
            execute=execute,
        )
    )
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (_tool("web_search", "联网搜索公开网页"),),
            execute=execute,
        )
    )
    return registry


class _Service:
    def __init__(self, registry: ToolProviderRegistry) -> None:
        self.registry = registry
        self._tool_selector = ToolCandidateSelector()
        self._tool_metrics = ToolKernelMetrics()
        self._tool_invocations = None
        self._tool_artifacts = None

    def _build_tool_registry(
        self,
        _runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> ToolProviderRegistry:
        del web_was_used
        return self.registry

    @staticmethod
    def _decode_tool_result(value: str) -> dict[str, object]:
        decoded = json.loads(value)
        assert isinstance(decoded, dict)
        return decoded


def _runtime() -> ToolRuntime:
    inbound = InboundMessage(
        message_id="m1",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="10001"),
        text="抽第一首",
        bot_user_id="99999",
    )
    return ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:10001",
        trigger_message_id="m1",
        actor_user_id="10001",
        runtime_config=SimpleNamespace(
            tooling=None,
            mcp=None,
            agent=SimpleNamespace(tool_result_max_characters=32_000),
            web=SimpleNamespace(max_calls_per_turn=3),
        ),
        origin=TurnOrigin.USER_MESSAGE,
        tool_mode=ToolMode.INHERIT,
        tool_groups=frozenset({"plugin"}),
        planner_scopes_explicit=True,
        selected_tool_names=frozenset({"album_share"}),
    )


def test_request_matcher_prefers_song_capability_and_has_no_arbitrary_fallback() -> None:
    catalog = _registry([]).catalog(object())

    matches = match_requestable_tools(catalog, query="搜索并发送网易云单曲", limit=2)

    assert matches[0].entry.descriptor.model_name == "song_share"
    assert match_requestable_tools(catalog, query="完全无关的量子天气", limit=2) == ()


@pytest.mark.asyncio
async def test_agent_can_request_and_then_call_an_omitted_authorized_tool() -> None:
    calls: list[str] = []
    backend = _ChatAgentBackend(_Service(_registry(calls)), _runtime())  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()

    first = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert first == {"album_share", REQUEST_TOOLS_NAME}

    hidden_call = ToolCall(
        id="hidden",
        function=ToolFunction(name="song_share", arguments="{}"),
    )
    backend.begin_batch((hidden_call,), agent_runtime)
    hidden = json.loads(
        await backend.execute("song_share", "{}", agent_runtime)  # type: ignore[arg-type]
    )
    assert hidden["error"] == "capability_not_loaded"
    assert calls == []

    request_arguments = json.dumps(
        {"query": "搜索并发送网易云单曲", "max_results": 1},
        ensure_ascii=False,
    )
    request_call = ToolCall(
        id="request",
        function=ToolFunction(name=REQUEST_TOOLS_NAME, arguments=request_arguments),
    )
    backend.begin_batch((request_call,), agent_runtime)
    requested = json.loads(
        await backend.execute(
            REQUEST_TOOLS_NAME,
            request_arguments,
            agent_runtime,  # type: ignore[arg-type]
        )
    )
    assert requested["ok"] is True
    assert requested["data"]["loaded_tools"][0]["name"] == "song_share"

    second = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert second == {"album_share", "song_share"}

    song_call = ToolCall(id="song", function=ToolFunction(name="song_share", arguments="{}"))
    backend.begin_batch((song_call,), agent_runtime)
    outcome = json.loads(
        await backend.execute("song_share", "{}", agent_runtime)  # type: ignore[arg-type]
    )
    assert outcome["ok"] is True
    assert calls == ["song_share"]


@pytest.mark.asyncio
async def test_agent_cannot_request_a_tool_outside_planner_approved_scopes() -> None:
    calls: list[str] = []
    backend = _ChatAgentBackend(_Service(_registry(calls)), _runtime())  # type: ignore[arg-type]
    agent_runtime = SimpleNamespace()

    exposed = {tool.name for tool in backend.definitions(agent_runtime, web_was_used=False)}
    assert exposed == {"album_share", REQUEST_TOOLS_NAME}

    arguments = json.dumps(
        {"query": "web_search", "max_results": 1},
        ensure_ascii=False,
    )
    request_call = ToolCall(
        id="request-web",
        function=ToolFunction(name=REQUEST_TOOLS_NAME, arguments=arguments),
    )
    backend.begin_batch((request_call,), agent_runtime)
    result = json.loads(
        await backend.execute(REQUEST_TOOLS_NAME, arguments, agent_runtime)  # type: ignore[arg-type]
    )

    assert result == {
        "ok": False,
        "error": "capability_not_found",
        "detail": "当前真实用户和场景允许的工具目录中没有匹配能力",
    }
    assert calls == []
