"""Debounced group observation delegated to the same Planner-first chat path."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.structured import StructuredTaskRunner
from qq_ai_bot.persistence.repositories import MemoryRepository
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.models import PlannerDecision
from qq_ai_bot.planner.provider import PlannerInterruptedError as ProviderPlannerInterruptedError
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.plugin_host.planner_adapter import PluginPlannerSignalAdapter
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    PlannerInterruptedError,
    TurnSupersededError,
    TurnToken,
)

logger = logging.getLogger(__name__)


class _LegacyParticipation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confidence: float = Field(ge=0, le=1)
    reason: str = ""


@dataclass(slots=True)
class _GroupState:
    messages: deque[InboundMessage] = field(default_factory=lambda: deque(maxlen=100))
    profiles: deque[UserProfileSnapshot] = field(default_factory=lambda: deque(maxlen=100))
    senders: deque[OutboundSender] = field(default_factory=lambda: deque(maxlen=100))
    latest_token: TurnToken | None = None
    task: asyncio.Task[None] | None = None
    human_version: int = 0
    last_response_human_version: int = -1
    last_response_at: float | None = None
    hourly_responses: deque[float] = field(default_factory=deque)


class AutonomousGroupService:
    """Only debounce batches; Planner owns all participation decisions."""

    def __init__(
        self,
        *,
        settings: Settings,
        chat: ChatService,
        runtime_config: RuntimeConfigService | None = None,
        planner_context: PlannerContextBuilder | None = None,
        planner: PlannerService | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        planner_signals: PluginPlannerSignalAdapter | None = None,
        # Kept as compatibility-only constructor inputs for tests/extensions built
        # against 1.5.  The 1.6 flow no longer owns a second confidence LLM.
        provider: ModelCompleter | None = None,
        model_executor: ModelExecutor | None = None,
        concurrency: ConcurrencyManager | None = None,
        memories: MemoryRepository | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._chat = chat
        self._runtime_config = runtime_config or chat._runtime_config
        self._planner_context = planner_context
        self._planner = planner
        self._coordinator = turn_coordinator or chat._turn_coordinator
        self._planner_signals = planner_signals
        self._legacy_models = (
            require_model_executor(
                model_executor,
                provider=provider,
                model=settings.llm_model or "fake",
            )
            if model_executor is not None or provider is not None
            else None
        )
        self._legacy_structured = (
            StructuredTaskRunner(self._legacy_models) if self._legacy_models is not None else None
        )
        self._legacy_concurrency = concurrency
        self._legacy_memories = memories
        self._clock = clock
        self._states: dict[str, _GroupState] = {}

    def observe(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        sender: OutboundSender,
        turn_token: TurnToken | None = None,
    ) -> None:
        if message.group_id is None:
            return
        state = self._states.setdefault(message.group_id, _GroupState())
        state.messages.append(message)
        state.profiles.append(profile)
        state.senders.append(sender)
        state.latest_token = turn_token
        state.human_version += 1
        if state.task is not None and not state.task.done():
            state.task.cancel()
        state.task = asyncio.create_task(
            self._after_silence(message.group_id),
            name=f"planner-group-{message.group_id}",
        )

    async def _after_silence(self, group_id: str) -> None:
        try:
            runtime = await self._runtime_config.snapshot(group_id=group_id)
            planner_context = self._planner_context
            if runtime.planner.enabled:
                if not runtime.planner.group_enabled or planner_context is None:
                    return
            elif not runtime.autonomous.enabled:
                return
            debounce_seconds = (
                runtime.planner.group_debounce_seconds
                if runtime.planner.enabled and self._planner is not None
                else runtime.autonomous.silence_seconds
            )
            await asyncio.sleep(debounce_seconds)
            state = self._states.get(group_id)
            if state is None or not state.messages:
                return
            if not runtime.planner.enabled:
                await self._legacy_after_silence(group_id, state, runtime)
                return
            if planner_context is None:
                return
            planner = self._planner
            if planner is None:
                # Compatibility instances without Planner preserve silence instead
                # of reintroducing the deleted candidate/confidence route.
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
            planner_input = await planner_context.build(
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
                outcome = await planner.plan(
                    planner_input,
                    runtime=runtime,
                    turn_version=token.version,
                )
            plan = outcome.planned_turn.plan
            if plan.decision is PlannerDecision.WAIT:
                if plan.wait_seconds > 0:
                    await asyncio.sleep(plan.wait_seconds)
                if not self._coordinator.is_current(token):
                    await planner.record_delivery(
                        outcome.run_id,
                        messages_sent=0,
                        interrupted=True,
                    )
                    return
                await planner.record_delivery(
                    outcome.run_id,
                    messages_sent=0,
                    interrupted=False,
                )
                # Re-plan exactly once after a bounded wait. A second wait becomes
                # silence, so one group message cannot create an endless loop.
                refreshed = await planner_context.build(
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
                    outcome = await planner.plan(
                        refreshed,
                        runtime=runtime,
                        turn_version=token.version,
                    )
                plan = outcome.planned_turn.plan
                if plan.decision is PlannerDecision.WAIT:
                    await planner.record_delivery(
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
            await planner.record_delivery(
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
        except (LLMError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning("planner_group_failed exception_category=%s", type(exc).__name__)

    async def _legacy_after_silence(
        self,
        group_id: str,
        state: _GroupState,
        runtime: RuntimeConfigSnapshot,
    ) -> None:
        """Preserve 1.5.2 only when Planner is explicitly disabled."""

        models = self._legacy_models
        structured = self._legacy_structured
        concurrency = self._legacy_concurrency
        if models is None or structured is None or concurrency is None:
            return
        config = runtime
        now = self._clock()
        while state.hourly_responses and now - state.hourly_responses[0] >= 3600:
            state.hourly_responses.popleft()
        if state.human_version <= state.last_response_human_version:
            return
        if (
            state.last_response_at is not None
            and now - state.last_response_at < config.autonomous.cooldown_seconds
        ):
            return
        if len(state.hourly_responses) >= config.autonomous.max_per_hour:
            return
        last = state.messages[-1]
        if not await self._legacy_candidate(last):
            return
        transcript = [
            {"user_id": item.sender.user_id, "content": item.text}
            for item in tuple(state.messages)[-config.context.local_event_limit :]
        ]
        result = await concurrency.run_llm(
            f"autonomous-decision:{group_id}",
            lambda: structured.run(
                task=ModelTask.UTILITY_STRUCTURED,
                instruction=(
                    "判断一个像真实群友的机器人此时是否应主动插话。"
                    "只有能自然帮助对话且不会打扰时才参与。"
                ),
                structured_input={"transcript": transcript},
                output_model=_LegacyParticipation,
                temperature=0,
                max_output_tokens=None,
                allow_text_json=True,
            ),
        )
        if result.confidence < config.autonomous.confidence_threshold:
            return
        batch = "\n".join(f"[QQ {item.sender.user_id}] {item.text}" for item in state.messages)
        identity = ConversationIdentity.group(
            group_id,
            last.sender.user_id,
            ConversationMode.SHARED,
        )
        sent = await self._chat.respond(
            last,
            identity,
            state.profiles[-1],
            f"以下是群聊刚刚的消息，请像普通群友一样谨慎参与：\n{batch}",
            state.senders[-1],
            autonomous=True,
            runtime_snapshot=config,
        )
        if sent:
            finished = self._clock()
            state.last_response_at = finished
            state.hourly_responses.append(finished)
            state.last_response_human_version = state.human_version

    async def _legacy_candidate(self, message: InboundMessage) -> bool:
        text = message.text.strip()
        if message.reply_sender_user_id == message.bot_user_id:
            return True
        if any(token in text.casefold() for token in ("机器人", "bot", "yuki")):
            return True
        if text.endswith(("?", "？")) or any(
            token in text for token in ("谁", "什么", "怎么", "为何", "为什么", "吗")
        ):
            return True
        if message.group_id and self._legacy_memories is not None:
            memories = await self._legacy_memories.list_group(message.group_id, limit=30)
            return any(
                fragment.strip() and fragment in text
                for memory in memories
                for fragment in (
                    memory.content[index : index + 2]
                    for index in range(max(0, len(memory.content) - 1))
                )
            )
        return False

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
