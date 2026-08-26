from __future__ import annotations

import tempfile
import unittest
import asyncio
import base64
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from price_server.config import PriceSettings
from price_server.contracts import (
    canonical_content_hash,
    section_content_hash,
    validate_sync_payload,
)
from price_server.repository import PriceRepository, StaleSnapshotError
from price_server.scheduler import PriceScheduler
from price_server.service import PricePublicationService
from price_server.telegram_api import TelegramMessage
from fastapi import HTTPException
from starlette.requests import Request


class FakeTelegram:
    def __init__(self):
        self.next_id = 100
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[str, int, str]] = []
        self.deleted: list[tuple[str, int]] = []

    @staticmethod
    def _message(chat_id: str, message_id: int, html_text: str) -> TelegramMessage:
        import hashlib

        return TelegramMessage(
            message_id=message_id,
            chat_id=str(chat_id),
            post_url=f"https://t.me/testchannel/{message_id}",
            html=html_text,
            content_hash=hashlib.sha256(html_text.encode()).hexdigest(),
        )

    def send_message(self, chat_id, html_text):
        self.next_id += 1
        self.sent.append((str(chat_id), html_text))
        return self._message(str(chat_id), self.next_id, html_text)

    def edit_message(self, chat_id, message_id, html_text):
        self.edited.append((str(chat_id), int(message_id), html_text))
        return self._message(str(chat_id), int(message_id), html_text)

    def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), int(message_id)))
        return True


def snapshot(at: datetime, price: int = 100):
    section = {
        "position": 0,
        "section_key": "phones-test",
        "title": "Телефоны Test",
        "plain_text": f"**【 Телефоны Test 】**\nModel: {price}",
        "clipboard_html": (
            '<div>**【 Телефоны Test 】**</div>'
            f'<div><a href="https://t.me/test/1">Model</a>: {price}</div>'
        ),
        "telegram_blocks": [
            '<b>【 Телефоны Test 】</b>\n'
            f'<a href="https://t.me/test/1">Model</a>: {price}'
        ],
        "changed_recently": True,
    }
    section["content_hash"] = section_content_hash(section)
    payload = {
        "schema_version": 1,
        "generated_at": at.isoformat(),
        "timezone": "Asia/Tashkent",
        "html_document": f"<!doctype html><html><body>{price}</body></html>",
        "products": [{"product_id": 1, "price": price}],
        "sections": [section],
        "catalog_groups": [{"group_id": "phones"}],
    }
    payload["content_sha256"] = canonical_content_hash(payload)
    return validate_sync_payload(payload)


class PriceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = PriceRepository(Path(self.temp.name) / "price.db")
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_is_atomic_idempotent_and_rejects_stale(self):
        first = self.repo.ingest_snapshot(snapshot(self.now))
        duplicate = self.repo.ingest_snapshot(snapshot(self.now))
        self.assertTrue(first["created"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["snapshot_id"], duplicate["snapshot_id"])

        with self.assertRaises(StaleSnapshotError):
            self.repo.ingest_snapshot(
                snapshot(self.now - timedelta(minutes=1), price=90)
            )

    def test_send_edit_and_durable_job(self):
        self.repo.ingest_snapshot(snapshot(self.now))
        fake = FakeTelegram()
        settings = PriceSettings(
            enabled=True,
            db_path=Path(self.temp.name) / "price.db",
            legacy_html_path=Path(self.temp.name) / "legacy.html",
            admin_username="admin",
            admin_password="secret",
            sync_api_key="sync",
            telegram_bot_token="fake-token",
            telegram_channel_id="-1001234567890",
            telegram_channel_username="testchannel",
            product_sort_sheet_id="sheet",
            posts_sheet_name="Telegram Posts",
            timezone="Asia/Tashkent",
            scheduler_poll_seconds=1,
            sync_max_bytes=2_000_000,
        )
        service = PricePublicationService(
            settings,
            self.repo,
            telegram=fake,
        )
        sent = service.execute_job(
            {
                "action": "send",
                "section_key": "phones-test",
                "channel_id": settings.telegram_channel_id,
                "snapshot_policy": "latest",
            }
        )
        self.assertEqual(sent["message_ids"], [101])
        current = self.repo.list_telegram_posts(current_only=True)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["message_id"], 101)

        self.repo.ingest_snapshot(snapshot(self.now + timedelta(minutes=1), price=110))
        edited = service.execute_job(
            {
                "action": "edit",
                "section_key": "phones-test",
                "channel_id": settings.telegram_channel_id,
                "snapshot_policy": "latest",
            }
        )
        self.assertEqual(edited["message_ids"], [101])
        self.assertIn("110", fake.edited[-1][2])

        job = self.repo.enqueue_job(
            "phones-test",
            "send",
            self.now,
            channel_id=settings.telegram_channel_id,
        )
        claimed = self.repo.claim_due_jobs(self.now, limit=1)
        self.assertEqual(claimed[0]["job_id"], job["job_id"])
        self.assertTrue(
            self.repo.complete_job(
                job["job_id"],
                claimed[0]["lease_token"],
                self.now,
                {"ok": True},
            )
        )
        self.assertEqual(self.repo.get_job(job["job_id"])["status"], "done")
        self.assertGreaterEqual(len(self.repo.list_outbox()), 1)

        scheduled = self.repo.enqueue_job(
            "phones-test",
            "edit",
            self.now,
            channel_id=settings.telegram_channel_id,
        )
        service.sync_sheets_outbox = lambda **_: 0
        scheduler = PriceScheduler(
            settings,
            self.repo,
            service,
            clock=lambda: self.now,
        )
        self.assertEqual(asyncio.run(scheduler.run_once()), 1)
        self.assertEqual(self.repo.get_job(scheduled["job_id"])["status"], "done")

        cancellable = self.repo.enqueue_job(
            "phones-test",
            "send",
            self.now + timedelta(hours=1),
            channel_id=settings.telegram_channel_id,
        )
        self.assertTrue(self.repo.cancel_job(cancellable["job_id"], now=self.now))
        self.assertEqual(
            self.repo.get_job(cancellable["job_id"])["status"],
            "cancelled",
        )


