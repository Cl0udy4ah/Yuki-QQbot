from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import pytest

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.plugin_host.agent_backend import PluginAgentToolBackend
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime
from qq_ai_bot.time.models import TimeContext


def _tool(name: str) -> ChatTool:
    return ChatTool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
    )


@dataclass(slots=True)
class FakeToolService:
    executed: list[tuple[str, ToolRuntime]] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)

    def definitions(self, runtime: ToolRuntime) -> tuple[ChatTool, ...]:
        del runtime
        return (
            _tool("get_person_memories"),
            _tool("get_group_memories"),
            _tool("search_chat_history"),
            _tool("call_onebot_api"),
        )

    async def execute(self, name: str, arguments: str, runtime: ToolRuntime) -> str:
        self.arguments.append(arguments)
        self.executed.append((name, runtime))
        return json.dumps({"ok": True})


def _runtime(
    *,
    group_id: str | None = None,
    superuser: bool = False,
    capabilities: frozenset[str] = frozenset({"get_person_memories"}),
) -> AgentRuntime:
    now = datetime.now(UTC)
    return AgentRuntime(
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_user_id="10001",
        actor_is_superuser=superuser,
        delegated_authority=None,
        conversation_key="plugin-agent:test:run",
        current_group_id=group_id,
        bot_user_id="99999",
        gateway=None,
        runtime_config=cast(RuntimeConfigSnapshot, object()),
        current_time=TimeContext(utc=now, local=now, timezone="Asia/Shanghai"),
        allowed_capabilities=capabilities,
        max_tool_calls=5,
        max_model_requests=6,
    )


def _backend(fake: FakeToolService) -> PluginAgentToolBackend:
    return PluginAgentToolBackend(cast(AgentToolService, fake))


def test_definitions_only_expose_host_approved_capabilities() -> None:
    fake = FakeToolService()
    runtime = _runtime(capabilities=frozenset({"get_person_memories", "search_chat_history"}))

    definitions = _backend(fake).definitions(runtime, web_was_used=False)

    assert [item.name for item in definitions] == [
        "get_person_memories",
        "search_chat_history",
    ]


@pytest.mark.asyncio
async def test_non_superuser_person_memory_is_restricted_to_actor() -> None:
    fake = FakeToolService()
    backend = _backend(fake)
    runtime = _runtime()

    denied = await backend.execute(
        "get_person_memories",
        json.dumps({"user_id": "20002"}),
        runtime,
    )
    allowed = await backend.execute(
        "get_person_memories",
        json.dumps({"user_id": "10001"}),
        runtime,
    )

    assert json.loads(denied)["error"] == "scope_denied"
    assert json.loads(allowed)["ok"] is True
    assert len(fake.executed) == 1
    assert fake.executed[0][1].actor_user_id == "10001"
    assert fake.executed[0][1].allow_generic_onebot is False


@pytest.mark.asyncio
async def test_group_history_cannot_escape_current_group() -> None:
    fake = FakeToolService()
    backend = _backend(fake)
    runtime = _runtime(
        group_id="30003",
        capabilities=frozenset({"search_chat_history"}),
    )

    result = await backend.execute(
        "search_chat_history",
        json.dumps({"keyword": "hello", "group_id": "40004"}),
        runtime,
    )

    assert json.loads(result)["error"] == "scope_denied"
    assert fake.executed == []


@pytest.mark.asyncio
async def test_group_history_is_automatically_scoped_when_model_omits_group() -> None:
    fake = FakeToolService()
    backend = _backend(fake)
    runtime = _runtime(
        group_id="30003",
        capabilities=frozenset({"search_chat_history"}),
    )

    result = await backend.execute(
        "search_chat_history",
        json.dumps({"keyword": "hello"}),
        runtime,
    )

    assert json.loads(result)["ok"] is True
    assert json.loads(fake.arguments[0])["group_id"] == "30003"


@pytest.mark.asyncio
async def test_superuser_may_use_approved_cross_person_read() -> None:
    fake = FakeToolService()
    backend = _backend(fake)
    runtime = _runtime(superuser=True)

    result = await backend.execute(
        "get_person_memories",
        json.dumps({"user_id": "20002"}),
        runtime,
    )

    assert json.loads(result)["ok"] is True
    assert len(fake.executed) == 1
