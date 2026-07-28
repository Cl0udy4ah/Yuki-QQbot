from __future__ import annotations

import asyncio

import pytest

from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from yuki_plugin_sdk.errors import RegistrationError
from yuki_plugin_sdk.events import EventEnvelope, EventName


async def test_hooks_are_ordered_and_failures_are_isolated() -> None:
    bus = PluginEventBus(default_timeout_seconds=0.2)
    called: list[str] = []

    async def good(_event: EventEnvelope) -> None:
        called.append("good")

    async def bad(_event: EventEnvelope) -> None:
        called.append("bad")
        raise RuntimeError("secret text must not escape")

    bus.subscribe(
        plugin_id="com.example.good",
        hook_id="good",
        event=EventName.REPLY_SENT,
        handler=good,
        priority=10,
    )
    bus.subscribe(
        plugin_id="com.example.bad",
        hook_id="bad",
        event=EventName.REPLY_SENT,
        handler=bad,
    )
    results = await bus.publish(EventEnvelope(name=EventName.REPLY_SENT))

    assert called == ["good", "bad"]
    assert [result.success for result in results] == [True, False]
    assert results[1].error_category == "RuntimeError"


async def test_hook_timeout_does_not_block_other_hooks() -> None:
    bus = PluginEventBus(default_timeout_seconds=0.01)
    completed = False

    async def slow(_event: EventEnvelope) -> None:
        await asyncio.sleep(1)

    async def fast(_event: EventEnvelope) -> None:
        nonlocal completed
        completed = True

    bus.subscribe(
        plugin_id="com.example.slow",
        hook_id="slow",
        event=EventName.MESSAGE_RECORDED,
        handler=slow,
    )
    bus.subscribe(
        plugin_id="com.example.fast",
        hook_id="fast",
        event=EventName.MESSAGE_RECORDED,
        handler=fast,
    )
    results = await bus.publish(EventEnvelope(name=EventName.MESSAGE_RECORDED))
    assert completed is True
    assert {result.error_category for result in results} == {"hook_timeout", None}


async def test_notification_hooks_receive_isolated_event_copies() -> None:
    bus = PluginEventBus()
    seen: list[str] = []

    async def mutating(event: EventEnvelope) -> None:
        assert isinstance(event.payload, dict)
        event.payload["value"] = "changed"

    async def observing(event: EventEnvelope) -> None:
        seen.append(str(event.payload["value"]))

    bus.subscribe(
        plugin_id="com.example.mutating",
        hook_id="mutating",
        event=EventName.MESSAGE_RECORDED,
        handler=mutating,
        priority=10,
    )
    bus.subscribe(
        plugin_id="com.example.observing",
        hook_id="observing",
        event=EventName.MESSAGE_RECORDED,
        handler=observing,
    )
    original = EventEnvelope(name=EventName.MESSAGE_RECORDED, payload={"value": "original"})
    await bus.publish(original)

    assert seen == ["original"]
    assert original.payload["value"] == "original"


def test_duplicate_hook_is_rejected() -> None:
    bus = PluginEventBus()

    async def handler(_event: EventEnvelope) -> None:
        return None

    kwargs = {
        "plugin_id": "com.example.echo",
        "hook_id": "same",
        "event": EventName.REPLY_SENT,
        "handler": handler,
    }
    bus.subscribe(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(RegistrationError):
        bus.subscribe(**kwargs)  # type: ignore[arg-type]