class PricePageAuthTests(unittest.TestCase):
    def test_enabled_price_page_is_password_protected(self):
        module = importlib.import_module("price_server.router")
        with tempfile.TemporaryDirectory() as folder:
            legacy = Path(folder) / "index.html"
            legacy.write_text(
                "<!doctype html><html><body>price</body></html>",
                encoding="utf-8",
            )
            configured = PriceSettings(
                enabled=True,
                db_path=Path(folder) / "price.db",
                legacy_html_path=legacy,
                admin_username="admin",
                admin_password="secret",
                sync_api_key="sync",
                telegram_bot_token="",
                telegram_channel_id="",
                telegram_channel_username="",
                product_sort_sheet_id="sheet",
                posts_sheet_name="Telegram Posts",
                timezone="Asia/Tashkent",
                scheduler_poll_seconds=1,
                sync_max_bytes=2_000_000,
            )
            old = (
                module.settings,
                module._repository,
                module._service,
                module._startup_error,
            )
            module.settings = configured
            module._repository = None
            module._service = None
            module._startup_error = ""
            try:
                anonymous = Request(
                    {"type": "http", "method": "GET", "path": "/price", "headers": []}
                )
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(module.price_page(anonymous))
                self.assertEqual(raised.exception.status_code, 401)

                token = base64.b64encode(b"admin:secret").decode()
                authorized = Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/price",
                        "headers": [(b"authorization", f"Basic {token}".encode())],
                    }
                )
                response = asyncio.run(module.price_page(authorized))
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"/price/assets/admin.js", response.body)
                self.assertEqual(response.headers["x-robots-tag"], "noindex, nofollow, noarchive")
            finally:
                (
                    module.settings,
                    module._repository,
                    module._service,
                    module._startup_error,
                ) = old

    def test_sync_key_is_checked_before_stream_is_read(self):
        module = importlib.import_module("price_server.router")
        with tempfile.TemporaryDirectory() as folder:
            configured = PriceSettings(
                enabled=True,
                db_path=Path(folder) / "price.db",
                legacy_html_path=Path(folder) / "index.html",
                admin_username="admin",
                admin_password="secret",
                sync_api_key="correct-key",
                telegram_bot_token="",
                telegram_channel_id="",
                telegram_channel_username="",
                product_sort_sheet_id="sheet",
                posts_sheet_name="Telegram Posts",
                timezone="Asia/Tashkent",
                scheduler_poll_seconds=1,
                sync_max_bytes=2_000_000,
            )
            old = (
                module.settings,
                module._repository,
                module._service,
                module._startup_error,
            )
            module.settings = configured
            module._repository = None
            module._service = None
            module._startup_error = ""
            body_was_read = False

            async def forbidden_receive():
                nonlocal body_was_read
                body_was_read = True
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/price/api/v1/sync",
                    "headers": [(b"x-price-sync-key", b"wrong-key")],
                },
                forbidden_receive,
            )
            try:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(module.sync_price_snapshot(request))
                self.assertEqual(raised.exception.status_code, 403)
                self.assertFalse(body_was_read)

                raw = json.dumps(snapshot(datetime.now(timezone.utc))).encode()
                messages = [
                    {"type": "http.request", "body": raw[:100], "more_body": True},
                    {"type": "http.request", "body": raw[100:], "more_body": False},
                ]

                async def receive():
                    return messages.pop(0)

                authorized = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/price/api/v1/sync",
                        "headers": [(b"x-price-sync-key", b"correct-key")],
                    },
                    receive,
                )
                result = asyncio.run(module.sync_price_snapshot(authorized))
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["section_count"], 1)
            finally:
                (
                    module.settings,
                    module._repository,
                    module._service,
                    module._startup_error,
                ) = old


if __name__ == "__main__":
    unittest.main()
