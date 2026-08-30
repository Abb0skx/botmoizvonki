import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.models import Order, OrderEvent

SCHEMA_VERSION = 4
SQLITE_INT_MAX = 2**63 - 1
KNOWN_STATUSES = frozenset({
    "draft",
    "pending",
    "picked_up",
    "on_way",
    "awaiting_photo",
    "awaiting_amount",
    "completed",
    "cancelled",
})
KNOWN_PAYMENT_STATUSES = frozenset({"collect_on_delivery", "paid_at_assembly"})
MONEY_FIELDS = frozenset({"amount_usd", "amount_uzs", "received_usd", "received_uzs"})
COORDINATE_PAIRS = (
    ("latitude", "longitude"),
    ("second_latitude", "second_longitude"),
)
PUBLICATION_FIELDS = frozenset({
    "delivery_chat_id",
    "delivery_message_id",
    "location_chat_id",
    "location_message_id",
    "location_details_message_id",
    "location_footer_message_id",
    "second_location_chat_id",
    "second_location_message_id",
    "second_location_details_message_id",
    "second_location_footer_message_id",
    "manager_chat_id",
    "manager_message_id",
    "orders_channel_chat_id",
    "orders_channel_message_id",
    "sync_needed",
})
CLEANUP_MAX_ATTEMPTS = 8
CLEANUP_RETRY_BASE_SECONDS = 30
CLEANUP_RETRY_MAX_SECONDS = 60 * 60

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
    cancelled_by_id INTEGER,
    cancelled_by_name TEXT,
    cancelled_by_username TEXT,
    cancelled_at TEXT,
    cancelled_from_status TEXT,
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
    actor_username TEXT,
    actor_role TEXT,
    courier_id INTEGER,
    courier_name TEXT,
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
    next_attempt_at TEXT,
    terminal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_cleanup_queue_retry
ON telegram_cleanup_queue(attempts, id);
CREATE TABLE IF NOT EXISTS periodic_job_claims (
    job_name TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK(slot >= 0),
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(job_name, slot)
) WITHOUT ROWID;
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
    "cancelled_by_id": "INTEGER",
    "cancelled_by_name": "TEXT",
    "cancelled_by_username": "TEXT",
    "cancelled_at": "TEXT",
    "cancelled_from_status": "TEXT",
}

ORDER_EVENT_MIGRATION_COLUMNS = {
    "actor_username": "TEXT",
    "courier_id": "INTEGER",
    "courier_name": "TEXT",
}

CLEANUP_MIGRATION_COLUMNS = {
    "next_attempt_at": "TEXT",
    "terminal": "INTEGER NOT NULL DEFAULT 0",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _retry_at(attempts: int) -> str:
    """Return the bounded exponential retry timestamp for one failed cleanup."""
    timestamp = datetime.fromisoformat(now())
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    delay = min(
        CLEANUP_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)),
        CLEANUP_RETRY_MAX_SECONDS,
    )
    return (timestamp.astimezone(timezone.utc) + timedelta(seconds=delay)).isoformat(
        timespec="microseconds"
    )


