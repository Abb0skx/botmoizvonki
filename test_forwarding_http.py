"""HTTP smoke checks for the forwarding dashboard; never send a real SMS."""

import unittest
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

_IMPORT_TMP = tempfile.TemporaryDirectory()
_IMPORT_DIR = Path(_IMPORT_TMP.name)
os.environ["DB_PATH"] = str(_IMPORT_DIR / "calls.db")
os.environ["INSTAGRAM_DB_PATH"] = str(_IMPORT_DIR / "instagram.db")
os.environ["REVIEWS_DB_PATH"] = str(_IMPORT_DIR / "reviews.db")
os.environ["BUSINESS_DB_PATH"] = str(_IMPORT_DIR / "business.db")
os.environ["PRODUCT_URLS_PATH"] = str(_IMPORT_DIR / "products.xlsx")
os.environ["TRANSCRIPTION_ENABLED"] = "false"

import botmoizvonki as bot


class ForwardingDashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.auth = mock.Mock()
        self.auth.principal.return_value = SimpleNamespace(role="admin")
        self.service = mock.Mock()
        self.service.states.return_value = [{"code": "poco", "status_label": "Ожидание"}]
        self.service.queue_web.return_value = {"queued": True, "reason": "queued"}
        self.patches.enter_context(mock.patch.object(
            bot, "monitoring_settings", SimpleNamespace(enabled=True)
        ))
        self.patches.enter_context(mock.patch.object(
            bot, "get_monitoring_auth", return_value=self.auth
        ))
        self.patches.enter_context(mock.patch.object(
            bot, "get_forwarding_service", return_value=self.service
        ))
        self.patches.enter_context(mock.patch.object(
            bot, "list_device_manager_assignments", return_value=[]
        ))
        self.patches.enter_context(mock.patch.object(
            bot, "forwarding_security_error", return_value=""
        ))
        self.send_sms = self.patches.enter_context(mock.patch.object(
            bot, "send_client_sms", side_effect=AssertionError("SMS must not be sent")
        ))
        self.client = TestClient(bot.app, base_url="https://bot.texnikach.uz")

    def test_get_shows_forwarding_state_without_sending_sms(self):
        response = self.client.get("/admin/device-managers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["forwarding"][0]["code"], "poco")
        self.service.states.assert_called_once_with()
        self.send_sms.assert_not_called()

    def test_post_queues_command_only_after_admin_and_csrf(self):
        response = self.client.post(
            "/admin/device-managers",
            json={"action": "forwarding", "source": "poco", "target": "redmi",
                  "request_id": "smoke-request-0001"},
            headers={"Origin": "https://bot.texnikach.uz"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["forwarding"]["queued"])
        self.service.queue_web.assert_called_once_with(
            "poco", "redmi", "smoke-request-0001"
        )
        self.assertGreaterEqual(self.auth.verify_csrf.call_count, 1)
        self.send_sms.assert_not_called()

    def test_post_rejects_missing_csrf_without_queueing(self):
        self.auth.verify_csrf.side_effect = bot.HTTPException(
            status_code=403, detail="csrf_failed"
        )
        response = self.client.post(
            "/admin/device-managers",
            json={"action": "forwarding", "source": "poco", "target": "redmi",
                  "request_id": "smoke-request-0002"},
            headers={"Origin": "https://bot.texnikach.uz"},
        )
        self.assertEqual(response.status_code, 403)
        self.service.queue_web.assert_not_called()
        self.send_sms.assert_not_called()


if __name__ == "__main__":
    unittest.main()
