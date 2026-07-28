"""Unified local speech synthesis service and Genie provider implementation."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from qq_ai_bot.admin.models import SpeechRuntimeConfig
from qq_ai_bot.planner.models import PlannerSpeechContext
from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator, TurnSupersededError
from qq_ai_bot.speech.cache import GENIE_TTS_VERSION, SpeechCache, speech_cache_key
from qq_ai_bot.speech.genie_client import (
    GenieWorkerClient,
    GenieWorkerFailure,
    GenieWorkerUnavailable,
)
from qq_ai_bot.speech.models import VoiceProfile
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.provider import (
    SpeechProviderHealth,
    SpeechSynthesisRequest,
    SynthesizedSpeech,
    TTSProvider,
)
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository
from qq_ai_bot.speech.style_resolver import StyleResolver
from qq_ai_bot.speech.text_normalizer import normalize_speech_text
from yuki_plugin_sdk.events import EventName


class SpeechUnavailableError(RuntimeError):
    pass


class SpeechQueueFullError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechMetrics:
    queue_depth: int
    last_generation_at: datetime | None
    last_generation_latency_seconds: float | None
    last_error_category: str | None


class GenieTTSProvider(TTSProvider):
    """Persisted provider backed only by the local Genie Worker IPC client."""

    def __init__(
        self,
        *,
        client: GenieWorkerClient,
        profiles: VoiceProfileRepository,
        generations: SpeechGenerationRepository,
        cache: SpeechCache,
        paths: SpeechPathPolicy,
        styles: StyleResolver | None = None,
    ) -> None:
        self._client = client
        self._profiles = profiles
        self._generations = generations
        self._cache = cache
        self._paths = paths
        self._styles = styles or StyleResolver()

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> SynthesizedSpeech:
        profile = await self._profile(request.profile_id)
        reference = self._styles.resolve(profile, request.style_hint)
        key = speech_cache_key(
            profile=profile,
            reference=reference,
            normalized_text=request.text,
            split_sentence=request.split_sentence,
        )
        generation = await self._generations.create(
            request_id=request.request_id,
            conversation_key_hash=_hash(request.conversation_key),
            trigger_event_id=request.trigger_event_id,
            profile_id=profile.profile_id,
            reference_id=reference.id,
            engine_version=GENIE_TTS_VERSION,
            text_hash=_hash(request.text),
            normalized_text_hash=_hash(request.text),
            character_count=len(request.text),
            cache_key=key,
            expires_at=None,
        )
        cached = await self._cache.find(key)
        if cached is not None:
            if (
                cached.sample_rate is None
                or cached.channels is None
                or cached.duration_milliseconds is None
            ):
                raise RuntimeError("successful speech cache metadata is incomplete")
            complete = await self._generations.complete(
                generation.id,
                output_relative_path=cached.output_relative_path,
                sample_rate=cached.sample_rate,
                channels=cached.channels,
                duration_milliseconds=cached.duration_milliseconds,
            )
            return SynthesizedSpeech(
                complete.id,
                profile.profile_id,
                reference.reference_key,
                complete.output_relative_path,
                complete.output_format,
                cached.sample_rate,
                cached.channels,
                cached.duration_milliseconds,
                True,
            )
        await self._generations.set_generating(generation.id)
        output = f"cache/{request.request_id}.wav"
        try:
            response = await self._client.synthesize(
                request_id=request.request_id,
                profile=profile,
                reference=reference,
                text=request.text,
                split_sentence=request.split_sentence,
                output_relative_path=output,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            await self._generations.mark_cancelled(generation.id)
            raise
        except GenieWorkerFailure as exc:
            await self._generations.mark_failed(generation.id, exc.code.value)
            raise
        except GenieWorkerUnavailable:
            await self._generations.mark_failed(generation.id, "worker_unavailable")
            raise
        if (
            response.output_relative_path is None
            or response.sample_rate is None
            or response.channels is None
            or response.duration_milliseconds is None
        ):
            await self._generations.mark_failed(generation.id, "invalid_worker_response")
            raise GenieWorkerUnavailable("local Genie-TTS worker returned incomplete metadata")
        self._paths.resolve(response.output_relative_path, must_exist=True)
        complete = await self._generations.complete(
            generation.id,
            output_relative_path=response.output_relative_path,
            sample_rate=response.sample_rate,
            channels=response.channels,
            duration_milliseconds=response.duration_milliseconds,
        )
        return SynthesizedSpeech(
            complete.id,
            profile.profile_id,
            reference.reference_key,
            complete.output_relative_path,
            complete.output_format,
            response.sample_rate,
            response.channels,
            response.duration_milliseconds,
            False,
        )

    async def health(self) -> SpeechProviderHealth:
        return await self._client.health()

    async def close(self) -> None:
        await self._client.close()

    async def _profile(self, profile_id: str) -> VoiceProfile:
        profile = (
            await self._profiles.get_profile(profile_id)
            if profile_id
            else await self._profiles.get_default()
        )
        if profile is None or not profile.enabled:
            raise SpeechUnavailableError("no enabled voice profile is available")
        return profile


class SpeechService:
    """Apply runtime policy and cancellation before calling a generic provider."""

    def __init__(
        self,
        *,
        provider: TTSProvider,
        generations: SpeechGenerationRepository,
        cache: SpeechCache,
        paths: SpeechPathPolicy,
        profiles: VoiceProfileRepository,
        turns: ConversationTurnCoordinator | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._provider = provider
        self._generations = generations
        self._cache = cache
        self._paths = paths
        self._profiles = profiles
        self._turns = turns
        self._event_publisher = event_publisher
        self._last_generation_at: datetime | None = None
        self._last_generation_latency_seconds: float | None = None
        self._last_error_category: str | None = None

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        runtime: SpeechRuntimeConfig,
        cancellation: asyncio.Event | None = None,
    ) -> SynthesizedSpeech:
        if not runtime.enabled:
            raise SpeechUnavailableError("local speech is disabled")
        text = normalize_speech_text(request.text)
        if not text:
            raise ValueError("reply contains no speakable text")
        if (
            runtime.max_synthesis_characters is not None
            and len(text) > runtime.max_synthesis_characters
        ):
            raise ValueError("speech text exceeds the configured synthesis limit")
        if runtime.queue_max_pending is not None:
            depth = await self._generations.queue_depth()
            if depth >= runtime.queue_max_pending:
                raise SpeechQueueFullError("speech queue is full")
        normalized = SpeechSynthesisRequest(
            request_id=request.request_id,
            profile_id=request.profile_id or runtime.default_profile,
            style_hint=request.style_hint,
            text=text,
            split_sentence=runtime.split_sentence,
            conversation_key=request.conversation_key,
            trigger_event_id=request.trigger_event_id,
            turn_token=request.turn_token,
        )
        await publish_notification(
            self._event_publisher,
            EventName.SPEECH_QUEUED,
            {
                "request_id": normalized.request_id,
                "profile_id": normalized.profile_id,
                "character_count": len(normalized.text),
            },
        )
        await publish_notification(
            self._event_publisher,
            EventName.SPEECH_GENERATION_STARTED,
            {"request_id": normalized.request_id, "profile_id": normalized.profile_id},
        )
        started = time.perf_counter()
        try:
            if self._turns is not None and request.turn_token is not None:
                async with self._turns.track(request.turn_token, "generation"):
                    result = await self._provider.synthesize(
                        normalized, cancellation=cancellation
                    )
                if not self._turns.is_current(request.turn_token):
                    raise TurnSupersededError("speech completed after its turn was superseded")
            else:
                result = await self._provider.synthesize(normalized, cancellation=cancellation)
        except asyncio.CancelledError:
            self._last_error_category = "cancelled"
            await publish_notification(
                self._event_publisher,
                EventName.SPEECH_GENERATION_CANCELLED,
                {"request_id": normalized.request_id},
            )
            raise
        except (
            ValueError,
            LookupError,
            OSError,
            SpeechUnavailableError,
            SpeechQueueFullError,
            GenieWorkerUnavailable,
            GenieWorkerFailure,
            TurnSupersededError,
        ) as exc:
            self._last_error_category = type(exc).__name__
            await publish_notification(
                self._event_publisher,
                EventName.SPEECH_GENERATION_FAILED,
                {
                    "request_id": normalized.request_id,
                    "error_category": type(exc).__name__,
                },
            )
            raise
        else:
            self._last_generation_at = datetime.now(UTC)
            self._last_error_category = None
            await publish_notification(
                self._event_publisher,
                EventName.SPEECH_GENERATION_COMPLETED,
                {
                    "request_id": normalized.request_id,
                    "generation_id": result.generation_id,
                    "profile_id": result.profile_id,
                    "reference_key": result.reference_key,
                    "duration_milliseconds": result.duration_milliseconds,
                    "cache_hit": result.cache_hit,
                },
            )
            return result
        finally:
            self._last_generation_latency_seconds = time.perf_counter() - started

    async def health(self) -> SpeechProviderHealth:
        return await self._provider.health()

    async def metrics(self) -> SpeechMetrics:
        return SpeechMetrics(
            queue_depth=await self._generations.queue_depth(),
            last_generation_at=self._last_generation_at,
            last_generation_latency_seconds=self._last_generation_latency_seconds,
            last_error_category=self._last_error_category,
        )

    async def planner_context(self, *, runtime: SpeechRuntimeConfig) -> PlannerSpeechContext:
        if not runtime.enabled or not runtime.planner_enabled:
            return PlannerSpeechContext(enabled=runtime.enabled)
        health = await self._provider.health()
        profile = await self._profile_for_context(runtime.default_profile)
        return PlannerSpeechContext(
            enabled=True,
            available=health.available and profile is not None,
            default_profile=profile.display_name if profile is not None else "",
            available_styles=(
                tuple(dict.fromkeys(item.style for item in profile.references if item.enabled))
                if profile is not None
                else ()
            ),
        )

    async def cleanup(self, *, runtime: SpeechRuntimeConfig) -> tuple[int, int]:
        return await self._cache.cleanup(runtime.cache_retention_hours)

    def audio_path(self, speech: SynthesizedSpeech) -> Path:
        return self._paths.resolve(speech.relative_path, must_exist=True)

    async def mark_sent(self, generation_id: int) -> None:
        await self._generations.mark_sent(generation_id)
        await publish_notification(
            self._event_publisher,
            EventName.SPEECH_SENT,
            {"generation_id": generation_id},
        )

    async def close(self) -> None:
        await self._provider.close()

    async def _profile_for_context(self, profile_id: str) -> VoiceProfile | None:
        profile = (
            await self._profiles.get_profile(profile_id)
            if profile_id
            else await self._profiles.get_default()
        )
        return profile if profile is not None and profile.enabled else None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
