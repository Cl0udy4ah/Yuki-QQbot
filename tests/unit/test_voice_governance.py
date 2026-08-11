from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventRecord, PeopleRepository
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.repository import PlannerRepository, PlannerVoiceCadence
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.speech.models import (
    VoiceIntent,
    VoicePreferenceChange,
    VoicePreferenceDuration,
    VoicePreferenceMode,
    VoiceReplyPlan,
)
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
from qq_ai_bot.speech.preference_service import VoicePreferenceService
from qq_ai_bot.speech.reply_effect import VoiceReplyEffectService


def test_voice_name_mapping_does_not_change_the_text_reply() -> None:
    effects = VoiceReplyEffectService(
        MagicMock(),
        bot_display_name="Mika",
        bot_voice_name="みか",
    )
    response_text = "我是Mika，今天也在。"

    assert effects.spoken_text(response_text) == "我是みか，今天也在。"
    assert response_text == "我是Mika，今天也在。"


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


def test_emoji_cadence_groups_split_messages_into_reply_turns() -> None:
    now = datetime.now(UTC)
    inbound = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="in-1",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="你好",
        visual_summary="",
        segments=({"type": "text", "data": {"text": "你好"}},),
        occurred_at=now,
        private_peer_user_id="1001",
    )
    text_reply = replace(
        inbound,
        id=2,
        platform_message_id="out-1",
        sender_user_id="8000",
        direction="outbound",
        content="在呢",
        segments=({"type": "text", "data": {"text": "在呢"}},),
    )
    split_reply = replace(text_reply, id=3, platform_message_id="out-2", content="怎么啦")
    emoji_reply = replace(
        text_reply,
        id=5,
        platform_message_id="out-3",
        content="",
        segments=({"type": "image", "data": {"emoji_id": "emoji-1"}},),
    )
    cadence = PlannerContextBuilder._emoji_cadence(
        (inbound, text_reply, split_reply, replace(inbound, id=4), emoji_reply),
        "8000",
    )

    assert cadence.turns == 2
    assert cadence.emoji_turns == 1
    assert cadence.ratio == 0.5
    assert not PlannerContextBuilder._effect_frequency_allows(
        cadence.turns,
        cadence.emoji_turns,
        0.15,
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
    emoji = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.IMAGE,
                summary="一个开心的表情",
                emoji_id="emoji-1",
            ),
        )
    )

    assert ChatService._ledger_content(media_only) == ""
    assert ChatService._ledger_content(spoken) == "ゆきだよ。"
    assert ChatService._ledger_content(emoji) == ""
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

    assert ChatEventPromptRenderer.event_content(legacy, "current", "当前消息") == ""


def test_model_history_omits_transport_annotations_and_media_only_events() -> None:
    now = datetime.now(UTC)
    image = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="image",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="[表情：一个开心的表情]",
        visual_summary="",
        segments=({"type": "image", "data": {"summary": "一个开心的表情"}},),
        occurred_at=now,
        private_peer_user_id="1001",
    )
    contaminated_text = replace(
        image,
        id=2,
        platform_message_id="text",
        content="[21:10] 我会正常说话。",
        segments=({"type": "text", "data": {"text": "[21:10] 我会正常说话。"}},),
    )
    copied_media_description = replace(
        image,
        id=3,
        platform_message_id="copied-placeholder",
        content="[21:10] [表情：不应作为台词]",
        segments=({"type": "text", "data": {"text": "[21:10] [表情：不应作为台词]"}},),
    )
    leaked_identity = replace(
        contaminated_text,
        id=4,
        platform_message_id="leaked-identity",
        content=("[发送者:Yuki|QQ:8000|消息:old-output|时间:2026-08-05T15:39:05.884399] 看到了。"),
    )

    renderer = ChatEventPromptRenderer((image, contaminated_text))
    assert renderer.event_content(image, "current", "当前消息") == ""
    rendered = renderer.render_event(contaminated_text)
    assert rendered == "[发送者:Yuki|QQ:8000|消息:text] 我会正常说话。"
    assert (
        renderer.event_content(
            leaked_identity,
            "current",
            "当前消息",
        )
        == "看到了。"
    )
    assert (
        renderer.event_content(
            copied_media_description,
            "current",
            "当前消息",
        )
        == ""
    )
