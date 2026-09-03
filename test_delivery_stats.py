import base64
from io import BytesIO
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from app.database import OrderRepository
from app.handlers.orders import show_statistics
from app.stats_service import TASHKENT, _money_text, build_delivery_stats, parse_report_day
from app.utils.static_map import (
    DeliverySequenceStop,
    MAP_HEIGHT,
    MAP_WIDTH,
    _spread_marker_positions,
    render_delivery_sequence_map,
)


MUZROB_ID = 1799690992
ABBOS_ID = 202134293


def _rgb_pixels(image):
    rgb_image = image.convert("RGB")
    get_flattened_data = getattr(rgb_image, "get_flattened_data", None)
    if get_flattened_data is not None:
        return get_flattened_data()
    return rgb_image.getdata()


class DeliveryStatsServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "delivery.db"
        self.repo = OrderRepository(self.database_path)
        self.repo.initialize()
        self.today = datetime.now(TASHKENT).date()

    def tearDown(self):
        self.tempdir.cleanup()

    def _order(self, product: str, latitude: float, longitude: float):
        return self.repo.create(
            manager_id=11,
            manager_name="Abbos",
            data={
                "seller_name": "Ali",
                "client_phone": "+998901333999",
                "product": product,
                "amount_usd": 100,
                "latitude": latitude,
                "longitude": longitude,
                "address_text": f"Адрес {product}, Ташкент",
            },
        )

    def test_today_stats_number_completed_then_active_stops(self):
        completed = self._order("A57", 41.31, 69.27)
        completed = self.repo.transition(
            completed.id,
            {"draft"},
            status="pending",
            assigned_courier_id=MUZROB_ID,
            assigned_courier_name="Muzrob Oka",
        )
        completed = self.repo.transition(
            completed.id,
            {"pending"},
            status="on_way",
            courier_id=MUZROB_ID,
            courier_name="Muzrob Oka",
            time_started=datetime.now(TASHKENT).isoformat(),
        )
        self.repo.transition(
            completed.id,
            {"on_way"},
            status="completed",
            courier_id=MUZROB_ID,
            courier_name="Muzrob Oka",
            delivered_at=datetime.now(TASHKENT).isoformat(),
        )

        active = self._order("A56", 41.35, 69.30)
        active = self.repo.update(
            active.id,
            second_latitude=41.36,
            second_longitude=69.31,
            second_address_text="Дополнительный адрес, Ташкент",
        )
        self.repo.transition(
            active.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )

        report = build_delivery_stats(self.repo, self.today)

        self.assertEqual(report["summary"]["orders"], 2)
        self.assertEqual(report["summary"]["completed"], 1)
        self.assertEqual(report["summary"]["active"], 1)
        self.assertEqual(report["summary"]["mapped"], 3)
        self.assertEqual([stop["sequence"] for stop in report["stops"]], [1, 2, 3])
        self.assertEqual(report["stops"][0]["order_number"], completed.order_number)
        self.assertEqual(report["stops"][1]["order_number"], active.order_number)
        self.assertEqual(report["stops"][2]["location_number"], 2)
        self.assertEqual({route["courier_id"] for route in report["routes"]}, {MUZROB_ID, ABBOS_ID})
        self.assertTrue(any(item["kind"] == "departed" for item in report["timeline"]))
        self.assertTrue(any(item["kind"] == "completed" for item in report["timeline"]))

        muzrob = build_delivery_stats(self.repo, self.today, courier_id=MUZROB_ID)
        self.assertEqual(muzrob["summary"]["orders"], 1)
        self.assertEqual(muzrob["summary"]["completed"], 1)
        self.assertTrue(all(stop["courier_id"] == MUZROB_ID for stop in muzrob["stops"]))

    def test_day_shortcuts_and_invalid_future_date(self):
        self.assertEqual(parse_report_day("today", today=self.today), self.today)
        self.assertEqual(
            parse_report_day("yesterday", today=self.today).toordinal(),
            self.today.toordinal() - 1,
        )
        with self.assertRaises(ValueError):
            parse_report_day("not-a-date", today=self.today)

    def test_reassigned_active_order_belongs_only_to_current_courier(self):
        order = self._order("A58", 41.32, 69.28)
        order = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )
        order = self.repo.transition(
            order.id,
            {"pending"},
            status="pending",
            courier_read_at=datetime.now(TASHKENT).isoformat(),
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
            event_type="courier_read",
        )
        self.repo.transition(
            order.id,
            {"pending"},
            status="pending",
            assigned_courier_id=MUZROB_ID,
            assigned_courier_name="Muzrob Oka",
            courier_id=None,
            courier_name=None,
            courier_read_at=None,
            actor_id=11,
            actor_name="Manager",
            actor_role="manager",
        )

        all_report = build_delivery_stats(self.repo, self.today)
        old_courier = build_delivery_stats(
            self.repo,
            self.today,
            courier_id=ABBOS_ID,
        )
        new_courier = build_delivery_stats(
            self.repo,
            self.today,
            courier_id=MUZROB_ID,
        )

        self.assertEqual(all_report["orders"][0]["courier_id"], MUZROB_ID)
        self.assertEqual(old_courier["summary"]["orders"], 0)
        self.assertEqual(new_courier["summary"]["orders"], 1)
        self.assertEqual(new_courier["summary"]["active"], 1)
        self.assertTrue(all(
            item["courier_id"] == MUZROB_ID
            for item in new_courier["timeline"]
        ))

    def test_report_exposes_text_only_orders_and_optional_map_details(self):
        mapped = self._order("A59", 41.34, 69.29)
        self.repo.update(
            mapped.id,
            delivery_time="До 16:30",
            comment="Проверит товар",
            second_address_text="Вторая точка только текстом",
        )
        text_only = self.repo.create(
            manager_id=11,
            manager_name="Abbos",
            data={
                "seller_name": "Ali",
                "client_phone": "+998909998877",
                "product": "A60",
                "amount_usd": 140,
                "address_text": "Юнусабад, 19 квартал",
                "delivery_time": "Срочно",
                "comment": "Домофон не работает",
            },
        )

        report = build_delivery_stats(self.repo, self.today)

        mapped_stop = next(stop for stop in report["stops"] if stop["order_id"] == mapped.id)
        self.assertEqual(mapped_stop["delivery_time"], "До 16:30")
        self.assertEqual(mapped_stop["comment"], "Проверит товар")
        self.assertFalse(any(stop["order_id"] == text_only.id for stop in report["stops"]))
        self.assertEqual(len(report["unmapped_orders"]), 2)
        second_text = next(
            item for item in report["unmapped_orders"] if item["id"] == mapped.id
        )
        self.assertEqual(second_text["location_number"], 2)
        text_only_row = next(
            item for item in report["unmapped_orders"] if item["id"] == text_only.id
        )
        self.assertEqual(text_only_row["delivery_time"], "Срочно")
        self.assertEqual(report["summary"]["unmapped"], 2)

    def test_yesterday_uses_status_at_end_of_that_day(self):
        yesterday = self.today - timedelta(days=1)
        yesterday_time = datetime(
            yesterday.year,
            yesterday.month,
            yesterday.day,
            15,
            tzinfo=TASHKENT,
        ).astimezone(timezone.utc).isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=yesterday_time):
            order = self._order("A58", 41.32, 69.28)
            order = self.repo.transition(
                order.id,
                {"draft"},
                status="pending",
                assigned_courier_id=MUZROB_ID,
                assigned_courier_name="Muzrob Oka",
            )

        order = self.repo.transition(
            order.id,
            {"pending"},
            status="on_way",
            courier_id=MUZROB_ID,
            courier_name="Muzrob Oka",
            time_started=datetime.now(TASHKENT).isoformat(),
            actor_id=MUZROB_ID,
            actor_name="Muzrob Oka",
            actor_role="courier",
        )
        self.repo.transition(
            order.id,
            {"on_way"},
            status="completed",
            delivered_at=datetime.now(TASHKENT).isoformat(),
            actor_id=MUZROB_ID,
            actor_name="Muzrob Oka",
            actor_role="courier",
        )

        report = build_delivery_stats(self.repo, yesterday)

        self.assertEqual(report["summary"]["orders"], 1)
        self.assertEqual(report["summary"]["completed"], 0)
        self.assertEqual(report["summary"]["active"], 1)
        self.assertEqual(report["orders"][0]["status"], "pending")

    def test_same_status_edit_does_not_create_false_completion_activity(self):
        yesterday = self.today - timedelta(days=1)
        event_time = datetime(
            yesterday.year, yesterday.month, yesterday.day, 14, tzinfo=TASHKENT
        ).astimezone(timezone.utc).isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=event_time):
            order = self._order("A61", 41.32, 69.28)
            order = self.repo.transition(order.id, {"draft"}, status="pending")
            order = self.repo.transition(
                order.id,
                {"pending"},
                status="on_way",
                time_started=datetime(
                    yesterday.year, yesterday.month, yesterday.day, 13, 30,
                    tzinfo=TASHKENT,
                ).isoformat(),
            )
            order = self.repo.transition(
                order.id,
                {"on_way"},
                status="completed",
                delivered_at=datetime(
                    yesterday.year, yesterday.month, yesterday.day, 14,
                    tzinfo=TASHKENT,
                ).isoformat(),
            )

        self.repo.update(order.id, comment="Только исправили комментарий")
        report = build_delivery_stats(self.repo, self.today)

        self.assertEqual(report["summary"]["orders"], 0)
        self.assertEqual(report["summary"]["completed"], 0)
        self.assertEqual(report["timeline"], [])

    def test_undo_completion_and_cancel_are_net_final_states(self):
        order = self._order("A62", 41.33, 69.29)
        order = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
        )
        order = self.repo.transition(
            order.id,
            {"pending"},
            status="on_way",
            time_started=datetime.now(TASHKENT).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )
        order = self.repo.transition(
            order.id,
            {"on_way"},
            status="completed",
            received_usd=100,
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
            received_usd=None,
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )

        cancelled = self._order("A63", 41.34, 69.30)
        cancelled = self.repo.transition(cancelled.id, {"draft"}, status="pending")
        cancelled = self.repo.transition(cancelled.id, {"pending"}, status="cancelled")
        self.repo.transition(cancelled.id, {"cancelled"}, status="pending")

        report = build_delivery_stats(self.repo, self.today)
        reopened = next(row for row in report["orders"] if row["id"] == order.id)

        self.assertEqual(report["summary"]["completed"], 0)
        self.assertEqual(report["summary"]["cancelled"], 0)
        self.assertEqual(report["summary"]["received_usd"], 0)
        self.assertEqual(reopened["status"], "on_way")
        self.assertIsNone(reopened["completed_time"])
        self.assertEqual(
            [stop["state"] for stop in report["stops"] if stop["order_id"] == order.id],
            ["on_way"],
        )

    def test_cancelled_stop_is_not_a_remaining_route_stop(self):
        order = self._order("Cancelled", 41.34, 69.30)
        order = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )
        self.repo.transition(
            order.id,
            {"pending"},
            status="cancelled",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )

        report = build_delivery_stats(self.repo, self.today)
        stop = next(item for item in report["stops"] if item["order_id"] == order.id)
        route = next(item for item in report["routes"] if item["courier_id"] == ABBOS_ID)

        self.assertEqual(stop["state"], "cancelled")
        self.assertNotIn(stop["sequence"], route["remaining_sequences"])

    def test_assignment_read_and_warehouse_response_metrics(self):
        start = datetime.combine(self.today, datetime.min.time(), TASHKENT) + timedelta(hours=10)
        def utc_text(value):
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

        with patch("app.database.repository.now", return_value=utc_text(start)):
            order = self._order("SLA", 41.33, 69.29)
        with patch("app.database.repository.now", return_value=utc_text(start + timedelta(minutes=5))):
            order = self.repo.transition(
                order.id,
                {"draft"},
                status="pending",
                assigned_courier_id=ABBOS_ID,
                assigned_courier_name="Abbos",
                actor_id=11,
                actor_name="Manager",
                actor_role="manager",
            )
        with patch("app.database.repository.now", return_value=utc_text(start + timedelta(minutes=10))):
            order = self.repo.transition(
                order.id,
                {"pending"},
                status="pending",
                courier_read_at=(start + timedelta(minutes=10)).isoformat(),
                courier_id=ABBOS_ID,
                courier_name="Abbos",
                actor_id=ABBOS_ID,
                actor_name="Abbos",
                actor_role="courier",
                event_type="courier_read",
            )
        with patch("app.database.repository.now", return_value=utc_text(start + timedelta(minutes=30))):
            self.repo.transition(
                order.id,
                {"pending"},
                status="picked_up",
                picked_up_at=(start + timedelta(minutes=30)).isoformat(),
                actor_id=11,
                actor_name="Warehouse",
                actor_role="staff",
            )

        report = build_delivery_stats(self.repo, self.today)
        row = next(item for item in report["orders"] if item["id"] == order.id)

        self.assertEqual(row["response_minutes"], 5)
        self.assertEqual(row["warehouse_minutes"], 20)
        self.assertEqual(report["summary"]["average_response_minutes"], 5)
        self.assertEqual(report["summary"]["average_warehouse_minutes"], 20)

    def test_past_delivery_keeps_historical_courier_after_reassignment(self):
        day = self.today - timedelta(days=1)
        start = datetime.combine(day, datetime.min.time(), TASHKENT) + timedelta(hours=10)
        def utc_text(value):
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

        with patch("app.database.repository.now", return_value=utc_text(start)):
            order = self._order("History", 41.35, 69.31)
            order = self.repo.transition(
                order.id,
                {"draft"},
                status="pending",
                assigned_courier_id=ABBOS_ID,
                assigned_courier_name="Abbos",
            )
        with patch("app.database.repository.now", return_value=utc_text(start + timedelta(hours=1))):
            order = self.repo.transition(
                order.id,
                {"pending"},
                status="completed",
                courier_id=ABBOS_ID,
                courier_name="Abbos",
                delivered_at=(start + timedelta(hours=1)).isoformat(),
                actor_id=ABBOS_ID,
                actor_name="Abbos",
                actor_role="courier",
            )
        self.repo.transition(
            order.id,
            {"completed"},
            status="pending",
            assigned_courier_id=MUZROB_ID,
            assigned_courier_name="Muzrob Oka",
            courier_id=None,
            courier_name=None,
            delivered_at=None,
        )

        abbos = build_delivery_stats(self.repo, day, courier_id=ABBOS_ID)
        muzrob = build_delivery_stats(self.repo, day, courier_id=MUZROB_ID)

        self.assertEqual(abbos["summary"]["completed"], 1)
        self.assertEqual(abbos["orders"][0]["courier_id"], ABBOS_ID)
        self.assertEqual(muzrob["summary"]["orders"], 0)

    def test_stats_plan_keeps_carried_goods_at_last_customer(self):
        now = datetime.now(TASHKENT).replace(microsecond=0)
        delivered = self._order("First", 41.31, 69.27)
        delivered = self.repo.transition(
            delivered.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
        )
        delivered = self.repo.transition(
            delivered.id,
            {"pending"},
            status="picked_up",
            picked_up_at=(now - timedelta(minutes=50)).isoformat(),
        )
        carried = self._order("Second", 41.36, 69.31)
        carried = self.repo.transition(
            carried.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
        )
        self.repo.transition(
            carried.id,
            {"pending"},
            status="picked_up",
            picked_up_at=(now - timedelta(minutes=50)).isoformat(),
        )
        self.repo.transition(
            delivered.id,
            {"picked_up"},
            status="completed",
            delivered_at=(now - timedelta(minutes=10)).isoformat(),
            actor_id=ABBOS_ID,
            actor_name="Abbos",
            actor_role="courier",
        )

        report = build_delivery_stats(self.repo, self.today)
        route = next(item for item in report["routes"] if item["courier_id"] == ABBOS_ID)

        self.assertEqual(route["return_path"], [])
        self.assertEqual(route["planned_path"][0], [41.31, 69.27])
        self.assertIn([41.36, 69.31], route["planned_path"])

    def test_stats_plan_tolerates_legacy_missing_pickup_timestamp(self):
        for index, picked_up_at in enumerate((None, datetime.now(TASHKENT).isoformat())):
            order = self._order(f"Legacy-{index}", 41.34 + index / 100, 69.28)
            self.repo.transition(
                order.id,
                {"draft"},
                status="picked_up",
                assigned_courier_id=ABBOS_ID,
                assigned_courier_name="Abbos",
                courier_id=ABBOS_ID,
                courier_name="Abbos",
                picked_up_at=picked_up_at,
            )

        report = build_delivery_stats(self.repo, self.today)
        route = next(item for item in report["routes"] if item["courier_id"] == ABBOS_ID)

        self.assertEqual(len(route["planned_order_ids"]), 2)

    def test_cross_midnight_delivery_keeps_duration(self):
        start = datetime(
            self.today.year, self.today.month, self.today.day, tzinfo=TASHKENT
        ) - timedelta(minutes=10)
        completed = start + timedelta(minutes=20)
        with patch(
            "app.database.repository.now",
            return_value=start.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        ):
            order = self._order("A64", 41.35, 69.31)
            order = self.repo.transition(order.id, {"draft"}, status="pending")
            order = self.repo.transition(
                order.id,
                {"pending"},
                status="on_way",
                time_started=start.isoformat(),
            )
        with patch(
            "app.database.repository.now",
            return_value=completed.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        ):
            self.repo.transition(
                order.id,
                {"on_way"},
                status="completed",
                delivered_at=completed.isoformat(),
            )

        report = build_delivery_stats(self.repo, self.today)

        self.assertEqual(report["orders"][0]["duration_minutes"], 20)
        self.assertEqual(report["summary"]["average_minutes"], 20)

    def test_day_read_event_wins_over_later_mutable_read_field(self):
        yesterday = self.today - timedelta(days=1)
        read_at = datetime(
            yesterday.year, yesterday.month, yesterday.day, 16, 5, tzinfo=TASHKENT
        )
        with patch(
            "app.database.repository.now",
            return_value=read_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        ):
            order = self._order("A65", 41.36, 69.32)
            order = self.repo.transition(
                order.id,
                {"draft"},
                status="pending",
                assigned_courier_id=ABBOS_ID,
                assigned_courier_name="Abbos",
            )
            self.repo.transition(
                order.id,
                {"pending"},
                status="pending",
                courier_read_at=read_at.isoformat(),
                courier_id=ABBOS_ID,
                courier_name="Abbos",
                actor_id=ABBOS_ID,
                actor_name="Abbos",
                actor_role="courier",
                event_type="courier_read",
            )
        self.repo.update(
            order.id,
            courier_read_at=datetime.now(TASHKENT).isoformat(),
        )

        report = build_delivery_stats(self.repo, yesterday)

        self.assertEqual(report["orders"][0]["read_time"], "16:05")
        self.assertEqual(report["orders"][0]["courier_id"], ABBOS_ID)

    def test_money_summary_is_explicit_and_never_assumes_received(self):
        order = self._order("A66", 41.37, 69.33)
        order = self.repo.transition(order.id, {"draft"}, status="pending")
        self.repo.transition(
            order.id,
            {"pending"},
            status="completed",
            delivered_at=datetime.now(TASHKENT).isoformat(),
        )

        report = build_delivery_stats(self.repo, self.today)

        self.assertEqual(_money_text(100, 0).count("100$"), 1)
        self.assertEqual(report["summary"]["created_value_usd"], 100)
        self.assertEqual(report["summary"]["delivered_value_usd"], 100)
        self.assertEqual(report["summary"]["received_usd"], 0)
        self.assertEqual(report["summary"]["missing_received_confirmation"], 1)
        self.assertEqual(report["breakdowns"]["managers"][0]["name"], "Abbos")


