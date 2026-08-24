import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.models import Order, OrderEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number INTEGER NOT NULL UNIQUE,
    manager_id INTEGER NOT NULL,
    manager_name TEXT NOT NULL,
    seller_name TEXT,
    payment_status TEXT NOT NULL DEFAULT 'collect_on_delivery',
    client_phone TEXT NOT NULL,
    client_phone_2 TEXT,
    product TEXT NOT NULL,
    amount_usd INTEGER,
    amount_uzs INTEGER,
    location_url TEXT,
    latitude REAL,
    longitude REAL,
    address_text TEXT,
    district TEXT,
    mahalla TEXT,
    second_location_url TEXT,
    second_latitude REAL,
    second_longitude REAL,
    second_address_text TEXT,
    second_district TEXT,
    second_mahalla TEXT,
    delivery_time TEXT,
    comment TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    assigned_courier_id INTEGER,
    assigned_courier_name TEXT,
    courier_id INTEGER,
    courier_name TEXT,
    delivery_photo TEXT,
    received_usd INTEGER,
    received_uzs INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    courier_read_at TEXT,
    picked_up_at TEXT,
    time_started TEXT,
    delivery_chat_id INTEGER,
    delivery_message_id INTEGER,
    location_chat_id INTEGER,
    location_message_id INTEGER,
    location_details_message_id INTEGER,
    location_footer_message_id INTEGER,
    second_location_chat_id INTEGER,
    second_location_message_id INTEGER,
    second_location_details_message_id INTEGER,
    second_location_footer_message_id INTEGER,
    manager_chat_id INTEGER,
    manager_message_id INTEGER,
    orders_channel_chat_id INTEGER,
    orders_channel_message_id INTEGER,
    creation_token TEXT,
    sync_needed INTEGER NOT NULL DEFAULT 0,
    sync_attempted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_manager ON orders(manager_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_courier ON orders(courier_id, delivered_at);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO counters(name, value) VALUES ('order_number', 0);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    order_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_id INTEGER,
    actor_name TEXT,
    actor_role TEXT,
    from_status TEXT,
    to_status TEXT,
    changed_fields TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, id);
