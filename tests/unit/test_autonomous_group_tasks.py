from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.services.autonomous_groups import AutonomousGroupService, _GroupState


class _FailingRuntime:
    async def snapshot(self, **_kwargs: object) -> object:
        raise SQLAlchemyError("database unavailable")


def _service() -> AutonomousGroupService:
    chat = SimpleNamespace(_runtime_config=_FailingRuntime(), _turn_coordinator=object())
    return AutonomousGroupService(
        chat=cast(Any, chat),
        planner_context=cast(Any, object()),
        planner=cast(Any, object()),
        runtime_config=cast(Any, _FailingRuntime()),
        turn_coordinator=cast(Any, object()),
    )


@pytest.mark.asyncio
async def test_after_silence_observes_sqlalchemy_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service()
    caplog.set_level(logging.WARNING, logger="qq_ai_bot.services.autonomous_groups")
    await service._after_silence("2001")
    assert service.task_failures == 1
    assert "autonomous_group_task_failed" in caplog.text
    assert "SQLAlchemyError" in caplog.text


@pytest.mark.asyncio
async def test_task_owner_consumes_unexpected_failure_and_clears_reference(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service()
    service._states["2001"] = _GroupState()
    caplog.set_level(logging.ERROR, logger="qq_ai_bot.services.autonomous_groups")

    async def fail() -> None:
        raise LookupError("unexpected")

    task = asyncio.create_task(fail())
    service._states["2001"].task = task
    task.add_done_callback(lambda completed: service._task_done("2001", completed))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert service._states["2001"].task is None
    assert service.task_failures == 1
    assert "autonomous_group_task_failed" in caplog.text
    assert "Traceback" in caplog.text


@pytest.mark.asyncio
async def test_task_owner_treats_cancellation_as_normal() -> None:
    service = _service()
    service._states["2001"] = _GroupState()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    service._states["2001"].task = task
    task.add_done_callback(lambda completed: service._task_done("2001", completed))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert service._states["2001"].task is None
    assert service.task_failures == 0
