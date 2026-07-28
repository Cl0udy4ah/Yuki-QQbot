from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
)
from qq_ai_bot.speech.provider import (
    SpeechProviderHealth,
    SpeechSynthesisRequest,
    SynthesizedSpeech,
)
from qq_ai_bot.speech.service import SpeechService
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import GeneratedSpeechHandle
from yuki_plugin_sdk.permissions import PluginPermission


@dataclass(slots=True)
class RecordGateway:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def call_api(self, action: str, params: dict[str, Any]) -> object:
        self.calls.append((action, params))
        return {"message_id": 81}


class FakeSpeech:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sent: list[int] = []

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        runtime: object,
        cancellation: object = None,
    ) -> SynthesizedSpeech:
        return SynthesizedSpeech(
            generation_id=12,
            profile_id=request.profile_id or "yuki",
            reference_key="neutral",
            target_language="zh",
            relative_path="cache/plugin.wav",
            format="wav",
            sample_rate=32_000,
            channels=1,
            duration_milliseconds=120,
            cache_hit=False,
        )

    async def health(self) -> SpeechProviderHealth:
        return SpeechProviderHealth(True, True, True, False, "yuki")

    def audio_path(self, speech: SynthesizedSpeech) -> Path:
        return self.path

    async def mark_sent(self, generation_id: int) -> None:
        self.sent.append(generation_id)


def _inbound() -> InboundMessage:
    return InboundMessage(
        message_id="speech-plugin-message",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="10001", nickname="Tester"),
        text="用语音说",
        bot_user_id="99999",
        received_at=datetime.now(UTC),
    )


async def _invocation(
    database: Database,
    gateway: RecordGateway,
) -> PluginInvocation:
    settings = Settings.model_validate(
        {
            "database_url": database.url,
            "speech_enabled": True,
            "speech_default_profile": "yuki",
        }
    )
    config = RuntimeConfigService(settings=settings, database=database)
    await config.initialize()
    inbound = _inbound()
    return PluginInvocation(
        plugin_id="example.speech",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=inbound.sender.user_id,
        bot_user_id=inbound.bot_user_id,
        inbound=inbound,
        gateway=gateway,
        runtime_config=await config.snapshot(user_id="10001"),
        reply_effects=[],
    )


async def test_plugin_speech_requires_explicit_permission(
    database: Database, tmp_path: Path
) -> None:
    context = HostPluginContext(plugin_id="example.speech", approved_permissions=())
    with context.bind(await _invocation(database, RecordGateway())):
        with pytest.raises(PluginPermissionError, match=r"speech\.generate"):
            await context.speech.synthesize("你好")


async def test_plugin_speech_handle_is_opaque_owned_and_sent_as_record(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "plugin.wav"
    path.write_bytes(b"local-wave")
    fake = FakeSpeech(path)
    gateway = RecordGateway()
    context = HostPluginContext(
        plugin_id="example.speech",
        approved_permissions=(
            PluginPermission.SPEECH_GENERATE,
            PluginPermission.SPEECH_SEND,
        ),
        services=PluginFacadeServices(speech=cast(SpeechService, fake)),
    )
    with context.bind(await _invocation(database, gateway)):
        handle = await context.speech.synthesize("你好", style_hint="gentle")
        assert set(handle.model_dump()) == {
            "handle_id",
            "generation_id",
            "profile_id",
            "duration_milliseconds",
            "expires_at",
        }
        assert "path" not in handle.model_dump_json()
        with pytest.raises(PluginPermissionError, match="not owned"):
            await context.speech.send_private(
                "10001",
                GeneratedSpeechHandle(
                    handle_id="forged",
                    generation_id=handle.generation_id,
                    profile_id=handle.profile_id,
                    duration_milliseconds=handle.duration_milliseconds,
                ),
            )
        result = await context.speech.send_private("10001", handle)
        assert result.ok

    action, params = gateway.calls[0]
    assert action == "send_private_msg"
    assert params["user_id"] == "10001"
    message = cast(list[dict[str, dict[str, str]]], params["message"])
    assert message[0]["type"] == "record"
    assert message[0]["data"]["file"].startswith("base64://")
    assert fake.sent == [12]
