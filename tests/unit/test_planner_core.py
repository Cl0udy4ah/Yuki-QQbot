from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from qq_ai_bot.admin.models import (
    AgentRuntimeConfig,
    AutonomousRuntimeConfig,
    ContextRuntimeConfig,
    EmojiRuntimeConfig,
    LLMRuntimeConfig,
    PlannerRuntimeConfig,
    PluginRuntimeConfig,
    RelationshipRuntimeConfig,
    ReplyRuntimeConfig,
    RuntimeConfigSnapshot,
    SpeechRuntimeConfig,
    VisionRuntimeConfig,
    WebRuntimeConfig,
)
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.planner import (
    DeliveryMode,
    FakePlannerProvider,
    LLMPlannerProvider,
    PlannerDecision,
    PlannerInput,
    PlannerInterruptedError,
    PlannerMessage,
    PlannerObservability,
    PlannerReasonCode,
    PlannerResponseError,
    PlannerSignal,
    ReplyNecessityFeatures,
    ReplyNecessityScorer,
    ToolMode,
    TurnPlan,
    constrain_turn_plan,
)
from qq_ai_bot.planner.models import PlannerSpeechContext
from qq_ai_bot.planner.prompt import build_planner_messages
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.speech.models import (
    SpeechLanguageHint,
    VoiceAgentToolPolicy,
    VoiceIntent,
    VoiceMode,
    VoicePreferenceChange,
    VoicePreferenceDuration,
    VoicePreferenceMode,
    VoiceReplyPlan,
)


def _runtime() -> RuntimeConfigSnapshot:
    return RuntimeConfigSnapshot(
        autonomous=AutonomousRuntimeConfig(
            enabled=True,
            silence_seconds=8,
            confidence_threshold=0.85,
            cooldown_seconds=300,
            max_per_hour=3,
        ),
        planner=PlannerRuntimeConfig(
            enabled=True,
            model="",
            direct_enabled=True,
            group_enabled=True,
            temperature=0.1,
            max_output_tokens=512,
            timeout_seconds=20,
            confidence_threshold=0.65,
            reply_necessity_threshold=80,
            max_pending_messages=20,
            recent_presence_window_seconds=300,
            max_wait_seconds=60,
            interrupt_autonomous_on_new_message=True,
            record_runs=True,
        ),
        plugins=PluginRuntimeConfig(
            hook_timeout_seconds=3,
            max_prompt_fragment_characters=2000,
            max_prompt_characters_per_plugin=4000,
            max_total_prompt_characters=8000,
        ),
        context=ContextRuntimeConfig(local_event_limit=30, related_people_limit=5),
        reply=ReplyRuntimeConfig(
            daily_split_enabled=True,
            daily_split_max_characters=80,
            daily_split_max_messages=5,
            delay_min_seconds=3,
            delay_max_seconds=5,
            max_qq_message_chars=1500,
            cancel_on_new_message=True,
            plan_hard_max_messages=10,
        ),
        llm=LLMRuntimeConfig(
            model="main-model",
            timeout_seconds=30,
            max_retries=1,
            temperature=0.7,
            max_output_tokens=2048,
            thinking_enabled=True,
        ),
        agent=AgentRuntimeConfig(
            max_tool_calls=5,
            max_model_requests=6,
            tool_result_max_characters=32_000,
        ),
        web=WebRuntimeConfig(
            search_max_results=5,
            extract_max_results=3,
            max_calls_per_turn=3,
            tool_result_max_characters=20_000,
            source_retention_days=7,
            source_max_runs_per_conversation=10,
        ),
        relationship=RelationshipRuntimeConfig(
            confidence_threshold=0.7,
            max_auto_delta=5,
            daily_positive_cap=10,
            daily_negative_cap=-10,
            conflict_preference_min_gap=10,
            initial_affection=50,
            initial_trust=50,
        ),
        vision=VisionRuntimeConfig(
            max_images_per_turn=10,
            max_frames_per_turn=10,
            gif_max_frames=8,
            thinking_enabled=False,
            thinking_budget=0,
            low_confidence_retry_threshold=0.5,
            per_user_requests_per_minute=10,
            per_group_requests_per_minute=30,
            analysis_retention_days=7,
        ),
        emoji=EmojiRuntimeConfig(
            enabled=True,
            collection_enabled=True,
            collection_mode="likely",
            collect_private=True,
            collect_group=True,
            auto_adopt_enabled=True,
            auto_adopt_min_confidence=0.78,
            pool_capacity=None,
            replacement_mode="score",
            selector_enabled=True,
            selector_candidate_count=6,
            max_effects_per_reply=1,
            near_duplicate_enabled=True,
            near_duplicate_distance=6,
            same_emoji_cooldown_seconds=300,
            scope_repeat_cooldown_seconds=60,
            cache_retention_days=30,
            worker_batch_size=10,
            worker_poll_seconds=2,
            worker_lease_seconds=120,
            worker_max_attempts=3,
            worker_retry_delay_seconds=30,
            analysis_version="emoji-v1",
        ),
        speech=SpeechRuntimeConfig(
            enabled=False,
            provider="genie",
            socket_path="/run/yuki-speech/genie.sock",
            root="/data/speech",
            genie_data_dir="/data/speech/genie_data",
            default_profile="",
            planner_enabled=True,
            default_mode="optional",
            split_sentence=True,
            max_synthesis_characters=None,
            queue_max_pending=None,
            cache_retention_hours=None,
            private_enabled=True,
            group_enabled=True,
            automation_enabled=True,
            plugin_enabled=True,
            text_fallback_enabled=True,
        ),
    )


