"""Read-only monthly publication hints for the price-entry catalogue."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def publication_preview(db_path: Path, timezone_name: str, *, now: datetime | None = None) -> dict:
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(timezone_name)).date()
    # Do not construct PriceRepository here: its constructor runs migrations.
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        rows = db.execute(
            """SELECT day_of_month, section_key FROM calendar_publication_plan
               WHERE enabled=1 ORDER BY day_of_month, slot, subposition"""
        ).fetchall()
    days = []
    for offset in (1, 2):
        target = today + timedelta(days=offset)
        plan_days = [target.day] if target.day <= 30 else []
        if target.day == 1:
            # Same rollover rule as materialize_due_schedules: missing days
            # 29/30 of February are added to March 1, alongside day 1.
            previous_last_day = (target - timedelta(days=1)).day
            plan_days.extend(range(previous_last_day + 1, 31))
        sections = list(dict.fromkeys(section for day in plan_days for row_day, section in rows if row_day == day))
        days.append({"offset": offset, "date": target.isoformat(), "section_keys": sections})
    return {"timezone": timezone_name, "today": today.isoformat(), "days": days}
