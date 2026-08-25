import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import OrderRepository
from app.database.repository import (
    MIGRATION_COLUMNS,
    ORDER_EVENT_MIGRATION_COLUMNS,
    SCHEMA,
    SCHEMA_VERSION,
)
from app.models import Order
from app.utils.formatters import courier_card, manager_card, orders_channel_card


class CancellationAuditRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "delivery.db"
        self.repo = OrderRepository(self.path)
        self.repo.initialize()
        self.order = self.repo.create(
            manager_id=101,
            manager_name="Manager",
            data={
                "client_phone": "+998901333999",
                "product": "A57 Pro",
                "amount_usd": 375,
            },
        )
        self.order = self.repo.update(
            self.order.id,
            status="on_way",
            assigned_courier_id=202134293,
            assigned_courier_name="Abbos",
            courier_id=202134293,
            courier_name="Abbos",
            time_started="2026-08-25T17:50:00+05:00",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_cancellation_snapshot_and_actor_username_are_persisted(self) -> None:
        cancelled = self.repo.transition(
            self.order.id,
            {"on_way"},
            expected_updated_at=self.order.updated_at,
            status="cancelled",
            cancelled_by_id=7497097483,
            cancelled_by_name="Otabek",
            cancelled_by_username="otabek_texnikach",
            cancelled_at="2026-08-25T18:15:00+05:00",
            cancelled_from_status="on_way",
            actor_id=7497097483,
            actor_name="Otabek",
            actor_username="otabek_texnikach",
            actor_role="group_member",
            event_type="order_cancelled",
        )

        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.cancelled_by_id, 7497097483)
        self.assertEqual(cancelled.cancelled_by_name, "Otabek")
        self.assertEqual(cancelled.cancelled_by_username, "otabek_texnikach")
        self.assertEqual(cancelled.cancelled_at, "2026-08-25T18:15:00+05:00")
        self.assertEqual(cancelled.cancelled_from_status, "on_way")
        # Cancelling by another group member must never steal courier custody.
        self.assertEqual(cancelled.assigned_courier_id, 202134293)
        self.assertEqual(cancelled.courier_id, 202134293)

        event = self.repo.list_events(cancelled.id)[-1]
        self.assertEqual(event.event_type, "order_cancelled")
        self.assertEqual(event.actor_id, 7497097483)
        self.assertEqual(event.actor_name, "Otabek")
        self.assertEqual(event.actor_username, "otabek_texnikach")
        self.assertEqual(event.actor_role, "group_member")
        self.assertEqual(event.courier_id, 202134293)
        self.assertEqual(event.courier_name, "Abbos")
        self.assertEqual((event.from_status, event.to_status), ("on_way", "cancelled"))

    def test_restoration_keeps_last_cancellation_snapshot_and_audits_actor(self) -> None:
        cancelled = self.repo.transition(
            self.order.id,
            {"on_way"},
            status="cancelled",
            cancelled_by_id=7497097483,
            cancelled_by_name="Otabek",
            cancelled_by_username="otabek_texnikach",
            cancelled_at="2026-08-25T18:15:00+05:00",
            cancelled_from_status="on_way",
            actor_id=7497097483,
            actor_name="Otabek",
            actor_username="otabek_texnikach",
            actor_role="group_member",
            event_type="order_cancelled",
        )

        restored = self.repo.transition(
            cancelled.id,
            {"cancelled"},
            status=cancelled.cancelled_from_status,
            actor_id=202134293,
            actor_name="Abbos",
            actor_username="abbos_texnikach",
            actor_role="courier",
            event_type="order_cancel_restored",
        )

        self.assertEqual(restored.status, "on_way")
        self.assertEqual(restored.cancelled_by_id, 7497097483)
        self.assertEqual(restored.cancelled_by_username, "otabek_texnikach")
        self.assertEqual(restored.cancelled_at, "2026-08-25T18:15:00+05:00")
        event = self.repo.list_events(restored.id)[-1]
        self.assertEqual(event.event_type, "order_cancel_restored")
        self.assertEqual(event.actor_username, "abbos_texnikach")
        self.assertEqual(event.courier_id, 202134293)
        self.assertEqual(event.courier_name, "Abbos")
        self.assertEqual((event.from_status, event.to_status), ("cancelled", "on_way"))

    def test_unknown_cancellation_source_status_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown cancellation source status"):
            self.repo.transition(
                self.order.id,
                {"on_way"},
                status="cancelled",
                cancelled_from_status="teleported",
            )

    def test_initialize_migrates_legacy_order_and_event_tables(self) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.db"
        removed_order_columns = {
            "cancelled_by_id",
            "cancelled_by_name",
            "cancelled_by_username",
            "cancelled_at",
            "cancelled_from_status",
        }
        removed_event_columns = set(ORDER_EVENT_MIGRATION_COLUMNS)
        legacy_lines: list[str] = []
        table: str | None = None
        for line in SCHEMA.splitlines():
            stripped = line.strip()
            if stripped.startswith("CREATE TABLE IF NOT EXISTS orders "):
                table = "orders"
            elif stripped.startswith("CREATE TABLE IF NOT EXISTS order_events "):
                table = "order_events"
            column_names = (
                removed_order_columns
                if table == "orders"
                else removed_event_columns if table == "order_events" else set()
            )
            if any(stripped.startswith(f"{column} ") for column in column_names):
                continue
            legacy_lines.append(line)
            if stripped == ");":
                table = None
        legacy_schema = "\n".join(legacy_lines)
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)
            db.execute(
                """INSERT INTO orders
                   (order_number, manager_id, manager_name, client_phone, product,
                    created_at, updated_at)
                   VALUES (1, 101, 'Manager', '+998901333999', 'A57',
                           '2026-08-25T10:00:00+00:00', '2026-08-25T10:00:00+00:00')"""
            )
            db.execute(
                """INSERT INTO order_events
                   (order_id, order_number, event_type, actor_id, actor_name,
                    actor_role, from_status, to_status, changed_fields, created_at)
                   VALUES (1, 1, 'status_changed', 202134293, 'Abbos',
                           'courier', 'pending', 'on_way', '[]',
                           '2026-08-25T10:05:00+00:00')"""
            )

        legacy_repo = OrderRepository(legacy_path)
        legacy_repo.initialize()
        legacy_repo.initialize()

        with legacy_repo.connect() as db:
            order_columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
            event_columns = {
                row[1] for row in db.execute("PRAGMA table_info(order_events)")
            }
            version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue(removed_order_columns <= order_columns)
        self.assertTrue(set(MIGRATION_COLUMNS) <= order_columns)
        self.assertTrue(removed_event_columns <= event_columns)
        self.assertEqual(version, SCHEMA_VERSION)
        migrated_event = legacy_repo.list_events(1)[0]
        self.assertEqual(migrated_event.actor_name, "Abbos")
        self.assertIsNone(migrated_event.actor_username)
        self.assertIsNone(migrated_event.courier_id)
        self.assertIsNone(migrated_event.courier_name)

    def test_explicit_event_courier_snapshot_is_independent_from_actor(self) -> None:
        event = self.repo.add_event(
            self.order.id,
            "group_note",
            actor_id=900,
            actor_name="Сотрудник склада",
            actor_username="warehouse_staff",
            actor_role="group_member",
            event_courier_id=202134293,
            event_courier_name="Abbos",
        )

        self.assertEqual(event.actor_id, 900)
        self.assertEqual(event.actor_name, "Сотрудник склада")
        self.assertEqual(event.courier_id, 202134293)
        self.assertEqual(event.courier_name, "Abbos")


