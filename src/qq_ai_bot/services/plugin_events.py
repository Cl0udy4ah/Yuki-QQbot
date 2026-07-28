"""Privacy-minimized notification publishing for the chat lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from yuki_plugin_sdk.events import EventEnvelope, EventName
from yuki_plugin_sdk.models import JsonValue

logger = logging.getLogger(__name__)


class LifecycleEventPublisher(Protocol):
    """Minimal interface implemented by the host's notification EventBus."""

    async def publish(self, event: EventEnvelope) -> object: ...


async def publish_notification(
    publisher: LifecycleEventPublisher | None,
    name: EventName,
    payload: Mapping[str, JsonValue],
) -> None:
    """Publish one notification without letting plugin failures affect chat.

    Callers must provide metadata-only payloads.  Exception messages are not logged
    because a third-party publisher may include message bodies or credentials in them.
    """

    if publisher is None:
        return
    try:
        await publisher.publish(EventEnvelope(name=name, payload=payload))
    except Exception as exc:
        logger.warning(
            "plugin_lifecycle_publish_failed event=%s error_category=%s",
            name.value,
            type(exc).__name__,
        )
