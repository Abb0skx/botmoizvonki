from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import re
from typing import Any

from app.database import OrderRepository
from app.models import Order
from app.stats_service import (
    ACTIVE_STATUSES,
    COURIER_COLORS,
    COURIER_DARK_COLORS,
    TASHKENT,
    _courier_id,
    _courier_name,
    _order_map_url,
    build_delivery_stats,
    local_datetime,
)
from app.utils.couriers import COURIERS
from app.utils.formatters import short_address
from app.utils.parsers import display_phone
from app.utils.sellers import SELLERS


WAREHOUSE = {
    "name": "Склад · Малика",
    "latitude": 41.337420,
    "longitude": 69.272104,
}
UNASSIGNED_COLOR = "#64748b"
UNASSIGNED_DARK_COLOR = "#334155"
LIVE_STATUSES = {
    "draft", "pending", "picked_up", "on_way", "awaiting_photo", "awaiting_amount",
}
STATUS_PRIORITY = {
    "on_way": 0,
    "awaiting_photo": 1,
    "awaiting_amount": 1,
    "picked_up": 2,
    "pending": 3,
    "draft": 4,
}
_CLOCK_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_HOURS_RE = re.compile(r"(?<!\d)(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*час", re.I)


def _timestamp(value: str | None) -> float:
    parsed = local_datetime(value)
    return parsed.timestamp() if parsed else 0.0


def _time(value: str | None) -> str | None:
    parsed = local_datetime(value)
    return parsed.strftime("%H:%M") if parsed else None


def _minutes_since(value: str | None) -> int | None:
    parsed = local_datetime(value)
    if not parsed:
        return None
    elapsed = datetime.now(TASHKENT) - parsed
    if elapsed.total_seconds() < 0:
        return 0
    return round(elapsed.total_seconds() / 60)


def _delivery_deadline(order: Order) -> tuple[datetime | None, bool]:
    """Best-effort structured deadline derived from the existing free-text field.

    Managers can still enter arbitrary text. Exact clock values and the supplied
    relative presets are safe to interpret; everything else remains display-only.
    """
    raw = (order.delivery_time or "").strip().casefold()
    created = local_datetime(order.created_at)
    urgent = "сроч" in raw
    if not raw:
        return None, False
    clock = _CLOCK_RE.search(raw)
    if clock:
        base = created or datetime.now(TASHKENT)
        return base.replace(
            hour=int(clock.group(1)),
            minute=int(clock.group(2)),
            second=0,
            microsecond=0,
        ), urgent
    hours = _HOURS_RE.search(raw)
    if hours and created:
        # A range such as 2–3 hours is due at the end of the promised window.
        amount = int(hours.group(2) or hours.group(1))
        return created + timedelta(hours=amount), urgent
    if urgent and created:
        # "Urgent" has no exact contractual time. A 60-minute operational
        # target makes it visible without claiming an exact customer promise.
        return created + timedelta(hours=1), True
    return None, urgent


def _attention(order: Order) -> str | None:
    deadline, urgent = _delivery_deadline(order)
    if deadline:
        overdue = datetime.now(TASHKENT) - deadline
        if overdue.total_seconds() >= 0:
            return f"Просрочено на {max(1, round(overdue.total_seconds() / 60))} мин"
    if order.status == "draft":
        minutes = _minutes_since(order.created_at)
        if minutes is not None and minutes >= 10:
            return f"Не отправлен курьеру {minutes} мин"
    if order.status == "pending":
        minutes = _minutes_since(order.created_at)
        if minutes is not None and minutes >= 30:
            return f"Ждёт забора товара {minutes} мин"
    elif order.status == "picked_up":
        minutes = _minutes_since(order.picked_up_at)
        if minutes is not None and minutes >= 20:
            return f"Товар забран, выезд не начат {minutes} мин"
    elif order.status in {"on_way", "awaiting_photo", "awaiting_amount"}:
        minutes = _minutes_since(order.time_started)
        if minutes is not None and minutes >= 90:
            return f"В пути {minutes} мин"
    if urgent:
        return "Срочный заказ"
    return None


def _active_order_sort_key(order: Order) -> tuple[Any, ...]:
    deadline, urgent = _delivery_deadline(order)
    now_timestamp = datetime.now(TASHKENT).timestamp()
    deadline_timestamp = deadline.timestamp() if deadline else float("inf")
    return (
        STATUS_PRIORITY.get(order.status, 9),
        0 if deadline and deadline.timestamp() <= now_timestamp else 1,
        0 if urgent else 1,
        deadline_timestamp,
        _timestamp(order.time_started)
        or _timestamp(order.picked_up_at)
        or _timestamp(order.created_at),
        order.order_number,
    )


