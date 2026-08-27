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


class FakeTelegramDeleteError(RuntimeError):
    retryable = False


class FakeTelegram:
    def __init__(self):
        self.next_id = 100
        self.sent: list[tuple[str, str, dict]] = []
        self.edited: list[tuple[str, int, str]] = []
        self.deleted: list[tuple[str, int]] = []
        self.updates: list[dict] = []
        self.callback_answers: list[tuple[str, str, bool]] = []
        self.member_status = "administrator"
        self.delete_failures: dict[tuple[str, int], Exception] = {}

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

    def send_message(self, chat_id, html_text, **kwargs):
        self.next_id += 1
        self.sent.append((str(chat_id), html_text, kwargs))
        return self._message(str(chat_id), self.next_id, html_text)

    def edit_message(self, chat_id, message_id, html_text):
        self.edited.append((str(chat_id), int(message_id), html_text))
        return self._message(str(chat_id), int(message_id), html_text)

    def delete_message(self, chat_id, message_id):
        key = (str(chat_id), int(message_id))
        error = self.delete_failures.get(key)
        if error is not None:
            raise error
        self.deleted.append(key)
        return True

    def get_updates(self, **_kwargs):
        updates, self.updates = self.updates, []
        return updates

    def answer_callback_query(self, callback_query_id, *, text="", show_alert=False):
        self.callback_answers.append(
            (str(callback_query_id), str(text), bool(show_alert))
        )
        return True

    def get_chat_member(self, _chat_id, _user_id):
        return {"status": self.member_status}


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


