"""Unified relationship administration for commands and natural-language tools."""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.relationships import RelationshipSnapshot
from qq_ai_bot.persistence.repositories import (
    RelationshipEventRecord,
    RelationshipRepository,
)
from qq_ai_bot.services.admin.common import (
    require_real_superuser,
    require_self_or_superuser,
)


class RelationshipAdminService:
    """Read relationship state and perform explicitly authorized manual changes."""

    def __init__(
        self,
        *,
        settings: Settings,
        relationships: RelationshipRepository,
        audit: AdminAuditService,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._relationships = relationships
        self._audit = audit
        self._runtime_config = runtime_config

    async def get_relationship(
        self,
        actor: AdminActor,
        target: str,
    ) -> RelationshipSnapshot:
        require_self_or_superuser(actor, target, self._settings)
        return await self._get_or_create(target)

    async def set_affection(
        self,
        actor: AdminActor,
        target: str,
        value: int,
    ) -> tuple[RelationshipSnapshot, RelationshipSnapshot]:
        require_real_superuser(actor, self._settings)
        started = time.perf_counter()
        try:
            async with self._audit.transaction() as session:
                before = await self._get_or_create(target, session=session)
                after = await self._relationships.set_affection(
                    user_id=target,
                    actor_user_id=actor.user_id,
                    score=value,
                    session=session,
                )
                await self._record_change(
                    actor,
                    "set_affection",
                    target,
                    before,
                    after,
                    started,
                    session=session,
                )
        except Exception as exc:
            await self._record_failure(
                actor,
                "set_affection",
                target,
                {"requested_affection": value},
                exc,
                started,
            )
            raise
        return before, after

    async def adjust_affection(
        self,
        actor: AdminActor,
        target: str,
        delta: int,
    ) -> tuple[RelationshipSnapshot, RelationshipSnapshot]:
        require_real_superuser(actor, self._settings)
        started = time.perf_counter()
        try:
            async with self._audit.transaction() as session:
                before = await self._get_or_create(target, session=session)
                after = await self._relationships.adjust_affection(
                    user_id=target,
                    actor_user_id=actor.user_id,
                    delta=delta,
                    session=session,
                )
                await self._record_change(
                    actor,
                    "adjust_affection",
                    target,
                    before,
                    after,
                    started,
                    session=session,
                )
        except Exception as exc:
            await self._record_failure(
                actor,
                "adjust_affection",
                target,
                {"requested_delta": delta},
                exc,
                started,
            )
            raise
        return before, after

    async def set_trust(
        self,
        actor: AdminActor,
        target: str,
        value: int,
    ) -> tuple[RelationshipSnapshot, RelationshipSnapshot]:
        require_real_superuser(actor, self._settings)
        started = time.perf_counter()
        try:
            async with self._audit.transaction() as session:
                before = await self._get_or_create(target, session=session)
                after = await self._relationships.set_trust(
                    user_id=target,
                    actor_user_id=actor.user_id,
                    score=value,
                    session=session,
                )
                await self._record_change(
                    actor,
                    "set_trust",
                    target,
                    before,
                    after,
                    started,
                    session=session,
                )
        except Exception as exc:
            await self._record_failure(
                actor,
                "set_trust",
                target,
                {"requested_trust": value},
                exc,
                started,
            )
            raise
        return before, after

    async def get_history(
        self,
        actor: AdminActor,
        target: str,
        *,
        limit: int = 10,
    ) -> tuple[RelationshipEventRecord, ...]:
        require_self_or_superuser(actor, target, self._settings)
        return await self._relationships.history(target, limit=limit)

    async def _record_change(
        self,
        actor: AdminActor,
        operation: str,
        target: str,
        before: RelationshipSnapshot,
        after: RelationshipSnapshot,
        started: float,
        *,
        session: AsyncSession,
    ) -> None:
        await self._audit.record(
            actor=actor,
            capability="relationship",
            operation=operation,
            target_type="user",
            target_id=target,
            before={
                "affection": before.affection_score,
                "trust": before.trust_score,
            },
            after={
                "affection": after.affection_score,
                "trust": after.trust_score,
            },
            success=True,
            duration_seconds=time.perf_counter() - started,
            session=session,
        )

    async def _record_failure(
        self,
        actor: AdminActor,
        operation: str,
        target: str,
        requested: object,
        error: Exception,
        started: float,
    ) -> None:
        try:
            await self._audit.record(
                actor=actor,
                capability="relationship",
                operation=operation,
                target_type="user",
                target_id=target,
                before=None,
                after=requested,
                success=False,
                error_category=type(error).__name__,
                duration_seconds=time.perf_counter() - started,
            )
        except Exception:
            # Preserve the original mutation/audit failure; no business change committed.
            pass

    async def _get_or_create(
        self,
        target: str,
        *,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if self._runtime_config is None:
            return await self._relationships.get_or_create(target, session=session)
        runtime = await self._runtime_config.snapshot(user_id=target)
        return await self._relationships.get_or_create(
            target,
            initial_affection=runtime.relationship.initial_affection,
            initial_trust=runtime.relationship.initial_trust,
            session=session,
        )