CREATE INDEX IF NOT EXISTS idx_order_events_created ON order_events(created_at);
CREATE TRIGGER IF NOT EXISTS order_events_no_update
BEFORE UPDATE ON order_events
BEGIN
    SELECT RAISE(ABORT, 'order_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS order_events_no_delete
BEFORE DELETE ON order_events
BEGIN
    SELECT RAISE(ABORT, 'order_events is append-only');
END;
CREATE TABLE IF NOT EXISTS telegram_cleanup_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_cleanup_queue_retry
ON telegram_cleanup_queue(attempts, id);
"""

MIGRATION_COLUMNS = {
    "assigned_courier_id": "INTEGER",
    "assigned_courier_name": "TEXT",
    "client_phone_2": "TEXT",
    "seller_name": "TEXT",
    "payment_status": "TEXT NOT NULL DEFAULT 'collect_on_delivery'",
    "address_text": "TEXT",
    "district": "TEXT",
    "mahalla": "TEXT",
    "location_chat_id": "INTEGER",
    "location_message_id": "INTEGER",
    "location_details_message_id": "INTEGER",
    "location_footer_message_id": "INTEGER",
    "second_location_url": "TEXT",
    "second_latitude": "REAL",
    "second_longitude": "REAL",
    "second_address_text": "TEXT",
    "second_district": "TEXT",
    "second_mahalla": "TEXT",
    "second_location_chat_id": "INTEGER",
    "second_location_message_id": "INTEGER",
    "second_location_details_message_id": "INTEGER",
    "second_location_footer_message_id": "INTEGER",
    "manager_chat_id": "INTEGER",
    "manager_message_id": "INTEGER",
    "orders_channel_chat_id": "INTEGER",
    "orders_channel_message_id": "INTEGER",
    "creation_token": "TEXT",
    "sync_needed": "INTEGER NOT NULL DEFAULT 0",
    "sync_attempted_at": "TEXT",
    "courier_read_at": "TEXT",
    "picked_up_at": "TEXT",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class OrderRepository:
    editable_fields = {
        "seller_name", "payment_status", "product", "client_phone", "client_phone_2", "amount_usd", "amount_uzs", "location_url",
        "latitude", "longitude", "address_text", "district", "mahalla",
        "second_location_url", "second_latitude", "second_longitude",
        "second_address_text", "second_district", "second_mahalla",
        "delivery_time", "comment", "status",
        "assigned_courier_id", "assigned_courier_name",
        "courier_id", "courier_name", "delivery_photo", "received_usd",
        "received_uzs", "delivered_at", "courier_read_at", "picked_up_at", "time_started", "delivery_chat_id",
        "delivery_message_id", "location_chat_id", "location_message_id",
        "location_details_message_id", "second_location_chat_id",
        "location_footer_message_id", "second_location_message_id",
        "second_location_details_message_id", "second_location_footer_message_id",
        "manager_chat_id", "manager_message_id", "orders_channel_chat_id",
        "orders_channel_message_id", "sync_needed",
    }

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            existing = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
            for column, definition in MIGRATION_COLUMNS.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_sync_retry "
                "ON orders(sync_needed, sync_attempted_at, updated_at, id)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_creation_token "
                "ON orders(creation_token) WHERE creation_token IS NOT NULL"
            )
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """UPDATE counters
                   SET value=MAX(value, (SELECT COALESCE(MAX(order_number), 0) FROM orders))
                   WHERE name='order_number'"""
            )

    @staticmethod
    def _insert_event(
        db: sqlite3.Connection,
        *,
        order_id: int,
        order_number: int,
        event_type: str,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_role: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        changed_fields: set[str] | list[str] | tuple[str, ...] = (),
    ) -> sqlite3.Row:
        event_type = event_type.strip()
        if not event_type:
            raise ValueError("event_type cannot be empty")
        serialized_fields = json.dumps(
            sorted(set(changed_fields)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cursor = db.execute(
            """INSERT INTO order_events
               (order_id, order_number, event_type, actor_id, actor_name, actor_role,
                from_status, to_status, changed_fields, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                order_number,
                event_type,
                actor_id,
                actor_name,
                actor_role,
                from_status,
                to_status,
                serialized_fields,
                now(),
            ),
        )
        return db.execute(
            "SELECT * FROM order_events WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()

    def create(self, *, manager_id: int, manager_name: str, data: dict[str, Any]) -> Order:
        timestamp = now()
        creation_token = data.get("creation_token")
        if creation_token is not None:
            creation_token = str(creation_token).strip()
            if not creation_token or len(creation_token) > 128:
                raise ValueError("invalid creation_token")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if creation_token:
                existing = db.execute(
                    "SELECT * FROM orders WHERE creation_token=?",
                    (creation_token,),
                ).fetchone()
                if existing:
                    return Order.from_row(existing)
            db.execute("UPDATE counters SET value=value+1 WHERE name='order_number'")
            number = db.execute("SELECT value FROM counters WHERE name='order_number'").fetchone()[0]
            cursor = db.execute(
                """INSERT INTO orders
                (order_number, manager_id, manager_name, seller_name, payment_status, client_phone, client_phone_2, product,
                 amount_usd, amount_uzs, location_url, latitude, longitude,
                 address_text, district, mahalla,
                 second_location_url, second_latitude, second_longitude,
                 second_address_text, second_district, second_mahalla,
                 delivery_time, comment,
                 creation_token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (number, manager_id, manager_name, data.get("seller_name"),
                 data.get("payment_status", "collect_on_delivery"),
                 data["client_phone"], data.get("client_phone_2"), data["product"],
                 data.get("amount_usd"), data.get("amount_uzs"), data.get("location_url"),
                 data.get("latitude"), data.get("longitude"), data.get("address_text"),
                 data.get("district"), data.get("mahalla"), data.get("second_location_url"),
                 data.get("second_latitude"), data.get("second_longitude"),
                 data.get("second_address_text"), data.get("second_district"),
                 data.get("second_mahalla"), data.get("delivery_time"),
                 data.get("comment"), creation_token, timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone()
            self._insert_event(
                db,
                order_id=row["id"],
                order_number=row["order_number"],
                event_type="order_created",
                actor_id=manager_id,
                actor_name=manager_name,
                actor_role="manager",
                to_status=row["status"],
                changed_fields=set(data) | {"manager_id", "manager_name", "order_number"},
            )
        return Order.from_row(row)

    def get(self, order_id: int) -> Order | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return Order.from_row(row) if row else None

    def get_by_creation_token(self, creation_token: str | None) -> Order | None:
        token = (creation_token or "").strip()
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM orders WHERE creation_token=?",
                (token,),
            ).fetchone()
        return Order.from_row(row) if row else None

    def get_active_delivery(self, courier_id: int) -> Order | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM orders
                   WHERE courier_id=? AND status IN ('awaiting_photo', 'awaiting_amount')
                   ORDER BY updated_at DESC LIMIT 1""",
                (courier_id,),
            ).fetchone()
        return Order.from_row(row) if row else None

    def list_active(self) -> list[Order]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE status IN ('pending', 'picked_up', 'on_way', 'awaiting_photo', 'awaiting_amount')
                   ORDER BY order_number"""
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def count_completed_for_courier_since(self, courier_id: int, since: datetime) -> int:
        """Count completed rows using parsed timestamps instead of string ordering."""
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        with self.connect() as db:
            rows = db.execute(
                """SELECT delivered_at FROM orders
                   WHERE status='completed' AND courier_id=? AND delivered_at IS NOT NULL""",
                (courier_id,),
            ).fetchall()
        count = 0
        for row in rows:
            try:
                delivered = datetime.fromisoformat(row["delivered_at"])
            except (TypeError, ValueError):
                continue
            if delivered.tzinfo is None:
                delivered = delivered.replace(tzinfo=timezone.utc)
            if delivered.astimezone(timezone.utc) >= since.astimezone(timezone.utc):
                count += 1
        return count

    def list_manager_open(self, manager_id: int) -> list[Order]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE manager_id=?
                     AND status IN ('draft', 'pending', 'picked_up', 'on_way', 'awaiting_photo', 'awaiting_amount')
                   ORDER BY order_number DESC LIMIT 20""",
                (manager_id,),
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def list_all(self) -> list[Order]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM orders ORDER BY order_number DESC"
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    @staticmethod
    def _page_bounds(limit: int, offset: int) -> tuple[int, int]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        return limit, offset

    def count_all(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) FROM orders").fetchone()
        return int(row[0])

    def list_all_page(self, *, limit: int = 20, offset: int = 0) -> list[Order]:
        limit, offset = self._page_bounds(limit, offset)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM orders ORDER BY order_number DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def list_open(self) -> list[Order]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE status IN ('draft', 'pending', 'picked_up', 'on_way', 'awaiting_photo', 'awaiting_amount')
                   ORDER BY order_number DESC"""
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def list_orders_channel_reconcile(self, channel_id: int) -> list[Order]:
        """Rows that have never been published to the configured order journal."""
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE orders_channel_chat_id IS NULL
                      OR orders_channel_message_id IS NULL
                      OR orders_channel_chat_id != ?
                   ORDER BY order_number""",
                (channel_id,),
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def count_open(self) -> int:
        with self.connect() as db:
            row = db.execute(
                """SELECT COUNT(*) FROM orders
                   WHERE status IN ('draft', 'pending', 'picked_up', 'on_way', 'awaiting_photo', 'awaiting_amount')"""
            ).fetchone()
        return int(row[0])

    def list_open_page(self, *, limit: int = 20, offset: int = 0) -> list[Order]:
        limit, offset = self._page_bounds(limit, offset)
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE status IN ('draft', 'pending', 'picked_up', 'on_way', 'awaiting_photo', 'awaiting_amount')
                   ORDER BY order_number DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def get_on_way_for_courier(
        self,
        courier_id: int,
        *,
        exclude_order_id: int | None = None,
    ) -> Order | None:
        """Return the courier's canonical current destination, if any."""
        query = (
            "SELECT * FROM orders "
            "WHERE status='on_way' AND COALESCE(courier_id, assigned_courier_id)=?"
        )
        parameters: list[Any] = [courier_id]
        if exclude_order_id is not None:
            query += " AND id!=?"
            parameters.append(exclude_order_id)
        query += " ORDER BY time_started DESC, order_number DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, parameters).fetchone()
        return Order.from_row(row) if row else None

    def update(
        self,
        order_id: int,
        *,
        expected_updated_at: str | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_role: str | None = None,
        **fields: Any,
    ) -> Order | None:
        invalid = set(fields) - self.editable_fields
        if invalid:
            raise ValueError(f"Unsupported fields: {invalid}")
        changed_fields = set(fields)
        if "sync_needed" not in fields:
            fields["sync_needed"] = 1
        if fields.get("sync_needed"):
            fields["sync_attempted_at"] = None
        fields["updated_at"] = now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT order_number, status, updated_at FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if not previous:
                return None
            if expected_updated_at is not None and previous["updated_at"] != expected_updated_at:
                return None
            where = "id=?"
            params: list[Any] = [*fields.values(), order_id]
            if expected_updated_at is not None:
                where += " AND updated_at=?"
                params.append(expected_updated_at)
            cursor = db.execute(f"UPDATE orders SET {assignments} WHERE {where}", params)
            if cursor.rowcount != 1:
                return None
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            self._insert_event(
                db,
                order_id=order_id,
                order_number=row["order_number"],
                event_type="status_changed" if row["status"] != previous["status"] else "order_updated",
                actor_id=actor_id,
                actor_name=actor_name,
                actor_role=actor_role,
                from_status=previous["status"],
                to_status=row["status"],
                changed_fields=changed_fields,
            )
        return Order.from_row(row)

    def transition(
        self,
        order_id: int,
        from_statuses: set[str],
        *,
        guard_courier_id: int | None = None,
        require_unassigned_or_same: bool = False,
        expected_updated_at: str | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_role: str | None = None,
        event_type: str | None = None,
        cleanup_messages: list[tuple[int, int]] | tuple[tuple[int, int], ...] = (),
        **fields: Any,
    ) -> Order | None:
        """Atomically change an order only when its current state still permits it."""
        invalid = set(fields) - self.editable_fields
        if invalid:
            raise ValueError(f"Unsupported fields: {invalid}")
        if not from_statuses:
            raise ValueError("from_statuses cannot be empty")
        cleanups = [
            (int(chat_id), int(message_id))
            for chat_id, message_id in cleanup_messages
            if chat_id and message_id
        ]
        changed_fields = set(fields)
        if "sync_needed" not in fields:
            fields["sync_needed"] = 1
        if fields.get("sync_needed"):
            fields["sync_attempted_at"] = None
        fields["updated_at"] = now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        placeholders = ", ".join("?" for _ in from_statuses)
        where = f"id=? AND status IN ({placeholders})"
        params: list[Any] = [*fields.values(), order_id, *sorted(from_statuses)]
        if require_unassigned_or_same:
            if guard_courier_id is None:
                raise ValueError("guard_courier_id is required")
            where += " AND (courier_id IS NULL OR courier_id=?)"
            params.append(guard_courier_id)
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            params.append(expected_updated_at)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT order_number, status, courier_id, courier_name, updated_at FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if previous and expected_updated_at is not None and previous["updated_at"] != expected_updated_at:
                return None
            cursor = db.execute(f"UPDATE orders SET {assignments} WHERE {where}", params)
            if cursor.rowcount != 1:
                return None
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            event_actor_id = actor_id or fields.get("courier_id") or guard_courier_id
            event_actor_name = actor_name or fields.get("courier_name")
            if event_actor_name is None and previous and event_actor_id == previous["courier_id"]:
                event_actor_name = previous["courier_name"]
            event_actor_role = actor_role or ("courier" if event_actor_id is not None else None)
            self._insert_event(
                db,
                order_id=order_id,
                order_number=row["order_number"],
                event_type=(
                    event_type
                    or ("status_changed" if row["status"] != previous["status"] else "order_updated")
                ),
                actor_id=event_actor_id,
                actor_name=event_actor_name,
                actor_role=event_actor_role,
                from_status=previous["status"],
                to_status=row["status"],
                changed_fields=changed_fields,
            )
            for chat_id, message_id in cleanups:
                db.execute(
                    """INSERT OR IGNORE INTO telegram_cleanup_queue
                       (order_id, chat_id, message_id, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (order_id, chat_id, message_id, now()),
                )
        return Order.from_row(row)

    def list_needing_sync(self, *, limit: int = 100) -> list[Order]:
        limit, _ = self._page_bounds(limit, 0)
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE sync_needed=1
                   ORDER BY COALESCE(sync_attempted_at, ''), updated_at, id
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def mark_synced(
        self,
        order_id: int,
        *,
        expected_updated_at: str | None = None,
    ) -> bool:
        """Clear the sync marker without changing the order version or audit log."""
        where = "id=? AND sync_needed=1"
        params: list[Any] = [order_id]
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            params.append(expected_updated_at)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE orders SET sync_needed=0, sync_attempted_at=NULL WHERE {where}",
                params,
            )
        return cursor.rowcount == 1

    def mark_sync_attempted(
        self,
        order_id: int,
        *,
        expected_updated_at: str | None = None,
    ) -> bool:
        """Move a failed row behind newer work without changing its version."""
        where = "id=? AND sync_needed=1"
        params: list[Any] = [now(), order_id]
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            params.append(expected_updated_at)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE orders SET sync_attempted_at=? WHERE {where}",
                params,
            )
        return cursor.rowcount == 1

    def mark_needs_sync(
        self,
        order_id: int,
        *,
        expected_updated_at: str | None = None,
    ) -> bool:
        """Set the retry marker without changing business data or audit history."""
        where = "id=?"
        params: list[Any] = [order_id]
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            params.append(expected_updated_at)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE orders SET sync_needed=1, sync_attempted_at=NULL WHERE {where}",
                params,
            )
        return cursor.rowcount == 1

    def list_cleanup_messages(
        self,
        *,
        limit: int = 100,
        order_id: int | None = None,
    ) -> list[dict[str, Any]]:
        limit, _ = self._page_bounds(limit, 0)
        where = ""
        params: list[Any] = []
        if order_id is not None:
            where = "WHERE order_id=?"
            params.append(order_id)
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT * FROM telegram_cleanup_queue
                    {where}
                    ORDER BY attempts, id
                    LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue_cleanup_messages(
        self,
        order_id: int,
        messages: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    ) -> int:
        cleanups = [
            (int(chat_id), int(message_id))
            for chat_id, message_id in messages
            if chat_id and message_id
        ]
        if not cleanups:
            return 0
        inserted = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
                return 0
            for chat_id, message_id in cleanups:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO telegram_cleanup_queue
                       (order_id, chat_id, message_id, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (order_id, chat_id, message_id, now()),
                )
                inserted += cursor.rowcount
        return inserted

    def mark_cleanup_done(self, cleanup_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM telegram_cleanup_queue WHERE id=?",
                (cleanup_id,),
            )
        return cursor.rowcount == 1

    def mark_cleanup_failed(self, cleanup_id: int, error: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE telegram_cleanup_queue
                   SET attempts=attempts+1, last_error=?
                   WHERE id=?""",
                (error[:500], cleanup_id),
            )
        return cursor.rowcount == 1

    def add_event(
        self,
        order_id: int,
        event_type: str,
        *,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_role: str | None = None,
        changed_fields: set[str] | list[str] | tuple[str, ...] = (),
    ) -> OrderEvent | None:
        """Append an explicit audit event without changing the order."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute(
                "SELECT order_number, status FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if not order:
                return None
            row = self._insert_event(
                db,
                order_id=order_id,
                order_number=order["order_number"],
                event_type=event_type,
                actor_id=actor_id,
                actor_name=actor_name,
                actor_role=actor_role,
                from_status=order["status"],
                to_status=order["status"],
                changed_fields=changed_fields,
            )
        return OrderEvent.from_row(row)

    def list_events(self, order_id: int) -> list[OrderEvent]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM order_events WHERE order_id=? ORDER BY id",
                (order_id,),
            ).fetchall()
        return [OrderEvent.from_row(row) for row in rows]

    def list_events_between(
        self,
        start: datetime,
        end: datetime,
    ) -> list[OrderEvent]:
        """Return append-only audit events in one timezone-aware interval."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("event interval must be timezone-aware")
        if end <= start:
            raise ValueError("event interval end must be after start")
        start_utc = start.astimezone(timezone.utc).isoformat(timespec="microseconds")
        end_utc = end.astimezone(timezone.utc).isoformat(timespec="microseconds")
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM order_events
                   WHERE created_at>=? AND created_at<?
                   ORDER BY created_at, id""",
                (start_utc, end_utc),
            ).fetchall()
        return [OrderEvent.from_row(row) for row in rows]