class OrderRepository:
    editable_fields = {
        "seller_name", "payment_status", "product", "client_phone", "client_phone_2", "amount_usd", "amount_uzs", "location_url",
        "latitude", "longitude", "address_text", "district", "mahalla",
        "second_location_url", "second_latitude", "second_longitude",
        "second_address_text", "second_district", "second_mahalla",
        "delivery_time", "comment", "status",
        "cancelled_by_id", "cancelled_by_name", "cancelled_by_username",
        "cancelled_at", "cancelled_from_status",
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

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self._lock = Lock()

    def connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _add_column_if_missing(
        db: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """Add a migration column safely when two app instances start together."""
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            return
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            # A concurrent initializer may have added it after our first PRAGMA.
            current = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in current:
                raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            existing = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
            for column, definition in MIGRATION_COLUMNS.items():
                if column not in existing:
                    self._add_column_if_missing(db, "orders", column, definition)
            event_existing = {
                row[1] for row in db.execute("PRAGMA table_info(order_events)")
            }
            for column, definition in ORDER_EVENT_MIGRATION_COLUMNS.items():
                if column not in event_existing:
                    self._add_column_if_missing(
                        db,
                        "order_events",
                        column,
                        definition,
                    )
            cleanup_existing = {
                row[1] for row in db.execute("PRAGMA table_info(telegram_cleanup_queue)")
            }
            for column, definition in CLEANUP_MIGRATION_COLUMNS.items():
                if column not in cleanup_existing:
                    self._add_column_if_missing(
                        db,
                        "telegram_cleanup_queue",
                        column,
                        definition,
                    )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_sync_retry "
                "ON orders(sync_needed, sync_attempted_at, updated_at, id)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_creation_token "
                "ON orders(creation_token) WHERE creation_token IS NOT NULL"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_created_at "
                "ON orders(created_at, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_delivered_at "
                "ON orders(delivered_at, id) WHERE delivered_at IS NOT NULL"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_effective_courier_status "
                "ON orders(status, COALESCE(courier_id, assigned_courier_id), time_started, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_events_order_created "
                "ON order_events(order_id, created_at, id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cleanup_queue_due "
                "ON telegram_cleanup_queue(terminal, next_attempt_at, attempts, id)"
            )
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """UPDATE counters
                   SET value=MAX(value, (SELECT COALESCE(MAX(order_number), 0) FROM orders))
                   WHERE name='order_number'"""
            )
            current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if current_version < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def claim_periodic_job(self, job_name: str, slot: int) -> bool:
        """Atomically claim one wall-clock slot across all bot processes."""
        clean_name = job_name.strip() if isinstance(job_name, str) else ""
        if not clean_name:
            raise ValueError("job_name cannot be empty")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError("slot must be a non-negative integer")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """INSERT INTO periodic_job_claims(job_name, slot, claimed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(job_name, slot) DO NOTHING""",
                (clean_name, slot, now()),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _validate_money(field: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer or null")
        if value < 0:
            raise ValueError(f"{field} cannot be negative")
        if value > SQLITE_INT_MAX:
            raise ValueError(f"{field} exceeds the SQLite integer range")

    @classmethod
    def _validate_domain_fields(
        cls,
        fields: dict[str, Any],
        *,
        current: sqlite3.Row | dict[str, Any] | None = None,
    ) -> None:
        """Validate writes without rejecting untouched values in legacy rows."""
        if "status" in fields and fields["status"] not in KNOWN_STATUSES:
            raise ValueError(f"Unknown order status: {fields['status']!r}")
        if (
            "cancelled_from_status" in fields
            and fields["cancelled_from_status"] is not None
            and fields["cancelled_from_status"] not in KNOWN_STATUSES
        ):
            raise ValueError(
                "Unknown cancellation source status: "
                f"{fields['cancelled_from_status']!r}"
            )
        if (
            "payment_status" in fields
            and fields["payment_status"] not in KNOWN_PAYMENT_STATUSES
        ):
            raise ValueError(f"Unknown payment status: {fields['payment_status']!r}")
        for field in MONEY_FIELDS & fields.keys():
            cls._validate_money(field, fields[field])

        def resulting_value(field: str) -> Any:
            if field in fields:
                return fields[field]
            if current is None:
                return None
            return current[field]

        for latitude_field, longitude_field in COORDINATE_PAIRS:
            if latitude_field not in fields and longitude_field not in fields:
                continue
            latitude = resulting_value(latitude_field)
            longitude = resulting_value(longitude_field)
            if (latitude is None) != (longitude is None):
                raise ValueError(
                    f"{latitude_field} and {longitude_field} must be set or cleared together"
                )
            if latitude is None:
                continue
            for field, value, minimum, maximum in (
                (latitude_field, latitude, -90.0, 90.0),
                (longitude_field, longitude, -180.0, 180.0),
            ):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not minimum <= float(value) <= maximum
                ):
                    raise ValueError(
                        f"{field} must be a finite coordinate between {minimum:g} and {maximum:g}"
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
        actor_username: str | None = None,
        actor_role: str | None = None,
        courier_id: int | None = None,
        courier_name: str | None = None,
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
               (order_id, order_number, event_type, actor_id, actor_name, actor_username, actor_role,
                courier_id, courier_name, from_status, to_status, changed_fields, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id,
                order_number,
                event_type,
                actor_id,
                actor_name,
                actor_username,
                actor_role,
                courier_id,
                courier_name,
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
            creation_fields = dict(data)
            creation_fields.setdefault("status", "draft")
            creation_fields.setdefault("payment_status", "collect_on_delivery")
            self._validate_domain_fields(creation_fields)
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

    @staticmethod
    def _event_courier_snapshot(
        row: sqlite3.Row,
        *,
        event_courier_id: int | None,
        event_courier_name: str | None,
        actor_id: int | None,
        actor_name: str | None,
        actor_role: str | None,
    ) -> tuple[int | None, str | None]:
        """Keep the business courier separate from the event's human actor."""
        courier_id = (
            event_courier_id
            if event_courier_id is not None
            else (row["courier_id"] or row["assigned_courier_id"])
        )
        courier_name = event_courier_name
        if courier_name is None and courier_id is not None:
            if row["courier_id"] is not None and courier_id == row["courier_id"]:
                courier_name = row["courier_name"]
            elif (
                row["assigned_courier_id"] is not None
                and courier_id == row["assigned_courier_id"]
            ):
                courier_name = row["assigned_courier_name"]
        if actor_role == "courier" and actor_id is not None:
            if courier_id is None:
                courier_id = actor_id
            if courier_name is None and courier_id == actor_id:
                courier_name = actor_name
        return courier_id, courier_name

    def update(
        self,
        order_id: int,
        *,
        expected_updated_at: str | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_username: str | None = None,
        actor_role: str | None = None,
        event_courier_id: int | None = None,
        event_courier_name: str | None = None,
        **fields: Any,
    ) -> Order | None:
        invalid = set(fields) - self.editable_fields
        if invalid:
            raise ValueError(f"Unsupported fields: {invalid}")
        changed_fields = set(fields)
        publication_only = bool(changed_fields) and changed_fields <= PUBLICATION_FIELDS
        if "sync_needed" not in fields:
            fields["sync_needed"] = 1
        if fields.get("sync_needed"):
            fields["sync_attempted_at"] = None
        if not publication_only:
            fields["updated_at"] = now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if not previous:
                return None
            if expected_updated_at is not None and previous["updated_at"] != expected_updated_at:
                return None
            self._validate_domain_fields(fields, current=previous)
            where = "id=?"
            params: list[Any] = [*fields.values(), order_id]
            if expected_updated_at is not None:
                where += " AND updated_at=?"
                params.append(expected_updated_at)
            cursor = db.execute(f"UPDATE orders SET {assignments} WHERE {where}", params)
            if cursor.rowcount != 1:
                return None
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not publication_only:
                effective_courier_id, effective_courier_name = (
                    self._event_courier_snapshot(
                        row,
                        event_courier_id=event_courier_id,
                        event_courier_name=event_courier_name,
                        actor_id=actor_id,
                        actor_name=actor_name,
                        actor_role=actor_role,
                    )
                )
                self._insert_event(
                    db,
                    order_id=order_id,
                    order_number=row["order_number"],
                    event_type=(
                        "status_changed"
                        if row["status"] != previous["status"]
                        else "order_updated"
                    ),
                    actor_id=actor_id,
                    actor_name=actor_name,
                    actor_username=actor_username,
                    actor_role=actor_role,
                    courier_id=effective_courier_id,
                    courier_name=effective_courier_name,
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
        require_assigned_to_courier: bool = False,
        require_no_other_on_way_for_courier: bool = False,
        expected_updated_at: str | None = None,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_username: str | None = None,
        actor_role: str | None = None,
        event_courier_id: int | None = None,
        event_courier_name: str | None = None,
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
        if require_unassigned_or_same and require_assigned_to_courier:
            raise ValueError(
                "require_unassigned_or_same and require_assigned_to_courier "
                "are mutually exclusive"
            )
        if (
            require_unassigned_or_same
            or require_assigned_to_courier
            or require_no_other_on_way_for_courier
        ) and guard_courier_id is None:
            raise ValueError("guard_courier_id is required")
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
            # Authorization belongs in the same transaction as the status
            # update. A manager can reassign a pending order between a
            # courier's button click and this UPDATE; guarding only
            # ``courier_id`` would then let the old courier mutate the newly
            # assigned courier's order while courier_id is still NULL.
            where += (
                " AND (assigned_courier_id IS NULL OR assigned_courier_id=?)"
                " AND (courier_id IS NULL OR courier_id=?)"
            )
            params.extend((guard_courier_id, guard_courier_id))
        elif require_assigned_to_courier:
            # Starting a trip is allowed only for the courier who is still
            # assigned at the instant of the UPDATE. Unlike the more general
            # guard above, an unassigned row cannot be claimed from a stale
            # courier-group button.
            where += (
                " AND assigned_courier_id=?"
                " AND (courier_id IS NULL OR courier_id=?)"
            )
            params.extend((guard_courier_id, guard_courier_id))
        if require_no_other_on_way_for_courier:
            where += (
                " AND NOT EXISTS ("
                "SELECT 1 FROM orders AS active_on_way "
                "WHERE active_on_way.id != orders.id "
                "AND active_on_way.status='on_way' "
                "AND COALESCE(active_on_way.courier_id, "
                "active_on_way.assigned_courier_id)=?"
                ")"
            )
            params.append(guard_courier_id)
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            params.append(expected_updated_at)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if not previous:
                return None
            if expected_updated_at is not None and previous["updated_at"] != expected_updated_at:
                return None
            self._validate_domain_fields(fields, current=previous)
            if require_unassigned_or_same or require_assigned_to_courier:
                for courier_field in ("assigned_courier_id", "courier_id"):
                    # Validate only values this call is trying to write. An
                    # already-reassigned row is a normal stale-button race;
                    # the atomic WHERE guard below must return ``None`` rather
                    # than turn it into an application error.
                    if (
                        courier_field in fields
                        and fields[courier_field] not in {None, guard_courier_id}
                    ):
                        raise ValueError(
                            f"{courier_field} must match guard_courier_id"
                        )
            if require_no_other_on_way_for_courier:
                resulting_status = fields.get("status", previous["status"])
                if resulting_status != "on_way":
                    raise ValueError(
                        "require_no_other_on_way_for_courier requires status='on_way'"
                    )
                # Starting a trip must explicitly claim the order for the
                # guarded courier; merely inheriting assigned_courier_id is
                # not enough for an authorization-sensitive transition.
                if fields.get("courier_id") != guard_courier_id:
                    raise ValueError(
                        "guard_courier_id must match the order's effective courier"
                    )
            cursor = db.execute(f"UPDATE orders SET {assignments} WHERE {where}", params)
            if cursor.rowcount != 1:
                return None
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            event_actor_id = actor_id or fields.get("courier_id") or guard_courier_id
            event_actor_name = actor_name or fields.get("courier_name")
            if event_actor_name is None and previous and event_actor_id == previous["courier_id"]:
                event_actor_name = previous["courier_name"]
            event_actor_role = actor_role or ("courier" if event_actor_id is not None else None)
            effective_courier_id, effective_courier_name = (
                self._event_courier_snapshot(
                    row,
                    event_courier_id=event_courier_id,
                    event_courier_name=event_courier_name,
                    actor_id=event_actor_id,
                    actor_name=event_actor_name,
                    actor_role=event_actor_role,
                )
            )
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
                actor_username=actor_username,
                actor_role=event_actor_role,
                courier_id=effective_courier_id,
                courier_name=effective_courier_name,
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

    def operational_counts(self) -> dict[str, int]:
        """Return small queue-health counters without loading queue rows."""
        with self.connect() as db:
            row = db.execute(
                """SELECT
                       (SELECT COUNT(*) FROM orders WHERE sync_needed=1) AS sync_pending,
                       (SELECT COUNT(*) FROM telegram_cleanup_queue WHERE terminal=0)
                           AS cleanup_pending,
                       (SELECT COUNT(*) FROM telegram_cleanup_queue WHERE terminal=1)
                           AS cleanup_terminal"""
            ).fetchone()
        return {
            "sync_pending": int(row["sync_pending"]),
            "cleanup_pending": int(row["cleanup_pending"]),
            "cleanup_terminal": int(row["cleanup_terminal"]),
        }

    def list_cleanup_messages(
        self,
        *,
        limit: int = 100,
        order_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return only non-terminal cleanup work whose retry time has arrived."""
        limit, _ = self._page_bounds(limit, 0)
        clauses = ["terminal=0", "(next_attempt_at IS NULL OR next_attempt_at<=?)"]
        params: list[Any] = [now()]
        if order_id is not None:
            clauses.append("order_id=?")
            params.append(order_id)
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT * FROM telegram_cleanup_queue
                    WHERE {' AND '.join(clauses)}
                    ORDER BY COALESCE(next_attempt_at, ''), attempts, id
                    LIMIT ?""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_terminal_cleanup_messages(
        self,
        *,
        limit: int = 100,
        order_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return dead-letter cleanup work for diagnostics or manual requeue."""
        limit, _ = self._page_bounds(limit, 0)
        clauses = ["terminal=1"]
        params: list[Any] = []
        if order_id is not None:
            clauses.append("order_id=?")
            params.append(order_id)
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT * FROM telegram_cleanup_queue
                    WHERE {' AND '.join(clauses)}
                    ORDER BY attempts DESC, id
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

    def mark_cleanup_failed(
        self,
        cleanup_id: int,
        error: str,
        *,
        permanent: bool = False,
    ) -> bool:
        """Back off transient cleanup failures and dead-letter permanent ones."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT attempts, terminal FROM telegram_cleanup_queue WHERE id=?",
                (cleanup_id,),
            ).fetchone()
            if not row or row["terminal"]:
                return False
            attempts = int(row["attempts"]) + 1
            terminal = int(permanent or attempts >= CLEANUP_MAX_ATTEMPTS)
            next_attempt_at = None if terminal else _retry_at(attempts)
            cursor = db.execute(
                """UPDATE telegram_cleanup_queue
                   SET attempts=?, last_error=?, next_attempt_at=?, terminal=?
                   WHERE id=? AND terminal=0""",
                (
                    attempts,
                    str(error)[:500],
                    next_attempt_at,
                    terminal,
                    cleanup_id,
                ),
            )
        return cursor.rowcount == 1

    def requeue_cleanup_message(self, cleanup_id: int) -> bool:
        """Explicitly return a dead-letter cleanup item to the runnable queue."""
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE telegram_cleanup_queue
                   SET attempts=0, last_error=NULL, next_attempt_at=NULL, terminal=0
                   WHERE id=? AND terminal=1""",
                (cleanup_id,),
            )
        return cursor.rowcount == 1

    def add_event(
        self,
        order_id: int,
        event_type: str,
        *,
        actor_id: int | None = None,
        actor_name: str | None = None,
        actor_username: str | None = None,
        actor_role: str | None = None,
        event_courier_id: int | None = None,
        event_courier_name: str | None = None,
        changed_fields: set[str] | list[str] | tuple[str, ...] = (),
    ) -> OrderEvent | None:
        """Append an explicit audit event without changing the order."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute(
                """SELECT order_number, status, courier_id, courier_name,
                          assigned_courier_id, assigned_courier_name
                   FROM orders WHERE id=?""",
                (order_id,),
            ).fetchone()
            if not order:
                return None
            effective_courier_id, effective_courier_name = (
                self._event_courier_snapshot(
                    order,
                    event_courier_id=event_courier_id,
                    event_courier_name=event_courier_name,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    actor_role=actor_role,
                )
            )
            row = self._insert_event(
                db,
                order_id=order_id,
                order_number=order["order_number"],
                event_type=event_type,
                actor_id=actor_id,
                actor_name=actor_name,
                actor_username=actor_username,
                actor_role=actor_role,
                courier_id=effective_courier_id,
                courier_name=effective_courier_name,
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

    def list_all_events(self) -> list[OrderEvent]:
        """Return the append-only audit stream without per-order N+1 reads."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM order_events ORDER BY created_at, id"
            ).fetchall()
        return [OrderEvent.from_row(row) for row in rows]

    def list_orders_with_events(self) -> tuple[list[Order], list[OrderEvent]]:
        """Read orders and their audit stream from one consistent SQLite snapshot."""
        with self.connect() as db:
            db.execute("BEGIN")
            order_rows = db.execute(
                "SELECT * FROM orders ORDER BY order_number DESC"
            ).fetchall()
            event_rows = db.execute(
                "SELECT * FROM order_events ORDER BY created_at, id"
            ).fetchall()
        return (
            [Order.from_row(row) for row in order_rows],
            [OrderEvent.from_row(row) for row in event_rows],
        )

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
