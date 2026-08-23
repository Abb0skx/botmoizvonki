from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.database import OrderRepository
from app.models import Order, OrderEvent
from app.utils.couriers import COURIERS, COURIERS_BY_ID
from app.utils.formatters import short_address, telegram_location_url
from app.utils.parsers import display_phone


TASHKENT = ZoneInfo("Asia/Tashkent")
ACTIVE_STATUSES = {"draft", "pending", "picked_up", "on_way", "awaiting_photo", "awaiting_amount"}
COURIER_COLORS = {
    7636344727: "#ef4444",
    202134293: "#2563eb",
    1799690992: "#10b981",
}
UNKNOWN_COURIER_COLOR = "#7c3aed"
COURIER_DARK_COLORS = {
    7636344727: "#991b1b",
    202134293: "#1e3a8a",
    1799690992: "#065f46",
}


def parse_report_day(value: str | None, *, today: date | None = None) -> date:
    current = today or datetime.now(TASHKENT).date()
    cleaned = (value or "today").strip().casefold()
    if cleaned == "today":
        return current
    if cleaned == "yesterday":
        return current - timedelta(days=1)
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as error:
        raise ValueError("Неверная дата. Используйте формат ГГГГ-ММ-ДД.") from error
    if parsed.year < 2024 or parsed > current + timedelta(days=1):
        raise ValueError("Дата находится вне доступного диапазона.")
    return parsed


def local_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TASHKENT)
    return parsed.astimezone(TASHKENT)


def _day_interval(report_day: date) -> tuple[datetime, datetime]:
    start = datetime(
        report_day.year,
        report_day.month,
        report_day.day,
        tzinfo=TASHKENT,
    )
    return start, start + timedelta(days=1)


def _event_time(event: OrderEvent) -> datetime | None:
    return local_datetime(event.created_at)


def _courier_id(order: Order) -> int | None:
    return order.courier_id or order.assigned_courier_id


def _courier_name(order: Order) -> str:
    courier_id = _courier_id(order)
    configured = COURIERS_BY_ID.get(courier_id)
    return (
        order.courier_name
        or order.assigned_courier_name
        or (configured.name if configured else None)
        or "Не назначен"
    )


def _courier_color(courier_id: int | None) -> str:
    return COURIER_COLORS.get(courier_id, UNKNOWN_COURIER_COLOR)


def _event_courier_id(event: OrderEvent, order: Order) -> int | None:
    if event.actor_role == "courier" and event.actor_id in COURIERS_BY_ID:
        return event.actor_id
    return _courier_id(order)


def _event_courier_name(event: OrderEvent, order: Order) -> str:
    courier_id = _event_courier_id(event, order)
    configured = COURIERS_BY_ID.get(courier_id)
    if event.actor_role == "courier" and event.actor_name:
        return event.actor_name
    return (configured.name if configured else None) or _courier_name(order)


def _report_courier(
    order: Order,
    events: list[OrderEvent],
) -> tuple[int | None, str]:
    courier_events = [
        event
        for event in events
        if event.actor_role == "courier" and event.actor_id in COURIERS_BY_ID
        and (
            event.to_status in {"picked_up", "on_way", "completed", "cancelled"}
            or event.from_status in {"picked_up", "on_way", "completed"}
        )
    ]
    if courier_events:
        event = courier_events[-1]
        courier_id = event.actor_id
        configured = COURIERS_BY_ID.get(courier_id)
        return courier_id, event.actor_name or configured.name
    return _courier_id(order), _courier_name(order)


def _status_at_day_end(
    order: Order,
    events: list[OrderEvent],
    report_day: date,
) -> str:
    if report_day >= datetime.now(TASHKENT).date():
        return order.status
    status_events = [event for event in events if event.to_status]
    if status_events:
        return status_events[-1].to_status
    return order.status


def _activity_event(event: OrderEvent) -> bool:
    return bool(
        event.event_type == "order_created"
        or event.to_status in {"picked_up", "on_way", "completed", "cancelled"}
        or (
            event.from_status in {"picked_up", "on_way", "completed", "cancelled"}
            and event.to_status in {"pending", "picked_up"}
        )
    )


def _order_map_url(order: Order, location_number: int = 1) -> str | None:
    if location_number == 2:
        latitude, longitude = order.second_latitude, order.second_longitude
    else:
        latitude, longitude = order.latitude, order.longitude
    if latitude is None or longitude is None:
        return telegram_location_url(order, location_number)
    query = urlencode({
        "ll": f"{longitude:.6f},{latitude:.6f}",
        "z": "17",
        "pt": f"{longitude:.6f},{latitude:.6f},pm2rdm",
    })
    return f"https://yandex.uz/maps/?{query}"