def snapshot_with_sections(at: datetime, section_keys: list[str]):
    sections = []
    for position, section_key in enumerate(section_keys):
        section = {
            "position": position,
            "section_key": section_key,
            "title": section_key,
            "plain_text": f"**【 {section_key} 】**\nModel: 100",
            "clipboard_html": f"<div>{section_key}</div>",
            "telegram_blocks": [f"<b>{section_key}</b>\nModel: 100"],
            "changed_recently": False,
        }
        section["content_hash"] = section_content_hash(section)
        sections.append(section)
    payload = {
        "schema_version": 1,
        "generated_at": at.isoformat(),
        "timezone": "Asia/Tashkent",
        "html_document": "<!doctype html><html><body>calendar</body></html>",
        "products": [
            {"product_id": index + 1, "section_key": key}
            for index, key in enumerate(section_keys)
        ],
        "sections": sections,
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

        replacement = service.execute_job(
            {
                "action": "send",
                "section_key": "phones-test",
                "channel_id": settings.telegram_channel_id,
                "snapshot_policy": "latest",
            }
        )
        self.assertEqual(replacement["message_ids"], [102])
        deletions = self.repo.list_telegram_deletions()
        self.assertEqual(len(deletions), 1)
        self.assertEqual(deletions[0]["message_id"], 101)
        self.assertEqual(service.cleanup_superseded_posts(), 1)
        self.assertIn((settings.telegram_channel_id, 101), fake.deleted)
        self.assertEqual(
            self.repo.get_telegram_post(
                "phones-test", settings.telegram_channel_id
            )["message_id"],
            102,
        )
        self.assertEqual(
            self.repo.list_telegram_deletions()[0]["status"], "done"
        )

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

    def test_delayed_preview_window_cancel_button_and_post_index(self):
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
            telegram_preview_channel_id="-1003922029862",
        )
        service = PricePublicationService(
            settings,
            self.repo,
            telegram=fake,
        )
        job = self.repo.enqueue_job(
            "phones-test",
            "send",
            self.now + timedelta(hours=25),
            channel_id=settings.telegram_channel_id,
            now=self.now,
        )
        self.assertEqual(service.ensure_scheduled_previews(self.now), 0)
        self.assertEqual(fake.sent, [])

        entered_window = self.now + timedelta(hours=2)
        self.assertEqual(service.ensure_scheduled_previews(entered_window), 1)
        previews = self.repo.list_scheduled_previews(job_id=job["job_id"])
        self.assertEqual(len(previews), 1)
        self.assertEqual(fake.sent[0][0], settings.telegram_preview_channel_id)
        button = fake.sent[0][2]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["callback_data"], f"price_cancel:{job['job_id']}")

        index = self.repo.build_post_index_records(
            settings.telegram_channel_id,
            settings.telegram_preview_channel_id,
        )
        self.assertEqual(len(index), 1)
        self.assertFalse(index[0]["has_current_post"])
        self.assertEqual(index[0]["main_message_ids"], [])
        self.assertEqual(index[0]["preview_message_ids"], [101])

        fake.updates.append({
            "update_id": 10,
            "callback_query": {
                "id": "callback-1",
                "data": f"price_cancel:{job['job_id']}",
                "from": {"id": 55},
                "message": {
                    "message_id": 101,
                    "chat": {"id": int(settings.telegram_preview_channel_id)},
                },
            },
        })
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertEqual(self.repo.get_job(job["job_id"])["status"], "cancelled")
        self.assertIn((settings.telegram_preview_channel_id, 101), fake.deleted)
        self.assertEqual(
            self.repo.list_scheduled_previews(job_id=job["job_id"])[0]["status"],
            "cancelled",
        )
        self.assertEqual(fake.callback_answers[-1][1], "Публикация отменена")

    def test_permanent_delete_failure_requests_manual_cleanup(self):
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
            telegram_preview_channel_id="-1003922029862",
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        for _ in range(2):
            service.execute_job({
                "action": "send",
                "section_key": "phones-test",
                "channel_id": settings.telegram_channel_id,
                "snapshot_policy": "latest",
            })

        target = (settings.telegram_channel_id, 101)
        fake.delete_failures[target] = FakeTelegramDeleteError(
            "message can't be deleted"
        )
        self.assertEqual(service.cleanup_superseded_posts(), 0)
        deletion = self.repo.list_telegram_deletions()[0]
        self.assertEqual(deletion["status"], "failed")

        self.assertEqual(service.ensure_manual_deletion_requests(), 1)
        request_message_id = fake.next_id
        request = self.repo.get_manual_deletion_request(
            deletion["deletion_id"]
        )
        self.assertIsNotNone(request)
        self.assertEqual(request["request_message_id"], request_message_id)
        self.assertEqual(fake.sent[-1][0], settings.telegram_preview_channel_id)
        self.assertIn("https://t.me/testchannel/101", fake.sent[-1][1])
        button = fake.sent[-1][2]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(
            button["callback_data"],
            f"price_deleted:{deletion['deletion_id']}",
        )
        self.assertEqual(service.ensure_manual_deletion_requests(), 0)

        callback = {
            "callback_query": {
                "id": "manual-delete-1",
                "data": f"price_deleted:{deletion['deletion_id']}",
                "from": {"id": 55},
                "message": {
                    "message_id": request_message_id,
                    "chat": {"id": int(settings.telegram_preview_channel_id)},
                },
            },
        }
        fake.updates.append({"update_id": 20, **callback})
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertEqual(
            self.repo.get_manual_deletion_request(deletion["deletion_id"])[
                "status"
            ],
            "active",
        )
        self.assertIn("Сначала удалите", fake.callback_answers[-1][1])

        del fake.delete_failures[target]
        callback["callback_query"]["id"] = "manual-delete-2"
        fake.updates.append({"update_id": 21, **callback})
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertIn(target, fake.deleted)
        self.assertIn(
            (settings.telegram_preview_channel_id, request_message_id),
            fake.deleted,
        )
        self.assertEqual(
            self.repo.get_manual_deletion_request(deletion["deletion_id"])[
                "status"
            ],
            "completed",
        )
        self.assertEqual(
            self.repo.list_telegram_deletions()[0]["status"], "manual_done"
        )
        old_post = next(
            post for post in self.repo.list_telegram_posts(limit=20)
            if post["message_id"] == 101
        )
        self.assertEqual(old_post["status"], "deleted")

    def test_shared_legacy_post_aliases_are_retired_safely(self):
        self.repo.ingest_snapshot(
            snapshot_with_sections(self.now, ["group-one", "group-two"])
        )
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
        shared_id = 777
        for section_key in ("group-one", "group-two"):
            self.repo.upsert_telegram_post({
                "record_key": f"legacy:{section_key}:{shared_id}",
                "section_key": section_key,
                "section_name": section_key,
                "channel_id": settings.telegram_channel_id,
                "channel_username": settings.telegram_channel_username,
                "message_id": shared_id,
                "post_url": f"https://t.me/testchannel/{shared_id}",
                "publication_mode": "legacy_import",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })

        fake = FakeTelegram()
        service = PricePublicationService(settings, self.repo, telegram=fake)
        service.execute_job({
            "action": "edit",
            "section_key": "group-one",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })

        current_one = self.repo.list_telegram_posts(
            section_key="group-one", current_only=True
        )
        current_two = self.repo.list_telegram_posts(
            section_key="group-two", current_only=True
        )
        self.assertEqual([post["message_id"] for post in current_one], [shared_id])
        self.assertEqual(
            current_one[0]["record_key"], f"legacy:group-one:{shared_id}"
        )
        self.assertEqual(current_two, [])
        self.assertEqual(self.repo.list_telegram_deletions(), [])
        index = {
            row["section_key"]: row
            for row in self.repo.build_post_index_records(
                settings.telegram_channel_id
            )
        }
        self.assertEqual(index["group-one"]["main_message_ids"], [shared_id])
        self.assertEqual(index["group-two"]["main_message_ids"], [])

        self.repo.upsert_telegram_post({
            "record_key": f"legacy:group-two:{shared_id}",
            "section_key": "group-two",
            "section_name": "group-two",
            "channel_id": settings.telegram_channel_id,
            "channel_username": settings.telegram_channel_username,
            "message_id": shared_id,
            "post_url": f"https://t.me/testchannel/{shared_id}",
            "publication_mode": "legacy_import",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        })
        service.execute_job({
            "action": "send",
            "section_key": "group-one",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })

        self.assertEqual(
            self.repo.list_telegram_posts(
                section_key="group-two", current_only=True
            ),
            [],
        )
        deletions = self.repo.list_telegram_deletions()
        self.assertEqual(len(deletions), 1)
        self.assertEqual(deletions[0]["message_id"], shared_id)
        self.assertEqual(service.cleanup_superseded_posts(), 1)
        self.assertEqual(
            fake.deleted,
            [(settings.telegram_channel_id, shared_id)],
        )
        aliases = [
            post for post in self.repo.list_telegram_posts(limit=100)
            if post["message_id"] == shared_id
        ]
        self.assertEqual(len(aliases), 2)
        self.assertTrue(all(post["status"] == "deleted" for post in aliases))

    def test_preview_channel_service_message_is_deleted(self):
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
            telegram_preview_channel_id="-1003922029862",
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        fake.updates.append({
            "update_id": 11,
            "channel_post": {
                "message_id": 777,
                "chat": {"id": int(settings.telegram_preview_channel_id)},
                "pinned_message": {"message_id": 101},
            },
        })
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertIn((settings.telegram_preview_channel_id, 777), fake.deleted)

    def test_calendar_materializes_without_duplicate_manual_jobs(self):
        calendar_db = Path(self.temp.name) / "calendar.db"
        settings = PriceSettings(
            enabled=True,
            db_path=calendar_db,
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
        repo = PriceRepository(settings)
        now = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
        keys = [
            "wearables-xiaomi",
            "wearables-samsung",
            "wearables-iqibla",
        ]
        repo.ingest_snapshot(snapshot_with_sections(now, keys))
        execute_at = datetime(2026, 8, 27, 4, 30, tzinfo=timezone.utc)
        for section_key in keys[:2]:
            repo.enqueue_job(
                section_key,
                "send",
                execute_at,
                channel_id=settings.telegram_channel_id,
                now=now,
            )

        self.assertEqual(len(repo.list_calendar_plan()), 95)
        self.assertEqual(repo.materialize_due_schedules(now, horizon_days=1), 1)
        self.assertEqual(repo.materialize_due_schedules(now, horizon_days=1), 0)
        jobs = [job for job in repo.list_jobs(limit=20) if job["execute_at"] == execute_at.isoformat(timespec="microseconds")]
        self.assertEqual({job["section_key"] for job in jobs}, set(keys))
        iqibla = next(job for job in jobs if job["section_key"] == "wearables-iqibla")
        self.assertEqual(iqibla["payload"]["source"], "price_calendar")
        self.assertEqual(iqibla["payload"]["plan_day"], 27)

    def test_calendar_folds_missing_february_days_into_march_first(self):
        calendar_db = Path(self.temp.name) / "february-calendar.db"
        settings = PriceSettings(
            enabled=True,
            db_path=calendar_db,
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
        repo = PriceRepository(settings)
        plan = repo.list_calendar_plan()
        overflow_keys = sorted({
            row["section_key"]
            for row in plan
            if row["day_of_month"] in {1, 29, 30}
        })
        now = datetime(2027, 2, 28, 10, 0, tzinfo=timezone.utc)
        repo.ingest_snapshot(snapshot_with_sections(now, overflow_keys))
        self.assertEqual(repo.materialize_due_schedules(now, horizon_days=1), 12)
        jobs = repo.list_jobs(limit=50)
        self.assertEqual(len(jobs), 12)
        self.assertEqual(
            {job["payload"]["plan_day"] for job in jobs},
            {1, 29, 30},
        )

        leap_settings = PriceSettings(
            **{
                **settings.__dict__,
                "db_path": Path(self.temp.name) / "leap-calendar.db",
            }
        )
        leap_repo = PriceRepository(leap_settings)
        leap_plan = leap_repo.list_calendar_plan()
        leap_keys = sorted({
            row["section_key"]
            for row in leap_plan
            if row["day_of_month"] in {1, 30}
        })
        leap_now = datetime(2028, 2, 29, 10, 0, tzinfo=timezone.utc)
        leap_repo.ingest_snapshot(
            snapshot_with_sections(leap_now, leap_keys)
        )
        self.assertEqual(
            leap_repo.materialize_due_schedules(leap_now, horizon_days=1),
            9,
        )
        self.assertEqual(
            {
                job["payload"]["plan_day"]
                for job in leap_repo.list_jobs(limit=50)
            },
            {1, 30},
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
