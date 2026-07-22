"""Safe health response construction."""

from __future__ import annotations

import time
from typing import TypedDict

from qq_ai_bot import __version__
from qq_ai_bot.container import ApplicationContainer


class HealthPayload(TypedDict):
    """Public, credential-free health response."""

    status: str
    version: str
    database: str
    llm_configured: bool
    onebot_connected: bool
    uptime_seconds: int


async def build_health_payload(container: ApplicationContainer) -> HealthPayload:
    """Check dependencies without probing or billing the LLM provider."""

    database_ok = await container.database.ping()
    return HealthPayload(
        status="ok" if database_ok else "degraded",
        version=__version__,
        database="ok" if database_ok else "unavailable",
        llm_configured=container.settings.llm_configured,
        onebot_connected=container.onebot_connected(),
        uptime_seconds=max(0, int(time.monotonic() - container.started_at)),
    )
