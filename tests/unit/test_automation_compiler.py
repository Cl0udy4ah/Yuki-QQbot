from __future__ import annotations

from datetime import UTC, datetime

from tests.conftest import make_settings

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.compiler import AutomationCompiler, TaskSpec
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    CapabilityArguments,
    build_capability_registry,
)
from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance


def _provenance(*, group_id: str | None = None) -> CreationProvenance:
    return CreationProvenance(
        creator_user_id="10001",
        bot_user_id="7777",
        message_id="task-spec",
        original_text="明天帮我使用麦当劳工具准备早餐",
        current_group_id=group_id,
        mentioned_user_ids=(),
        permission=PermissionLevel.SUPERUSER,
    )


def _registry():
    registry = build_capability_registry()
    registry.register(
        AutomationCapability(
            name="mcp.mcd.create-order",
            description="创建待支付麦当劳订单",
            argument_model=CapabilityArguments,
            output_schema={"type": "object"},
            required_permission=PermissionLevel.SUPERUSER,
            risk_class=RiskClass.MUTATE,
            retry_policy=RetryPolicy.NONE,
            allowed_origins=frozenset({TurnOrigin.SCHEDULED_AUTOMATION}),
        )
    )
    return registry


def test_static_task_compiles_without_model_or_tool_selection() -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:", automation_enabled=True)
    registry = _registry()
    plan = AutomationCompiler(settings=settings, registry=registry).compile(
        TaskSpec.model_validate(
            {
                "name": "喝水提醒",
                "goal": "该喝水了",
                "trigger": {"type": "after", "seconds": 300},
                "strategy": "static",
            }
        ),
        _provenance(),
        default_timezone="Asia/Shanghai",
    )

    assert plan.strategy == "static"
    assert plan.selected_capabilities == ()
    assert plan.script.limits.max_llm_calls == 0
    assert plan.script.steps[0].call == "onebot.send_private_message"


def test_agentic_task_uses_model_safe_id_and_exact_minimum_delegation() -> None:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        automation_enabled=True,
        automation_max_llm_calls_per_run=10,
        automation_max_tool_calls_per_run=16,
        automation_max_runtime_seconds=600,
    )
    registry = _registry()
    safe_id = registry.agent_tool_name("mcp.mcd.create-order")
    compiler = AutomationCompiler(settings=settings, registry=registry)
    plan = compiler.compile(
        TaskSpec.model_validate(
            {
                "name": "早餐订单",
                "goal": "在运行时查询菜单并创建待支付早餐订单",
                "trigger": {
                    "type": "once",
                    "local_datetime": "2026-08-01T09:45:00",
                },
                "strategy": "agentic",
                "capabilities": [safe_id],
            }
        ),
        _provenance(),
        default_timezone="Asia/Shanghai",
    )

    assert plan.selected_capabilities == ("mcp.mcd.create-order",)
    agent = plan.script.steps[0]
    assert agent.call == "yuki.agent"
    assert agent.arguments["max_model_requests"] == 10
    assert agent.arguments["allowed_capabilities"] == ["mcp.mcd.create-order"]
    validated = AutomationValidator(settings=settings, registry=registry).validate(
        plan.script,
        _provenance(),
        now_utc=datetime(2026, 7, 31, tzinfo=UTC),
    )
    assert validated.required_capabilities == (
        "yuki.agent",
        "mcp.mcd.create-order",
        "onebot.send_private_message",
    )


def test_capability_resolution_tolerates_hyphen_underscore_difference() -> None:
    registry = _registry()
    assert (
        registry.resolve_agent_reference("mcp.mcd.create_order")
        == "mcp.mcd.create-order"
    )
