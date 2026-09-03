from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import MessageEntity
from telegram.error import BadRequest

from sales_photo_bot.config import Settings
from sales_photo_bot.dates import tashkent_today
from sales_photo_bot.orders import card_order_id, ensure_card_order_id
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import BOT_CARD_MARKER, SalesPhotoService


CHAT_ID = -1001234567890
CHECK_CHAT_ID = -1004340217539
TOKEN = "1234567890:" + "A" * 35


def settings(root: Path) -> Settings:
    return Settings(
        bot_token=TOKEN,
        chat_id=CHAT_ID,
        db_path=root / "sales.db",
        heartbeat_path=root / "heartbeat",
        delete_retry_seconds=1,
        source_edit_grace_seconds=0,
        startup_drain_seconds=0,
    )


def utf16_offset(value: str, needle: str) -> int:
    index = value.index(needle)
    return len(value[:index].encode("utf-16-le")) // 2


class CardOrderFormattingTests(unittest.TestCase):
    def test_inserts_id_after_marker_and_preserves_entities(self):
        body = BOT_CARD_MARKER + "🛒💵:\nrasxod:\n\n📞:\n\nНаличка"
        entity = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(body, "Наличка"),
            length=len("Наличка"),
        )

        result = ensure_card_order_id(body, (entity,), 7, max_length=1024)

        self.assertTrue(result.changed)
        self.assertTrue(result.body.startswith(BOT_CARD_MARKER + "🆔: 7\n\n🛒"))
        self.assertEqual(card_order_id(result.body), 7)
        self.assertEqual(result.entities[0].offset, utf16_offset(result.body, "Наличка"))

    def test_date_stays_first_and_id_is_restored(self):
        body = (
            BOT_CARD_MARKER
            + "📆: 31/08/2026\n\n📦 A16\n\n🛒💵:\nrasxod:\n\n📞:"
        )
        inserted = ensure_card_order_id(body, (), 19, max_length=1024)
        restored = ensure_card_order_id(inserted.body, (), 20, max_length=1024)

        self.assertTrue(
            inserted.body.startswith(
                BOT_CARD_MARKER + "📆: 31/08/2026\n🆔: 19\n\n📦 A16"
            )
        )
        self.assertEqual(card_order_id(restored.body), 20)
        self.assertEqual(restored.body.count("🆔:"), 1)

    def test_invalid_and_duplicate_id_rows_are_replaced_not_appended(self):
        body = (
            BOT_CARD_MARKER
            + "📆: 31/08/2026\n"
            + "🆔: abc\n"
            + "🆔: 99\n\n"
            + "🛒💵:\nrasxod:\n\n📞:"
        )

        result = ensure_card_order_id(body, (), 4, max_length=1024)

        self.assertTrue(result.changed)
        self.assertEqual(result.body.count("🆔:"), 1)
        self.assertIn("📆: 31/08/2026\n🆔: 4\n", result.body)
        self.assertEqual(card_order_id(result.body), 4)


