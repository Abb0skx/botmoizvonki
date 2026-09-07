import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import OrderRepository


class DeliveryStatusFeedRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_order(self, **extra):
        return self.repo.create(
            manager_id=101,
            manager_name="Manager",
            data={
                "client_phone": "+998901111111",
                "client_phone_2": "+998902222222",
                "product": "iPhone 16 Pro Max",
                "amount_usd": 100,
                **extra,
            },
        )

    def test_raw_cursor_advances_across_pages_with_only_hidden_events(self) -> None:
        order = self.create_order()
        self.repo.add_event(order.id, "courier_read")
        self.repo.transition(order.id, {"draft"}, status="pending")

        first = self.repo.delivery_status_event_feed(after_event_id=0, limit=2)

        self.assertEqual(first["events"], [])
        self.assertEqual(first["next_after_event_id"], 2)
        self.assertEqual(first["latest_event_id"], 3)
        self.assertTrue(first["has_more"])

        second = self.repo.delivery_status_event_feed(
            after_event_id=first["next_after_event_id"],
            limit=2,
        )

        self.assertEqual(
            [event["to_status"] for event in second["events"]],
            ["pending"],
        )
        self.assertEqual(second["next_after_event_id"], 3)
        self.assertEqual(second["latest_event_id"], 3)
        self.assertFalse(second["has_more"])

    def test_feed_filters_noop_internal_backward_and_draft_cancellation(self) -> None:
        order = self.create_order()
        pending = self.repo.transition(order.id, {"draft"}, status="pending")
        self.repo.add_event(order.id, "courier_read")
        picked_up = self.repo.transition(order.id, {"pending"}, status="picked_up")
        on_way = self.repo.transition(order.id, {"picked_up"}, status="on_way")
        backward = self.repo.transition(order.id, {"on_way"}, status="picked_up")
        resumed = self.repo.transition(order.id, {"picked_up"}, status="on_way")
        cancelled = self.repo.transition(order.id, {"on_way"}, status="cancelled")

        draft_cancelled = self.create_order(product="AirPods Pro 2")
        self.repo.transition(draft_cancelled.id, {"draft"}, status="cancelled")

        feed = self.repo.delivery_status_event_feed(limit=100)

        self.assertEqual(
            [event["to_status"] for event in feed["events"]],
            ["cancelled"],
        )
        visible_ids = {event["event_id"] for event in feed["events"]}
        backward_event = next(
            event
            for event in self.repo.list_events(order.id)
            if event.from_status == "on_way" and event.to_status == "picked_up"
        )
        self.assertNotIn(backward_event.id, visible_ids)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(picked_up.status, "picked_up")
        self.assertEqual(on_way.status, "on_way")
        self.assertEqual(backward.status, "picked_up")
        self.assertEqual(resumed.status, "on_way")
        self.assertEqual(cancelled.status, "cancelled")

        first = feed["events"][0]
        self.assertEqual(first["order_id"], order.id)
        self.assertEqual(first["order_number"], order.order_number)
        self.assertEqual(first["client_phone"], "+998901111111")
        self.assertEqual(first["client_phone_2"], "+998902222222")
        self.assertEqual(first["product"], "iPhone 16 Pro Max")
        self.assertEqual(first["current_status"], "cancelled")
        self.assertNotIn("manager_name", first)
        self.assertNotIn("amount_usd", first)

        # Restoring an order is an internal/backward transition. It also makes
        # the older cancellation stale, so neither state is exposed.
        self.repo.transition(order.id, {"cancelled"}, status="pending")
        restored_feed = self.repo.delivery_status_event_feed(limit=100)
        self.assertEqual(restored_feed["events"], [])
        restored = next(
            item
            for item in restored_feed["invalidations"]
            if item["order_id"] == order.id
        )
        self.assertEqual(
            restored["current_status"], "pending"
        )

    def test_feed_identity_is_stable_and_cursor_reset_is_explicit(self) -> None:
        first = self.repo.delivery_status_event_feed()
        self.repo.initialize()
        second = self.repo.delivery_status_event_feed(
            after_event_id=first["latest_event_id"] + 10
        )

        self.assertEqual(first["feed_instance_id"], second["feed_instance_id"])
        self.assertTrue(second["cursor_reset_required"])
        self.assertEqual(second["events"], [])
        self.assertEqual(second["invalidations"], [])
        self.assertEqual(
            second["next_after_event_id"], first["latest_event_id"] + 10
        )

    def test_internal_confirmation_is_hidden_but_completion_is_visible(self) -> None:
        order = self.create_order()
        self.repo.transition(order.id, {"draft"}, status="pending")
        self.repo.transition(order.id, {"pending"}, status="awaiting_photo")
        self.repo.transition(order.id, {"awaiting_photo"}, status="completed")

        feed = self.repo.delivery_status_event_feed(limit=100)

        self.assertEqual(
            [event["to_status"] for event in feed["events"]],
            ["completed"],
        )

    def test_feed_validates_cursor_and_page_size(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.delivery_status_event_feed(after_event_id=-1)
        with self.assertRaises(ValueError):
            self.repo.delivery_status_event_feed(limit=0)
        with self.assertRaises(ValueError):
            self.repo.delivery_status_event_feed(limit=501)


class DeliveryStatusFeedWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "delivery.db"
        self.repo = OrderRepository(self.database_path)
        self.repo.initialize()
        self.order = self.repo.create(
            manager_id=101,
            manager_name="Manager",
            data={
                "client_phone": "+998901111111",
                "client_phone_2": "+998902222222",
                "product": "iPhone 16 Pro Max",
                "amount_usd": 100,
            },
        )
        self.repo.transition(self.order.id, {"draft"}, status="pending")

        from app import stats

        self.stats = stats
        self.database_patch = patch.object(stats, "DATABASE_PATH", self.database_path)
        self.token_patch = patch.object(
            stats,
            "MONITORING_DELIVERY_SERVICE_TOKEN",
            "portal-service-key",
        )
        self.database_patch.start()
        self.token_patch.start()
        self.client = TestClient(stats.app)

    def tearDown(self) -> None:
        self.client.close()
        self.token_patch.stop()
        self.database_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def auth() -> dict[str, str]:
        return {"Authorization": "Bearer portal-service-key"}

    def test_endpoint_requires_internal_bearer_and_has_no_store_headers(self) -> None:
        denied = self.client.get(
            "/internal/monitoring/v1/delivery/status-events"
        )
        wrong = self.client.get(
            "/internal/monitoring/v1/delivery/status-events",
            headers={"Authorization": "Bearer wrong"},
        )
        response = self.client.get(
            "/internal/monitoring/v1/delivery/status-events?limit=1",
            headers=self.auth(),
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["events"], [])
        self.assertEqual(response.json()["next_after_event_id"], 1)
        self.assertEqual(response.json()["latest_event_id"], 2)
        self.assertTrue(response.json()["has_more"])

    def test_endpoint_returns_minimal_visible_event_on_next_page(self) -> None:
        response = self.client.get(
            "/internal/monitoring/v1/delivery/status-events"
            "?after_event_id=1&limit=10",
            headers=self.auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["next_after_event_id"], 2)
        self.assertEqual(payload["latest_event_id"], 2)
        self.assertFalse(payload["has_more"])
        self.assertFalse(payload["cursor_reset_required"])
        self.assertTrue(payload["feed_instance_id"])
        self.assertEqual(payload["invalidations"], [])
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(
            set(payload["events"][0]),
            {
                "event_id",
                "order_id",
                "order_number",
                "event_type",
                "from_status",
                "to_status",
                "created_at",
                "client_phone",
                "client_phone_2",
                "product",
                "current_status",
            },
        )

    def test_endpoint_rejects_invalid_cursor_and_limit(self) -> None:
        for query in ("after_event_id=-1", "limit=0", "limit=501"):
            with self.subTest(query=query):
                response = self.client.get(
                    "/internal/monitoring/v1/delivery/status-events?" + query,
                    headers=self.auth(),
                )
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