def _timeline_item(event: OrderEvent, order: Order) -> dict[str, Any] | None:
    occurred = _event_time(event)
    if not occurred:
        return None
    courier_name = _event_courier_name(event, order)
    address = short_address(order)
    base = {
        "timestamp": occurred.isoformat(),
        "time": occurred.strftime("%H:%M"),
        "order_id": order.id,
        "order_number": order.order_number,
        "courier_id": _event_courier_id(event, order),
        "courier_name": courier_name,
    }
    if event.event_type == "order_created":
        return {
            **base,
            "kind": "created",
            "icon": "📥",
            "text": (
                f"Пришёл заказ №{order.order_number} · {order.product}"
                f" · продавец {order.seller_name or '—'}"
            ),
        }
    if event.from_status == "completed" and event.to_status in {"pending", "picked_up", "on_way"}:
        return {
            **base,
            "kind": "completion_undone",
            "icon": "↩️",
            "text": f"{courier_name} отменил завершение заказа №{order.order_number}",
        }
    if event.from_status == "on_way" and event.to_status in {"pending", "picked_up"}:
        return {
            **base,
            "kind": "departure_undone",
            "icon": "↩️",
            "text": f"{courier_name} отменил выезд к заказу №{order.order_number}",
        }
    if event.from_status == "cancelled" and event.to_status in {"pending", "picked_up"}:
        return {
            **base,
            "kind": "restored",
            "icon": "↩️",
            "text": f"Заказ №{order.order_number} возвращён в доставку",
        }
    if event.from_status == "picked_up" and event.to_status == "pending":
        return {
            **base,
            "kind": "pickup_undone",
            "icon": "↩️",
            "text": f"Снята отметка «товар забран» с заказа №{order.order_number}",
        }
    if event.to_status == "picked_up":
        return {
            **base,
            "kind": "picked_up",
            "icon": "📦",
            "text": f"{courier_name} забрал товар для заказа №{order.order_number}",
        }
    if event.to_status == "on_way":
        return {
            **base,
            "kind": "departed",
            "icon": "🚗",
            "text": f"{courier_name} выехал к заказу №{order.order_number} → {address}",
        }
    if event.to_status == "completed":
        return {
            **base,
            "kind": "completed",
            "icon": "✅",
            "text": f"{courier_name} приехал и доставил заказ №{order.order_number} · {address}",
        }
    if event.to_status == "cancelled":
        return {
            **base,
            "kind": "cancelled",
            "icon": "❌",
            "text": f"{courier_name} отменил заказ №{order.order_number}",
        }
    return None


def _order_times(
    order: Order,
    events: list[OrderEvent],
    report_day: date,
) -> tuple[datetime | None, datetime | None, datetime | None, datetime | None]:
    created = local_datetime(order.created_at)
    pickups = [
        occurred
        for event in events
        if event.to_status == "picked_up" and (occurred := _event_time(event))
    ]
    starts = [
        occurred
        for event in events
        if event.to_status == "on_way" and (occurred := _event_time(event))
    ]
    completions = [
        occurred
        for event in events
        if event.to_status == "completed" and (occurred := _event_time(event))
    ]
    picked_up = pickups[-1] if pickups else local_datetime(order.picked_up_at)
    started = starts[-1] if starts else local_datetime(order.time_started)
    completed = completions[-1] if completions else local_datetime(order.delivered_at)
    if created and created.date() != report_day:
        created = None
    if picked_up and picked_up.date() != report_day:
        picked_up = None
    if started and started.date() != report_day:
        started = None
    if completed and completed.date() != report_day:
        completed = None
    return created, picked_up, started, completed


def _duration_minutes(started: datetime | None, completed: datetime | None) -> int | None:
    if not started or not completed or completed < started:
        return None
    return round((completed - started).total_seconds() / 60)


def _money_text(usd: int, uzs: int) -> str:
    values = []
    if usd:
        values.append(f"{usd:,}$".replace(",", " "))
    if uzs:
        values.append(f"{uzs:,} сум".replace(",", " "))
    return " · ".join(values) or "0"


