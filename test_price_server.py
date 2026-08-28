from __future__ import annotations

import tempfile
import unittest
import asyncio
import base64
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from price_server.config import PriceSettings
from price_server.contracts import (
    canonical_content_hash,
    section_content_hash,
    validate_sync_payload,
)
from price_server.repository import (
    IdempotencyConflictError,
    PriceRepository,
    QuickLinkRotationConflictError,
    StaleSnapshotError,
)
from price_server.quick_links import (
    CATALOG_DATE_UPDATE_TIME,
    CATALOG_QUICK_POST_KEY,
    QUICK_LINK_POST_SPECS,
    QUICK_LINK_ROTATION_ORDER,
)
from price_server.post_formatting import (
    PRICE_INFO_RU_HTML,
    PRICE_INFO_UZ_HTML,
    format_price_sections,
)
from price_server.scheduler import PriceScheduler
from price_server.service import PricePublicationService
from price_server.sheets_registry import (
    QUICK_LINK_HEADERS,
    QUICK_LINK_ROTATION_HEADERS,
    ProductSortQuickLinkRegistry,
    ProductSortQuickLinkRotationRegistry,
)
from price_server.telegram_api import (
    TelegramAPIError,
    TelegramClient,
    TelegramMessage,
    telegram_text_units,
    telegram_visible_text,
)
from fastapi import HTTPException
from starlette.requests import Request


class FakeTelegramDeleteError(RuntimeError):
    retryable = False


class FakeTelegramRetryableError(RuntimeError):
    retryable = True
    retry_after = 0


class FakeTelegramAmbiguousError(RuntimeError):
    retryable = False
    ambiguous = True


