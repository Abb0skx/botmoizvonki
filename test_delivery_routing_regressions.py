import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

from app.routing_service import MAX_ROUTING_POINTS, RoutingService, enrich_monitor_routes


def road_result(points: list[list[float]]) -> dict:
    return {
        "provider": "osrm",
        "approximate": True,
        "geometry": points,
        "legs": [
            {
                "geometry": [start, finish],
                "distance_m": 100,
                "duration_s": 10,
                "summary": "",
            }
            for start, finish in zip(points, points[1:])
        ],
        "distance_m": 100 * (len(points) - 1),
        "duration_s": 10 * (len(points) - 1),
    }


class DeliveryRoutingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = RoutingService(
            Path(self.tempdir.name) / "routing.db",
            "https://router.invalid",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_long_route_is_split_into_bounded_overlapping_requests(self):
        points = [[41.2 + index / 1000, 69.2] for index in range(45)]

        with patch.object(
            self.service,
            "_fetch",
            side_effect=lambda part: road_result(part),
        ) as fetch, patch("app.routing_service.time.sleep"):
            result = self.service._route_sync(points)

        self.assertGreater(fetch.call_count, 1)
        self.assertTrue(all(
            len(call.args[0]) <= MAX_ROUTING_POINTS for call in fetch.call_args_list
        ))
        self.assertEqual(result["geometry"][0], points[0])
        self.assertEqual(result["geometry"][-1], points[-1])
        self.assertEqual(len(result["legs"]), len(points) - 1)

    def test_unroutable_point_does_not_disable_unrelated_route(self):
        bad = [[41.31, 69.27], [41.32, 69.28]]
        good = [[41.33, 69.29], [41.34, 69.30]]
        http_error = HTTPError(
            "https://router.invalid",
            400,
            "bad point",
            hdrs=None,
            fp=None,
        )
        fetch = Mock(side_effect=[http_error, road_result(good)])

        with patch.object(self.service, "_fetch", fetch), patch("app.routing_service.time.sleep"):
            self.assertEqual(self.service._route_sync(bad)["provider"], "fallback")
            self.assertEqual(self.service._route_sync(good)["provider"], "osrm")

        self.assertEqual(fetch.call_count, 2)

    def test_rate_limit_uses_short_global_cooldown_not_route_poisoning(self):
        points = [[41.31, 69.27], [41.32, 69.28]]
        http_error = HTTPError(
            "https://router.invalid",
            429,
            "too many requests",
            hdrs=None,
            fp=None,
        )

        with patch.object(
            self.service,
            "_fetch",
            side_effect=http_error,
        ), patch("app.routing_service.time.sleep"):
            result = self.service._route_sync(points)

        self.assertEqual(result["provider"], "fallback")
        self.assertGreater(self.service._failed_until, 0)
        self.assertNotIn(self.service._key(points), self.service._failed_keys)

    def test_transient_chunk_fallback_does_not_poison_long_route_cache(self):
        points = [[41.2 + index / 1000, 69.2] for index in range(21)]
        http_error = HTTPError(
            "https://router.invalid",
            429,
            "too many requests",
            hdrs=None,
            fp=None,
        )
        with patch.object(
            self.service,
            "_fetch",
            side_effect=[road_result(points[:20]), http_error],
        ), patch("app.routing_service.time.sleep"):
            result = self.service._route_sync(points)

        self.assertEqual(result["provider"], "mixed")
        self.assertIsNone(self.service._cached(self.service._key(points)))

    def test_corrupt_disposable_cache_does_not_block_service_startup(self):
        cache = Path(self.tempdir.name) / "corrupt-routing.db"
        cache.write_bytes(b"not a sqlite database")

        with self.assertLogs("app.routing_service", level="WARNING"):
            service = RoutingService(cache, "https://router.invalid")

        self.assertFalse(service._cache_enabled)


class DeliveryPlannedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_remaining_distance_includes_future_queue(self):
        routing = Mock()

        def result(points):
            response = road_result(points)
            response["distance_m"] = 1_000 * (len(points) - 1)
            response["duration_s"] = 600 * (len(points) - 1)
            return response

        routing.route = AsyncMock(side_effect=result)
        state = {
            "summary": {},
            "couriers": [{"id": 202134293}],
            "routes": [{
                "courier_id": 202134293,
                "dark_paths": [],
                "current_path": [[41.31, 69.27], [41.32, 69.28]],
                "return_path": [],
                "planned_path": [
                    [41.32, 69.28],
                    [41.36, 69.31],
                    [41.337420, 69.272104],
                ],
                "movement_kind": "delivery",
                "movement_started_at": None,
            }],
        }

        result_state = await enrich_monitor_routes(state, routing)
        route = result_state["routes"][0]

        self.assertEqual(route["planned_route"]["distance_km"], 2.0)
        self.assertEqual(route["planned_remaining_km"], 3.0)
        self.assertEqual(result_state["summary"]["planned_remaining_km"], 3.0)
        self.assertTrue(route["planned_road_path"])


if __name__ == "__main__":
    unittest.main()
