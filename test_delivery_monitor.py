import base64
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.bot.keyboards import manager_sent_keyboard, orders_channel_keyboard
from app.database import OrderRepository
from app.handlers.orders import courier_action, manager_pickup_action
from app.monitor_service import WAREHOUSE, build_delivery_monitor
from app.stats_service import TASHKENT, build_delivery_stats


ABBOS_ID = 202134293


def order_data(product: str) -> dict:
    return {
        "seller_name": "Ali",
        "client_phone": "+998901333999",
        "product": product,
        "amount_usd": 100,
        "latitude": 41.311,
        "longitude": 69.279,
        "address_text": f"Адрес {product}, Ташкент",
    }


class PickupWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def assigned_order(self, product: str = "A57"):
        order = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data=order_data(product),
        )
        return self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            manager_chat_id=11,
            manager_message_id=90 + order.id,
            delivery_chat_id=-5216093690,
            delivery_message_id=190 + order.id,
        )

    async def test_manager_marks_and_undoes_goods_pickup(self):
        order = self.assigned_order()
        query = SimpleNamespace(
            data=f"pickup:{order.id}",
            from_user=SimpleNamespace(id=11, full_name="Otabek", username=None),
            message=SimpleNamespace(chat_id=11, message_id=order.manager_message_id),
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(manager_ids=frozenset({11}), orders_channel_id=-1001),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        async def synchronized(_context, order_id):
            return self.repo.get(order_id), True

        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        picked_up = self.repo.get(order.id)
        self.assertEqual(picked_up.status, "picked_up")
        self.assertIsNotNone(picked_up.picked_up_at)
        self.assertEqual(picked_up.courier_id, ABBOS_ID)
        self.assertIn("Товар у курьера", query.answer.await_args.args[0])

        query.data = f"undo_pickup:{order.id}"
        query.answer.reset_mock()
        with patch("app.handlers.orders._sync_order", side_effect=synchronized):
            await manager_pickup_action(SimpleNamespace(callback_query=query), context)

        pending = self.repo.get(order.id)
        self.assertEqual(pending.status, "pending")
        self.assertIsNone(pending.picked_up_at)
        self.assertIsNone(pending.courier_id)

    async def test_one_courier_cannot_have_two_current_destinations(self):
        first = self.assigned_order("A56")
        first = self.repo.transition(
            first.id,
            {"pending"},
            status="on_way",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            picked_up_at=datetime.now(TASHKENT).isoformat(),
            time_started=datetime.now(TASHKENT).isoformat(),
        )
        second = self.assigned_order("A57")
        query = SimpleNamespace(
            data=f"onway:{second.id}",
            from_user=SimpleNamespace(id=ABBOS_ID, full_name="Abbos", username=None),
            message=SimpleNamespace(
                chat_id=second.delivery_chat_id,
                message_id=second.delivery_message_id,
            ),
            answer=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "settings": SimpleNamespace(courier_ids=frozenset({ABBOS_ID})),
                "repo": self.repo,
            }),
            bot=SimpleNamespace(),
        )

        await courier_action(SimpleNamespace(callback_query=query), context)

        self.assertEqual(self.repo.get(second.id).status, "pending")
        self.assertIn(f"№{first.order_number}", query.answer.await_args.args[0])
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    def test_pickup_buttons_follow_order_state(self):
        pending = self.assigned_order()
        callbacks = [
            button.callback_data
            for keyboard in (manager_sent_keyboard(pending), orders_channel_keyboard(pending))
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(callbacks.count(f"pickup:{pending.id}"), 2)
        labels = [
            button.text
            for keyboard in (manager_sent_keyboard(pending), orders_channel_keyboard(pending))
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data == f"pickup:{pending.id}"
        ]
        self.assertEqual(labels, ["📦 Abbos забрал товар", "📦 Abbos забрал товар"])

        picked = self.repo.transition(
            pending.id,
            {"pending"},
            status="picked_up",
            picked_up_at=datetime.now(TASHKENT).isoformat(),
        )
        callbacks = [
            button.callback_data
            for row in manager_sent_keyboard(picked).inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn(f"undo_pickup:{picked.id}", callbacks)
        undo_button = next(
            button
            for row in manager_sent_keyboard(picked).inline_keyboard
            for button in row
            if button.callback_data == f"undo_pickup:{picked.id}"
        )
        self.assertEqual(undo_button.text, "↩️ Отменить: Abbos забрал товар")


class DeliveryMonitorServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def _assigned(self, product: str, latitude: float):
        order = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data={**order_data(product), "latitude": latitude},
        )
        return self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )

    def test_monitor_builds_dark_history_bright_current_segment_and_counts(self):
        now = datetime.now(TASHKENT).replace(second=0, microsecond=0)
        completed = self._assigned("A56", 41.320)
        completed = self.repo.transition(
            completed.id,
            {"pending"},
            status="picked_up",
            picked_up_at=(now - timedelta(minutes=80)).isoformat(),
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            actor_id=11,
            actor_name="Otabek",
            actor_role="manager",
        )
        completed = self.repo.transition(
            completed.id,
            {"picked_up"},
            status="on_way",
            time_started=(now - timedelta(minutes=70)).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )
        self.repo.transition(
            completed.id,
            {"on_way"},
            status="completed",
            delivered_at=(now - timedelta(minutes=45)).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )

        current = self._assigned("A57", 41.350)
        current = self.repo.transition(
            current.id,
            {"pending"},
            status="picked_up",
            picked_up_at=(now - timedelta(minutes=30)).isoformat(),
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            actor_id=11,
            actor_name="Otabek",
            actor_role="manager",
        )
        self.repo.transition(
            current.id,
            {"picked_up"},
            status="on_way",
            time_started=(now - timedelta(minutes=20)).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )
        waiting = self._assigned("A58", 41.360)

        monitor = build_delivery_monitor(self.repo)
        route = next(item for item in monitor["routes"] if item["courier_id"] == ABBOS_ID)

        self.assertEqual(monitor["summary"]["active"], 2)
        self.assertNotIn("new_orders", monitor["summary"])
        self.assertEqual(monitor["summary"]["waiting_pickup"], 1)
        self.assertEqual(monitor["summary"]["on_way"], 1)
        self.assertEqual(monitor["manager_counts"], [{"name": "Otabek", "orders": 3}])
        self.assertEqual(route["current_target"]["order_number"], current.order_number)
        self.assertEqual(route["current_path"][0], [WAREHOUSE["latitude"], WAREHOUSE["longitude"]])
        self.assertIsNotNone(route["courier_marker"])
        self.assertTrue(route["dark_paths"])
        states = {stop["order_number"]: stop["state"] for stop in monitor["stops"]}
        self.assertEqual(states[completed.order_number], "completed")
        self.assertEqual(states[current.order_number], "on_way")
        self.assertEqual(states[waiting.order_number], "pending")

        report = build_delivery_stats(self.repo, now.date())
        current_row = next(row for row in report["orders"] if row["id"] == current.id)
        self.assertIsNotNone(current_row["picked_up_time"])
        self.assertTrue(any(item["kind"] == "picked_up" for item in report["timeline"]))

    def test_monitor_exposes_text_only_orders_and_optional_map_details(self):
        mapped = self._assigned("A59", 41.355)
        self.repo.update(
            mapped.id,
            delivery_time="До 17:00",
            comment="Позвонить заранее",
            second_address_text="Вторая точка текстом",
        )
        text_only = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data={
                "seller_name": "Ali",
                "client_phone": "+998901112233",
                "product": "A60",
                "amount_usd": 120,
                "address_text": "Чиланзар, ориентир Корзинка",
                "delivery_time": "После 18:00",
                "comment": "Вход со двора",
            },
        )
        self.repo.transition(
            text_only.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )

        monitor = build_delivery_monitor(self.repo)

        mapped_stop = next(stop for stop in monitor["stops"] if stop["order_id"] == mapped.id)
        self.assertEqual(mapped_stop["delivery_time"], "До 17:00")
        self.assertEqual(mapped_stop["comment"], "Позвонить заранее")
        self.assertFalse(any(stop["order_id"] == text_only.id for stop in monitor["stops"]))
        self.assertEqual(len(monitor["unmapped_orders"]), 2)
        mapped_text = next(
            item for item in monitor["unmapped_orders"] if item["id"] == mapped.id
        )
        self.assertEqual(mapped_text["location_number"], 2)
        text_only_item = next(
            item for item in monitor["unmapped_orders"] if item["id"] == text_only.id
        )
        self.assertEqual(text_only_item["comment"], "Вход со двора")
        self.assertEqual(monitor["summary"]["unmapped"], 2)

    def test_undo_completion_is_not_duplicated_on_monitor_map(self):
        order = self._assigned("A61", 41.365)
        order = self.repo.transition(
            order.id,
            {"pending"},
            status="on_way",
            time_started=datetime.now(TASHKENT).isoformat(),
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )
        order = self.repo.transition(
            order.id,
            {"on_way"},
            status="completed",
            delivered_at=datetime.now(TASHKENT).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )
        self.repo.transition(
            order.id,
            {"completed"},
            status="on_way",
            delivered_at=None,
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )

        monitor = build_delivery_monitor(self.repo)
        order_stops = [
            stop for stop in monitor["stops"] if stop["order_id"] == order.id
        ]

        self.assertEqual(monitor["summary"]["completed_today"], 0)
        self.assertEqual(len(order_stops), 1)
        self.assertEqual(order_stops[0]["state"], "on_way")


class DeliveryMonitorWebTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "delivery.db"
        repo = OrderRepository(self.database_path)
        repo.initialize()
        repo.create(manager_id=11, manager_name="Otabek", data=order_data("A57"))
        from app import stats
        self.stats = stats
        self.patches = [
            patch.object(stats, "DATABASE_PATH", self.database_path),
            patch.object(stats, "STATS_USERNAME", "admin"),
            patch.object(stats, "STATS_PASSWORD", "strong-secret"),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(stats.app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    @staticmethod
    def auth():
        token = base64.b64encode(b"admin:strong-secret").decode()
        return {"Authorization": f"Basic {token}"}

    def test_monitor_page_and_state_use_same_password(self):
        self.assertEqual(self.client.get("/delivery/monitor").status_code, 401)
        page = self.client.get("/delivery/monitor", headers=self.auth())
        self.assertEqual(page.status_code, 200)
        self.assertIn("Мониторинг доставки", page.text)
        self.assertIn("Расчётная позиция", page.text)
        self.assertIn("Нет координат", page.text)
        self.assertIn("movementMarkers", page.text)
        self.assertIn("AbortController", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")

        state = self.client.get("/delivery/monitor/api/state", headers=self.auth())
        self.assertEqual(state.status_code, 200)
        self.assertIn("couriers", state.json())
        self.assertIn("manager_counts", state.json())


if __name__ == "__main__":
    unittest.main()