class CancellationAuditFormatterTests(unittest.TestCase):
    @staticmethod
    def cancelled_order() -> Order:
        return Order(
            id=7,
            order_number=31,
            manager_id=101,
            manager_name="Manager",
            client_phone="+998901333999",
            product="A57 Pro",
            status="cancelled",
            assigned_courier_id=202134293,
            assigned_courier_name="Abbos",
            cancelled_by_id=7497097483,
            cancelled_by_name="Otabek <Admin>",
            cancelled_by_username="@otabek&texnikach",
            cancelled_at="2026-08-25T13:15:00+00:00",
            cancelled_from_status="on_way",
        )

    def test_current_cancelled_cards_show_escaped_actor_id_username_and_time(self) -> None:
        order = self.cancelled_order()

        for text in (
            courier_card(order, "❌ <b>Заказ отменён</b>"),
            manager_card(order),
            orders_channel_card(order),
        ):
            with self.subTest(card=text[:30]):
                self.assertIn("Отменил: <b>Otabek &lt;Admin&gt;</b>", text)
                self.assertIn("@otabek&amp;texnikach", text)
                self.assertIn("Telegram ID: <code>7497097483</code>", text)
                self.assertIn("Время отмены: 25.08.2026 18:15", text)
                self.assertNotIn("<Admin>", text)

    def test_restored_card_does_not_show_retained_cancellation_snapshot(self) -> None:
        order = self.cancelled_order()
        order.status = "on_way"

        self.assertNotIn("Отменил:", courier_card(order))
        self.assertNotIn("Telegram ID:", manager_card(order))
        self.assertNotIn("Время отмены:", orders_channel_card(order))


if __name__ == "__main__":
    unittest.main()
