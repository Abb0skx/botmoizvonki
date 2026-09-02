from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from sales_photo_bot.config import Settings
from sales_photo_bot.reminders import (
    ADMIN_MENTION,
    CLEANUP_CLOCKS,
    FILL_REMINDER_MARKER,
    NOTICE_CLOCKS,
    build_fill_reminder,
    inspect_fill_fields,
    latest_active_slot,
    next_slot,
)
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import BOT_CARD_MARKER, SalesPhotoService


UTC = timezone.utc
CHAT_ID = -1001234567890
TOKEN = "1234567890:" + "A" * 35
CARD = (
    BOT_CARD_MARKER
    + "🆔: 1\n\n"
    "🛒💵:A30 Umiddan 170$\n"
    "rasxod:\n\n"
    "📞:\n\n"
    "Наличка\n💵:\n🇺🇿:\n\n"
    "Card/Terminal/Paynet\n💵:\n🇺🇿:"
)


def settings(root: Path) -> Settings:
    return Settings(
        bot_token=TOKEN,
        chat_id=CHAT_ID,
        db_path=root / "sales.db",
        heartbeat_path=root / "heartbeat",
        delete_retry_seconds=1,
        source_edit_grace_seconds=1,
        startup_drain_seconds=1,
    )


class FillReminderFormattingTests(unittest.TestCase):
    def test_only_non_whitespace_after_colon_counts_as_filled(self):
        empty = inspect_fill_fields("🛒💵:   \n📞:\t", None)
        arbitrary = inspect_fill_fields("🛒💵: x\n📞: не номер", "Ali")

        self.assertFalse(empty.supplier_price)
        self.assertFalse(empty.phone)
        self.assertFalse(empty.complete)
        self.assertTrue(arbitrary.complete)

    def test_notice_lists_only_missing_fields_and_always_mentions_admin(self):
        check = inspect_fill_fields("🛒💵: заполнено\n📞:", None)
        text = build_fill_reminder(check)

        self.assertTrue(text.startswith(FILL_REMINDER_MARKER + ADMIN_MENTION))
        self.assertIn("номер телефона 📞", text)
        self.assertIn("выберите менеджера", text)
        self.assertNotIn("поставщика и цену", text)

    def test_selected_manager_is_shown(self):
        check = inspect_fill_fields("🛒💵:\n📞: x", "Otabek")
        text = build_fill_reminder(check)

        self.assertIn("Менеджер: Otabek", text)
        self.assertIn("поставщика и цену 🛒💵", text)
        self.assertNotIn("выберите менеджера", text)


class FillReminderScheduleTests(unittest.TestCase):
    def test_requested_clocks(self):
        self.assertEqual(NOTICE_CLOCKS, tuple(time(hour) for hour in range(11, 22)))
        self.assertEqual(CLEANUP_CLOCKS[0], time(10, 15))
        self.assertEqual(CLEANUP_CLOCKS[-1], time(21, 15))
        self.assertTrue(
            all(
                (
                    datetime.combine(date(2000, 1, 1), later)
                    - datetime.combine(date(2000, 1, 1), earlier)
                ).total_seconds()
                == 30 * 60
                for earlier, later in zip(CLEANUP_CLOCKS, CLEANUP_CLOCKS[1:])
            )
        )

    def test_slot_helpers_do_not_send_old_notice_outside_window(self):
        at_1159 = datetime(2026, 9, 1, 11, 59, tzinfo=timezone.utc).astimezone(
            timezone.utc
        )
        # 11:59 UTC is 16:59 in Tashkent, so the 16:00 notice is still active.
        self.assertEqual(
            latest_active_slot(
                at_1159,
                NOTICE_CLOCKS,
                grace=timedelta(minutes=59, seconds=59),
            ).hour,
            16,
        )
        late = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)  # 23:00 Tashkent
        self.assertIsNone(
            latest_active_slot(
                late,
                NOTICE_CLOCKS,
                grace=timedelta(minutes=59, seconds=59),
            )
        )
        self.assertEqual(next_slot(late, NOTICE_CLOCKS).hour, 11)


class FillReminderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_reminder_is_never_treated_as_a_short_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            reminder = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=300,
                text=FILL_REMINDER_MARKER
                + ADMIN_MENTION
                + "\n\nПожалуйста, заполните:\n• номер телефона 📞",
                entities=(),
                caption=None,
            )

            await service.on_text(
                SimpleNamespace(effective_message=reminder),
                SimpleNamespace(bot=SimpleNamespace()),
            )

            self.assertNotIn(300, repo.tracked_message_ids(CHAT_ID))

    async def test_publish_replaces_old_notice_and_cleanup_removes_when_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            repo.mark_reposted(CHAT_ID, 10, 200)
            repo.mark_complete(CHAT_ID, 10)
            next_message_id = 800

            async def send_message(**kwargs):
                nonlocal next_message_id
                next_message_id += 1
                return SimpleNamespace(message_id=next_message_id)

            bot = SimpleNamespace(
                forward_message=AsyncMock(),
                send_message=AsyncMock(side_effect=send_message),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )

            sent = bot.send_message.await_args.kwargs
            self.assertEqual(sent["reply_parameters"].message_id, 200)
            self.assertIn(ADMIN_MENTION, sent["text"])
            self.assertIn("номер телефона", sent["text"])
            self.assertIn("выберите менеджера", sent["text"])
            first_reminder = next_message_id

            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )
            self.assertIn(
                call(CHAT_ID, first_reminder),
                bot.delete_message.await_args_list,
            )
            second_reminder = next_message_id

            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            self.assertTrue(
                repo.sync_sale_details(
                    CHAT_ID,
                    200,
                    ("+998 90 123 45 67",),
                    "A30",
                    supplier_price_filled=True,
                )
            )
            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )

            self.assertIn(
                call(CHAT_ID, second_reminder),
                bot.delete_message.await_args_list,
            )
            candidate = repo.fill_reminder_candidates(
                CHAT_ID,
                date(2026, 9, 1),
                date(2026, 9, 1),
            )[0]
            self.assertIsNone(candidate.reminder_message_id)

            repo.claim_photo(
                CHAT_ID,
                11,
                "old-unique",
                source_file_id="old-file",
                sale_date=date(2026, 8, 29),
            )
            repo.mark_reposted(CHAT_ID, 11, 201)
            repo.mark_complete(CHAT_ID, 11)
            repo.record_fill_reminder(CHAT_ID, 11, 777)

            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )

            self.assertIn(call(CHAT_ID, 777), bot.delete_message.await_args_list)
            bot.forward_message.assert_not_awaited()
            self.assertEqual(
                repo.fill_reminders_outside_window(
                    CHAT_ID,
                    date(2026, 8, 30),
                    date(2026, 9, 1),
                ),
                (),
            )


if __name__ == "__main__":
    unittest.main()
