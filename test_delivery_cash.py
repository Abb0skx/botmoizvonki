import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import Forbidden

from app.database import OrderRepository
from app.handlers.cash import (
    cash_correction_input,
    cash_review_action,
    courier_cash_input,
    reconcile_cash_entries,
)
from app.utils.parsers import parse_courier_cash


class CourierCashParserTests(unittest.TestCase):
    def test_mixed_unmarked_values_use_requested_threshold(self):
        parsed = parse_courier_cash("40 -25000")
        self.assertEqual((parsed.usd, parsed.uzs), (40, -25_000))
        self.assertFalse(parsed.is_handover)

    def test_minus_never_disappears_when_spacing_varies(self):
        for text in ("40 - 25000", "40$ - 25000 сум", "40-25000"):
            with self.subTest(text=text):
                parsed = parse_courier_cash(text)
                self.assertEqual((parsed.usd, parsed.uzs), (40, -25_000))

    def test_explicit_markers_override_threshold_and_keep_sign(self):
        parsed = parse_courier_cash("10000$ -5000 сум")
        self.assertEqual((parsed.usd, parsed.uzs), (10_000, -5_000))

    def test_handover_keywords_are_case_insensitive(self):
        for text in ("40$ касса", "40$ KASSA", "40$ 500000 сум Cash"):
            with self.subTest(text=text):
                self.assertTrue(parse_courier_cash(text).is_handover)

    def test_threshold_is_strictly_above_9000_for_uzs(self):
        self.assertEqual(parse_courier_cash("9000").usd, 9000)
        self.assertEqual(parse_courier_cash("9001").uzs, 9001)
        self.assertEqual(parse_courier_cash("-9000").usd, -9000)
        self.assertEqual(parse_courier_cash("-9001").uzs, -9001)

    def test_ambiguous_or_unsafe_messages_are_rejected(self):
        for text in (
            "касса",
            "-40$ касса",
            "40 50",
            "A56 40$",
            "Заказ 31 40$",
            "40.50$",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_courier_cash(text)


class CourierCashRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def create(self, *, message_id=10, kind="receipt", usd=40, uzs=-25_000):
        return self.repo.create_cash_entry(
            courier_id=1799690992,
            courier_name="Muzrob Oka",
            entry_type=kind,
            usd=usd,
            uzs=uzs,
            raw_text="40 -25000" if kind == "receipt" else "40$ касса",
            source_chat_id=-5125237049,
            source_message_id=message_id,
        )

    def test_receipt_changes_both_currency_balances(self):
        entry, created = self.create()
        self.assertTrue(created)
        self.assertEqual(entry.status, "recorded")
        self.assertEqual(self.repo.cash_balance(entry.courier_id), (40, -25_000))

    def test_same_telegram_message_is_idempotent(self):
        first, first_created = self.create()
        second, second_created = self.create()
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.repo.cash_balance(first.courier_id), (40, -25_000))

    def test_handover_changes_balance_only_after_confirmation(self):
        self.create(message_id=1, usd=70, uzs=175_000)
        handover, _ = self.create(message_id=2, kind="handover", usd=40, uzs=0)
        self.assertEqual(self.repo.cash_balance(handover.courier_id), (70, 175_000))

        confirmed = self.repo.review_cash_handover(
            handover.id,
            expected_revision=1,
            decision="confirmed",
            reviewer_id=202134293,
            reviewer_name="Abbos",
        )
        duplicate = self.repo.review_cash_handover(
            handover.id,
            expected_revision=1,
            decision="confirmed",
            reviewer_id=202134293,
            reviewer_name="Abbos",
        )

        self.assertEqual(confirmed.status, "confirmed")
        self.assertIsNone(duplicate)
        self.assertEqual(self.repo.cash_balance(handover.courier_id), (30, 175_000))

    def test_rejected_handover_does_not_change_balance(self):
        receipt, _ = self.create(message_id=1, usd=40, uzs=20_000)
        handover, _ = self.create(message_id=2, kind="handover", usd=10, uzs=0)
        rejected = self.repo.review_cash_handover(
            handover.id,
            expected_revision=1,
            decision="rejected",
            reviewer_id=1,
            reviewer_name="Admin",
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(self.repo.cash_balance(receipt.courier_id), (40, 20_000))

    def test_correction_increments_revision_and_keeps_handover_pending(self):
        handover, _ = self.create(message_id=2, kind="handover", usd=40, uzs=0)
        corrected = self.repo.correct_cash_handover(
            handover.id,
            expected_revision=1,
            usd=35,
            uzs=200_000,
            actor_id=1,
            actor_name="Admin",
        )
        self.assertEqual(corrected.status, "pending")
        self.assertEqual(corrected.revision, 2)
        self.assertEqual((corrected.amount_usd, corrected.amount_uzs), (35, 200_000))
        self.assertIsNone(
            self.repo.review_cash_handover(
                handover.id,
                expected_revision=1,
                decision="confirmed",
                reviewer_id=1,
                reviewer_name="Admin",
            )
        )

    def test_two_admins_cannot_confirm_same_handover_twice(self):
        handover, _ = self.create(message_id=2, kind="handover", usd=40, uzs=0)
        barrier = threading.Barrier(2)

        def confirm(reviewer_id):
            repository = OrderRepository(self.repo.path)
            barrier.wait(timeout=5)
            return repository.review_cash_handover(
                handover.id,
                expected_revision=1,
                decision="confirmed",
                reviewer_id=reviewer_id,
                reviewer_name=f"Admin {reviewer_id}",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, (1, 2)))

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.repo.cash_balance(handover.courier_id), (-40, 0))


class CourierCashHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        self.settings = SimpleNamespace(cash_notification_channel_id=-1003927727489)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(
                return_value=SimpleNamespace(chat_id=-1003927727489, message_id=700)
            ),
            delete_message=AsyncMock(),
            edit_message_text=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
        )
        self.application = SimpleNamespace(
            bot_data={"repo": self.repo, "settings": self.settings},
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def context(self, *, user_data=None):
        return SimpleNamespace(
            application=self.application,
            bot=self.bot,
            user_data={} if user_data is None else user_data,
        )

    @staticmethod
    def update(text, *, message_id=50, chat_id=-5125237049, user_id=1799690992):
        message = SimpleNamespace(
            text=text,
            caption=None,
            message_id=message_id,
            reply_to_message=None,
            reply_text=AsyncMock(),
        )
        return SimpleNamespace(
            effective_user=SimpleNamespace(
                id=user_id,
                full_name="Muzrob Oka",
                username="muzrob",
            ),
            effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
            effective_message=message,
        )

    async def test_receipt_is_recorded_and_original_message_stays(self):
        await courier_cash_input(self.update("40 -25000"), self.context())
        self.assertEqual(self.repo.cash_balance(1799690992), (40, -25_000))
        self.bot.delete_message.assert_not_awaited()
        sent = self.bot.send_message.await_args.kwargs
        self.assertIn("Сумма учтена", sent["text"])
        self.assertIn("+40 $", sent["text"])
        self.assertIn("−25 000 сум", sent["text"])

    async def test_handover_is_published_then_original_is_deleted(self):
        await courier_cash_input(self.update("40$ касса"), self.context())
        entry = self.repo.get_cash_entry(1)
        self.assertEqual(entry.status, "pending")
        self.assertEqual(entry.notification_message_id, 700)
        self.assertIsNotNone(entry.source_deleted_at)
        self.bot.delete_message.assert_awaited_once_with(-5125237049, 50)
        keyboard = self.bot.send_message.await_args.kwargs["reply_markup"]
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(labels, ["✅ Получил", "❌ Не получил", "✏️ Другая сумма"])

    async def test_photo_caption_is_parsed_as_cash(self):
        update = self.update(None)
        update.effective_message.caption = "40$ -25 000 сум"
        update.effective_message.photo = [SimpleNamespace(file_id="photo")]

        await courier_cash_input(update, self.context())

        self.assertEqual(self.repo.cash_balance(1799690992), (40, -25_000))

    async def test_failed_handover_publication_keeps_source_and_recovers(self):
        self.bot.send_message.side_effect = RuntimeError("temporary")
        await courier_cash_input(self.update("40$ касса"), self.context())
        pending = self.repo.get_cash_entry(1)
        self.assertIsNone(pending.notification_message_id)
        self.assertIsNone(pending.source_deleted_at)
        self.bot.delete_message.assert_not_awaited()

        self.bot.send_message.side_effect = None
        self.bot.send_message.return_value = SimpleNamespace(
            chat_id=-1003927727489,
            message_id=701,
        )
        self.application.bot = self.bot
        await reconcile_cash_entries(self.application)

        recovered = self.repo.get_cash_entry(1)
        self.assertEqual(recovered.notification_message_id, 701)
        self.assertIsNotNone(recovered.source_deleted_at)
        self.bot.delete_message.assert_awaited_once_with(-5125237049, 50)

    async def test_permanent_delete_denial_is_not_retried_forever(self):
        self.bot.delete_message.side_effect = Forbidden("not enough rights")
        await courier_cash_input(self.update("40$ касса"), self.context())

        entry = self.repo.get_cash_entry(1)
        self.assertEqual(entry.source_delete_attempts, 1)
        self.assertEqual(entry.source_delete_terminal, 1)
        self.assertEqual(self.repo.list_cash_sources_needing_deletion(), [])

    async def test_confirmed_db_change_repairs_channel_after_edit_failure(self):
        await courier_cash_input(self.update("40$ касса"), self.context())
        query = SimpleNamespace(
            data="cash_ok:1:1",
            from_user=SimpleNamespace(id=202134293, full_name="Abbos", username="abbos"),
            message=SimpleNamespace(chat_id=-1003927727489, message_id=700),
            answer=AsyncMock(),
        )
        self.bot.edit_message_text.side_effect = RuntimeError("temporary")
        with self.assertRaises(RuntimeError):
            await cash_review_action(SimpleNamespace(callback_query=query), self.context())
        changed = self.repo.get_cash_entry(1)
        self.assertEqual(changed.status, "confirmed")
        self.assertEqual(changed.notification_sync_needed, 1)

        self.bot.edit_message_text.side_effect = None
        self.application.bot = self.bot
        await reconcile_cash_entries(self.application)

        repaired = self.repo.get_cash_entry(1)
        self.assertEqual(repaired.notification_sync_needed, 0)
        self.assertIn("Касса получена", self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_message_from_wrong_group_is_ignored(self):
        await courier_cash_input(
            self.update("40 -25000", chat_id=-5216093690),
            self.context(),
        )
        self.assertEqual(self.repo.cash_balance(1799690992), (0, 0))
        self.bot.send_message.assert_not_awaited()

    async def test_channel_admin_confirmation_updates_balance_once(self):
        await courier_cash_input(self.update("40$ касса"), self.context())
        query = SimpleNamespace(
            data="cash_ok:1:1",
            from_user=SimpleNamespace(id=202134293, full_name="Abbos", username="abbos"),
            message=SimpleNamespace(chat_id=-1003927727489, message_id=700),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)
        await cash_review_action(update, self.context())
        await cash_review_action(update, self.context())

        self.assertEqual(self.repo.cash_balance(1799690992), (-40, 0))
        current = self.repo.get_cash_entry(1)
        self.assertEqual(current.status, "confirmed")
        self.assertEqual(current.reviewed_by_name, "Abbos")
        self.assertIn("Касса получена", self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_non_admin_cannot_confirm(self):
        await courier_cash_input(self.update("40$ касса"), self.context())
        self.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        query = SimpleNamespace(
            data="cash_ok:1:1",
            from_user=SimpleNamespace(id=99, full_name="User", username=None),
            message=SimpleNamespace(chat_id=-1003927727489, message_id=700),
            answer=AsyncMock(),
        )
        await cash_review_action(SimpleNamespace(callback_query=query), self.context())
        self.assertEqual(self.repo.get_cash_entry(1).status, "pending")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_other_amount_is_corrected_in_private_and_requires_confirmation(self):
        await courier_cash_input(self.update("40$ касса"), self.context())
        user_data = {}
        query = SimpleNamespace(
            data="cash_other:1:1",
            from_user=SimpleNamespace(id=202134293, full_name="Abbos", username="abbos"),
            message=SimpleNamespace(chat_id=-1003927727489, message_id=700),
            answer=AsyncMock(),
        )
        context = self.context(user_data=user_data)
        await cash_review_action(SimpleNamespace(callback_query=query), context)
        self.assertEqual(user_data["cash_correction"], {"entry_id": 1, "revision": 1})

        private_message = SimpleNamespace(text="35$ 200000 сум", reply_text=AsyncMock())
        private_update = SimpleNamespace(
            effective_user=query.from_user,
            effective_chat=SimpleNamespace(id=202134293, type="private"),
            effective_message=private_message,
        )
        with self.assertRaises(Exception) as stopped:
            await cash_correction_input(private_update, context)
        self.assertEqual(stopped.exception.__class__.__name__, "ApplicationHandlerStop")
        corrected = self.repo.get_cash_entry(1)
        self.assertEqual(corrected.status, "pending")
        self.assertEqual(corrected.revision, 2)
        self.assertEqual((corrected.amount_usd, corrected.amount_uzs), (35, 200_000))


if __name__ == "__main__":
    unittest.main()