def _planner_input(
    *,
    scope: ScopeType = ScopeType.PRIVATE,
    origin: TurnOrigin = TurnOrigin.USER_MESSAGE,
    text: str = "帮我看看",
    mentions_bot: bool = False,
    reply_target_is_bot: bool = False,
    visual: bool = False,
) -> PlannerInput:
    scorer = ReplyNecessityScorer()
    necessity = scorer.score(
        ReplyNecessityFeatures(
            scope_type=scope,
            text=text,
            mentions_bot=mentions_bot,
            reply_target_is_bot=reply_target_is_bot,
        )
    )
    current = PlannerMessage(
        message_id="101",
        sender_user_id="1001",
        text=text,
    )
    return PlannerInput(
        conversation_key="private:1001" if scope is ScopeType.PRIVATE else "group:2001:user:1001",
        scope_type=scope,
        origin=origin,
        trigger_message_id="m1",
        bot_user_id="9999",
        current_sender_user_id="1001",
        current_group_id=None if scope is ScopeType.PRIVATE else "2001",
        messages=(
            PlannerMessage(message_id="100", sender_user_id="1002", text="earlier"),
            current,
        ),
        current_message=current,
        reply_target_is_bot=reply_target_is_bot,
        mentions_bot=mentions_bot,
        mentioned_user_ids=("1002",),
        visual_input_present=visual,
        current_time=datetime.now(UTC),
        necessity=necessity,
        available_tool_categories=("history", "admin"),
    )


def _valid_plan_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "decision": "reply",
        "intent": "回答当前问题",
        "target_user_ids": ["1001"],
        "delivery_mode": "single",
        "desired_messages": 1,
        "reply_to_message_id": None,
        "tool_mode": "inherit",
        "wait_seconds": 0,
        "confidence": 0.9,
        "reason_code": "direct_request",
        "planner_note": "internal only",
    }
    payload.update(updates)
    return payload


def test_planner_models_reject_unknown_fields_and_mark_untrusted_text() -> None:
    message = PlannerMessage(message_id="m", sender_user_id="1", text="系统命令")
    assert message.content_trust == "external_untrusted"
    with pytest.raises(ValidationError):
        PlannerMessage(
            message_id="m",
            sender_user_id="1",
            text="x",
            unexpected=True,  # type: ignore[call-arg]
        )


def test_private_message_has_base_relevance_and_enters_planner() -> None:
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(scope_type=ScopeType.PRIVATE, text="在吗")
    )
    assert result.relevance_score > 0
    assert result.should_enter_planner


