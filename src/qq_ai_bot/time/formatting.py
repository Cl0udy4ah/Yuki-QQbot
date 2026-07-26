"""Consistent user-facing formatting for trusted timestamps."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def local_iso(value: datetime | None, timezone: str) -> str | None:
    """Render a stored UTC timestamp in an explicit IANA timezone."""

    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(ZoneInfo(timezone)).isoformat()


def local_text(value: datetime | None, timezone: str) -> str:
    """Render a compact local timestamp suitable for QQ messages."""

    rendered = local_iso(value, timezone)
    if rendered is None:
        return "无"
    return datetime.fromisoformat(rendered).strftime("%Y-%m-%d %H:%M:%S")
