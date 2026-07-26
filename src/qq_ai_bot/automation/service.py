"""Application service shared by Agent tools and deterministic commands."""

from __future__ import annotations

import time

from pydantic import ValidationError

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.automation.authority import DelegatedAuthority, permission_for
from qq_ai_bot.automation.models import (
    AutomationRecord,
    AutomationRunRecord,
    AutomationScript,
    AutomationStatus,
)
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.time.schedules import initial_run_at
from qq_ai_bot.time.service import TimeContextService


class AutomationService:
    """Create and manage only validated tasks owned by the real event sender."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: AutomationRepository,
        registry: AutomationCapabilityRegistry,
        time_service: TimeContextService,
        audit: AdminAuditService | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._registry = registry
        self._time = time_service
        self._validator = AutomationValidator(settings=settings, registry=registry)
        self._audit = audit

    @property
    def enabled(self) -> bool:
        return self._settings.automation_enabled

    async def create(
        self,
        script_payload: object,
        *,
        inbound: InboundMessage,
        conversation_key: str,
        max_runs: int | None = None,
    ) -> AutomationRecord:
        self._require_enabled()
        if max_runs is not None and (
            isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs <= 0
        ):
            raise ValueError("max_runs 必须是正整数")
        started = time.perf_counter()
        try:
            script = AutomationScript.model_validate(script_payload)
        except ValidationError as exc:
            raise ValueError(f"自动化脚本格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        permission = permission_for(self._settings, inbound.sender.user_id)
        provenance = CreationProvenance(
            creator_user_id=inbound.sender.user_id,
            bot_user_id=inbound.bot_user_id,
            message_id=inbound.message_id,
            original_text=inbound.text,
            current_group_id=inbound.group_id,
            mentioned_user_ids=inbound.mentioned_user_ids,
            permission=permission,
        )
        validated = self._validator.validate(script, provenance, now_utc=now)
        maximum = (
            self._settings.automation_max_active_per_superuser
            if permission.value == "superuser"
            else self._settings.automation_max_active_per_user
        )
        if await self._repository.active_count(inbound.sender.user_id) >= maximum:
            raise ValueError(f"当前用户最多同时启用 {maximum} 个自动化任务")
        authority = DelegatedAuthority(
            creator_user_id=inbound.sender.user_id,
            bot_user_id=inbound.bot_user_id,
            created_from_message_id=inbound.message_id,
            created_at=now.isoformat(),
            permission_level=permission,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={
                name: self._registry.require(name).schema_version
                for name in validated.required_capabilities
            },
            current_group_id=inbound.group_id,
        )
        row = await self._repository.create(
            validated,
            authority,
            max_runs=max_runs,
            misfire_grace_seconds=self._settings.automation_default_misfire_grace_seconds,
            now=now,
        )
        await self._audit_event(
            inbound,
            conversation_key,
            operation="create",
            automation_id=row.id,
            after={"name": row.name, "status": row.status.value},
            started=started,
        )
        return row

    async def update(
        self,
        automation_id: int,
        script_payload: object,
        *,
        inbound: InboundMessage,
        conversation_key: str,
    ) -> AutomationRecord:
        existing = await self.require_owned(automation_id, inbound.sender.user_id)
        try:
            script = AutomationScript.model_validate(script_payload)
        except ValidationError as exc:
            raise ValueError(f"自动化脚本格式错误：{exc.errors()[0]['msg']}") from exc
        now = self._time.clock.now()
        permission = permission_for(self._settings, inbound.sender.user_id)
        validated = self._validator.validate(
            script,
            CreationProvenance(
                creator_user_id=inbound.sender.user_id,
                bot_user_id=inbound.bot_user_id,
                message_id=inbound.message_id,
                original_text=inbound.text,
                current_group_id=inbound.group_id,
                mentioned_user_ids=inbound.mentioned_user_ids,
                permission=permission,
            ),
            now_utc=now,
        )
        authority = DelegatedAuthority(
            creator_user_id=inbound.sender.user_id,
            bot_user_id=inbound.bot_user_id,
            created_from_message_id=inbound.message_id,
            created_at=now.isoformat(),
            permission_level=permission,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={
                name: self._registry.require(name).schema_version
                for name in validated.required_capabilities
            },
            current_group_id=inbound.group_id,
        )
        row = await self._repository.update_script(
            automation_id,
            creator_user_id=inbound.sender.user_id,
            validated=validated,
            authority=authority,
            now=now,
        )
        if row is None:
            raise ValueError("该任务已经结束，不能更新")
        await self._audit_event(
            inbound,
            conversation_key,
            operation="update",
            automation_id=row.id,
            before={"script_hash": existing.script_hash},
            after={"script_hash": row.script_hash},
        )
        return row

    async def list(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return all tasks for backwards-compatible internal callers."""

        self._require_enabled()
        return await self._repository.list_for_creator(creator_user_id)

    async def list_current(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return only active and paused tasks in current display order."""

        self._require_enabled()
        return await self._repository.list_current_for_creator(creator_user_id)

    async def list_completed(self, creator_user_id: str) -> tuple[AutomationRecord, ...]:
        """Return terminal tasks in a separate newest-first history queue."""

        self._require_enabled()
        return await self._repository.list_terminal_for_creator(creator_user_id)

    async def current_by_number(self, creator_user_id: str, task_number: int) -> AutomationRecord:
        """Resolve a transient 1-based number against the current task queue."""

        if task_number <= 0:
            raise ValueError("当前任务编号必须是正整数")
        rows = await self.list_current(creator_user_id)
        if task_number > len(rows):
            raise ValueError("没有找到该当前任务编号，请先使用 /ai automation list 刷新列表")
        return rows[task_number - 1]

    async def require_owned(self, automation_id: int, creator_user_id: str) -> AutomationRecord:
        self._require_enabled()
        row = await self._repository.get(automation_id)
        if row is None or row.creator_user_id != creator_user_id:
            raise ValueError("没有找到属于当前用户的自动化任务")
        return row

    async def pause(
        self, automation_id: int, *, inbound: InboundMessage, conversation_key: str
    ) -> bool:
        await self.require_owned(automation_id, inbound.sender.user_id)
        changed = await self._repository.set_status(
            automation_id,
            creator_user_id=inbound.sender.user_id,
            status=AutomationStatus.PAUSED,
            now=self._time.clock.now(),
        )
        await self._audit_event(
            inbound,
            conversation_key,
            operation="pause",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def resume(
        self, automation_id: int, *, inbound: InboundMessage, conversation_key: str
    ) -> bool:
        row = await self.require_owned(automation_id, inbound.sender.user_id)
        now = self._time.clock.now()
        next_run = initial_run_at(row.script.schedule, now, row.timezone)
        changed = await self._repository.resume(
            automation_id,
            creator_user_id=inbound.sender.user_id,
            next_run_at=next_run,
            now=now,
        )
        await self._audit_event(
            inbound,
            conversation_key,
            operation="resume",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def cancel(
        self, automation_id: int, *, inbound: InboundMessage, conversation_key: str
    ) -> bool:
        await self.require_owned(automation_id, inbound.sender.user_id)
        changed = await self._repository.set_status(
            automation_id,
            creator_user_id=inbound.sender.user_id,
            status=AutomationStatus.CANCELLED,
            now=self._time.clock.now(),
        )
        await self._audit_event(
            inbound,
            conversation_key,
            operation="cancel",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def run_now(
        self, automation_id: int, *, inbound: InboundMessage, conversation_key: str
    ) -> bool:
        await self.require_owned(automation_id, inbound.sender.user_id)
        changed = await self._repository.schedule_now(
            automation_id,
            creator_user_id=inbound.sender.user_id,
            now=self._time.clock.now(),
        )
        await self._audit_event(
            inbound,
            conversation_key,
            operation="run_now",
            automation_id=automation_id,
            after={"changed": changed},
        )
        return changed

    async def history(
        self, automation_id: int, *, creator_user_id: str, limit: int = 20
    ) -> tuple[AutomationRunRecord, ...]:
        await self.require_owned(automation_id, creator_user_id)
        return await self._repository.run_history(automation_id, limit=limit)

    async def current_time(self, user_id: str) -> dict[str, str]:
        return (await self._time.current(user_id)).to_model_dict()

    async def timezone(self, user_id: str) -> str:
        return await self._time.timezone_for(user_id)

    async def set_timezone(self, user_id: str, timezone: str) -> str:
        return await self._time.set_timezone(user_id, timezone)

    def _require_enabled(self) -> None:
        if not self._settings.automation_enabled:
            raise ValueError("自动化功能当前未启用")

    async def _audit_event(
        self,
        inbound: InboundMessage,
        conversation_key: str,
        *,
        operation: str,
        automation_id: int,
        before: object = None,
        after: object = None,
        started: float | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            actor=AdminActor(
                user_id=inbound.sender.user_id,
                is_superuser=inbound.sender.user_id in self._settings.superusers,
                trigger_message_id=inbound.message_id,
                conversation_key=conversation_key,
                current_group_id=inbound.group_id,
                mentioned_user_ids=inbound.mentioned_user_ids,
                current_message_text=inbound.text,
            ),
            capability="automation",
            operation=operation,
            target_type="automation",
            target_id=str(automation_id),
            before=before,
            after=after,
            success=True,
            duration_seconds=(time.perf_counter() - started if started is not None else 0),
        )
