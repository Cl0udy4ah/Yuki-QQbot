from __future__ import annotations

import asyncio

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    PlannerInterruptedError,
    ReplySequenceCancelled,
)


async def test_new_group_message_interrupts_autonomous_planner() -> None:
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:1", TurnOrigin.AUTONOMOUS_GROUP)
    entered = asyncio.Event()

    async def old_turn() -> None:
        with pytest.raises(PlannerInterruptedError):
            async with coordinator.track(token, "planner"):
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(old_turn())
    await entered.wait()
    newer = await coordinator.notify_message("group:1", TurnOrigin.USER_MESSAGE)
    await task
    assert coordinator.is_current(newer)
    assert not coordinator.is_current(token)


async def test_new_message_cancels_only_unsent_reply_for_direct_turn() -> None:
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("private:1")
    entered = asyncio.Event()

    async def reply() -> None:
        with pytest.raises(ReplySequenceCancelled):
            async with coordinator.track(token, "reply"):
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(reply())
    await entered.wait()
    await coordinator.notify_message("private:1")
    await task


async def test_mutation_started_prevents_automatic_generation_cancel() -> None:
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:1", TurnOrigin.AUTONOMOUS_GROUP)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generation() -> None:
        async with coordinator.track(token, "generation"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(generation())
    await entered.wait()
    await coordinator.mark_mutation_started(token)
    await coordinator.notify_message("group:1")
    assert not task.done()
    release.set()
    await task


async def test_promoted_autonomous_planner_is_cancelled_by_next_real_message() -> None:
    coordinator = ConversationTurnCoordinator()
    observed = await coordinator.notify_message("group:42")
    autonomous = await coordinator.begin_autonomous(observed)
    assert autonomous is not None
    entered = asyncio.Event()

    async def run_planner() -> None:
        with pytest.raises(PlannerInterruptedError):
            async with coordinator.track(autonomous, "planner"):
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(run_planner())
    await entered.wait()
    replacement = await coordinator.notify_message("group:42")
    await task
    assert replacement.version == observed.version + 1


async def test_autonomous_promotion_does_not_override_active_explicit_turn() -> None:
    coordinator = ConversationTurnCoordinator()
    explicit = await coordinator.notify_message("group:42")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_generation() -> None:
        async with coordinator.track(explicit, "generation"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(run_generation())
    await entered.wait()
    assert await coordinator.begin_autonomous(explicit) is None
    release.set()
    await task
