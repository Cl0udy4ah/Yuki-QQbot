"""Explicit administrator action registry, target validation, and dispatch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.services.admin import (
    GroupAdminService,
    MemoryAdminService,
    PreferenceAdminService,
    PrivateAccessAdminService,
    RelationshipAdminService,
)
from qq_ai_bot.services.admin.common import require_real_superuser

_NUMERIC_ID = re.compile(r"[1-9][0-9]{4,19}")


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One explicitly registered business operation."""

    name: str
    display_name: str
    description: str
    target_kind: str
    mutating: bool
    self_service: bool = False


class ActionRegistry:
    """Allowlist of natural-language administrator business actions."""

    def __init__(self) -> None:
        specs = (
            ActionSpec(
                "relationship.get",
                "查看关系",
                "读取人物好感度与信任度。",
                "user",
                False,
                self_service=True,
            ),
            ActionSpec(
                "relationship.set_affection",
                "设置好感度",
                "把人物好感度设置为 0～100。",
                "user",
                True,
            ),
            ActionSpec(
                "relationship.adjust_affection",
                "调整好感度",
                "按 -20～20 的明确数值调整人物好感度。",
                "user",
                True,
            ),
            ActionSpec(
                "relationship.set_trust",
                "设置信任度",
                "把人物信任度设置为 0～100。",
                "user",
                True,
            ),
            ActionSpec(
                "relationship.history",
                "查看关系历史",
                "查看人物最近关系变化。",
                "user",
                False,
                self_service=True,
            ),
            ActionSpec(
                "memory.list",
                "查看人物记忆",
                "列出人物显式与自动记忆。",
                "user",
                False,
                self_service=True,
            ),
            ActionSpec(
                "memory.add",
                "添加人物记忆",
                "添加一条显式人物记忆。",
                "user",
                True,
                self_service=True,
            ),
            ActionSpec(
                "memory.update",
                "修改人物记忆",
                "修改指定记忆 ID。",
                "user",
                True,
                self_service=True,
            ),
            ActionSpec(
                "memory.delete",
                "删除人物记忆",
                "删除指定记忆 ID。",
                "user",
                True,
                self_service=True,
            ),
            ActionSpec(
                "preference.list",
                "查看偏好",
                "列出人物交互偏好。",
                "user",
                False,
                self_service=True,
            ),
            ActionSpec(
                "preference.set",
                "设置偏好",
                "设置人物交互偏好键值。",
                "user",
                True,
                self_service=True,
            ),
            ActionSpec(
                "preference.delete",
                "删除偏好",
                "删除人物指定交互偏好。",
                "user",
                True,
                self_service=True,
            ),
            ActionSpec("group.enable", "启用群", "启用当前或明确指定群。", "group", True),
            ActionSpec("group.disable", "停用群", "停用当前或明确指定群。", "group", True),
            ActionSpec(
                "group.autonomous_enable",
                "启用群自主发言",
                "打开指定群的自主参与开关。",
                "group",
                True,
            ),
            ActionSpec(
                "group.autonomous_disable",
                "关闭群自主发言",
                "关闭指定群的自主参与开关。",
                "group",
                True,
            ),
            ActionSpec(
                "private_access.enable",
                "恢复私聊",
                "允许指定 QQ 再次私聊。",
                "user",
                True,
            ),
            ActionSpec(
                "private_access.disable",
                "阻止私聊",
                "阻止指定 QQ 私聊，超级管理员不可被阻止。",
                "user",
                True,
            ),
        )
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name: str) -> ActionSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"未知管理员 action：{name}") from exc

    def list(self) -> tuple[ActionSpec, ...]:
        return tuple(self._specs.values())


