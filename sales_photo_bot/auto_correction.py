from __future__ import annotations

from datetime import datetime, time, timedelta

from .dates import TASHKENT_TZ


EVENT_QUIET_PERIOD_SECONDS = 5 * 60
AUTO_CORRECTION_CLOCKS = (
    time(7, 0),
    time(10, 0),
    time(11, 0),
    time(12, 0),
    time(13, 0),
    time(14, 0),
    time(15, 0),
    time(16, 0),
    time(17, 0),
    time(18, 0),
    time(18, 30),
    time(19, 0),
    time(20, 0),
    time(21, 0),
    time(22, 0),
    time(23, 0),
)


def _tashkent(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TASHKENT_TZ)
    return value.astimezone(TASHKENT_TZ)


def latest_auto_correction_slot(now: datetime) -> datetime:
    """Return the latest scheduled Tashkent slot at or before ``now``."""

    local = _tashkent(now)
    for clock in reversed(AUTO_CORRECTION_CLOCKS):
        candidate = datetime.combine(local.date(), clock, tzinfo=TASHKENT_TZ)
        if candidate <= local:
            return candidate
    previous = local.date() - timedelta(days=1)
    return datetime.combine(
        previous,
        AUTO_CORRECTION_CLOCKS[-1],
        tzinfo=TASHKENT_TZ,
    )


def next_auto_correction_slot(now: datetime) -> datetime:
    """Return the first scheduled Tashkent slot strictly after ``now``."""

    local = _tashkent(now)
    for clock in AUTO_CORRECTION_CLOCKS:
        candidate = datetime.combine(local.date(), clock, tzinfo=TASHKENT_TZ)
        if candidate > local:
            return candidate
    following = local.date() + timedelta(days=1)
    return datetime.combine(
        following,
        AUTO_CORRECTION_CLOCKS[0],
        tzinfo=TASHKENT_TZ,
    )