class FakeTelegram:
    def __init__(self):
        self.next_id = 100
        self.sent: list[tuple[str, str, dict]] = []
        self.edited: list[tuple[str, int, str]] = []
        self.deleted: list[tuple[str, int]] = []
        self.pinned: list[tuple[str, int, bool]] = []
        self.unpinned: list[tuple[str, int]] = []
        self.updates: list[dict] = []
        self.callback_answers: list[tuple[str, str, bool]] = []
        self.member_status = "administrator"
        self.delete_failures: dict[tuple[str, int], Exception] = {}
        self.edit_failures: dict[tuple[str, int], Exception] = {}
        self.pin_failures: dict[tuple[str, int], Exception] = {}
        self.unpin_failures: dict[tuple[str, int], Exception] = {}
        self.send_failure: Exception | None = None

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
        if self.send_failure is not None:
            raise self.send_failure
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

    def pin_message(self, chat_id, message_id, *, disable_notification=True):
        key = (str(chat_id), int(message_id))
        error = self.pin_failures.get(key)
        if error is not None:
            raise error
        self.pinned.append((*key, bool(disable_notification)))
        return True

    def unpin_message(self, chat_id, message_id):
        key = (str(chat_id), int(message_id))
        error = self.unpin_failures.get(key)
        if error is not None:
            raise error
        self.unpinned.append(key)
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

    def rotation_fixture(self, db_name="rotation.db"):
        settings = PriceSettings(
            enabled=True,
            db_path=Path(self.temp.name) / db_name,
            legacy_html_path=Path(self.temp.name) / "legacy.html",
            admin_username="admin",
            admin_password="secret",
            sync_api_key="sync",
            telegram_bot_token="fake-token",
            telegram_channel_id="-1001463992448",
            telegram_channel_username="testchannel",
            product_sort_sheet_id="sheet",
            posts_sheet_name="Telegram Posts",
            timezone="Asia/Tashkent",
            scheduler_poll_seconds=1,
            sync_max_bytes=2_000_000,
            telegram_preview_channel_id="-1003922029862",
        )
        repo = PriceRepository(settings)
        fake = FakeTelegram()
        service = PricePublicationService(settings, repo, telegram=fake)
        self.assertEqual(service.ensure_quick_link_registry(), 9)
        self.assertEqual(service.refresh_quick_link_posts(), 9)
        fake.edited.clear()
        return settings, repo, fake, service

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

    def test_price_post_format_is_canonical_and_idempotent(self):
        raw = (
            '📌 <a href="https://t.me/Texnikach_info">Do‘kon</a> | '
            '📞 <a href="https://t.me/Texnikach_info">Aloqa</a> | '
            '📦 <a href="https://t.me/Texnikach_info">Yetkazish</a>\n\n'
            '<b>【 Телефоны Infinix 】</b>\n'
            '<a href="https://t.me/catalog/3/64gb">Infinix Smart 10</a>\n'
            '• 3/64Gb Black, Silver: 130\n'
            '• 4/128Gb : 205 🔺\n'
            '• Black: 120 🔻\n'
            'Product aloqa special yetkazish edition: 100\n'
            '【Limited】 Edition: 100\n\n'
            '📌 <a href="https://t.me/Texnikach_info">Магазин</a> │ '
            '📞 <a href="https://t.me/Texnikach_info">Связь</a> │ '
            '📦 <a href="https://t.me/Texnikach_info">Доставка</a>'
        )
        rendered = format_price_sections([
            ("smartphones-infinix", "Телефоны Infinix", [raw])
        ])
        self.assertEqual(len(rendered), 1)
        message = rendered[0]
        self.assertTrue(message.startswith(PRICE_INFO_UZ_HTML + "\n\n"))
        self.assertTrue(message.endswith("\n\n" + PRICE_INFO_RU_HTML))
        self.assertEqual(message.count("https://texnikach.uz/go"), 2)
        self.assertIn("<b>━━ ТЕЛЕФОНЫ · INFINIX ━━</b>", message)
        self.assertIn("• 3/64 GB · Black, Silver — <b>130</b>", message)
        self.assertIn("• 4/128 GB — <b>205</b> ↑", message)
        self.assertIn("• Black — <b>120</b> ↓", message)
        self.assertIn('href="https://t.me/catalog/3/64gb"', message)
        self.assertIn(
            "Product aloqa special yetkazish edition — <b>100</b>",
            message,
        )
        self.assertIn("【Limited】 Edition — <b>100</b>", message)
        self.assertNotIn("Texnikach_info", message)
        self.assertNotIn("🔺", message)
        self.assertNotIn("🔻", message)
        self.assertLessEqual(telegram_text_units(message), 4096)
        self.assertEqual(
            format_price_sections([
                ("smartphones-infinix", "Телефоны Infinix", [message])
            ]),
            rendered,
        )
        self.assertEqual(
            format_price_sections([
                ("smartphones-infinix", "Телефоны · Infinix", [message])
            ]),
            rendered,
        )
        self.assertNotIn("· ·", message)

    def test_price_post_format_compacts_long_heading_and_bolds_prices(self):
        raw = (
            "<b>【 Dyson — фены и стайлеры 】</b>\n"
            "Dyson Supersonic Nural HD16\n"
            "• Prussian Blue/Rich Copper: 330\n"
            "• Ceramic Apricot/Topaz: 1 200,50 🔺"
        )
        rendered = format_price_sections([
            ("dyson-hair", "Dyson — фены и стайлеры", [raw])
        ])
        self.assertEqual(len(rendered), 1)
        message = rendered[0]
        self.assertIn("<b>▰ DYSON — ФЕНЫ И СТАЙЛЕРЫ</b>", message)
        self.assertNotIn("━━ DYSON", message)
        self.assertIn("• Prussian Blue/Rich Copper — <b>330</b>", message)
        self.assertIn(
            "• Ceramic Apricot/Topaz — <b>1 200,50</b> ↑",
            message,
        )
        self.assertEqual(
            format_price_sections([
                ("dyson-hair", "Dyson — фены и стайлеры", [message])
            ]),
            rendered,
        )

    def test_compact_transformed_heading_remains_idempotent(self):
        raw = (
            "<b>【 Телефоны Xiaomi, Redmi, Poco 】</b>\n"
            "Poco Test\n"
            "• 8/256Gb Black: 300"
        )
        rendered = format_price_sections([
            (
                "smartphones-xiaomi-poco",
                "Телефоны Xiaomi, Redmi, Poco",
                [raw],
            )
        ])
        message = rendered[0]
        self.assertIn(
            "<b>▰ ТЕЛЕФОНЫ · XIAOMI, REDMI, POCO</b>",
            message,
        )
        self.assertEqual(
            format_price_sections([
                (
                    "smartphones-xiaomi-poco",
                    "Телефоны Xiaomi, Redmi, Poco",
                    [message],
                )
            ]),
            rendered,
        )

    def test_price_post_format_splits_one_oversized_html_block_safely(self):
        long_text = "x" * 4050
        raw = (
            "<b>【 Long 】</b>\n"
            f'<a href="https://example.test/long">{long_text}</a>'
        )
        rendered = format_price_sections([("long", "Long", [raw])])
        self.assertGreater(len(rendered), 1)
        self.assertEqual(
            sum(telegram_visible_text(item).count("x") for item in rendered),
            len(long_text),
        )
        self.assertTrue(all(
            telegram_text_units(item) <= 4096 for item in rendered
        ))
        self.assertTrue(all(
            item.count('<a href="https://example.test/long">') == 1
            for item in rendered
        ))
        self.assertTrue(all(item.count("</a>") >= 3 for item in rendered))
        self.assertTrue(all(
            item.startswith(PRICE_INFO_UZ_HTML + "\n\n")
            and item.endswith("\n\n" + PRICE_INFO_RU_HTML)
            for item in rendered
        ))

    def test_edit_all_groups_shared_posts_and_runs_sequentially(self):
        self.repo.ingest_snapshot(snapshot_with_sections(
            self.now,
            ["group-one", "group-two", "solo"],
        ))
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
        for section_key, message_id in (
            ("group-one", 777),
            ("group-two", 777),
            ("solo", 778),
        ):
            self.repo.upsert_telegram_post({
                "record_key": f"legacy:{section_key}:{message_id}",
                "publication_id": f"publication:{section_key}",
                "section_key": section_key,
                "section_name": section_key,
                "channel_id": settings.telegram_channel_id,
                "channel_username": settings.telegram_channel_username,
                "message_id": message_id,
                "post_url": f"https://t.me/testchannel/{message_id}",
                "publication_mode": "legacy_import",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })

        batch_id = "01234567-89ab-4cde-8fab-0123456789ab"
        batch = self.repo.enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key=batch_id,
            now=self.now,
        )
        self.assertEqual(batch["job_count"], 2)
        self.assertEqual(batch["section_count"], 3)
        self.assertEqual(
            batch["jobs"][0]["payload"]["section_keys"],
            ["group-one", "group-two"],
        )
        self.assertEqual(
            batch["jobs"][0]["payload"]["expected_message_ids"],
            [777],
        )
        self.assertEqual(
            {job["snapshot_policy"] for job in batch["jobs"]},
            {"pinned"},
        )
        replay = self.repo.enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key=batch_id,
            now=self.now,
        )
        self.assertTrue(replay["duplicate"])
        self.assertEqual(
            [job["job_id"] for job in replay["jobs"]],
            [job["job_id"] for job in batch["jobs"]],
        )

        fake = FakeTelegram()
        service = PricePublicationService(settings, self.repo, telegram=fake)
        first = self.repo.claim_due_jobs(self.now, limit=20)
        self.assertEqual(len(first), 1)
        self.assertEqual(
            first[0]["payload"]["batch_position"],
            1,
        )
        result = service.execute_job(first[0])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(fake.edited[0][1], 777)
        self.assertIn("GROUP-ONE", fake.edited[0][2])
        self.assertIn("GROUP-TWO", fake.edited[0][2])
        self.assertTrue(self.repo.complete_job(
            first[0]["job_id"],
            first[0]["lease_token"],
            self.now,
            result,
        ))
        self.assertEqual(self.repo.claim_due_jobs(self.now, limit=20), [])

        second_at = self.now + timedelta(seconds=2)
        second = self.repo.claim_due_jobs(second_at, limit=20)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["payload"]["batch_position"], 2)
        result = service.execute_job(second[0])
        self.assertEqual(result["status"], "updated")
        self.assertTrue(self.repo.complete_job(
            second[0]["job_id"],
            second[0]["lease_token"],
            second_at,
            result,
        ))
        self.assertEqual([item[1] for item in fake.edited], [777, 778])
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.deleted, [])
        for section_key in ("group-one", "group-two"):
            current = self.repo.list_telegram_posts(
                section_key=section_key,
                current_only=True,
            )
            self.assertEqual([item["message_id"] for item in current], [777])

    def test_edit_all_skips_when_current_binding_changes(self):
        self.repo.ingest_snapshot(
            snapshot_with_sections(self.now, ["group-one"])
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
        old = {
            "record_key": "legacy:group-one:701",
            "publication_id": "publication-old",
            "section_key": "group-one",
            "section_name": "group-one",
            "channel_id": settings.telegram_channel_id,
            "channel_username": settings.telegram_channel_username,
            "message_id": 701,
            "post_url": "https://t.me/testchannel/701",
            "publication_mode": "legacy_import",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        }
        self.repo.upsert_telegram_post(old)
        batch = self.repo.enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key="fedcba98-7654-4cba-8765-fedcba987654",
            now=self.now,
        )
        old["is_current"] = False
        old["status"] = "superseded"
        self.repo.upsert_telegram_post(old)
        self.repo.upsert_telegram_post({
            **old,
            "record_key": "legacy:group-one:702",
            "publication_id": "publication-new",
            "message_id": 702,
            "post_url": "https://t.me/testchannel/702",
            "status": "published",
            "is_current": True,
        })

        fake = FakeTelegram()
        service = PricePublicationService(settings, self.repo, telegram=fake)
        result = service.execute_job(batch["jobs"][0])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "current_binding_changed")
        self.assertEqual(fake.edited, [])
        self.assertEqual(fake.sent, [])

    def test_edit_all_zero_job_replay_preserves_original_decision(self):
        self.repo.ingest_snapshot(
            snapshot_with_sections(self.now, ["group-one"])
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
        self.repo.upsert_telegram_post({
            "record_key": "legacy:group-one:701",
            "section_key": "group-one",
            "section_name": "group-one",
            "channel_id": settings.telegram_channel_id,
            "channel_username": settings.telegram_channel_username,
            "message_id": 701,
            "post_url": "https://t.me/testchannel/701",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        })
        active = self.repo.enqueue_job(
            "group-one",
            "edit",
            self.now,
            channel_id=settings.telegram_channel_id,
            now=self.now,
        )
        batch_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        first = self.repo.enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key=batch_id,
            now=self.now,
        )
        self.assertEqual(first["job_count"], 0)
        self.assertEqual(first["section_count"], 0)
        self.assertEqual(first["skipped"], {
            "active_job": ["group-one"],
        })

        self.assertTrue(self.repo.cancel_job(active["job_id"], now=self.now))
        replay = self.repo.enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key=batch_id,
            now=self.now + timedelta(seconds=1),
        )
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["job_count"], 0)
        self.assertEqual(replay["section_count"], 0)
        self.assertEqual(replay["skipped"], first["skipped"])
        self.assertEqual(replay["jobs"], [])
        history = self.repo.list_publication_edit_batches()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["batch_id"], batch_id)
        self.assertEqual(history[0]["job_count"], 0)
        self.assertEqual(history[0]["skipped"], first["skipped"])
        with self.assertRaises(IdempotencyConflictError):
            self.repo.enqueue_current_post_edit_batch(
                channel_id="-1009999999999",
                channel_key="otherchannel",
                idempotency_key=batch_id,
                now=self.now + timedelta(seconds=2),
            )

    def test_claim_due_jobs_locks_all_bulk_section_aliases(self):
        self.repo.ingest_snapshot(
            snapshot_with_sections(self.now, ["group-one", "group-two"])
        )
        channel_id = "-1001234567890"
        for section_key in ("group-one", "group-two"):
            self.repo.upsert_telegram_post({
                "record_key": f"legacy:{section_key}:777",
                "section_key": section_key,
                "section_name": section_key,
                "channel_id": channel_id,
                "channel_username": "testchannel",
                "message_id": 777,
                "post_url": "https://t.me/testchannel/777",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })
        batch = self.repo.enqueue_current_post_edit_batch(
            channel_id=channel_id,
            channel_key="testchannel",
            idempotency_key="11111111-2222-4333-8444-555555555555",
            now=self.now,
        )
        competing = self.repo.enqueue_job(
            "group-two",
            "edit",
            self.now,
            channel_id=channel_id,
            now=self.now,
        )

        claimed = self.repo.claim_due_jobs(self.now, limit=20)
        self.assertEqual(
            [job["job_id"] for job in claimed],
            [batch["jobs"][0]["job_id"]],
        )
        self.assertEqual(
            self.repo.get_job(competing["job_id"])["status"],
            "pending",
        )

    def test_edit_all_fence_rejects_newer_content_on_same_message_id(self):
        self.repo.ingest_snapshot(
            snapshot_with_sections(self.now, ["group-one"])
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
        self.repo.upsert_telegram_post({
            "record_key": "legacy:group-one:701",
            "publication_id": "publication-one",
            "section_key": "group-one",
            "section_name": "group-one",
            "channel_id": settings.telegram_channel_id,
            "channel_username": settings.telegram_channel_username,
            "message_id": 701,
            "post_url": "https://t.me/testchannel/701",
            "content_hash": "old-content",
            "snapshot_id": "1",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        })
        batch = self.repo.enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key="99999999-8888-4777-8666-555555555555",
            now=self.now,
        )
        fake = FakeTelegram()
        service = PricePublicationService(settings, self.repo, telegram=fake)
        service.execute_job({
            "action": "edit",
            "section_key": "group-one",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        self.assertEqual([item[1] for item in fake.edited], [701])
        fake.edited.clear()

        result = service.execute_job(batch["jobs"][0])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "current_binding_changed")
        self.assertEqual(fake.edited, [])

    def test_atomic_alias_upsert_rolls_back_every_alias_on_failure(self):
        channel_id = "-1001234567890"
        for section_key in ("group-one", "group-two"):
            self.repo.upsert_telegram_post({
                "record_key": f"legacy:{section_key}:777",
                "section_key": section_key,
                "section_name": section_key,
                "channel_id": channel_id,
                "channel_username": "testchannel",
                "message_id": 777,
                "post_url": "https://t.me/testchannel/777",
                "content_hash": "old-content",
                "snapshot_id": "old-snapshot",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })
        updates = []
        for post in self.repo.list_telegram_posts(current_only=True):
            updates.append({
                **post,
                "content_hash": "new-content",
                "snapshot_id": "new-snapshot",
            })

        original = self.repo._upsert_post_tx
        calls = 0

        def fail_second(db, values):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated second-alias failure")
            return original(db, values)

        self.repo._upsert_post_tx = fail_second
        try:
            with self.assertRaises(RuntimeError):
                self.repo.upsert_telegram_posts_atomic(updates)
        finally:
            self.repo._upsert_post_tx = original

        current = self.repo.list_telegram_posts(current_only=True)
        self.assertEqual(len(current), 2)
        self.assertEqual(
            {post["content_hash"] for post in current},
            {"old-content"},
        )
        self.assertEqual(
            {post["snapshot_id"] for post in current},
            {"old-snapshot"},
        )

    def test_edit_all_skips_multipart_and_quick_link_message_ids(self):
        self.repo.ingest_snapshot(snapshot_with_sections(
            self.now,
            ["multi", "truncated", "collision"],
        ))
        channel_id = "-1001234567890"
        for part_no, message_id in ((1, 701), (2, 702)):
            self.repo.upsert_telegram_post({
                "record_key": f"legacy:multi:{message_id}",
                "section_key": "multi",
                "section_name": "multi",
                "channel_id": channel_id,
                "channel_username": "testchannel",
                "part_no": part_no,
                "part_count": 2,
                "message_id": message_id,
                "post_url": f"https://t.me/testchannel/{message_id}",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })
        self.repo.upsert_telegram_post({
            "record_key": "legacy:truncated:703",
            "section_key": "truncated",
            "section_name": "truncated",
            "channel_id": channel_id,
            "channel_username": "testchannel",
            "part_no": 1,
            "part_count": 2,
            "message_id": 703,
            "post_url": "https://t.me/testchannel/703",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        })
        self.repo.upsert_telegram_post({
            "record_key": "legacy:collision:900",
            "section_key": "collision",
            "section_name": "collision",
            "channel_id": channel_id,
            "channel_username": "testchannel",
            "message_id": 900,
            "post_url": "https://t.me/testchannel/900",
            "sent_at": self.now.isoformat(),
            "status": "published",
            "is_current": True,
        })
        self.repo.ensure_quick_link_posts(
            [quick_link_spec(["collision"], message_id=900)],
            channel_id=channel_id,
            channel_username="testchannel",
        )

        batch = self.repo.enqueue_current_post_edit_batch(
            channel_id=channel_id,
            channel_key="testchannel",
            idempotency_key="12345678-1234-4123-8123-123456789abc",
            now=self.now,
        )
        self.assertEqual(batch["job_count"], 0)
        self.assertEqual(batch["section_count"], 0)
        self.assertEqual(batch["skipped"], {
            "invalid_binding": ["multi", "truncated"],
            "quick_link_collision": ["collision"],
        })

    def test_edit_all_skips_every_alias_when_one_is_missing_from_snapshot(self):
        self.repo.ingest_snapshot(
            snapshot_with_sections(self.now, ["group-one"])
        )
        channel_id = "-1001234567890"
        for section_key in ("group-one", "removed-section"):
            self.repo.upsert_telegram_post({
                "record_key": f"legacy:{section_key}:777",
                "section_key": section_key,
                "section_name": section_key,
                "channel_id": channel_id,
                "channel_username": "testchannel",
                "message_id": 777,
                "post_url": "https://t.me/testchannel/777",
                "sent_at": self.now.isoformat(),
                "status": "published",
                "is_current": True,
            })

        batch = self.repo.enqueue_current_post_edit_batch(
            channel_id=channel_id,
            channel_key="testchannel",
            idempotency_key="abcdefab-cdef-4abc-8def-abcdefabcdef",
            now=self.now,
        )
        self.assertEqual(batch["job_count"], 0)
        self.assertEqual(batch["skipped"], {
            "invalid_binding": ["group-one", "removed-section"],
        })

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

        self.assertEqual(service.refresh_quick_link_posts(), 9)
        self.assertEqual(len(fake.edited), 9)
        self.assertEqual(
            {item[1] for item in fake.edited},
            {
                spec["message_id"]
                for spec in QUICK_LINK_POST_SPECS
            },
        )
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
        for quick_post_key in QUICK_LINK_ROTATION_ORDER:
            self.assertIn(
                f'{{{{quick_post_url:{quick_post_key}}}}}',
                master["template_html"],
            )
        self.assertIn("{{context:catalog_date}}", master["template_html"])
        self.assertIn("▸ ", master["template_html"])
        self.assertIn("• ", master["template_html"])
        self.assertIn(
            '<a href="tel:+998998446162">+998 (99) 844-61-62</a>',
            master["template_html"],
        )
        self.assertIn(
            '<a href="tel:+998998334466">+998 (99) 833-44-66</a>',
            master["template_html"],
        )
        self.assertNotIn("<tg-emoji", master["template_html"])

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

    def test_quick_link_sheet_expands_legacy_column_grid(self):
        settings, repo, _fake, _service = self.rotation_fixture()
        records = repo.build_quick_link_registry_records()
        legacy_headers = [
            "quick_post_key", "title", "channel_id", "channel_username",
            "message_id", "post_url", "linked_section_keys",
            "target_message_ids", "target_post_urls", "desired_revision",
            "applied_revision", "status", "last_render_hash",
            "last_edited_at", "updated_at", "last_sync_at", "last_error",
        ]

        class LegacyWorksheet:
            def __init__(self):
                self.values = [legacy_headers]
                self.col_count = len(legacy_headers)
                self.resized_to = None

            def get_all_values(self):
                return self.values

            def resize(self, *, cols):
                self.resized_to = cols
                self.col_count = cols

            def batch_update(self, requests, *, value_input_option):
                self.requests = list(requests)
                self.value_input_option = value_input_option

        worksheet = LegacyWorksheet()
        registry = ProductSortQuickLinkRegistry(settings)
        registry._worksheet = lambda: worksheet
        self.assertEqual(registry.upsert(records), 9)
        self.assertEqual(worksheet.resized_to, len(QUICK_LINK_HEADERS))
        self.assertEqual(
            len(worksheet.requests[0]["values"][0]),
            len(QUICK_LINK_HEADERS),
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
            clock=lambda: self.now + timedelta(seconds=1),
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


    def test_rotation_schedule_and_exact_eight_post_cycle(self):
        _settings, repo, _fake, _service = self.rotation_fixture()
        friday = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            repo.materialize_due_quick_link_rotations(
                friday,
                horizon_days=20,
            ),
            9,
        )
        self.assertEqual(
            repo.materialize_due_quick_link_rotations(
                friday,
                horizon_days=20,
            ),
            0,
        )
        rotations = list(reversed(repo.list_quick_link_rotations(limit=20)))
        self.assertEqual(
            [row["secondary_quick_post_key"] for row in rotations[:9]],
            [*QUICK_LINK_ROTATION_ORDER, QUICK_LINK_ROTATION_ORDER[0]],
        )
        for row in rotations:
            scheduled = datetime.fromisoformat(row["scheduled_for"])
            local = scheduled.astimezone(ZoneInfo("Asia/Tashkent"))
            self.assertIn(local.isoweekday(), {2, 4, 6})
            self.assertEqual((local.hour, local.minute), (11, 0))
        self.assertIsNone(
            repo.claim_due_quick_link_rotation(
                datetime(2026, 8, 29, 5, 59, tzinfo=timezone.utc)
            )
        )

    def test_manual_rotation_off_schedule_is_idempotent_and_rebases_cycle(self):
        settings, repo, _fake, _service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        request_id = "01234567-89ab-4cde-8fab-0123456789ab"

        queued = repo.enqueue_manual_quick_link_rotation(
            now,
            idempotency_key=request_id,
        )
        replay = PriceRepository(settings).enqueue_manual_quick_link_rotation(
            now,
            idempotency_key=request_id,
        )

        self.assertFalse(queued["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["rotation_id"], queued["rotation_id"])
        self.assertEqual(queued["trigger_source"], "manual")
        self.assertEqual(queued["local_date"], "2026-08-28")
        rotations = sorted(
            repo.list_quick_link_rotations(limit=20),
            key=lambda row: (row["scheduled_for"], row["rotation_id"]),
        )
        self.assertEqual(len(rotations), 4)
        self.assertEqual(
            [row["secondary_quick_post_key"] for row in rotations],
            list(QUICK_LINK_ROTATION_ORDER[:4]),
        )
        claimed = repo.claim_due_quick_link_rotation(now)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["rotation_id"], queued["rotation_id"])

    def test_manual_rotation_accelerates_today_schedule_once(self):
        _settings, repo, _fake, _service = self.rotation_fixture()
        now = datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=7)
        before = next(
            row for row in repo.list_quick_link_rotations(limit=20)
            if row["local_date"] == "2026-08-29"
        )
        before_count = len(repo.list_quick_link_rotations(limit=20))

        queued = repo.enqueue_manual_quick_link_rotation(
            now,
            idempotency_key="11111111-2222-4333-8444-555555555555",
        )

        self.assertEqual(queued["rotation_id"], before["rotation_id"])
        self.assertEqual(queued["trigger_source"], "manual")
        self.assertEqual(
            datetime.fromisoformat(queued["scheduled_for"]),
            now,
        )
        self.assertEqual(
            len(repo.list_quick_link_rotations(limit=20)),
            before_count,
        )
        with self.assertRaises(QuickLinkRotationConflictError) as conflict:
            repo.enqueue_manual_quick_link_rotation(
                now,
                idempotency_key=(
                    "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                ),
            )
        self.assertEqual(
            str(conflict.exception),
            "quick_link_rotation_already_active",
        )

    def test_manual_rotation_does_not_clear_scheduled_retry_backoff(self):
        _settings, repo, _fake, _service = self.rotation_fixture()
        now = datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=7)
        scheduled = next(
            row for row in repo.list_quick_link_rotations(limit=20)
            if row["local_date"] == "2026-08-29"
        )
        retry_at = now + timedelta(minutes=30)
        retry_text = retry_at.isoformat(timespec="microseconds")
        with repo._tx(True) as db:
            db.execute(
                """UPDATE telegram_quick_link_rotations
                   SET attempts=1,next_attempt_at=?,last_error=?
                   WHERE rotation_id=?""",
                (
                    retry_text,
                    "Telegram retry_after",
                    scheduled["rotation_id"],
                ),
            )

        with self.assertRaises(QuickLinkRotationConflictError) as conflict:
            repo.enqueue_manual_quick_link_rotation(
                now,
                idempotency_key=(
                    "cccccccc-dddd-4eee-8fff-000000000000"
                ),
            )

        self.assertEqual(
            str(conflict.exception),
            "quick_link_rotation_already_active",
        )
        preserved = repo.get_quick_link_rotation(scheduled["rotation_id"])
        self.assertEqual(preserved["attempts"], 1)
        self.assertEqual(preserved["next_attempt_at"], retry_text)
        self.assertEqual(
            preserved["dedupe_key"],
            "quick-link-rotation:2026-08-29",
        )

    def test_manual_rotation_uses_existing_restart_safe_state_machine(self):
        _settings, repo, fake, service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        queued = repo.enqueue_manual_quick_link_rotation(
            now,
            idempotency_key="22222222-3333-4444-8555-666666666666",
        )

        self.assertEqual(service.process_quick_link_rotations(now), 1)

        finished = repo.get_quick_link_rotation(queued["rotation_id"])
        self.assertEqual((finished["status"], finished["phase"]), (
            "done", "completed",
        ))
        self.assertEqual(finished["trigger_source"], "manual")
        self.assertEqual(len(fake.sent), 1)
        new_main_id = fake.next_id
        self.assertEqual(fake.pinned, [(
            "-1001463992448", new_main_id, True,
        )])
        self.assertIn(("-1001463992448", 5050), fake.unpinned)
        self.assertEqual(
            [message_id for _channel, message_id, _html in fake.edited],
            [5050, new_main_id],
        )
        with self.assertRaises(QuickLinkRotationConflictError) as conflict:
            repo.enqueue_manual_quick_link_rotation(
                now,
                idempotency_key=(
                    "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
                ),
            )
        self.assertEqual(
            str(conflict.exception),
            "quick_link_rotation_already_exists_today",
        )

    def test_stale_manual_rotation_does_not_double_publish_on_restart(self):
        _settings, repo, fake, service = self.rotation_fixture()
        requested = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        queued = repo.enqueue_manual_quick_link_rotation(
            requested,
            idempotency_key="33333333-4444-4555-8666-777777777777",
        )

        recovery = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(recovery, horizon_days=7)

        restored = repo.get_quick_link_rotation(queued["rotation_id"])
        self.assertEqual((restored["status"], restored["phase"]), (
            "skipped", "skipped",
        ))
        today = next(
            row for row in repo.list_quick_link_rotations(limit=20)
            if row["local_date"] == "2026-08-29"
        )
        self.assertEqual(
            today["secondary_quick_post_key"],
            QUICK_LINK_ROTATION_ORDER[0],
        )
        self.assertEqual(service.process_quick_link_rotations(recovery), 1)
        self.assertEqual(service.process_quick_link_rotations(recovery), 0)
        self.assertEqual(len(fake.sent), 1)

    def test_expired_stale_manual_lease_is_recovered_before_skip(self):
        _settings, repo, fake, service = self.rotation_fixture()
        requested = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        queued = repo.enqueue_manual_quick_link_rotation(
            requested,
            idempotency_key="55555555-6666-4777-8888-999999999999",
        )
        with repo._tx(True) as db:
            db.execute(
                """UPDATE telegram_quick_link_rotations
                   SET status='running',phase='planned',attempts=1,
                       lease_token='expired',lease_expires_at=?
                   WHERE rotation_id=?""",
                (
                    requested.isoformat(timespec="microseconds"),
                    queued["rotation_id"],
                ),
            )

        recovery = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(recovery, horizon_days=7)

        restored = repo.get_quick_link_rotation(queued["rotation_id"])
        self.assertEqual((restored["status"], restored["phase"]), (
            "skipped", "skipped",
        ))
        self.assertEqual(service.process_quick_link_rotations(recovery), 1)
        self.assertEqual(service.process_quick_link_rotations(recovery), 0)
        self.assertEqual(len(fake.sent), 1)

    def test_catalogue_date_updates_once_daily_after_0001(self):
        _settings, repo, fake, service = self.rotation_fixture()
        self.assertEqual(CATALOG_DATE_UPDATE_TIME, "00:01")
        before = datetime(2026, 8, 28, 19, 0, 59, tzinfo=timezone.utc)
        due = datetime(2026, 8, 28, 19, 1, 0, tzinfo=timezone.utc)

        self.assertEqual(repo.ensure_quick_link_catalog_date(before), 0)
        self.assertEqual(repo.ensure_quick_link_catalog_date(due), 1)
        self.assertEqual(repo.ensure_quick_link_catalog_date(due), 0)
        self.assertEqual(
            repo.resolve_quick_link_post(CATALOG_QUICK_POST_KEY)["context"][
                "catalog_date"
            ],
            "29.08.2026",
        )
        self.assertEqual(service.refresh_quick_link_posts(due), 1)
        self.assertEqual(fake.edited[-1][1], 5050)
        self.assertIn("Каталог товаров | 29.08.2026", fake.edited[-1][2])
        self.assertEqual(service.refresh_quick_link_posts(due), 0)

    def test_catalogue_date_catches_up_once_after_restart(self):
        settings, repo, _fake, _service = self.rotation_fixture()
        before = next(
            item for item in repo.list_quick_link_updates()
            if item["quick_post_key"] == CATALOG_QUICK_POST_KEY
        )["desired_revision"]
        reopened = PriceRepository(settings)
        recovery = datetime(2026, 9, 2, 19, 1, tzinfo=timezone.utc)

        self.assertEqual(reopened.ensure_quick_link_catalog_date(recovery), 1)
        self.assertEqual(reopened.ensure_quick_link_catalog_date(recovery), 0)
        self.assertEqual(
            reopened.resolve_quick_link_post(CATALOG_QUICK_POST_KEY)["context"][
                "catalog_date"
            ],
            "03.09.2026",
        )
        after = next(
            item for item in reopened.list_quick_link_updates()
            if item["quick_post_key"] == CATALOG_QUICK_POST_KEY
        )["desired_revision"]
        self.assertEqual(after, before + 1)

    def test_failed_catalogue_date_edit_is_retried_after_one_hour(self):
        settings, repo, fake, service = self.rotation_fixture()
        due = datetime(2026, 8, 28, 19, 1, tzinfo=timezone.utc)
        target = (settings.telegram_channel_id, 5050)
        fake.edit_failures[target] = FakeTelegramDeleteError(
            "temporary permissions failure"
        )

        self.assertEqual(repo.ensure_quick_link_catalog_date(due), 1)
        self.assertEqual(service.refresh_quick_link_posts(due), 0)
        failed = next(
            item for item in repo.list_quick_link_updates()
            if item["quick_post_key"] == CATALOG_QUICK_POST_KEY
        )
        self.assertEqual(failed["status"], "failed")
        desired_revision = failed["desired_revision"]
        self.assertEqual(
            repo.ensure_quick_link_catalog_date(
                due + timedelta(minutes=59)
            ),
            0,
        )

        del fake.edit_failures[target]
        retry_at = due + timedelta(hours=1)
        self.assertEqual(repo.ensure_quick_link_catalog_date(retry_at), 1)
        pending = next(
            item for item in repo.list_quick_link_updates()
            if item["quick_post_key"] == CATALOG_QUICK_POST_KEY
        )
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["desired_revision"], desired_revision)
        self.assertEqual(service.refresh_quick_link_posts(retry_at), 1)
        self.assertIn("Каталог товаров | 29.08.2026", fake.edited[-1][2])

    def test_scheduler_edits_catalogue_date_at_0001_on_non_rotation_day(self):
        settings, repo, fake, service = self.rotation_fixture()
        clock = [
            datetime(2026, 8, 30, 19, 0, 59, tzinfo=timezone.utc)
        ]
        service.sync_sheets_outbox = lambda **_kwargs: 0
        scheduler = PriceScheduler(
            settings,
            repo,
            service,
            clock=lambda: clock[0],
        )

        asyncio.run(scheduler.run_once())
        self.assertEqual(fake.edited, [])
        clock[0] = datetime(2026, 8, 30, 19, 1, tzinfo=timezone.utc)
        asyncio.run(scheduler.run_once())
        self.assertEqual(len(fake.edited), 1)
        self.assertEqual(fake.edited[0][1], 5050)
        self.assertIn("Каталог товаров | 31.08.2026", fake.edited[0][2])

    def test_delayed_rotation_does_not_regress_newer_catalogue_date(self):
        _settings, repo, fake, service = self.rotation_fixture()
        friday = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        delayed = datetime(2026, 8, 29, 19, 5, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(friday, horizon_days=2)

        self.assertEqual(
            repo.ensure_quick_link_catalog_date(
                datetime(2026, 8, 29, 19, 1, tzinfo=timezone.utc)
            ),
            1,
        )
        self.assertEqual(service.refresh_quick_link_posts(delayed), 1)
        self.assertIn("Каталог товаров | 30.08.2026", fake.edited[-1][2])
        fake.edited.clear()

        self.assertEqual(service.process_quick_link_rotations(delayed), 1)
        self.assertIn("Каталог товаров | 30.08.2026", fake.sent[-1][1])
        self.assertNotIn("Каталог товаров | 29.08.2026", fake.sent[-1][1])
        self.assertEqual(
            repo.resolve_quick_link_post(CATALOG_QUICK_POST_KEY)["context"][
                "catalog_date"
            ],
            "30.08.2026",
        )
        self.assertEqual(service.refresh_quick_link_posts(delayed), 0)
        new_main = repo.get_quick_link_post(CATALOG_QUICK_POST_KEY)
        self.assertEqual(fake.edited[-1][1], new_main["message_id"])
        self.assertIn("Каталог товаров | 30.08.2026", fake.edited[-1][2])

    def test_same_day_calendar_jobs_materialize_after_0930_restart(self):
        _settings, repo, _fake, _service = self.rotation_fixture()
        plan = [
            row for row in repo.list_calendar_plan()
            if row["day_of_month"] == 29
        ]
        self.assertEqual(len(plan), 3)
        now = datetime(2026, 8, 29, 5, 30, tzinfo=timezone.utc)
        repo.ingest_snapshot(snapshot_with_sections(
            now,
            [row["section_key"] for row in plan],
        ))
        self.assertEqual(
            repo.materialize_due_schedules(now, horizon_days=0),
            3,
        )
        self.assertEqual(
            {
                datetime.fromisoformat(job["execute_at"])
                for job in repo.list_jobs(limit=20)
            },
            {datetime(2026, 8, 29, 4, 30, tzinfo=timezone.utc)},
        )

    def test_stale_planned_rotations_are_skipped_without_catchup_burst(self):
        _settings, repo, _fake, _service = self.rotation_fixture()
        friday = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(friday, horizon_days=20)
        recovery = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(recovery, horizon_days=0)
        rotations = list(reversed(repo.list_quick_link_rotations(limit=30)))
        stale = [
            row for row in rotations
            if row["local_date"] in {"2026-08-29", "2026-09-01", "2026-09-03"}
        ]
        self.assertEqual({row["status"] for row in stale}, {"skipped"})
        next_run = next(
            row for row in rotations if row["local_date"] == "2026-09-05"
        )
        self.assertEqual(next_run["status"], "pending")
        self.assertEqual(
            next_run["secondary_quick_post_key"],
            QUICK_LINK_ROTATION_ORDER[0],
        )
        self.assertIsNone(repo.claim_due_quick_link_rotation(recovery))
        due = repo.claim_due_quick_link_rotation(
            datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)
        )
        self.assertIsNotNone(due)
        self.assertEqual(
            due["secondary_quick_post_key"],
            QUICK_LINK_ROTATION_ORDER[0],
        )

    def test_rotation_recycles_main_updates_ids_and_preserves_runtime_binding(self):
        settings, repo, fake, service = self.rotation_fixture()
        friday = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(
            repo.materialize_due_quick_link_rotations(
                friday,
                horizon_days=2,
            ),
            1,
        )
        self.assertEqual(service.process_quick_link_rotations(execute), 1)

        master = repo.get_quick_link_post(CATALOG_QUICK_POST_KEY)
        smartphones = repo.get_quick_link_post(
            "quick-index-smartphones"
        )
        self.assertEqual(master["message_id"], 101)
        self.assertEqual(smartphones["message_id"], 5050)
        self.assertEqual(fake.pinned, [(
            settings.telegram_channel_id,
            101,
            True,
        )])
        self.assertEqual(fake.unpinned, [(
            settings.telegram_channel_id,
            5050,
        )])
        self.assertEqual(
            [(item[1], item[2]) for item in fake.edited],
            [
                (5050, smartphones["last_rendered_html"]),
                (101, master["last_rendered_html"]),
            ],
        )
        self.assertIn("<b>Смартфоны</b>", fake.edited[0][2])
        self.assertTrue(all(
            line.startswith("• ")
            for line in fake.edited[0][2].splitlines()[2::2]
        ))
        sent_main = fake.sent[0][1]
        self.assertIn("Каталог товаров | 29.08.2026", sent_main)
        self.assertIn(
            'href="https://t.me/testchannel/4942">Телефоны</a>',
            sent_main,
        )
        self.assertIn(
            'href="https://t.me/testchannel/5050">Телефоны</a>',
            fake.edited[1][2],
        )
        self.assertNotIn("<tg-emoji", sent_main)

        retired = repo.list_quick_link_retired_posts()
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]["message_id"], 4942)
        self.assertEqual(retired[0]["title"], "Смартфоны")
        self.assertEqual(service.ensure_quick_link_registry(), 0)
        self.assertEqual(
            repo.get_quick_link_post(CATALOG_QUICK_POST_KEY)["message_id"],
            101,
        )
        self.assertEqual(
            repo.get_quick_link_post("quick-index-smartphones")["message_id"],
            5050,
        )

        records = {
            row["quick_post_key"]: row
            for row in repo.build_quick_link_registry_records()
        }
        self.assertEqual(records[CATALOG_QUICK_POST_KEY]["message_id"], 101)
        self.assertEqual(
            records["quick-index-smartphones"]["message_id"],
            5050,
        )
        self.assertEqual(
            records[CATALOG_QUICK_POST_KEY]["target_message_ids"][
                "quick-index-smartphones"
            ],
            5050,
        )

    def test_nine_real_rotations_keep_unique_ids_and_wrap_to_smartphones(self):
        _settings, repo, fake, service = self.rotation_fixture()
        friday = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            repo.materialize_due_quick_link_rotations(
                friday,
                horizon_days=20,
            ),
            9,
        )
        rotations = list(reversed(repo.list_quick_link_rotations(limit=20)))
        for rotation in rotations:
            execute = datetime.fromisoformat(rotation["scheduled_for"])
            self.assertEqual(service.process_quick_link_rotations(execute), 1)
            bindings = repo.list_quick_link_posts()
            self.assertEqual(len(bindings), 9)
            self.assertEqual(
                len({row["message_id"] for row in bindings}),
                9,
            )

        self.assertEqual(len(fake.sent), 9)
        self.assertEqual(len(fake.pinned), 9)
        self.assertEqual(len(fake.unpinned), 9)
        final_main = repo.get_quick_link_post(CATALOG_QUICK_POST_KEY)
        final_smartphones = repo.get_quick_link_post(
            "quick-index-smartphones"
        )
        self.assertEqual(final_main["message_id"], 109)
        self.assertEqual(final_smartphones["message_id"], 108)
        final_rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertEqual(
            final_rotation["secondary_quick_post_key"],
            QUICK_LINK_ROTATION_ORDER[0],
        )

    def test_price_refresh_edits_recycled_secondary_message(self):
        settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        self.assertEqual(service.process_quick_link_rotations(execute), 1)
        fake.edited.clear()

        repo.ingest_snapshot(
            snapshot_with_sections(before, ["smartphones-xiaomi-poco"])
        )
        service.execute_job({
            "action": "send",
            "section_key": "smartphones-xiaomi-poco",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        self.assertEqual(fake.edited[-1][1], 5050)
        self.assertIn("https://t.me/testchannel/102", fake.edited[-1][2])

    def test_rotation_pin_retry_does_not_resend_catalogue(self):
        settings, repo, fake, service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=2)
        fake.pin_failures[(settings.telegram_channel_id, 101)] = (
            FakeTelegramRetryableError("temporary pin failure")
        )
        self.assertEqual(service.process_quick_link_rotations(execute), 0)
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertEqual(rotation["status"], "pending")
        self.assertEqual(rotation["phase"], "main_sent")
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.edited, [])

        del fake.pin_failures[(settings.telegram_channel_id, 101)]
        self.assertEqual(
            service.process_quick_link_rotations(
                execute + timedelta(seconds=1)
            ),
            1,
        )
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(len(fake.edited), 2)
        self.assertEqual(fake.edited[0][1], 5050)
        self.assertEqual(fake.edited[1][1], 101)

    def test_due_planned_rotation_allows_link_queue_to_drain_first(self):
        import hashlib

        settings, repo, _fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        repo.ingest_snapshot(
            snapshot_with_sections(before, ["smartphones-xiaomi-poco"])
        )
        service.execute_job({
            "action": "send",
            "section_key": "smartphones-xiaomi-poco",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        claimed = repo.claim_quick_link_updates(execute, limit=20)
        self.assertEqual(
            [row["quick_post_key"] for row in claimed],
            ["quick-index-smartphones"],
        )
        task = claimed[0]
        post = repo.resolve_quick_link_post(task["quick_post_key"])
        rendered, targets = service._render_quick_link(post)
        self.assertTrue(repo.complete_quick_link_update(
            task["quick_post_key"],
            task["lease_token"],
            execute,
            rendered_html=rendered,
            render_hash=hashlib.sha256(rendered.encode()).hexdigest(),
            resolved_targets=targets,
        ))
        rotation = repo.claim_due_quick_link_rotation(execute)
        self.assertIsNotNone(rotation)
        self.assertEqual(rotation["phase"], "planned")

    def test_ambiguous_rotation_send_requires_review_without_duplicate(self):
        _settings, repo, fake, service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=2)
        fake.send_failure = FakeTelegramAmbiguousError("timeout")
        self.assertEqual(service.process_quick_link_rotations(execute), 0)
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertEqual(rotation["status"], "needs_review")
        self.assertEqual(rotation["phase"], "send_inflight")
        self.assertEqual(
            repo.get_quick_link_post(CATALOG_QUICK_POST_KEY)["message_id"],
            5050,
        )
        self.assertEqual(
            repo.get_quick_link_post("quick-index-smartphones")["message_id"],
            4942,
        )
        fake.send_failure = None
        self.assertEqual(
            service.process_quick_link_rotations(
                execute + timedelta(hours=1)
            ),
            0,
        )
        self.assertEqual(fake.pinned, [])
        self.assertEqual(fake.edited, [])

    def test_ambiguous_rotation_blocks_link_refresh_until_reconciled(self):
        settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        fake.send_failure = FakeTelegramAmbiguousError("timeout")
        self.assertEqual(service.process_quick_link_rotations(execute), 0)
        fake.send_failure = None

        repo.ingest_snapshot(
            snapshot_with_sections(before, ["smartphones-xiaomi-poco"])
        )
        service.execute_job({
            "action": "send",
            "section_key": "smartphones-xiaomi-poco",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        fake.edited.clear()
        self.assertEqual(
            repo.claim_quick_link_updates(
                execute + timedelta(minutes=1),
                limit=20,
            ),
            [],
        )
        update = next(
            row for row in repo.list_quick_link_updates()
            if row["quick_post_key"] == "quick-index-smartphones"
        )
        self.assertEqual(update["status"], "pending")

    def test_ambiguous_rotation_can_be_confirmed_absent_and_retried(self):
        _settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        fake.send_failure = FakeTelegramAmbiguousError("timeout")
        self.assertEqual(service.process_quick_link_rotations(execute), 0)
        fake.send_failure = None
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertTrue(repo.confirm_quick_link_rotation_not_sent(
            rotation["rotation_id"],
            now=execute + timedelta(minutes=1),
        ))
        self.assertEqual(
            service.process_quick_link_rotations(
                execute + timedelta(minutes=1)
            ),
            1,
        )
        self.assertEqual(len(fake.sent), 1)

    def test_reconciled_rotation_rejects_active_quick_post_id(self):
        _settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        fake.send_failure = FakeTelegramAmbiguousError("timeout")
        service.process_quick_link_rotations(execute)
        fake.send_failure = None
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        with self.assertRaises(ValueError):
            repo.reconcile_quick_link_rotation_main_message(
                rotation["rotation_id"],
                5050,
            )
        self.assertTrue(repo.reconcile_quick_link_rotation_main_message(
            rotation["rotation_id"],
            6000,
        ))
        self.assertEqual(
            service.process_quick_link_rotations(
                execute + timedelta(minutes=1)
            ),
            1,
        )
        self.assertEqual(fake.pinned[-1][1], 6000)

    def test_failed_post_send_phase_can_be_retried_without_resend(self):
        settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        fake.pin_failures[(settings.telegram_channel_id, 101)] = (
            FakeTelegramDeleteError("not enough rights")
        )
        self.assertEqual(service.process_quick_link_rotations(execute), 0)
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertEqual((rotation["status"], rotation["phase"]), (
            "failed", "main_sent",
        ))
        del fake.pin_failures[(settings.telegram_channel_id, 101)]
        self.assertTrue(repo.retry_failed_quick_link_rotation(
            rotation["rotation_id"],
            now=execute + timedelta(minutes=1),
        ))
        self.assertEqual(
            service.process_quick_link_rotations(
                execute + timedelta(minutes=1)
            ),
            1,
        )
        self.assertEqual(len(fake.sent), 1)

    def test_secondary_edit_failure_leaves_new_catalogue_link_functional(self):
        settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        fake.edit_failures[(settings.telegram_channel_id, 5050)] = (
            FakeTelegramDeleteError("not enough rights")
        )
        self.assertEqual(service.process_quick_link_rotations(execute), 0)
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertEqual((rotation["status"], rotation["phase"]), (
            "failed", "new_pinned",
        ))
        self.assertIn(
            'href="https://t.me/testchannel/4942">Телефоны</a>',
            fake.sent[0][1],
        )
        self.assertEqual(
            repo.get_quick_link_post(CATALOG_QUICK_POST_KEY)["message_id"],
            5050,
        )
        self.assertEqual(
            repo.get_quick_link_post("quick-index-smartphones")["message_id"],
            4942,
        )

    def test_rotation_resumes_after_restart_from_every_persisted_phase(self):
        phases = (
            "planned", "send_inflight", "main_sent", "new_pinned",
            "secondary_edited", "catalog_edited", "swapped",
            "old_unpinned",
        )
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        restart_at = execute + timedelta(minutes=4)

        for target_phase in phases:
            with self.subTest(phase=target_phase):
                settings, repo, fake, service = self.rotation_fixture(
                    db_name=f"restart-{target_phase}.db"
                )
                fake.next_id = 6000
                repo.materialize_due_quick_link_rotations(
                    before,
                    horizon_days=2,
                )
                sent = None
                if target_phase != "planned":
                    rotation = repo.claim_due_quick_link_rotation(execute)
                    rotation_id = rotation["rotation_id"]
                    token = rotation["lease_token"]
                    plan = service._rotation_render_plan(rotation)
                    self.assertTrue(repo.mark_quick_link_rotation_send_inflight(
                        rotation_id,
                        token,
                        previous_main_message_id=plan[
                            "previous_main_message_id"
                        ],
                        previous_main_post_url=plan[
                            "previous_main_post_url"
                        ],
                        previous_secondary_message_id=plan[
                            "previous_secondary_message_id"
                        ],
                        previous_secondary_post_url=plan[
                            "previous_secondary_post_url"
                        ],
                        main_html=plan["main_html"],
                        main_render_hash=plan["main_render_hash"],
                        main_targets=plan["main_targets"],
                        secondary_html=plan["secondary_html"],
                        secondary_render_hash=plan[
                            "secondary_render_hash"
                        ],
                        secondary_targets=plan["secondary_targets"],
                        now=execute,
                    ))
                    sent = fake.send_message(
                        plan["channel_id"],
                        plan["send_main_html"],
                    )
                    if target_phase != "send_inflight":
                        self.assertTrue(
                            repo.record_quick_link_rotation_main_sent(
                                rotation_id,
                                token,
                                message_id=sent.message_id,
                                post_url=sent.post_url,
                                now=execute,
                            )
                        )
                    if target_phase in {
                        "new_pinned", "secondary_edited", "catalog_edited",
                        "swapped", "old_unpinned",
                    }:
                        fake.pin_message(
                            settings.telegram_channel_id,
                            sent.message_id,
                        )
                        self.assertTrue(repo.mark_quick_link_rotation_phase(
                            rotation_id,
                            token,
                            expected_phase="main_sent",
                            phase="new_pinned",
                            now=execute,
                        ))
                    if target_phase in {
                        "secondary_edited", "catalog_edited", "swapped",
                        "old_unpinned",
                    }:
                        fake.edit_message(
                            settings.telegram_channel_id,
                            plan["previous_main_message_id"],
                            plan["secondary_html"],
                        )
                        self.assertTrue(repo.mark_quick_link_rotation_phase(
                            rotation_id,
                            token,
                            expected_phase="new_pinned",
                            phase="secondary_edited",
                            now=execute,
                        ))
                    if target_phase in {
                        "catalog_edited", "swapped", "old_unpinned",
                    }:
                        fake.edit_message(
                            settings.telegram_channel_id,
                            sent.message_id,
                            plan["main_html"],
                        )
                        self.assertTrue(repo.mark_quick_link_rotation_phase(
                            rotation_id,
                            token,
                            expected_phase="secondary_edited",
                            phase="catalog_edited",
                            now=execute,
                        ))
                    if target_phase in {"swapped", "old_unpinned"}:
                        self.assertTrue(repo.commit_quick_link_rotation_swap(
                            rotation_id,
                            token,
                            now=execute,
                        ))
                    if target_phase == "old_unpinned":
                        fake.unpin_message(
                            settings.telegram_channel_id,
                            plan["previous_main_message_id"],
                        )
                        self.assertTrue(repo.mark_quick_link_rotation_phase(
                            rotation_id,
                            token,
                            expected_phase="swapped",
                            phase="old_unpinned",
                            now=execute,
                        ))

                reopened = PriceRepository(settings)
                resumed = PricePublicationService(
                    settings,
                    reopened,
                    telegram=fake,
                )
                if target_phase == "send_inflight":
                    self.assertEqual(
                        reopened.recover_stale_quick_link_rotations(restart_at),
                        1,
                    )
                    row = reopened.list_quick_link_rotations(limit=1)[0]
                    self.assertEqual((row["status"], row["phase"]), (
                        "needs_review", "send_inflight",
                    ))
                    self.assertTrue(
                        reopened.reconcile_quick_link_rotation_main_message(
                            row["rotation_id"],
                            sent.message_id,
                            now=restart_at,
                        )
                    )
                self.assertEqual(
                    resumed.process_quick_link_rotations(restart_at),
                    1,
                )
                row = reopened.list_quick_link_rotations(limit=1)[0]
                self.assertEqual((row["status"], row["phase"]), (
                    "done", "completed",
                ))
                self.assertEqual(len(fake.sent), 1)
                expected_main_id = 6001
                self.assertEqual(
                    reopened.get_quick_link_post(
                        CATALOG_QUICK_POST_KEY
                    )["message_id"],
                    expected_main_id,
                )
                self.assertEqual(
                    reopened.get_quick_link_post(
                        "quick-index-smartphones"
                    )["message_id"],
                    5050,
                )

    def test_rotated_secondary_manual_cleanup_has_human_title_and_url(self):
        settings, repo, fake, service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=2)
        self.assertEqual(service.process_quick_link_rotations(execute), 1)
        target = (settings.telegram_channel_id, 4942)
        fake.delete_failures[target] = FakeTelegramDeleteError(
            "message can't be deleted"
        )
        self.assertEqual(service.cleanup_superseded_posts(), 0)
        self.assertEqual(service.ensure_manual_deletion_requests(), 1)
        helper = fake.sent[-1][1]
        self.assertIn("Раздел: Смартфоны", helper)
        self.assertIn("https://t.me/testchannel/4942", helper)

    def test_catalogue_date_survives_later_direct_price_link_refresh(self):
        settings, repo, fake, service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=2)
        self.assertEqual(service.process_quick_link_rotations(execute), 1)
        fake.edited.clear()

        repo.ingest_snapshot(
            snapshot_with_sections(now, ["apple-computers-all"])
        )
        service.execute_job({
            "action": "send",
            "section_key": "apple-computers-all",
            "channel_id": settings.telegram_channel_id,
            "snapshot_policy": "latest",
        })
        self.assertEqual(service.refresh_quick_link_posts(), 1)
        self.assertEqual(fake.edited[-1][1], 101)
        self.assertIn(
            "Каталог товаров | 29.08.2026",
            fake.edited[-1][2],
        )

    def test_quick_link_rotation_sheet_upserts_stable_history_rows(self):
        settings, repo, _fake, _service = self.rotation_fixture()
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        repo.materialize_due_quick_link_rotations(now, horizon_days=7)
        records = repo.list_quick_link_rotations(limit=20)

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
        registry = ProductSortQuickLinkRotationRegistry(settings)
        registry._worksheet = lambda: worksheet
        self.assertEqual(registry.upsert(records), len(records))
        self.assertEqual(
            worksheet.requests[0]["values"],
            [list(QUICK_LINK_ROTATION_HEADERS)],
        )
        worksheet.values = [list(QUICK_LINK_ROTATION_HEADERS)] + [
            request["values"][0]
            for request in worksheet.requests[1:]
        ]
        self.assertEqual(registry.upsert(records), len(records))
        self.assertEqual(len(worksheet.requests), len(records))

    def test_main_channel_pin_service_message_is_deleted(self):
        settings, _repo, fake, service = self.rotation_fixture()
        fake.updates.append({
            "update_id": 12,
            "channel_post": {
                "message_id": 778,
                "chat": {"id": int(settings.telegram_channel_id)},
                "pinned_message": {"message_id": 101},
            },
        })
        self.assertEqual(service.poll_preview_updates(), 1)
        self.assertIn((settings.telegram_channel_id, 778), fake.deleted)

    def test_telegram_client_uses_exact_pin_and_unpin_payloads(self):
        class Response:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "result": True}

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, *, json, timeout):
                self.calls.append((url, json, timeout))
                return Response()

        session = Session()
        client = TelegramClient("test-token", session=session)
        self.assertTrue(client.pin_message("-100123", 456))
        self.assertTrue(client.unpin_message("-100123", 456))
        self.assertTrue(session.calls[0][0].endswith("/pinChatMessage"))
        self.assertEqual(session.calls[0][1], {
            "chat_id": "-100123",
            "message_id": 456,
            "disable_notification": True,
        })
        self.assertTrue(session.calls[1][0].endswith("/unpinChatMessage"))
        self.assertEqual(session.calls[1][1], {
            "chat_id": "-100123",
            "message_id": 456,
        })

    def test_send_message_invalid_success_json_is_ambiguous(self):
        class Response:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                raise ValueError("truncated")

        class Session:
            @staticmethod
            def post(_url, *, json, timeout):
                return Response()

        client = TelegramClient("test-token", session=Session())
        with self.assertRaises(TelegramAPIError) as raised:
            client.send_message("-100123", "test")
        self.assertTrue(raised.exception.ambiguous)
        self.assertFalse(raised.exception.retryable)

    def test_pin_and_unpin_are_idempotent_and_preserve_retry_after(self):
        class Response:
            ok = False
            status_code = 400

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        class Session:
            def __init__(self):
                self.responses = [
                    Response({
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message is already pinned",
                    }),
                    Response({
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message to unpin not found",
                    }),
                    Response({
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 7},
                    }),
                ]

            def post(self, _url, *, json, timeout):
                return self.responses.pop(0)

        client = TelegramClient("test-token", session=Session())
        self.assertTrue(client.pin_message("-100123", 456))
        self.assertTrue(client.unpin_message("-100123", 456))
        with self.assertRaises(TelegramAPIError) as raised:
            client.pin_message("-100123", 456)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 7)

    def test_scheduler_publishes_due_price_before_1100_catalogue(self):
        settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.ingest_snapshot(
            snapshot_with_sections(before, ["apple-computers-all"])
        )
        repo.enqueue_job(
            "apple-computers-all",
            "send",
            execute,
            channel_id=settings.telegram_channel_id,
            payload={
                "source": "price_calendar",
                "calendar_date": "2026-08-29",
            },
            now=before,
        )
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        service.sync_sheets_outbox = lambda **_kwargs: 0
        scheduler = PriceScheduler(
            settings,
            repo,
            service,
            clock=lambda: execute,
        )
        self.assertEqual(asyncio.run(scheduler.run_once()), 2)
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("APPLE-COMPUTERS-ALL", fake.sent[0][1])
        self.assertIn("Каталог товаров | 29.08.2026", fake.sent[1][1])

    def test_rotation_waits_while_same_day_calendar_post_is_retrying(self):
        settings, repo, fake, service = self.rotation_fixture()
        before = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        execute = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)
        repo.ingest_snapshot(
            snapshot_with_sections(before, ["apple-computers-all"])
        )
        repo.enqueue_job(
            "apple-computers-all",
            "send",
            execute,
            channel_id=settings.telegram_channel_id,
            payload={
                "source": "price_calendar",
                "calendar_date": "2026-08-29",
            },
            now=before,
        )
        repo.materialize_due_quick_link_rotations(before, horizon_days=2)
        failure = FakeTelegramRetryableError("temporary outage")
        failure.retry_after = 3600
        fake.send_failure = failure
        service.sync_sheets_outbox = lambda **_kwargs: 0
        scheduler = PriceScheduler(
            settings,
            repo,
            service,
            clock=lambda: execute,
        )
        self.assertEqual(asyncio.run(scheduler.run_once()), 1)
        rotation = repo.list_quick_link_rotations(limit=1)[0]
        self.assertEqual((rotation["status"], rotation["phase"]), (
            "pending", "planned",
        ))
        self.assertEqual(fake.pinned, [])


