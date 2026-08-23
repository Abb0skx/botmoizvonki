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
from app.stats_service import TASHKENT, build_delivery_stats, parse_report_day
from app.utils.static_map import (
    DeliverySequenceStop,
    MAP_HEIGHT,
    MAP_WIDTH,
    _spread_marker_positions,
    render_delivery_sequence_map,
)


MUZROB_ID = 1799690992
ABBOS_ID = 202134293


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
            self.assertGreaterEqual(y, 56)
            self.assertLessEqual(y, MAP_HEIGHT - 40)
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
                for red, green, blue in image.convert("RGB").get_flattened_data()
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
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.headers["x-frame-options"], "DENY")

        report = self.client.get(
            "/delivery/stats/api/report?day=today",
            headers=self._auth(),
        )
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["summary"]["orders"], 1)

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
