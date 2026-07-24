"""Bounded model tools over NapCat and local person-centric memory."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatTool, InboundMessage
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    MemoryRepository,
)


class OneBotToolGateway(Protocol):
    """The subset of the event-bound adapter required by Agent tools."""

    async def call_api(self, action: str, params: dict[str, Any]) -> Any:
        """Call a OneBot action over the already-connected adapter."""


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """Authorization and scene data that cannot be supplied by the model."""

    inbound: InboundMessage
    gateway: OneBotToolGateway | None
    allow_generic_onebot: bool


def _object_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class AgentToolService:
    """Define and execute tools without granting authority through prompt text."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedgerRepository,
        memories: MemoryRepository,
        actions: AgentActionRepository,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._memories = memories
        self._actions = actions

    def definitions(self, runtime: ToolRuntime) -> tuple[ChatTool, ...]:
        tools = [
            ChatTool(
                name="get_recent_chat_history",
                description=(
                    "直接从 NapCat 读取当前私聊或当前群最近 20 条消息。"
                    "当用户问刚才说了什么、当前对话历史或人物上下文时使用。"
                ),
                parameters=_object_schema({}),
            ),
            ChatTool(
                name="search_chat_history",
                description="搜索永久 QQ 聊天账本，可按 QQ、群号和时间约束。",
                parameters=_object_schema(
                    {
                        "keyword": {"type": "string"},
                        "user_id": {"type": "string"},
                        "group_id": {"type": "string"},
                        "after": {
                            "type": "string",
                            "description": "ISO 8601 时间，可省略",
                        },
                        "before": {
                            "type": "string",
                            "description": "ISO 8601 时间，可省略",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    required=("keyword",),
                ),
            ),
            ChatTool(
                name="get_person_memories",
                description="读取指定 QQ 人物的跨私聊和跨群结构记忆。",
                parameters=_object_schema({"user_id": {"type": "string"}}, required=("user_id",)),
            ),
            ChatTool(
                name="get_group_memories",
                description="读取指定群号的共同结构记忆。",
                parameters=_object_schema({"group_id": {"type": "string"}}, required=("group_id",)),
            ),
        ]
        if runtime.allow_generic_onebot:
            tools.append(
                ChatTool(
                    name="call_onebot_api",
                    description=(
                        "以当前超级管理员身份调用任意 NapCat/OneBot action。"
                        "action 和 params 原样传递，不要编造执行结果。"
                    ),
                    parameters=_object_schema(
                        {
                            "action": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        required=("action", "params"),
                    ),
                )
            )
        return tuple(tools)

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
    ) -> str:
        """Execute one tool and return JSON, including safe model-readable errors."""

        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            return self._result(error="invalid_json", detail="工具参数不是有效 JSON")
        if not isinstance(arguments, dict):
            return self._result(error="invalid_arguments", detail="工具参数必须是对象")
        try:
            if name == "get_recent_chat_history":
                return await self._recent_history(runtime)
            if name == "search_chat_history":
                return await self._search(arguments, runtime)
            if name == "get_person_memories":
                return await self._person_memories(arguments)
            if name == "get_group_memories":
                return await self._group_memories(arguments)
            if name == "call_onebot_api":
                return await self._call_onebot(arguments, runtime)
            return self._result(error="unknown_tool", detail=f"未知工具：{name}")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return self._result(error=type(exc).__name__, detail="工具执行失败")

    async def _recent_history(self, runtime: ToolRuntime) -> str:
        if runtime.gateway is None:
            return self._result(error="onebot_unavailable", detail="当前没有 OneBot 连接")
        inbound = runtime.inbound
        limit = self._settings.recent_history_tool_limit
        if inbound.scope_type is ScopeType.GROUP:
            if inbound.group_id is None:
                return self._result(error="missing_group", detail="当前群号缺失")
            action = "get_group_msg_history"
            params: dict[str, Any] = {"group_id": inbound.group_id, "count": limit}
        else:
            action = "get_friend_msg_history"
            params = {"user_id": inbound.sender.user_id, "count": limit}
        payload = await runtime.gateway.call_api(action, params)
        messages = self._history_messages(payload)[-limit:]
        stored = 0
        for item in messages:
            if await self._store_history_item(item, inbound):
                stored += 1
        return self._result(
            data={
                "source": "NapCat",
                "scope": inbound.scope_type.value,
                "count": len(messages),
                "newly_recorded": stored,
                "messages": messages,
            }
        )

    @staticmethod
    def _history_messages(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("messages", "message_list", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("messages")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
        return []

    async def _store_history_item(self, item: dict[str, Any], inbound: InboundMessage) -> bool:
        message_id = str(item.get("message_id") or item.get("id") or "")
        sender_id = str(
            item.get("user_id")
            or (
                item.get("sender", {}).get("user_id")
                if isinstance(item.get("sender"), dict)
                else ""
            )
            or ""
        )
        if not message_id or not sender_id:
            return False
        raw_segments = item.get("message")
        segments = self._segments(raw_segments)
        content = self._segments_text(segments)
        timestamp_value = item.get("time")
        try:
            if not isinstance(timestamp_value, str | int | float):
                raise TypeError
            occurred_at = datetime.fromtimestamp(float(timestamp_value), tz=UTC)
        except (TypeError, ValueError, OSError):
            occurred_at = datetime.now(UTC)
        _, created = await self._ledger.append(
            bot_user_id=inbound.bot_user_id or "unknown-bot",
            platform_message_id=message_id,
            scope_type=inbound.scope_type,
            sender_user_id=sender_id,
            direction=("outbound" if sender_id == inbound.bot_user_id else "inbound"),
            content=content,
            segments=segments,
            group_id=inbound.group_id,
            private_peer_user_id=(
                inbound.sender.user_id if inbound.scope_type is ScopeType.PRIVATE else None
            ),
            reply_to_message_id=self._reply_id(segments),
            occurred_at=occurred_at,
            sender_is_bot=sender_id == inbound.bot_user_id,
        )
        return created

    @staticmethod
    def _segments(raw: Any) -> tuple[dict[str, Any], ...]:
        if isinstance(raw, str):
            return ({"type": "text", "data": {"text": raw}},)
        if not isinstance(raw, list):
            return ()
        return tuple(item for item in raw if isinstance(item, dict))

    @staticmethod
    def _segments_text(segments: tuple[dict[str, Any], ...]) -> str:
        parts: list[str] = []
        for segment in segments:
            kind = str(segment.get("type", "unknown"))
            data = segment.get("data")
            data = data if isinstance(data, dict) else {}
            if kind == "text":
                parts.append(str(data.get("text", "")))
            elif kind == "at":
                parts.append(f"[@{data.get('qq', '')}]")
            elif kind == "face":
                parts.append(f"[QQ表情:{data.get('id', '')}]")
            else:
                parts.append(f"[{kind}]")
        return "".join(parts).strip()

    @staticmethod
    def _reply_id(segments: tuple[dict[str, Any], ...]) -> str | None:
        for segment in segments:
            if segment.get("type") != "reply":
                continue
            data = segment.get("data")
            if isinstance(data, dict) and data.get("id") is not None:
                return str(data["id"])
        return None

    async def _search(self, arguments: dict[str, Any], runtime: ToolRuntime) -> str:
        keyword = arguments.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            return self._result(error="invalid_keyword", detail="keyword 必须是非空字符串")
        after = self._parse_time(arguments.get("after"))
        before = self._parse_time(arguments.get("before"))
        user_id = self._optional_string(arguments.get("user_id"))
        group_id = self._optional_string(arguments.get("group_id"))
        if (
            len(keyword.strip()) < 3
            and not user_id
            and not group_id
            and after is None
            and before is None
        ):
            if runtime.inbound.group_id:
                group_id = runtime.inbound.group_id
            else:
                user_id = runtime.inbound.sender.user_id
        rows = await self._ledger.search(
            keyword=keyword,
            user_id=user_id,
            group_id=group_id,
            after=after,
            before=before,
            limit=self._bounded_int(arguments.get("limit"), default=20, maximum=100),
        )
        return self._result(data={"events": [self._event_json(row) for row in rows]})

    async def _person_memories(self, arguments: dict[str, Any]) -> str:
        user_id = arguments.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return self._result(error="invalid_user_id", detail="user_id 必须是字符串")
        rows = await self._memories.list_person(
            user_id, limit=self._settings.person_memory_max_entries
        )
        return self._result(
            data={
                "user_id": user_id,
                "memories": [
                    {
                        "id": row.id,
                        "category": row.category,
                        "content": row.content,
                        "importance": row.importance,
                        "source_type": row.source_type,
                    }
                    for row in rows
                ],
            }
        )

    async def _group_memories(self, arguments: dict[str, Any]) -> str:
        group_id = arguments.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            return self._result(error="invalid_group_id", detail="group_id 必须是字符串")
        rows = await self._memories.list_group(
            group_id, limit=self._settings.group_memory_max_entries
        )
        return self._result(
            data={
                "group_id": group_id,
                "memories": [
                    {
                        "id": row.id,
                        "category": row.category,
                        "content": row.content,
                        "subject_user_id": row.subject_user_id,
                    }
                    for row in rows
                ],
            }
        )

    async def _call_onebot(self, arguments: dict[str, Any], runtime: ToolRuntime) -> str:
        if not runtime.allow_generic_onebot:
            return self._result(error="permission_denied", detail="当前轮次不是超级管理员直发")
        if runtime.gateway is None:
            return self._result(error="onebot_unavailable", detail="当前没有 OneBot 连接")
        action = arguments.get("action")
        params = arguments.get("params")
        if not isinstance(action, str) or not action.strip() or not isinstance(params, dict):
            return self._result(
                error="invalid_arguments", detail="action 必须是字符串且 params 必须是对象"
            )
        started = time.perf_counter()
        try:
            result = await runtime.gateway.call_api(action, params)
        except (OSError, RuntimeError) as exc:
            await self._actions.record(
                actor_user_id=runtime.inbound.sender.user_id,
                action=action,
                success=False,
                duration_seconds=time.perf_counter() - started,
                error_category=type(exc).__name__,
            )
            raise
        await self._actions.record(
            actor_user_id=runtime.inbound.sender.user_id,
            action=action,
            success=True,
            duration_seconds=time.perf_counter() - started,
        )
        await self._record_onebot_send(action, params, result, runtime.inbound)
        return self._result(data={"action": action, "result": result})

    async def _record_onebot_send(
        self,
        action: str,
        params: dict[str, Any],
        result: Any,
        inbound: InboundMessage,
    ) -> None:
        if action not in {
            "send_private_msg",
            "send_group_msg",
            "send_msg",
            "send_private_forward_msg",
            "send_group_forward_msg",
            "send_forward_msg",
        }:
            return
        raw_message = params.get("message", params.get("messages", ""))
        segments = self._segments(raw_message)
        if isinstance(raw_message, str):
            content = raw_message
        else:
            content = self._segments_text(segments)
        group_id = self._optional_string(params.get("group_id"))
        user_id = self._optional_string(params.get("user_id"))
        if group_id:
            scope = ScopeType.GROUP
            peer = None
        elif user_id:
            scope = ScopeType.PRIVATE
            peer = user_id
        else:
            return
        message_id: str | None = None
        if isinstance(result, str | int):
            message_id = str(result)
        elif isinstance(result, dict):
            raw_id = result.get("message_id") or result.get("id")
            if raw_id is not None:
                message_id = str(raw_id)
        await self._ledger.append(
            bot_user_id=inbound.bot_user_id or "unknown-bot",
            platform_message_id=message_id or f"agent-out-{uuid.uuid4()}",
            scope_type=scope,
            group_id=group_id,
            private_peer_user_id=peer,
            sender_user_id=inbound.bot_user_id or "unknown-bot",
            direction="outbound",
            content=content,
            segments=segments,
            sender_is_bot=True,
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError("time must be an ISO string")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    @staticmethod
    def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("limit must be an integer")
        return max(1, min(int(value), maximum))

    @staticmethod
    def _event_json(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "sender_user_id": row.sender_user_id,
            "scope": row.scope_type.value,
            "group_id": row.group_id,
            "direction": row.direction,
            "content": row.content,
            "occurred_at": row.occurred_at.isoformat(),
        }

    def _result(self, *, data: Any = None, error: str | None = None, detail: str = "") -> str:
        payload = (
            {"ok": False, "error": error, "detail": detail} if error else {"ok": True, "data": data}
        )
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        limit = self._settings.agent_tool_result_max_characters
        if len(rendered) > limit:
            rendered = rendered[:limit] + "\n[工具结果已截断]"
        return rendered