class PricePageAuthTests(unittest.TestCase):
    def test_admin_script_exposes_guarded_manual_catalogue_action(self):
        script = (
            Path(__file__).resolve().parent
            / "price_server"
            / "static"
            / "admin.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Опубликовать новый главный пост", script)
        self.assertIn(
            "/price/api/v1/quick-link-rotations/publish-now",
            script,
        )
        self.assertIn("price-server-publish-main-idempotency", script)
        self.assertIn('{"Idempotency-Key": idempotencyKey}', script)
        self.assertIn("if (result.duplicate)", script)
        self.assertIn("предыдущий главный пост станет", script)

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

    def test_update_all_endpoint_requires_auth_csrf_and_is_idempotent(self):
        module = importlib.import_module("price_server.router")
        with tempfile.TemporaryDirectory() as folder:
            configured = PriceSettings(
                enabled=True,
                db_path=Path(folder) / "price.db",
                legacy_html_path=Path(folder) / "index.html",
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
            repo = PriceRepository(configured)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            repo.ingest_snapshot(snapshot_with_sections(now, ["group-one"]))
            repo.upsert_telegram_post({
                "record_key": "legacy:group-one:701",
                "section_key": "group-one",
                "section_name": "group-one",
                "channel_id": configured.telegram_channel_id,
                "channel_username": configured.telegram_channel_username,
                "message_id": 701,
                "post_url": "https://t.me/testchannel/701",
                "sent_at": now.isoformat(),
                "status": "published",
                "is_current": True,
            })
            old = (
                module.settings,
                module._repository,
                module._service,
                module._startup_error,
            )
            module.settings = configured
            module._repository = repo
            module._service = object()
            module._startup_error = ""
            token = base64.b64encode(b"admin:secret").decode()
            body = json.dumps({
                "confirm": True,
                "channel_id": "-1000000000000",
                "section_keys": ["attacker-selected"],
                "action": "send",
            }).encode()
            body_reads = 0

            async def receive():
                nonlocal body_reads
                body_reads += 1
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }

            def request(headers):
                return Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/price/api/v1/posts/update-all",
                        "headers": headers,
                    },
                    receive,
                )

            try:
                with self.assertRaises(HTTPException) as anonymous:
                    asyncio.run(module.update_all_current_posts(request([])))
                self.assertEqual(anonymous.exception.status_code, 401)
                self.assertEqual(body_reads, 0)

                auth = (b"authorization", f"Basic {token}".encode())
                with self.assertRaises(HTTPException) as no_csrf:
                    asyncio.run(module.update_all_current_posts(request([auth])))
                self.assertEqual(no_csrf.exception.status_code, 403)
                self.assertEqual(body_reads, 0)

                batch_id = "01234567-89ab-4cde-8fab-0123456789ab"
                headers = [
                    auth,
                    (b"x-requested-with", b"TexnikachPriceAdmin"),
                    (b"idempotency-key", batch_id.encode()),
                    (b"content-type", b"application/json"),
                ]
                first = asyncio.run(
                    module.update_all_current_posts(request(headers))
                )
                second = asyncio.run(
                    module.update_all_current_posts(request(headers))
                )
                self.assertEqual(first["job_count"], 1)
                self.assertEqual(first["section_count"], 1)
                self.assertFalse(first["duplicate"])
                self.assertTrue(second["duplicate"])
                self.assertEqual(second["job_ids"], first["job_ids"])
                job = repo.get_job(first["job_ids"][0])
                self.assertEqual(job["action"], "edit")
                self.assertEqual(job["channel_id"], configured.telegram_channel_id)
                self.assertEqual(job["payload"]["section_keys"], ["group-one"])
                self.assertNotIn("lease_token", first)
                history = asyncio.run(module.price_jobs(Request({
                    "type": "http",
                    "method": "GET",
                    "path": "/price/api/v1/jobs",
                    "headers": [auth],
                })))
                self.assertEqual(
                    history["edit_batches"][0]["batch_id"],
                    first["batch_id"],
                )

                module.settings = PriceSettings(**{
                    **configured.__dict__,
                    "telegram_channel_id": "-1009999999999",
                })
                with self.assertRaises(HTTPException) as conflict:
                    asyncio.run(
                        module.update_all_current_posts(request(headers))
                    )
                self.assertEqual(conflict.exception.status_code, 409)
                self.assertEqual(
                    conflict.exception.detail,
                    "idempotency_key_conflict",
                )
                self.assertEqual(len(repo.list_jobs()), 1)
            finally:
                (
                    module.settings,
                    module._repository,
                    module._service,
                    module._startup_error,
                ) = old

    def test_update_all_endpoint_validates_confirmation_and_snapshot(self):
        module = importlib.import_module("price_server.router")
        with tempfile.TemporaryDirectory() as folder:
            configured = PriceSettings(
                enabled=True,
                db_path=Path(folder) / "price.db",
                legacy_html_path=Path(folder) / "index.html",
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
            old = (
                module.settings,
                module._repository,
                module._service,
                module._startup_error,
            )
            module.settings = configured
            module._repository = PriceRepository(configured)
            module._service = object()
            module._startup_error = ""
            token = base64.b64encode(b"admin:secret").decode()
            base_headers = [
                (b"authorization", f"Basic {token}".encode()),
                (b"x-requested-with", b"TexnikachPriceAdmin"),
                (b"content-type", b"application/json"),
            ]

            def request(raw_body: bytes, idempotency_key: str):
                delivered = False

                async def receive():
                    nonlocal delivered
                    if delivered:
                        return {
                            "type": "http.request",
                            "body": b"",
                            "more_body": False,
                        }
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": raw_body,
                        "more_body": False,
                    }

                return Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/price/api/v1/posts/update-all",
                        "headers": [
                            *base_headers,
                            (b"idempotency-key", idempotency_key.encode()),
                        ],
                    },
                    receive,
                )

            try:
                with self.assertRaises(HTTPException) as confirm:
                    asyncio.run(module.update_all_current_posts(
                        request(b'{"confirm": false}', "not-a-uuid")
                    ))
                self.assertEqual(confirm.exception.status_code, 422)
                self.assertEqual(
                    confirm.exception.detail,
                    "explicit_confirmation_required",
                )

                with self.assertRaises(HTTPException) as key_error:
                    asyncio.run(module.update_all_current_posts(
                        request(b'{"confirm": true}', "not-a-uuid")
                    ))
                self.assertEqual(key_error.exception.status_code, 422)
                self.assertEqual(
                    key_error.exception.detail,
                    "valid_idempotency_key_required",
                )

                with self.assertRaises(HTTPException) as missing:
                    asyncio.run(module.update_all_current_posts(request(
                        b'{"confirm": true}',
                        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    )))
                self.assertEqual(missing.exception.status_code, 409)
                self.assertEqual(
                    missing.exception.detail,
                    "current_snapshot_not_available",
                )
            finally:
                (
                    module.settings,
                    module._repository,
                    module._service,
                    module._startup_error,
                ) = old

    def test_publish_main_endpoint_is_guarded_idempotent_and_queue_only(self):
        module = importlib.import_module("price_server.router")
        with tempfile.TemporaryDirectory() as folder:
            configured = PriceSettings(
                enabled=True,
                db_path=Path(folder) / "price.db",
                legacy_html_path=Path(folder) / "index.html",
                admin_username="admin",
                admin_password="secret",
                sync_api_key="sync",
                telegram_bot_token="fake-token",
                telegram_channel_id="-1001463992448",
                telegram_channel_username="testchannel",
                product_sort_sheet_id="sheet",
                posts_sheet_name="Telegram Posts",
                timezone="Asia/Tashkent",
                scheduler_poll_seconds=1,
                sync_max_bytes=2_000_000,
                telegram_preview_channel_id="-1003922029862",
            )
            repo = PriceRepository(configured)
            fake = FakeTelegram()
            service = PricePublicationService(
                configured,
                repo,
                telegram=fake,
            )
            self.assertEqual(service.ensure_quick_link_registry(), 9)
            self.assertEqual(service.refresh_quick_link_posts(), 9)
            fake.sent.clear()
            fake.edited.clear()
            fake.pinned.clear()
            fake.unpinned.clear()
            old = (
                module.settings,
                module._repository,
                module._service,
                module._startup_error,
            )
            module.settings = configured
            module._repository = repo
            module._service = service
            module._startup_error = ""
            token = base64.b64encode(b"admin:secret").decode()
            auth = (b"authorization", f"Basic {token}".encode())
            request_id = "44444444-5555-4666-8777-888888888888"
            body_reads = 0

            def request(headers, raw_body=b'{"confirm": true}'):
                delivered = False

                async def receive():
                    nonlocal delivered, body_reads
                    if delivered:
                        return {
                            "type": "http.request",
                            "body": b"",
                            "more_body": False,
                        }
                    delivered = True
                    body_reads += 1
                    return {
                        "type": "http.request",
                        "body": raw_body,
                        "more_body": False,
                    }

                return Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": (
                            "/price/api/v1/quick-link-rotations/"
                            "publish-now"
                        ),
                        "headers": headers,
                    },
                    receive,
                )

            try:
                with self.assertRaises(HTTPException) as anonymous:
                    asyncio.run(
                        module.publish_main_quick_link_post_now(request([]))
                    )
                self.assertEqual(anonymous.exception.status_code, 401)
                self.assertEqual(body_reads, 0)

                with self.assertRaises(HTTPException) as no_csrf:
                    asyncio.run(
                        module.publish_main_quick_link_post_now(
                            request([auth])
                        )
                    )
                self.assertEqual(no_csrf.exception.status_code, 403)
                self.assertEqual(body_reads, 0)

                headers = [
                    auth,
                    (b"x-requested-with", b"TexnikachPriceAdmin"),
                    (b"idempotency-key", request_id.encode()),
                    (b"content-type", b"application/json"),
                ]
                first = asyncio.run(
                    module.publish_main_quick_link_post_now(
                        request(headers)
                    )
                )
                replay = asyncio.run(
                    module.publish_main_quick_link_post_now(
                        request(headers)
                    )
                )
                self.assertFalse(first["duplicate"])
                self.assertTrue(replay["duplicate"])
                self.assertEqual(first["status"], "queued")
                self.assertEqual(replay["status"], "existing")
                self.assertEqual(replay["rotation_id"], first["rotation_id"])
                self.assertEqual(first["rotation_status"], "pending")
                self.assertEqual(first["phase"], "planned")
                self.assertTrue(first["secondary_title"])
                self.assertEqual(fake.sent, [])
                self.assertEqual(fake.edited, [])
                self.assertEqual(fake.pinned, [])
                self.assertEqual(fake.unpinned, [])
                manual = [
                    row for row in repo.list_quick_link_rotations(limit=20)
                    if row["trigger_source"] == "manual"
                ]
                self.assertEqual(len(manual), 1)

                conflict_headers = [
                    *headers[:2],
                    (
                        b"idempotency-key",
                        b"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    ),
                    headers[3],
                ]
                with self.assertRaises(HTTPException) as conflict:
                    asyncio.run(
                        module.publish_main_quick_link_post_now(
                            request(conflict_headers)
                        )
                    )
                self.assertEqual(conflict.exception.status_code, 409)
                self.assertEqual(
                    conflict.exception.detail,
                    "quick_link_rotation_already_active",
                )

                with self.assertRaises(HTTPException) as confirmation:
                    asyncio.run(
                        module.publish_main_quick_link_post_now(
                            request(headers, b'{"confirm": false}')
                        )
                    )
                self.assertEqual(confirmation.exception.status_code, 422)
                self.assertEqual(
                    confirmation.exception.detail,
                    "explicit_confirmation_required",
                )

                process_at = (
                    datetime.fromisoformat(first["scheduled_for"])
                    + timedelta(seconds=1)
                )
                self.assertEqual(
                    service.process_quick_link_rotations(process_at),
                    1,
                )
                sent_count = len(fake.sent)
                completed_replay = asyncio.run(
                    module.publish_main_quick_link_post_now(
                        request(headers)
                    )
                )
                self.assertTrue(completed_replay["duplicate"])
                self.assertEqual(
                    completed_replay["rotation_status"],
                    "done",
                )
                self.assertEqual(
                    completed_replay["phase"],
                    "completed",
                )
                self.assertEqual(len(fake.sent), sent_count)
            finally:
                (
                    module.settings,
                    module._repository,
                    module._service,
                    module._startup_error,
                ) = old

    def test_publish_main_endpoint_rejects_invalid_idempotency_key(self):
        module = importlib.import_module("price_server.router")
        with tempfile.TemporaryDirectory() as folder:
            configured = PriceSettings(
                enabled=True,
                db_path=Path(folder) / "price.db",
                legacy_html_path=Path(folder) / "index.html",
                admin_username="admin",
                admin_password="secret",
                sync_api_key="sync",
                telegram_bot_token="fake-token",
                telegram_channel_id="-1001463992448",
                telegram_channel_username="testchannel",
                product_sort_sheet_id="sheet",
                posts_sheet_name="Telegram Posts",
                timezone="Asia/Tashkent",
                scheduler_poll_seconds=1,
                sync_max_bytes=2_000_000,
                telegram_preview_channel_id="-1003922029862",
            )
            old = (
                module.settings,
                module._repository,
                module._service,
                module._startup_error,
            )
            module.settings = configured
            module._repository = PriceRepository(configured)
            module._service = object()
            module._startup_error = ""
            token = base64.b64encode(b"admin:secret").decode()
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
                delivered = True
                return {
                    "type": "http.request",
                    "body": b'{"confirm": true}',
                    "more_body": False,
                }

            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": (
                        "/price/api/v1/quick-link-rotations/publish-now"
                    ),
                    "headers": [
                        (
                            b"authorization",
                            f"Basic {token}".encode(),
                        ),
                        (
                            b"x-requested-with",
                            b"TexnikachPriceAdmin",
                        ),
                        (b"idempotency-key", b"not-a-uuid"),
                        (b"content-type", b"application/json"),
                    ],
                },
                receive,
            )
            try:
                with self.assertRaises(HTTPException) as invalid:
                    asyncio.run(
                        module.publish_main_quick_link_post_now(request)
                    )
                self.assertEqual(invalid.exception.status_code, 422)
                self.assertEqual(
                    invalid.exception.detail,
                    "valid_idempotency_key_required",
                )
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