class DailyOrderRepositoryTests(unittest.TestCase):
    def test_each_day_has_an_independent_durable_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repo = SalesPhotoRepository(path)
            cases = (
                (10, date(2026, 8, 31), 1),
                (11, date(2026, 8, 31), 2),
                (12, date(2026, 9, 1), 1),
                (13, date(2026, 8, 31), 3),
            )
            for source_id, sale_day, expected_id in cases:
                self.assertTrue(
                    repo.claim_photo(
                        CHAT_ID,
                        source_id,
                        f"unique-{source_id}",
                        source_file_id=f"file-{source_id}",
                        sale_date=sale_day,
                    )
                )
                self.assertEqual(
                    repo.daily_order_for_source(CHAT_ID, source_id),
                    (sale_day, expected_id),
                )

            reopened = SalesPhotoRepository(path)
            self.assertEqual(
                reopened.daily_order_for_source(CHAT_ID, 13),
                (date(2026, 8, 31), 3),
            )

    def test_backdated_tenth_sale_does_not_change_today_first_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            yesterday = date(2026, 8, 31)
            today = date(2026, 9, 1)
            published_today = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
            for source_id in range(10, 20):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"yesterday-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=yesterday,
                    at=published_today,
                )
                repo.mark_reposted(
                    CHAT_ID,
                    source_id,
                    source_id + 190,
                    at=published_today,
                )

            repo.claim_photo(
                CHAT_ID,
                20,
                "today-first",
                source_file_id="today-file",
                sale_date=today,
                at=published_today,
            )

            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 19),
                (yesterday, 10),
            )
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 20),
                (today, 1),
            )

    def test_failed_reservations_are_removed_before_new_card_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            sale_day = date(2026, 9, 1)
            for source_id in (10, 11):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"failed-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_failed(CHAT_ID, source_id, "TimedOut")

            repo.claim_photo(
                CHAT_ID,
                12,
                "successful",
                source_file_id="successful-file",
                sale_date=sale_day,
            )

            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 10))
            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 11))
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 12),
                (sale_day, 1),
            )

    def test_processing_reservations_compact_after_older_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            sale_day = date(2026, 9, 1)
            for source_id in (10, 11):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"processing-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
            repo.mark_failed(CHAT_ID, 10, "TimedOut")

            repo.claim_photo(
                CHAT_ID,
                12,
                "next",
                source_file_id="next-file",
                sale_date=sale_day,
            )

            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 10))
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 11),
                (sale_day, 1),
            )
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 12),
                (sale_day, 2),
            )

    def test_existing_cards_are_numbered_by_tashkent_creation_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repo = SalesPhotoRepository(path)
            created = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)
            for source_id, replacement_id in ((10, 200), (11, 201)):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"legacy-{source_id}",
                    source_file_id=f"file-{source_id}",
                    at=created,
                )
                repo.mark_reposted(CHAT_ID, source_id, replacement_id, at=created)
                repo.mark_complete(CHAT_ID, source_id, at=created)

            migrated = SalesPhotoRepository(path)

            self.assertEqual(
                migrated.daily_order_for_source(CHAT_ID, 10),
                (date(2026, 8, 31), 1),
            )
            self.assertEqual(
                migrated.daily_order_for_source(CHAT_ID, 11),
                (date(2026, 8, 31), 2),
            )
            self.assertEqual(
                [job.order_id for job in migrated.pending_order_backfills(CHAT_ID)],
                [1, 2],
            )

    def test_removed_sale_compacts_later_ids_and_next_sale_uses_count(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            sale_day = date(2026, 8, 31)
            for source_id in range(10, 15):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_reposted(CHAT_ID, source_id, source_id + 190)
                repo.mark_complete(CHAT_ID, source_id)
                repo.mark_order_card_applied(CHAT_ID, source_id)

            removed_day, changed = repo.mark_order_card_removed(CHAT_ID, 12)

            self.assertEqual(removed_day, sale_day)
            self.assertEqual(changed, 2)
            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 12))
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 13),
                (sale_day, 3),
            )
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 14),
                (sale_day, 4),
            )
            self.assertEqual(
                [job.order_id for job in repo.pending_order_backfills(CHAT_ID)],
                [3, 4],
            )

            repo.claim_photo(
                CHAT_ID,
                15,
                "unique-15",
                source_file_id="file-15",
                sale_date=sale_day,
            )
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 15),
                (sale_day, 5),
            )
            reopened = SalesPhotoRepository(repo.path)
            self.assertIsNone(reopened.daily_order_for_source(CHAT_ID, 12))
            self.assertEqual(
                reopened.daily_order_for_source(CHAT_ID, 15),
                (sale_day, 5),
            )


class ExistingCardBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_caption_gets_id_without_losing_manual_finance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sales.db"
            repo = SalesPhotoRepository(path)
            sale_day = tashkent_today()
            created = datetime(
                sale_day.year,
                sale_day.month,
                sale_day.day,
                10,
                0,
                tzinfo=timezone.utc,
            )
            repo.claim_photo(
                CHAT_ID,
                10,
                "legacy",
                source_file_id="file",
                at=created,
            )
            repo.mark_reposted(CHAT_ID, 10, 200, at=created)
            repo.mark_complete(CHAT_ID, 10, at=created)
            repo = SalesPhotoRepository(path)
            service = SalesPhotoService(
                replace(settings(root), check_chat_id=CHECK_CHAT_ID),
                repo,
            )
            current = (
                BOT_CARD_MARKER
                + "🛒💵: ACME $100\nrasxod: $3\n\n"
                "📞: +998 90 123 45 67\n\n"
                "Наличка\n💵: 101\n🇺🇿: 1 250 000"
            )
            forwarded = SimpleNamespace(
                message_id=900,
                chat_id=CHECK_CHAT_ID,
                caption=current,
                caption_entities=(),
                text=None,
                reply_markup=None,
            )
            bot = SimpleNamespace(
                forward_message=AsyncMock(return_value=forwarded),
                edit_message_caption=AsyncMock(),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )

            await service.backfill_order_cards(bot, ignore_delay=True)

            bot.forward_message.assert_awaited_once()
            forward_kwargs = bot.forward_message.await_args.kwargs
            self.assertEqual(forward_kwargs["chat_id"], CHECK_CHAT_ID)
            self.assertEqual(forward_kwargs["from_chat_id"], CHAT_ID)
            self.assertTrue(forward_kwargs["disable_notification"])
            bot.delete_message.assert_awaited_once_with(CHECK_CHAT_ID, 900)
            bot.edit_message_caption.assert_awaited_once()
            edited = bot.edit_message_caption.await_args.kwargs["caption"]
            self.assertTrue(edited.startswith(BOT_CARD_MARKER + "🆔: 1\n\n"))
            self.assertIn("🛒💵: ACME $100", edited)
            self.assertIn("rasxod: $3", edited)
            self.assertIn("🇺🇿: 1 250 000", edited)
            self.assertEqual(repo.pending_order_backfills(CHAT_ID), ())

    async def test_deleted_legacy_message_is_retired_without_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            sale_day = tashkent_today()
            created = datetime(
                sale_day.year,
                sale_day.month,
                sale_day.day,
                10,
                0,
                tzinfo=timezone.utc,
            )
            repo.claim_photo(
                CHAT_ID,
                10,
                "legacy",
                source_file_id="file",
                at=created,
            )
            repo.mark_reposted(CHAT_ID, 10, 200, at=created)
            repo.mark_complete(CHAT_ID, 10, at=created)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            bot = SimpleNamespace(
                forward_message=AsyncMock(
                    side_effect=BadRequest("Message to forward not found")
                ),
                delete_message=AsyncMock(),
            )

            await service.backfill_order_cards(bot, ignore_delay=True)

            self.assertEqual(repo.pending_order_backfills(CHAT_ID), ())
            bot.delete_message.assert_not_awaited()

    async def test_ambiguous_forward_update_is_deleted_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            service._order_backfill_forward_sources[200] = float("inf")
            message = SimpleNamespace(
                chat_id=CHAT_ID,
                chat=SimpleNamespace(id=CHAT_ID),
                message_id=900,
                text=BOT_CARD_MARKER + "🆔: 1\n\n🛒💵:",
                entities=(),
                forward_origin=SimpleNamespace(message_id=200),
            )
            bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))

            await service.on_text(
                SimpleNamespace(effective_message=message),
                SimpleNamespace(bot=bot),
            )

            bot.delete_message.assert_awaited_once_with(CHAT_ID, 900)

    async def test_audit_detects_deleted_card_and_compacts_the_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            sale_day = tashkent_today()
            for source_id in (10, 11, 12):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_reposted(CHAT_ID, source_id, source_id + 190)
                repo.mark_complete(CHAT_ID, source_id)
                repo.mark_order_card_applied(CHAT_ID, source_id)
            service = SalesPhotoService(settings(root), repo)

            async def edit_markup(**kwargs):
                if kwargs["message_id"] == 201:
                    raise BadRequest("Message to edit not found")
                raise BadRequest("Message is not modified")

            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(side_effect=edit_markup)
            )

            removed = await service.audit_deleted_order_cards(bot, force=True)

            self.assertEqual(removed, 1)
            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 11))
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 12),
                (sale_day, 2),
            )
            pending = repo.pending_order_backfills(CHAT_ID)
            self.assertEqual(
                [(job.source_message_id, job.order_id) for job in pending],
                [(12, 2)],
            )


if __name__ == "__main__":
    unittest.main()
