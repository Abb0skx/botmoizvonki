from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from telegram.error import BadRequest, NetworkError

from sales_photo_bot.config import Settings
from sales_photo_bot.reminders import (
    ADMIN_MENTION,
    FILL_REMINDER_MARKER,
    NOTICE_CLOCKS,
    build_signed_fill_reminder,
    build_fill_reminder,
    extract_reminder_token,
    inspect_fill_fields,
    latest_active_slot,
    next_slot,
)
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import (
    BOT_CARD_MARKER,
    REMINDER_CLEANUP_INTERVAL_SECONDS,
    SalesPhotoService,
)


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
        dash = inspect_fill_fields("🛒💵: x\n📞: -", "Ali")
        no_number = inspect_fill_fields("🛒💵: x\n📞: Без номера", "Ali")

        self.assertFalse(empty.supplier_price)
        self.assertFalse(empty.phone)
        self.assertFalse(empty.complete)
        self.assertTrue(arbitrary.complete)
        self.assertTrue(dash.complete)
        self.assertTrue(no_number.complete)

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
        self.assertEqual(NOTICE_CLOCKS, tuple(datetime.min.replace(hour=hour).time() for hour in range(11, 22)))
        self.assertEqual(REMINDER_CLEANUP_INTERVAL_SECONDS, 5 * 60)

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
    @staticmethod
    def _forwarded(message_id: int, body: str):
        return SimpleNamespace(
            message_id=message_id,
            caption=body,
            caption_entities=(),
            text=None,
            entities=(),
        )

    @staticmethod
    def _bot(body: str):
        forward_id = 900
        reminder_id = 800

        async def forward_message(**_kwargs):
            nonlocal forward_id
            forward_id += 1
            return FillReminderServiceTests._forwarded(forward_id, body)

        async def send_message(**_kwargs):
            nonlocal reminder_id
            reminder_id += 1
            return SimpleNamespace(message_id=reminder_id)

        return SimpleNamespace(
            forward_message=AsyncMock(side_effect=forward_message),
            send_message=AsyncMock(side_effect=send_message),
            edit_message_text=AsyncMock(return_value=True),
            edit_message_caption=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
        )

    @staticmethod
    def _sale(
        repo: SalesPhotoRepository,
        source_id: int = 10,
        replacement_id: int = 200,
        sale_date: date = date(2026, 9, 1),
    ):
        repo.claim_photo(
            CHAT_ID,
            source_id,
            f"unique-{source_id}",
            source_file_id=f"file-{source_id}",
            sale_date=sale_date,
        )
        repo.mark_reposted(CHAT_ID, source_id, replacement_id)
        repo.mark_complete(CHAT_ID, source_id)

    async def test_placeholder_phone_is_persisted_as_intentionally_filled(self):
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
            service = SalesPhotoService(settings(root), repo)

            service._sync_sale_details(
                CHAT_ID,
                200,
                CARD.replace("📞:", "📞: Без номера"),
            )

            candidate = repo.fill_reminder_candidates(
                CHAT_ID,
                date(2026, 9, 1),
                date(2026, 9, 1),
            )[0]
            self.assertTrue(candidate.phone_filled)

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

    async def test_signed_payload_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            attempt = repo.create_fill_reminder_attempt(CHAT_ID, 10, 200)
            payload = build_signed_fill_reminder(
                inspect_fill_fields("🛒💵:\n📞:", None),
                repo.fill_reminder_token_url(attempt.attempt_id),
            )

            token = extract_reminder_token(payload.text, payload.entities)

            self.assertIsNotNone(token)
            self.assertTrue(repo.valid_fill_reminder_token(*token))
            self.assertIn(ADMIN_MENTION, payload.text)

    async def test_hourly_publish_keeps_one_existing_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )
            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(bot.send_message.await_count, 1)
            attempts = repo.open_fill_reminder_attempts(CHAT_ID, 10)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].state, "confirmed")
            self.assertEqual(attempts[0].telegram_message_id, 801)

    async def test_live_card_removes_stale_cached_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            repo.record_fill_reminder(CHAT_ID, 10, 777)
            complete = CARD.replace("📞:", "📞: -")
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0))
            bot = self._bot(complete)
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )

            self.assertIn(call(CHAT_ID, 777), bot.delete_message.await_args_list)
            candidate = repo.fill_reminder_candidates(
                CHAT_ID,
                date(2026, 9, 1),
                date(2026, 9, 1),
            )[0]
            self.assertIsNone(candidate.reminder_message_id)
            self.assertTrue(candidate.phone_filled)

    async def test_ambiguous_send_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            bot = self._bot(CARD)
            bot.send_message.side_effect = NetworkError("lost response")
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )
            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(bot.send_message.await_count, 1)
            attempt = repo.open_fill_reminder_attempts(CHAT_ID, 10)[0]
            self.assertEqual(attempt.state, "ambiguous")
            self.assertIsNone(attempt.telegram_message_id)

    async def test_observer_recovers_lost_send_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo, sale_date=date(2026, 9, 4))
            bot = self._bot(CARD)
            bot.send_message.side_effect = NetworkError("lost response")
            service = SalesPhotoService(settings(root), repo)
            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 4),
            )
            attempt = repo.open_fill_reminder_attempts(CHAT_ID, 10)[0]
            payload = build_signed_fill_reminder(
                inspect_fill_fields(CARD, None),
                repo.fill_reminder_token_url(attempt.attempt_id),
            )
            observed = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=801,
                text=payload.text,
                entities=payload.entities,
                caption=None,
                reply_to_message=SimpleNamespace(message_id=200),
            )

            await service.on_text(
                SimpleNamespace(effective_message=observed),
                SimpleNamespace(bot=bot),
            )

            recovered = repo.fill_reminder_attempt(attempt.attempt_id)
            self.assertEqual(recovered.state, "confirmed")
            self.assertEqual(recovered.telegram_message_id, 801)
            self.assertEqual(bot.send_message.await_count, 1)

    async def test_observation_can_arrive_before_send_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo, sale_date=date(2026, 9, 4))
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)
            send_started = asyncio.Event()
            release_send = asyncio.Event()

            async def delayed_send(**kwargs):
                send_started.set()
                await release_send.wait()
                return SimpleNamespace(message_id=801)

            bot.send_message.side_effect = delayed_send
            publisher = asyncio.create_task(
                service.run_fill_reminder_check(
                    bot,
                    publish=True,
                    reference_date=date(2026, 9, 4),
                )
            )
            await send_started.wait()
            attempt = repo.open_fill_reminder_attempts(CHAT_ID, 10)[0]
            payload = build_signed_fill_reminder(
                inspect_fill_fields(CARD, None),
                repo.fill_reminder_token_url(attempt.attempt_id),
            )
            observed = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=801,
                text=payload.text,
                entities=payload.entities,
                caption=None,
                reply_to_message=SimpleNamespace(message_id=200),
            )
            observer = asyncio.create_task(
                service.on_text(
                    SimpleNamespace(effective_message=observed),
                    SimpleNamespace(bot=bot),
                )
            )
            await asyncio.sleep(0)

            self.assertEqual(
                repo.fill_reminder_attempt(attempt.attempt_id).state,
                "confirmed",
            )
            release_send.set()
            await asyncio.gather(publisher, observer)

            recovered = repo.fill_reminder_attempt(attempt.attempt_id)
            self.assertEqual(recovered.state, "confirmed")
            self.assertEqual(recovered.telegram_message_id, 801)

    async def test_observation_wins_when_send_response_is_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo, sale_date=date(2026, 9, 4))
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)
            send_started = asyncio.Event()
            lose_response = asyncio.Event()

            async def lost_response(**_kwargs):
                send_started.set()
                await lose_response.wait()
                raise NetworkError("accepted but response was lost")

            bot.send_message.side_effect = lost_response
            publisher = asyncio.create_task(
                service.run_fill_reminder_check(
                    bot,
                    publish=True,
                    reference_date=date(2026, 9, 4),
                )
            )
            await send_started.wait()
            attempt = repo.open_fill_reminder_attempts(CHAT_ID, 10)[0]
            payload = build_signed_fill_reminder(
                inspect_fill_fields(CARD, None),
                repo.fill_reminder_token_url(attempt.attempt_id),
            )
            observed = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=801,
                text=payload.text,
                entities=payload.entities,
                caption=None,
                reply_to_message=SimpleNamespace(message_id=200),
            )
            observer = asyncio.create_task(
                service.on_text(
                    SimpleNamespace(effective_message=observed),
                    SimpleNamespace(bot=bot),
                )
            )
            await asyncio.sleep(0)
            lose_response.set()
            await asyncio.gather(publisher, observer)

            recovered = repo.fill_reminder_attempt(attempt.attempt_id)
            self.assertEqual(recovered.state, "confirmed")
            self.assertEqual(recovered.telegram_message_id, 801)
            self.assertEqual(bot.send_message.await_count, 1)

    async def test_observer_requires_the_signed_reply_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo, sale_date=date(2026, 9, 4))
            attempt = repo.create_fill_reminder_attempt(CHAT_ID, 10, 200)
            payload = build_signed_fill_reminder(
                inspect_fill_fields(CARD, None),
                repo.fill_reminder_token_url(attempt.attempt_id),
            )
            observed = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=801,
                text=payload.text,
                entities=payload.entities,
                caption=None,
                reply_to_message=SimpleNamespace(message_id=201),
            )
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)

            await service.on_text(
                SimpleNamespace(effective_message=observed),
                SimpleNamespace(bot=bot),
            )

            untouched = repo.fill_reminder_attempt(attempt.attempt_id)
            self.assertEqual(untouched.state, "pending")
            self.assertIsNone(untouched.telegram_message_id)

    async def test_edited_card_immediately_removes_obsolete_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo, sale_date=date(2026, 9, 4))
            repo.record_fill_reminder(CHAT_ID, 10, 777)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            complete = CARD.replace("📞:", "📞: -")
            message = SimpleNamespace(
                chat_id=CHAT_ID,
                message_id=200,
                caption=complete,
                caption_entities=(),
                text=None,
                entities=(),
                reply_markup=None,
            )
            bot = self._bot(complete)
            service = SalesPhotoService(settings(root), repo)

            await service.on_edited_photo(
                SimpleNamespace(edited_channel_post=message, update_id=50),
                SimpleNamespace(bot=bot),
            )

            self.assertIn(call(CHAT_ID, 777), bot.delete_message.await_args_list)
            self.assertEqual(repo.open_fill_reminder_attempts(CHAT_ID, 10), ())

    async def test_duplicate_confirmed_reminders_are_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            first = repo.create_fill_reminder_attempt(CHAT_ID, 10, 200)
            second = repo.create_fill_reminder_attempt(CHAT_ID, 10, 200)
            repo.confirm_fill_reminder_attempt(first.attempt_id, 801)
            repo.confirm_fill_reminder_attempt(second.attempt_id, 802)
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )

            self.assertIn(call(CHAT_ID, 801), bot.delete_message.await_args_list)
            open_attempts = repo.open_fill_reminder_attempts(CHAT_ID, 10)
            self.assertEqual(len(open_attempts), 1)
            self.assertEqual(open_attempts[0].telegram_message_id, 802)

    async def test_delete_failure_is_durable_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            repo.record_fill_reminder(CHAT_ID, 10, 777)
            complete = CARD.replace("📞:", "📞: -")
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            bot = self._bot(complete)
            bot.delete_message.side_effect = [True, NetworkError("temporary"), True, True]
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )
            attempt = repo.open_fill_reminder_attempts(CHAT_ID, 10)[0]
            self.assertEqual(attempt.state, "delete_pending")

            bot.delete_message.side_effect = None
            bot.delete_message.return_value = True
            with repo._connect() as db:
                db.execute(
                    """UPDATE sales_photo_reminder_attempts
                       SET updated_at=? WHERE attempt_id=?""",
                    (
                        "2026-09-01T00:00:00+00:00",
                        attempt.attempt_id,
                    ),
                )
                db.commit()
            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(repo.open_fill_reminder_attempts(CHAT_ID, 10), ())

    async def test_message_not_found_completes_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            repo.record_fill_reminder(CHAT_ID, 10, 777)
            complete = CARD.replace("📞:", "📞: -")
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            bot = self._bot(complete)

            async def delete_message(_chat_id, message_id):
                if message_id == 777:
                    raise BadRequest("Message to delete not found")
                return True

            bot.delete_message.side_effect = delete_message
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=False,
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(repo.open_fill_reminder_attempts(CHAT_ID, 10), ())

    async def test_failed_delete_is_reused_if_card_becomes_incomplete_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            repo.record_fill_reminder(CHAT_ID, 10, 777)
            attempt = repo.open_fill_reminder_attempts(CHAT_ID, 10)[0]
            repo.mark_fill_reminder_attempt_state(
                attempt.attempt_id,
                "delete_pending",
                "temporary",
            )
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)

            await service.run_fill_reminder_check(
                bot,
                publish=True,
                reference_date=date(2026, 9, 1),
            )

            self.assertEqual(bot.send_message.await_count, 0)
            recovered = repo.open_fill_reminder_attempts(CHAT_ID, 10)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].state, "confirmed")
            self.assertEqual(recovered[0].telegram_message_id, 777)

    async def test_concurrent_publishers_create_only_one_reminder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            self._sale(repo)
            bot = self._bot(CARD)
            service = SalesPhotoService(settings(root), repo)

            await asyncio.gather(
                service.run_fill_reminder_check(
                    bot,
                    publish=True,
                    reference_date=date(2026, 9, 1),
                ),
                service.run_fill_reminder_check(
                    bot,
                    publish=True,
                    reference_date=date(2026, 9, 1),
                ),
            )

            self.assertEqual(bot.send_message.await_count, 1)


class FillReminderMigrationTests(unittest.TestCase):
    def test_legacy_pointer_is_migrated_to_confirmed_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "sales.db")
            FillReminderServiceTests._sale(repo)
            with repo._connect() as db:
                db.execute(
                    """INSERT INTO sales_photo_fill_reminders(
                           chat_id,source_message_id,reminder_message_id,
                           created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (
                        CHAT_ID,
                        10,
                        777,
                        "2026-09-01T10:00:00+00:00",
                        "2026-09-01T10:00:00+00:00",
                    ),
                )
                db.commit()

            migrated = SalesPhotoRepository(root / "sales.db")

            attempts = migrated.open_fill_reminder_attempts(CHAT_ID, 10)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].telegram_message_id, 777)
            self.assertEqual(attempts[0].state, "confirmed")
            with migrated._connect() as db:
                legacy_count = db.execute(
                    "SELECT COUNT(*) FROM sales_photo_fill_reminders"
                ).fetchone()[0]
            self.assertEqual(legacy_count, 0)


if __name__ == "__main__":
    unittest.main()
