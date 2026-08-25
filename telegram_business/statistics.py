from __future__ import annotations

import statistics
from collections.abc import Iterable
from datetime import datetime, time, timedelta, timezone

from .migrations import connect


PERIODS = ("today", "yesterday", "7_days", "30_days")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[int], percentile: float) -> float | None:
    """Return a deterministic linear percentile without optional dependencies."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize_cycles(rows: Iterable) -> dict[str, float | int | None]:
    rows = list(rows)
    waits = [int(row["work_response_seconds"]) for row in rows if row["work_response_seconds"] is not None]
    bot_waits = [int(row["bot_response_seconds"]) for row in rows if row["bot_response_seconds"] is not None]

    def share_within(seconds: int) -> float | None:
        if not waits:
            return None
        return round(sum(value <= seconds for value in waits) / len(waits), 4)

    return {
        "total": len(rows),
        "waiting": sum(row["status"] == "waiting_manager" for row in rows),
        "answered": len(waits),
        "avg_bot_response_seconds": round(statistics.fmean(bot_waits), 2) if bot_waits else None,
        "median_manager_seconds": statistics.median(waits) if waits else None,
        "p90_manager_seconds": round(_percentile(waits, 0.9), 2) if waits else None,
        "share_within_15m": share_within(15 * 60),
        "share_within_30m": share_within(30 * 60),
        "share_within_60m": share_within(60 * 60),
    }


def period_bounds(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": (midnight, now),
        "yesterday": (midnight - timedelta(days=1), midnight),
        "7_days": (midnight - timedelta(days=6), now),
        "30_days": (midnight - timedelta(days=29), now),
    }


def _in_period(value: str | None, start: datetime, end: datetime) -> bool:
    parsed = _parse(value)
    return parsed is not None and start <= parsed < end


def _is_night(value: datetime, start: time = time(20), end: time = time(9, 30)) -> bool:
    local = value.timetz().replace(tzinfo=None)
    return local >= start or local < end


def collect_statistics(
    db_path,
    now: datetime,
    night_start: time = time(20),
    night_end: time = time(9, 30),
) -> dict[str, dict[str, float | int | None]]:
    """Calculate workbook metrics from SQLite, the source of truth."""
    with connect(db_path) as db:
        cycles = db.execute("SELECT * FROM response_cycles").fetchall()
        sessions = db.execute("SELECT * FROM business_sessions").fetchall()
        messages = db.execute("SELECT * FROM business_messages").fetchall()

    result: dict[str, dict[str, float | int | None]] = {}
    for period, (start, end) in period_bounds(now).items():
        period_cycles = [row for row in cycles if _in_period(row["first_client_at"], start, end)]
        period_sessions = [row for row in sessions if _in_period(row["started_at"], start, end)]
        period_messages = [row for row in messages if _in_period(row["created_at"], start, end)]
        cycle_stats = summarize_cycles(period_cycles)
        waiting_ages = [
            (now - first).total_seconds()
            for row in period_cycles
            if row["status"] == "waiting_manager" and (first := _parse(row["first_client_at"])) is not None
        ]
        client_starts = [
            first
            for row in period_cycles
            if (first := _parse(row["first_client_at"])) is not None
        ]
        result[period] = {
            "total_requests": len(period_cycles),
            "unique_clients": len({str(row["chat_id"]) for row in period_cycles}),
            "night_requests": sum(_is_night(value, night_start, night_end) for value in client_starts),
            "day_requests": sum(not _is_night(value, night_start, night_end) for value in client_starts),
            "bot_replies": sum(row["sender_type"] == "business_bot" for row in period_messages),
            "product_searches": sum(
                (row["template_code"] or "") in {"product_result", "ambiguous", "not_found_1", "not_found_2"}
                for row in period_messages
            ),
            "prices_sent": sum((row["template_code"] or "") == "product_result" for row in period_messages),
            "locations_received": sum(bool(row["location_received"]) for row in period_sessions),
            "order_intents": sum(bool(row["order_intent"]) for row in period_sessions),
            "credit_intents": sum(bool(row["credit_intent"]) for row in period_sessions),
            "handed_to_manager": sum(
                bool(row["priority"])
                or bool(row["handoff_reason"])
                or row["status"] == "human_handoff"
                for row in period_sessions
            ),
            "waiting_manager": cycle_stats["waiting"],
            "unhandled_over_30m": sum(age > 30 * 60 for age in waiting_ages),
            "unhandled_over_60m": sum(age > 60 * 60 for age in waiting_ages),
            "avg_bot_response_seconds": cycle_stats["avg_bot_response_seconds"],
            "median_manager_seconds": cycle_stats["median_manager_seconds"],
            "p90_manager_seconds": cycle_stats["p90_manager_seconds"],
            "share_within_15m": cycle_stats["share_within_15m"],
            "share_within_30m": cycle_stats["share_within_30m"],
            "share_within_60m": cycle_stats["share_within_60m"],
        }
    return result


def statistics_rows(
    db_path,
    now: datetime,
    night_start: time = time(20),
    night_end: time = time(9, 30),
) -> list[list[object]]:
    snapshot = collect_statistics(db_path, now, night_start, night_end)
    updated = now.astimezone(timezone.utc).isoformat()
    return [
        [period, metric, "" if value is None else value, updated]
        for period in PERIODS
        for metric, value in snapshot[period].items()
    ]