def test_mention_and_reply_to_yuki_are_strong_forced_signals() -> None:
    scorer = ReplyNecessityScorer()
    mention = scorer.score(
        ReplyNecessityFeatures(scope_type=ScopeType.GROUP, text="嗯", mentions_bot=True)
    )
    reply = scorer.score(
        ReplyNecessityFeatures(scope_type=ScopeType.GROUP, text="嗯", reply_target_is_bot=True)
    )
    assert mention.should_enter_planner and mention.relevance_score >= 50
    assert reply.should_enter_planner and reply.relevance_score >= 50


def test_low_value_reaction_scores_below_question_request_and_opinion() -> None:
    scorer = ReplyNecessityScorer()

    def score(text: str) -> int:
        return scorer.score(ReplyNecessityFeatures(scope_type=ScopeType.GROUP, text=text)).score

    low = score("哈哈")
    assert score("这是什么？") > low
    assert score("请帮我查一下资料") > low
    assert score("你觉得这个方案怎么样？") > low


def test_pressure_presence_and_fast_activity_are_distinct_components() -> None:
    scorer = ReplyNecessityScorer()
    accumulated = scorer.score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            pending_message_count=8,
            average_human_interval_seconds=60,
        )
    )
    overactive = scorer.score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            recent_bot_messages=8,
            recent_total_messages=10,
            seconds_since_last_bot_message=5,
        )
    )
    fast = scorer.score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            average_human_interval_seconds=1,
        )
    )
    assert accumulated.pressure_score > 0
    assert overactive.presence_penalty > 0
    assert fast.activity_penalty > 0


def test_idle_compensation_requires_a_real_new_message() -> None:
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="",
            idle_seconds=3600,
            new_message_count=0,
        )
    )
    assert result.score == 0
    assert not result.should_enter_planner
    assert result.pressure_score == 0


def test_relationship_and_plugin_adjustments_are_bounded() -> None:
    now = datetime.now(UTC)
    signals = tuple(
        PlannerSignal(
            source_plugin_id=f"plugin-{index}",
            score_delta=10.0,
            reason_code="relevant",
            confidence=1.0,
            expires_at=now + timedelta(minutes=1),
        )
        for index in range(3)
    )
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="普通内容",
            relationship_adjustment=999,
            plugin_signals=signals,
            now=now,
        )
    )
    assert result.relationship_adjustment == 5
    assert result.plugin_adjustment == 15


def test_same_plugin_and_expired_signals_cannot_bypass_caps() -> None:
    now = datetime.now(UTC)
    signals = (
        PlannerSignal(
            source_plugin_id="same",
            score_delta=10.0,
            reason_code="one",
            confidence=1.0,
        ),
        PlannerSignal(
            source_plugin_id="same",
            score_delta=10.0,
            reason_code="two",
            confidence=1.0,
        ),
        PlannerSignal(
            source_plugin_id="expired",
            score_delta=10.0,
            reason_code="old",
            confidence=1.0,
            expires_at=now - timedelta(seconds=1),
        ),
    )
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="普通内容",
            plugin_signals=signals,
            now=now,
        )
    )
    assert result.plugin_adjustment == 10


def test_prompt_contains_only_planner_contract_and_explicit_untrusted_envelope() -> None:
    messages = build_planner_messages(
        _planner_input(),
        preferred_messages=4,
        hard_max_messages=20,
    )
    assert len(messages) == 2
    assert "只负责生成本轮计划" in (messages[0].content or "")
    payload = json.loads(messages[1].content or "")
    assert payload["current_message"]["text"] == "帮我看看"
    assert "content_trust" not in payload["current_message"]
    assert "<external_untrusted_planner_input>" not in (messages[1].content or "")
    assert "desired_messages 设为 4" not in (messages[0].content or "")


def test_planner_applies_hot_natural_multi_target_without_affecting_structure() -> None:
    runtime = _runtime()
    runtime = replace(
        runtime,
        planner=replace(runtime.planner, preferred_messages=4),
        reply=replace(runtime.reply, plan_hard_max_messages=20),
    )
    planner_input = _planner_input(text="今天过得怎么样")
    natural = PlannerService._constrain_business_rules(
        TurnPlan(**_valid_plan_payload(delivery_mode="natural_multi", desired_messages=1)),
        planner_input,
        runtime,
        administrator_request=False,
    )
    structured = PlannerService._constrain_business_rules(
        TurnPlan(**_valid_plan_payload(delivery_mode="structured", desired_messages=7)),
        planner_input,
        runtime,
        administrator_request=False,
    )
    explicit = PlannerService._constrain_business_rules(
        TurnPlan(**_valid_plan_payload(delivery_mode="single", desired_messages=1)),
        _planner_input(text="你尝试多发几条消息"),
        runtime,
        administrator_request=False,
    )

    assert natural.delivery_mode is DeliveryMode.NATURAL_MULTI
    assert natural.desired_messages == 4
    assert structured.delivery_mode is DeliveryMode.STRUCTURED
    assert structured.desired_messages == 7
    assert explicit.delivery_mode is DeliveryMode.NATURAL_MULTI
    assert explicit.desired_messages == 4


