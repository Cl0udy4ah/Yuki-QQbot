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
    web_configured: bool
    vision_configured: bool
    onebot_connected: bool
    automation_enabled: bool
    automation_worker_running: bool
    active_automation_count: int
    uptime_seconds: int


async def build_health_payload(container: ApplicationContainer) -> HealthPayload:
    """Check dependencies without probing or billing the LLM provider."""

    database_ok = await container.database.ping()
    return HealthPayload(
        status="ok" if database_ok else "degraded",
        version=__version__,
        database="ok" if database_ok else "unavailable",
        llm_configured=container.settings.llm_configured,
        web_configured=container.settings.web_configured,
        vision_configured=container.settings.vision_configured,
        onebot_connected=container.onebot_connected(),
        automation_enabled=container.settings.automation_enabled,
        automation_worker_running=container.automation_worker.running,
        active_automation_count=await container.automation_repository.active_count(),
        uptime_seconds=max(0, int(time.monotonic() - container.started_at)),
    )