class TargetResolver:
    """Resolve only targets proven by the current authoritative event."""

    @staticmethod
    def user(arguments: dict[str, Any], actor: AdminActor) -> str:
        target = arguments.get("target")
        if target == "self":
            return actor.user_id
        if target == "mentioned_user":
            requested = _optional_id(arguments.get("user_id"))
            if requested:
                if requested not in actor.mentioned_user_ids:
                    raise ValueError("该 QQ 没有在当前消息中被真实 @")
                return requested
            if len(actor.mentioned_user_ids) != 1:
                raise ValueError("请明确指定当前消息中唯一一个被 @ 的成员")
            return _required_id(actor.mentioned_user_ids[0], "user_id")
        if target == "explicit_user_id":
            requested = _required_id(arguments.get("user_id"), "user_id")
            TargetResolver._require_in_current_text(requested, actor.current_message_text)
            return requested
        raise ValueError("用户目标必须是 self、mentioned_user 或 explicit_user_id")

    @staticmethod
    def group(arguments: dict[str, Any], actor: AdminActor) -> str:
        target = arguments.get("target")
        if target == "current_group":
            if not actor.current_group_id:
                raise ValueError("当前消息不在群聊中")
            return actor.current_group_id
        if target == "explicit_group_id":
            requested = _required_id(arguments.get("group_id"), "group_id")
            TargetResolver._require_in_current_text(requested, actor.current_message_text)
            return requested
        raise ValueError("群目标必须是 current_group 或 explicit_group_id")

    @staticmethod
    def config_scope(
        scope_type: str,
        scope_id: object,
        actor: AdminActor,
    ) -> tuple[str, str]:
        normalized = scope_type.casefold()
        raw_id = str(scope_id or "").strip()
        if normalized == "global":
            if raw_id:
                raise ValueError("global 作用域的 scope_id 必须为空")
            return normalized, ""
        if normalized == "group":
            if raw_id in {"current", "current_group", "本群", ""}:
                if not actor.current_group_id:
                    raise ValueError("当前消息不在群聊中")
                return normalized, actor.current_group_id
            requested = _required_id(raw_id, "scope_id")
            if requested != actor.current_group_id:
                TargetResolver._require_in_current_text(
                    requested,
                    actor.current_message_text,
                )
            return normalized, requested
        if normalized == "user":
            if raw_id in {"self", "我的", ""}:
                return normalized, actor.user_id
            requested = _required_id(raw_id, "scope_id")
            if requested != actor.user_id and requested not in actor.mentioned_user_ids:
                TargetResolver._require_in_current_text(
                    requested,
                    actor.current_message_text,
                )
            return normalized, requested
        raise ValueError("scope_type 必须是 global、group 或 user")

    @staticmethod
    def _require_in_current_text(target_id: str, text: str) -> None:
        if re.search(rf"(?<!\d){re.escape(target_id)}(?!\d)", text) is None:
            raise ValueError("显式 QQ/群号必须真实出现在当前消息正文中")


def _optional_id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value:
        return None
    if _NUMERIC_ID.fullmatch(value) is None:
        raise ValueError("QQ/群号格式错误")
    return value


def _required_id(value: object, name: str) -> str:
    result = _optional_id(value)
    if result is None:
        raise ValueError(f"缺少 {name}")
    return result


def _required_int(arguments: dict[str, Any], name: str) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    return value


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


