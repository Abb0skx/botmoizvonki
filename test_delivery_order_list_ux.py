import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.database import OrderRepository
from app.handlers.orders import (
    _show_orders,
    begin_edit,
    courier_assignment_action,
    courier_action,
    manager_action,
    manager_sync_action,
    save_edit,
    toggle_edit_menu,
)
from app.utils.couriers import courier_group_id, courier_option


def _order_data(index: int = 1) -> dict:
    return {
        "seller_name": "Ali",
        "client_phone": "+998901333999",
        "product": f"Model {index}",
        "amount_usd": 100 + index,
    }


class CourierConfigurationTests(unittest.TestCase):
    def test_muzrob_is_authorized_and_routed_to_his_group(self):
        courier = courier_option(1799690992)

        self.assertIsNotNone(courier)
        self.assertEqual(courier.name, "Muzrob Oka")
        self.assertEqual(courier_group_id(1799690992), -5125237049)


class OrderListUxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        for index in range(1, 13):
            self.repo.create(manager_id=11, manager_name="Manager", data=_order_data(index))
        self.settings = SimpleNamespace(manager_ids=frozenset({11}))

    def tearDown(self):
        self.tempdir.cleanup()

    def _context(self):
        return SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": self.settings, "repo": self.repo}),
            user_data={},
        )

    async def test_initial_list_is_one_read_only_message(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=11),
            effective_message=message,
            message=message,
            callback_query=None,
        )

        await _show_orders(update, self._context(), active_only=False)

        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        self.assertIn("<b>№12</b> · Model 12", text)
        self.assertIn("<b>№3</b> · Model 3", text)
        self.assertNotIn("<b>№2</b> · Model 2", text)
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertTrue(callback_data)
        self.assertTrue(all(value.startswith("orders_page:") for value in callback_data))

    async def test_page_button_answers_first_and_edits_same_message(self):
        events: list[str] = []

        async def answer(*_args, **_kwargs):
            events.append("answer")

        async def edit(*_args, **_kwargs):
            events.append("edit")

        query = SimpleNamespace(
            answer=AsyncMock(side_effect=answer),
            edit_message_text=AsyncMock(side_effect=edit),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=11),
            effective_message=query.message,
            message=None,
            callback_query=query,
        )

        await _show_orders(update, self._context(), active_only=False, page=1)

        self.assertEqual(events, ["answer", "edit"])
        query.message.reply_text.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("<b>№2</b> · Model 2", text)
        self.assertIn("<b>№1</b> · Model 1", text)
        self.assertNotIn("<b>№12</b> · Model 12", text)

    async def test_paid_at_assembly_is_visible_in_compact_list(self):
        newest = self.repo.list_all()[0]
        self.repo.update(newest.id, payment_status="paid_at_assembly")
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=11),
            effective_message=message,
            message=message,
            callback_query=None,
        )

        await _show_orders(update, self._context(), active_only=False)

        self.assertIn("✅ Оплачено", message.reply_text.await_args.args[0])


class CallbackAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    async def test_manager_send_opens_courier_selection_without_publishing(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())

        query = SimpleNamespace(
            data=f"send:{order.id}",
            from_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=SimpleNamespace(chat_id=11, message_id=90),
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11})),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        await manager_action(update, context)

        self.assertEqual(self.repo.get(order.id).status, "draft")
        query.answer.assert_awaited_once_with("Выберите курьера")
        keyboard = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [row[0].text for row in keyboard.inline_keyboard[:3]],
            ["🚚 Olmas", "🚚 Abbos", "🚚 Muzrob Oka"],
        )

    async def test_manager_assigns_draft_to_selected_courier_group(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            manager_chat_id=11,
            manager_message_id=90,
        )
        query = SimpleNamespace(
            data=f"courier_assign:{order.id}:7636344727",
            from_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=SimpleNamespace(chat_id=11, message_id=90),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(
                chat_id=-5111626405,
                message_id=50,
            )),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    courier_ids=frozenset(),
                    delivery_group_id=-100,
                    location_channel_id=-1002,
                ),
                "repo": self.repo,
            }),
        )

        async def synchronized(_context, order_id):
            return self.repo.get(order_id), True

        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await courier_assignment_action(SimpleNamespace(callback_query=query), context)

        assigned = self.repo.get(order.id)
        self.assertEqual(assigned.status, "pending")
        self.assertEqual(assigned.assigned_courier_id, 7636344727)
        self.assertEqual(assigned.assigned_courier_name, "Olmas")
        self.assertIsNone(assigned.courier_id)
        self.assertEqual(assigned.delivery_chat_id, -5111626405)
        self.assertEqual(bot.send_message.await_args.args[0], -5111626405)
        self.assertIn("🚚 Курьер: Olmas", bot.send_message.await_args.args[1])

    async def test_reassignment_publishes_new_card_then_deletes_old_card(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            status="on_way",
            assigned_courier_id=7636344727,
            assigned_courier_name="Olmas",
            courier_id=7636344727,
            courier_name="Olmas",
            time_started="2026-08-23T10:00:00+05:00",
            delivery_chat_id=-5111626405,
            delivery_message_id=50,
            manager_chat_id=11,
            manager_message_id=90,
        )
        query = SimpleNamespace(
            data=f"courier_assign:{order.id}:202134293",
            from_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=SimpleNamespace(chat_id=11, message_id=90),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(
                chat_id=-5216093690,
                message_id=60,
            )),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    courier_ids=frozenset(),
                    delivery_group_id=-100,
                    location_channel_id=-1002,
                ),
                "repo": self.repo,
            }),
        )

        async def synchronized(_context, order_id):
            return self.repo.get(order_id), True

        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await courier_assignment_action(SimpleNamespace(callback_query=query), context)

        assigned = self.repo.get(order.id)
        self.assertEqual(assigned.status, "pending")
        self.assertEqual(assigned.assigned_courier_id, 202134293)
        self.assertEqual(assigned.assigned_courier_name, "Abbos")
        self.assertIsNone(assigned.courier_id)
        self.assertIsNone(assigned.time_started)
        self.assertEqual(assigned.delivery_chat_id, -5216093690)
        bot.delete_message.assert_awaited_once_with(chat_id=-5111626405, message_id=50)

    async def test_failed_reassignment_keeps_old_courier_and_card(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            status="pending",
            assigned_courier_id=7636344727,
            assigned_courier_name="Olmas",
            delivery_chat_id=-5111626405,
            delivery_message_id=50,
            manager_chat_id=11,
            manager_message_id=90,
        )
        query = SimpleNamespace(
            data=f"courier_assign:{order.id}:202134293",
            from_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=SimpleNamespace(chat_id=11, message_id=90),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("telegram unavailable")),
            delete_message=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=bot,
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11})),
                "repo": self.repo,
            }),
        )

        with patch("app.handlers.orders._notify_manager", new=AsyncMock()) as notify:
            await courier_assignment_action(SimpleNamespace(callback_query=query), context)

        unchanged = self.repo.get(order.id)
        self.assertEqual(unchanged.assigned_courier_id, 7636344727)
        self.assertEqual(unchanged.delivery_chat_id, -5111626405)
        self.assertEqual(unchanged.delivery_message_id, 50)
        bot.delete_message.assert_not_awaited()
        notify.assert_awaited_once()

    async def test_manual_sync_answers_before_order_sync(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        events: list[str] = []

        async def answer(*_args, **_kwargs):
            events.append("answer")

        async def sync(context, order_id):
            self.assertEqual(events, ["answer"])
            events.append("sync")
            return self.repo.get(order_id), True

        query = SimpleNamespace(
            data=f"sync:{order.id}",
            from_user=SimpleNamespace(id=11),
            answer=AsyncMock(side_effect=answer),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11})),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        with patch("app.handlers.orders._sync_order", side_effect=sync):
            await manager_sync_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(events, ["answer", "sync"])
        query.answer.assert_awaited_once_with("Синхронизация запущена…")

    async def test_courier_status_answers_before_message_refresh(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        self.repo.update(order.id, status="pending")
        events: list[str] = []

        async def answer(*_args, **_kwargs):
            events.append("answer")

        async def finish(*_args, **_kwargs):
            self.assertEqual(events, ["answer"])
            events.append("refresh")
            return True

        query = SimpleNamespace(
            data=f"complete:{order.id}",
            from_user=SimpleNamespace(id=22, full_name="Courier", username=None),
            message=SimpleNamespace(chat_id=-100),
            answer=AsyncMock(side_effect=answer),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    delivery_group_id=-100,
                    courier_ids=frozenset({22}),
                ),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        with (
            patch("app.handlers.orders._finish_status_change", side_effect=finish),
            patch("app.handlers.orders._notify_manager", new=AsyncMock()),
        ):
            await courier_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(events, ["answer", "refresh"])
        query.answer.assert_awaited_once_with("Заказ доставлен")

    async def test_stale_courier_card_cannot_change_canonical_order(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            status="pending",
            delivery_chat_id=-100,
            delivery_message_id=100,
        )
        query = SimpleNamespace(
            data=f"complete:{order.id}",
            from_user=SimpleNamespace(id=22, full_name="Courier", username=None),
            message=SimpleNamespace(chat_id=-100, message_id=99),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    delivery_group_id=-100,
                    courier_ids=frozenset({22}),
                ),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        await courier_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(order.id).status, "pending")
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_stale_manager_card_cannot_cancel_order(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            manager_chat_id=11,
            manager_message_id=100,
        )
        query = SimpleNamespace(
            data=f"manager_cancel:{order.id}",
            from_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=SimpleNamespace(chat_id=11, message_id=99),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11})),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        await manager_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(order.id).status, "draft")
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_stale_manager_card_cannot_start_or_expand_edit(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            manager_chat_id=11,
            manager_message_id=100,
        )
        settings = SimpleNamespace(manager_ids=frozenset({11}))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": settings, "repo": self.repo}),
            user_data={},
        )

        edit_query = SimpleNamespace(
            data=f"edit:{order.id}:product",
            from_user=SimpleNamespace(id=11),
            message=SimpleNamespace(chat_id=11, message_id=99, reply_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await begin_edit(SimpleNamespace(callback_query=edit_query), context)

        self.assertNotIn("edit", context.user_data)
        edit_query.message.reply_text.assert_not_awaited()
        self.assertTrue(edit_query.answer.await_args.kwargs["show_alert"])

        menu_query = SimpleNamespace(
            data=f"edit_menu:{order.id}",
            from_user=SimpleNamespace(id=11),
            message=SimpleNamespace(chat_id=11, message_id=99),
            edit_message_reply_markup=AsyncMock(),
            answer=AsyncMock(),
        )
        await toggle_edit_menu(SimpleNamespace(callback_query=menu_query), context)

        menu_query.edit_message_reply_markup.assert_not_awaited()
        self.assertTrue(menu_query.answer.await_args.kwargs["show_alert"])

    async def test_failed_draft_card_refresh_remains_queued_for_canonical_sync(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            manager_chat_id=11,
            manager_message_id=100,
        )
        self.repo.mark_synced(order.id, expected_updated_at=order.updated_at)
        order = self.repo.get(order.id)
        message = SimpleNamespace(
            text="Updated model",
            location=None,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=11),
            effective_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=message,
        )
        context = SimpleNamespace(
            user_data={
                "edit": {
                    "order_id": order.id,
                    "field": "product",
                    "message_id": 100,
                    "chat_id": 11,
                    "updated_at": order.updated_at,
                }
            },
            application=SimpleNamespace(bot_data={"repo": self.repo}),
            bot=SimpleNamespace(edit_message_text=AsyncMock(side_effect=RuntimeError("timeout"))),
        )

        with patch("app.handlers.orders._schedule_sync_retry") as schedule:
            await save_edit(update, context)

        updated = self.repo.get(order.id)
        self.assertEqual(updated.product, "Updated model")
        self.assertEqual(updated.sync_needed, 1)
        schedule.assert_called_once_with(context, order.id)
        self.assertIn("карточку менеджера обновить не удалось", message.reply_text.await_args.args[0])

    async def test_courier_cancelled_order_cannot_be_restored_by_manager(self):
        order = self.repo.create(manager_id=11, manager_name="Manager", data=_order_data())
        order = self.repo.update(
            order.id,
            status="cancelled",
            courier_id=22,
            courier_name="Courier",
            delivery_chat_id=None,
            delivery_message_id=None,
            manager_chat_id=11,
            manager_message_id=100,
        )
        query = SimpleNamespace(
            data=f"manager_restore:{order.id}",
            from_user=SimpleNamespace(id=11, full_name="Manager", username=None),
            message=SimpleNamespace(chat_id=11, message_id=100),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11})),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        await manager_action(SimpleNamespace(callback_query=query), context)

        restored = self.repo.get(order.id)
        self.assertEqual(restored.status, "cancelled")
        self.assertEqual(restored.courier_id, 22)
        query.edit_message_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