def build_delivery_stats(
    repo: OrderRepository,
    report_day: date,
    *,
    courier_id: int | None = None,
) -> dict[str, Any]:
    start, end = _day_interval(report_day)
    events = repo.list_events_between(start, end)
    all_orders = repo.list_all()
    all_day_events_by_order: dict[int, list[OrderEvent]] = defaultdict(list)
    activity_events_by_order: dict[int, list[OrderEvent]] = defaultdict(list)
    for event in events:
        all_day_events_by_order[event.order_id].append(event)
        if _activity_event(event):
            activity_events_by_order[event.order_id].append(event)

    relevant: list[Order] = []
    for order in all_orders:
        created = local_datetime(order.created_at)
        picked_up = local_datetime(order.picked_up_at)
        started = local_datetime(order.time_started)
        delivered = local_datetime(order.delivered_at)
        has_day_field = any(
            value and value.date() == report_day
            for value in (created, picked_up, started, delivered)
        )
        activity_events = activity_events_by_order.get(order.id, [])
        if not has_day_field and not activity_events:
            continue
        if courier_id is not None:
            event_couriers = {
                _event_courier_id(event, order)
                for event in activity_events
                if event.actor_role == "courier"
            }
            if courier_id not in {_courier_id(order), *event_couriers}:
                continue
        relevant.append(order)

    rows: list[dict[str, Any]] = []
    completed_ids: set[int] = set()
    cancelled_ids: set[int] = set()
    created_ids: set[int] = set()
    durations: list[int] = []
    pickup_durations: list[int] = []
    total_usd = total_uzs = received_usd = received_uzs = 0

    for order in relevant:
        day_events = all_day_events_by_order.get(order.id, [])
        activity_events = activity_events_by_order.get(order.id, [])
        created, picked_up, started, completed = _order_times(order, day_events, report_day)
        created_today = bool(created) or any(
            event.event_type == "order_created" for event in activity_events
        )
        completed_today = bool(completed) or any(
            event.to_status == "completed" for event in activity_events
        )
        cancelled_today = any(
            event.to_status == "cancelled" for event in activity_events
        )
        if created_today:
            created_ids.add(order.id)
        if completed_today:
            completed_ids.add(order.id)
        if cancelled_today:
            cancelled_ids.add(order.id)
        duration = _duration_minutes(started, completed)
        pickup_duration = _duration_minutes(picked_up, completed)
        if duration is not None:
            durations.append(duration)
        if pickup_duration is not None:
            pickup_durations.append(pickup_duration)

        total_usd += order.amount_usd or 0
        total_uzs += order.amount_uzs or 0
        if completed_today:
            received_usd += order.received_usd if order.received_usd is not None else (order.amount_usd or 0)
            received_uzs += order.received_uzs if order.received_uzs is not None else (order.amount_uzs or 0)

        courier, courier_name = _report_courier(order, day_events)
        report_status = _status_at_day_end(order, day_events, report_day)
        phones = [display_phone(order.client_phone)]
        if order.client_phone_2 and order.client_phone_2 != order.client_phone:
            phones.append(display_phone(order.client_phone_2))
        sort_time = completed or started or picked_up or created or local_datetime(order.updated_at) or start
        rows.append({
            "id": order.id,
            "order_number": order.order_number,
            "product": order.product,
            "seller_name": order.seller_name or "—",
            "courier_id": courier,
            "courier_name": courier_name,
            "courier_color": _courier_color(courier),
            "status": report_status,
            "phones": phones,
            "address": short_address(order),
            "created_time": created.strftime("%H:%M") if created else None,
            "picked_up_time": picked_up.strftime("%H:%M") if picked_up else None,
            "started_time": started.strftime("%H:%M") if started else None,
            "completed_time": completed.strftime("%H:%M") if completed else None,
            "duration_minutes": duration,
            "pickup_duration_minutes": pickup_duration,
            "amount_usd": order.amount_usd or 0,
            "amount_uzs": order.amount_uzs or 0,
            "amount_text": _money_text(order.amount_usd or 0, order.amount_uzs or 0),
            "created_today": created_today,
            "completed_today": completed_today,
            "cancelled_today": cancelled_today,
            "sort_timestamp": sort_time.isoformat(),
        })

    rows.sort(key=lambda item: (item["sort_timestamp"], item["order_number"]))
    row_by_id = {row["id"]: row for row in rows}

    stop_candidates: list[
        tuple[tuple[Any, ...], Order, int, float, float, str]
    ] = []
    for order in relevant:
        row = row_by_id[order.id]
        if row["completed_today"]:
            route_state, priority = "completed", 0
        elif row["status"] == "on_way":
            route_state, priority = "on_way", 1
        elif row["status"] == "picked_up":
            route_state, priority = "picked_up", 2
        else:
            route_state, priority = "remaining", 3
        base_key = (priority, row["sort_timestamp"], order.order_number)
        if order.latitude is not None and order.longitude is not None:
            stop_candidates.append((base_key + (1,), order, 1, order.latitude, order.longitude, route_state))
        if order.second_latitude is not None and order.second_longitude is not None:
            stop_candidates.append((base_key + (2,), order, 2, order.second_latitude, order.second_longitude, route_state))
    stop_candidates.sort(key=lambda item: item[0])

    stops: list[dict[str, Any]] = []
    for sequence, (_, order, location_number, latitude, longitude, route_state) in enumerate(stop_candidates, 1):
        row = row_by_id[order.id]
        address = short_address(order, location_number)
        stops.append({
            "sequence": sequence,
            "order_id": order.id,
            "order_number": order.order_number,
            "location_number": location_number,
            "latitude": latitude,
            "longitude": longitude,
            "address": address,
            "product": order.product,
            "seller_name": order.seller_name or "—",
            "courier_id": row["courier_id"],
            "courier_name": row["courier_name"],
            "color": row["courier_color"],
            "completed": row["completed_today"],
            "state": route_state,
            "time": row["completed_time"] or row["started_time"] or row["picked_up_time"] or row["created_time"],
            "map_url": _order_map_url(order, location_number),
        })

    routes: list[dict[str, Any]] = []
    grouped_stops: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for stop in stops:
        grouped_stops[stop["courier_id"]].append(stop)
    for route_courier_id, route_stops in grouped_stops.items():
        configured = COURIERS_BY_ID.get(route_courier_id)
        routes.append({
            "courier_id": route_courier_id,
            "courier_name": configured.name if configured else route_stops[0]["courier_name"],
            "color": _courier_color(route_courier_id),
            "dark_color": COURIER_DARK_COLORS.get(route_courier_id, "#4c1d95"),
            "sequences": [stop["sequence"] for stop in route_stops],
            "completed_sequences": [
                stop["sequence"] for stop in route_stops if stop["state"] == "completed"
            ],
            "current_sequences": [
                stop["sequence"] for stop in route_stops if stop["state"] == "on_way"
            ],
            "remaining_sequences": [
                stop["sequence"] for stop in route_stops
                if stop["state"] in {"picked_up", "remaining"}
            ],
        })

    timeline = []
    for event in events:
        order = next((item for item in relevant if item.id == event.order_id), None)
        if not order:
            continue
        item = _timeline_item(event, order)
        if item:
            timeline.append(item)
    timeline.sort(key=lambda item: (item["timestamp"], item["order_number"]))

    courier_summaries = []
    for courier in COURIERS:
        courier_rows = [row for row in rows if row["courier_id"] == courier.user_id]
        courier_durations = [
            row["duration_minutes"]
            for row in courier_rows
            if row["duration_minutes"] is not None
        ]
        courier_summaries.append({
            "id": courier.user_id,
            "name": courier.name,
            "color": _courier_color(courier.user_id),
            "orders": len(courier_rows),
            "completed": sum(1 for row in courier_rows if row["completed_today"]),
            "active": sum(1 for row in courier_rows if row["status"] in ACTIVE_STATUSES),
            "picked_up": sum(1 for row in courier_rows if row["status"] == "picked_up"),
            "on_way": sum(1 for row in courier_rows if row["status"] == "on_way"),
            "cancelled": sum(1 for row in courier_rows if row["cancelled_today"]),
            "average_minutes": (
                round(sum(courier_durations) / len(courier_durations))
                if courier_durations
                else None
            ),
        })

    return {
        "day": report_day.isoformat(),
        "day_label": report_day.strftime("%d.%m.%Y"),
        "generated_at": datetime.now(TASHKENT).isoformat(timespec="seconds"),
        "selected_courier_id": courier_id,
        "summary": {
            "orders": len(rows),
            "created": len(created_ids),
            "completed": len(completed_ids),
            "cancelled": len(cancelled_ids),
            "active": sum(1 for row in rows if row["status"] in ACTIVE_STATUSES),
            "picked_up": sum(1 for row in rows if row["status"] == "picked_up"),
            "on_way": sum(1 for row in rows if row["status"] == "on_way"),
            "mapped": len(stops),
            "average_minutes": round(sum(durations) / len(durations)) if durations else None,
            "average_pickup_minutes": (
                round(sum(pickup_durations) / len(pickup_durations))
                if pickup_durations
                else None
            ),
            "amount_usd": total_usd,
            "amount_uzs": total_uzs,
            "amount_text": _money_text(total_usd, total_uzs),
            "received_usd": received_usd,
            "received_uzs": received_uzs,
            "received_text": _money_text(received_usd, received_uzs),
        },
        "couriers": courier_summaries,
        "orders": rows,
        "stops": stops,
        "routes": routes,
        "timeline": timeline,
        "warehouse": {
            "name": "Склад · Малика",
            "latitude": 41.337420,
            "longitude": 69.272104,
        },
    }