def test_planner_voice_language_is_bounded_by_the_active_profile() -> None:
    runtime = replace(_runtime(), speech=replace(_runtime().speech, enabled=True))
    planner_input = _planner_input().model_copy(
        update={
            "speech": PlannerSpeechContext(
                enabled=True,
                available=True,
                default_profile="roxy",
                available_styles=("neutral", "gentle"),
                available_languages=("zh", "jp"),
            )
        }
    )
    japanese = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                voice=VoiceReplyPlan(
                    mode=VoiceMode.OPTIONAL,
                    style_hint="gentle",
                    language=SpeechLanguageHint.JP,
                )
            )
        ),
        planner_input,
        runtime,
        administrator_request=False,
    )
    unavailable = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                voice=VoiceReplyPlan(
                    mode=VoiceMode.OPTIONAL,
                    style_hint="unknown",
                    language=SpeechLanguageHint.JP,
                )
            )
        ),
        planner_input.model_copy(
            update={
                "speech": planner_input.speech.model_copy(update={"available_languages": ("zh",)})
            }
        ),
        runtime,
        administrator_request=False,
    )

    assert japanese.voice.language.value == "jp"
    assert japanese.voice.style_hint == "gentle"
    assert unavailable.voice.language.value == "auto"
    assert unavailable.voice.style_hint == ""


def test_planner_semantic_voice_intent_is_enforced_without_keyword_matching() -> None:
    runtime = replace(
        _runtime(),
        speech=replace(_runtime().speech, enabled=True, default_mode="optional"),
    )
    speech = PlannerSpeechContext(
        enabled=True,
        available=True,
        default_profile="roxy",
        available_styles=("neutral", "gentle"),
        available_languages=("zh", "jp"),
    )

    def constrained(voice: VoiceReplyPlan, *, spontaneous_allowed: bool = True) -> TurnPlan:
        planner_input = _planner_input(text="任意自然语言，不由后端匹配关键词").model_copy(
            update={
                "speech": speech.model_copy(update={"spontaneous_allowed": spontaneous_allowed})
            }
        )
        return PlannerService._constrain_business_rules(
            TurnPlan(**_valid_plan_payload(voice=voice)),
            planner_input,
            runtime,
            administrator_request=False,
        )

    explicit = constrained(
        VoiceReplyPlan(
            mode=VoiceMode.TEXT,
            intent=VoiceIntent.EXPLICIT_REQUEST,
            agent_tool=VoiceAgentToolPolicy.REQUIRED,
        )
    )
    opt_out = constrained(
        VoiceReplyPlan(
            mode=VoiceMode.TEXT_AND_VOICE,
            intent=VoiceIntent.EXPLICIT_OPT_OUT,
            preference_change=VoicePreferenceChange(
                mode=VoicePreferenceMode.TEXT_ONLY,
                duration=VoicePreferenceDuration.PERSISTENT,
            ),
        )
    )
    neutral = constrained(VoiceReplyPlan(mode=VoiceMode.OPTIONAL))
    cadence_blocked = constrained(
        VoiceReplyPlan(mode=VoiceMode.VOICE),
        spontaneous_allowed=False,
    )

    assert explicit.voice.mode is VoiceMode.VOICE
    assert explicit.voice.agent_tool is VoiceAgentToolPolicy.REQUIRED
    assert opt_out.voice.mode is VoiceMode.TEXT
    assert opt_out.voice.preference_change is not None
    assert neutral.voice.mode is VoiceMode.VOICE
    assert neutral.voice.agent_tool is VoiceAgentToolPolicy.FORBIDDEN
    assert cadence_blocked.voice.mode is VoiceMode.TEXT


