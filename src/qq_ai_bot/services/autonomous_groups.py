"""Debounced group observation delegated to the same Planner-first chat path."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.models import PlannerDecision
from qq_ai_bot.planner.provider import PlannerInterruptedError as ProviderPlannerInterruptedError
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.plugin_host.planner_adapter import PluginPlannerSignalAdapter
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    PlannerInterruptedError,
    TurnSupersededError,
    TurnToken,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _GroupState:
    messages: deque[InboundMessage] = field(default_factory=lambda: deque(maxlen=100))
    profiles: deque[UserProfileSnapshot] = field(default_factory=lambda: deque(maxlen=100))
    senders: deque[OutboundSender] = field(default_factory=lambda: deque(maxlen=100))
    latest_token: TurnToken | None = None
    task: asyncio.Task[None] | None = None


class AutonomousGroupService:
    """Only debounce batches; Planner owns all participation decisions."""

    def __init__(
        self,
        *,
        chat: ChatService,
        planner_context: PlannerContextBuilder,
        planner: PlannerService,
        runtime_config: RuntimeConfigService | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        planner_signals: PluginPlannerSignalAdapter | None = None,
    ) -> None:
        self._chat = chat
        self._runtime_config = runtime_config or chat._runtime_config
        self._planner_context = planner_context
        self._planner = planner
        self._coordinator = turn_coordinator or chat._turn_coordinator
        self._planner_signals = planner_signals
        self._states: dict[str, _GroupState] = {}
        self._task_failures = 0

    @property
    def task_failures(self) -> int:
        """Return the process-local count of observed background task failures."""

        return self._task_failures

    def observe(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        sender: OutboundSender,
        turn_token: TurnToken | None = None,
    ) -> None:
        group_id = message.group_id
        if group_id is None:
            return
        state = self._states.setdefault(group_id, _GroupState())
        state.messages.append(message)
        state.profiles.append(profile)
        state.senders.append(sender)
        state.latest_token = turn_token
        if state.task is not None and not state.task.done():
            state.task.cancel()
        task = asyncio.create_task(
            self._after_silence(group_id),
            name=f"planner-group-{group_id}",
        )
        state.task = task

        def task_done(completed: asyncio.Task[None]) -> None:
            self._task_done(group_id, completed)

        task.add_done_callback(task_done)

    def _task_done(self, group_id: str, completed: asyncio.Task[None]) -> None:
        """Own a detached task, consume its outcome, and release its state reference."""

        state = self._states.get(group_id)
        if state is not None and state.task is completed:
            state.task = None
        try:
            completed.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._task_failures += 1
            logger.exception(
                "autonomous_group_task_failed exception_category=%s",
                type(exc).__name__,
            )

    async def _after_silence(self, group_id: str) -> None:
        try:
            runtime = await self._runtime_config.snapshot(group_id=group_id)
            if not runtime.planner.group_enabled:
                return
            await asyncio.sleep(runtime.planner.group_debounce_seconds)
            state = self._states.get(group_id)
            if state is None or not state.messages:
                return
            last = state.messages[-1]
            token = state.latest_token
            if token is None:
                token = await self._coordinator.notify_message(
                    f"group:{group_id}",
                    TurnOrigin.AUTONOMOUS_GROUP,
                )
            else:
                token = await self._coordinator.begin_autonomous(token)
                if token is None:
                    return
            batch_messages = tuple(state.messages)[-runtime.planner.max_pending_messages :]
            batch = "\n".join(
                f"[外部不可信群消息，发送者 QQ {item.sender.user_id}] {item.text}"
                for item in batch_messages
            )
            planner_input = await self._planner_context.build(
                inbound=last,
                conversation_key=token.conversation_key,
                content=batch,
                origin=TurnOrigin.AUTONOMOUS_GROUP,
                runtime=runtime,
                visual_input_present=False,
                available_tool_categories=("history", "memory", "web"),
                plugin_signals=(
                    await self._planner_signals.collect(
                        message=last,
                        origin=TurnOrigin.AUTONOMOUS_GROUP,
                        runtime=runtime,
                    )
                    if self._planner_signals is not None
                    else ()
                ),
            )
            async with self._coordinator.track(token, "planner"):
                outcome = await self._planner.plan(
                    planner_input,
                    runtime=runtime,
                    turn_version=token.version,
                )
            plan = outcome.planned_turn.plan
            if plan.decision is PlannerDecision.WAIT:
                if plan.wait_seconds > 0:
                    await asyncio.sleep(plan.wait_seconds)
                if not self._coordinator.is_current(token):
                    await self._planner.record_delivery(
                        outcome.run_id,
                        messages_sent=0,
                        interrupted=True,
                    )
                    return
                await self._planner.record_delivery(
                    outcome.run_id,
                    messages_sent=0,
                    interrupted=False,
                )
                # Re-plan exactly once after a bounded wait. A second wait becomes
                # silence, so one group message cannot create an endless loop.
                refreshed = await self._planner_context.build(
                    inbound=last,
                    conversation_key=token.conversation_key,
                    content=batch,
                    origin=TurnOrigin.AUTONOMOUS_GROUP,
                    runtime=runtime,
                    visual_input_present=False,
                    available_tool_categories=("history", "memory", "web"),
                    plugin_signals=(
                        await self._planner_signals.collect(
                            message=last,
                            origin=TurnOrigin.AUTONOMOUS_GROUP,
                            runtime=runtime,
                        )
                        if self._planner_signals is not None
                        else ()
                    ),
                )
                async with self._coordinator.track(token, "planner"):
                    outcome = await self._planner.plan(
                        refreshed,
                        runtime=runtime,
                        turn_version=token.version,
                    )
                plan = outcome.planned_turn.plan
                if plan.decision is PlannerDecision.WAIT:
                    await self._planner.record_delivery(
                        outcome.run_id,
                        messages_sent=0,
                        interrupted=False,
                    )
                    return
            if plan.decision is not PlannerDecision.REPLY:
                return
            identity = ConversationIdentity.group(
                group_id,
                last.sender.user_id,
                ConversationMode.SHARED,
            )
            sent = await self._chat.respond(
                last,
                identity,
                state.profiles[-1],
                batch,
                state.senders[-1],
                autonomous=True,
                runtime_snapshot=runtime,
                planned_turn=outcome.planned_turn,
                turn_token=token,
            )
            await self._planner.record_delivery(
                outcome.run_id,
                messages_sent=sent,
                interrupted=not self._coordinator.is_current(token),
            )
        except asyncio.CancelledError:
            raise
        except (
            PlannerInterruptedError,
            ProviderPlannerInterruptedError,
            TurnSupersededError,
        ):
            return
        except SQLAlchemyError as exc:
            self._task_failures += 1
            logger.warning(
                "autonomous_group_task_failed exception_category=%s",
                type(exc).__name__,
            )
        except (LLMError, OSError, RuntimeError, ValueError, TypeError) as exc:
            self._task_failures += 1
            logger.warning(
                "autonomous_group_task_failed exception_category=%s",
                type(exc).__name__,
            )

    async def wait_until_idle(self, group_id: str) -> None:
        state = self._states.get(group_id)
        if state is None or state.task is None:
            return
        await asyncio.shield(state.task)

    async def close(self) -> None:
        tasks = [
            state.task
            for state in self._states.values()
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
