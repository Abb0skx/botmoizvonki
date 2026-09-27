import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import requests

from forwarding.config import OPERATOR, load_forwarding_settings
from forwarding.repository import ForwardingRepository
from forwarding.sms_service import SMS_DEVICES, SMS_ROUTES, SMSForwardingService


class SMSControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        def connect():
            conn = sqlite3.connect(Path(self.tmp.name) / "test.db")
            conn.row_factory = sqlite3.Row
            return conn
        self.repo = ForwardingRepository(connect, OPERATOR, SMS_DEVICES, SMS_ROUTES)
        self.repo.init_schema()
        self.sender = Mock(return_value={"success": True, "status": "SMS posted"})
        self.service = SMSForwardingService(
            repository=self.repo, settings=replace(load_forwarding_settings(), enabled=True,
            command_cooldown_seconds=300, correlation_window_seconds=300),
            chat_id=-100123, telegram_api=Mock(return_value={"result": {"message_id": 123}}),
            send_sms=self.sender,
        )
        self.now = 1800000000
        self.repo.reserve_daily_post("2027-01-01", "-100123", self.now)
        self.repo.mark_post_sent("2027-01-01", 123, self.now)
        self.repo.mark_post_pinned("2027-01-01", self.now)

    def queue(self, source="redmi", target="poco", actor=202134293, key="first"):
        return self.service.queue_callback(callback_query_id=key,
            callback_data=f"fwd:{source}:{target}", telegram_user={"id": actor},
            chat_id=-100123, message_id=123, now_ts=self.now)

    def test_all_routes_exact_commands(self):
        for source, target in SMS_ROUTES:
            self.assertEqual(self.service.service_number(source, target)[1], "ON " + SMS_DEVICES[target].name)
        self.assertEqual(self.service.service_number("poco", "off")[1], "OFF")
        with self.assertRaises(ValueError):
            self.service.service_number("poco", "poco")

    def test_sms_always_from_poco(self):
        self.assertTrue(self.queue()["queued"])
        self.service.dispatch_one(self.now)
        self.sender.assert_called_once_with("+998908534466", "texnikach@gmail.com", "ON Poco")
        self.assertEqual(self.repo.get_operation(1)["status"], "api_accepted")
        self.assertFalse(self.service.dispatch_one(self.now + 1)["processed"])

    def test_acl_and_poco_admin(self):
        self.assertEqual(self.queue("tecno", actor=7636344727)["reason"], "forbidden")
        self.assertEqual(self.queue("poco", "redmi", actor=702960146)["reason"], "forbidden")
        self.assertTrue(self.queue("poco", "redmi")["queued"])

    def test_duplicate_and_pending(self):
        self.queue()
        self.assertEqual(self.queue()["reason"], "replay")
        self.assertEqual(self.queue(key="new")["reason"], "busy")

    def test_timeout_not_retried(self):
        self.queue()
        self.sender.side_effect = requests.Timeout()
        self.service.dispatch_one(self.now)
        self.assertEqual(self.repo.get_operation(1)["status"], "unconfirmed")
        self.service.dispatch_one(self.now + 1)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.queue(key="new")["reason"], "unconfirmed")

    def test_reply_only_incoming_preserves_operator_error(self):
        self.queue()
        self.service.dispatch_one(self.now)
        webhook = {"user_login": "texnikach@gmail.com"}
        event = {"direction": 1, "event_type": 32, "client_number": "+998908534466",
                 "start_time": self.now + 10, "db_call_id": 1,
                 "text": "Ошибка оператора\nБатарея: 68%\nРежим звонка: Normal"}
        self.assertFalse(self.service.handle_sms_reply(webhook, event, self.now + 11))
        event["direction"] = 0
        self.assertTrue(self.service.handle_sms_reply(webhook, event, self.now + 11))
        self.assertEqual(self.repo.get_operation(1)["status"], "sms_reply_received")
        self.assertEqual(self.repo.get_device("redmi")["forwarding_status"], "unknown")
        self.service.handle_sms_reply(webhook, event, self.now + 12)
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM forwarding_sms_replies").fetchone()[0], 1)

    def test_sms_operation_cannot_swallow_customer_call(self):
        self.queue()
        self.assertFalse(self.service._dial_matches(self.repo.get_operation(1), "+998901313999", {}, self.now))

    def test_legacy_mmi_not_sent(self):
        with self.assertRaises(ValueError):
            self.service._send_command("texnikach@gmail.com", "##21#")
        self.sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
