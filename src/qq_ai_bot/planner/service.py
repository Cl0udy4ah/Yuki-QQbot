"""Planner orchestration, deterministic fallback, and monotonic backend constraints."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannedTurn,
    PlannerDecision,
    PlannerInput,
    PlannerReasonCode,
    ToolMode,
    TurnPlan,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.provider import PlannerProvider, deterministic_fallback_plan
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.services.plugin_events import (
    LifecycleEventPublisher,
    publish_notification,
)
from qq_ai_bot.speech.models import VoiceMode, VoiceReplyPlan
from yuki_plugin_sdk.events import EventName

_MULTI_MESSAGE_REQUESTS = (
    "多发几条",
    "发多条",
    "多条消息",
    "分几条",
    "拆成几条",
    "分开发",
    "一句一条",
)


@dataclass(frozen=True, slots=True)
class PlannerOutcome:
    planned_turn: PlannedTurn
    run_id: int | None = None


class PlannerService:
    """Make Planner the single decision boundary before the normal Yuki Agent."""

    def __init__(
        self,
        *,
        provider: PlannerProvider,
        observability: PlannerObservability,
        repository: PlannerRepository | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._provider = provider
        self._observability = observability
        self._repository = repository
        self._event_publisher = event_publisher

    @property
    def observability(self) -> PlannerObservability:
        return self._observability

    @property
    def repository(self) -> PlannerRepository | None:
        return self._repository

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        """Attach the host notification bus without coupling Planner to PluginHost."""

        self._event_publisher = publisher

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        turn_version: int,
        administrator_request: bool = False,
    ) -> PlannerOutcome:
        started = time.perf_counter()
        self._observability.record_necessity(
            planner_input.necessity,
            conversation_key=planner_input.conversation_key,
        )
        enabled_for_turn = runtime.planner.enabled and (
            (planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP and runtime.planner.group_enabled)
            or (
                planner_input.origin is not TurnOrigin.AUTONOMOUS_GROUP
                and runtime.planner.direct_enabled
            )
        )
        should_call = enabled_for_turn and planner_input.necessity.should_enter_planner
        run_id = (
            await self._begin_run(
                planner_input,
                planner_used=should_call,
                planner_model=((runtime.planner.model or runtime.llm.model) if should_call else ""),
            )
            if runtime.planner.record_runs
            else None
        )
        fallback_used = False
        if not planner_input.necessity.should_enter_planner:
            plan = self._silent_gate_plan()
        elif not enabled_for_turn:
            plan = deterministic_fallback_plan(planner_input)
            fallback_used = True
        else:
            plan = await self._provider.plan(planner_input, runtime=runtime)
            fallback_used = plan.reason_code is PlannerReasonCode.PLANNER_FALLBACK
        plan = self._constrain_business_rules(
            plan,
            planner_input,
            runtime,
            administrator_request=administrator_request,
        )
        latency = time.perf_counter() - started
        planned = PlannedTurn(
            plan=plan,
            necessity=planner_input.necessity,
            planner_model=(runtime.planner.model or runtime.llm.model) if should_call else "",
            planner_latency_seconds=latency,
            planner_used=should_call,
            fallback_used=fallback_used,
            turn_version=turn_version,
        )
        await self._finish_run(run_id, planned)
        await publish_notification(
            self._event_publisher,
            EventName.PLANNER_PLANNED,
            {
                "trigger_message_id": planner_input.trigger_message_id,
                "scope_type": planner_input.scope_type.value,
                "origin": planner_input.origin.value,
                "decision": plan.decision.value,
                "reason_code": plan.reason_code.value,
                "delivery_mode": plan.delivery_mode.value,
                "desired_messages": plan.desired_messages,
                "tool_mode": plan.tool_mode.value,
                "confidence": plan.confidence,
                "planner_used": should_call,
                "fallback_used": fallback_used,
                "latency_milliseconds": round(latency * 1000),
                "turn_version": turn_version,
            },
        )
        return PlannerOutcome(planned, run_id)

    @staticmethod
    def _silent_gate_plan() -> TurnPlan:
        return TurnPlan(
            decision=PlannerDecision.SILENT,
            intent="回复必要性不足，本轮不打扰群聊",
            delivery_mode=DeliveryMode.CONCISE,
            desired_messages=1,
            tool_mode=ToolMode.NONE,
            confidence=1.0,
            reason_code=PlannerReasonCode.LOW_RELEVANCE,
        )

    @staticmethod
    def _constrain_business_rules(
        plan: TurnPlan,
        planner_input: PlannerInput,
        runtime: RuntimeConfigSnapshot,
        *,
        administrator_request: bool,
    ) -> TurnPlan:
        hard_max = runtime.reply.plan_hard_max_messages
        preferred = min(runtime.planner.preferred_messages, hard_max)
        requested_multi = any(
            token in planner_input.current_message.text for token in _MULTI_MESSAGE_REQUESTS
        )
        delivery_mode = DeliveryMode.NATURAL_MULTI if requested_multi else plan.delivery_mode
        desired_messages = (
            preferred
            if delivery_mode is DeliveryMode.NATURAL_MULTI
            else min(plan.desired_messages, hard_max)
        )
        updates: dict[str, object] = {
            "delivery_mode": delivery_mode,
            "desired_messages": desired_messages,
            "reply_to_message_id": (
                plan.reply_to_message_id
                if plan.reply_to_message_id is not None
                and plan.reply_to_message_id.isdigit()
                and plan.reply_to_message_id in planner_input.known_message_ids
                else None
            ),
            "wait_seconds": min(plan.wait_seconds, runtime.planner.max_wait_seconds),
        }
        speech_allowed = (
            runtime.speech.enabled
            and runtime.speech.planner_enabled
            and planner_input.speech.available
            and (
                runtime.speech.private_enabled
                if planner_input.scope_type is ScopeType.PRIVATE
                else runtime.speech.group_enabled
            )
        )
        if not speech_allowed:
            updates["voice"] = VoiceReplyPlan(mode=VoiceMode.TEXT)
        elif (
            plan.voice.style_hint
            and plan.voice.style_hint not in planner_input.speech.available_styles
        ):
            updates["voice"] = plan.voice.model_copy(update={"style_hint": ""})
        explicit = (
            planner_input.scope_type is ScopeType.PRIVATE
            or planner_input.mentions_bot
            or planner_input.reply_target_is_bot
        )
        text = planner_input.current_message.text
        looks_like_request = any(
            token in text
            for token in ("?", "？", "请", "帮我", "怎么", "为什么", "能不能", "改成", "设置")
        )
        if (
            explicit
            or administrator_request
            or (planner_input.scope_type is ScopeType.PRIVATE and looks_like_request)
        ):
            updates["decision"] = PlannerDecision.REPLY
            updates["wait_seconds"] = 0.0
        if (
            planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP
            and plan.decision is PlannerDecision.REPLY
            and plan.reason_code is not PlannerReasonCode.PLANNER_FALLBACK
            and plan.confidence < runtime.planner.confidence_threshold
        ):
            updates.update(
                decision=PlannerDecision.SILENT,
                reason_code=PlannerReasonCode.LOW_RELEVANCE,
                wait_seconds=0.0,
            )
        if not explicit and planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP:
            # The model may only decide after the deterministic gate has admitted it.
            if not planner_input.necessity.should_enter_planner:
                updates["decision"] = PlannerDecision.SILENT
        return plan.model_copy(update=updates)

    async def _begin_run(
        self,
        planner_input: PlannerInput,
        *,
        planner_used: bool,
        planner_model: str,
    ) -> int | None:
        if self._repository is None:
            return None
        row = await self._repository.begin(
            conversation_key=planner_input.conversation_key,
            trigger_message_id=planner_input.trigger_message_id,
            scope_type=planner_input.scope_type.value,
            origin=planner_input.origin.value,
            sender_user_id=planner_input.current_sender_user_id,
            group_id=planner_input.current_group_id,
            necessity_score=planner_input.necessity.score,
            necessity_reasons={"reasons": planner_input.necessity.reasons},
            gate_decision=("enter" if planner_input.necessity.should_enter_planner else "skip"),
            planner_used=planner_used,
            planner_model=planner_model,
        )
        return row.id

    async def _finish_run(self, run_id: int | None, planned: PlannedTurn) -> None:
        if self._repository is None or run_id is None:
            return
        plan = planned.plan
        await self._repository.finish(
            run_id,
            planner_decision=plan.decision.value,
            reason_code=plan.reason_code.value,
            delivery_mode=plan.delivery_mode.value,
            desired_messages=plan.desired_messages,
            tool_mode=plan.tool_mode.value,
            confidence=plan.confidence,
            latency_seconds=planned.planner_latency_seconds,
            fallback_used=planned.fallback_used,
            messages_planned=plan.desired_messages if plan.decision is PlannerDecision.REPLY else 0,
        )

    async def record_delivery(
        self,
        run_id: int | None,
        *,
        messages_sent: int,
        interrupted: bool = False,
        error_category: str | None = None,
    ) -> None:
        if self._repository is not None and run_id is not None:
            await self._repository.update_delivery(
                run_id,
                messages_sent=messages_sent,
                interrupted=interrupted,
                error_category=error_category,
            )
