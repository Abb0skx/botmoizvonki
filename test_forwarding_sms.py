import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_device_controls_allowed_commands_and_poco_only_hotspot(self):
        for device in ("redmi", "tecno", "poco"):
            states = next(s for s in self.service.states() if s["code"] == device)
            self.assertEqual(len(states["controls"]), 9 if device == "poco" else 7)
        with self.assertRaises(ValueError):
            self.service.device_controls.queue("tecno", "hotspot_on", "bad", self.now)
        with self.assertRaises(ValueError):
            self.service.device_controls.queue("tecno", "WIFI on", "bad2", self.now)

    def test_device_control_exact_sms_and_shared_cooldown(self):
        controls = self.service.device_controls
        self.assertTrue(controls.queue("tecno", "wifi_on", "wifi1", self.now)["queued"])
        self.assertEqual(controls.queue("tecno", "sound_on", "sound1", self.now)["reason"], "busy")
        self.assertEqual(self.queue("tecno", key="forward")["reason"], "busy")
        controls.dispatch_one(self.now)
        self.sender.assert_called_once_with("+998908456162", "texnikach@gmail.com", "WIFI ON")
        controls.dispatch_one(self.now + 1)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.repo.get_device("tecno")["forwarding_status"], "unknown")

    def test_settings_reply_and_location_no_expected_sms(self):
        controls = self.service.device_controls
        controls.queue("redmi", "sound_off", "sound1", self.now)
        controls.dispatch_one(self.now)
        event = {"event_type": 32, "direction": 0, "client_number": "+998908534466",
                 "start_time": self.now + 10, "text": "SOUND OFF\nБатарея: 80%\nРежим звонка: Silent"}
        self.assertTrue(self.service.handle_sms_reply({"user_login": "texnikach@gmail.com"}, event, self.now+10))
        self.assertEqual(controls.state("redmi")["reply"], event["text"])
        self.assertEqual(self.repo.get_device("redmi")["forwarding_status"], "unknown")
        controls.queue("poco", "location", "location1", self.now)
        controls.dispatch_one(self.now)
        self.assertIn("30 минут", controls.state("poco")["status_label"])
        event.update(client_number="+998901313999", text="Нет интернета")
        self.assertTrue(self.service.handle_sms_reply({"user_login":"texnikach@gmail.com"}, event, self.now+10))
        self.assertEqual(controls.state("poco")["reply"], "Нет интернета")

    def test_settings_timeout_restart_does_not_retry(self):
        controls = self.service.device_controls
        controls.queue("poco", "hotspot_on", "hotspot1", self.now)
        self.sender.side_effect = requests.Timeout()
        controls.dispatch_one(self.now)
        controls.dispatch_one(self.now+61)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(controls.state("poco")["status_label"], "Подтверждения нет; автоповтор отключён")

    def test_forwarding_blocks_device_settings(self):
        self.queue()
        self.assertEqual(self.service.device_controls.queue("redmi", "wifi_off", "off1", self.now)["reason"], "busy")

    def test_interrupted_settings_dispatch_is_not_repeated(self):
        controls = self.service.device_controls
        controls.queue("tecno", "internet_on", "restart1", self.now)
        with self.repo.connect() as conn:
            conn.execute("UPDATE device_sms_commands SET status='sending',dispatched_at=?", (self.now,))
        controls.dispatch_one(self.now+61)
        self.sender.assert_not_called()
        self.assertEqual(controls.state("tecno")["status_label"], "Подтверждения нет; автоповтор отключён")

    def test_concurrent_settings_clicks_queue_exactly_one(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.service.device_controls.queue(
                "redmi", "wifi_on", "parallel"+str(i), self.now), range(4)))
        self.assertEqual(sum(result["queued"] for result in results), 1)

    def test_sms_always_from_poco(self):
        self.assertTrue(self.queue()["queued"])
        self.service.dispatch_one(self.now)
        self.sender.assert_called_once_with("+998908534466", "texnikach@gmail.com", "ON Poco")
        self.assertEqual(self.repo.get_operation(1)["status"], "api_accepted")
        self.assertFalse(self.service.dispatch_one(self.now + 1)["processed"])

    def test_web_and_telegram_commands_keep_distinct_origins(self):
        web = self.service.queue_web("poco", "redmi", "web-request-1")
        self.assertTrue(web["queued"])
        self.assertEqual(self.repo.get_operation(1)["origin"], "web")
        self.assertEqual(self.repo.get_operation(1)["service_number"], "ON Redmi")

        telegram = self.queue("redmi", "poco", key="telegram-request-1")
        self.assertTrue(telegram["queued"])
        self.assertEqual(self.repo.get_operation(2)["origin"], "telegram")
        self.sender.assert_not_called()

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

    def test_answer_unlocks_next_command_and_next_reply_matches(self):
        self.assertTrue(self.queue()["queued"])
        self.service.dispatch_one(self.now)
        webhook = {"user_login": "texnikach@gmail.com"}
        first = {"direction": 0, "event_type": 32,
                 "client_number": "+998908534466", "start_time": self.now + 10,
                 "db_call_id": 101,
                 "text": "ON Poco\nПереадресация выполнена\nБатарея: 80%\nРежим звонка: Normal"}
        self.assertTrue(self.service.handle_sms_reply(webhook, first, self.now + 10))
        self.assertEqual(self.repo.get_operation(1)["status"], "sms_reply_received")

        self.now += 11
        self.assertTrue(self.queue(target="off", key="second")["queued"])
        self.service.dispatch_one(self.now)
        second = {**first, "start_time": self.now + 10, "db_call_id": 102,
                  "text": "OFF\nУдаление выполнено успешно.\nБатарея: 79%\nРежим звонка: Normal"}
        self.assertTrue(self.service.handle_sms_reply(webhook, second, self.now + 10))
        self.assertEqual(self.repo.get_operation(2)["status"], "sms_reply_received")
        with self.repo.connect() as conn:
            self.assertEqual([row[0] for row in conn.execute(
                "SELECT operation_id FROM forwarding_sms_replies ORDER BY received_at"
            )], [1, 2])

    def test_unanswered_command_waits_at_most_two_minutes(self):
        self.assertTrue(self.queue()["queued"])
        self.sender.side_effect = requests.Timeout()
        self.service.dispatch_one(self.now)
        self.now += 119
        blocked = self.queue(key="early")
        self.assertEqual(blocked["reason"], "unconfirmed")
        self.assertEqual(blocked["retry_after"], 1)
        self.now += 1
        self.assertTrue(self.queue(key="after-two-minutes")["queued"])
        self.assertEqual(self.sender.call_count, 1)

    def test_web_command_unlocks_after_reply(self):
        with patch("forwarding.sms_service.utc_timestamp", return_value=self.now):
            self.assertTrue(self.service.queue_web("tecno", "off", "web-first")["queued"])
        self.service.dispatch_one(self.now)
        reply = {"direction": 0, "event_type": 32,
                 "client_number": "+998908456162", "start_time": self.now + 10,
                 "db_call_id": 104,
                 "text": "OFF\nУдаление выполнено успешно.\nБатарея: 10%\nРежим звонка: Normal"}
        self.assertTrue(self.service.handle_sms_reply(
            {"user_login": "texnikach@gmail.com"}, reply, self.now + 10))
        with patch("forwarding.sms_service.utc_timestamp", return_value=self.now + 11):
            self.assertTrue(self.service.queue_web("tecno", "redmi", "web-second")["queued"])

    def test_late_reply_after_manual_repeat_is_not_assigned_arbitrarily(self):
        self.assertTrue(self.queue()["queued"])
        self.sender.side_effect = requests.Timeout()
        self.service.dispatch_one(self.now)
        self.now += 120
        self.assertTrue(self.queue(key="manual-repeat")["queued"])
        self.sender.side_effect = None
        self.sender.return_value = {"success": True, "status": "SMS posted"}
        self.service.dispatch_one(self.now)
        late = {"direction": 0, "event_type": 32,
                "client_number": "+998908534466", "start_time": self.now + 1,
                "db_call_id": 103,
                "text": "ON Poco\nПереадресация выполнена\nБатарея: 78%\nРежим звонка: Normal"}
        self.assertTrue(self.service.handle_sms_reply(
            {"user_login": "texnikach@gmail.com"}, late, self.now + 1))
        with self.repo.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT operation_id FROM forwarding_sms_replies"
            ).fetchone()[0])
        self.assertEqual(self.repo.get_operation(2)["status"], "api_accepted")

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

    def test_telegram_http_error_keeps_description_not_token_url(self):
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"description":"Bad Request: message to unpin not found"}'
        api = Mock(side_effect=requests.HTTPError("secret-token-url", response=response))
        service = SMSForwardingService(repository=self.repo, settings=self.service.settings,
            chat_id=-100123, telegram_api=api, send_sms=self.sender)
        with self.assertRaisesRegex(RuntimeError, "message to unpin not found") as error:
            service.telegram_api("unpinChatMessage")
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
