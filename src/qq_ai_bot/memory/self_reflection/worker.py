"""Three-times-daily, bounded Yuki self-reflection scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.self_reflection.models import SelfReflectionHealth
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService

logger = logging.getLogger(__name__)


class SelfReflectionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: SelfReflectionRepository,
        service: SelfReflectionService,
        metrics: MemoryLifecycleMetrics,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._service = service
        self._metrics = metrics
        self._hours = frozenset(
            int(item.strip()) for item in settings.memory_self_reflection_schedule_hours.split(",")
        )
        self._timezone = ZoneInfo(settings.memory_self_reflection_timezone)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._settings.memory_self_reflection_enabled or self._task is not None:
            return
        self._stop.clear()
        await self._repository.scan_new_events()
        self._task = asyncio.create_task(self._run(), name="memory-self-reflection-worker")
        logger.info(
            "memory_self_reflection_started schedule_hours=%s timezone=%s "
            "max_sessions=%d max_daily_calls=%d",
            ",".join(str(item) for item in sorted(self._hours)),
            self._settings.memory_self_reflection_timezone,
            self._settings.memory_self_reflection_max_sessions_per_run,
            self._settings.memory_self_reflection_max_daily_calls,
        )

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def process_once(self, now: datetime | None = None) -> int:
        await self._repository.scan_new_events()
        local = (now or datetime.now(UTC)).astimezone(self._timezone)
        if local.hour not in self._hours:
            return 0
        slot = f"{local.date().isoformat()}:{local.hour:02d}"
        episodes = await self._repository.claim_due(
            scheduled_slot=slot,
            local_date=local.date().isoformat(),
            event_threshold=self._settings.memory_self_reflection_event_threshold,
            character_threshold=self._settings.memory_self_reflection_character_threshold,
            max_wait_seconds=self._settings.memory_self_reflection_max_wait_seconds,
            max_sessions=self._settings.memory_self_reflection_max_sessions_per_run,
            max_daily_calls=self._settings.memory_self_reflection_max_daily_calls,
            max_events=self._settings.memory_self_reflection_max_events,
            max_characters=self._settings.memory_self_reflection_max_characters,
        )
        for episode in episodes:
            try:
                proposals, committed = await self._service.reflect(episode)
                await self._repository.complete(
                    episode,
                    proposals=proposals,
                    committed=committed,
                )
                logger.info(
                    "memory_self_reflection_completed run_id=%d trigger=%s "
                    "events=%d proposals=%d committed=%d",
                    episode.run_id,
                    episode.trigger_reason,
                    len(episode.events),
                    proposals,
                    committed,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                await self._repository.fail(episode.run_id, type(exc).__name__)
                self._metrics.increment("self_reflection_failed")
                logger.warning(
                    "memory_self_reflection_failed run_id=%d trigger=%s error_category=%s",
                    episode.run_id,
                    episode.trigger_reason,
                    type(exc).__name__,
                )
        await self._repository.cleanup_receipts()
        return len(episodes)

    async def health(self) -> SelfReflectionHealth:
        local = datetime.now(UTC).astimezone(self._timezone)
        pending, calls, status, completed_at = await self._repository.health_snapshot(
            local_date=local.date().isoformat()
        )
        return SelfReflectionHealth(
            enabled=self._settings.memory_self_reflection_enabled,
            running=self._task is not None and not self._task.done(),
            schedule_hours=tuple(sorted(self._hours)),
            timezone=self._settings.memory_self_reflection_timezone,
            pending_conversations=pending,
            calls_today=calls,
            last_run_status=status,
            last_run_completed_at=completed_at,
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "memory_self_reflection_cycle_failed error_category=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._settings.memory_self_reflection_poll_seconds,
                )
            except TimeoutError:
                pass
