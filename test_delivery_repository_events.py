import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import OrderRepository
from app.database.repository import SCHEMA


class DeliveryRepositoryEventTests(unittest.TestCase):
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
                "product": "A7 Pro",
                "amount_usd": 100,
                **extra,
            },
        )

    def test_second_phone_is_created_read_and_updated(self) -> None:
        order = self.create_order(client_phone_2="+998902222222")
        self.assertEqual(order.client_phone_2, "+998902222222")

        updated = self.repo.update(order.id, client_phone_2="+998903333333")

        self.assertEqual(updated.client_phone_2, "+998903333333")
        self.assertEqual(self.repo.get(order.id).client_phone_2, "+998903333333")

    def test_sync_and_message_reference_fields_are_migrated_and_editable(self) -> None:
        order = self.create_order()
        self.assertIsNone(order.location_details_message_id)
        self.assertIsNone(order.second_location_details_message_id)
        self.assertIsNone(order.manager_chat_id)
        self.assertIsNone(order.manager_message_id)
        self.assertEqual(order.sync_needed, 0)

        updated = self.repo.update(
            order.id,
            location_details_message_id=501,
            second_location_details_message_id=502,
            manager_chat_id=601,
            manager_message_id=602,
        )

        self.assertEqual(updated.location_details_message_id, 501)
        self.assertEqual(updated.second_location_details_message_id, 502)
        self.assertEqual(updated.manager_chat_id, 601)
        self.assertEqual(updated.manager_message_id, 602)
        self.assertEqual(updated.sync_needed, 1)

    def test_second_phone_is_added_to_an_existing_database(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.db"
        legacy_schema = "\n".join(
            line
            for line in SCHEMA.splitlines()
            if not line.strip().startswith("client_phone_2 ")
        )
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)

        OrderRepository(legacy_path).initialize()

        with sqlite3.connect(legacy_path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        self.assertIn("client_phone_2", columns)

    def test_pickup_timestamp_is_migrated_and_keeps_order_open(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy-pickup.db"
        legacy_schema = "\n".join(
            line
            for line in SCHEMA.splitlines()
            if not line.strip().startswith("picked_up_at ")
        )
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)
        legacy = OrderRepository(legacy_path)
        legacy.initialize()
        order = legacy.create(
            manager_id=101,
            manager_name="Manager",
            data={
                "client_phone": "+998901111111",
                "product": "A7 Pro",
                "amount_usd": 100,
            },
        )
        picked_up = legacy.transition(
            order.id,
            {"draft"},
            status="picked_up",
            picked_up_at="2026-08-23T10:30:00+05:00",
        )

        self.assertEqual(picked_up.picked_up_at, "2026-08-23T10:30:00+05:00")
        self.assertEqual(legacy.count_open(), 1)
        self.assertEqual(legacy.list_active()[0].status, "picked_up")

    def test_pagination_and_counts_preserve_existing_list_methods(self) -> None:
        orders = [self.create_order(product=f"Model {number}") for number in range(5)]
        self.repo.transition(orders[0].id, {"draft"}, status="completed")
        self.repo.transition(orders[1].id, {"draft"}, status="cancelled")

        self.assertEqual(self.repo.count_all(), 5)
        self.assertEqual(
            [order.order_number for order in self.repo.list_all_page(limit=2, offset=1)],
            [4, 3],
        )
        self.assertEqual(len(self.repo.list_all()), 5)
        self.assertEqual(self.repo.count_open(), 3)
        self.assertEqual(
            [order.order_number for order in self.repo.list_open_page(limit=2)],
            [5, 4],
        )
        self.assertEqual(len(self.repo.list_open()), 3)
        with self.assertRaises(ValueError):
            self.repo.list_all_page(limit=0)
        with self.assertRaises(ValueError):
            self.repo.list_open_page(offset=-1)

    def test_optimistic_guards_reject_stale_updates_and_transitions(self) -> None:
        order = self.create_order()
        first = self.repo.update(
            order.id,
            expected_updated_at=order.updated_at,
            product="Updated once",
        )
        self.assertIsNotNone(first)
        self.assertNotEqual(first.updated_at, order.updated_at)

        stale = self.repo.update(
            order.id,
            expected_updated_at=order.updated_at,
            product="Stale update",
        )
        self.assertIsNone(stale)
        self.assertEqual(self.repo.get(order.id).product, "Updated once")

        stale_transition = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            expected_updated_at=order.updated_at,
        )
        self.assertIsNone(stale_transition)
        current = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            expected_updated_at=first.updated_at,
        )
        self.assertEqual(current.status, "pending")

    def test_sync_queue_and_mark_synced_do_not_add_an_event(self) -> None:
        first = self.create_order(product="First")
        second = self.create_order(product="Second")
        first = self.repo.update(first.id, comment="Needs synchronization")
        second = self.repo.update(second.id, product="Second updated")
        initial_event_count = len(self.repo.list_events(first.id))

        pending = self.repo.list_needing_sync(limit=10)
        self.assertEqual({order.id for order in pending}, {first.id, second.id})
        self.assertTrue(self.repo.mark_sync_attempted(first.id))
        self.assertEqual(
            [order.id for order in self.repo.list_needing_sync(limit=10)],
            [second.id, first.id],
        )
        self.assertTrue(
            self.repo.mark_synced(first.id, expected_updated_at=first.updated_at)
        )
        synced = self.repo.get(first.id)
        self.assertEqual(synced.sync_needed, 0)
        self.assertEqual(synced.updated_at, first.updated_at)
        self.assertEqual(len(self.repo.list_events(first.id)), initial_event_count)
        self.assertEqual([order.id for order in self.repo.list_needing_sync()], [second.id])

        stale_version = second.updated_at
        second = self.repo.update(second.id, comment="Changed during synchronization")
        self.assertFalse(
            self.repo.mark_synced(second.id, expected_updated_at=stale_version)
        )
        self.assertEqual(self.repo.get(second.id).sync_needed, 1)
        self.assertFalse(
            self.repo.mark_sync_attempted(
                second.id,
                expected_updated_at=stale_version,
            )
        )
        self.assertIsNone(self.repo.get(second.id).sync_attempted_at)
        self.assertTrue(
            self.repo.mark_sync_attempted(
                second.id,
                expected_updated_at=second.updated_at,
            )
        )

    def test_publication_references_do_not_invalidate_a_manager_edit(self) -> None:
        order = self.create_order()
        version = order.updated_at
        event_count = len(self.repo.list_events(order.id))

        published = self.repo.update(
            order.id,
            expected_updated_at=version,
            manager_chat_id=101,
            manager_message_id=501,
        )

        self.assertIsNotNone(published)
        self.assertEqual(published.updated_at, version)
        self.assertEqual(len(self.repo.list_events(order.id)), event_count)

        edited = self.repo.update(
            order.id,
            expected_updated_at=version,
            product="A7 Pro 256GB",
        )
        self.assertIsNotNone(edited)
        self.assertNotEqual(edited.updated_at, version)
        self.assertEqual(edited.product, "A7 Pro 256GB")
        self.assertEqual(len(self.repo.list_events(order.id)), event_count + 1)

    def test_location_cleanup_outbox_is_committed_with_transition(self) -> None:
        order = self.create_order()
        updated = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            cleanup_messages=[(-100123, 80), (-100123, 81)],
        )

        self.assertIsNotNone(updated)
        queued = self.repo.list_cleanup_messages(order_id=order.id)
        self.assertEqual(
            [(item["chat_id"], item["message_id"]) for item in queued],
            [(-100123, 80), (-100123, 81)],
        )
        self.assertTrue(self.repo.mark_cleanup_failed(queued[0]["id"], "timeout"))
        due = self.repo.list_cleanup_messages(order_id=order.id)
        self.assertEqual([item["message_id"] for item in due], [81])
        self.assertTrue(self.repo.mark_cleanup_done(due[0]["id"]))
        self.assertEqual(self.repo.list_cleanup_messages(order_id=order.id), [])
        with self.repo.connect() as db:
            delayed = db.execute(
                "SELECT * FROM telegram_cleanup_queue WHERE id=?", (queued[0]["id"],)
            ).fetchone()
        self.assertEqual(delayed["attempts"], 1)
        self.assertIsNotNone(delayed["next_attempt_at"])

    def test_create_update_and_transition_append_events(self) -> None:
        order = self.create_order()
        self.repo.update(order.id, product="A7 Pro 256GB")
        self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            courier_id=202,
            courier_name="Courier",
            guard_courier_id=202,
            require_unassigned_or_same=True,
        )

        events = self.repo.list_events(order.id)

        self.assertEqual(
            [event.event_type for event in events],
            ["order_created", "order_updated", "status_changed"],
        )
        self.assertEqual(events[0].actor_id, 101)
        self.assertEqual(events[0].actor_role, "manager")
        self.assertEqual(events[1].changed_fields, ("product",))
        self.assertEqual(events[2].from_status, "draft")
        self.assertEqual(events[2].to_status, "pending")
        self.assertEqual(events[2].actor_id, 202)
        self.assertEqual(events[2].actor_role, "courier")

    def test_events_can_be_selected_for_a_timezone_aware_day(self) -> None:
        order = self.create_order()
        self.repo.transition(order.id, {"draft"}, status="pending")
        center = datetime.now(timezone.utc)

        events = self.repo.list_events_between(
            center - timedelta(minutes=1),
            center + timedelta(minutes=1),
        )

        self.assertEqual(
            [event.event_type for event in events],
            ["order_created", "status_changed"],
        )
        with self.assertRaises(ValueError):
            self.repo.list_events_between(
                center.replace(tzinfo=None),
                center + timedelta(minutes=1),
            )

    def test_explicit_event_helper_and_append_only_guards(self) -> None:
        order = self.create_order()
        event = self.repo.add_event(
            order.id,
            "manager_note",
            actor_id=101,
            actor_name="Manager",
            actor_role="manager",
            changed_fields={"comment"},
        )
        self.assertEqual(event.event_type, "manager_note")
        self.assertEqual(event.changed_fields, ("comment",))

        with self.assertRaises(sqlite3.IntegrityError), self.repo.connect() as db:
            db.execute("UPDATE order_events SET event_type='changed' WHERE id=?", (event.id,))
        with self.assertRaises(sqlite3.IntegrityError), self.repo.connect() as db:
            db.execute("DELETE FROM order_events WHERE id=?", (event.id,))


if __name__ == "__main__":
    unittest.main()
