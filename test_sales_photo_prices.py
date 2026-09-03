from __future__ import annotations

import unittest
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import MessageEntity

from sales_photo_bot.config import Settings
from sales_photo_bot.dates import tashkent_today
from sales_photo_bot.prices import normalize_card_prices
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import BOT_CARD_MARKER
from sales_photo_bot.service import SalesPhotoService


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


def utf16_offset(value: str, needle: str, start: int = 0) -> int:
    index = value.index(needle, start)
    return len(value[:index].encode("utf-16-le")) // 2


class PriceCardFormattingTests(unittest.TestCase):
    def test_normalizes_complete_user_example(self):
        body = (
            BOT_CARD_MARKER
            + "🆔: 3\n\n"
            + "🛒💵:A3 Mirsaid 87\n"
            + "rasxod:\n\n"
            + "📞:\n\n"
            + "Наличка\n"
            + "💵:\n"
            + "🇺🇿:1000000\n\n"
            + "Card/Terminal/Paynet\n"
            + "💵:\n"
            + "🇺🇿:200000 paynet Abbos"
        )

        result = normalize_card_prices(body, max_length=1024)

        self.assertTrue(result.changed)
        self.assertIn("🛒💵:A3 Mirsaid 87$", result.body)
        self.assertIn("🇺🇿:1 000 000 So'm", result.body)
        self.assertIn("🇺🇿:200 000 So'm paynet Abbos", result.body)
        bold_offsets = {
            int(entity.offset)
            for entity in result.entities
            if entity.type == MessageEntity.BOLD
        }
        self.assertIn(utf16_offset(result.body, "So'm"), bold_offsets)
        second_start = result.body.index("So'm") + len("So'm")
        self.assertIn(
            utf16_offset(result.body, "So'm", second_start),
            bold_offsets,
        )

    def test_supplier_code_is_not_mistaken_for_a_price(self):
        body = BOT_CARD_MARKER + "🛒💵:A3 Mirsaid\nrasxod:\n\n📞:"

        result = normalize_card_prices(body, max_length=1024)

        self.assertFalse(result.changed)
        self.assertEqual(result.body, body)

    def test_header_uses_threshold_and_is_idempotent(self):
        cases = (
            ("🛒💵: ACME 4999", "🛒💵: ACME 4999$"),
            ("🛒💵: ACME 5000", "🛒💵: ACME 5 000 So'm"),
            ("🛒💵: ACME $87", "🛒💵: ACME 87$"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                body = source + "\nrasxod:\n\n📞:"
                first = normalize_card_prices(body, max_length=1024)
                second = normalize_card_prices(
                    first.body,
                    first.entities,
                    max_length=1024,
                )
                self.assertEqual(first.body.splitlines()[0], expected)
                self.assertFalse(second.changed)

    def test_exact_dollar_rows_get_suffix_but_expense_does_not(self):
        body = (
            "🛒💵:\nrasxod:100\n\n📞:\n\n"
            "Наличка\n💵:87\n🇺🇿:\n\n"
            "Card/Terminal/Paynet\n💵: 200 manager\n🇺🇿:"
        )

        result = normalize_card_prices(body, max_length=1024)

        self.assertIn("rasxod:100", result.body)
        self.assertIn("💵:87$", result.body)
        self.assertIn("💵: 200$ manager", result.body)

    def test_preserves_and_shifts_unrelated_bold_headings(self):
        body = (
            "🛒💵: ACME 87\nrasxod:\n\n📞:\n\n"
            "Наличка\n💵:\n🇺🇿:1000000"
        )
        heading = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(body, "Наличка"),
            length=len("Наличка"),
        )

        result = normalize_card_prices(body, (heading,), max_length=1024)

        heading_entities = [
            entity
            for entity in result.entities
            if int(entity.length) == len("Наличка")
        ]
        self.assertEqual(len(heading_entities), 1)
        self.assertEqual(
            int(heading_entities[0].offset),
            utf16_offset(result.body, "Наличка"),
        )

    def test_uzs_currency_is_bold_even_when_text_is_already_canonical(self):
        body = "🛒💵:\nrasxod:\n\n📞:\n\n🇺🇿:1 000 000 So'm"

        first = normalize_card_prices(body, max_length=1024)
        second = normalize_card_prices(
            first.body,
            first.entities,
            max_length=1024,
        )

        self.assertTrue(first.changed)
        self.assertEqual(len(first.entities), 1)
        self.assertEqual(first.entities[0].type, MessageEntity.BOLD)
        self.assertFalse(second.changed)

    def test_suffix_entity_is_preserved_when_amount_is_replaced(self):
        body = (
            "🛒💵:\nrasxod:\n\n📞:\n\n"
            "Card/Terminal/Paynet\n🇺🇿:200000 paynet Abbos"
        )
        note = MessageEntity(
            type=MessageEntity.ITALIC,
            offset=utf16_offset(body, "paynet Abbos"),
            length=len("paynet Abbos"),
        )

        result = normalize_card_prices(body, (note,), max_length=1024)

        italic = next(
            entity
            for entity in result.entities
            if entity.type == MessageEntity.ITALIC
        )
        self.assertEqual(
            int(italic.offset),
            utf16_offset(result.body, "paynet Abbos"),
        )
        self.assertIn("🇺🇿:200 000 So'm paynet Abbos", result.body)

    def test_fails_closed_if_an_entity_crosses_a_replaced_value(self):
        body = "🛒💵: ACME 87\nrasxod:\n\n📞:"
        crossing = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(body, "ACME"),
            length=len("ACME 87\nrasxod"),
        )

        result = normalize_card_prices(body, (crossing,), max_length=1024)

        self.assertFalse(result.changed)
        self.assertEqual(result.body, body)
        self.assertEqual(result.entities, (crossing,))


class ExistingPriceBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_card_is_normalized_without_losing_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "legacy",
                source_file_id="file",
                sale_date=tashkent_today(),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)
            repo.mark_complete(CHAT_ID, 10)
            repo.mark_order_card_applied(CHAT_ID, 10)
            service = SalesPhotoService(settings(root), repo)
            current = (
                BOT_CARD_MARKER
                + "🆔: 3\n\n"
                + "🛒💵:A3 Mirsaid 87\n"
                + "rasxod:\n\n📞:\n\n"
                + "Наличка\n💵:\n🇺🇿:1000000\n\n"
                + "Card/Terminal/Paynet\n💵:\n"
                + "🇺🇿:200000 paynet Abbos"
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

            self.assertEqual(len(repo.pending_price_backfills(CHAT_ID)), 1)
            await service.backfill_price_cards(bot, ignore_delay=True)

            edited = bot.edit_message_caption.await_args.kwargs
            self.assertIn("🛒💵:A3 Mirsaid 87$", edited["caption"])
            self.assertIn("🇺🇿:1 000 000 So'm", edited["caption"])
            self.assertIn(
                "🇺🇿:200 000 So'm paynet Abbos",
                edited["caption"],
            )
            self.assertEqual(
                sum(
                    entity.type == MessageEntity.BOLD
                    and int(entity.length) == len("So'm")
                    for entity in edited["caption_entities"]
                ),
                2,
            )
            bot.delete_message.assert_awaited_once_with(CHAT_ID, 900)
            self.assertEqual(repo.pending_price_backfills(CHAT_ID), ())

    async def test_generated_card_can_be_marked_without_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "new",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)

            self.assertEqual(len(repo.pending_price_backfills(CHAT_ID)), 1)
            self.assertTrue(repo.mark_price_card_applied(CHAT_ID, 10))
            self.assertEqual(repo.pending_price_backfills(CHAT_ID), ())


if __name__ == "__main__":
    unittest.main()
