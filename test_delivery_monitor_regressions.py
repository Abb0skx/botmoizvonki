import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.database import OrderRepository
from app.monitor_service import TASHKENT, build_delivery_monitor


ABBOS_ID = 202134293


def order_data(product: str) -> dict:
    return {
        "seller_name": "Ali",
        "client_phone": "+998901333999",
        "product": product,
        "amount_usd": 100,
        "latitude": 41.31,
        "longitude": 69.27,
        "address_text": "Основной адрес",
    }


class DeliveryMonitorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def _pending(self, product: str, *, read: bool = False):
        order = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data=order_data(product),
        )
        values = {
            "status": "pending",
            "assigned_courier_id": ABBOS_ID,
            "assigned_courier_name": "Abbos",
        }
        if read:
            values["courier_read_at"] = datetime.now(TASHKENT).isoformat(timespec="seconds")
            values["courier_id"] = ABBOS_ID
            values["courier_name"] = "Abbos"
        return self.repo.transition(order.id, {"draft"}, **values)

    def test_mapped_primary_does_not_hide_text_only_second_location(self):
        order = self._pending("A57")
        self.repo.update(
            order.id,
            second_address_text="Вторая точка, ориентир школа",
        )

        monitor = build_delivery_monitor(self.repo)

        self.assertEqual(len(monitor["stops"]), 1)
        missing = monitor["unmapped_locations"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["id"], order.id)
        self.assertEqual(missing[0]["location_number"], 2)
        self.assertIn("Вторая точка", missing[0]["address"])

    def test_all_pending_orders_wait_for_pickup_regardless_of_historical_read(self):
        self._pending("Новый")
        self._pending("Прочитан", read=True)
        self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data={**order_data("Черновик"), "delivery_time": "Срочно 🚨"},
        )

        monitor = build_delivery_monitor(self.repo)

        self.assertEqual(monitor["summary"]["active"], 3)
        self.assertNotIn("new_orders", monitor["summary"])
        self.assertEqual(monitor["summary"]["waiting_pickup"], 2)
        self.assertEqual(monitor["summary"]["unassigned"], 1)
        courier = next(item for item in monitor["couriers"] if item["id"] == ABBOS_ID)
        self.assertNotIn("new_orders", courier)
        self.assertNotIn("heading_to_warehouse", courier)
        self.assertEqual(courier["waiting_pickup"], 2)
        route = next(item for item in monitor["routes"] if item["courier_id"] == ABBOS_ID)
        self.assertIsNone(route["movement_kind"])
        self.assertNotIn("warehouse_started_at", route)
        draft = next(item for item in monitor["active_orders"] if item["status"] == "draft")
        self.assertTrue(draft["urgent"])
        self.assertIsNotNone(draft["deadline_at"])
        draft_stop = next(
            stop for stop in monitor["stops"] if stop["order_id"] == draft["id"]
        )
        self.assertIsNone(draft_stop["courier_id"])
        self.assertTrue(any(route["courier_id"] is None for route in monitor["routes"]))

    def test_remaining_orders_form_a_planned_route_back_to_warehouse(self):
        current = self._pending("Текущий", read=True)
        current = self.repo.transition(
            current.id,
            {"pending"},
            status="picked_up",
            picked_up_at=datetime.now(TASHKENT).isoformat(timespec="seconds"),
        )
        self.repo.transition(
            current.id,
            {"picked_up"},
            status="on_way",
            time_started=datetime.now(TASHKENT).isoformat(timespec="seconds"),
        )
        remaining = self._pending("Следующий")
        self.repo.update(
            remaining.id,
            latitude=41.36,
            longitude=69.31,
            delivery_time="Срочно 🚨",
        )

        monitor = build_delivery_monitor(self.repo)
        route = next(item for item in monitor["routes"] if item["courier_id"] == ABBOS_ID)

        self.assertEqual(route["planned_order_ids"], [remaining.id])
        self.assertEqual(route["planned_path"][0], route["current_path"][-1])
        self.assertEqual(route["planned_path"][-1], [41.337420, 69.272104])
        self.assertIn([41.36, 69.31], route["planned_path"])
        self.assertLess(
            route["planned_path"].index([41.337420, 69.272104]),
            route["planned_path"].index([41.36, 69.31]),
        )

    def test_carried_order_after_delivery_starts_at_last_customer(self):
        now = datetime.now(TASHKENT).replace(microsecond=0)
        completed = self._pending("Доставлен", read=True)
        completed = self.repo.transition(
            completed.id,
            {"pending"},
            status="picked_up",
            picked_up_at=(now - timedelta(minutes=50)).isoformat(),
        )
        completed = self.repo.transition(
            completed.id,
            {"picked_up"},
            status="on_way",
            time_started=(now - timedelta(minutes=30)).isoformat(),
        )
        self.repo.transition(
            completed.id,
            {"on_way"},
            status="completed",
            delivered_at=(now - timedelta(minutes=10)).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )
        carried = self._pending("У курьера", read=True)
        self.repo.update(carried.id, latitude=41.36, longitude=69.31)
        self.repo.transition(
            carried.id,
            {"pending"},
            status="picked_up",
            picked_up_at=(now - timedelta(minutes=50)).isoformat(),
        )

        monitor = build_delivery_monitor(self.repo)
        route = next(item for item in monitor["routes"] if item["courier_id"] == ABBOS_ID)

        self.assertEqual(route["return_path"], [])
        self.assertEqual(route["planned_path"][0], [41.31, 69.27])
        self.assertIn([41.36, 69.31], route["planned_path"])


if __name__ == "__main__":
    unittest.main()
