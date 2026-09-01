from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

from telegram import MessageEntity

from .phones import normalize_uzbek_phone


TASHKENT = ZoneInfo("Asia/Tashkent")
STATUS_LABELS = {
    "draft": "📝 Черновик",
    "pending": "🆕 Ожидает курьера",
    "picked_up": "📦 Товар у курьера",
    "on_way": "🚗 Курьер едет",
    "awaiting_photo": "📸 Подтверждается",
    "awaiting_amount": "💰 Ожидается сумма",
    "completed": "✅ Доставлено",
    "cancelled": "❌ Отменено",
}
_DELIVERY_BLOCK_RE = re.compile(
    r"(?:\r?\n){2}Доставка:[^\r\n]*(?:\r?\n)Статус:[^\r\n]*[ \t]*$"
)


def _local_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TASHKENT)
    return parsed.astimezone(TASHKENT)


def _phone_key(value: object) -> str | None:
    normalized = normalize_uzbek_phone(value)
    if normalized is None:
        return None
    return "".join(character for character in normalized if character.isdigit())


def _single_line(value: object) -> str | None:
    normalized = " ".join(str(value or "").split())
    return normalized or None


@dataclass(frozen=True)
class DeliveryOrder:
    id: int
    order_number: int
    phones: frozenset[str]
    created_date: date
    manager_name: str | None
    courier_name: str | None
    status: str
    delivered_at: datetime | None
    updated_at: str | None = None

    @property
    def status_text(self) -> str:
        label = STATUS_LABELS.get(self.status, self.status.strip() or "—")
        if self.status == "completed" and self.delivered_at is not None:
            return f"{label} ({self.delivered_at:%H:%M})"
        return label


@dataclass(frozen=True)
class DeliveryEvent:
    id: int
    order_id: int
    event_type: str
    changed_fields: frozenset[str]


@dataclass(frozen=True)
class DeliveryIndex:
    orders: tuple[DeliveryOrder, ...]

    def match(
        self,
        phones: Iterable[str],
        sale_date: date,
    ) -> DeliveryOrder | None:
        keys = frozenset(
            key for value in phones if (key := _phone_key(value)) is not None
        )
        if not keys:
            return None
        matched = {
            order.id: order
            for order in self.orders
            if order.created_date == sale_date and order.phones.intersection(keys)
        }
        return next(iter(matched.values())) if len(matched) == 1 else None

    def match_count(self, phones: Iterable[str], sale_date: date) -> int:
        keys = frozenset(
            key for value in phones if (key := _phone_key(value)) is not None
        )
        return len(
            {
                order.id
                for order in self.orders
                if order.created_date == sale_date
                and order.phones.intersection(keys)
            }
        )


class DeliveryReader:
    """Read the live delivery SQLite database without write permissions."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def validate(self) -> None:
        with self._connect() as db:
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if not {"orders", "order_events"}.issubset(tables):
            raise RuntimeError("delivery database schema is incomplete")

    @staticmethod
    def _order(row: sqlite3.Row) -> DeliveryOrder | None:
        created_at = _local_datetime(row["created_at"])
        if created_at is None:
            return None
        phones = frozenset(
            key
            for value in (row["client_phone"], row["client_phone_2"])
            if (key := _phone_key(value)) is not None
        )
        return DeliveryOrder(
            id=int(row["id"]),
            order_number=int(row["order_number"]),
            phones=phones,
            created_date=created_at.date(),
            manager_name=_single_line(row["manager_name"]),
            courier_name=_single_line(
                row["courier_name"] or row["assigned_courier_name"]
            ),
            status=_single_line(row["status"]) or "",
            delivered_at=_local_datetime(row["delivered_at"]),
            updated_at=str(row["updated_at"] or "") or None,
        )

    def index(self, date_from: date, date_to: date) -> DeliveryIndex:
        # ISO timestamps in the current database are UTC, while legacy rows can
        # carry local offsets. Filtering the bounded recent tail in Python keeps
        # date comparison correct for both representations.
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,order_number,client_phone,client_phone_2,
                          manager_name,assigned_courier_name,courier_name,
                          status,created_at,updated_at,delivered_at
                   FROM orders ORDER BY id DESC LIMIT 10000"""
            ).fetchall()
        orders = []
        for row in rows:
            order = self._order(row)
            if order is not None and date_from <= order.created_date <= date_to:
                orders.append(order)
        return DeliveryIndex(tuple(orders))

    def orders_by_ids(self, order_ids: Iterable[int]) -> dict[int, DeliveryOrder]:
        values = tuple(sorted({int(value) for value in order_ids}))
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT id,order_number,client_phone,client_phone_2,
                           manager_name,assigned_courier_name,courier_name,
                           status,created_at,updated_at,delivered_at
                    FROM orders WHERE id IN ({placeholders})""",
                values,
            ).fetchall()
        result = {}
        for row in rows:
            order = self._order(row)
            if order is not None:
                result[order.id] = order
        return result

    def latest_event_id(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(MAX(id),0) FROM order_events").fetchone()
        return int(row[0])

    def events_after(self, event_id: int, limit: int = 500) -> tuple[DeliveryEvent, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,order_id,event_type,changed_fields
                   FROM order_events WHERE id>? ORDER BY id LIMIT ?""",
                (int(event_id), max(1, min(int(limit), 2000))),
            ).fetchall()
        result = []
        for row in rows:
            try:
                fields = json.loads(str(row["changed_fields"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                fields = []
            result.append(
                DeliveryEvent(
                    id=int(row["id"]),
                    order_id=int(row["order_id"]),
                    event_type=str(row["event_type"] or ""),
                    changed_fields=frozenset(str(value) for value in fields),
                )
            )
        return tuple(result)


@dataclass(frozen=True)
class DeliveryBlockNormalization:
    body: str
    entities: tuple[MessageEntity, ...]
    changed: bool = False


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def normalize_delivery_block(
    body: object,
    entities: Sequence[MessageEntity] | None,
    order: DeliveryOrder | None,
    *,
    max_length: int,
) -> DeliveryBlockNormalization:
    original = str(body or "")
    original_entities = tuple(entities or ())
    match = _DELIVERY_BLOCK_RE.search(original)
    if match is None and order is None:
        return DeliveryBlockNormalization(original, original_entities)
    start = match.start() if match else len(original)
    end = match.end() if match else len(original)
    base = original[:start].rstrip()
    replacement = ""
    if order is not None:
        replacement = (
            f"\n\nДоставка: {order.courier_name or ''}\n"
            f"Статус: {order.status_text}"
        )
    normalized = base + replacement
    if normalized == original:
        return DeliveryBlockNormalization(original, original_entities)
    if _utf16_length(normalized) > max(1, int(max_length)):
        return DeliveryBlockNormalization(original, original_entities)

    start_utf16 = _utf16_length(original[:start])
    end_utf16 = start_utf16 + _utf16_length(original[start:end])
    delta = _utf16_length(replacement) - (end_utf16 - start_utf16)
    shifted: list[MessageEntity] = []
    for entity in original_entities:
        entity_start = int(entity.offset)
        entity_end = entity_start + int(entity.length)
        if entity_end <= start_utf16:
            shifted.append(entity)
        elif entity_start >= end_utf16:
            shifted.extend(MessageEntity.shift_entities(delta, (entity,)))
        elif entity_start >= start_utf16 and entity_end <= end_utf16:
            continue
        else:
            return DeliveryBlockNormalization(original, original_entities)
    return DeliveryBlockNormalization(normalized, tuple(shifted), True)
