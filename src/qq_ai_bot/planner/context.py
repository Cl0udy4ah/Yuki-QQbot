"""Build a bounded PlannerInput from trusted transport fields and ledger projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from math import ceil
from typing import Protocol

from qq_ai_bot.admin.models import RuntimeConfigSnapshot, SpeechRuntimeConfig
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.repositories import EventLedgerRepository, RelationshipRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.planner.models import (
    PlannerInput,
    PlannerMessage,
    PlannerSignal,
    PlannerSpeechContext,
)
from qq_ai_bot.planner.necessity import ReplyNecessityFeatures, ReplyNecessityScorer
from qq_ai_bot.planner.repository import PlannerRepository, PlannerVoiceCadence
from qq_ai_bot.speech.models import VoicePreferenceMode
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository


@dataclass(frozen=True, slots=True)
class _ConversationMetrics:
    pending: int
    bot_count: int
    average_interval: float
    idle: float
    since_bot: float | None
    last_was_bot: bool


class SpeechPlannerContextProvider(Protocol):
    async def planner_context(self, *, runtime: SpeechRuntimeConfig) -> PlannerSpeechContext: ...


class PlannerContextBuilder:
    """Keep repository reads out of PlannerService and the model provider."""

    def __init__(
        self,
        *,
        ledger: EventLedgerRepository,
        relationships: RelationshipRepository,
        speech: SpeechPlannerContextProvider | None = None,
        voice_preferences: VoicePreferenceRepository | None = None,
        planner_runs: PlannerRepository | None = None,
    ) -> None:
        self._ledger = ledger
        self._relationships = relationships
        self._speech = speech
        self._voice_preferences = voice_preferences
        self._planner_runs = planner_runs

    async def build(
        self,
        *,
        inbound: InboundMessage,
        conversation_key: str,
        content: str,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
        visual_input_present: bool = False,
        available_tool_categories: tuple[str, ...] = (),
        plugin_signals: tuple[PlannerSignal, ...] = (),
        speech: PlannerSpeechContext | None = None,
        now: datetime | None = None,
    ) -> PlannerInput:
        current_time = now or datetime.now(UTC)
        recent = await self._ledger.list_recent(
            scope_type=inbound.scope_type,
            user_id=inbound.sender.user_id,
            group_id=inbound.group_id,
            limit=runtime.planner.max_pending_messages,
        )
        relationship = await self._relationships.get(inbound.sender.user_id)
        metrics = self._metrics(recent, inbound.bot_user_id, current_time)
        relationship_adjustment = 0.0
        if relationship is not None:
            relationship_adjustment = max(
                -5.0,
                min(5.0, (relationship.relationship_weight - 50) / 10),
            )
        scorer = ReplyNecessityScorer(
            threshold=runtime.planner.reply_necessity_threshold,
        )
        necessity = scorer.score(
            ReplyNecessityFeatures(
                scope_type=inbound.scope_type,
                text=content,
                reply_target_is_bot=(
                    bool(inbound.reply_sender_user_id)
                    and inbound.reply_sender_user_id == inbound.bot_user_id
                ),
                mentions_bot=inbound.mentions_bot,
                continuation=metrics.last_was_bot,
                pending_message_count=metrics.pending,
                recent_bot_messages=metrics.bot_count,
                recent_total_messages=len(recent),
                average_human_interval_seconds=metrics.average_interval,
                idle_seconds=metrics.idle,
                seconds_since_last_bot_message=metrics.since_bot,
                relationship_adjustment=relationship_adjustment,
                plugin_signals=plugin_signals,
                new_message_count=max(1, metrics.pending),
                media_only=not content.strip() and bool(inbound.attachments),
                now=current_time,
            )
        )
        messages = tuple(self._planner_message(row) for row in recent)
        current = PlannerMessage(
            message_id=inbound.message_id,
            sender_user_id=inbound.sender.user_id,
            text=content,
            sender_is_bot=False,
            sent_at=inbound.received_at,
        )
        speech_context = (
            await self._speech.planner_context(runtime=runtime.speech)
            if self._speech is not None
            else speech
        )
        saved_preference = (
            await self._voice_preferences.get(inbound.sender.user_id)
            if self._voice_preferences is not None
            else None
        )
        preference_mode = (
            saved_preference.mode
            if saved_preference is not None
            else self._default_preference_mode(runtime.speech.default_mode)
        )
        cadence = (
            await self._planner_runs.voice_cadence(conversation_key)
            if self._planner_runs is not None
            else PlannerVoiceCadence(0, 0)
        )
        spontaneous_allowed = self._spontaneous_allowed(
            cadence,
            frequency=runtime.speech.spontaneous_frequency,
            preference_mode=preference_mode,
        )
        base_speech = speech_context or PlannerSpeechContext()
        speech_context = base_speech.model_copy(
            update={
                "preference_mode": preference_mode,
                "spontaneous_frequency": runtime.speech.spontaneous_frequency,
                "recent_spontaneous_turns": cadence.spontaneous_turns,
                "recent_spontaneous_voice_turns": cadence.spontaneous_voice_turns,
                "recent_spontaneous_voice_ratio": cadence.ratio,
                "spontaneous_allowed": spontaneous_allowed,
            }
        )
        return PlannerInput(
            conversation_key=conversation_key,
            scope_type=inbound.scope_type,
            origin=origin,
            trigger_message_id=inbound.message_id,
            bot_user_id=inbound.bot_user_id,
            current_sender_user_id=inbound.sender.user_id,
            current_group_id=inbound.group_id,
            messages=messages,
            current_message=current,
            reply_target_is_bot=(
                bool(inbound.reply_sender_user_id)
                and inbound.reply_sender_user_id == inbound.bot_user_id
            ),
            mentions_bot=inbound.mentions_bot,
            mentioned_user_ids=inbound.mentioned_user_ids,
            visual_input_present=visual_input_present,
            relationship_stage=relationship.stage if relationship is not None else None,
            current_time=current_time,
            necessity=necessity,
            available_tool_categories=available_tool_categories,
            plugin_signals=plugin_signals,
            speech=speech_context or PlannerSpeechContext(),
        )

    @staticmethod
    def _spontaneous_allowed(
        cadence: PlannerVoiceCadence,
        *,
        frequency: float,
        preference_mode: VoicePreferenceMode,
    ) -> bool:
        if preference_mode is VoicePreferenceMode.TEXT_ONLY or frequency <= 0:
            return False
        budget = ceil((cadence.spontaneous_turns + 1) * min(1.0, frequency))
        return cadence.spontaneous_voice_turns < budget

    @staticmethod
    def _default_preference_mode(default_mode: str) -> VoicePreferenceMode:
        if default_mode == "text":
            return VoicePreferenceMode.TEXT_ONLY
        if default_mode in {"voice", "text_and_voice"}:
            return VoicePreferenceMode.PREFER_VOICE
        return VoicePreferenceMode.AUTO

    @staticmethod
    def _planner_message(row: EventRecord) -> PlannerMessage:
        return PlannerMessage(
            message_id=row.platform_message_id,
            sender_user_id=row.sender_user_id,
            text=row.content[:4000],
            sender_is_bot=row.direction == "outbound",
            sent_at=row.occurred_at,
        )

    @staticmethod
    def _metrics(
        rows: tuple[EventRecord, ...],
        bot_user_id: str,
        now: datetime,
    ) -> _ConversationMetrics:
        human = [row for row in rows if row.sender_user_id != bot_user_id]
        bot = [row for row in rows if row.sender_user_id == bot_user_id]
        normalized_now = PlannerContextBuilder._aware_utc(now)
        intervals = [
            max(
                0.0,
                (
                    PlannerContextBuilder._aware_utc(right.occurred_at)
                    - PlannerContextBuilder._aware_utc(left.occurred_at)
                ).total_seconds(),
            )
            for left, right in pairwise(human)
        ]
        last_bot_index = max(
            (index for index, row in enumerate(rows) if row.sender_user_id == bot_user_id),
            default=-1,
        )
        pending = sum(1 for row in rows[last_bot_index + 1 :] if row.sender_user_id != bot_user_id)
        last_time = (
            PlannerContextBuilder._aware_utc(rows[-1].occurred_at) if rows else normalized_now
        )
        last_bot_time = PlannerContextBuilder._aware_utc(bot[-1].occurred_at) if bot else None
        return _ConversationMetrics(
            pending=pending,
            bot_count=len(bot),
            average_interval=sum(intervals) / len(intervals) if intervals else 60.0,
            idle=max(0.0, (normalized_now - last_time).total_seconds()),
            since_bot=(
                max(0.0, (normalized_now - last_bot_time).total_seconds())
                if last_bot_time is not None
                else None
            ),
            last_was_bot=bool(rows and rows[-1].sender_user_id == bot_user_id),
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
