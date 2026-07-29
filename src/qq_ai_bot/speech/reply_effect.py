"""Resolve Planner voice intent into the existing outbound media pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage, OutboundMedia, OutboundMessage
from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from qq_ai_bot.services.turn_coordinator import TurnSupersededError, TurnToken
from qq_ai_bot.speech.genie_client import GenieWorkerFailure, GenieWorkerUnavailable
from qq_ai_bot.speech.models import VoiceMode
from qq_ai_bot.speech.provider import SpeechSynthesisRequest
from qq_ai_bot.speech.service import (
    SpeechQueueFullError,
    SpeechService,
    SpeechUnavailableError,
)
from yuki_plugin_sdk.events import EventName


@dataclass(frozen=True, slots=True)
class VoiceReplyEffect:
    generation_id: int
    profile_id: str
    reference_key: str
    relative_path: str
    duration_milliseconds: int
    mode: VoiceMode


@dataclass(frozen=True, slots=True)
class PendingVoiceReplyEffect:
    """A path-free voice request queued by an Agent tool or plugin."""

    profile_id: str = ""
    style_hint: str = ""
    language_hint: str = "auto"
    mode: VoiceMode = VoiceMode.OPTIONAL
    source: str = "plugin"


@dataclass(frozen=True, slots=True)
class PreparedVoiceReply:
    effect: VoiceReplyEffect
    message: OutboundMessage
    suppress_text: bool


class VoiceReplyEffectService:
    def __init__(
        self,
        speech: SpeechService,
        *,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._speech = speech
        self._event_publisher = event_publisher

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    async def prepare(
        self,
        *,
        inbound: InboundMessage,
        response_text: str,
        runtime: RuntimeConfigSnapshot,
        token: TurnToken,
        mode: VoiceMode,
        style_hint: str,
        language_hint: str = "auto",
        profile_id: str = "",
    ) -> PreparedVoiceReply | None:
        if mode is VoiceMode.TEXT:
            return None
        scope_enabled = (
            runtime.speech.private_enabled
            if inbound.group_id is None
            else runtime.speech.group_enabled
        )
        if not scope_enabled:
            return None
        cancellation = asyncio.Event()
        try:
            generated = await self._speech.synthesize(
                SpeechSynthesisRequest(
                    request_id=str(uuid4()),
                    profile_id=profile_id or runtime.speech.default_profile,
                    style_hint=style_hint,
                    text=response_text,
                    split_sentence=runtime.speech.split_sentence,
                    conversation_key=token.conversation_key,
                    trigger_event_id=None,
                    turn_token=token,
                    language_hint=language_hint,
                ),
                runtime=runtime.speech,
                cancellation=cancellation,
            )
            path = self._speech.audio_path(generated)
        except (
            ValueError,
            LookupError,
            SpeechUnavailableError,
            SpeechQueueFullError,
            GenieWorkerUnavailable,
            GenieWorkerFailure,
            TurnSupersededError,
            OSError,
        ):
            return None
        effect = VoiceReplyEffect(
            generation_id=generated.generation_id,
            profile_id=generated.profile_id,
            reference_key=generated.reference_key,
            relative_path=generated.relative_path,
            duration_milliseconds=generated.duration_milliseconds,
            mode=mode,
        )
        voice_only = mode in {VoiceMode.VOICE, VoiceMode.OPTIONAL}
        return PreparedVoiceReply(
            effect=effect,
            message=OutboundMessage(
                media=(
                    OutboundMedia(
                        kind=AttachmentKind.AUDIO,
                        mime_type="audio/wav",
                        summary="语音消息",
                        local_path=str(path),
                        spoken_text=response_text if voice_only else "",
                        generation_id=generated.generation_id,
                        voice_profile_id=generated.profile_id,
                        voice_reference_key=generated.reference_key,
                        voice_language=generated.target_language,
                        duration_milliseconds=generated.duration_milliseconds,
                    ),
                )
            ),
            suppress_text=voice_only,
        )

    async def record_success(self, message: OutboundMessage) -> None:
        for media in message.media:
            if media.kind is AttachmentKind.AUDIO and media.generation_id is not None:
                await self._speech.mark_sent(media.generation_id)

    async def record_failure(self, message: OutboundMessage) -> None:
        for media in message.media:
            if media.kind is AttachmentKind.AUDIO and media.generation_id is not None:
                await publish_notification(
                    self._event_publisher,
                    EventName.SPEECH_SEND_FAILED,
                    {"generation_id": media.generation_id},
                )