def test_unavailable_speech_cannot_smuggle_a_neutral_persistent_preference() -> None:
    planner_input = _planner_input().model_copy(
        update={
            "speech": PlannerSpeechContext(enabled=False, available=False),
        }
    )
    model_plan = TurnPlan(
        **_valid_plan_payload(
            voice=VoiceReplyPlan(
                mode=VoiceMode.VOICE,
                preference_change=VoicePreferenceChange(
                    mode=VoicePreferenceMode.PREFER_VOICE,
                    duration=VoicePreferenceDuration.PERSISTENT,
                ),
            )
        )
    )

    constrained = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        _runtime(),
        administrator_request=False,
    )

    assert constrained.voice.mode is VoiceMode.TEXT
    assert constrained.voice.agent_tool is VoiceAgentToolPolicy.FORBIDDEN
    assert constrained.voice.preference_change is None


def test_agent_speech_runtime_policy_contains_no_internal_transport_details() -> None:
    source = inspect.getsource(PromptComposer)
    assert "/run/yuki-speech" not in source
    assert "8080" not in source and "6099" not in source


@pytest.mark.parametrize(
    "planner_input",
    (
        _planner_input(scope=ScopeType.PRIVATE, text="随便聊聊"),
        _planner_input(scope=ScopeType.GROUP, text="在吗", mentions_bot=True),
        _planner_input(scope=ScopeType.GROUP, text="接着说", reply_target_is_bot=True),
    ),
)
def test_explicit_turns_cannot_be_silenced_or_delayed_by_planner(
    planner_input: PlannerInput,
) -> None:
    runtime = _runtime()
    model_plan = TurnPlan(
        **_valid_plan_payload(
            decision="silent",
            wait_seconds=30,
            reason_code="low_relevance",
        )
    )
    constrained = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        runtime,
        administrator_request=False,
    )

    assert constrained.decision is PlannerDecision.REPLY
    assert constrained.wait_seconds == 0


def test_plan_validation_rejects_limits_and_unknown_targets_without_clamping() -> None:
    planner_input = _planner_input(visual=True)
    payload = _valid_plan_payload(
        decision="wait",
        target_user_ids=["unknown", "1002", "1002", "1001"],
        reply_to_message_id="100",
        desired_messages=99,
        wait_seconds=250,
        tool_mode="inherit",
    )
    with pytest.raises(PlannerResponseError):
        constrain_turn_plan(
            payload,
            planner_input,
            hard_max_messages=4,
            max_wait_seconds=30,
        )

    with pytest.raises(PlannerResponseError):
        constrain_turn_plan(
            _valid_plan_payload(reply_to_message_id="outside-current-context"),
            _planner_input(scope=ScopeType.GROUP),
        )


def test_plan_parser_rejects_unknown_fields_and_permission_modes() -> None:
    planner_input = _planner_input()
    with pytest.raises(PlannerResponseError):
        constrain_turn_plan(_valid_plan_payload(root=True), planner_input)
    with pytest.raises(PlannerResponseError):
        constrain_turn_plan(_valid_plan_payload(tool_mode="write_all"), planner_input)


@pytest.mark.asyncio
async def test_llm_planner_is_tool_free_non_thinking_and_uses_separate_model() -> None:
    payload = _valid_plan_payload()
    llm = FakeLLMProvider(lambda _request: json.dumps(payload))
    provider = LLMPlannerProvider(llm, model="planner-model")
    plan = await provider.plan(_planner_input(), runtime=_runtime())
    request = llm.requests[0]
    assert plan.decision is PlannerDecision.REPLY
    assert request.model == "planner-model"
    assert request.temperature == 0.1
    assert request.max_output_tokens == 512
    assert request.thinking_enabled is False
    assert request.tools == ()
    assert request.tool_choice is None


