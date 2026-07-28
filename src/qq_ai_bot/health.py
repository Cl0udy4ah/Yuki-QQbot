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
    planner_enabled: bool
    planner_configured: bool
    planner_active_requests: int
    plugin_system_enabled: bool
    plugin_running_count: int
    emoji_enabled: bool
    emoji_worker_running: bool
    emoji_asset_count: int
    emoji_pending_jobs: int
    uptime_seconds: int


async def build_health_payload(container: ApplicationContainer) -> HealthPayload:
    """Check dependencies without probing or billing the LLM provider."""

    database_ok = await container.database.ping()
    planner_metrics = container.planner_observability.snapshot()
    plugin_manager = getattr(container, "plugin_manager", None)
    plugin_running_count = int(getattr(plugin_manager, "running_count", 0))
    emoji_counts = await container.emoji_repository.counts()
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
        planner_enabled=container.settings.planner_enabled,
        planner_configured=container.settings.planner_configured,
        planner_active_requests=planner_metrics.active_requests,
        plugin_system_enabled=container.settings.plugin_system_enabled,
        plugin_running_count=plugin_running_count,
        emoji_enabled=container.settings.emoji_enabled,
        emoji_worker_running=(
            container.emoji_worker is not None and container.emoji_worker.running
        ),
        emoji_asset_count=sum(
            value for key, value in emoji_counts.items() if key != "jobs_pending"
        ),
        emoji_pending_jobs=emoji_counts.get("jobs_pending", 0),
        uptime_seconds=max(0, int(time.monotonic() - container.started_at)),
    )
