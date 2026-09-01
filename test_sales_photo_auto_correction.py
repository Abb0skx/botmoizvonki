from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

from sales_photo_bot.auto_correction import (
    AUTO_CORRECTION_CLOCKS,
    latest_auto_correction_slot,
    next_auto_correction_slot,
)
from sales_photo_bot.config import Settings
from sales_photo_bot.dates import TASHKENT_TZ
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import BOT_CARD_MARKER, SalesPhotoService


CHAT_ID = -1001234567890
TOKEN = "1234567890:" + "A" * 35
UTC = timezone.utc


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


def raw_card(order_id: int = 99) -> str:
    return (
        BOT_CARD_MARKER
        + "📆: 1/1/2020\n"
        + f"🆔: {order_id}\n\n"
        + "🛒💵:A3 Mirsaid 87\n"
        + "rasxod:\n\n"
        + "📞: 901234567\n\n"
        + "Наличка\n💵:100\n🇺🇿:1000000\n\n"
        + "Card/Terminal/Paynet\n💵:\n"
        + "🇺🇿:200000 paynet Abbos"
    )


class AutoCorrectionClockTests(unittest.TestCase):
    def test_exact_requested_tashkent_schedule(self):
        self.assertEqual(
            AUTO_CORRECTION_CLOCKS,
            (
                time(7, 0),
                time(10, 0),
                time(11, 0),
                time(12, 0),
                time(13, 0),
                time(14, 0),
                time(15, 0),
                time(16, 0),
                time(17, 0),
                time(18, 0),
                time(18, 30),
                time(19, 0),
                time(20, 0),
                time(21, 0),
                time(22, 0),
                time(23, 0),
            ),
        )

    def test_slots_are_computed_in_tashkent_and_cross_midnight(self):
        before_first = datetime(2026, 9, 1, 6, 59, tzinfo=TASHKENT_TZ)
        at_half_hour = datetime(2026, 9, 1, 18, 30, tzinfo=TASHKENT_TZ)
        after_last = datetime(2026, 9, 1, 23, 1, tzinfo=TASHKENT_TZ)

        self.assertEqual(
            next_auto_correction_slot(before_first),
            datetime(2026, 9, 1, 7, 0, tzinfo=TASHKENT_TZ),
        )
        self.assertEqual(latest_auto_correction_slot(at_half_hour), at_half_hour)
        self.assertEqual(
            next_auto_correction_slot(after_last),
            datetime(2026, 9, 2, 7, 0, tzinfo=TASHKENT_TZ),
        )
        self.assertEqual(
            next_auto_correction_slot(
                datetime(2026, 9, 1, 1, 59, tzinfo=UTC)
            ),
            datetime(2026, 9, 1, 7, 0, tzinfo=TASHKENT_TZ),
        )


class AutoCorrectionRepositoryTests(unittest.TestCase):
    def test_replacement_atomically_creates_one_debounce_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique",
                source_file_id="file",
                sale_date=date(2026, 9, 1),
            )
            created = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)

            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200, at=created), "recorded")
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200, at=created), "same")
            state = repo.auto_correction_state(CHAT_ID)

            self.assertEqual(state.last_new_at, created)
            self.assertEqual(state.event_generation, 1)
            self.assertEqual(state.completed_event_generation, 0)

    def test_window_query_uses_sale_date_not_creation_date(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            for source_id, sale_day in enumerate(
                (
                    date(2026, 8, 29),
                    date(2026, 8, 30),
                    date(2026, 8, 31),
                    date(2026, 9, 1),
                ),
                start=10,
            ):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_reposted(CHAT_ID, source_id, source_id + 190)

            candidates = repo.auto_correction_candidates(
                CHAT_ID,
                date(2026, 8, 30),
                date(2026, 9, 1),
            )

            self.assertEqual(
                [candidate.sale_date for candidate in candidates],
                [
                    date(2026, 8, 30),
                    date(2026, 8, 31),
                    date(2026, 9, 1),
                ],
            )

    def test_new_generation_during_sweep_remains_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            first = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
            repo.note_auto_correction_new_card(CHAT_ID, at=first)
            captured = repo.auto_correction_state(CHAT_ID).event_generation
            repo.note_auto_correction_new_card(
                CHAT_ID,
                at=first + timedelta(minutes=6),
            )

            repo.mark_auto_correction_complete(
                CHAT_ID,
                completed_event_generation=captured,
            )
            state = repo.auto_correction_state(CHAT_ID)

            self.assertEqual(state.completed_event_generation, captured)
            self.assertGreater(
                state.event_generation,
                state.completed_event_generation,
            )

    def test_unpublished_failures_do_not_consume_sold_order_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            sale_day = date(2026, 9, 1)
            repo.claim_photo(
                CHAT_ID,
                10,
                "failed",
                source_file_id="failed-file",
                sale_date=sale_day,
            )
            repo.mark_failed(CHAT_ID, 10, "TimedOut")
            repo.claim_photo(
                CHAT_ID,
                11,
                "published",
                source_file_id="published-file",
                sale_date=sale_day,
            )
            repo.mark_reposted(CHAT_ID, 11, 201)

            released, changed = repo.compact_recent_daily_orders(
                CHAT_ID,
                sale_day - timedelta(days=2),
                sale_day,
            )

            self.assertEqual((released, changed), (1, 1))
            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 10))
            self.assertEqual(
                repo.daily_order_for_replacement(CHAT_ID, 201),
                (sale_day, 1),
            )

            # A late successful retry receives the next ID atomically instead
            # of restoring the old gap-producing reservation.
            self.assertEqual(
                repo.record_replacement(CHAT_ID, 10, 200),
                "recorded",
            )
            self.assertEqual(
                repo.daily_order_for_replacement(CHAT_ID, 200),
                (sale_day, 2),
            )


class RecentCardCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_only_today_yesterday_and_day_before(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            current_day = date(2026, 9, 1)
            cards: dict[int, str] = {}
            for index, sale_day in enumerate(
                (
                    current_day - timedelta(days=3),
                    current_day - timedelta(days=2),
                    current_day - timedelta(days=1),
                    current_day,
                )
            ):
                source_id = 10 + index
                replacement_id = 200 + index
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_reposted(CHAT_ID, source_id, replacement_id)
                repo.mark_complete(CHAT_ID, source_id)
                cards[replacement_id] = raw_card()

            async def forward_message(**kwargs):
                source_id = int(kwargs["message_id"])
                return SimpleNamespace(
                    message_id=900 + source_id,
                    chat_id=CHAT_ID,
                    caption=cards[source_id],
                    caption_entities=(),
                    text=None,
                    reply_markup=None,
                )

            bot = SimpleNamespace(
                forward_message=AsyncMock(side_effect=forward_message),
                edit_message_caption=AsyncMock(),
                edit_message_text=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root), repo)

            success = await service.auto_correct_recent_cards(
                bot,
                reason="test",
                reference_date=current_day,
            )

            self.assertTrue(success)
            forwarded_ids = {
                int(call.kwargs["message_id"])
                for call in bot.forward_message.await_args_list
            }
            self.assertEqual(forwarded_ids, {201, 202, 203})
            self.assertEqual(bot.edit_message_caption.await_count, 3)
            for call in bot.edit_message_caption.await_args_list:
                normalized = call.kwargs["caption"]
                self.assertIn("🆔: 1", normalized)
                self.assertIn("🛒💵:A3 Mirsaid 87$", normalized)
                self.assertIn("📞: +998 90 123 45 67", normalized)
                self.assertIn("💵:100$", normalized)
                self.assertIn("🇺🇿:1 000 000 So'm", normalized)
                self.assertIn("🇺🇿:200 000 So'm paynet Abbos", normalized)
                self.assertNotIn("📆: 1/1/2020", normalized)

    async def test_deleted_middle_card_compacts_later_id_in_same_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            sale_day = date(2026, 9, 1)
            bodies: dict[int, str] = {}
            for offset in range(3):
                source_id = 10 + offset
                replacement_id = 200 + offset
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_reposted(CHAT_ID, source_id, replacement_id)
                repo.mark_complete(CHAT_ID, source_id)
                bodies[replacement_id] = (
                    BOT_CARD_MARKER
                    + f"🆔: {offset + 1}\n\n"
                    + "🛒💵:\nrasxod:\n\n📞:"
                )

            async def forward_message(**kwargs):
                replacement_id = int(kwargs["message_id"])
                if replacement_id == 201:
                    raise BadRequest("Message to forward not found")
                return SimpleNamespace(
                    message_id=900 + replacement_id,
                    chat_id=CHAT_ID,
                    caption=bodies[replacement_id],
                    caption_entities=(),
                    text=None,
                    reply_markup=None,
                )

            bot = SimpleNamespace(
                forward_message=AsyncMock(side_effect=forward_message),
                edit_message_caption=AsyncMock(),
                edit_message_text=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root), repo)

            success = await service.auto_correct_recent_cards(
                bot,
                reason="test-delete",
                reference_date=sale_day,
            )

            self.assertTrue(success)
            self.assertIsNone(repo.daily_order_for_source(CHAT_ID, 11))
            self.assertEqual(
                repo.daily_order_for_source(CHAT_ID, 12),
                (sale_day, 2),
            )
            edited = bot.edit_message_caption.await_args.kwargs["caption"]
            self.assertIn("🆔: 2", edited)
            self.assertNotIn("🆔: 3", edited)

    async def test_registered_forward_without_marker_is_deleted_not_reposted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            service._order_backfill_forward_sources[200] = float("inf")
            forwarded = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=900,
                caption="🛒💵: ACME 87\nrasxod:\n\n📞:",
                photo=(
                    SimpleNamespace(
                        file_id="forwarded",
                        file_unique_id="forwarded-unique",
                        width=100,
                        height=100,
                    ),
                ),
                forward_origin=SimpleNamespace(message_id=200),
                from_user=SimpleNamespace(id=50, is_bot=False),
            )
            bot = SimpleNamespace(
                delete_message=AsyncMock(return_value=True),
                send_photo=AsyncMock(),
            )

            await service.on_photo(
                SimpleNamespace(effective_message=forwarded),
                SimpleNamespace(bot=bot),
            )

            bot.delete_message.assert_awaited_once_with(CHAT_ID, 900)
            bot.send_photo.assert_not_awaited()

    async def test_one_card_failure_waits_for_next_trigger_not_minute_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            sale_day = date(2026, 9, 1)
            for offset in range(2):
                source_id = 10 + offset
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    sale_date=sale_day,
                )
                repo.mark_reposted(CHAT_ID, source_id, 200 + offset)
                repo.mark_complete(CHAT_ID, source_id)

            async def forward_message(**kwargs):
                replacement_id = int(kwargs["message_id"])
                if replacement_id == 200:
                    raise RuntimeError("temporary Telegram failure")
                return SimpleNamespace(
                    message_id=901,
                    chat_id=CHAT_ID,
                    caption=raw_card(order_id=2),
                    caption_entities=(),
                    text=None,
                    reply_markup=None,
                )

            bot = SimpleNamespace(
                forward_message=AsyncMock(side_effect=forward_message),
                edit_message_caption=AsyncMock(),
                edit_message_text=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                delete_message=AsyncMock(return_value=True),
            )
            service = SalesPhotoService(settings(root), repo)

            completed = await service.auto_correct_recent_cards(
                bot,
                reason="test-partial-failure",
                reference_date=sale_day,
            )

            self.assertTrue(completed)
            self.assertEqual(bot.forward_message.await_count, 2)
            bot.edit_message_caption.assert_awaited_once()


class AutoCorrectionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_before_seven_waits_for_seven_not_previous_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            now = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)  # 06:00 Tashkent
            waits: list[float] = []

            async def capture_wait(timeout: float):
                waits.append(timeout)
                raise asyncio.CancelledError

            service._wait_for_auto_correction_wake = capture_wait
            service.auto_correct_recent_cards = AsyncMock(return_value=True)
            with patch("sales_photo_bot.service.utc_now", return_value=now):
                with self.assertRaises(asyncio.CancelledError):
                    await service._auto_correction_scheduler(SimpleNamespace())

            service.auto_correct_recent_cards.assert_not_awaited()
            self.assertAlmostEqual(waits[0], 3600, delta=0.01)

    async def test_new_card_at_four_fifty_nine_restarts_full_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            start = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)  # 08:00 Tashkent
            repo.mark_auto_correction_complete(
                CHAT_ID,
                schedule_done_through=datetime(2026, 9, 1, 2, 0, tzinfo=UTC),
            )
            repo.note_auto_correction_new_card(CHAT_ID, at=start)
            now = [start]
            waits: list[float] = []

            async def fake_wait(timeout: float):
                waits.append(timeout)
                if len(waits) == 1:
                    now[0] = start + timedelta(minutes=4, seconds=59)
                    repo.note_auto_correction_new_card(CHAT_ID, at=now[0])
                    return
                raise asyncio.CancelledError

            service._wait_for_auto_correction_wake = fake_wait
            with patch("sales_photo_bot.service.utc_now", side_effect=lambda: now[0]):
                with self.assertRaises(asyncio.CancelledError):
                    await service._auto_correction_scheduler(SimpleNamespace())

            self.assertAlmostEqual(waits[0], 300, delta=0.01)
            self.assertAlmostEqual(waits[1], 300, delta=0.01)

    async def test_time_and_event_due_together_use_one_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            service = SalesPhotoService(settings(root), repo)
            now = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)  # 10:00 Tashkent
            repo.mark_auto_correction_complete(
                CHAT_ID,
                schedule_done_through=datetime(2026, 9, 1, 2, 0, tzinfo=UTC),
            )
            repo.note_auto_correction_new_card(
                CHAT_ID,
                at=now - timedelta(minutes=5),
            )
            service.auto_correct_recent_cards = AsyncMock(return_value=True)

            async def stop_after_success(timeout: float):
                raise asyncio.CancelledError

            service._wait_for_auto_correction_wake = stop_after_success
            with patch("sales_photo_bot.service.utc_now", return_value=now):
                with self.assertRaises(asyncio.CancelledError):
                    await service._auto_correction_scheduler(SimpleNamespace())

            service.auto_correct_recent_cards.assert_awaited_once()
            reason = service.auto_correct_recent_cards.await_args.kwargs["reason"]
            self.assertIn("time:10:00", reason)
            self.assertIn("event:quiet-5m", reason)
            state = repo.auto_correction_state(CHAT_ID)
            self.assertEqual(
                state.completed_event_generation,
                state.event_generation,
            )
            self.assertEqual(state.schedule_done_through, now)


if __name__ == "__main__":
    unittest.main()