def _locations(order: Order) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for number, latitude, longitude in (
        (1, order.latitude, order.longitude),
        (2, order.second_latitude, order.second_longitude),
    ):
        if latitude is None or longitude is None:
            continue
        result.append({
            "location_number": number,
            "latitude": latitude,
            "longitude": longitude,
            "address": short_address(order, number),
            "map_url": _order_map_url(order, number),
        })
    return result


def _unmapped_locations(order: Order) -> list[dict[str, Any]]:
    """Return every text/link location that has no coordinate map marker."""
    result: list[dict[str, Any]] = []
    for number, latitude, longitude, address, url in (
        (1, order.latitude, order.longitude, order.address_text, order.location_url),
        (
            2,
            order.second_latitude,
            order.second_longitude,
            order.second_address_text,
            order.second_location_url,
        ),
    ):
        if latitude is not None and longitude is not None:
            continue
        if not (address or url):
            continue
        result.append({
            "location_number": number,
            "address": short_address(order, number),
            "map_url": _order_map_url(order, number),
        })
    return result


def _order_payload(order: Order) -> dict[str, Any]:
    phones = [display_phone(order.client_phone)]
    if order.client_phone_2 and order.client_phone_2 != order.client_phone:
        phones.append(display_phone(order.client_phone_2))
    deadline, urgent = _delivery_deadline(order)
    return {
        "id": order.id,
        "order_number": order.order_number,
        "product": order.product,
        "seller_name": order.seller_name or "—",
        "manager_name": order.manager_name or "—",
        "courier_id": _courier_id(order),
        "courier_name": _courier_name(order),
        "status": order.status,
        "phones": phones,
        "address": short_address(order),
        "map_url": _order_map_url(order),
        "delivery_time": order.delivery_time,
        "comment": order.comment,
        "amount_usd": order.amount_usd or 0,
        "amount_uzs": order.amount_uzs or 0,
        "payment_status": order.payment_status,
        "urgent": urgent,
        "deadline_at": deadline.isoformat(timespec="seconds") if deadline else None,
        "overdue": bool(deadline and datetime.now(TASHKENT) >= deadline),
        "created_time": _time(order.created_at),
        # Kept in the API for historical audit compatibility only. The live
        # monitor must not infer courier movement or an operational state from
        # a legacy read confirmation.
        "read_time": _time(order.courier_read_at),
        "read_at": order.courier_read_at,
        "picked_up_time": _time(order.picked_up_at),
        "started_time": _time(order.time_started),
        "completed_time": _time(order.delivered_at),
        "attention": _attention(order),
    }


def _midpoint(points: list[list[float]]) -> dict[str, float] | None:
    if len(points) < 2:
        return None
    start, finish = points[0], points[1]
    return {
        "latitude": (start[0] + finish[0]) / 2,
        "longitude": (start[1] + finish[1]) / 2,
    }


