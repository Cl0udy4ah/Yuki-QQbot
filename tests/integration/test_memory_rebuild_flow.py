"""Offline event-ledger to reviewed Memory V2 fact integration flow."""

from __future__ import annotations

import pytest
from tests.unit.test_memory_rebuild import _event, _service

from qq_ai_bot.memory.enums import MemoryRebuildRunStatus, MemorySourceType
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.worker import MemoryRebuildWorker
from qq_ai_bot.persistence.database import Database


@pytest.mark.asyncio
async def test_memory_rebuild_reviewed_commit_end_to_end(database: Database) -> None:
    settings, ledger, facts, provider, service = await _service(database)
    await _event(ledger, message_id="integration-history", content="我长期住在杭州")

    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service,
        interval_seconds=settings.memory_rebuild_worker_interval_seconds,
    )
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert (await service.repository.get_run(run.public_id)).status is MemoryRebuildRunStatus.REVIEW
    assert (
        await service.set_review(
            run.public_id,
            "all",
            approved=True,
            actor_user_id="9000",
        )
        == 1
    )
    await service.commit(run.public_id, actor_user_id="9000")
    assert await worker.process_once() == 1

    status = await service.status(run.public_id, actor_user_id="9000")
    stored = await facts.list_person("1001")
    assert status["run"]["status"] == MemoryRebuildRunStatus.COMPLETED.value
    assert status["statistics"]["receipts_completed"] == 1
    assert provider.requests == 1
    assert len(stored) == 1
    assert stored[0].content == "我长期住在杭州"
    assert stored[0].source_type is MemorySourceType.REBUILD
