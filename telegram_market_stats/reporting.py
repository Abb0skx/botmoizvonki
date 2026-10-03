from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


TASHKENT = ZoneInfo("Asia/Tashkent")


class MarketStatsUnavailable(RuntimeError):
    """The isolated market statistics database cannot be read safely."""


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _local_iso(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(TASHKENT).isoformat(
        timespec="minutes"
    )


def period_bounds(
    period: str,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    local_now = (now or datetime.now(TASHKENT)).astimezone(TASHKENT)
    today = local_now.date()
    if period == "today":
        first = last = today
        label = "Сегодня"
    elif period == "yesterday":
        first = last = today - timedelta(days=1)
        label = "Вчера"
    elif period in {"7d", "30d"}:
        days = 7 if period == "7d" else 30
        first, last = today - timedelta(days=days - 1), today
        label = f"{days} дней"
    elif period == "custom":
        try:
            first = date.fromisoformat(date_from or "")
            last = date.fromisoformat(date_to or "")
        except ValueError as exc:
            raise ValueError("invalid_custom_period") from exc
        if first > last:
            raise ValueError("invalid_custom_period")
        if (last - first).days > 366:
            raise ValueError("custom_period_too_long")
        label = f"{first:%d.%m.%Y} — {last:%d.%m.%Y}"
    else:
        raise ValueError("invalid_period")
    start = datetime.combine(first, time.min, TASHKENT)
    end = datetime.combine(last + timedelta(days=1), time.min, TASHKENT)
    return start, end, label


def build_market_report(
    database_path: Path,
    *,
    group_id: int,
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> dict:
    path = Path(database_path)
    if not path.is_file():
        raise MarketStatsUnavailable("market_stats_database_not_found")
    start, end, label = period_bounds(
        period, date_from, date_to, now=now
    )
    start_utc, end_utc = _utc_iso(start), _utc_iso(end)
    try:
        database = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
        )
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        database.execute("PRAGMA busy_timeout=5000")
        required = {
            "market_sources", "market_messages", "market_model_mentions",
            "market_competitors", "market_checkpoints",
        }
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required.issubset(tables):
            raise MarketStatsUnavailable("market_stats_schema_incomplete")
        group = str(group_id)
        source = database.execute(
            "SELECT title FROM market_sources WHERE group_id=?", (group,)
        ).fetchone()
        summary = dict(database.execute(
            """SELECT
                 COUNT(*) AS messages,
                 COUNT(DISTINCT sender_id) AS active_senders,
                 SUM(CASE WHEN intent='demand' THEN 1 ELSE 0 END) AS demand_messages,
                 SUM(CASE WHEN intent='supply' THEN 1 ELSE 0 END) AS supply_messages
               FROM market_messages
               WHERE group_id=? AND telegram_date>=? AND telegram_date<?""",
            (group, start_utc, end_utc),
        ).fetchone())
        mention_summary = dict(database.execute(
            """SELECT
                 COUNT(*) AS model_mentions,
                 COUNT(DISTINCT model_key) AS unique_models,
                 COUNT(DISTINCT CASE WHEN intent='demand' THEN sender_id END)
                   AS unique_searchers,
                 SUM(CASE WHEN intent='demand' THEN 1 ELSE 0 END) AS searches,
                 SUM(CASE WHEN intent='demand' AND is_competitor=1 THEN 1 ELSE 0 END)
                   AS competitor_searches
               FROM market_model_mentions
               WHERE group_id=? AND telegram_date>=? AND telegram_date<?""",
            (group, start_utc, end_utc),
        ).fetchone())
        summary.update(mention_summary)
        summary = {key: int(value or 0) for key, value in summary.items()}

        top_models = [dict(row) for row in database.execute(
            """SELECT model_name,COUNT(*) AS searches,
                      COUNT(DISTINCT sender_id) AS unique_senders,
                      SUM(is_competitor) AS competitor_searches,
                      MAX(telegram_date) AS last_search_at
               FROM market_model_mentions
               WHERE group_id=? AND intent='demand'
                 AND telegram_date>=? AND telegram_date<?
               GROUP BY model_key,model_name
               ORDER BY searches DESC,model_name COLLATE NOCASE LIMIT 100""",
            (group, start_utc, end_utc),
        )]
        total_searches = summary["searches"]
        for row in top_models:
            row["share_percent"] = round(
                row["searches"] * 100 / total_searches, 1
            ) if total_searches else 0.0
            row["last_search_at"] = _local_iso(row["last_search_at"])

        competitors = [dict(row) for row in database.execute(
            """SELECT c.telegram_user_id,c.label,COUNT(m.id) AS searches,
                      COUNT(DISTINCT m.model_key) AS unique_models,
                      MAX(m.telegram_date) AS last_search_at
               FROM market_competitors c
               LEFT JOIN market_model_mentions m
                 ON m.sender_id=c.telegram_user_id AND m.group_id=?
                AND m.intent='demand' AND m.telegram_date>=? AND m.telegram_date<?
               WHERE c.enabled=1
               GROUP BY c.telegram_user_id,c.label
               ORDER BY searches DESC,c.label COLLATE NOCASE""",
            (group, start_utc, end_utc),
        )]
        for row in competitors:
            row["last_search_at"] = _local_iso(row["last_search_at"])
            counts = database.execute(
                """SELECT COUNT(*) AS messages,
                          SUM(CASE WHEN has_model=0 THEN 1 ELSE 0 END) AS unrecognized_messages
                   FROM market_messages
                   WHERE group_id=? AND sender_id=? AND telegram_date>=? AND telegram_date<?""",
                (group, row["telegram_user_id"], start_utc, end_utc),
            ).fetchone()
            row["messages"] = int(counts["messages"] or 0)
            row["unrecognized_messages"] = int(counts["unrecognized_messages"] or 0)

        competitor_models = [dict(row) for row in database.execute(
            """SELECT c.label,m.model_name,COUNT(*) AS searches,
                      MAX(m.telegram_date) AS last_search_at
               FROM market_model_mentions m
               JOIN market_competitors c ON c.telegram_user_id=m.sender_id
               WHERE m.group_id=? AND m.intent='demand' AND c.enabled=1
                 AND m.telegram_date>=? AND m.telegram_date<?
               GROUP BY c.telegram_user_id,c.label,m.model_key,m.model_name
               ORDER BY c.label COLLATE NOCASE,searches DESC,
                        m.model_name COLLATE NOCASE LIMIT 200""",
            (group, start_utc, end_utc),
        )]
        for row in competitor_models:
            row["last_search_at"] = _local_iso(row["last_search_at"])

        daily = [dict(row) for row in database.execute(
            """SELECT date(telegram_date,'+5 hours') AS day,
                      COUNT(*) AS searches,
                      COUNT(DISTINCT sender_id) AS unique_senders,
                      SUM(is_competitor) AS competitor_searches
               FROM market_model_mentions
               WHERE group_id=? AND intent='demand'
                 AND telegram_date>=? AND telegram_date<?
               GROUP BY day ORDER BY day""",
            (group, start_utc, end_utc),
        )]
        checkpoint = database.execute(
            """SELECT backfill_scanned,backfill_complete,last_collected_at,last_error
               FROM market_checkpoints WHERE group_id=?""", (group,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise MarketStatsUnavailable("market_stats_database_unavailable") from exc
    finally:
        if "database" in locals():
            database.close()

    checkpoint_data = dict(checkpoint) if checkpoint else {}
    checkpoint_data["backfill_complete"] = bool(
        checkpoint_data.get("backfill_complete")
    )
    checkpoint_data["last_collected_at"] = _local_iso(
        checkpoint_data.get("last_collected_at")
    )
    return {
        "source": {"group_id": group, "title": source["title"] if source else ""},
        "period": {
            "type": period,
            "label": label,
            "date_from": start.date().isoformat(),
            "date_to": (end.date() - timedelta(days=1)).isoformat(),
        },
        "summary": summary,
        "top_models": top_models,
        "competitors": competitors,
        "competitor_models": competitor_models,
        "daily": daily,
        "collector": checkpoint_data,
    }
