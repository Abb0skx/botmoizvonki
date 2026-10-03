"""Calendar boundary for daily client queues (independent of bot work hours)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DAILY_CODES = ("NEW", "OLMAS", "OTABEK", "ALI", "ABBOS")
TASHKENT = ZoneInfo("Asia/Tashkent")


def day_start(now: datetime) -> datetime:
    return now.astimezone(TASHKENT).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)


def day_key(now: datetime) -> str:
    return now.astimezone(TASHKENT).date().isoformat()


def stamp_day(stamp: str | None) -> str | None:
    try:
        value = datetime.fromisoformat(stamp or "")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return day_key(value)
    except (TypeError, ValueError):
        return None


def is_today(stamp: str | None, now: datetime) -> bool:
    return stamp_day(stamp) == day_key(now)