@pytest.mark.asyncio
async def test_runtime_planner_limits_reject_invalid_plan_without_clamping() -> None:
    payload = _valid_plan_payload(
        decision="wait",
        desired_messages=9,
        wait_seconds=50,
    )
    llm = FakeLLMProvider(lambda _request: json.dumps(payload))
    runtime = _runtime()
    runtime = replace(
        runtime,
        planner=replace(
            runtime.planner,
            model="runtime-planner",
            temperature=0.25,
            max_output_tokens=333,
            timeout_seconds=4,
            max_wait_seconds=12,
        ),
        reply=replace(runtime.reply, plan_hard_max_messages=3),
    )
    provider = LLMPlannerProvider(
        llm,
        model="constructor-fallback",
        temperature=0.9,
        max_output_tokens=999,
        timeout_seconds=30,
        hard_max_messages=8,
        max_wait_seconds=40,
        fallback_on_error=False,
    )
    with pytest.raises(PlannerResponseError):
        await provider.plan(_planner_input(), runtime=runtime)
    request = llm.requests[0]
    assert request.model == "constructor-fallback"
    assert request.temperature == 0.25
    assert request.max_output_tokens == 333


@pytest.mark.asyncio
async def test_invalid_planner_json_is_not_retried_and_falls_back_safely() -> None:
    llm = FakeLLMProvider(lambda _request: "not-json")
    provider = LLMPlannerProvider(llm)
    plan = await provider.plan(_planner_input(), runtime=_runtime())
    assert len(llm.requests) == 1
    assert plan.decision is PlannerDecision.REPLY
    assert plan.reason_code is PlannerReasonCode.PLANNER_FALLBACK


@pytest.mark.asyncio
async def test_admitted_autonomous_group_failure_falls_back_to_reply() -> None:
    llm = FakeLLMProvider(lambda _request: "not-json")
    provider = LLMPlannerProvider(llm)
    planner_input = _planner_input(
        scope=ScopeType.GROUP,
        origin=TurnOrigin.AUTONOMOUS_GROUP,
        text="Yuki，你觉得呢？",
    )
    planner_input = planner_input.model_copy(
        update={
            "necessity": planner_input.necessity.model_copy(update={"should_enter_planner": True})
        }
    )
    plan = await provider.plan(planner_input, runtime=_runtime())
    assert plan.decision is PlannerDecision.REPLY
    assert plan.tool_mode is ToolMode.INHERIT


@pytest.mark.asyncio
async def test_cancellation_event_interrupts_an_active_llm_planner() -> None:
    llm = FakeLLMProvider(lambda _request: json.dumps(_valid_plan_payload()), delay_seconds=5)
    provider = LLMPlannerProvider(llm, timeout_seconds=10)
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        provider.plan(_planner_input(), runtime=_runtime(), cancellation=cancellation)
    )
    for _ in range(100):
        if llm.requests:
            break
        await asyncio.sleep(0.001)
    cancellation.set()
    with pytest.raises(PlannerInterruptedError):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_fake_planner_obeys_the_same_cancellation_event() -> None:
    provider = FakePlannerProvider(delay_seconds=5)
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        provider.plan(_planner_input(), runtime=_runtime(), cancellation=cancellation)
    )
    await asyncio.sleep(0)
    cancellation.set()
    with pytest.raises(PlannerInterruptedError):
        await asyncio.wait_for(task, timeout=1)


def test_observability_tracks_active_fallback_and_hashes_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = PlannerObservability()
    planner_input = _planner_input()
    caplog.set_level(logging.INFO, logger="qq_ai_bot.planner.observability")
    token = metrics.request_started(
        conversation_key=planner_input.conversation_key,
        sender_user_id=planner_input.current_sender_user_id,
        group_id=planner_input.current_group_id,
    )
    assert metrics.snapshot().active_requests == 1
    plan = TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="reply",
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        tool_mode=ToolMode.NONE,
        wait_seconds=0.0,
        confidence=0.0,
        reason_code=PlannerReasonCode.PLANNER_FALLBACK,
    )
    metrics.request_finished(token, plan=plan, latency_seconds=0.2, fallback=True)
    snapshot = metrics.snapshot()
    assert snapshot.active_requests == 0
    assert snapshot.total_requests == 1
    assert snapshot.successful_plans == 1
    assert snapshot.fallback_plans == 1
    assert snapshot.last_decision is PlannerDecision.REPLY
    assert "private:1001" not in caplog.text
    assert "sender_user_id=1001" not in caplog.text
