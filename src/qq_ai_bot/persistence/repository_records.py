"""Immutable projections returned by persistence repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qq_ai_bot.domain.conversations import ConversationMode, ScopeType


@dataclass(frozen=True, slots=True)
class GroupSetting:
    """Domain projection of a group setting row."""

    group_id: str
    enabled: bool
    require_mention: bool
    conversation_mode: ConversationMode
    autonomous_enabled: bool = True
    name: str = ""


@dataclass(frozen=True, slots=True)
class PrivateUserSetting:
    """Domain projection of one private-chat access state."""

    user_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One permanent ledger event."""

    id: int
    bot_user_id: str
    platform_message_id: str
    scope_type: ScopeType
    sender_user_id: str
    direction: str
    content: str
    visual_summary: str
    segments: tuple[dict[str, Any], ...]
    occurred_at: datetime
    group_id: str | None = None
    private_peer_user_id: str | None = None
    reply_to_message_id: str | None = None
    origin: str = "user_message"
    automation_id: int | None = None
    automation_run_id: int | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    reply_sender_user_id: str | None = None
    event_kind: str = "message"
    source_plugin_id: str | None = None
    external_source: str | None = None
    external_event_key: str | None = None
    external_event_type: str | None = None
    external_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MediaAnalysisRecord:
    """A cached structured observation; it never contains source image bytes."""

    id: int
    source_event_id: int | None
    segment_index: int
    content_hash: str
    analysis_mode: str
    question_hash: str
    provider: str
    model: str
    prompt_version: str
    observation_json: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EmojiDescriptionRecord:
    """A persistent, reusable description of one stable QQ emoji identity."""

    id: int
    emoji_key: str
    analysis_mode: str
    question_hash: str
    provider: str
    model: str
    prompt_version: str
    description: str
    observation_json: str
    hit_count: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class RelationshipEventRecord:
    """One relationship change without duplicated chat content."""

    id: int
    user_id: str
    change_type: str
    affection_before: int
    affection_delta: int
    affection_after: int
    trust_before: int
    trust_delta: int
    trust_after: int
    reason_code: str
    confidence: float | None
    created_at: datetime
    source_event_id: int | None = None
    actor_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipJobRecord:
    """A claimed relationship job with bounded person-specific context."""

    job_id: int
    attempts: int
    user_id: str
    conversation_key: str
    trigger_event: EventRecord
    recent_events: tuple[EventRecord, ...]
