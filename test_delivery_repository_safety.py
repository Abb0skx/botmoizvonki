import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.database import OrderRepository
from app.database.repository import (
    CLEANUP_MAX_ATTEMPTS,
    SCHEMA,
    SCHEMA_VERSION,
    SQLITE_INT_MAX,
)


class DeliveryRepositorySafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "delivery.db"
        self.repo = OrderRepository(self.path)
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

    def test_domain_validation_rejects_invalid_states_and_payment_statuses(self) -> None:
        order = self.create_order()

        with self.assertRaisesRegex(ValueError, "Unknown order status"):
            self.repo.update(order.id, status="lost")
        with self.assertRaisesRegex(ValueError, "Unknown payment status"):
            self.repo.update(order.id, payment_status="cash")
        with self.assertRaisesRegex(ValueError, "Unknown order status"):
            self.repo.transition(order.id, {"draft"}, status="shipping")

        current = self.repo.get(order.id)
        self.assertEqual(current.status, "draft")
        self.assertEqual(current.payment_status, "collect_on_delivery")

    def test_money_validation_prevents_overflow_and_non_integer_values(self) -> None:
        invalid_values = (-1, True, 10.5, SQLITE_INT_MAX + 1)
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.repo.create(
                        manager_id=101,
                        manager_name="Manager",
                        data={
                            "client_phone": "+998901111111",
                            "product": "Invalid",
                            "amount_uzs": value,
                        },
                    )

        order = self.create_order()
        for field in ("amount_usd", "amount_uzs", "received_usd", "received_uzs"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.repo.update(order.id, **{field: SQLITE_INT_MAX + 1})
        self.assertEqual(self.repo.count_all(), 1)

    def test_coordinates_are_finite_in_range_and_written_as_pairs(self) -> None:
        invalid_payloads = (
            {"latitude": 41.3},
            {"latitude": 91, "longitude": 69.2},
            {"latitude": 41.3, "longitude": -181},
            {"latitude": float("nan"), "longitude": 69.2},
            {"second_latitude": 41.3},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.create_order(**payload)

        order = self.create_order(latitude=41.338586, longitude=69.272757)
        with self.assertRaises(ValueError):
            self.repo.update(order.id, latitude=None)
        cleared = self.repo.update(order.id, latitude=None, longitude=None)
        self.assertIsNone(cleared.latitude)
        self.assertIsNone(cleared.longitude)

    def test_untouched_invalid_legacy_values_do_not_block_unrelated_updates(self) -> None:
        order = self.create_order(latitude=41.3, longitude=69.2)
        with self.repo.connect() as db:
            db.execute(
                """UPDATE orders
                   SET status='legacy', payment_status='legacy', amount_usd=-5,
                       latitude=999, longitude=999
                   WHERE id=?""",
                (order.id,),
            )

        updated = self.repo.update(order.id, comment="Legacy row remains editable")

        self.assertEqual(updated.comment, "Legacy row remains editable")
        self.assertEqual(updated.status, "legacy")
        with self.assertRaises(ValueError):
            self.repo.update(order.id, amount_usd=-5)
        with self.assertRaises(ValueError):
            self.repo.update(order.id, latitude=41.3)
        recovered = self.repo.transition(order.id, {"legacy"}, status="pending")
        self.assertEqual(recovered.status, "pending")

    def test_only_one_order_can_atomically_become_on_way_for_a_courier(self) -> None:
        orders = [self.create_order(product=f"Order {index}") for index in range(2)]
        for order in orders:
            self.repo.transition(
                order.id,
                {"draft"},
                status="pending",
                assigned_courier_id=7636344727,
                assigned_courier_name="Olmas",
            )

        barrier = threading.Barrier(2)

        def start(order_id: int):
            repository = OrderRepository(self.path)
            barrier.wait(timeout=5)
            return repository.transition(
                order_id,
                {"pending"},
                guard_courier_id=7636344727,
                require_no_other_on_way_for_courier=True,
                status="on_way",
                courier_id=7636344727,
                courier_name="Olmas",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, [order.id for order in orders]))

        self.assertEqual(sum(result is not None for result in results), 1)
        with self.repo.connect() as db:
            count = db.execute(
                """SELECT COUNT(*) FROM orders
                   WHERE status='on_way'
                     AND COALESCE(courier_id, assigned_courier_id)=?""",
                (7636344727,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_on_way_guard_requires_and_matches_effective_courier(self) -> None:
        order = self.create_order()
        order = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=10,
        )
        with self.assertRaisesRegex(ValueError, "guard_courier_id is required"):
            self.repo.transition(
                order.id,
                {"pending"},
                require_no_other_on_way_for_courier=True,
                status="on_way",
            )

    def test_old_courier_cannot_mutate_an_order_after_reassignment(self) -> None:
        order = self.create_order()
        order = self.repo.transition(
            order.id,
            {"draft"},
            status="pending",
            assigned_courier_id=10,
            assigned_courier_name="Courier A",
        )
        reassigned = self.repo.transition(
            order.id,
            {"pending"},
            status="pending",
            assigned_courier_id=11,
            assigned_courier_name="Courier B",
        )

        stale_courier_action = self.repo.transition(
            order.id,
            {"pending"},
            status="cancelled",
            courier_id=10,
            courier_name="Courier A",
            guard_courier_id=10,
            require_unassigned_or_same=True,
        )

        self.assertIsNone(stale_courier_action)
        current = self.repo.get(order.id)
        self.assertEqual(current.updated_at, reassigned.updated_at)
        self.assertEqual(current.status, "pending")
        self.assertEqual(current.assigned_courier_id, 11)
        stale_start = self.repo.transition(
            order.id,
            {"pending"},
            status="on_way",
            courier_id=10,
            courier_name="Courier A",
            guard_courier_id=10,
            require_assigned_to_courier=True,
            require_no_other_on_way_for_courier=True,
        )
        self.assertIsNone(stale_start)
        with self.assertRaisesRegex(ValueError, "must match"):
            self.repo.transition(
                order.id,
                {"pending"},
                guard_courier_id=11,
                require_no_other_on_way_for_courier=True,
                status="on_way",
            )

    def test_unassigned_order_cannot_be_started_by_courier(self) -> None:
        order = self.create_order()
        order = self.repo.transition(order.id, {"draft"}, status="pending")

        started = self.repo.transition(
            order.id,
            {"pending"},
            status="on_way",
            courier_id=10,
            courier_name="Courier A",
            guard_courier_id=10,
            require_assigned_to_courier=True,
            require_no_other_on_way_for_courier=True,
        )

        self.assertIsNone(started)
        self.assertEqual(self.repo.get(order.id).status, "pending")

    def test_cleanup_retry_is_due_only_after_backoff_and_eventually_terminal(self) -> None:
        base = datetime(2026, 8, 24, tzinfo=timezone.utc)
        base_text = base.isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=base_text):
            order = self.create_order()
            self.repo.enqueue_cleanup_messages(order.id, [(-1001, 55)])
            cleanup = self.repo.list_cleanup_messages()[0]
            self.assertTrue(self.repo.mark_cleanup_failed(cleanup["id"], "timeout"))
            self.assertEqual(self.repo.list_cleanup_messages(), [])

        with self.repo.connect() as db:
            delayed = db.execute(
                "SELECT * FROM telegram_cleanup_queue WHERE id=?", (cleanup["id"],)
            ).fetchone()
        self.assertEqual(delayed["attempts"], 1)
        self.assertEqual(
            delayed["next_attempt_at"],
            (base + timedelta(seconds=30)).isoformat(timespec="microseconds"),
        )

        before_due = (base + timedelta(seconds=29)).isoformat(timespec="microseconds")
        after_due = (base + timedelta(seconds=31)).isoformat(timespec="microseconds")
        with patch("app.database.repository.now", return_value=before_due):
            self.assertEqual(self.repo.list_cleanup_messages(), [])
        with patch("app.database.repository.now", return_value=after_due):
            self.assertEqual(self.repo.list_cleanup_messages()[0]["id"], cleanup["id"])

        for attempt in range(2, CLEANUP_MAX_ATTEMPTS + 1):
            timestamp = (base + timedelta(hours=attempt)).isoformat(timespec="microseconds")
            with patch("app.database.repository.now", return_value=timestamp):
                self.assertTrue(
                    self.repo.mark_cleanup_failed(cleanup["id"], f"failure {attempt}")
                )
        self.assertEqual(self.repo.list_cleanup_messages(), [])
        terminal = self.repo.list_terminal_cleanup_messages()
        self.assertEqual(terminal[0]["id"], cleanup["id"])
        self.assertEqual(terminal[0]["attempts"], CLEANUP_MAX_ATTEMPTS)
        self.assertEqual(terminal[0]["terminal"], 1)

        self.assertTrue(self.repo.requeue_cleanup_message(cleanup["id"]))
        requeued = self.repo.list_cleanup_messages()[0]
        self.assertEqual(requeued["attempts"], 0)
        self.assertIsNone(requeued["last_error"])

    def test_permanent_cleanup_failure_and_operational_counts(self) -> None:
        order = self.create_order()
        self.repo.update(order.id, comment="needs sync")
        self.repo.enqueue_cleanup_messages(order.id, [(-1001, 56), (-1001, 57)])
        cleanup = self.repo.list_cleanup_messages()[0]

        self.assertTrue(
            self.repo.mark_cleanup_failed(cleanup["id"], "message missing", permanent=True)
        )
        self.assertFalse(
            self.repo.mark_cleanup_failed(cleanup["id"], "again", permanent=True)
        )
        self.assertEqual(
            self.repo.operational_counts(),
            {"sync_pending": 1, "cleanup_pending": 1, "cleanup_terminal": 1},
        )

    def test_initialize_migrates_cleanup_queue_and_is_idempotent(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy-cleanup.db"
        legacy_schema = "\n".join(
            line
            for line in SCHEMA.splitlines()
            if not line.strip().startswith(("next_attempt_at ", "terminal "))
        )
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)

        legacy_repo = OrderRepository(legacy_path)
        legacy_repo.initialize()
        legacy_repo.initialize()

        with sqlite3.connect(legacy_path) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(telegram_cleanup_queue)")
            }
            version = db.execute("PRAGMA user_version").fetchone()[0]
            indices = {
                row[1] for row in db.execute("PRAGMA index_list(telegram_cleanup_queue)")
            }
        self.assertIn("next_attempt_at", columns)
        self.assertIn("terminal", columns)
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("idx_cleanup_queue_due", indices)

    def test_schema_v7_preserves_old_separator_ids_as_location_headers(self) -> None:
        order = self.create_order()
        self.repo.update(
            order.id,
            location_chat_id=-1002,
            location_message_id=500,
            location_details_message_id=499,
            location_footer_message_id=501,
            second_location_chat_id=-1002,
            second_location_message_id=510,
            second_location_details_message_id=509,
            second_location_footer_message_id=511,
        )
        with sqlite3.connect(self.repo.path) as db:
            db.execute("PRAGMA user_version=6")

        self.repo.initialize()

        migrated = self.repo.get(order.id)
        self.assertEqual(migrated.location_header_message_id, 499)
        self.assertIsNone(migrated.location_details_message_id)
        self.assertEqual(migrated.location_footer_message_id, 501)
        self.assertEqual(migrated.second_location_header_message_id, 509)
        self.assertIsNone(migrated.second_location_details_message_id)
        self.assertEqual(migrated.second_location_footer_message_id, 511)

    def test_schema_v7_does_not_reshape_an_already_migrated_partial_block(self) -> None:
        order = self.create_order()
        self.repo.update(
            order.id,
            location_chat_id=-1002,
            location_header_message_id=None,
            location_message_id=500,
            location_details_message_id=501,
            location_footer_message_id=502,
        )

        self.repo.initialize()

        unchanged = self.repo.get(order.id)
        self.assertIsNone(unchanged.location_header_message_id)
        self.assertEqual(unchanged.location_details_message_id, 501)
        self.assertEqual(unchanged.location_footer_message_id, 502)

    def test_publication_references_are_attach_once_without_changing_business_revision(self) -> None:
        order = self.create_order()
        version = order.updated_at

        published = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "delivery_chat_id": None,
                "delivery_message_id": None,
            },
            delivery_chat_id=-1001,
            delivery_message_id=10,
        )
        losing_publish = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "delivery_chat_id": None,
                "delivery_message_id": None,
            },
            delivery_chat_id=-1001,
            delivery_message_id=11,
        )

        self.assertIsNotNone(published)
        self.assertEqual(published.updated_at, version)
        self.assertIsNone(losing_publish)
        canonical = self.repo.get(order.id)
        self.assertEqual(canonical.delivery_message_id, 10)
        self.assertEqual(canonical.updated_at, version)

    def test_stale_publication_clear_cannot_erase_a_replacement(self) -> None:
        order = self.create_order()
        order = self.repo.update(
            order.id,
            status="completed",
            delivered_at="2026-09-15T10:00:00+05:00",
            post_delivery_prompt_required=1,
            post_delivery_prompt_chat_id=-1001,
            post_delivery_prompt_message_id=11,
        )
        version = order.updated_at
        cleared = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": -1001,
                "post_delivery_prompt_message_id": 11,
            },
            post_delivery_prompt_chat_id=None,
            post_delivery_prompt_message_id=None,
        )
        replacement = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": None,
                "post_delivery_prompt_message_id": None,
            },
            post_delivery_prompt_chat_id=-1001,
            post_delivery_prompt_message_id=12,
        )
        stale_clear = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": -1001,
                "post_delivery_prompt_message_id": 11,
            },
            post_delivery_prompt_chat_id=None,
            post_delivery_prompt_message_id=None,
        )

        self.assertIsNotNone(cleared)
        self.assertIsNotNone(replacement)
        self.assertIsNone(stale_clear)
        canonical = self.repo.get(order.id)
        self.assertEqual(canonical.post_delivery_prompt_message_id, 12)
        self.assertEqual(canonical.updated_at, version)

    def test_transition_rejects_stale_publication_and_does_not_queue_wrong_cleanup(self) -> None:
        order = self.create_order()
        order = self.repo.update(
            order.id,
            status="completed",
            courier_id=202,
            courier_name="Courier",
            delivered_at="2026-09-15T10:00:00+05:00",
            post_delivery_prompt_required=1,
            post_delivery_prompt_chat_id=-1001,
            post_delivery_prompt_message_id=11,
        )
        version = order.updated_at
        cleared = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": -1001,
                "post_delivery_prompt_message_id": 11,
            },
            post_delivery_prompt_chat_id=None,
            post_delivery_prompt_message_id=None,
        )
        replacement = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": None,
                "post_delivery_prompt_message_id": None,
            },
            post_delivery_prompt_chat_id=-1001,
            post_delivery_prompt_message_id=12,
        )
        self.assertIsNotNone(cleared)
        self.assertIsNotNone(replacement)

        stale_transition = self.repo.transition(
            order.id,
            {"completed"},
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": -1001,
                "post_delivery_prompt_message_id": 11,
            },
            cleanup_messages=[(-1001, 11)],
            status="on_way",
            delivered_at=None,
            post_delivery_prompt_required=0,
            post_delivery_prompt_chat_id=None,
            post_delivery_prompt_message_id=None,
        )

        self.assertIsNone(stale_transition)
        canonical = self.repo.get(order.id)
        self.assertEqual(canonical.status, "completed")
        self.assertEqual(canonical.post_delivery_prompt_message_id, 12)
        self.assertEqual(self.repo.list_cleanup_messages(order_id=order.id), [])

        current_transition = self.repo.transition(
            order.id,
            {"completed"},
            expected_updated_at=version,
            expected_publications={
                "post_delivery_prompt_chat_id": -1001,
                "post_delivery_prompt_message_id": 12,
            },
            cleanup_messages=[(-1001, 12)],
            status="on_way",
            delivered_at=None,
            post_delivery_prompt_required=0,
            post_delivery_prompt_chat_id=None,
            post_delivery_prompt_message_id=None,
        )
        self.assertIsNotNone(current_transition)
        queued = self.repo.list_cleanup_messages(order_id=order.id)
        self.assertEqual(
            [(row["chat_id"], row["message_id"]) for row in queued],
            [(-1001, 12)],
        )

    def test_mark_synced_rejects_a_newer_publication_generation(self) -> None:
        order = self.create_order()
        order = self.repo.update(
            order.id,
            status="pending",
            delivery_chat_id=-1001,
            delivery_message_id=10,
        )
        version = order.updated_at
        cleared = self.repo.update(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "delivery_chat_id": -1001,
                "delivery_message_id": 10,
            },
            delivery_chat_id=None,
            delivery_message_id=None,
        )
        self.assertIsNotNone(cleared)
        self.assertEqual(cleared.updated_at, version)

        marked = self.repo.mark_synced(
            order.id,
            expected_updated_at=version,
            expected_publications={
                "delivery_chat_id": -1001,
                "delivery_message_id": 10,
            },
        )

        self.assertFalse(marked)
        canonical = self.repo.get(order.id)
        self.assertEqual(canonical.sync_needed, 1)
        self.assertIsNone(canonical.delivery_chat_id)
        self.assertIsNone(canonical.delivery_message_id)

    def test_concurrent_initializers_do_not_race_on_legacy_columns(self) -> None:
        legacy_path = Path(self.tempdir.name) / "concurrent-legacy.db"
        legacy_schema = "\n".join(
            line
            for line in SCHEMA.splitlines()
            if not line.strip().startswith(
                ("client_phone_2 ", "next_attempt_at ", "terminal ")
            )
        )
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)
        barrier = threading.Barrier(2)

        def initialize(_: int) -> None:
            barrier.wait(timeout=5)
            OrderRepository(legacy_path).initialize()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(initialize, range(2)))

        with sqlite3.connect(legacy_path) as db:
            order_columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
            cleanup_columns = {
                row[1] for row in db.execute("PRAGMA table_info(telegram_cleanup_queue)")
            }
        self.assertIn("client_phone_2", order_columns)
        self.assertIn("next_attempt_at", cleanup_columns)
        self.assertIn("terminal", cleanup_columns)

    def test_concurrent_process_initializers_retry_sqlite_wal_lock(self) -> None:
        shared_path = Path(self.tempdir.name) / "multiprocess-initialize.db"
        start_signal = Path(self.tempdir.name) / "start-initialize"
        script = (
            "import sys,time; from pathlib import Path; "
            "from app.database import OrderRepository; "
            "db=Path(sys.argv[1]); signal=Path(sys.argv[2]); "
            "\nwhile not signal.exists(): time.sleep(0.001)"
            "\nOrderRepository(db).initialize()"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(shared_path), str(start_signal)],
                cwd=Path(__file__).parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        start_signal.touch()
        failures: list[str] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode:
                failures.append(f"exit={process.returncode}\n{stdout}\n{stderr}")
        self.assertEqual(failures, [])

        with sqlite3.connect(shared_path) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_periodic_job_claim_table_is_initialized_at_current_schema(self) -> None:
        with self.repo.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                ("periodic_job_claims",),
            ).fetchone()

        self.assertEqual(SCHEMA_VERSION, 7)
        self.assertEqual(version, 7)
        self.assertIsNotNone(table)

    def test_periodic_job_claim_is_idempotent_per_job_and_slot(self) -> None:
        self.assertTrue(self.repo.claim_periodic_job("pickup_reminder", 100))
        self.assertFalse(self.repo.claim_periodic_job("pickup_reminder", 100))
        self.assertTrue(self.repo.claim_periodic_job("pickup_reminder", 101))
        self.assertTrue(self.repo.claim_periodic_job("another_job", 100))

    def test_concurrent_repositories_claim_periodic_slot_exactly_once(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)

        def claim(_: int) -> bool:
            repository = OrderRepository(self.path)
            barrier.wait(timeout=5)
            return repository.claim_periodic_job("pickup_reminder", 202608251230)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(claim, range(workers)))

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), workers - 1)


if __name__ == "__main__":
    unittest.main()