def _courier_route(
    courier_id: int,
    courier_name: str,
    completed: list[Order],
    active: list[Order],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    completed.sort(key=lambda order: (_timestamp(order.delivered_at), order.order_number))
    active.sort(key=_active_order_sort_key)
    current_orders = [order for order in active if order.status == "on_way"]
    current = current_orders[-1] if current_orders else None
    remaining = [order for order in active if order is not current]

    dark_paths: list[list[list[float]]] = []
    current_path: list[list[float]] = []
    return_path: list[list[float]] = []
    last_completed_at = None
    last_completed_point = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
    path: list[list[float]] = [[WAREHOUSE["latitude"], WAREHOUSE["longitude"]]]
    for order in completed:
        picked_up = local_datetime(order.picked_up_at)
        if last_completed_at and (picked_up is None or picked_up > last_completed_at):
            path.append([WAREHOUSE["latitude"], WAREHOUSE["longitude"]])
            if len(path) > 1:
                dark_paths.append(path)
            path = [[WAREHOUSE["latitude"], WAREHOUSE["longitude"]]]
        for location in _locations(order):
            point = [location["latitude"], location["longitude"]]
            path.append(point)
            last_completed_point = point
        delivered = local_datetime(order.delivered_at)
        if delivered:
            last_completed_at = delivered
    if len(path) > 1:
        dark_paths.append(path)

    current_origin = last_completed_point
    carried_active = [
        order
        for order in remaining
        if order.status in {"picked_up", "awaiting_photo", "awaiting_amount"}
    ]
    latest_active_pickup = max(
        (
            parsed
            for order in carried_active
            if (parsed := local_datetime(order.picked_up_at))
        ),
        default=None,
    )
    if current:
        current_pickup = local_datetime(current.picked_up_at)
        if (
            not last_completed_at
            or current_pickup is None
            or current_pickup > last_completed_at
        ):
            current_origin = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
            if dark_paths and dark_paths[-1][-1] != current_origin:
                dark_paths[-1].append(current_origin)
        current_path = [current_origin]
        current_path.extend([
            [location["latitude"], location["longitude"]]
            for location in _locations(current)
        ])
    elif last_completed_at:
        warehouse_point = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
        if latest_active_pickup and latest_active_pickup > last_completed_at:
            # A later pickup at the warehouse proves that the return leg has
            # already happened, so it belongs to the completed history.
            if dark_paths and dark_paths[-1][-1] != warehouse_point:
                dark_paths[-1].append(warehouse_point)
        elif not carried_active and last_completed_point != warehouse_point:
            return_path = [last_completed_point, warehouse_point]

    # A light plan connects every remaining coordinate in operational order.
    # It is intentionally separate from the bright current movement: only the
    # latter is used to estimate the courier's live position.
    warehouse_point = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
    planned_path: list[list[float]] = []
    planned_order_ids: list[int] = []
    if remaining:
        plan_origin = current_path[-1] if current_path else warehouse_point
        if (
            not current_path
            and carried_active
            and last_completed_at
            and (latest_active_pickup is None or latest_active_pickup <= last_completed_at)
        ):
            # Goods picked up before the last completed stop are still in the
            # courier's vehicle; the next leg begins at that customer, not at
            # the warehouse.
            plan_origin = last_completed_point
        planned_path = [plan_origin]
        carried_orders = carried_active
        warehouse_orders = [order for order in remaining if order.status == "pending"]
        for remaining_order in carried_orders:
            order_points = [
                [location["latitude"], location["longitude"]]
                for location in _locations(remaining_order)
            ]
            if not order_points:
                continue
            planned_path.extend(order_points)
            planned_order_ids.append(remaining_order.id)
        # Pending goods are still at the shop. Never draw an impossible
        # client→client leg as if the courier already carried them.
        if warehouse_orders and planned_path[-1] != warehouse_point:
            planned_path.append(warehouse_point)
        for remaining_order in warehouse_orders:
            order_points = [
                [location["latitude"], location["longitude"]]
                for location in _locations(remaining_order)
            ]
            if not order_points:
                continue
            planned_path.extend(order_points)
            planned_order_ids.append(remaining_order.id)
        if len(planned_path) > 1 and planned_path[-1] != warehouse_point:
            planned_path.append(warehouse_point)
        if len(planned_path) < 2:
            planned_path = []

    stops: list[dict[str, Any]] = []
    sequence = 0
    for state, orders in (
        ("completed", completed),
        ("on_way", [current] if current else []),
        ("remaining", remaining),
    ):
        for order in orders:
            for location in _locations(order):
                sequence += 1
                stops.append({
                    **location,
                    "sequence": sequence,
                    "state": state if state != "remaining" else order.status,
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "product": order.product,
                    "seller_name": order.seller_name or "—",
                    "delivery_time": order.delivery_time,
                    "comment": order.comment,
                    "courier_id": courier_id,
                    "courier_name": courier_name,
                    "color": COURIER_COLORS[courier_id],
                })

    current_target = _order_payload(current) if current else None
    movement_kind = "delivery" if current_path else ("return" if return_path else None)
    if current:
        movement_started_at = current.time_started
    else:
        movement_started_at = (
            last_completed_at.isoformat() if return_path and last_completed_at else None
        )
    route = {
        "courier_id": courier_id,
        "courier_name": courier_name,
        "color": COURIER_COLORS[courier_id],
        "dark_color": COURIER_DARK_COLORS[courier_id],
        "dark_paths": dark_paths,
        "current_path": current_path,
        "return_path": return_path,
        "planned_path": planned_path,
        "planned_order_ids": planned_order_ids,
        "movement_kind": movement_kind,
        "movement_started_at": movement_started_at,
        "courier_marker": _midpoint(current_path or return_path),
        "current_target": current_target,
    }
    return route, stops


def build_delivery_monitor(repo: OrderRepository) -> dict[str, Any]:
    now = datetime.now(TASHKENT)
    report = build_delivery_stats(repo, now.date())
    operational = repo.operational_counts()
    all_orders = repo.list_all()
    orders_by_id = {order.id: order for order in all_orders}
    active_orders = [order for order in all_orders if order.status in LIVE_STATUSES]
    report_rows = report["orders"]

    created_today = [row for row in report_rows if row["created_today"]]
    manager_counts = Counter(
        orders_by_id[row["id"]].manager_name or "—"
        for row in created_today
    )
    seller_counts = Counter(
        orders_by_id[row["id"]].seller_name or "—"
        for row in created_today
    )

    map_stops: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    courier_cards: list[dict[str, Any]] = []
    for courier in COURIERS:
        active = [
            order for order in active_orders
            if _courier_id(order) == courier.user_id
        ]
        completed = [
            orders_by_id[row["id"]]
            for row in report_rows
            if row["completed_today"] and row["courier_id"] == courier.user_id
        ]
        route, stops = _courier_route(
            courier.user_id,
            courier.name,
            completed,
            active,
        )
        routes.append(route)
        map_stops.extend(stops)
        courier_cards.append({
            "id": courier.user_id,
            "name": courier.name,
            "color": COURIER_COLORS[courier.user_id],
            "dark_color": COURIER_DARK_COLORS[courier.user_id],
            "completed_today": len(completed),
            "remaining": len(active),
            "waiting_pickup": sum(
                order.status == "pending" for order in active
            ),
            "picked_up": sum(order.status == "picked_up" for order in active),
            "on_way": sum(order.status == "on_way" for order in active),
            "attention": sum(_attention(order) is not None for order in active),
            "current_target": route["current_target"],
        })

    unassigned_active = [
        order
        for order in active_orders
        if order.status == "draft" or _courier_id(order) is None
    ]
    unassigned_stops: list[dict[str, Any]] = []
    for order in sorted(unassigned_active, key=_active_order_sort_key):
        for location in _locations(order):
            unassigned_stops.append({
                **location,
                "sequence": len(unassigned_stops) + 1,
                "state": order.status,
                "order_id": order.id,
                "order_number": order.order_number,
                "product": order.product,
                "seller_name": order.seller_name or "—",
                "delivery_time": order.delivery_time,
                "comment": order.comment,
                "courier_id": None,
                "courier_name": "Не назначен",
                "color": UNASSIGNED_COLOR,
            })
    if unassigned_stops:
        routes.append({
            "courier_id": None,
            "courier_name": "Не назначен",
            "color": UNASSIGNED_COLOR,
            "dark_color": UNASSIGNED_DARK_COLOR,
            "dark_paths": [],
            "current_path": [],
            "return_path": [],
            "planned_path": [],
            "planned_order_ids": [],
            "movement_kind": None,
            "movement_started_at": None,
            "courier_marker": None,
            "current_target": None,
        })
        map_stops.extend(unassigned_stops)

    active_payloads = [_order_payload(order) for order in active_orders]
    active_payloads.sort(key=lambda item: (
        0 if item["overdue"] else 1,
        0 if item["urgent"] else 1,
        item["deadline_at"] or "9999-12-31T23:59:59+05:00",
        STATUS_PRIORITY.get(item["status"], 9),
        item["courier_name"],
        item["order_number"],
    ))
    unmapped_payloads: list[dict[str, Any]] = []
    for order in active_orders:
        base = _order_payload(order)
        for location in _unmapped_locations(order):
            unmapped_payloads.append({**base, **location})
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_time": now.strftime("%H:%M:%S"),
        "day": now.date().isoformat(),
        "warehouse": WAREHOUSE,
        "summary": {
            "created_today": len(created_today),
            "active": len(active_orders),
            "unassigned": sum(
                order.status == "draft" or _courier_id(order) is None
                for order in active_orders
            ),
            "waiting_pickup": sum(
                order.status == "pending" for order in active_orders
            ),
            "picked_up": sum(order.status == "picked_up" for order in active_orders),
            "on_way": sum(order.status == "on_way" for order in active_orders),
            "completed_today": report["summary"]["completed"],
            "cancelled_today": report["summary"]["cancelled"],
            "attention": sum(_attention(order) is not None for order in active_orders),
            "unmapped": len(unmapped_payloads),
            "sync_attention": (
                operational["sync_pending"]
                + operational["cleanup_pending"]
                + operational["cleanup_terminal"]
            ),
            "amount_text": report["summary"]["amount_text"],
        },
        "system": operational,
        "couriers": courier_cards,
        "manager_counts": [
            {"name": name, "orders": count}
            for name, count in sorted(manager_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "seller_counts": [
            {"name": seller, "orders": seller_counts.get(seller, 0)}
            for seller in SELLERS
        ],
        "active_orders": active_payloads,
        # Backwards-compatible key used by the current web UI. Entries are
        # location-based, so a mapped primary plus text-only secondary address
        # is no longer silently lost.
        "unmapped_orders": unmapped_payloads,
        "unmapped_locations": unmapped_payloads,
        "routes": routes,
        "stops": map_stops,
        "disclaimer": (
            "Значок курьера показывает расчётную позицию после нажатия "
            "«Еду к заказу» и при расчётном возврате на склад после доставки. "
            "До начала выезда текущая позиция курьера неизвестна. GPS-геолокация и текущие пробки не используются."
        ),
    }