class AdminActionService:
    """Dispatch a registered action to the same services used by `/ai` commands."""

    def __init__(
        self,
        *,
        settings: Settings,
        relationships: RelationshipAdminService,
        memories: MemoryAdminService,
        preferences: PreferenceAdminService,
        groups: GroupAdminService,
        private_access: PrivateAccessAdminService,
        registry: ActionRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._relationships = relationships
        self._memories = memories
        self._preferences = preferences
        self._groups = groups
        self._private_access = private_access
        self.registry = registry or ActionRegistry()

    async def execute(
        self,
        action: str,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> dict[str, Any]:
        """Execute one allowlisted action after backend target validation."""

        require_real_superuser(actor, self._settings)
        spec = self.registry.get(action)
        target = (
            TargetResolver.user(arguments, actor)
            if spec.target_kind == "user"
            else TargetResolver.group(arguments, actor)
        )
        if action == "relationship.get":
            row = await self._relationships.get_relationship(actor, target)
            return _relationship_json(row)
        if action == "relationship.set_affection":
            before, after = await self._relationships.set_affection(
                actor,
                target,
                _required_int(arguments, "value"),
            )
            return {
                "target_user_id": target,
                "before": _relationship_json(before),
                "after": _relationship_json(after),
            }
        if action == "relationship.adjust_affection":
            before, after = await self._relationships.adjust_affection(
                actor,
                target,
                _required_int(arguments, "delta"),
            )
            return {
                "target_user_id": target,
                "before": _relationship_json(before),
                "after": _relationship_json(after),
            }
        if action == "relationship.set_trust":
            before, after = await self._relationships.set_trust(
                actor,
                target,
                _required_int(arguments, "value"),
            )
            return {
                "target_user_id": target,
                "before": _relationship_json(before),
                "after": _relationship_json(after),
            }
        if action == "relationship.history":
            relationship_events = await self._relationships.get_history(actor, target)
            return {
                "target_user_id": target,
                "events": [
                    {
                        "id": row.id,
                        "affection_delta": row.affection_delta,
                        "trust_delta": row.trust_delta,
                        "change_type": row.change_type,
                        "reason_code": row.reason_code,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in relationship_events
                ],
            }
        if action == "memory.list":
            memory_rows = await self._memories.list_memories(actor, target)
            return {
                "target_user_id": target,
                "memories": [
                    {
                        "id": row.id,
                        "content": row.content,
                        "source_type": row.source_type,
                    }
                    for row in memory_rows
                ],
            }
        if action == "memory.add":
            memory_row = await self._memories.add_memory(
                actor,
                target,
                _required_text(arguments, "content"),
            )
            return {
                "target_user_id": target,
                "memory_id": memory_row.id,
                "content": memory_row.content,
            }
        if action == "memory.update":
            memory_id = _required_int(arguments, "memory_id")
            updated = await self._memories.update_memory(
                actor,
                target,
                memory_id,
                _required_text(arguments, "content"),
            )
            if not updated:
                raise ValueError("没有找到可修改的记忆")
            return {"target_user_id": target, "memory_id": memory_id, "updated": True}
        if action == "memory.delete":
            memory_id = _required_int(arguments, "memory_id")
            deleted = await self._memories.delete_memory(actor, target, memory_id)
            if not deleted:
                raise ValueError("没有找到该记忆")
            return {"target_user_id": target, "memory_id": memory_id, "deleted": True}
        if action == "preference.list":
            preference_rows = await self._preferences.list_preferences(actor, target)
            return {
                "target_user_id": target,
                "preferences": [{"key": row.key, "value": row.value} for row in preference_rows],
            }
        if action == "preference.set":
            preference_row = await self._preferences.set_preference(
                actor,
                target,
                _required_text(arguments, "key"),
                _required_text(arguments, "value"),
            )
            return {
                "target_user_id": target,
                "key": preference_row.key,
                "value": preference_row.value,
            }
        if action == "preference.delete":
            key = _required_text(arguments, "key")
            deleted = await self._preferences.delete_preference(actor, target, key)
            if not deleted:
                raise ValueError("没有找到该偏好")
            return {"target_user_id": target, "key": key, "deleted": True}
        if action == "group.enable":
            group_row = await self._groups.enable_current_group(actor, target)
            return {"group_id": target, "enabled": group_row.enabled}
        if action == "group.disable":
            group_row = await self._groups.disable_current_group(actor, target)
            return {"group_id": target, "enabled": group_row.enabled}
        if action == "group.autonomous_enable":
            group_row = await self._groups.set_autonomous_enabled(actor, target, True)
            return {
                "group_id": target,
                "autonomous_enabled": group_row.autonomous_enabled,
            }
        if action == "group.autonomous_disable":
            group_row = await self._groups.set_autonomous_enabled(actor, target, False)
            return {
                "group_id": target,
                "autonomous_enabled": group_row.autonomous_enabled,
            }
        if action == "private_access.enable":
            private_row = await self._private_access.enable_user(actor, target)
            return {"target_user_id": target, "enabled": private_row.enabled}
        if action == "private_access.disable":
            private_row = await self._private_access.disable_user(actor, target)
            return {"target_user_id": target, "enabled": private_row.enabled}
        raise KeyError(f"未实现管理员 action：{action}")


def _relationship_json(row: Any) -> dict[str, Any]:
    return {
        "user_id": row.user_id,
        "affection_score": row.affection_score,
        "trust_score": row.trust_score,
        "effective_trust": row.effective_trust,
        "stage": row.stage.value,
    }
