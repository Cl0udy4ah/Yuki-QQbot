"""Dynamic Tool Kernel request gateway tests."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
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
from qq_ai_bot.services.chat import ChatService, _ChatAgentBackend


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


def test_tool_exposure_log_records_scopes_and_final_tool_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qq_ai_bot.services.chat")
    backend = _ChatAgentBackend(_Service(_registry([])), _runtime())  # type: ignore[arg-type]

    backend.definitions(SimpleNamespace(), web_was_used=False)

    assert "agent_tools_exposed" in caplog.text
    assert "planner_scope_source=explicit" in caplog.text
    assert "planner_scopes=plugin" in caplog.text
    assert "effective_scopes=plugin" in caplog.text
    assert "tools=album_share,request_tools" in caplog.text
    assert "exposed_count=2" in caplog.text
    assert "private:10001" not in caplog.text


def test_tool_exposure_log_distinguishes_inherited_scopes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="qq_ai_bot.services.chat")
    runtime = replace(
        _runtime(),
        tool_groups=frozenset(),
        planner_scopes_explicit=False,
    )
    backend = _ChatAgentBackend(_Service(_registry([])), runtime)  # type: ignore[arg-type]

    backend.definitions(SimpleNamespace(), web_was_used=False)

    assert "planner_scope_source=inherited" in caplog.text
    assert "planner_scopes=backend_authorized" in caplog.text
    assert "effective_scopes=backend_authorized" in caplog.text


def test_person_memory_lookup_survives_flash_reranker_omission() -> None:
    calls: list[str] = []

    async def execute(name: str, _arguments: str, _runtime: object) -> object:
        calls.append(name)
        return {"ok": True}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _runtime: (
                _tool("get_group_memories", "查询群整体记忆"),
                _tool("get_person_memories", "查询某个群友在本群的记忆"),
            ),
            execute=execute,
        )
    )
    catalog = registry.catalog(object())
    group_only = [catalog.by_model_name("get_group_memories")]
    selected = [item for item in group_only if item is not None]
    runtime = replace(
        _runtime(),
        tool_groups=frozenset({"memory"}),
        selection_query="查一下917568554的群记忆",
        planner_intent="查询群友信息",
    )

    retained = ChatService._retain_turn_required_tools(selected, catalog.entries, runtime)

    assert {item.descriptor.model_name for item in retained} == {
        "get_group_memories",
        "get_person_memories",
    }


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
    assert second == {"album_share", "song_share", REQUEST_TOOLS_NAME}

    song_call = ToolCall(id="song", function=ToolFunction(name="song_share", arguments="{}"))
    backend.begin_batch((song_call,), agent_runtime)
    outcome = json.loads(
        await backend.execute("song_share", "{}", agent_runtime)  # type: ignore[arg-type]
    )
    assert outcome["ok"] is True
    assert calls == ["song_share"]


@pytest.mark.asyncio
async def test_agent_can_request_authorized_tool_outside_planner_priority_scopes() -> None:
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

    assert result["ok"] is True
    assert result["data"]["loaded_tools"][0]["name"] == "web_search"
    assert calls == []