class DeliverySequenceMapTests(unittest.IsolatedAsyncioTestCase):
    def test_dense_markers_are_spread_without_leaving_the_image(self):
        positions = _spread_marker_positions(
            [(sequence, 640 + sequence % 3, 480 + sequence % 2) for sequence in range(1, 13)],
            obstacle=(640, 480),
        )

        self.assertEqual(len(positions), 12)
        self.assertTrue(any(point != (640 + sequence % 3, 480 + sequence % 2) for sequence, point in positions.items()))
        for x, y in positions.values():
            self.assertGreaterEqual(x, 40)
            self.assertLessEqual(x, MAP_WIDTH - 40)
            self.assertGreaterEqual(y, 90)
            self.assertLessEqual(y, MAP_HEIGHT - 72)
        values = list(positions.values())
        for index, (x, y) in enumerate(values):
            for other_x, other_y in values[index + 1:]:
                self.assertGreaterEqual(
                    ((x - other_x) ** 2 + (y - other_y) ** 2) ** 0.5,
                    72,
                )

    async def test_png_uses_numbered_colored_stops_and_route(self):
        stops = [
            DeliverySequenceStop(1, 20, 41.31, 69.27, MUZROB_ID, "Muzrob Oka", "#10b981"),
            DeliverySequenceStop(2, 21, 41.36, 69.31, MUZROB_ID, "Muzrob Oka", "#10b981"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.utils.static_map._load_tile",
                AsyncMock(return_value=Image.new("RGB", (256, 256), "#dbeafe")),
            ):
                result = await render_delivery_sequence_map(
                    stops,
                    report_day_label="23.08.2026",
                    cache_dir=Path(directory),
                )

        with Image.open(result) as image:
            self.assertEqual(image.size, (MAP_WIDTH, MAP_HEIGHT))
            green_pixels = sum(
                1
                for red, green, blue in _rgb_pixels(image)
                if green > 100 and red < 80 and blue < 170
            )
        self.assertGreater(green_pixels, 300)


class DeliveryStatsWebTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "delivery.db"
        repo = OrderRepository(self.database_path)
        repo.initialize()
        repo.create(
            manager_id=11,
            manager_name="Abbos",
            data={
                "seller_name": "Ali",
                "client_phone": "+998901333999",
                "product": "A57",
                "amount_usd": 100,
                "latitude": 41.31,
                "longitude": 69.27,
            },
        )
        from app import stats
        self.stats = stats
        self.database_patch = patch.object(stats, "DATABASE_PATH", self.database_path)
        self.password_patch = patch.object(stats, "STATS_PASSWORD", "strong-secret")
        self.username_patch = patch.object(stats, "STATS_USERNAME", "admin")
        self.database_patch.start()
        self.password_patch.start()
        self.username_patch.start()
        self.client = TestClient(stats.app)

    def tearDown(self):
        self.client.close()
        self.username_patch.stop()
        self.password_patch.stop()
        self.database_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _auth(username: str = "admin", password: str = "strong-secret") -> dict[str, str]:
        value = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {value}"}

    def test_page_and_api_require_password(self):
        response = self.client.get("/delivery/stats")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["www-authenticate"])
        wrong = self.client.get("/delivery/stats", headers=self._auth(password="wrong"))
        self.assertEqual(wrong.status_code, 401)

        page = self.client.get("/delivery/stats", headers=self._auth())
        self.assertEqual(page.status_code, 200)
        self.assertIn("Статистика доставки", page.text)
        self.assertIn("Разбивка за день", page.text)
        self.assertIn("loadMapPhoto", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.headers["x-frame-options"], "DENY")

        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["database"], "ok")

        report = self.client.get(
            "/delivery/stats/api/report?day=today",
            headers=self._auth(),
        )
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["summary"]["orders"], 1)
        self.assertIn("breakdowns", report.json())
        self.assertIn("created_value_text", report.json()["summary"])

    def test_map_endpoint_returns_png(self):
        image = BytesIO(b"png-data")
        image.name = "stats.png"
        with patch.object(
            self.stats,
            "render_delivery_sequence_map",
            AsyncMock(return_value=image),
        ):
            response = self.client.get(
                "/delivery/stats/map.png?day=today",
                headers=self._auth(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, b"png-data")

    def test_internal_monitoring_api_uses_separate_service_token(self):
        with patch.object(
            self.stats, "MONITORING_DELIVERY_SERVICE_TOKEN", "portal-service-key"
        ), patch.object(
            self.stats,
            "enrich_stats_routes",
            AsyncMock(side_effect=lambda report, _service: report),
        ):
            denied = self.client.get(
                "/internal/monitoring/v1/delivery/report?day=today"
            )
            self.assertEqual(denied.status_code, 403)
            response = self.client.get(
                "/internal/monitoring/v1/delivery/report?day=today",
                headers={"Authorization": "Bearer portal-service-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["orders"], 1)

    def test_legacy_page_redirects_to_monitoring_with_filters(self):
        with patch.object(
            self.stats,
            "MONITORING_BASE_URL",
            "https://bot.texnikach.uz/monitoring",
        ):
            response = self.client.get(
                "/delivery/stats?day=yesterday&delivery_courier_id=1799690992",
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "https://bot.texnikach.uz/monitoring/delivery/stats"
            "?day=yesterday&delivery_courier_id=1799690992",
        )


class DeliveryStatsBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_gets_today_yesterday_and_courier_links(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=11),
            effective_message=message,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "settings": SimpleNamespace(
                        manager_ids=frozenset({11}),
                        stats_url="https://bot.texnikach.uz/delivery/stats",
                    )
                }
            ),
            user_data={},
        )

        await show_statistics(update, context)

        markup = message.reply_text.await_args.kwargs["reply_markup"]
        urls = [
            button.url
            for row in markup.inline_keyboard
            for button in row
            if button.url
        ]
        self.assertTrue(any("day=today" in url for url in urls))
        self.assertTrue(any("day=yesterday" in url for url in urls))
        self.assertEqual(sum("courier_id=" in url for url in urls), 3)


if __name__ == "__main__":
    unittest.main()
