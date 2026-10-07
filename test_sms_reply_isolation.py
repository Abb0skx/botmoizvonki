"""Mock-only checks for replies from known phones; no SMS is sent."""
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from forwarding.config import OPERATOR, load_forwarding_settings
from forwarding.repository import ForwardingRepository, utc_timestamp
from forwarding.sms_service import SMS_DEVICES, SMS_ROUTES, SMSForwardingService


class ReplyIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        def connect():
            conn = sqlite3.connect(Path(self.tmp.name) / "calls.db")
            conn.row_factory = sqlite3.Row
            return conn

        self.repo = ForwardingRepository(connect, OPERATOR, SMS_DEVICES, SMS_ROUTES)
        self.repo.init_schema()
        self.sender = Mock(return_value={"success": True})
        self.service = SMSForwardingService(
            repository=self.repo,
            settings=replace(load_forwarding_settings(), enabled=True),
            chat_id=-100123,
            telegram_api=Mock(return_value={"ok": True}),
            send_sms=self.sender,
        )
        self.now = utc_timestamp()
        self.webhook = {"user_login": "texnikach@gmail.com"}

    def event(self, text, db_call_id=1):
        return {"direction": 0, "event_type": 32,
                "client_number": SMS_DEVICES["redmi"].sim_number,
                "start_time": self.now + 10, "db_call_id": db_call_id,
                "text": text}

    def test_plain_phone_sms_is_kept_unlinked_and_deduplicated(self):
        event = self.event("Ответ оператора без шаблона Automate")
        self.assertTrue(self.service.handle_sms_reply(self.webhook, event, self.now + 10))
        self.assertTrue(self.service.handle_sms_reply(self.webhook, event, self.now + 11))
        with self.repo.connect() as conn:
            rows = conn.execute("SELECT operation_id FROM forwarding_sms_replies").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][0])
        self.sender.assert_not_called()

    def test_empty_phone_sms_is_not_treated_as_customer_rating(self):
        self.assertTrue(self.service.handle_sms_reply(self.webhook, self.event(""), self.now + 10))
        with self.repo.connect() as conn:
            row = conn.execute("SELECT operation_id FROM forwarding_sms_replies").fetchone()
        self.assertIsNone(row[0])

    def test_competing_settings_command_prevents_forwarding_attribution(self):
        self.repo.queue_operation(
            callback_query_id="web:00000000-0000-4000-8000-000000000001",
            employee=SMS_DEVICES["redmi"], action="enable",
            target=SMS_DEVICES["poco"], target_number=SMS_DEVICES["poco"].sim_number,
            service_number="ON Poco", requested_by=0, requested_username="test",
            telegram_chat_id=-100123, telegram_message_id=0,
            now_ts=self.now, cooldown_seconds=300,
            correlation_window_seconds=300, origin="web",
        )
        self.service.dispatch_one(self.now)
        with self.repo.connect() as conn:
            conn.execute("""INSERT INTO device_sms_commands
                (request_id,device_code,command,requested_at,status,dispatched_at)
                VALUES (?,?,?,?,?,?)""",
                ("other", "redmi", "WIFI ON", self.now + 1, "api_accepted", self.now + 1))
        event = self.event("ON Poco\nБатарея: 80%\nРежим звонка: Normal")
        self.assertTrue(self.service.handle_sms_reply(self.webhook, event, self.now + 10))
        with self.repo.connect() as conn:
            row = conn.execute("SELECT operation_id FROM forwarding_sms_replies").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(self.repo.get_operation(1)["status"], "api_accepted")

    def test_settings_state_exposes_command_and_reply_times(self):
        controls = self.service.device_controls
        controls.queue("redmi", "wifi_on", "00000000-0000-4000-8000-000000000003", self.now)
        self.assertEqual(controls.state("redmi")["requested_at"], self.now)
        self.assertIsNone(controls.state("redmi")["completed_at"])
        controls.dispatch_one(self.now)
        event = self.event("WIFI ON: команда выполнена.\nБатарея: 80%\nРежим звонка: Normal", 4)
        self.assertTrue(self.service.handle_sms_reply(self.webhook, event, self.now + 10))
        state = controls.state("redmi")
        self.assertEqual(state["requested_at"], self.now)
        self.assertEqual(state["completed_at"], self.now + 10)


if __name__ == "__main__":
    unittest.main()
