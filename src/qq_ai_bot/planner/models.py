"""Strict, provider-neutral domain models for Planner decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.relationships import RelationshipStage
from qq_ai_bot.emoji.models import EmojiReplyPlan
from qq_ai_bot.speech.models import VoicePreferenceMode, VoiceReplyPlan


class _StrictPlannerModel(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannerDecision(StrEnum):
    """Whether the main Agent should answer this turn."""

    REPLY = "reply"
    SILENT = "silent"
    WAIT = "wait"


class DeliveryMode(StrEnum):
    """How a later reply sequence should present the final answer."""

    SINGLE = "single"
    NATURAL_MULTI = "natural_multi"
    STRUCTURED = "structured"
    CONCISE = "concise"
    DETAILED = "detailed"


class ToolMode(StrEnum):
    """A monotonic restriction over capabilities granted by the backend."""

    INHERIT = "inherit"
    NONE = "none"
    READ_ONLY = "read_only"


class ToolGroup(StrEnum):
    """Coarse capability groups that Planner may retain for this turn."""

    WEB = "web"
    MEMORY = "memory"
    ADMIN = "admin"
    CONFIG = "config"
    AUTOMATION = "automation"
    ONEBOT = "onebot"
    EMOJI = "emoji"
    SPEECH = "speech"
    PLUGIN = "plugin"


class ToolSelection(_StrictPlannerModel):
    """A monotonic tool mode plus dynamic backend-advertised scopes.

    ``groups`` remains an accepted legacy spelling for 2.0 Planner responses.
    New providers should emit ``scopes``.
    """

    mode: ToolMode = ToolMode.INHERIT
    scopes: tuple[str, ...] = ()
    groups: tuple[ToolGroup, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _merge_legacy_groups(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value.get("groups"):
            return value
        normalized = dict(value)
        scopes = [str(item) for item in normalized.get("scopes", ())]
        for group in normalized.get("groups", ()):
            group_id = group.value if isinstance(group, ToolGroup) else str(group)
            if group_id not in scopes:
                scopes.append(group_id)
        normalized["scopes"] = scopes
        normalized["groups"] = []
        return normalized

    @property
    def scope_ids(self) -> tuple[str, ...]:
        return self.scopes


class ToolScopeSummary(_StrictPlannerModel):
    """Compact Planner-visible scope metadata; never contains JSON Schemas."""

    scope_id: str = Field(min_length=1, max_length=128)
    parent: str | None = Field(default=None, max_length=128)
    display_name: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=300)
    tool_count: int = Field(ge=0, strict=True)
    provider_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


class PlannerReasonCode(StrEnum):
    """Stable, low-cardinality reasons suitable for metrics and audit records."""

    DIRECT_REQUEST = "direct_request"
    DIRECT_MENTION = "direct_mention"
    CONTINUATION = "continuation"
    USEFUL_CONTRIBUTION = "useful_contribution"
    EMOTIONAL_SUPPORT = "emotional_support"
    CASUAL_REACTION = "casual_reaction"
    LOW_RELEVANCE = "low_relevance"
    BOT_OVERACTIVE = "bot_overactive"
    CONVERSATION_TOO_FAST = "conversation_too_fast"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    WAIT_FOR_MORE_CONTEXT = "wait_for_more_context"
    PLANNER_FALLBACK = "planner_fallback"


class PlannerMessage(_StrictPlannerModel):
    """One message exposed to Planner with an explicit trust-boundary marker."""

    message_id: str
    sender_user_id: str
    text: str
    sender_is_bot: bool = False
    sent_at: datetime | None = None
    content_trust: Literal["external_untrusted"] = "external_untrusted"


class PlannerSignal(_StrictPlannerModel):
    """A bounded, non-authoritative relevance hint contributed by one plugin."""

    source_plugin_id: str = Field(min_length=1, max_length=128)
    score_delta: float = Field(ge=-10, le=10, strict=True)
    reason_code: str = Field(min_length=1, max_length=64)
    summary: str = Field(default="", max_length=300)
    confidence: float = Field(default=1.0, ge=0, le=1, strict=True)
    expires_at: datetime | None = None


class PlannerSpeechContext(_StrictPlannerModel):
    """Trusted speech availability without filesystem or model internals."""

    enabled: bool = Field(default=False, description="后端是否启用语音功能。")
    available: bool = Field(
        default=False,
        description="后端是否确认本轮能够合成并发送语音；这是可信运行时状态。",
    )
    default_profile: str = ""
    available_styles: tuple[str, ...] = ()
    available_languages: tuple[str, ...] = ()
    preference_mode: VoicePreferenceMode = VoicePreferenceMode.AUTO
    spontaneous_frequency: float = Field(default=0.15, ge=0, le=1, strict=True)
    recent_spontaneous_turns: int = Field(default=0, ge=0, strict=True)
    recent_spontaneous_voice_turns: int = Field(default=0, ge=0, strict=True)
    recent_spontaneous_voice_ratio: float = Field(default=0, ge=0, le=1, strict=True)
    spontaneous_allowed: bool = True


class ReplyNecessitySnapshot(_StrictPlannerModel):
    """Deterministic gate result captured before a Planner request."""

    score: int = Field(ge=0, le=100, strict=True)
    should_enter_planner: bool
    relevance_score: int = Field(ge=0, le=100, strict=True)
    content_score: int = Field(ge=-100, le=100, strict=True)
    pressure_score: int = Field(ge=0, le=100, strict=True)
    presence_penalty: int = Field(ge=0, le=100, strict=True)
    activity_penalty: int = Field(ge=0, le=100, strict=True)
    relationship_adjustment: int = Field(ge=-5, le=5, strict=True)
    plugin_adjustment: int = Field(default=0, ge=-15, le=15, strict=True)
    reasons: tuple[str, ...] = ()
    pending_message_count: int = Field(ge=0, le=100, strict=True)
    recent_bot_messages: int = Field(ge=0, strict=True)
    recent_total_messages: int = Field(ge=0, strict=True)
    average_human_interval_seconds: float = Field(ge=0, strict=True)
    idle_seconds: float = Field(ge=0, strict=True)


class PlannerInput(_StrictPlannerModel):
    """Trusted envelope plus explicitly untrusted conversation material."""

    conversation_key: str
    scope_type: ScopeType
    origin: TurnOrigin
    trigger_message_id: str
    bot_user_id: str
    current_sender_user_id: str
    current_group_id: str | None = None
    messages: tuple[PlannerMessage, ...] = Field(default=(), max_length=100)
    current_message: PlannerMessage
    reply_target_is_bot: bool = False
    mentions_bot: bool = False
    mentioned_user_ids: tuple[str, ...] = ()
    visual_input_present: bool = False
    relationship_stage: RelationshipStage | None = None
    current_time: datetime
    necessity: ReplyNecessitySnapshot
    available_tool_categories: tuple[str, ...] = ()
    available_tool_scopes: tuple[ToolScopeSummary, ...] = ()
    plugin_signals: tuple[PlannerSignal, ...] = ()
    speech: PlannerSpeechContext = PlannerSpeechContext()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_target_user_ids(self) -> tuple[str, ...]:
        """Return only QQ identities that occur in the trusted current envelope."""

        known: list[str] = []
        for user_id in (
            self.current_sender_user_id,
            *self.mentioned_user_ids,
            *(message.sender_user_id for message in self.messages),
        ):
            if not user_id or user_id == self.bot_user_id or user_id in known:
                continue
            known.append(user_id)
        return tuple(known)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known_message_ids(self) -> tuple[str, ...]:
        """Return message IDs from only the bounded current conversation input."""

        known: list[str] = []
        for message_id in (
            *(message.message_id for message in self.messages),
            self.current_message.message_id,
        ):
            if message_id and message_id not in known:
                known.append(message_id)
        return tuple(known)


class TurnPlan(_StrictPlannerModel):
    """Validated plan; it cannot grant tools or contain a final reply body."""

    schema_version: Literal[1] = 1
    decision: PlannerDecision
    intent: str = Field(default="", max_length=300)
    target_user_ids: tuple[str, ...] = Field(default=(), max_length=5)
    delivery_mode: DeliveryMode = DeliveryMode.SINGLE
    desired_messages: int = Field(default=1, ge=1, le=20, strict=True)
    reply_to_message_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "默认必须为 null。只有需要在多人对话中明确指向某条真实消息，或特意回到较早消息时"
            "才填写；正常回答当前消息不得使用。"
        ),
    )
    tool_selection: ToolSelection = ToolSelection()
    wait_seconds: float = Field(default=0, ge=0, le=300, strict=True)
    confidence: float = Field(ge=0, le=1, strict=True)
    reason_code: PlannerReasonCode
    planner_note: str = ""
    emoji: EmojiReplyPlan = EmojiReplyPlan()
    voice: VoiceReplyPlan = VoiceReplyPlan()

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_tool_mode(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "tool_mode" not in value:
            return value
        normalized = dict(value)
        legacy = normalized.pop("tool_mode")
        if "tool_selection" not in normalized:
            normalized["tool_selection"] = {
                "mode": legacy,
                # An empty scope list means all backend-approved scopes.  This
                # preserves the 2.0 ``tool_mode`` contract without fabricating
                # names that may not exist in a dynamic provider catalog.
                "scopes": [],
            }
        return normalized

    @property
    def tool_mode(self) -> ToolMode:
        """Source-compatible projection for 1.8 integrations."""

        return self.tool_selection.mode


class PlannedTurn(_StrictPlannerModel):
    """Planner result metadata passed to later orchestration without hidden reasoning."""

    plan: TurnPlan
    necessity: ReplyNecessitySnapshot
    planner_model: str
    planner_latency_seconds: float = Field(ge=0, strict=True)
    planner_used: bool
    fallback_used: bool
    turn_version: int = Field(ge=0, strict=True)
