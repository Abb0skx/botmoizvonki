import asyncio
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

    async def test_legacy_read_button_is_retired_without_recording_read(self):
        order = self.assigned_order()
        query = SimpleNamespace(
            data=f"read:{order.id}",
            from_user=SimpleNamespace(id=ABBOS_ID, full_name="Abbos", username=None),
            message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        bot = SimpleNamespace()
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

        finish = AsyncMock(return_value=True)
        with patch("app.handlers.orders._finish_status_change", new=finish):
            await courier_action(SimpleNamespace(callback_query=query), context)

        read = self.repo.get(order.id)
        self.assertEqual(read.status, "pending")
        self.assertIsNone(read.courier_read_at)
        self.assertIsNone(read.courier_id)
        self.assertIsNone(read.courier_name)
        query.answer.assert_awaited_once_with("Кнопка прочтения больше не используется")
        finish.assert_awaited_once()
        callbacks = [
            button.callback_data
            for row in finish.await_args.args[4].inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertNotIn(f"read:{order.id}", callbacks)
        self.assertIn(f"cancel:{order.id}", callbacks)
        events = self.repo.list_events(order.id)
        read_events = [event for event in events if event.event_type == "courier_read"]
        self.assertEqual(read_events, [])

    def test_keyboard_has_no_read_action_but_historical_log_keeps_time(self):
        order = self.assigned_order()
        callbacks = [
            button.callback_data
            for row in courier_keyboard(order).inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(
            callbacks,
            [f"group_pickup:{order.id}", f"onway:{order.id}", f"cancel:{order.id}"],
        )

        order = self.repo.update(
            order.id,
            courier_read_at=datetime.now(TASHKENT).isoformat(timespec="seconds"),
            courier_id=ABBOS_ID,
            courier_name="Abbos",
        )
        callbacks = [
            button.callback_data
            for row in courier_keyboard(order).inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(
            callbacks,
            [f"group_pickup:{order.id}", f"onway:{order.id}", f"cancel:{order.id}"],
        )
        self.assertNotIn("Заказ прочитан", courier_card(order))
        self.assertNotIn("Едет на склад", courier_card(order))
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
            delivery_chat_id=ABBOS_GROUP_ID,
            delivery_message_id=77,
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

    async def test_manager_can_mark_goods_picked_up_from_courier_group(self):
        query = SimpleNamespace(
            data=f"group_pickup:{self.order.id}",
            from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
            message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        with patch(
            "app.handlers.orders._finish_status_change_locked",
            new=AsyncMock(return_value=True),
        ):
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        picked = self.repo.get(self.order.id)
        self.assertEqual(picked.status, "picked_up")
        self.assertEqual(picked.courier_id, ABBOS_ID)
        pickup_event = self.repo.list_events(self.order.id)[-1]
        self.assertEqual(pickup_event.actor_id, 11)
        self.assertEqual(pickup_event.actor_role, "manager")
        self.assertEqual(pickup_event.courier_id, ABBOS_ID)
        self.assertTrue(any(
            call.kwargs.get("chat_id") == -1004459657817
            and "товар забран" in call.kwargs.get("text", "")
            for call in bot.send_message.await_args_list
        ))

    async def test_assigned_courier_cannot_mark_own_goods_as_picked_up(self):
        query = SimpleNamespace(
            data=f"group_pickup:{self.order.id}",
            from_user=SimpleNamespace(id=ABBOS_ID, full_name="Abbos", username=None),
            message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({ABBOS_ID}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=bot,
        )

        await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        query.answer.assert_awaited_once_with(
            "Назначенный курьер не может ставить эту отметку.",
            show_alert=True,
        )

    async def test_non_manager_in_courier_group_cannot_mark_pickup(self):
        query = SimpleNamespace(
            data=f"group_pickup:{self.order.id}",
            from_user=SimpleNamespace(id=777, full_name="Участник", username=None),
            message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

        await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        query.answer.assert_awaited_once_with(
            "Только менеджеры могут отметить получение товара.",
            show_alert=True,
        )

    async def test_group_pickup_button_roundtrip_restores_pending_action(self):
        query = SimpleNamespace(
            data=f"group_pickup:{self.order.id}",
            from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
            message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

        with patch(
            "app.handlers.orders._finish_status_change_locked",
            new=AsyncMock(return_value=True),
        ):
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)
            picked = self.repo.get(self.order.id)
            self.assertEqual(picked.status, "picked_up")
            self.assertIsNotNone(picked.picked_up_at)
            picked_callbacks = [
                button.callback_data
                for row in courier_keyboard(picked).inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertIn(f"group_undo_pickup:{self.order.id}", picked_callbacks)

            query.data = f"group_undo_pickup:{self.order.id}"
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        restored = self.repo.get(self.order.id)
        self.assertEqual(restored.status, "pending")
        self.assertIsNone(restored.picked_up_at)
        self.assertIsNone(restored.courier_id)
        restored_callbacks = [
            button.callback_data
            for row in courier_keyboard(restored).inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn(f"group_pickup:{self.order.id}", restored_callbacks)

    async def test_old_log_pickup_button_is_retired_without_changing_status(self):
        query = SimpleNamespace(
            data=f"pickup_log:{self.order.id}:{ABBOS_ID}",
            from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
            message=SimpleNamespace(chat_id=-1004459657817, message_id=91),
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

        await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        query.answer.assert_awaited_once_with(
            "Кнопка перенесена в группу назначенного курьера.",
            show_alert=True,
        )
        query.edit_message_reply_markup.assert_awaited_once()
        callbacks = [
            button.callback_data
            for row in query.edit_message_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertFalse(any("pickup" in callback for callback in callbacks))

    async def test_pickup_from_wrong_group_or_stale_message_is_denied(self):
        for chat_id, message_id in (
            (-1004459657817, 66),
            (ABBOS_GROUP_ID, 999),
        ):
            with self.subTest(chat_id=chat_id, message_id=message_id):
                query = SimpleNamespace(
                    data=f"group_pickup:{self.order.id}",
                    from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
                    message=SimpleNamespace(chat_id=chat_id, message_id=message_id),
                    answer=AsyncMock(),
                )
                context = SimpleNamespace(
                    application=SimpleNamespace(bot_data={
                        "settings": SimpleNamespace(
                            manager_ids=frozenset({11}),
                            orders_channel_id=-1004459657817,
                        ),
                        "repo": self.repo,
                    }),
                    bot=SimpleNamespace(send_message=AsyncMock()),
                )

                await manager_pickup_action(SimpleNamespace(callback_query=query), context)

                self.assertEqual(self.repo.get(self.order.id).status, "pending")
                self.assertTrue(query.answer.await_args.kwargs["show_alert"])
                self.assertIn("актуальную карточку", query.answer.await_args.args[0])

    async def test_concurrent_group_pickup_creates_only_one_transition(self):
        def make_query():
            return SimpleNamespace(
                data=f"group_pickup:{self.order.id}",
                from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
                message=SimpleNamespace(chat_id=ABBOS_GROUP_ID, message_id=77),
                answer=AsyncMock(),
            )

        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(
                    manager_ids=frozenset({11}),
                    orders_channel_id=-1004459657817,
                ),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        first, second = make_query(), make_query()

        with patch(
            "app.handlers.orders._finish_status_change_locked",
            new=AsyncMock(return_value=True),
        ):
            await asyncio.gather(
                manager_pickup_action(SimpleNamespace(callback_query=first), context),
                manager_pickup_action(SimpleNamespace(callback_query=second), context),
            )

        self.assertEqual(self.repo.get(self.order.id).status, "picked_up")
        pickup_events = [
            event
            for event in self.repo.list_events(self.order.id)
            if event.from_status == "pending" and event.to_status == "picked_up"
        ]
        self.assertEqual(len(pickup_events), 1)
        answers = [first.answer.await_args.args[0], second.answer.await_args.args[0]]
        self.assertEqual(sum("Товар у курьера" in value for value in answers), 1)

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

        expected_arrival = (started + timedelta(minutes=43)).strftime("%H:%M")
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

    def test_historical_read_does_not_change_estimated_return_to_warehouse(self):
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
        self.assertEqual(route["movement_kind"], "return")
        self.assertEqual(route["return_path"][0], [completed.latitude, completed.longitude])
        self.assertEqual(route["return_path"][-1], [WAREHOUSE["latitude"], WAREHOUSE["longitude"]])
        self.assertEqual(route["movement_started_at"], completed.delivered_at)
        self.assertNotIn("heading_to_warehouse", courier)
        self.assertNotIn("heading_to_warehouse", monitor["summary"])
        self.assertEqual(courier["waiting_pickup"], 1)

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

    async def test_return_movement_gets_road_eta_and_return_layer(self):
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
                "movement_kind": "return",
                "movement_started_at": started,
            }],
        }

        result = await enrich_monitor_routes(state, routing)
        route = result["routes"][0]
        self.assertEqual(route["movement"]["kind"], "return")
        self.assertEqual(route["return_road_path"], points)
        self.assertEqual(route["movement"]["distance_km"], 4.2)
        self.assertIsNotNone(route["movement"]["eta_at"])
