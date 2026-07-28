"""Deterministic Planner provider for unit and integration tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.planner.models import PlannerInput, TurnPlan
from qq_ai_bot.planner.provider import (
    _await_with_cancellation,
    constrain_turn_plan,
    deterministic_fallback_plan,
)

FakePlanFactory = Callable[[PlannerInput], TurnPlan | Awaitable[TurnPlan]]


class FakePlannerProvider:
    """Return a configured plan while preserving cancellation and call inspection."""

    def __init__(
        self,
        result: TurnPlan | FakePlanFactory | None = None,
        *,
        delay_seconds: float = 0.0,
        hard_max_messages: int | None = None,
        max_wait_seconds: float | None = None,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        self._result = result
        self._delay_seconds = delay_seconds
        self._hard_max_messages = hard_max_messages
        self._max_wait_seconds = max_wait_seconds
        self.inputs: list[PlannerInput] = []

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        cancellation: asyncio.Event | None = None,
    ) -> TurnPlan:
        """Record the input and return one constrained deterministic plan."""

        self.inputs.append(planner_input)
        if self._delay_seconds:
            await _await_with_cancellation(
                asyncio.sleep(self._delay_seconds),
                cancellation=cancellation,
                timeout_seconds=max(1.0, self._delay_seconds + 1.0),
            )
        elif cancellation is not None and cancellation.is_set():
            from qq_ai_bot.planner.provider import PlannerInterruptedError

            raise PlannerInterruptedError("planner request superseded")
        result = self._result
        if result is None:
            plan = deterministic_fallback_plan(planner_input)
        elif isinstance(result, TurnPlan):
            plan = result
        else:
            produced = result(planner_input)
            plan = await produced if inspect.isawaitable(produced) else produced
        return constrain_turn_plan(
            plan.model_dump(mode="python"),
            planner_input,
            hard_max_messages=(
                runtime.reply.plan_hard_max_messages or self._hard_max_messages or 10
            ),
            max_wait_seconds=(
                runtime.planner.max_wait_seconds
                if runtime.planner.max_wait_seconds is not None
                else (self._max_wait_seconds or 60.0)
            ),
        )
