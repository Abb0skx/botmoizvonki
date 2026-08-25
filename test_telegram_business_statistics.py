from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram_business.migrations import connect, migrate
from telegram_business.statistics import collect_statistics, summarize_cycles


TZ = ZoneInfo("Asia/Tashkent")


class TelegramBusinessStatisticsTests(unittest.TestCase):
    def test_cycle_percentiles_and_sla(self):
        rows = [
            {"status": "manager_answered", "work_response_seconds": 600, "bot_response_seconds": 2},
            {"status": "manager_answered", "work_response_seconds": 1800, "bot_response_seconds": 4},
            {"status": "waiting_manager", "work_response_seconds": None, "bot_response_seconds": None},
        ]
        result = summarize_cycles(rows)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(result["avg_bot_response_seconds"], 3)
        self.assertEqual(result["median_manager_seconds"], 1200)
        self.assertEqual(result["share_within_15m"], 0.5)
        self.assertEqual(result["share_within_30m"], 1.0)

    def test_collects_required_period_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "business.db"
            migrate(path)
            now = datetime(2026, 8, 23, 15, tzinfo=TZ)
            with connect(path) as db:
                db.execute(
                    """INSERT INTO response_cycles(
                        cycle_id,chat_id,session_id,first_client_at,last_client_at,
                        first_bot_at,first_manager_at,bot_response_seconds,
                        calendar_response_seconds,work_response_seconds,
                        needs_manager_reply,closed_at,status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "cycle", "42", "session", "2026-08-23T14:00:00+05:00",
                        "2026-08-23T14:00:00+05:00", "2026-08-23T14:00:03+05:00",
                        "2026-08-23T14:10:00+05:00", 3, 600, 600, 0,
                        "2026-08-23T14:10:00+05:00", "manager_answered",
                    ),
                )
                db.execute(
                    """INSERT INTO business_sessions(
                        session_id,chat_id,business_date,started_at,location_received,
                        order_intent,credit_intent,priority,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "session", "42", "2026-08-23", "2026-08-23T14:00:00+05:00",
                        1, 1, 1, 1, "2026-08-23T14:00:00+05:00",
                        "2026-08-23T14:00:00+05:00",
                    ),
                )
                db.execute(
                    """INSERT INTO business_messages(
                        business_connection_id,chat_id,message_id,session_id,direction,
                        sender_type,message_type,text,template_code,telegram_date,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "connection", "42", 10, "session", "outgoing", "business_bot",
                        "text", "prices", "product_result", "2026-08-23T14:00:03+05:00",
                        "2026-08-23T14:00:03+05:00",
                    ),
                )
            today = collect_statistics(path, now)["today"]
            self.assertEqual(today["total_requests"], 1)
            self.assertEqual(today["unique_clients"], 1)
            self.assertEqual(today["bot_replies"], 1)
            self.assertEqual(today["prices_sent"], 1)
            self.assertEqual(today["locations_received"], 1)
            self.assertEqual(today["share_within_15m"], 1.0)


if __name__ == "__main__":
    unittest.main()
