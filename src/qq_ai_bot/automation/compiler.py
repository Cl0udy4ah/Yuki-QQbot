"""Compile model-friendly task intents into the strict automation runtime IR."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from qq_ai_bot.automation.models import (
    AutomationContext,
    AutomationLimits,
    AutomationScript,
    AutomationStep,
    Schedule,
    StrictModel,
)
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.automation.validator import CreationProvenance
from qq_ai_bot.config import Settings


class TaskStrategy(StrEnum):
    AUTO = "auto"
    STATIC = "static"
    GENERATED = "generated"
    AGENTIC = "agentic"


class TaskDelivery(StrictModel):
    target: Literal["auto", "self_private", "current_group", "none"] = "auto"
    text: str | None = Field(default=None, min_length=1, max_length=12000)


class TaskSpec(StrictModel):
    """Small provider-neutral intent contract exposed to the conversational Agent."""

    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2500)
    trigger: Schedule
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    strategy: TaskStrategy = TaskStrategy.AUTO
    capabilities: tuple[str, ...] = Field(default=(), max_length=16)
    constraints: tuple[str, ...] = Field(default=(), max_length=12)
    context: AutomationContext = Field(default_factory=AutomationContext)
    delivery: TaskDelivery = Field(default_factory=TaskDelivery)

    @model_validator(mode="after")
    def _strategy_matches_capabilities(self) -> TaskSpec:
        if self.strategy in {TaskStrategy.STATIC, TaskStrategy.GENERATED} and self.capabilities:
            raise ValueError(f"{self.strategy.value} 策略不能声明外部 capability")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities 不能重复")
        return self


class ExecutionPlan(StrictModel):
    """Backend-generated plan; only the contained script is persisted and executed."""

    strategy: Literal["static", "generated", "agentic"]
    selected_capabilities: tuple[str, ...]
    script: AutomationScript
    warnings: tuple[str, ...] = ()


class AutomationCompiler:
    """Deterministically lower a TaskSpec into the already-audited DSL."""

    _BASE_MODEL_REQUESTS = 10

    def __init__(
        self,
        *,
        settings: Settings,
        registry: AutomationCapabilityRegistry,
    ) -> None:
        self._settings = settings
        self._registry = registry

    def compile(
        self,
        task: TaskSpec,
        provenance: CreationProvenance,
        *,
        default_timezone: str,
    ) -> ExecutionPlan:
        selected = self._resolve_capabilities(task.capabilities, provenance)
        strategy = self._resolve_strategy(task, selected)
        timezone = task.timezone or default_timezone
        delivery = self._resolve_delivery(task.delivery, provenance)
        warnings: list[str] = []
        steps: tuple[AutomationStep, ...]

        if strategy == "static":
            if delivery is None:
                raise ValueError("static 任务必须声明消息投递目标")
            steps = (self._delivery_step(delivery, task.delivery.text or task.goal),)
            limits = AutomationLimits(
                max_steps=1,
                max_llm_calls=0,
                max_tool_calls=1,
                max_messages=1,
                timeout_seconds=min(30, self._settings.automation_max_runtime_seconds),
            )
        elif strategy == "generated":
            generate = AutomationStep(
                id="generate",
                call="yuki.generate",
                arguments={
                    "instruction": self._instruction(task),
                    "context_profile": task.context.scene,
                    "max_characters": 4000,
                },
                save_as="result",
            )
            steps = (generate,)
            if delivery is not None:
                steps += (self._delivery_step(delivery, "${result.text}", step_id="deliver"),)
            limits = AutomationLimits(
                max_steps=len(steps),
                max_llm_calls=1,
                max_tool_calls=len(steps),
                max_messages=int(delivery is not None),
                timeout_seconds=min(120, self._settings.automation_max_runtime_seconds),
            )
        else:
            if not selected:
                warnings.append("Agentic 任务没有外部 capability，只会使用模型完成目标")
            delivery_calls = int(delivery is not None)
            tool_budget = max(
                0,
                min(
                    16,
                    self._settings.automation_max_tool_calls_per_run - 1 - delivery_calls,
                ),
            )
            if selected and tool_budget <= 0:
                raise ValueError("后端工具预算不足，无法编译 Agentic 任务")
            model_budget = min(
                self._BASE_MODEL_REQUESTS,
                self._settings.automation_max_llm_calls_per_run,
            )
            if model_budget <= 0:
                raise ValueError("后端模型预算不足，无法编译 Agentic 任务")
            execute = AutomationStep(
                id="execute",
                call="yuki.agent",
                arguments={
                    "instruction": self._instruction(task),
                    "context_profile": task.context.scene,
                    "max_tool_calls": tool_budget,
                    "max_model_requests": model_budget,
                    "allowed_capabilities": list(selected),
                },
                save_as="result",
            )
            steps = (execute,)
            if delivery is not None:
                steps += (self._delivery_step(delivery, "${result.text}", step_id="deliver"),)
            limits = AutomationLimits(
                max_steps=len(steps),
                max_llm_calls=model_budget,
                max_tool_calls=1 + tool_budget + delivery_calls,
                max_messages=delivery_calls,
                timeout_seconds=self._settings.automation_max_runtime_seconds,
            )

        script = AutomationScript(
            version=1,
            name=task.name,
            timezone=timezone,
            schedule=task.trigger,
            context=task.context,
            steps=steps,
            limits=limits,
        )
        return ExecutionPlan(
            strategy=strategy,
            selected_capabilities=selected,
            script=script,
            warnings=tuple(warnings),
        )

    def capability_catalog(self) -> tuple[dict[str, str], ...]:
        """Return model-safe IDs while keeping provider-native names internal."""

        return tuple(
            {
                "id": self._registry.agent_tool_name(item.name),
                "name": item.name,
                "description": item.description,
                "permission": item.required_permission.value,
            }
            for item in self._registry.delegatable()
        )

    def _resolve_capabilities(
        self,
        references: tuple[str, ...],
        provenance: CreationProvenance,
    ) -> tuple[str, ...]:
        result: list[str] = []
        delegatable = {item.name for item in self._registry.delegatable()}
        for reference in references:
            name = self._registry.resolve_agent_reference(reference)
            if name not in delegatable:
                raise ValueError(f"capability 不能委托给自动化 Agent：{name}")
            definition = self._registry.require(name)
            if not definition.permits(provenance.permission):
                raise PermissionError(f"当前用户无权委托 capability：{name}")
            if name not in result:
                result.append(name)
        return tuple(result)

    @staticmethod
    def _resolve_strategy(task: TaskSpec, selected: tuple[str, ...]) -> str:
        if task.strategy is TaskStrategy.AUTO:
            return "agentic" if selected else "static"
        return task.strategy.value

    @staticmethod
    def _resolve_delivery(
        delivery: TaskDelivery,
        provenance: CreationProvenance,
    ) -> Literal["self_private", "current_group"] | None:
        target = delivery.target
        if target == "none":
            return None
        if target == "auto":
            return "current_group" if provenance.current_group_id else "self_private"
        if target == "current_group" and provenance.current_group_id is None:
            raise ValueError("当前消息不是群聊，不能把任务结果投递到 current_group")
        return target

    @staticmethod
    def _delivery_step(
        target: Literal["self_private", "current_group"],
        text: str,
        *,
        step_id: str = "deliver",
    ) -> AutomationStep:
        if target == "current_group":
            return AutomationStep(
                id=step_id,
                call="onebot.send_group_message",
                arguments={"group_id": "$current_group_id", "text": text},
            )
        return AutomationStep(
            id=step_id,
            call="onebot.send_private_message",
            arguments={"user_id": "$creator_user_id", "text": text},
        )

    @staticmethod
    def _instruction(task: TaskSpec) -> str:
        payload = {
            "goal": task.goal,
            "constraints": task.constraints,
            "delivery": task.delivery.target,
            "rules": (
                "只完成本次计划目标，不创建或修改其他自动化。"
                "动态编号和价格必须在任务运行时查询；不要假装外部操作成功。"
                "最终只返回适合直接发给用户的简短结果。"
            ),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:4000]
