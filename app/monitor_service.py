from __future__ import annotations

from collections import Counter
from datetime import datetime
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
LIVE_STATUSES = {"pending", "picked_up", "on_way", "awaiting_photo", "awaiting_amount"}
STATUS_PRIORITY = {
    "on_way": 0,
    "awaiting_photo": 1,
    "awaiting_amount": 1,
    "picked_up": 2,
    "pending": 3,
}


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


def _attention(order: Order) -> str | None:
    if order.status == "pending":
        minutes = _minutes_since(order.created_at)
        if minutes is not None and minutes >= 30:
            return f"Ждёт забора {minutes} мин"
    elif order.status == "picked_up":
        minutes = _minutes_since(order.picked_up_at)
        if minutes is not None and minutes >= 20:
            return f"Товар забран, выезд не начат {minutes} мин"
    elif order.status in {"on_way", "awaiting_photo", "awaiting_amount"}:
        minutes = _minutes_since(order.time_started)
        if minutes is not None and minutes >= 90:
            return f"В пути {minutes} мин"
    return None


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


def _order_payload(order: Order) -> dict[str, Any]:
    phones = [display_phone(order.client_phone)]
    if order.client_phone_2 and order.client_phone_2 != order.client_phone:
        phones.append(display_phone(order.client_phone_2))
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
        "created_time": _time(order.created_at),
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
    active.sort(key=lambda order: (
        STATUS_PRIORITY.get(order.status, 9),
        _timestamp(order.time_started)
        or _timestamp(order.picked_up_at)
        or _timestamp(order.created_at),
        order.order_number,
    ))
    current_orders = [order for order in active if order.status == "on_way"]
    current = current_orders[-1] if current_orders else None
    remaining = [order for order in active if order is not current]

    dark_paths: list[list[list[float]]] = []
    current_path: list[list[float]] = []
    last_completed_at = None
    last_completed_point = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
    path: list[list[float]] = [[WAREHOUSE["latitude"], WAREHOUSE["longitude"]]]
    for order in completed:
        picked_up = local_datetime(order.picked_up_at)
        if last_completed_at and picked_up and picked_up > last_completed_at:
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
    if current:
        current_pickup = local_datetime(current.picked_up_at)
        if not last_completed_at or (current_pickup and current_pickup > last_completed_at):
            current_origin = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
            if dark_paths and dark_paths[-1][-1] != current_origin:
                dark_paths[-1].append(current_origin)
        current_path = [current_origin]
        current_path.extend([
            [location["latitude"], location["longitude"]]
            for location in _locations(current)
        ])
    elif not active and dark_paths:
        warehouse_point = [WAREHOUSE["latitude"], WAREHOUSE["longitude"]]
        if dark_paths[-1][-1] != warehouse_point:
            dark_paths[-1].append(warehouse_point)

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
                    "courier_id": courier_id,
                    "courier_name": courier_name,
                    "color": COURIER_COLORS[courier_id],
                })

    current_target = _order_payload(current) if current else None
    route = {
        "courier_id": courier_id,
        "courier_name": courier_name,
        "color": COURIER_COLORS[courier_id],
        "dark_color": COURIER_DARK_COLORS[courier_id],
        "dark_paths": dark_paths,
        "current_path": current_path,
        "courier_marker": _midpoint(current_path),
        "current_target": current_target,
    }
    return route, stops


def build_delivery_monitor(repo: OrderRepository) -> dict[str, Any]:
    now = datetime.now(TASHKENT)
    report = build_delivery_stats(repo, now.date())
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
            "waiting_pickup": sum(order.status == "pending" for order in active),
            "picked_up": sum(order.status == "picked_up" for order in active),
            "on_way": sum(order.status == "on_way" for order in active),
            "attention": sum(_attention(order) is not None for order in active),
            "current_target": route["current_target"],
        })

    active_payloads = [_order_payload(order) for order in active_orders]
    active_payloads.sort(key=lambda item: (
        STATUS_PRIORITY.get(item["status"], 9),
        item["courier_name"],
        item["order_number"],
    ))
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_time": now.strftime("%H:%M:%S"),
        "day": now.date().isoformat(),
        "warehouse": WAREHOUSE,
        "summary": {
            "created_today": len(created_today),
            "active": len(active_orders),
            "waiting_pickup": sum(order.status == "pending" for order in active_orders),
            "picked_up": sum(order.status == "picked_up" for order in active_orders),
            "on_way": sum(order.status == "on_way" for order in active_orders),
            "completed_today": report["summary"]["completed"],
            "cancelled_today": report["summary"]["cancelled"],
            "attention": sum(_attention(order) is not None for order in active_orders),
            "amount_text": report["summary"]["amount_text"],
        },
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
        "routes": routes,
        "stops": map_stops,
        "disclaimer": (
            "Значок курьера показывает расчётную позицию на прямом отрезке к текущему заказу. "
            "GPS-геолокация курьера не используется."
        ),
    }
