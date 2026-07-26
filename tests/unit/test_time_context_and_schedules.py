from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qq_ai_bot.automation.models import (
    AfterSchedule,
    DailySchedule,
    IntervalSchedule,
    OnceSchedule,
    WeeklySchedule,
)
from qq_ai_bot.time.formatting import local_iso, local_text
from qq_ai_bot.time.schedules import initial_run_at, next_run_at, schedule_after_completion
from qq_ai_bot.time.service import TimeContextService


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def test_utc_storage_is_rendered_as_china_local_time() -> None:
    stored = datetime(2026, 7, 26, 19, 35, 44, tzinfo=UTC)

    assert local_iso(stored, "Asia/Shanghai") == "2026-07-27T03:35:44+08:00"
    assert local_text(stored, "Asia/Shanghai") == "2026-07-27 03:35:44"


@pytest.mark.asyncio
async def test_time_context_uses_persistent_person_timezone(database) -> None:
    now = datetime(2026, 7, 27, 15, 10, tzinfo=UTC)
    service = TimeContextService(database, clock=FakeClock(now))

    assert (await service.current("10001")).to_model_dict() == {
        "utc": "2026-07-27T15:10:00Z",
        "local": "2026-07-27T23:10:00+08:00",
        "timezone": "Asia/Shanghai",
        "date": "2026-07-27",
        "weekday": "Monday",
    }

    # A person row is required by the database FK and is normally created by
    # the real inbound event pipeline.
    from qq_ai_bot.persistence.repositories import PeopleRepository

    await PeopleRepository(database).observe(user_id="10001", nickname="测试")
    assert await service.set_timezone("10001", "America/New_York") == "America/New_York"
    assert (await service.current("10001")).timezone == "America/New_York"


@pytest.mark.parametrize("timezone", ["No/Such_Zone", "", "x" * 65])
def test_invalid_timezone_is_rejected(database, timezone: str) -> None:
    service = TimeContextService(database)
    with pytest.raises(ValueError):
        service.at(datetime.now(UTC), timezone)


def test_supported_schedules_calculate_utc() -> None:
    now = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
    assert initial_run_at(AfterSchedule(type="after", seconds=1200), now, "Asia/Shanghai") == (
        now + timedelta(minutes=20)
    )
    assert initial_run_at(
        OnceSchedule(
            type="once",
            local_datetime=datetime(2026, 7, 28, 15, 0),
            timezone="Asia/Shanghai",
        ),
        now,
        "Asia/Shanghai",
    ) == datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    assert initial_run_at(
        DailySchedule(type="daily", hour=15, minute=0, timezone="Asia/Shanghai"),
        now,
        "Asia/Shanghai",
    ) == datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    assert initial_run_at(
        WeeklySchedule(
            type="weekly", weekdays=(1, 3, 5), hour=15, minute=0, timezone="Asia/Shanghai"
        ),
        now,
        "Asia/Shanghai",
    ) == datetime(2026, 7, 29, 7, 0, tzinfo=UTC)
    interval = IntervalSchedule(type="interval", seconds=3600)
    assert next_run_at(interval, now, "Asia/Shanghai") == now + timedelta(hours=1)
    assert schedule_after_completion(interval, now, now + timedelta(hours=3), "Asia/Shanghai") == (
        now + timedelta(hours=4)
    )


def test_once_in_past_is_rejected() -> None:
    now = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="晚于"):
        initial_run_at(
            OnceSchedule(type="once", local_datetime=datetime(2026, 7, 27, 14, 59)),
            now,
            "Asia/Shanghai",
        )


def test_nonexistent_dst_time_moves_forward() -> None:
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    result = initial_run_at(
        OnceSchedule(
            type="once",
            local_datetime=datetime(2026, 3, 8, 2, 30),
            timezone="America/New_York",
        ),
        now,
        "America/New_York",
    )
    assert result == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
