from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventRecord, PeopleRepository
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.repository import PlannerRepository, PlannerVoiceCadence
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.speech.models import (
    VoiceIntent,
    VoicePreferenceChange,
    VoicePreferenceDuration,
    VoicePreferenceMode,
    VoiceReplyPlan,
)
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
from qq_ai_bot.speech.preference_service import VoicePreferenceService


@pytest.mark.asyncio
async def test_persistent_voice_preference_is_person_scoped_and_cascades(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    repository = VoicePreferenceRepository(database)
    service = VoicePreferenceService(repository)
    await people.observe(user_id="1001", nickname="测试用户")

    turn_only = VoiceReplyPlan(
        intent=VoiceIntent.EXPLICIT_OPT_OUT,
        preference_change=VoicePreferenceChange(
            mode=VoicePreferenceMode.TEXT_ONLY,
            duration=VoicePreferenceDuration.TURN,
        ),
    )
    assert (
        await service.apply(
            turn_only,
            user_id="1001",
            source_message_id="turn-only",
            origin=TurnOrigin.USER_MESSAGE,
        )
        is None
    )
    assert await repository.get("1001") is None

    persistent = turn_only.model_copy(
        update={
            "preference_change": VoicePreferenceChange(
                mode=VoicePreferenceMode.TEXT_ONLY,
                duration=VoicePreferenceDuration.PERSISTENT,
            )
        }
    )
    saved = await service.apply(
        persistent,
        user_id="1001",
        source_message_id="persistent",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert saved is not None
    assert saved.mode is VoicePreferenceMode.TEXT_ONLY
    assert saved.source_message_id == "persistent"

    await people.delete_person("1001")
    assert await repository.get("1001") is None


@pytest.mark.asyncio
async def test_planner_voice_cadence_counts_only_neutral_reply_turns(database: Database) -> None:
    repository = PlannerRepository(database)
    now = datetime(2026, 7, 29, tzinfo=UTC)

    for index, (intent, mode) in enumerate(
        (("neutral", "voice"), ("neutral", "text"), ("explicit_request", "voice")),
        start=1,
    ):
        row = await repository.begin(
            conversation_key="private:1001",
            trigger_message_id=str(index),
            scope_type="private",
            origin="user_message",
            sender_user_id="1001",
            group_id=None,
            necessity_score=100,
            necessity_reasons={},
            gate_decision="enter",
            planner_used=True,
            created_at=now,
        )
        await repository.finish(
            row.id,
            planner_decision="reply",
            reason_code="direct_request",
            delivery_mode="single",
            desired_messages=1,
            tool_mode="inherit",
            voice_mode=mode,
            voice_intent=intent,
            voice_tool_policy=("required" if intent == "explicit_request" else "forbidden"),
            confidence=1,
            latency_seconds=0,
            finished_at=now,
        )

    cadence = await repository.voice_cadence("private:1001")
    assert cadence == PlannerVoiceCadence(spontaneous_turns=2, spontaneous_voice_turns=1)
    assert cadence.ratio == 0.5


def test_spontaneous_frequency_is_a_deterministic_budget() -> None:
    allowed = PlannerContextBuilder._spontaneous_allowed

    assert allowed(
        PlannerVoiceCadence(spontaneous_turns=0, spontaneous_voice_turns=0),
        frequency=0.15,
        preference_mode=VoicePreferenceMode.AUTO,
    )
    assert not allowed(
        PlannerVoiceCadence(spontaneous_turns=1, spontaneous_voice_turns=1),
        frequency=0.15,
        preference_mode=VoicePreferenceMode.AUTO,
    )
    assert not allowed(
        PlannerVoiceCadence(spontaneous_turns=50, spontaneous_voice_turns=0),
        frequency=1,
        preference_mode=VoicePreferenceMode.TEXT_ONLY,
    )


def test_voice_ledger_separates_spoken_text_from_internal_metadata() -> None:
    technical_summary = "Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp"
    media_only = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.AUDIO,
                summary=technical_summary,
                voice_profile_id="roxy",
                voice_reference_key="happy",
                voice_language="jp",
            ),
        )
    )
    spoken = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.AUDIO,
                summary="语音消息",
                spoken_text="ゆきだよ。",
                voice_profile_id="roxy",
                voice_reference_key="happy",
                voice_language="jp",
            ),
        )
    )

    assert ChatService._ledger_content(media_only) == ""
    assert ChatService._ledger_content(spoken) == "ゆきだよ。"
    segment = ChatService._ledger_media_segment(spoken.media[0])
    assert segment["data"]["target_language"] == "jp"  # type: ignore[index]
    assert technical_summary not in ChatService._ledger_content(media_only)


def test_legacy_voice_metadata_is_hidden_from_model_history() -> None:
    now = datetime.now(UTC)
    legacy = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="legacy-voice",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="[语音：Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp]",
        visual_summary="",
        segments=(
            {
                "type": "text",
                "data": {"text": "[语音：Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp]"},
            },
        ),
        occurred_at=now,
        private_peer_user_id="1001",
    )

    assert ContextAssembler._history_event_content(legacy, "current", "当前消息") == ""
