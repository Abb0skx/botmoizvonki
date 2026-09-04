import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, filters

from app.database import OrderRepository
from app.handlers.orders import (
    _end_creation_with_order_list,
    cancel_conversation,
    comment,
    new_order,
    register_handlers,
    save_edit,
    show_all_locations,
    start,
)


def complete_draft(token: str = "stable-creation-token") -> dict:
    return {
        "creation_token": token,
        "seller_name": "Ali",
        "payment_status": "collect_on_delivery",
        "client_phone": "+998901333999",
        "product": "A56",
        "amount_usd": 375,
        "latitude": 41.31,
        "longitude": 69.24,
        "delivery_time": None,
    }


class RepositoryCreationIdempotencyTests(unittest.TestCase):
    def test_same_creation_token_reuses_order_number_and_event(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()

            first = repo.create(manager_id=1, manager_name="Manager", data=complete_draft())
            second = repo.create(manager_id=1, manager_name="Manager", data=complete_draft())

            self.assertEqual(second.id, first.id)
            self.assertEqual(second.order_number, first.order_number)
            self.assertEqual(repo.count_all(), 1)
            self.assertEqual(len(repo.list_events(first.id)), 1)

            next_order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data=complete_draft("another-token"),
            )
            self.assertEqual(next_order.order_number, first.order_number + 1)


class FinalCreationStepSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = OrderRepository(Path(self.directory.name) / "delivery.db")
        self.repo.initialize()
        self.user = SimpleNamespace(id=101, full_name="Manager", username="manager")

    def make_context(self):
        return SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "repo": self.repo,
                "settings": SimpleNamespace(manager_ids=frozenset({self.user.id})),
            }),
            user_data={"draft": complete_draft()},
        )

    def private_update(self, message):
        return SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=self.user,
        )

    async def test_retry_after_card_send_failure_does_not_duplicate_order(self):
        card_message = SimpleNamespace(chat_id=101, message_id=501)
        message = SimpleNamespace(
            text="Пропустить",
            reply_text=AsyncMock(side_effect=[RuntimeError("Telegram timeout"), card_message, None]),
        )
        update = self.private_update(message)
        context = self.make_context()

        with self.assertRaisesRegex(RuntimeError, "Telegram timeout"):
            await comment(update, context)

        self.assertEqual(self.repo.count_all(), 1)
        self.assertIn("draft", context.user_data)
        self.assertEqual(self.repo.list_needing_sync()[0].status, "draft")

        state = await comment(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.repo.count_all(), 1)
        self.assertNotIn("draft", context.user_data)
        order = self.repo.list_all()[0]
        self.assertEqual(order.manager_message_id, 501)
        # Telegram publication references are technical metadata, not a
        # manager-visible business edit in the append-only order history.
        self.assertEqual(len(self.repo.list_events(order.id)), 1)

    async def test_retry_after_confirmation_failure_does_not_resend_card(self):
        card_message = SimpleNamespace(chat_id=101, message_id=502)
        message = SimpleNamespace(
            text="Пропустить",
            reply_text=AsyncMock(side_effect=[card_message, RuntimeError("confirmation timeout"), None]),
        )
        update = self.private_update(message)
        context = self.make_context()

        with self.assertRaisesRegex(RuntimeError, "confirmation timeout"):
            await comment(update, context)
        state = await comment(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.repo.count_all(), 1)
        # One card, one failed confirmation, one successful confirmation.
        self.assertEqual(message.reply_text.await_count, 3)
        self.assertIn("Заказ №1", message.reply_text.await_args_list[0].args[0])
        self.assertEqual(message.reply_text.await_args_list[2].args[0], "Проверьте данные заказа.")

    async def test_new_order_photo_is_queued_for_sales_without_backfill(self):
        card_message = SimpleNamespace(chat_id=101, message_id=503)
        message = SimpleNamespace(
            text="Пропустить",
            reply_text=AsyncMock(side_effect=[card_message, None]),
        )
        update = self.private_update(message)
        context = self.make_context()
        context.user_data["draft"].update(
            product_photo_file_id="telegram-photo",
            product_photo_unique_id="unique-photo",
        )

        with (
            patch(
                "app.handlers.orders._persist_product_photo",
                new=AsyncMock(return_value="product_photos/order-1-photo.jpg"),
            ) as persist_photo,
            patch(
                "app.handlers.orders._sync_order",
                new=AsyncMock(return_value=(None, True)),
            ) as sync_order,
        ):
            state = await comment(update, context)

        self.assertEqual(state, ConversationHandler.END)
        order = self.repo.list_all()[0]
        self.assertEqual(order.sales_card_status, "pending")
        self.assertEqual(
            order.product_photo_path,
            "product_photos/order-1-photo.jpg",
        )
        persist_photo.assert_awaited_once()
        sync_order.assert_awaited_once_with(context, order.id)
        self.assertIn(
            "Фото нового товара отправляется",
            message.reply_text.await_args_list[-1].args[0],
        )

    async def test_edited_product_photo_is_queued_for_sales(self):
        order = self.repo.create(
            manager_id=self.user.id,
            manager_name=self.user.full_name,
            data=complete_draft(),
        )
        photo = SimpleNamespace(
            file_id="edited-telegram-photo",
            file_unique_id="edited-unique-photo",
            file_size=500,
            width=1000,
            height=1000,
        )
        message = SimpleNamespace(
            text=None,
            photo=[photo],
            location=None,
            reply_text=AsyncMock(),
        )
        update = self.private_update(message)
        update.effective_chat.id = self.user.id
        context = self.make_context()
        context.user_data = {
            "edit": {
                "order_id": order.id,
                "field": "product_photo",
                "message_id": 504,
                "chat_id": self.user.id,
                "updated_at": order.updated_at,
            }
        }
        context.bot = SimpleNamespace(edit_message_text=AsyncMock())

        with patch(
            "app.handlers.orders._persist_product_photo",
            new=AsyncMock(return_value="product_photos/order-1-edited.jpg"),
        ) as persist_photo:
            state = await save_edit(update, context)

        self.assertEqual(state, ConversationHandler.END)
        updated = self.repo.get(order.id)
        self.assertEqual(updated.sales_card_status, "pending")
        self.assertEqual(
            updated.product_photo_path,
            "product_photos/order-1-edited.jpg",
        )
        persist_photo.assert_awaited_once()
        self.assertIn(
            "отправляется в канал продаж",
            message.reply_text.await_args.args[0],
        )

    async def test_missing_persisted_draft_ends_safely(self):
        message = SimpleNamespace(text="Пропустить", reply_text=AsyncMock())
        update = self.private_update(message)
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "repo": self.repo,
                "settings": SimpleNamespace(manager_ids=frozenset({self.user.id})),
            }),
            user_data={},
        )

        state = await comment(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.repo.count_all(), 0)
        self.assertIn("Черновик заказа не найден", message.reply_text.await_args.args[0])

    async def test_revoked_manager_cannot_finish_persisted_creation_as_courier(self):
        message = SimpleNamespace(text="Пропустить", reply_text=AsyncMock())
        update = self.private_update(message)
        context = self.make_context()
        context.user_data["edit"] = {"order_id": 99}
        context.application.bot_data["settings"] = SimpleNamespace(
            manager_ids=frozenset(),
            courier_ids=frozenset({self.user.id}),
        )

        state = await comment(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.repo.count_all(), 0)
        self.assertNotIn("draft", context.user_data)
        self.assertNotIn("edit", context.user_data)
        self.assertIn("Доступ менеджера отозван", message.reply_text.await_args.args[0])

    async def test_revoked_manager_cannot_finish_persisted_edit_as_courier(self):
        order = self.repo.create(
            manager_id=self.user.id,
            manager_name=self.user.full_name,
            data=complete_draft(),
        )
        message = SimpleNamespace(
            text="Подменённая модель",
            location=None,
            reply_text=AsyncMock(),
        )
        update = self.private_update(message)
        context = self.make_context()
        context.user_data = {
            "edit": {
                "order_id": order.id,
                "field": "product",
                "chat_id": self.user.id,
                "updated_at": order.updated_at,
            }
        }
        context.application.bot_data["settings"] = SimpleNamespace(
            manager_ids=frozenset(),
            courier_ids=frozenset({self.user.id}),
        )

        state = await save_edit(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.repo.get(order.id).product, "A56")
        self.assertNotIn("edit", context.user_data)

    async def test_cancel_after_card_send_failure_cancels_committed_draft(self):
        message = SimpleNamespace(
            text="Пропустить",
            reply_text=AsyncMock(side_effect=RuntimeError("Telegram timeout")),
        )
        update = self.private_update(message)
        context = self.make_context()
        with self.assertRaisesRegex(RuntimeError, "Telegram timeout"):
            await comment(update, context)

        cancel_message = SimpleNamespace(reply_text=AsyncMock())
        cancel_update = self.private_update(cancel_message)
        state = await cancel_conversation(cancel_update, context)

        self.assertEqual(state, ConversationHandler.END)
        order = self.repo.list_all()[0]
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.sync_needed, 0)
        self.assertIn("Заказ №1 отменён", cancel_message.reply_text.await_args.args[0])

    async def test_cancel_keeps_creation_token_until_sqlite_confirms_cancellation(self):
        committed = self.repo.create(
            manager_id=self.user.id,
            manager_name=self.user.full_name,
            data=complete_draft(),
        )
        context = self.make_context()
        cancel_message = SimpleNamespace(reply_text=AsyncMock())
        cancel_update = self.private_update(cancel_message)

        with patch.object(
            self.repo,
            "transition",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            state = await cancel_conversation(cancel_update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertIn("draft", context.user_data)
        self.assertEqual(self.repo.get(committed.id).status, "draft")
        self.assertIn("пока не отменён", cancel_message.reply_text.await_args.args[0])

        state = await cancel_conversation(cancel_update, context)
        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("draft", context.user_data)
        self.assertEqual(self.repo.get(committed.id).status, "cancelled")

    async def test_new_order_reentry_recovers_committed_draft_before_replacing_token(self):
        failed_message = SimpleNamespace(
            text="Пропустить",
            reply_text=AsyncMock(side_effect=RuntimeError("Telegram timeout")),
        )
        failed_update = self.private_update(failed_message)
        context = self.make_context()
        with self.assertRaisesRegex(RuntimeError, "Telegram timeout"):
            await comment(failed_update, context)

        committed = self.repo.list_all()[0]
        old_token = context.user_data["draft"]["creation_token"]
        reentry_message = SimpleNamespace(text="➕ Новый заказ", reply_text=AsyncMock())
        reentry_update = SimpleNamespace(
            message=reentry_message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=self.user,
        )
        context.application.bot_data["settings"] = SimpleNamespace(
            manager_ids=frozenset({self.user.id})
        )

        with patch(
            "app.handlers.orders._sync_order",
            new=AsyncMock(return_value=(committed, True)),
        ) as sync:
            state = await new_order(reentry_update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(self.repo.count_all(), 1)
        self.assertNotIn("draft", context.user_data)
        self.assertEqual(self.repo.get_by_creation_token(old_token).id, committed.id)
        sync.assert_awaited_once_with(context, committed.id)
        self.assertIn("уже сохранён", reentry_message.reply_text.await_args.args[0])

    async def test_order_list_menu_ends_incomplete_creation(self):
        message = SimpleNamespace(text="📋 Активные заказы", reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=101),
            callback_query=None,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "repo": self.repo,
                "settings": SimpleNamespace(manager_ids=frozenset({101})),
            }),
            user_data={"draft": {"creation_token": "not-committed", "product": "A56"}},
        )

        state = await _end_creation_with_order_list(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("draft", context.user_data)
        message.reply_text.assert_awaited_once()


class StartConversationResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_clears_both_persisted_payloads_and_returns_end(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=101),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"settings": SimpleNamespace(manager_ids=frozenset({101}))}
            ),
            user_data={"draft": complete_draft(), "edit": {"order_id": 7}},
        )

        state = await start(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(context.user_data, {})
        message.reply_text.assert_awaited_once()

    def test_start_is_fallback_for_both_persistent_conversations(self):
        class CapturingApplication:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append((group, handler))

        application = CapturingApplication()
        register_handlers(application)
        conversations = {
            handler.name: handler
            for _, handler in application.handlers
            if isinstance(handler, ConversationHandler)
        }

        creation = conversations["delivery_order_creation"]
        edit = conversations["delivery_order_edit"]
        self.assertTrue(
            any(
                isinstance(handler, CommandHandler)
                and "start" in handler.commands
                and handler.callback is start
                for handler in creation.fallbacks
            )
        )
        self.assertTrue(
            any(
                isinstance(handler, CommandHandler) and "start" in handler.commands
                for handler in edit.fallbacks
            )
        )

        group_zero = [handler for group, handler in application.handlers if group == 0]
        creation_index = group_zero.index(creation)
        global_start_index = next(
            index
            for index, handler in enumerate(group_zero)
            if isinstance(handler, CommandHandler) and handler.callback is start
        )
        self.assertLess(creation_index, global_start_index)

    def test_global_manager_commands_are_private_chat_only(self):
        class CapturingApplication:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append((group, handler))

        application = CapturingApplication()
        register_handlers(application)
        callbacks = {start, cancel_conversation, show_all_locations}
        global_commands = [
            handler
            for group, handler in application.handlers
            if group == 0
            and isinstance(handler, CommandHandler)
            and handler.callback in callbacks
        ]

        self.assertEqual(len(global_commands), 3)
        self.assertTrue(
            all(handler.filters is filters.ChatType.PRIVATE for handler in global_commands)
        )

    async def test_inline_manager_action_ends_persisted_edit_conversation(self):
        class CapturingApplication:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler, group=0):
                self.handlers.append((group, handler))

        application = CapturingApplication()
        register_handlers(application)
        edit = next(
            handler
            for _, handler in application.handlers
            if isinstance(handler, ConversationHandler)
            and handler.name == "delivery_order_edit"
        )
        inline_fallback = next(
            handler
            for handler in edit.fallbacks
            if isinstance(handler, CallbackQueryHandler)
            and handler.pattern.match("edit_close:7")
        )
        context = SimpleNamespace(user_data={"edit": {"order_id": 7}})

        state = await inline_fallback.callback(SimpleNamespace(), context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("edit", context.user_data)


if __name__ == "__main__":
    unittest.main()
