"""Natural-language Agent tools for ordinary users and superusers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from qq_ai_bot.automation.models import AutomationRecord, AutomationScript
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.time.formatting import local_iso


def _object_schema(
    properties: Mapping[str, object], *, required: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class AutomationToolService:
    """Expose owner-scoped task management bound to the current real event."""

    _NAMES = frozenset(
        {
            "automation_create",
            "automation_list",
            "automation_list_history",
            "automation_get",
            "automation_update",
            "automation_pause",
            "automation_resume",
            "automation_cancel",
            "automation_run_now",
            "automation_history",
            "time_get_current",
            "time_get_timezone",
            "time_set_timezone",
        }
    )
    _ALLOWED_ARGUMENTS: ClassVar[dict[str, frozenset[str]]] = {
        "automation_create": frozenset({"script", "max_runs"}),
        "automation_list": frozenset(),
        "automation_list_history": frozenset({"limit"}),
        "automation_get": frozenset({"automation_id"}),
        "automation_update": frozenset({"automation_id", "script"}),
        "automation_pause": frozenset({"automation_id"}),
        "automation_resume": frozenset({"automation_id"}),
        "automation_cancel": frozenset({"automation_id"}),
        "automation_run_now": frozenset({"automation_id"}),
        "automation_history": frozenset({"automation_id"}),
        "time_get_current": frozenset(),
        "time_get_timezone": frozenset(),
        "time_set_timezone": frozenset({"timezone"}),
    }

    def __init__(self, service: AutomationService) -> None:
        self._service = service

    def definitions(self) -> tuple[ChatTool, ...]:
        script_schema = AutomationScript.model_json_schema()
        script_schema["description"] = (
            "持久化任务 DSL。普通定时提醒不要调用通用 OneBot action：私聊使用 "
            "onebot.send_private_message，群聊使用 onebot.send_group_message。"
        )
        script_schema["examples"] = [
            {
                "version": 1,
                "name": "五分钟后喝水提醒",
                "timezone": "Asia/Shanghai",
                "schedule": {"type": "after", "seconds": 300},
                "context": {"scene": "none"},
                "steps": [
                    {
                        "id": "send_reminder",
                        "call": "onebot.send_private_message",
                        "arguments": {
                            "user_id": "$creator_user_id",
                            "text": "该喝水啦～",
                        },
                    }
                ],
                "limits": {
                    "max_steps": 1,
                    "max_llm_calls": 0,
                    "max_tool_calls": 1,
                    "max_messages": 1,
                    "timeout_seconds": 30,
                },
            }
        ]
        id_schema = {"automation_id": {"type": "integer", "minimum": 1}}
        definitions = (
            ChatTool(
                name="automation_create",
                description=(
                    "创建真实持久化自动化任务。普通用户也可调用，但后端只授予其本人私聊、"
                    "当前群、生成和只读能力；超级管理员可额外委托管理员与通用 OneBot 能力。"
                    "一次性延迟提醒使用 schedule={type:'after',seconds:N}。私聊提醒步骤必须使用 "
                    "call='onebot.send_private_message'、user_id='$creator_user_id'；"
                    "当前群提醒使用 "
                    "call='onebot.send_group_message'、group_id='$current_group_id'。这两个是自动化"
                    "运行时已有的主动消息网关，不需要也不应改用聊天工具 call_onebot_api 或"
                    "自动化 capability onebot.call_api。时间含糊时先追问，只有工具返回 ok 才能"
                    "声称创建成功；失败时根据 detail 修正脚本后可重试。"
                ),
                parameters=_object_schema(
                    {"script": script_schema, "max_runs": {"type": "integer", "minimum": 1}},
                    required=("script",),
                ),
            ),
            ChatTool(
                name="automation_list",
                description=(
                    "只列出当前真实发送者仍在运行或暂停的任务。返回的 number 是每次从 1 "
                    "重新排列的用户可见编号；automation_id 仅供后续工具调用，回复用户时不要"
                    "显示为编号。已结束任务请使用 automation_list_history。"
                ),
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="automation_list_history",
                description=(
                    "单独列出当前真实发送者已完成、取消、失败或阻塞的任务历史，不占用当前任务编号。"
                ),
                parameters=_object_schema(
                    {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}
                ),
            ),
            ChatTool(
                name="automation_get",
                description="查看当前真实发送者自己的一个自动化任务。",
                parameters=_object_schema(id_schema, required=("automation_id",)),
            ),
            ChatTool(
                name="automation_update",
                description="用完整的新 DSL 创建任务新版本；只能修改当前发送者自己的任务。",
                parameters=_object_schema(
                    {**id_schema, "script": script_schema},
                    required=("automation_id", "script"),
                ),
            ),
            *(
                ChatTool(
                    name=f"automation_{operation}",
                    description=f"{description}当前真实发送者自己的任务。",
                    parameters=_object_schema(id_schema, required=("automation_id",)),
                )
                for operation, description in (
                    ("pause", "暂停"),
                    ("resume", "恢复"),
                    ("cancel", "取消"),
                    ("run_now", "立即调度执行一次"),
                    ("history", "查看执行历史"),
                )
            ),
            ChatTool(
                name="time_get_current",
                description="读取后端可信的当前 UTC、本地时间、日期、星期和时区。",
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="time_get_timezone",
                description="读取当前真实发送者保存的 IANA 时区。",
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="time_set_timezone",
                description="设置当前真实发送者自己的 IANA 时区。",
                parameters=_object_schema(
                    {"timezone": {"type": "string", "maxLength": 64}},
                    required=("timezone",),
                ),
            ),
        )
        if self._service.enabled:
            return definitions
        return tuple(tool for tool in definitions if tool.name.startswith("time_"))

    def owns(self, name: str) -> bool:
        return name in self._NAMES

    async def execute(self, name: str, arguments_json: str, runtime: ToolRuntime) -> str:
        if not self._valid_runtime(runtime):
            return _result(
                error="permission_context_mismatch", detail="自动化工具未绑定当前真实消息"
            )
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            return _result(error="invalid_arguments", detail=str(exc))
        allowed = self._ALLOWED_ARGUMENTS.get(name)
        if allowed is None:
            return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
        unexpected = set(arguments) - allowed
        if unexpected:
            return _result(
                error="invalid_arguments",
                detail=f"不接受参数：{', '.join(sorted(unexpected))}",
            )
        inbound = runtime.inbound
        try:
            if name == "automation_create":
                row = await self._service.create(
                    arguments.get("script"),
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                    max_runs=arguments.get("max_runs"),
                )
                return _result(data=_record(row))
            if name == "automation_list":
                automations = await self._service.list_current(inbound.sender.user_id)
                return _result(
                    data={
                        "timezone": await self._service.timezone(inbound.sender.user_id),
                        "current_tasks": [
                            _record(row, number=index)
                            for index, row in enumerate(automations, start=1)
                        ],
                        "numbering": "number 每次只按当前任务从 1 重新排列",
                    }
                )
            if name == "automation_list_history":
                maximum = arguments.get("limit", 50)
                if isinstance(maximum, bool) or not isinstance(maximum, int):
                    raise ValueError("limit 必须是整数")
                automations = await self._service.list_completed(inbound.sender.user_id)
                return _result(
                    data={
                        "timezone": await self._service.timezone(inbound.sender.user_id),
                        "completed_history": [
                            _record(row, history_number=index)
                            for index, row in enumerate(automations[:maximum], start=1)
                        ],
                    }
                )
            if name == "time_get_current":
                return _result(data=await self._service.current_time(inbound.sender.user_id))
            if name == "time_get_timezone":
                return _result(
                    data={"timezone": await self._service.timezone(inbound.sender.user_id)}
                )
            if name == "time_set_timezone":
                return _result(
                    data={
                        "timezone": await self._service.set_timezone(
                            inbound.sender.user_id, str(arguments.get("timezone") or "")
                        )
                    }
                )
            automation_id = _automation_id(arguments)
            if name == "automation_get":
                return _result(
                    data=_record(
                        await self._service.require_owned(automation_id, inbound.sender.user_id)
                    )
                )
            if name == "automation_update":
                row = await self._service.update(
                    automation_id,
                    arguments.get("script"),
                    inbound=inbound,
                    conversation_key=runtime.conversation_key,
                )
                return _result(data=_record(row))
            if name == "automation_pause":
                changed = await self._service.pause(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_resume":
                changed = await self._service.resume(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_cancel":
                changed = await self._service.cancel(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_run_now":
                changed = await self._service.run_now(
                    automation_id, inbound=inbound, conversation_key=runtime.conversation_key
                )
            elif name == "automation_history":
                task = await self._service.require_owned(automation_id, inbound.sender.user_id)
                history_rows = await self._service.history(
                    automation_id, creator_user_id=inbound.sender.user_id
                )
                return _result(
                    data={
                        "runs": [
                            {
                                "id": row.id,
                                "status": row.status.value,
                                "scheduled_for_local": local_iso(row.scheduled_for, task.timezone),
                                "finished_at_local": local_iso(row.finished_at, task.timezone),
                                "timezone": task.timezone,
                                "error_category": row.error_category,
                            }
                            for row in history_rows
                        ]
                    }
                )
            else:
                return _result(error="unknown_tool", detail=f"未知自动化工具：{name}")
            return _result(data={"automation_id": automation_id, "changed": changed})
        except (PermissionError, ValueError) as exc:
            return _result(error=type(exc).__name__, detail=str(exc))

    @staticmethod
    def _valid_runtime(runtime: ToolRuntime) -> bool:
        inbound = runtime.inbound
        return bool(
            runtime.allow_automation
            and runtime.actor_user_id == inbound.sender.user_id
            and runtime.trigger_message_id == inbound.message_id
            and runtime.current_group_id == inbound.group_id
        )


def _automation_id(arguments: dict[str, Any]) -> int:
    value = arguments.get("automation_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("automation_id 必须是正整数")
    return value


def _record(
    row: AutomationRecord,
    *,
    number: int | None = None,
    history_number: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "automation_id": row.id,
        "name": row.name,
        "status": row.status.value,
        "timezone": row.timezone,
        "schedule": row.script.schedule.model_dump(mode="json"),
        "next_run_at_local": local_iso(row.next_run_at, row.timezone),
        "required_capabilities": row.required_capabilities,
        "run_count": row.run_count,
    }
    if number is not None:
        payload["number"] = number
    if history_number is not None:
        payload["history_number"] = history_number
    return payload


def _result(*, data: object = None, error: str | None = None, detail: str = "") -> str:
    payload: dict[str, object] = {"ok": error is None}
    if error is None:
        payload["data"] = data
    else:
        payload.update({"error": error, "detail": detail})
    return json.dumps(payload, ensure_ascii=False, default=str)
