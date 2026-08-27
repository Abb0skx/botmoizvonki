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
from price_server.quick_links import QUICK_LINK_POST_SPECS
from price_server.scheduler import PriceScheduler
from price_server.service import PricePublicationService
from price_server.sheets_registry import (
    QUICK_LINK_HEADERS,
    ProductSortQuickLinkRegistry,
)
from price_server.telegram_api import TelegramMessage, telegram_text_units
from fastapi import HTTPException
from starlette.requests import Request


class FakeTelegramDeleteError(RuntimeError):
    retryable = False


class FakeTelegramRetryableError(RuntimeError):
    retryable = True
    retry_after = 0


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
        self.edit_failures: dict[tuple[str, int], Exception] = {}

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

    def edit_message(self, chat_id, message_id, html_text, **_kwargs):
        error = self.edit_failures.get((str(chat_id), int(message_id)))
        if error is not None:
            raise error
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


def quick_link_spec(
    section_keys: list[str],
    *,
    message_id: int = 900,
) -> dict:
    links = []
    targets = []
    for index, section_key in enumerate(section_keys, start=1):
        links.append(
            f'<a href="{{{{post_url:{section_key}}}}}">{section_key}</a>'
        )
        targets.append({
            "link_key": section_key,
            "section_keys": [section_key],
            "fallback_url": f"https://t.me/testchannel/{700 + index}",
        })
    return {
        "quick_post_key": "quick-test",
        "title": "Test index",
        "message_id": message_id,
        "reconcile_on_install": True,
        "template_html": "\n".join(links),
        "targets": targets,
    }


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
        service.ensure_quick_link_registry()
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
        helper_message = (
            settings.telegram_preview_channel_id, request_message_id
        )
        fake.delete_failures[helper_message] = FakeTelegramRetryableError(
            "temporary helper cleanup failure"
        )
        callback["callback_query"]["id"] = "manual-delete-2"
        fake.updates.append({"update_id": 21, **callback})
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertIn(target, fake.deleted)
        self.assertNotIn(helper_message, fake.deleted)
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

        # SQLite completion happens before helper-message cleanup. A transient
        # Telegram error is therefore recoverable without recreating a request.
        del fake.delete_failures[helper_message]
        self.assertEqual(service.cleanup_completed_manual_deletion_requests(), 1)
        self.assertIn(helper_message, fake.deleted)
        self.assertEqual(
            self.repo.get_manual_deletion_request(deletion["deletion_id"])[
                "status"
            ],
            "removed",
        )
        self.assertEqual(service.cleanup_completed_manual_deletion_requests(), 0)

    def test_manual_delete_waits_for_quick_link_update(self):
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
        service.ensure_quick_link_registry()
        for _ in range(2):
            service.execute_job({
                "action": "send",
                "section_key": "phones-test",
                "channel_id": settings.telegram_channel_id,
                "snapshot_policy": "latest",
            })
        target = (settings.telegram_channel_id, 101)
        fake.delete_failures[target] = FakeTelegramDeleteError("too old")
        self.assertEqual(service.cleanup_superseded_posts(), 0)
        deletion = self.repo.list_telegram_deletions()[0]
        self.assertEqual(service.ensure_manual_deletion_requests(), 1)
        request = self.repo.get_manual_deletion_request(deletion["deletion_id"])

        spec = quick_link_spec(["phones-test"])
        spec["targets"][0]["initial_url"] = "https://t.me/testchannel/101"
        self.repo.ensure_quick_link_posts(
            [spec],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
        )
        request = self.repo.get_manual_deletion_request(deletion["deletion_id"])
        self.assertTrue(request["quick_link_blocked"])
        del fake.delete_failures[target]

        callback = {
            "callback_query": {
                "id": "blocked-delete",
                "data": f"price_deleted:{deletion['deletion_id']}",
                "from": {"id": 55},
                "message": {
                    "message_id": request["request_message_id"],
                    "chat": {"id": int(settings.telegram_preview_channel_id)},
                },
            },
        }
        fake.updates.append({"update_id": 30, **callback})
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertNotIn(target, fake.deleted)
        self.assertIn("быстрой ссылки", fake.callback_answers[-1][1])

        self.assertEqual(service.refresh_quick_link_posts(), 1)
        request = self.repo.get_manual_deletion_request(deletion["deletion_id"])
        self.assertFalse(request["quick_link_blocked"])
        callback["callback_query"]["id"] = "unblocked-delete"
        fake.updates.append({"update_id": 31, **callback})
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertIn(target, fake.deleted)
        self.assertEqual(
            self.repo.get_manual_deletion_request(deletion["deletion_id"])[
                "status"
            ],
            "removed",
        )

    def test_approved_quick_link_specs_validate_and_fit_telegram(self):
        fake = FakeTelegram()
        settings = PriceSettings(
            enabled=True,
            db_path=Path(self.temp.name) / "price.db",
            legacy_html_path=Path(self.temp.name) / "legacy.html",
            admin_username="admin",
            admin_password="secret",
            sync_api_key="sync",
            telegram_bot_token="fake-token",
            telegram_channel_id="-1001463992448",
            telegram_channel_username="texnikach",
            product_sort_sheet_id="sheet",
            posts_sheet_name="Telegram Posts",
            timezone="Asia/Tashkent",
            scheduler_poll_seconds=1,
            sync_max_bytes=2_000_000,
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        self.assertEqual(service.ensure_quick_link_registry(), 9)
        self.assertEqual(service.ensure_quick_link_registry(), 0)
        self.assertEqual(len(self.repo.list_quick_link_posts()), 9)

        for post in self.repo.list_quick_link_posts():
            resolved = self.repo.resolve_quick_link_post(
                post["quick_post_key"]
            )
            rendered, _targets = service._render_quick_link(resolved)
            self.assertNotIn("{{post_url:", rendered)
            self.assertNotIn("<tg-emoji", rendered)
            self.assertLessEqual(telegram_text_units(rendered), 4096)

        self.assertEqual(service.refresh_quick_link_posts(), 1)
        self.assertEqual(len(fake.edited), 1)
        self.assertEqual(fake.edited[0][1], 4882)
        self.assertTrue(all(
            row["status"] == "done"
            for row in self.repo.list_quick_link_updates()
        ))

    def test_approved_quick_link_manifest_is_exact(self):
        expected_posts = {
            "quick-index-catalog": 5050,
            "quick-index-smartphones": 4942,
            "quick-index-tablets": 4978,
            "quick-index-audio": 4905,
            "quick-index-wearables": 4882,
            "quick-index-photo": 4878,
            "quick-index-vr": 4869,
            "quick-index-home": 5033,
            "quick-index-charging": 5016,
        }
        expected_targets = {
            "apple-computers-all": 4965,
            "gaming-playstation-xbox": 4863,
            "dyson-hair": 5021,
            "voice-recorders-plaud": 4955,
            "storage-all": 4671,
            "accessories-combined": 4824,
            "smartphones-xiaomi-poco": 5031,
            "smartphones-samsung": 5037,
            "smartphones-iphone-air-17": 5041,
            "smartphones-iphone-13-16": 5042,
            "smartphones-honor-huawei": 4964,
            "smartphones-google-pixel": 5024,
            "smartphones-infinix": 4992,
            "smartphones-tecno": 4944,
            "smartphones-keypad": 4904,
            "tablets-apple": 4931,
            "tablets-samsung": 5022,
            "tablets-xiaomi": 5032,
            "tablets-honor-huawei": 4876,
            "audio-apple": 5039,
            "audio-samsung": 5045,
            "audio-xiaomi": 5036,
            "audio-sony": 4754,
            "audio-huawei-honor": 4868,
            "audio-jbl": 4956,
            "audio-nothing": 5040,
            "audio-marshall": 4960,
            "audio-anker": 5043,
            "audio-beats-dyson": 4958,
            "audio-shokz": 4812,
            "audio-yandex": 5046,
            "wearables-apple": 5048,
            "wearables-samsung": 5053,
            "wearables-xiaomi": 5054,
            "wearables-amazfit-haylou-mibro": 5029,
            "wearables-huawei": 4867,
            "wearables-nothing": 4822,
            "wearables-porodo": 5044,
            "wearables-whoop-fitbit": 5047,
            "wearables-iqibla": 5052,
            "photo-dji": 5049,
            "photo-hollyland": 4954,
            "photo-insta360": 4940,
            "photo-gopro": 4940,
            "photo-instax": 4939,
            "glasses-ray-ban-meta": 4753,
            "glasses-oakley-meta": 4635,
            "vr-meta-quest": 4929,
            "home-tv-boxes": 4820,
            "home-wifi": 4815,
            "home-cameras": 4821,
            "home-yandex-sensors": 4761,
            "home-vacuums": 4875,
            "home-air": 4874,
            "charging-adapters-cables": 4903,
            "charging-car": 4803,
            "charging-power-bank": 5038,
            "charging-stations": 4752,
        }
        self.assertEqual(
            {
                spec["quick_post_key"]: spec["message_id"]
                for spec in QUICK_LINK_POST_SPECS
            },
            expected_posts,
        )
        actual_targets = {}
        for spec in QUICK_LINK_POST_SPECS:
            for target in spec["targets"]:
                actual_targets[target["link_key"]] = int(
                    target["fallback_url"].rsplit("/", 1)[1]
                )
        self.assertEqual(actual_targets, expected_targets)
        wearables = next(
            spec for spec in QUICK_LINK_POST_SPECS
            if spec["quick_post_key"] == "quick-index-wearables"
        )
        observed = {
            target["link_key"]: int(target["initial_url"].rsplit("/", 1)[1])
            for target in wearables["targets"]
            if "initial_url" in target
        }
        self.assertEqual(observed, {
            "wearables-samsung": 5026,
            "wearables-xiaomi": 5028,
            "wearables-iqibla": 4758,
        })
        master = next(
            spec for spec in QUICK_LINK_POST_SPECS
            if spec["quick_post_key"] == "quick-index-catalog"
        )
        for message_id in (4942, 4978, 4905, 4882, 4878, 4869, 5033, 5016):
            self.assertIn(
                f'https://t.me/texnikach/{message_id}',
                master["template_html"],
            )

    def test_quick_link_sheet_upserts_nine_stable_rows(self):
        settings = PriceSettings(
            enabled=True,
            db_path=Path(self.temp.name) / "price.db",
            legacy_html_path=Path(self.temp.name) / "legacy.html",
            admin_username="admin",
            admin_password="secret",
            sync_api_key="sync",
            telegram_bot_token="fake-token",
            telegram_channel_id="-1001463992448",
            telegram_channel_username="texnikach",
            product_sort_sheet_id="sheet",
            posts_sheet_name="Telegram Posts",
            timezone="Asia/Tashkent",
            scheduler_poll_seconds=1,
            sync_max_bytes=2_000_000,
        )
        self.repo.ensure_quick_link_posts(
            QUICK_LINK_POST_SPECS,
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
        )
        records = self.repo.build_quick_link_registry_records()

        class FakeWorksheet:
            def __init__(self):
                self.values = []
                self.requests = []

            def get_all_values(self):
                return self.values

            def batch_update(self, requests, *, value_input_option):
                self.requests = list(requests)
                self.value_input_option = value_input_option

        worksheet = FakeWorksheet()
        registry = ProductSortQuickLinkRegistry(settings)
        registry._worksheet = lambda: worksheet
        self.assertEqual(registry.upsert(records), 9)
        self.assertEqual(worksheet.value_input_option, "RAW")
        self.assertEqual(
            worksheet.requests[0]["values"], [list(QUICK_LINK_HEADERS)]
        )
        row_requests = worksheet.requests[1:]
        self.assertEqual(len(row_requests), 9)
        self.assertEqual(
            {request["values"][0][0] for request in row_requests},
            {record["quick_post_key"] for record in records},
        )

        worksheet.values = [list(QUICK_LINK_HEADERS)] + [
            request["values"][0] for request in row_requests
        ]
        self.assertEqual(registry.upsert(records), 9)
        self.assertEqual(len(worksheet.requests), 9)
        self.assertNotIn(
            "A11:Q11",
            {request["range"] for request in worksheet.requests},
        )

    def test_combined_quick_link_uses_first_available_section(self):
        spec = {
            "quick_post_key": "quick-combined",
            "title": "Combined",
            "message_id": 900,
            "template_html": '<a href="{{post_url:combined}}">Combined</a>',
            "targets": [{
                "link_key": "combined",
                "section_keys": ["group-one", "group-two"],
                "fallback_url": "https://t.me/testchannel/700",
            }],
        }
        channel_id = "-1001234567890"
        self.repo.ensure_quick_link_posts(
            [spec],
            channel_id=channel_id,
            channel_username="testchannel",
        )
        self.repo.upsert_telegram_post({
            "record_key": "group-two:802",
            "publication_id": "group-two-publication",
            "section_key": "group-two",
            "section_name": "Group two",
            "channel_id": channel_id,
            "channel_username": "testchannel",
            "message_id": 802,
            "post_url": "https://t.me/testchannel/802",
            "sent_at": self.now.isoformat(),
            "is_current": True,
        })
        resolved = self.repo.resolve_quick_link_post("quick-combined")
        self.assertEqual(
            resolved["resolved_targets"][0]["target_message_id"], 802
        )
        self.repo.upsert_telegram_post({
            "record_key": "group-one:801",
            "publication_id": "group-one-publication",
            "section_key": "group-one",
            "section_name": "Group one",
            "channel_id": channel_id,
            "channel_username": "testchannel",
            "message_id": 801,
            "post_url": "https://t.me/testchannel/801",
            "sent_at": self.now.isoformat(),
            "is_current": True,
        })
        resolved = self.repo.resolve_quick_link_post("quick-combined")
        self.assertEqual(
            resolved["resolved_targets"][0]["target_message_id"], 801
        )

    def test_quick_link_updates_before_superseded_post_deletion(self):
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
        self.repo.upsert_telegram_post({
            "record_key": "legacy:phones-test:701",
            "section_key": "phones-test",
            "section_name": "Phones",
            "channel_id": settings.telegram_channel_id,
            "channel_username": settings.telegram_channel_username,
            "message_id": 701,
            "post_url": "https://t.me/testchannel/701",
            "publication_mode": "legacy_import",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        })
        spec = quick_link_spec(["phones-test"])
        spec["reconcile_on_install"] = False
        spec["targets"][0]["initial_url"] = "https://t.me/testchannel/701"
        self.repo.ensure_quick_link_posts(
            [spec],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        self.assertEqual(service.refresh_quick_link_posts(), 0)

        service.execute_job({
            "action": "send",
            "section_key": "phones-test",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        self.assertEqual(service.cleanup_superseded_posts(), 0)
        self.assertNotIn((settings.telegram_channel_id, 701), fake.deleted)

        self.assertEqual(service.refresh_quick_link_posts(), 1)
        self.assertIn("/101", fake.edited[-1][2])
        self.assertEqual(service.cleanup_superseded_posts(), 1)
        self.assertIn((settings.telegram_channel_id, 701), fake.deleted)

        revision = self.repo.list_quick_link_updates()[0]["desired_revision"]
        service.execute_job({
            "action": "edit",
            "section_key": "phones-test",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        self.assertEqual(
            self.repo.list_quick_link_updates()[0]["desired_revision"],
            revision,
        )

    def test_quick_link_retry_never_republishes_price_job(self):
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
        self.repo.ensure_quick_link_posts(
            [quick_link_spec(["phones-test"])],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        fake.edit_failures[(settings.telegram_channel_id, 900)] = (
            FakeTelegramRetryableError("temporary edit failure")
        )
        job = self.repo.enqueue_job(
            "phones-test",
            "send",
            self.now,
            channel_id=settings.telegram_channel_id,
        )
        service.ensure_quick_link_registry = lambda: 0
        service.sync_sheets_outbox = lambda **_: 0
        scheduler = PriceScheduler(
            settings,
            self.repo,
            service,
            clock=lambda: self.now,
        )
        self.assertEqual(asyncio.run(scheduler.run_once()), 1)
        self.assertEqual(self.repo.get_job(job["job_id"])["status"], "done")
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(
            self.repo.list_quick_link_updates()[0]["status"], "pending"
        )

        self.assertEqual(asyncio.run(scheduler.run_once()), 0)
        self.assertEqual(len(fake.sent), 1)
        del fake.edit_failures[(settings.telegram_channel_id, 900)]
        self.assertEqual(asyncio.run(scheduler.run_once()), 0)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(
            self.repo.list_quick_link_updates()[0]["status"], "done"
        )
        self.assertIn("/101", fake.edited[-1][2])

    def test_multiple_publications_coalesce_into_latest_quick_link_html(self):
        keys = ["group-one", "group-two"]
        self.repo.ingest_snapshot(snapshot_with_sections(self.now, keys))
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
        self.repo.ensure_quick_link_posts(
            [quick_link_spec(keys)],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        for section_key in keys:
            service.execute_job({
                "action": "send",
                "section_key": section_key,
                "channel_id": settings.telegram_channel_id,
                "snapshot_policy": "latest",
            })
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        rendered = fake.edited[-1][2]
        self.assertIn("/101", rendered)
        self.assertIn("/102", rendered)
        registry = self.repo.build_quick_link_registry_records()[0]
        self.assertEqual(registry["message_id"], 900)
        self.assertEqual(
            set(registry["linked_section_keys"]), set(keys)
        )

    def test_quick_link_blocks_every_part_until_edit_succeeds(self):
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
        old_parts = []
        for part_no, message_id in enumerate((701, 702), start=1):
            old_parts.append({
                "record_key": f"old:{message_id}",
                "publication_id": "old-publication",
                "section_key": "phones-test",
                "section_name": "Phones",
                "channel_id": settings.telegram_channel_id,
                "channel_username": settings.telegram_channel_username,
                "part_no": part_no,
                "part_count": 2,
                "message_id": message_id,
                "post_url": f"https://t.me/testchannel/{message_id}",
                "publication_mode": "legacy_import",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })
        self.repo.replace_current_telegram_posts(
            "phones-test", settings.telegram_channel_id, old_parts
        )
        spec = quick_link_spec(["phones-test"])
        spec["reconcile_on_install"] = False
        spec["targets"][0]["initial_url"] = "https://t.me/testchannel/701"
        self.repo.ensure_quick_link_posts(
            [spec],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        self.assertEqual(service.refresh_quick_link_posts(), 0)

        new_parts = []
        for part_no, message_id in enumerate((801, 802), start=1):
            new_parts.append({
                "record_key": f"new:{message_id}",
                "publication_id": "new-publication",
                "section_key": "phones-test",
                "section_name": "Phones",
                "channel_id": settings.telegram_channel_id,
                "channel_username": settings.telegram_channel_username,
                "part_no": part_no,
                "part_count": 2,
                "message_id": message_id,
                "post_url": f"https://t.me/testchannel/{message_id}",
                "publication_mode": "send",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })
        self.repo.replace_current_telegram_posts(
            "phones-test", settings.telegram_channel_id, new_parts
        )
        fake.edit_failures[(settings.telegram_channel_id, 900)] = (
            FakeTelegramRetryableError("temporary edit failure")
        )
        self.assertEqual(service.refresh_quick_link_posts(), 0)
        self.assertEqual(service.cleanup_superseded_posts(), 0)
        self.assertNotIn((settings.telegram_channel_id, 701), fake.deleted)
        self.assertNotIn((settings.telegram_channel_id, 702), fake.deleted)

        del fake.edit_failures[(settings.telegram_channel_id, 900)]
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        self.assertIn("/801", fake.edited[-1][2])
        self.assertEqual(service.cleanup_superseded_posts(), 2)
        self.assertIn((settings.telegram_channel_id, 701), fake.deleted)
        self.assertIn((settings.telegram_channel_id, 702), fake.deleted)

    def test_new_quick_link_revision_survives_last_attempt_failure(self):
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
        self.repo.ensure_quick_link_posts(
            [quick_link_spec(["phones-test"])],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
            now=self.now,
        )
        for _ in range(11):
            task = self.repo.claim_quick_link_updates(self.now)[0]
            self.assertTrue(self.repo.retry_quick_link_update(
                task["quick_post_key"],
                task["lease_token"],
                self.now,
                "temporary",
                retry_after_seconds=0,
            ))
        task = self.repo.claim_quick_link_updates(self.now)[0]
        self.assertEqual(task["attempts"], 12)
        self.repo.replace_current_telegram_posts(
            "phones-test",
            settings.telegram_channel_id,
            [{
                "record_key": "new:901",
                "publication_id": "newer-publication",
                "section_key": "phones-test",
                "section_name": "Phones",
                "channel_id": settings.telegram_channel_id,
                "channel_username": settings.telegram_channel_username,
                "message_id": 901,
                "post_url": "https://t.me/testchannel/901",
                "publication_mode": "send",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            }],
            now=self.now,
        )
        self.assertTrue(self.repo.retry_quick_link_update(
            task["quick_post_key"],
            task["lease_token"],
            self.now,
            "old revision failed permanently",
            permanent=True,
        ))
        queued = self.repo.list_quick_link_updates()[0]
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(queued["attempts"], 0)
        self.assertGreater(
            queued["desired_revision"], queued["claimed_revision"]
        )

    def test_inflight_quick_edit_blocks_next_publication_deletion(self):
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

        def publish(message_id: int, publication_id: str) -> None:
            self.repo.replace_current_telegram_posts(
                "phones-test",
                settings.telegram_channel_id,
                [{
                    "record_key": f"post:{message_id}",
                    "publication_id": publication_id,
                    "section_key": "phones-test",
                    "section_name": "Phones",
                    "channel_id": settings.telegram_channel_id,
                    "channel_username": settings.telegram_channel_username,
                    "message_id": message_id,
                    "post_url": f"https://t.me/testchannel/{message_id}",
                    "sent_at": self.now.isoformat(),
                    "is_current": True,
                }],
                now=self.now,
            )

        publish(101, "publication-a")
        spec = quick_link_spec(["phones-test"])
        spec["targets"][0]["initial_url"] = "https://t.me/testchannel/101"
        self.repo.ensure_quick_link_posts(
            [spec],
            channel_id=settings.telegram_channel_id,
            channel_username=settings.telegram_channel_username,
            now=self.now,
        )
        service = PricePublicationService(settings, self.repo, telegram=fake)
        self.assertEqual(service.refresh_quick_link_posts(), 1)

        publish(201, "publication-b")
        inflight = self.repo.claim_quick_link_updates(self.now)[0]
        resolved = self.repo.resolve_quick_link_post("quick-test")
        rendered, _ = service._render_quick_link(resolved)
        fake.edit_message(
            settings.telegram_channel_id, 900, rendered
        )
        self.assertIn("/201", fake.edited[-1][2])

        # Telegram now points to B, but SQLite still records A. Publishing C
        # must not allow B to be deleted while the edit lease is unresolved.
        publish(301, "publication-c")
        self.assertEqual(service.cleanup_superseded_posts(), 0)
        self.assertNotIn((settings.telegram_channel_id, 201), fake.deleted)

        self.assertTrue(self.repo.retry_quick_link_update(
            inflight["quick_post_key"],
            inflight["lease_token"],
            self.now,
            "worker restarted before commit",
            retry_after_seconds=0,
        ))
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        self.assertIn("/301", fake.edited[-1][2])
        self.assertEqual(service.cleanup_superseded_posts(), 2)
        self.assertIn((settings.telegram_channel_id, 101), fake.deleted)
        self.assertIn((settings.telegram_channel_id, 201), fake.deleted)

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
