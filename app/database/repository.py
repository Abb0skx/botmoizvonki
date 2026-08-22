import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.models import Order

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number INTEGER NOT NULL UNIQUE,
    manager_id INTEGER NOT NULL,
    manager_name TEXT NOT NULL,
    seller_name TEXT,
    payment_status TEXT NOT NULL DEFAULT 'collect_on_delivery',
    client_phone TEXT NOT NULL,
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
    courier_id INTEGER,
    courier_name TEXT,
    delivery_photo TEXT,
    received_usd INTEGER,
    received_uzs INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    time_started TEXT,
    delivery_chat_id INTEGER,
    delivery_message_id INTEGER,
    location_chat_id INTEGER,
    location_message_id INTEGER,
    second_location_chat_id INTEGER,
    second_location_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_manager ON orders(manager_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_courier ON orders(courier_id, delivered_at);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO counters(name, value) VALUES ('order_number', 0);
"""

MIGRATION_COLUMNS = {
    "seller_name": "TEXT",
    "payment_status": "TEXT NOT NULL DEFAULT 'collect_on_delivery'",
    "address_text": "TEXT",
    "district": "TEXT",
    "mahalla": "TEXT",
    "location_chat_id": "INTEGER",
    "location_message_id": "INTEGER",
    "second_location_url": "TEXT",
    "second_latitude": "REAL",
    "second_longitude": "REAL",
    "second_address_text": "TEXT",
    "second_district": "TEXT",
    "second_mahalla": "TEXT",
    "second_location_chat_id": "INTEGER",
    "second_location_message_id": "INTEGER",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrderRepository:
    editable_fields = {
        "seller_name", "payment_status", "product", "client_phone", "amount_usd", "amount_uzs", "location_url",
        "latitude", "longitude", "address_text", "district", "mahalla",
        "second_location_url", "second_latitude", "second_longitude",
        "second_address_text", "second_district", "second_mahalla",
        "delivery_time", "comment", "status",
        "courier_id", "courier_name", "delivery_photo", "received_usd",
        "received_uzs", "delivered_at", "time_started", "delivery_chat_id",
        "delivery_message_id", "location_chat_id", "location_message_id",
        "second_location_chat_id", "second_location_message_id",
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
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """UPDATE counters
                   SET value=MAX(value, (SELECT COALESCE(MAX(order_number), 0) FROM orders))
                   WHERE name='order_number'"""
            )

    def create(self, *, manager_id: int, manager_name: str, data: dict[str, Any]) -> Order:
        timestamp = now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE counters SET value=value+1 WHERE name='order_number'")
            number = db.execute("SELECT value FROM counters WHERE name='order_number'").fetchone()[0]
            cursor = db.execute(
                """INSERT INTO orders
                (order_number, manager_id, manager_name, seller_name, payment_status, client_phone, product,
                 amount_usd, amount_uzs, location_url, latitude, longitude,
                 address_text, district, mahalla,
                 second_location_url, second_latitude, second_longitude,
                 second_address_text, second_district, second_mahalla,
                 delivery_time, comment,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (number, manager_id, manager_name, data.get("seller_name"),
                 data.get("payment_status", "collect_on_delivery"),
                 data["client_phone"], data["product"],
                 data.get("amount_usd"), data.get("amount_uzs"), data.get("location_url"),
                 data.get("latitude"), data.get("longitude"), data.get("address_text"),
                 data.get("district"), data.get("mahalla"), data.get("second_location_url"),
                 data.get("second_latitude"), data.get("second_longitude"),
                 data.get("second_address_text"), data.get("second_district"),
                 data.get("second_mahalla"), data.get("delivery_time"),
                 data.get("comment"), timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone()
        return Order.from_row(row)

    def get(self, order_id: int) -> Order | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
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
                   WHERE status IN ('pending', 'on_way', 'awaiting_photo', 'awaiting_amount')
                   ORDER BY order_number"""
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def list_manager_open(self, manager_id: int) -> list[Order]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE manager_id=?
                     AND status IN ('draft', 'pending', 'on_way', 'awaiting_photo', 'awaiting_amount')
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

    def list_open(self) -> list[Order]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM orders
                   WHERE status IN ('draft', 'pending', 'on_way', 'awaiting_photo', 'awaiting_amount')
                   ORDER BY order_number DESC"""
            ).fetchall()
        return [Order.from_row(row) for row in rows]

    def update(self, order_id: int, **fields: Any) -> Order | None:
        invalid = set(fields) - self.editable_fields
        if invalid:
            raise ValueError(f"Unsupported fields: {invalid}")
        fields["updated_at"] = now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE orders SET {assignments} WHERE id=?", (*fields.values(), order_id))
        return self.get(order_id)

    def transition(
        self,
        order_id: int,
        from_statuses: set[str],
        *,
        guard_courier_id: int | None = None,
        require_unassigned_or_same: bool = False,
        **fields: Any,
    ) -> Order | None:
        """Atomically change an order only when its current state still permits it."""
        invalid = set(fields) - self.editable_fields
        if invalid:
            raise ValueError(f"Unsupported fields: {invalid}")
        if not from_statuses:
            raise ValueError("from_statuses cannot be empty")
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
        with self.connect() as db:
            cursor = db.execute(f"UPDATE orders SET {assignments} WHERE {where}", params)
            if cursor.rowcount != 1:
                return None
        return self.get(order_id)
