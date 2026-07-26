"""Whole-script validation, provenance checks, and stable hashing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import AutomationScript, IntervalSchedule
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.automation.templates import TemplateError, referenced_steps, validate_templates
from qq_ai_bot.config import Settings
from qq_ai_bot.time.schedules import initial_run_at
from qq_ai_bot.time.service import validate_timezone

_PLATFORM_ID = re.compile(r"^[1-9][0-9]{4,19}$")
_SENSITIVE_KEYS = frozenset(
    {
        "user_id",
        "group_id",
        "action",
        "key",
        "scope_id",
        "automation_id",
        "target",
    }
)
_LLM_CAPABILITIES = frozenset({"yuki.generate", "yuki.agent"})
_MESSAGE_CAPABILITIES = frozenset({"onebot.send_private_message", "onebot.send_group_message"})


@dataclass(frozen=True, slots=True)
class CreationProvenance:
    creator_user_id: str
    bot_user_id: str
    message_id: str
    original_text: str
    current_group_id: str | None
    mentioned_user_ids: tuple[str, ...]
    permission: PermissionLevel


@dataclass(frozen=True, slots=True)
class ValidatedAutomation:
    script: AutomationScript
    script_hash: str
    required_capabilities: tuple[str, ...]
    next_run_at: datetime


class AutomationValidator:
    """Validate every executable field against immutable backend context."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: AutomationCapabilityRegistry,
    ) -> None:
        self._settings = settings
        self._registry = registry

    def validate(
        self,
        script: AutomationScript,
        provenance: CreationProvenance,
        *,
        now_utc: datetime,
    ) -> ValidatedAutomation:
        validate_timezone(script.timezone)
        self._reject_ambiguous_time(provenance.original_text)
        schedule_timezone = getattr(script.schedule, "timezone", None)
        if schedule_timezone:
            validate_timezone(schedule_timezone)
        if isinstance(script.schedule, IntervalSchedule) and (
            script.schedule.seconds < self._settings.automation_min_interval_seconds
        ):
            raise ValueError(f"interval 最短为 {self._settings.automation_min_interval_seconds} 秒")
        if script.context.scene == "current_group" and provenance.current_group_id is None:
            raise ValueError("当前消息不是群聊，不能声明 current_group 上下文")
        self._validate_limits(script)
        available_steps: set[str] = set()
        required: list[str] = []
        llm_calls = 0
        tool_calls = 0
        messages = 0
        for step in script.steps:
            definition = self._registry.require(step.call)
            if not definition.permits(provenance.permission):
                raise PermissionError(f"当前用户无权委托 capability：{step.call}")
            try:
                validate_templates(step.arguments)
            except TemplateError as exc:
                raise ValueError(str(exc)) from exc
            references = referenced_steps(step.arguments)
            missing = references - available_steps
            if missing:
                raise ValueError(f"步骤 {step.id} 引用了尚未执行的步骤：{sorted(missing)}")
            self._validate_untrusted_flow(step.arguments)
            self._validate_arguments(definition.argument_model, step.arguments)
            if step.call in _LLM_CAPABILITIES and (
                step.arguments.get("context_profile") != script.context.scene
            ):
                raise ValueError("Yuki 步骤的 context_profile 必须与脚本 context.scene 一致")
            self._validate_targets(step.call, step.arguments, provenance)
            available_steps.add(step.id)
            if step.save_as:
                available_steps.add(step.save_as)
            if step.call not in required:
                required.append(step.call)
            if step.call == "yuki.agent":
                for delegated in self._agent_delegation_capabilities(provenance.permission):
                    if delegated not in required:
                        required.append(delegated)
                llm_calls += int(step.arguments.get("max_model_requests", 4))
                tool_calls += 1 + int(step.arguments.get("max_tool_calls", 3))
            else:
                llm_calls += int(step.call in _LLM_CAPABILITIES)
                tool_calls += 1
            messages += int(step.call in _MESSAGE_CAPABILITIES)
        if llm_calls > script.limits.max_llm_calls:
            raise ValueError("脚本中的 LLM 调用数超过 limits.max_llm_calls")
        if messages > script.limits.max_messages:
            raise ValueError("脚本中的消息发送数超过 limits.max_messages")
        if tool_calls > script.limits.max_tool_calls:
            raise ValueError("脚本可能使用的工具次数超过 limits.max_tool_calls")
        next_run = initial_run_at(script.schedule, now_utc, script.timezone)
        return ValidatedAutomation(
            script=script,
            script_hash=canonical_script_hash(script),
            required_capabilities=tuple(required),
            next_run_at=next_run,
        )

    def _validate_limits(self, script: AutomationScript) -> None:
        limits = script.limits
        if len(script.steps) > self._settings.automation_max_steps:
            raise ValueError("脚本步骤数超过后端 AUTOMATION_MAX_STEPS")
        if limits.max_steps > self._settings.automation_max_steps:
            raise ValueError("limits.max_steps 超过后端硬限制")
        if limits.max_llm_calls > self._settings.automation_max_llm_calls_per_run:
            raise ValueError("limits.max_llm_calls 超过后端硬限制")
        if limits.max_tool_calls > self._settings.automation_max_tool_calls_per_run:
            raise ValueError("limits.max_tool_calls 超过后端硬限制")
        if limits.max_messages > self._settings.automation_max_messages_per_run:
            raise ValueError("limits.max_messages 超过后端硬限制")
        if limits.timeout_seconds > self._settings.automation_max_runtime_seconds:
            raise ValueError("limits.timeout_seconds 超过后端硬限制")

    def _agent_delegation_capabilities(self, permission: PermissionLevel) -> tuple[str, ...]:
        excluded = {
            "yuki.generate",
            "yuki.agent",
            "onebot.send_private_message",
            "onebot.send_group_message",
        }
        return tuple(name for name in self._registry.names_for(permission) if name not in excluded)

    @staticmethod
    def _reject_ambiguous_time(original_text: str) -> None:
        compact = "".join(original_text.split()).casefold()
        ambiguous = ("晚点", "过会", "等会", "有空时", "下周")
        if any(token in compact for token in ambiguous):
            raise ValueError("时间表达含糊，请明确日期、星期、时刻或延迟秒数")
        if re.search(
            r"(?<![上下凌晨晚])(?:[一二三四五六七八九十两]|\d{1,2})点", compact
        ) and not any(
            marker in compact for marker in ("上午", "下午", "晚上", "凌晨", "每天", "星期", "周")
        ):
            raise ValueError("请明确是上午、下午、晚上或具体日期的几点")

    @staticmethod
    def _validate_arguments(model: type[Any], arguments: dict[str, Any]) -> None:
        try:
            # Template values are strings and therefore remain schema-valid for all
            # privilege-bearing target fields. Runtime validates the resolved object again.
            model.model_validate(arguments)
        except ValidationError as exc:
            raise ValueError(f"capability 参数不符合 Schema：{exc.errors()[0]['msg']}") from exc

    @classmethod
    def _validate_untrusted_flow(cls, value: Any, *, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                cls._validate_untrusted_flow(child, key=child_key)
            return
        if isinstance(value, list):
            for child in value:
                cls._validate_untrusted_flow(child, key=key)
            return
        if key in _SENSITIVE_KEYS and isinstance(value, str) and "${" in value:
            raise ValueError(f"不可信步骤输出不能进入权限字段 {key}")

    @classmethod
    def _validate_targets(
        cls,
        call: str,
        arguments: dict[str, Any],
        provenance: CreationProvenance,
    ) -> None:
        if call == "onebot.send_private_message":
            cls._validate_user_target(arguments.get("user_id"), provenance)
        elif call == "onebot.send_group_message":
            cls._validate_group_target(arguments.get("group_id"), provenance)
        elif call == "memory.get_person":
            cls._validate_user_target(arguments.get("user_id"), provenance, read_only=True)
        elif call == "memory.get_group":
            cls._validate_group_target(arguments.get("group_id"), provenance, read_only=True)
        elif call == "history.search":
            if arguments.get("user_id"):
                cls._validate_user_target(arguments["user_id"], provenance, read_only=True)
            if arguments.get("group_id"):
                cls._validate_group_target(arguments["group_id"], provenance, read_only=True)
        elif call == "onebot.call_api":
            if provenance.permission is not PermissionLevel.SUPERUSER:
                raise PermissionError(f"{call} 只允许超级管理员委托")
            action = arguments.get("action")
            if not isinstance(action, str) or action not in provenance.original_text:
                raise ValueError("通用 OneBot action 必须明确出现在当前真实消息中")
            cls._validate_onebot_params(arguments.get("params"), provenance)
        elif call == "admin.execute_action":
            if provenance.permission is not PermissionLevel.SUPERUSER:
                raise PermissionError(f"{call} 只允许超级管理员委托")
            if arguments.get("user_id"):
                cls._validate_user_target(arguments["user_id"], provenance)
            if arguments.get("group_id"):
                cls._validate_group_target(arguments["group_id"], provenance)
        elif call in {"config.set", "config.get"}:
            if provenance.permission is not PermissionLevel.SUPERUSER:
                raise PermissionError(f"{call} 只允许超级管理员委托")
            scope_type = arguments.get("scope_type")
            scope_id = arguments.get("scope_id")
            if scope_type == "user":
                cls._validate_user_target(scope_id, provenance)
            elif scope_type == "group":
                cls._validate_group_target(scope_id, provenance)

    @classmethod
    def _validate_onebot_params(cls, value: Any, provenance: CreationProvenance) -> None:
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key == "user_id":
                cls._validate_user_target(child, provenance)
            elif key == "group_id":
                cls._validate_group_target(child, provenance)
            elif isinstance(child, dict):
                cls._validate_onebot_params(child, provenance)

    @classmethod
    def _validate_user_target(
        cls,
        target: Any,
        provenance: CreationProvenance,
        *,
        read_only: bool = False,
    ) -> None:
        if target == "$creator_user_id":
            return
        if not isinstance(target, str) or _PLATFORM_ID.fullmatch(target) is None:
            raise ValueError("目标 QQ 必须是可信内置变量或明确 QQ 号")
        if provenance.permission is PermissionLevel.USER and target != provenance.creator_user_id:
            raise PermissionError("普通用户的自动化只能访问或私聊本人")
        allowed = {provenance.creator_user_id, *provenance.mentioned_user_ids}
        if target not in allowed and not cls._id_in_text(target, provenance.original_text):
            raise ValueError("目标 QQ 必须明确出现在当前真实消息中")

    @classmethod
    def _validate_group_target(
        cls,
        target: Any,
        provenance: CreationProvenance,
        *,
        read_only: bool = False,
    ) -> None:
        if target == "$current_group_id":
            if provenance.current_group_id is None:
                raise ValueError("当前消息不是群聊，不能使用 $current_group_id")
            return
        if not isinstance(target, str) or _PLATFORM_ID.fullmatch(target) is None:
            raise ValueError("目标群必须是可信内置变量或明确群号")
        if provenance.permission is PermissionLevel.USER and target != provenance.current_group_id:
            raise PermissionError("普通用户的自动化只能访问当前真实群")
        if target != provenance.current_group_id and not cls._id_in_text(
            target, provenance.original_text
        ):
            raise ValueError("目标群号必须明确出现在当前真实消息中")

    @staticmethod
    def _id_in_text(target: str, text: str) -> bool:
        return re.search(rf"(?<!\d){re.escape(target)}(?!\d)", text) is not None


def canonical_script_hash(script: AutomationScript) -> str:
    payload = json.dumps(
        script.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
