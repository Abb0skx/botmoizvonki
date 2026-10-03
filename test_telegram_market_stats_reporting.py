from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest

from telegram_market_stats.repository import MarketStatsRepository
from telegram_market_stats.reporting import (
    MarketStatsUnavailable,
    build_market_report,
    period_bounds,
)


TASHKENT = ZoneInfo("Asia/Tashkent")


def _seed(path: Path) -> None:
    MarketStatsRepository(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO market_sources(group_id,title,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            ("-1002188560435", "Malika bozor N1", "2026-09-29T00:00:00+00:00", "2026-09-29T00:00:00+00:00"),
        )
        db.executemany(
            "INSERT INTO market_competitors VALUES(?,?,?,?,?)",
            [
                ("213962560", "Mobilon", 1, "2026-09-29T00:00:00+00:00", "2026-09-29T00:00:00+00:00"),
                ("6243942320", "MixMobiles_1", 1, "2026-09-29T00:00:00+00:00", "2026-09-29T00:00:00+00:00"),
            ],
        )
        messages = [
            (1, "213962560", "2026-09-29T19:30:00+00:00", "demand", 1),
            (2, "42", "2026-09-30T05:00:00+00:00", "demand", 0),
            (3, "43", "2026-09-30T06:00:00+00:00", "supply", 0),
        ]
        for message_id, sender_id, stamp, intent, competitor in messages:
            db.execute(
                """INSERT INTO market_messages(
                     group_id,message_id,sender_id,telegram_date,text_hash,intent,
                     is_competitor,has_model,processed_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("-1002188560435", message_id, sender_id, stamp, str(message_id), intent, competitor, 1, stamp),
            )
        db.executemany(
            """INSERT INTO market_model_mentions(
                 group_id,message_id,model_key,model_name,memory,color,intent,
                 sender_id,is_competitor,confidence,telegram_date)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ("-1002188560435", 1, "iphone-16-pro", "Apple iPhone 16 Pro", "", "", "demand", "213962560", 1, 1.0, "2026-09-29T19:30:00+00:00"),
                ("-1002188560435", 2, "iphone-16-pro", "Apple iPhone 16 Pro", "", "", "demand", "42", 0, 1.0, "2026-09-30T05:00:00+00:00"),
                ("-1002188560435", 3, "galaxy-s26-ultra", "Samsung Galaxy S26 Ultra", "", "", "supply", "43", 0, 1.0, "2026-09-30T06:00:00+00:00"),
            ],
        )
        db.execute(
            """INSERT INTO market_checkpoints(
                 group_id,last_message_id,backfill_scanned,backfill_complete,
                 last_collected_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            ("-1002188560435", 3, 2500, 0, "2026-09-30T06:05:00+00:00", "2026-09-30T06:05:00+00:00"),
        )


def test_market_report_uses_tashkent_day_and_competitor_breakdown():
    with TemporaryDirectory() as folder:
        path = Path(folder) / "market.db"
        _seed(path)
        report = build_market_report(
            path,
            group_id=-1002188560435,
            period="today",
            now=datetime(2026, 9, 30, 15, 0, tzinfo=TASHKENT),
        )

    assert report["source"]["title"] == "Malika bozor N1"
    assert report["summary"]["messages"] == 3
    assert report["summary"]["searches"] == 2
    assert report["summary"]["competitor_searches"] == 1
    assert report["summary"]["supply_messages"] == 1
    assert report["top_models"][0]["model_name"] == "Apple iPhone 16 Pro"
    assert report["top_models"][0]["share_percent"] == 100.0
    assert report["competitors"][0]["label"] == "Mobilon"
    assert report["competitors"][0]["searches"] == 1
    assert report["competitor_models"][0]["model_name"] == "Apple iPhone 16 Pro"
    assert report["collector"]["backfill_scanned"] == 2500
    assert report["collector"]["last_collected_at"].endswith("+05:00")


def test_period_bounds_validate_custom_range():
    with pytest.raises(ValueError, match="invalid_custom_period"):
        period_bounds("custom", "2026-09-30", "2026-09-01")
    with pytest.raises(ValueError, match="custom_period_too_long"):
        period_bounds("custom", "2025-01-01", "2026-09-30")


def test_missing_database_is_reported_without_creating_it(tmp_path: Path):
    path = tmp_path / "missing.db"
    with pytest.raises(MarketStatsUnavailable, match="database_not_found"):
        build_market_report(path, group_id=-1002188560435)
    assert not path.exists()


def test_reports_do_not_mix_groups_and_count_unrecognized_company_messages(tmp_path):
    path = tmp_path / "market.db"
    _seed(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO market_messages(group_id,message_id,sender_id,telegram_date,text_hash,intent,has_model,processed_at) VALUES(?,?,?,?,?,?,?,?)",
            ("-1001463992108", 1, "213962560", "2026-09-30T06:00:00+00:00", "accessory", "unknown", 0, "2026-09-30T06:00:00+00:00"),
        )
    report = build_market_report(path, group_id=-1001463992108, now=datetime(2026, 9, 30, 15, tzinfo=TASHKENT))
    assert report["summary"]["messages"] == 1
    assert report["summary"]["searches"] == 0
    mobilon = next(row for row in report["competitors"] if row["label"] == "Mobilon")
    assert mobilon["messages"] == mobilon["unrecognized_messages"] == 1
    assert mobilon["searches"] == 0
