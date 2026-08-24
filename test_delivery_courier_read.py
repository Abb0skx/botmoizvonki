import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.bot.keyboards import courier_keyboard, orders_page_keyboard
from app.database import OrderRepository
from app.handlers.orders import (
    _notify_on_way_log,
    courier_action,
    manager_pickup_action,
    open_order_from_list,
)
from app.monitor_service import WAREHOUSE, build_delivery_monitor
from app.routing_service import enrich_monitor_routes
from app.stats_service import TASHKENT, build_delivery_stats
from app.utils.formatters import courier_card, daily_delivery_report, orders_channel_card


ABBOS_ID = 202134293
ABBOS_GROUP_ID = -5216093690


def order_data(product: str, latitude: float = 41.311) -> dict:
    return {
        "seller_name": "Ali",
        "client_phone": "+998901333999",
        "product": product,
        "amount_usd": 100,
        "latitude": latitude,
        "longitude": 69.279,
        "address_text": f"Адрес {product}, Ташкент",
    }


class CourierReadWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def assigned_order(self):
        order = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data=order_data("A57"),
        )
        return self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            manager_chat_id=11,
            manager_message_id=55,
            delivery_chat_id=ABBOS_GROUP_ID,
            delivery_message_id=77,
        )

    async def test_courier_confirms_read_once_and_card_visibly_changes(self):
        order = self.assigned_order()
        query = SimpleNamespace(
            data=f"read:{order.id}",
            from_user=SimpleNamespace(id=ABBOS_ID, full_name="Abbos", username=None),
            message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[
                SimpleNamespace(chat_id=-1004459657817, message_id=88),
                SimpleNamespace(chat_id=-1004459657817, message_id=89),
            ]),
            edit_message_text=AsyncMock(),
            set_message_reaction=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    courier_ids=frozenset({ABBOS_ID}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        await courier_action(SimpleNamespace(callback_query=query), context)

        read = self.repo.get(order.id)
        self.assertEqual(read.status, "pending")
        self.assertIsNotNone(read.courier_read_at)
        self.assertEqual(read.courier_id, ABBOS_ID)
        self.assertEqual(read.courier_name, "Abbos")
        query.answer.assert_awaited_once_with("✅ Прочитано · еду на склад 🏬")
        bot.set_message_reaction.assert_awaited_once_with(
            chat_id=ABBOS_GROUP_ID,
            message_id=77,
            reaction="👀",
            is_big=True,
        )
        changed_text = query.edit_message_text.await_args.args[0]
        read_time = datetime.fromisoformat(read.courier_read_at).astimezone(TASHKENT).strftime("%H:%M")
        self.assertIn(f"Заказ прочитан {read_time}", changed_text)
        self.assertIn("Курьер едет на склад", changed_text)
        callbacks = [
            button.callback_data
            for row in query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn(f"read:{order.id}", callbacks)
        self.assertTrue(any(
            "Заказ №1 прочитан" in call.kwargs["text"]
            and "едет на склад" in call.kwargs["text"]
            and call.kwargs["chat_id"] == -1004459657817
            for call in bot.send_message.await_args_list
        ))
        log_markup = next(
            call.kwargs["reply_markup"]
            for call in bot.send_message.await_args_list
            if "Заказ №1 прочитан" in call.kwargs.get("text", "")
        )
        self.assertTrue(any(
            button.callback_data == f"pickup_log:{order.id}:{ABBOS_ID}"
            for row in log_markup.inline_keyboard
            for button in row
        ))
        self.assertFalse(any(call.args and call.args[0] == 11 for call in bot.send_message.await_args_list))
        events = self.repo.list_events(order.id)
        read_events = [event for event in events if event.event_type == "courier_read"]
        self.assertEqual(len(read_events), 1)
        self.assertEqual(read_events[0].actor_role, "courier")

        query.answer.reset_mock()
        query.edit_message_text.reset_mock()
        await courier_action(SimpleNamespace(callback_query=query), context)
        self.assertIn("Уже прочитано", query.answer.await_args.args[0])
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(
            len([event for event in self.repo.list_events(order.id) if event.event_type == "courier_read"]),
            1,
        )

    def test_keyboard_and_cards_show_read_confirmation(self):
        order = self.assigned_order()
        unread_button = next(
            button
            for row in courier_keyboard(order).inline_keyboard
            for button in row
            if button.callback_data == f"read:{order.id}"
        )
        self.assertEqual(unread_button.text, "👀 Заказ прочитан")

        order = self.repo.update(
            order.id,
            courier_read_at=datetime.now(TASHKENT).isoformat(timespec="seconds"),
            courier_id=ABBOS_ID,
            courier_name="Abbos",
        )
        read_button = next(
            button
            for row in courier_keyboard(order).inline_keyboard
            for button in row
            if button.callback_data == f"read:{order.id}"
        )
        self.assertEqual(read_button.text, "✅ Прочитано · еду на склад")
        self.assertIn("🏬 Едет на склад за товаром", courier_card(order))
        read_time = datetime.fromisoformat(order.courier_read_at).astimezone(TASHKENT).strftime("%H:%M")
        self.assertIn(f"👀 Заказ прочитан {read_time}", orders_channel_card(order))


class ManagerListAndLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        draft = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data=order_data("A57"),
        )
        self.order = self.repo.transition(
            draft.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            courier_read_at=datetime.now(TASHKENT).isoformat(timespec="seconds"),
            manager_chat_id=11,
            manager_message_id=55,
            orders_channel_chat_id=-1004459657817,
            orders_channel_message_id=66,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_active_page_has_edit_button_for_every_order(self):
        second = self.repo.create(
            manager_id=12,
            manager_name="Ali",
            data=order_data("A58"),
        )
        keyboard = orders_page_keyboard(
            "active",
            page=0,
            total_pages=1,
            orders=[self.order, second],
        )
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn(f"list_order:{self.order.id}", callbacks)
        self.assertIn(f"list_order:{second.id}", callbacks)

    async def test_manager_opens_order_from_active_list_as_editable_card(self):
        sent = SimpleNamespace(chat_id=11, message_id=99)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=sent),
            edit_message_reply_markup=AsyncMock(),
        )
        query = SimpleNamespace(
            data=f"list_order:{self.order.id}",
            from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
            message=SimpleNamespace(chat_id=11, message_id=88),
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11})),
                "repo": self.repo,
            }),
            bot=bot,
        )

        async def synchronized(_context, order_id):
            return self.repo.get(order_id), True

        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await open_order_from_list(SimpleNamespace(callback_query=query), context)

        opened = self.repo.get(self.order.id)
        self.assertEqual((opened.manager_chat_id, opened.manager_message_id), (11, 99))
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn(f"edit:{self.order.id}:product", callbacks)
        bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=11,
            message_id=55,
            reply_markup=None,
        )

    async def test_log_channel_member_can_mark_goods_picked_up(self):
        query = SimpleNamespace(
            data=f"pickup:{self.order.id}",
            from_user=SimpleNamespace(id=777, full_name="Сотрудник склада", username=None),
            message=SimpleNamespace(chat_id=-1004459657817, message_id=66),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            send_message=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset(),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        async def synchronized(_context, order_id):
            return self.repo.get(order_id), True

        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        picked = self.repo.get(self.order.id)
        self.assertEqual(picked.status, "picked_up")
        self.assertEqual(picked.courier_id, ABBOS_ID)
        bot.get_chat_member.assert_awaited_once_with(-1004459657817, 777)
        self.assertTrue(any(
            call.kwargs.get("chat_id") == -1004459657817
            and "товар забран" in call.kwargs.get("text", "")
            for call in bot.send_message.await_args_list
        ))

    async def test_assigned_courier_cannot_mark_own_goods_as_picked_up(self):
        query = SimpleNamespace(
            data=f"pickup:{self.order.id}",
            from_user=SimpleNamespace(id=ABBOS_ID, full_name="Abbos", username=None),
            message=SimpleNamespace(chat_id=-1004459657817, message_id=66),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(get_chat_member=AsyncMock(), send_message=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset(),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        query.answer.assert_awaited_once_with(
            "Эту отметку ставит сотрудник склада, не курьер.",
            show_alert=True,
        )
        bot.get_chat_member.assert_not_awaited()

    async def test_log_pickup_button_roundtrip_restores_pending_action(self):
        query = SimpleNamespace(
            data=f"pickup_log:{self.order.id}:{ABBOS_ID}",
            from_user=SimpleNamespace(
                id=777,
                full_name="Сотрудник склада",
                username=None,
            ),
            message=SimpleNamespace(chat_id=-1004459657817, message_id=91),
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            send_message=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset(),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        async def synchronized(_context, order_id):
            return self.repo.get(order_id), True

        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)
            picked = self.repo.get(self.order.id)
            self.assertEqual(picked.status, "picked_up")
            self.assertIsNotNone(picked.picked_up_at)
            pickup_markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
            self.assertTrue(any(
                button.callback_data == f"undo_pickup_log:{self.order.id}:{ABBOS_ID}"
                for row in pickup_markup.inline_keyboard
                for button in row
            ))

            query.data = f"undo_pickup_log:{self.order.id}:{ABBOS_ID}"
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        restored = self.repo.get(self.order.id)
        self.assertEqual(restored.status, "pending")
        self.assertIsNone(restored.picked_up_at)
        self.assertIsNone(restored.courier_id)
        restored_markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        self.assertTrue(any(
            button.callback_data == f"pickup_log:{self.order.id}:{ABBOS_ID}"
            for row in restored_markup.inline_keyboard
            for button in row
        ))

    async def test_assigned_courier_is_denied_on_log_pickup_button(self):
        query = SimpleNamespace(
            data=f"pickup_log:{self.order.id}:{ABBOS_ID}",
            from_user=SimpleNamespace(id=ABBOS_ID, full_name="Abbos", username=None),
            message=SimpleNamespace(chat_id=-1004459657817, message_id=91),
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        bot = SimpleNamespace(get_chat_member=AsyncMock(), send_message=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset(),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        query.answer.assert_awaited_once_with(
            "Эту отметку ставит сотрудник склада, не курьер.",
            show_alert=True,
        )
        query.edit_message_reply_markup.assert_not_awaited()
        bot.get_chat_member.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_on_way_log_contains_eta_customer_and_location_button(self):
        started = datetime.now(TASHKENT).replace(second=0, microsecond=0)
        order = self.repo.transition(
            self.order.id,
            {"pending"},
            status="on_way",
            picked_up_at=started.isoformat(),
            time_started=started.isoformat(),
            location_chat_id=-1004398605075,
            location_message_id=501,
        )
        routing = SimpleNamespace(
            route=AsyncMock(return_value={"duration_s": 30 * 60}),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(orders_channel_id=-1004459657817),
                "repo": self.repo,
                "routing_service": routing,
            }),
            bot=bot,
        )

        await _notify_on_way_log(context, order)

        expected_arrival = (started + timedelta(minutes=30)).strftime("%H:%M")
        sent = bot.send_message.await_args.kwargs
        self.assertIn("🚗 <b>Abbos</b> едет к заказу №1", sent["text"])
        self.assertIn("📦 Модель: <b>A57</b>", sent["text"])
        self.assertIn(f"Примерно к <b>{expected_arrival}</b> доставит", sent["text"])
        self.assertIn("📱 +998 90 133 39 99", sent["text"])
        self.assertIn("📍 Адрес A57", sent["text"])
        buttons = {
            button.text: button.url
            for row in sent["reply_markup"].inline_keyboard
            for button in row
        }
        self.assertEqual(
            buttons["📋 Открыть заказ"],
            "https://t.me/c/4459657817/66",
        )
        self.assertTrue(buttons["📍 Локация"].startswith("https://yandex.uz/maps/?"))
        self.assertNotIn("t.me/c/4398605075", buttons["📍 Локация"])
        routing.route.assert_awaited_once()


class CourierWarehouseMovementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def assigned(self, product: str, latitude: float):
        order = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data=order_data(product, latitude),
        )
        return self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )

    def test_read_after_delivery_starts_estimated_trip_to_warehouse(self):
        now = datetime.now(TASHKENT).replace(microsecond=0)
        completed = self.assigned("A56", 41.320)
        completed = self.repo.transition(
            completed.id,
            {"pending"},
            status="completed",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            picked_up_at=(now - timedelta(minutes=70)).isoformat(),
            time_started=(now - timedelta(minutes=60)).isoformat(),
            delivered_at=(now - timedelta(minutes=20)).isoformat(),
        )
        waiting = self.assigned("A57", 41.350)
        read_at = now - timedelta(minutes=10)
        self.repo.transition(
            waiting.id,
            {"pending"},
            status="pending",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            courier_read_at=read_at.isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
            event_type="courier_read",
        )

        monitor = build_delivery_monitor(self.repo)
        route = next(route for route in monitor["routes"] if route["courier_id"] == ABBOS_ID)
        courier = next(item for item in monitor["couriers"] if item["id"] == ABBOS_ID)
        self.assertEqual(route["movement_kind"], "warehouse")
        self.assertEqual(route["return_path"][0], [completed.latitude, completed.longitude])
        self.assertEqual(route["return_path"][-1], [WAREHOUSE["latitude"], WAREHOUSE["longitude"]])
        self.assertEqual(route["movement_started_at"], read_at.isoformat())
        self.assertTrue(courier["heading_to_warehouse"])
        self.assertEqual(monitor["summary"]["heading_to_warehouse"], 1)

        report = build_delivery_stats(self.repo, now.date())
        read_row = next(row for row in report["orders"] if row["id"] == waiting.id)
        self.assertEqual(read_row["read_time"], read_at.strftime("%H:%M"))
        self.assertTrue(any(item["kind"] == "read" for item in report["timeline"]))
        telegram_log = "\n".join(
            daily_delivery_report(
                self.repo.list_all(),
                now.date(),
                self.repo.list_events_between(
                    now.replace(hour=0, minute=0, second=0),
                    now.replace(hour=0, minute=0, second=0) + timedelta(days=1),
                ),
            )
        )
        self.assertIn("прочитал заказ", telegram_log)
        self.assertIn("выехал на склад", telegram_log)

    async def test_warehouse_movement_gets_road_eta_and_return_layer(self):
        started = (datetime.now(TASHKENT) - timedelta(minutes=5)).isoformat()
        points = [[41.320, 69.279], [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]]
        routing = Mock()
        routing.route = AsyncMock(return_value={
            "provider": "osrm",
            "geometry": points,
            "distance_m": 4200,
            "duration_s": 600,
        })
        state = {
            "summary": {},
            "couriers": [{"id": ABBOS_ID}],
            "routes": [{
                "courier_id": ABBOS_ID,
                "dark_paths": [],
                "current_path": [],
                "return_path": points,
                "movement_kind": "warehouse",
                "movement_started_at": started,
            }],
        }

        result = await enrich_monitor_routes(state, routing)
        route = result["routes"][0]
        self.assertEqual(route["movement"]["kind"], "warehouse")
        self.assertEqual(route["return_road_path"], points)
        self.assertEqual(route["movement"]["distance_km"], 4.2)
        self.assertIsNotNone(route["movement"]["eta_at"])
