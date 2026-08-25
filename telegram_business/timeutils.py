from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def localize(value: datetime, timezone_name: str = "Asia/Tashkent") -> datetime:
    zone = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def is_night(value: datetime, start: time = time(20), end: time = time(9, 30)) -> bool:
    current = value.timetz().replace(tzinfo=None)
    return current >= start or current < end


def business_date(value: datetime, start: time = time(20)) -> date:
    local = value
    return local.date() if local.timetz().replace(tzinfo=None) >= start else (local - timedelta(days=1)).date()


def telegram_datetime(unix_timestamp: object, fallback: datetime, timezone_name: str = "Asia/Tashkent") -> datetime:
    """Convert Telegram's UTC Unix timestamp to the configured business zone."""
    try:
        timestamp = int(unix_timestamp)
    except (TypeError, ValueError, OverflowError):
        return localize(fallback, timezone_name)
    try:
        return datetime.fromtimestamp(timestamp, tz=ZoneInfo(timezone_name))
    except (OSError, OverflowError, ValueError):
        return localize(fallback, timezone_name)


def manager_phrases(
    value: datetime,
    manager_start: time = time(10),
    workdays=frozenset(range(7)),
    night_start: time = time(20),
) -> tuple[str, str]:
    """Describe the next configured manager start in Russian and Uzbek."""
    local = value.timetz().replace(tzinfo=None)
    candidate = value.date() + timedelta(days=1 if local >= night_start else 0)
    allowed = frozenset(workdays) or frozenset(range(7))
    for _ in range(8):
        if candidate.weekday() in allowed:
            break
        candidate += timedelta(days=1)
    clock = manager_start.strftime("%H:%M")
    delta = (candidate - value.date()).days
    if delta == 0:
        return f"сегодня после {clock}", f"bugun soat {clock} dan keyin"
    if delta == 1:
        return f"завтра после {clock}", f"ertaga soat {clock} dan keyin"
    ru_days = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
    uz_days = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")
    return (
        f"{ru_days[candidate.weekday()]} после {clock}",
        f"{uz_days[candidate.weekday()]} soat {clock} dan keyin",
    )


def next_night_end(value: datetime, end: time = time(9, 30), start: time = time(20)) -> datetime:
    candidate = value.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    current = value.timetz().replace(tzinfo=None)
    if current >= start or current >= end:
        candidate += timedelta(days=1)
    return candidate


def manager_due_at(
    value: datetime,
    manager_start: time = time(10),
    manager_end: time = time(20),
    workdays=frozenset(range(7)),
) -> datetime:
    """Return when manager working time next begins for this request."""
    allowed = frozenset(workdays) or frozenset(range(7))
    local_time = value.timetz().replace(tzinfo=None)
    if value.weekday() in allowed and local_time < manager_end:
        if local_time < manager_start:
            return value.replace(
                hour=manager_start.hour,
                minute=manager_start.minute,
                second=0,
                microsecond=0,
            )
        return value
    candidate = value + timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in allowed:
            return candidate.replace(
                hour=manager_start.hour,
                minute=manager_start.minute,
                second=0,
                microsecond=0,
            )
        candidate += timedelta(days=1)
    return candidate


def work_seconds(start: datetime, end: datetime, work_start: time = time(10), work_end: time = time(20), workdays=frozenset(range(7))) -> int:
    if end <= start:
        return 0
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() in workdays:
            left = datetime.combine(day, work_start, tzinfo=start.tzinfo)
            right = datetime.combine(day, work_end, tzinfo=start.tzinfo)
            total += max(0.0, (min(end, right) - max(start, left)).total_seconds())
        day += timedelta(days=1)
    return int(total)
