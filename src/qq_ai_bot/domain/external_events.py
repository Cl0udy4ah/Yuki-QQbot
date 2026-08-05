"""Trusted envelopes for untrusted external events entering a real conversation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from yuki_plugin_sdk.models import JsonValue


class ConversationEventKind(StrEnum):
    MESSAGE = "message"
    EXTERNAL_EVENT = "external_event"


class ExternalActorType(StrEnum):
    PLUGIN = "plugin"
    EXTERNAL_SERVICE = "external_service"


@dataclass(frozen=True, slots=True)
class ExternalConversationEvent:
    source_plugin_id: str
    external_source: str
    event_key: str
    event_type: str
    target_type: str
    target_id: str
    bot_user_id: str
    occurred_at: datetime
    summary: str
    payload: Mapping[str, JsonValue]
