from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qq_ai_bot.admin.models import SpeechRuntimeConfig
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.speech.cache import SpeechCache
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.provider import (
    SpeechProviderHealth,
    SpeechSynthesisRequest,
    SynthesizedSpeech,
)
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository
from qq_ai_bot.speech.service import SpeechService
from yuki_plugin_sdk.events import EventEnvelope, EventName


class FakeTTSProvider:
    def __init__(self, *, wait_for_cancel: bool = False) -> None:
        self.requests: list[SpeechSynthesisRequest] = []
        self.wait_for_cancel = wait_for_cancel

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> SynthesizedSpeech:
        self.requests.append(request)
        if self.wait_for_cancel:
            assert cancellation is not None
            await cancellation.wait()
            raise asyncio.CancelledError
        return SynthesizedSpeech(
            generation_id=1,
            profile_id=request.profile_id or "default",
            reference_key="neutral",
            target_language="zh",
            relative_path="cache/fake.wav",
            format="wav",
            sample_rate=32_000,
            channels=1,
            duration_milliseconds=100,
            cache_hit=False,
        )

    async def health(self) -> SpeechProviderHealth:
        return SpeechProviderHealth(True, True, True, False, "default")

    async def close(self) -> None:
        return None


class FakeSpeechEvents:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def publish(self, event: EventEnvelope) -> object:
        self.events.append(event)
        return ()


def _runtime(*, maximum: int | None = None) -> SpeechRuntimeConfig:
    return SpeechRuntimeConfig(
        enabled=True,
        provider="genie",
        socket_path="/run/yuki-speech/genie.sock",
        root="/data/speech",
        genie_data_dir="/data/speech/genie_data",
        default_profile="default",
        planner_enabled=True,
        default_mode="optional",
        split_sentence=True,
        max_synthesis_characters=maximum,
        queue_max_pending=None,
        cache_retention_hours=None,
        private_enabled=True,
        group_enabled=True,
        automation_enabled=True,
        plugin_enabled=True,
        text_fallback_enabled=True,
    )


def _request(text: str) -> SpeechSynthesisRequest:
    return SpeechSynthesisRequest(
        request_id="request-1",
        profile_id="",
        style_hint="gentle",
        text=text,
        split_sentence=True,
        conversation_key="private:1001",
        trigger_event_id=None,
        turn_token=None,
    )


def _service(
    database: Database,
    tmp_path: Path,
    provider: FakeTTSProvider,
    events: FakeSpeechEvents | None = None,
) -> SpeechService:
    generations = SpeechGenerationRepository(database)
    paths = SpeechPathPolicy(tmp_path / "speech")
    paths.ensure_layout()
    return SpeechService(
        provider=provider,
        generations=generations,
        cache=SpeechCache(repository=generations, paths=paths),
        paths=paths,
        profiles=VoiceProfileRepository(database),
        event_publisher=events,
    )


async def test_speech_service_normalizes_without_silent_truncation(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeTTSProvider()
    service = _service(database, tmp_path, provider)
    result = await service.synthesize(
        _request("**晚安**\n```python\nprint('no')\n```"), runtime=_runtime(maximum=20)
    )
    assert result.profile_id == "default"
    assert provider.requests[0].text == "晚安"

    with pytest.raises(ValueError, match="exceeds"):
        await service.synthesize(_request("这段文字明确超过上限"), runtime=_runtime(maximum=4))
    assert len(provider.requests) == 1


async def test_speech_service_publishes_generation_lifecycle(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeTTSProvider()
    events = FakeSpeechEvents()
    service = _service(database, tmp_path, provider, events)

    await service.synthesize(_request("你好"), runtime=_runtime())

    assert [item.name for item in events.events] == [
        EventName.SPEECH_QUEUED,
        EventName.SPEECH_GENERATION_STARTED,
        EventName.SPEECH_GENERATION_COMPLETED,
    ]


async def test_speech_cancellation_is_a_normal_lifecycle_event(
    database: Database, tmp_path: Path
) -> None:
    provider = FakeTTSProvider(wait_for_cancel=True)
    events = FakeSpeechEvents()
    service = _service(database, tmp_path, provider, events)
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        service.synthesize(_request("等等"), runtime=_runtime(), cancellation=cancellation)
    )
    await asyncio.sleep(0)
    cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert EventName.SPEECH_GENERATION_CANCELLED in {item.name for item in events.events}
