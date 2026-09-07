import base64
import calendar
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.analytics_service import _forecast, build_delivery_analytics, parse_month, parse_week
from app.database import OrderRepository
from app.routing_service import RoutingService, enrich_monitor_routes, enrich_stats_routes
from app.stats_service import TASHKENT


ABBOS_ID = 202134293
MUZROB_ID = 1799690992
OLMAS_ID = 7636344727


def _road_result(points, *, distance=4_200, duration=600):
    return {
        "provider": "osrm",
        "approximate": True,
        "geometry": [points[0], [41.325, 69.275], points[-1]],
        "legs": [],
        "distance_m": distance,
        "duration_s": duration,
    }


class RoutingServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tempdir.name) / "routing.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_route_is_persistently_cached(self):
        points = [[41.33742, 69.272104], [41.311, 69.279]]
        service = RoutingService(self.cache_path, "https://router.invalid")
        response = _road_result(points)
        with patch.object(service, "_fetch", Mock(return_value=response)) as fetch:
            first = service._route_sync(points)
            second = service._route_sync(points)

        self.assertEqual(first, response)
        self.assertEqual(second, response)
        fetch.assert_called_once_with(points)

        restarted = RoutingService(self.cache_path, "https://router.invalid")
        with patch.object(restarted, "_fetch") as fetch_after_restart:
            self.assertEqual(restarted._route_sync(points), response)
        fetch_after_restart.assert_not_called()

    def test_router_failure_uses_safe_road_estimate(self):
        points = [[41.33742, 69.272104], [41.311, 69.279]]
        service = RoutingService(self.cache_path, "https://router.invalid")
        with patch.object(service, "_fetch", side_effect=OSError("offline")):
            route = service._route_sync(points)

        self.assertEqual(route["provider"], "fallback")
        self.assertGreater(route["distance_m"], 0)
        self.assertGreater(route["duration_s"], 0)

    async def test_monitor_enrichment_adds_eta_movement_and_mileage(self):
        started = (datetime.now(TASHKENT) - timedelta(minutes=5)).isoformat()
        points = [[41.33742, 69.272104], [41.311, 69.279]]
        routing = Mock()
        routing.route = AsyncMock(side_effect=lambda value: _road_result(value))
        state = {
            "summary": {},
            "couriers": [{"id": ABBOS_ID}],
            "routes": [{
                "courier_id": ABBOS_ID,
                "dark_paths": [],
                "current_path": points,
                "return_path": [],
                "movement_kind": "delivery",
                "movement_started_at": started,
            }],
        }

        result = await enrich_monitor_routes(state, routing)
        movement = result["routes"][0]["movement"]

        self.assertEqual(movement["kind"], "delivery")
        self.assertEqual(movement["distance_km"], 4.2)
        self.assertEqual(movement["duration_minutes"], 10)
        self.assertGreater(movement["progress"], 0.45)
        self.assertLess(movement["progress"], 0.55)
        self.assertIsNotNone(movement["eta_at"])
        self.assertGreater(result["summary"]["distance_today_km"], 2)

    async def test_stats_enrichment_counts_completed_return_and_planned_roads(self):
        routing = Mock()
        routing.route = AsyncMock(side_effect=lambda points: _road_result(points))
        report = {
            "summary": {},
            "couriers": [{"id": ABBOS_ID}],
            "routes": [{
                "courier_id": ABBOS_ID,
                "completed_paths": [[[41.33742, 69.272104], [41.311, 69.279]]],
                "current_path": [[41.311, 69.279], [41.35, 69.31]],
                "return_path": [],
            }],
        }

        result = await enrich_stats_routes(report, routing)

        self.assertEqual(result["summary"]["distance_km"], 4.2)
        self.assertEqual(result["summary"]["planned_distance_km"], 4.2)
        self.assertEqual(result["couriers"][0]["distance_km"], 4.2)
        self.assertEqual(result["couriers"][0]["route_minutes"], 10)
        self.assertEqual(result["routes"][0]["completed_road_segments"][0]["duration_minutes"], 10)
        self.assertEqual(result["routes"][0]["estimated_minutes"], 10)


class DeliveryAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "delivery.db"
        self.repo = OrderRepository(self.database_path)
        self.repo.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def _create(self, when: datetime, district: str, product: str):
        timestamp = when.astimezone(timezone.utc).isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=timestamp):
            return self.repo.create(
                manager_id=11,
                manager_name="Abbos",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": product,
                    "amount_usd": 100,
                    "latitude": 41.31,
                    "longitude": 69.27,
                    "district": district,
                },
            )

    def _complete(self, order, when: datetime, courier_id: int, courier_name: str):
        timestamp = when.astimezone(timezone.utc).isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=timestamp):
            return self.repo.transition(
                order.id,
                {order.status},
                status="completed",
                assigned_courier_id=courier_id,
                assigned_courier_name=courier_name,
                courier_id=courier_id,
                courier_name=courier_name,
                delivered_at=timestamp,
                actor_id=courier_id,
                actor_name=courier_name,
                actor_role="courier",
            )

    def test_month_week_districts_and_forecasts(self):
        today = datetime.now(TASHKENT).date()
        month_start = today.replace(day=1)
        previous_month_end = month_start - timedelta(days=1)
        previous_month_start = previous_month_end.replace(day=1)
        current_day = min(today.day, 15)
        selected_day = month_start.replace(day=current_day)
        previous_selected_day = previous_month_start + timedelta(
            days=min(current_day - 1, previous_month_end.day - 1)
        )
        self._create(
            datetime.combine(selected_day, datetime.min.time(), TASHKENT) + timedelta(hours=12),
            "Юнусабадский район",
            "A57",
        )
        self._create(
            datetime.combine(selected_day, datetime.min.time(), TASHKENT) + timedelta(hours=13),
            "Юнусабадский район",
            "A56",
        )
        self._create(
            datetime.combine(previous_selected_day, datetime.min.time(), TASHKENT) + timedelta(hours=12),
            "Чиланзарский район",
            "A55",
        )
        week = today - timedelta(days=today.weekday())
        result = build_delivery_analytics(
            self.repo,
            month=month_start.strftime("%Y-%m"),
            week=f"{week.isocalendar().year}-W{week.isocalendar().week:02d}",
        )

        self.assertEqual(result["month"]["orders"], 2)
        self.assertEqual(result["month"]["previous_orders"], 1)
        self.assertEqual(result["month"]["districts"][0]["name"], "Юнусабадский район")
        self.assertEqual(result["month"]["districts"][0]["orders"], 2)
        self.assertEqual(len(result["week"]["series"]), 7)
        self.assertTrue(all("baseline" in item for item in result["week"]["series"]))
        self.assertEqual(len(result["next_7_days"]), 7)
        self.assertTrue(all(0 <= item["probability"] <= 100 for item in result["next_7_days"]))

    def test_future_forecast_does_not_treat_unobserved_future_as_history(self):
        today = date(2026, 8, 24)
        counts = Counter({today - timedelta(days=7 * offset): 4 for offset in range(1, 9)})

        near = _forecast(today + timedelta(days=7), counts, min(counts), as_of=today)
        far = _forecast(today + timedelta(days=70), counts, min(counts), as_of=today)

        self.assertEqual(near["expected"], far["expected"])
        self.assertGreater(near["expected"], 0)

    def test_empty_history_has_low_forecast_confidence(self):
        today = date(2026, 8, 24)

        forecast = _forecast(
            today + timedelta(days=1),
            Counter(),
            None,
            as_of=today - timedelta(days=1),
        )

        self.assertEqual(forecast["sample"], 0)
        self.assertEqual(forecast["confidence"], "низкая")
        self.assertEqual(forecast["expected"], 0)

    def test_current_incomplete_day_does_not_change_future_forecast(self):
        today = datetime.now(TASHKENT).date()
        for offset in (7, 14, 21, 28):
            day = today - timedelta(days=offset)
            self._create(
                datetime.combine(day, datetime.min.time(), TASHKENT) + timedelta(hours=12),
                "Чиланзарский район",
                f"Past-{offset}",
            )
        before = build_delivery_analytics(self.repo)["next_7_days"]
        for index in range(12):
            self._create(
                datetime.combine(today, datetime.min.time(), TASHKENT) + timedelta(hours=10),
                "Чиланзарский район",
                f"Today-{index}",
            )

        after = build_delivery_analytics(self.repo)["next_7_days"]

        self.assertEqual(before, after)

    def test_current_month_change_uses_equal_number_of_days(self):
        fake_today = date(2026, 3, 31)
        for day_number in range(1, 32):
            self._create(
                datetime(2026, 3, day_number, 12, tzinfo=TASHKENT),
                "Юнусабадский район",
                f"March-{day_number}",
            )
        for day_number in range(1, 29):
            self._create(
                datetime(2026, 2, day_number, 12, tzinfo=TASHKENT),
                "Юнусабадский район",
                f"February-{day_number}",
            )

        with patch("app.analytics_service.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026,
                3,
                31,
                18,
                tzinfo=TASHKENT,
            )
            result = build_delivery_analytics(self.repo, month="2026-03")

        self.assertEqual(result["month"]["orders"], 31)
        self.assertEqual(result["month"]["previous_orders"], 28)
        self.assertEqual(result["month"]["change"], 0)
        self.assertEqual(result["month"]["districts"][0]["change"], 0)

    def test_analytics_can_be_filtered_by_courier(self):
        today = datetime.now(TASHKENT).date()
        first = self._create(
            datetime.combine(today, datetime.min.time(), TASHKENT) + timedelta(hours=11),
            "Юнусабадский район",
            "Abbos-order",
        )
        second = self._create(
            datetime.combine(today, datetime.min.time(), TASHKENT) + timedelta(hours=12),
            "Чиланзарский район",
            "Muzrob-order",
        )
        self.repo.transition(
            first.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
        )
        self.repo.transition(
            second.id,
            {"draft"},
            status="pending",
            assigned_courier_id=MUZROB_ID,
            assigned_courier_name="Muzrob Oka",
        )

        result = build_delivery_analytics(self.repo, courier_id=ABBOS_ID)

        self.assertEqual(result["selected_courier_id"], ABBOS_ID)
        self.assertEqual(result["month"]["orders"], 1)
        self.assertEqual(result["month"]["districts"][0]["name"], "Юнусабадский район")

    def test_completed_order_does_not_move_to_new_courier_analytics(self):
        today = datetime.now(TASHKENT).date()
        created_at = datetime.combine(today, datetime.min.time(), TASHKENT) + timedelta(hours=10)
        order = self._create(created_at, "Яшнабадский район", "Historical")
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
            status="completed",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            delivered_at=(created_at + timedelta(hours=1)).isoformat(),
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

        abbos = build_delivery_analytics(self.repo, courier_id=ABBOS_ID)
        muzrob = build_delivery_analytics(self.repo, courier_id=MUZROB_ID)

        self.assertEqual(abbos["month"]["orders"], 1)
        self.assertEqual(muzrob["month"]["orders"], 0)

    def test_monthly_delivery_totals_and_daily_counts_are_per_courier(self):
        today = datetime.now(TASHKENT).date()
        selected_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        created_at = datetime.combine(
            selected_month - timedelta(days=2),
            datetime.min.time(),
            TASHKENT,
        ) + timedelta(hours=12)
        abbos_order = self._create(created_at, "Яшнабадский район", "Abbos-delivery")
        muzrob_first = self._create(created_at, "Чиланзарский район", "Muzrob-1")
        muzrob_second = self._create(created_at, "Чиланзарский район", "Muzrob-2")
        self._complete(
            abbos_order,
            datetime.combine(selected_month.replace(day=2), datetime.min.time(), TASHKENT)
            + timedelta(hours=14),
            ABBOS_ID,
            "Abbos",
        )
        for order, hour in ((muzrob_first, 11), (muzrob_second, 16)):
            self._complete(
                order,
                datetime.combine(selected_month.replace(day=5), datetime.min.time(), TASHKENT)
                + timedelta(hours=hour),
                MUZROB_ID,
                "Muzrob Oka",
            )

        result = build_delivery_analytics(
            self.repo,
            month=selected_month.strftime("%Y-%m"),
            courier_id=ABBOS_ID,
        )
        deliveries = result["month"]["courier_deliveries"]
        rows = {row["id"]: row for row in deliveries["couriers"]}

        self.assertEqual(deliveries["total"], 3)
        self.assertEqual(rows[ABBOS_ID]["completed"], 1)
        self.assertEqual(rows[ABBOS_ID]["daily"][1]["completed"], 1)
        self.assertEqual(rows[MUZROB_ID]["completed"], 2)
        self.assertEqual(rows[MUZROB_ID]["daily"][4]["completed"], 2)
        self.assertEqual(rows[OLMAS_ID]["completed"], 0)
        self.assertEqual(rows[MUZROB_ID]["active_days"], 1)
        self.assertEqual(len(rows[ABBOS_ID]["daily"]), calendar.monthrange(
            selected_month.year,
            selected_month.month,
        )[1])

    def test_undone_completion_is_excluded_and_recompletion_counts_once(self):
        today = datetime.now(TASHKENT).date()
        selected_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        created_at = datetime.combine(selected_month, datetime.min.time(), TASHKENT)
        recompleted = self._create(created_at, "Яшнабадский район", "Recompleted")
        undone = self._create(created_at, "Яшнабадский район", "Undone")
        recompleted = self._complete(
            recompleted,
            datetime.combine(selected_month.replace(day=3), datetime.min.time(), TASHKENT)
            + timedelta(hours=12),
            ABBOS_ID,
            "Abbos",
        )
        undone = self._complete(
            undone,
            datetime.combine(selected_month.replace(day=3), datetime.min.time(), TASHKENT)
            + timedelta(hours=13),
            ABBOS_ID,
            "Abbos",
        )
        undo_time = datetime.combine(
            selected_month.replace(day=4), datetime.min.time(), TASHKENT
        ) + timedelta(hours=10)
        undo_timestamp = undo_time.astimezone(timezone.utc).isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=undo_timestamp):
            recompleted = self.repo.transition(
                recompleted.id,
                {"completed"},
                status="pending",
                assigned_courier_id=MUZROB_ID,
                assigned_courier_name="Muzrob Oka",
                courier_id=None,
                courier_name=None,
                delivered_at=None,
            )
            self.repo.transition(
                undone.id,
                {"completed"},
                status="pending",
                delivered_at=None,
            )
        self._complete(
            recompleted,
            datetime.combine(selected_month.replace(day=6), datetime.min.time(), TASHKENT)
            + timedelta(hours=15),
            MUZROB_ID,
            "Muzrob Oka",
        )

        deliveries = build_delivery_analytics(
            self.repo,
            month=selected_month.strftime("%Y-%m"),
        )["month"]["courier_deliveries"]
        rows = {row["id"]: row for row in deliveries["couriers"]}

        self.assertEqual(deliveries["total"], 1)
        self.assertEqual(rows[ABBOS_ID]["completed"], 0)
        self.assertEqual(rows[MUZROB_ID]["completed"], 1)
        self.assertEqual(rows[MUZROB_ID]["daily"][5]["completed"], 1)

    def test_delivery_day_uses_tashkent_timezone(self):
        created_at = datetime(2026, 7, 20, 12, tzinfo=TASHKENT)
        order = self._create(created_at, "Юнусабадский район", "Timezone")
        self._complete(
            order,
            datetime(2026, 7, 31, 20, 30, tzinfo=timezone.utc),
            ABBOS_ID,
            "Abbos",
        )

        deliveries = build_delivery_analytics(
            self.repo,
            month="2026-08",
        )["month"]["courier_deliveries"]
        abbos = next(row for row in deliveries["couriers"] if row["id"] == ABBOS_ID)

        self.assertEqual(deliveries["total"], 1)
        self.assertEqual(abbos["daily"][0]["completed"], 1)

    def test_month_and_week_validation(self):
        today = date(2026, 8, 24)
        self.assertEqual(parse_month("2026-08", today=today), date(2026, 8, 1))
        self.assertEqual(parse_week("2026-W35", today=today), date(2026, 8, 24))
        with self.assertRaises(ValueError):
            parse_month("08-2026", today=today)
        with self.assertRaises(ValueError):
            parse_week("2026-W99", today=today)


class DeliveryAnalyticsWebTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "delivery.db"
        repo = OrderRepository(self.database_path)
        repo.initialize()
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

    def test_analytics_endpoint_is_protected_and_navigable(self):
        self.assertEqual(self.client.get("/delivery/stats/api/analytics").status_code, 401)
        response = self.client.get(
            "/delivery/stats/api/analytics?month=2026-08&week=2026-W35",
            headers=self.auth(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["month"]["value"], "2026-08")
        self.assertEqual(response.json()["week"]["value"], "2026-W35")
        self.assertIn("courier_deliveries", response.json()["month"])

        unknown = self.client.get(
            "/delivery/stats/api/analytics?courier_id=999",
            headers=self.auth(),
        )
        self.assertEqual(unknown.status_code, 422)

    def test_statistics_page_contains_monthly_courier_calendar(self):
        response = self.client.get("/delivery/stats", headers=self.auth())

        self.assertEqual(response.status_code, 200)
        self.assertIn("Доставки курьеров за месяц", response.text)
        self.assertIn("delivery_courier_id", response.text)
        self.assertIn('aria-pressed="${active}"', response.text)
        self.assertIn('scope="col"', response.text)
        self.assertIn('aria-live="polite"', response.text)


if __name__ == "__main__":
    unittest.main()
