from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import MessageEntity

from sales_photo_bot.config import Settings
from sales_photo_bot.orders import card_order_id, ensure_card_order_id
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import BOT_CARD_MARKER, SalesPhotoService


CHAT_ID = -1001234567890
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


class ExistingCardBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_caption_gets_id_without_losing_manual_finance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sales.db"
            repo = SalesPhotoRepository(path)
            created = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
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
            service = SalesPhotoService(settings(root), repo)
            current = (
                BOT_CARD_MARKER
                + "🛒💵: ACME $100\nrasxod: $3\n\n"
                "📞: +998 90 123 45 67\n\n"
                "Наличка\n💵: 101\n🇺🇿: 1 250 000"
            )
            forwarded = SimpleNamespace(
                message_id=900,
                chat_id=CHAT_ID,
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
            bot.delete_message.assert_awaited_once_with(CHAT_ID, 900)
            bot.edit_message_caption.assert_awaited_once()
            edited = bot.edit_message_caption.await_args.kwargs["caption"]
            self.assertTrue(edited.startswith(BOT_CARD_MARKER + "🆔: 1\n\n"))
            self.assertIn("🛒💵: ACME $100", edited)
            self.assertIn("rasxod: $3", edited)
            self.assertIn("🇺🇿: 1 250 000", edited)
            self.assertEqual(repo.pending_order_backfills(CHAT_ID), ())


if __name__ == "__main__":
    unittest.main()
