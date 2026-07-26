"""Explicit capability registry for the automation DSL."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.automation.authority import AuthorityContext, PermissionLevel
from qq_ai_bot.automation.models import AutomationContext, RetryPolicy, RiskClass, TurnOrigin


class CapabilityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerateArguments(CapabilityArguments):
    instruction: str = Field(min_length=1, max_length=4000)
    context_profile: Literal["none", "creator_private", "current_group"] = "none"
    max_characters: int = Field(default=200, ge=1, le=4000)


class AgentArguments(CapabilityArguments):
    instruction: str = Field(min_length=1, max_length=4000)
    context_profile: Literal["none", "creator_private", "current_group"] = "none"
    max_tool_calls: int = Field(default=3, ge=0, le=8)
    max_model_requests: int = Field(default=4, ge=1, le=6)


class SendPrivateArguments(CapabilityArguments):
    user_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=12000)


class SendGroupArguments(CapabilityArguments):
    group_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=12000)


class OneBotCallArguments(CapabilityArguments):
    action: str = Field(min_length=1, max_length=128)
    params: dict[str, Any]


class AdminActionArguments(CapabilityArguments):
    action: str = Field(min_length=1, max_length=128)
    target: str | None = Field(default=None, max_length=32)
    user_id: str | None = Field(default=None, max_length=64)
    group_id: str | None = Field(default=None, max_length=64)
    value: Any = None
    delta: int | None = None
    memory_id: int | None = None
    content: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=128)


class ConfigGetArguments(CapabilityArguments):
    key: str = Field(min_length=1, max_length=128)
    scope_type: Literal["global", "group", "user"] = "global"
    scope_id: str = Field(default="", max_length=64)


class ConfigSetArguments(ConfigGetArguments):
    value: Any


class WebSearchArguments(CapabilityArguments):
    query: str = Field(min_length=1, max_length=400)
    topic: Literal["general", "news"] = "general"
    time_range: Literal["day", "week", "month", "year"] | None = None


class WebReadArguments(CapabilityArguments):
    url: str = Field(min_length=1, max_length=2048)
    question: str = Field(default="", max_length=1000)


class PersonMemoryArguments(CapabilityArguments):
    user_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


class GroupMemoryArguments(CapabilityArguments):
    group_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


class HistorySearchArguments(CapabilityArguments):
    keyword: str = Field(min_length=1, max_length=400)
    user_id: str | None = Field(default=None, max_length=64)
    group_id: str | None = Field(default=None, max_length=64)
    after: str | None = Field(default=None, max_length=64)
    before: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    data: dict[str, Any]
    llm_calls: int = 0
    tool_calls: int = 1
    messages_sent: int = 0


@dataclass(frozen=True, slots=True)
class CapabilityExecutionContext:
    authority: AuthorityContext
    automation_id: int
    automation_run_id: int
    step_id: str
    creator_user_id: str
    bot_user_id: str
    current_group_id: str | None
    scheduled_for: datetime
    actual_started_at: datetime
    local_time: datetime
    timezone: str
    automation_context: AutomationContext
    conversation_key: str
    web_was_used: bool = False


CapabilityHandler = Callable[
    [dict[str, Any], CapabilityExecutionContext], Awaitable[CapabilityResult]
]


@dataclass(frozen=True, slots=True)
class AutomationCapability:
    name: str
    description: str
    argument_model: type[BaseModel]
    output_schema: dict[str, object]
    required_permission: PermissionLevel
    risk_class: RiskClass
    retry_policy: RetryPolicy
    allowed_origins: frozenset[TurnOrigin]
    schema_version: int = 1
    handler: CapabilityHandler | None = field(default=None, repr=False)

    @property
    def input_schema(self) -> dict[str, object]:
        return self.argument_model.model_json_schema()

    def permits(self, permission: PermissionLevel) -> bool:
        return not (
            self.required_permission is PermissionLevel.SUPERUSER
            and permission is not PermissionLevel.SUPERUSER
        )


class AutomationCapabilityRegistry:
    """Reviewed capability allowlist; never reflects arbitrary Python functions."""

    def __init__(self) -> None:
        self._items: dict[str, AutomationCapability] = {}

    def register(self, definition: AutomationCapability) -> None:
        if definition.name in self._items:
            raise ValueError(f"duplicate automation capability: {definition.name}")
        self._items[definition.name] = definition

    def get(self, name: str) -> AutomationCapability | None:
        return self._items.get(name)

    def require(self, name: str) -> AutomationCapability:
        definition = self.get(name)
        if definition is None:
            raise ValueError(f"未登记的自动化 capability：{name}")
        return definition

    def list(self) -> tuple[AutomationCapability, ...]:
        return tuple(self._items[name] for name in sorted(self._items))

    def names_for(self, permission: PermissionLevel) -> tuple[str, ...]:
        return tuple(item.name for item in self.list() if item.permits(permission))


def build_capability_registry(
    handlers: dict[str, CapabilityHandler] | None = None,
) -> AutomationCapabilityRegistry:
    """Build the versioned 1.5 registry with optionally bound handlers."""

    bound = handlers or {}
    scheduled = frozenset({TurnOrigin.SCHEDULED_AUTOMATION, TurnOrigin.SYSTEM_TASK})
    definitions: Iterable[
        tuple[
            str,
            str,
            type[BaseModel],
            PermissionLevel,
            RiskClass,
            RetryPolicy,
        ]
    ] = (
        (
            "yuki.generate",
            "调用主模型生成文字，不开放工具。",
            GenerateArguments,
            PermissionLevel.USER,
            RiskClass.GENERATE,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "yuki.agent",
            "运行受委托能力约束的 Yuki Agent。",
            AgentArguments,
            PermissionLevel.USER,
            RiskClass.GENERATE,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "onebot.send_private_message",
            "主动发送一条普通私聊消息。",
            SendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "onebot.send_group_message",
            "主动发送一条普通群消息。",
            SendGroupArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "onebot.call_api",
            "调用任意公开 NapCat/OneBot action。",
            OneBotCallArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "admin.execute_action",
            "调用已登记的后端管理员业务 action。",
            AdminActionArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "config.get",
            "读取已登记运行时配置。",
            ConfigGetArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.READ,
            RetryPolicy.NONE,
        ),
        (
            "config.set",
            "修改已登记运行时配置。",
            ConfigSetArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "web.search",
            "通过受控 Tavily Provider 搜索公开网页。",
            WebSearchArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "web.read_page",
            "通过受控 Provider 读取一个公开网页。",
            WebReadArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "memory.get_person",
            "读取创建者本人或已授权人物记忆。",
            PersonMemoryArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "memory.get_group",
            "读取当前群或已授权群记忆。",
            GroupMemoryArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "history.search",
            "在明确范围内搜索本地永久聊天账本。",
            HistorySearchArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
    )
    registry = AutomationCapabilityRegistry()
    for name, description, model, permission, risk, retry in definitions:
        registry.register(
            AutomationCapability(
                name=name,
                description=description,
                argument_model=model,
                output_schema={"type": "object"},
                required_permission=permission,
                risk_class=risk,
                retry_policy=retry,
                allowed_origins=scheduled,
                handler=bound.get(name),
            )
        )
    return registry
