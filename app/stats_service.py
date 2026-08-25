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
from app.utils.payments import COLLECT_ON_DELIVERY, PAID_AT_ASSEMBLY, payment_label


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


def _transitioned_to(event: OrderEvent, status: str) -> bool:
    """Return true only for a real status transition, not a same-status edit."""
    return event.to_status == status and event.from_status != event.to_status


def _status_changed(event: OrderEvent) -> bool:
    return bool(event.to_status and event.from_status != event.to_status)


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
    if event.courier_id in COURIERS_BY_ID:
        return event.courier_id
    if event.actor_role == "courier" and event.actor_id in COURIERS_BY_ID:
        return event.actor_id
    return _courier_id(order)


def _event_courier_name(event: OrderEvent, order: Order) -> str:
    courier_id = _event_courier_id(event, order)
    configured = COURIERS_BY_ID.get(courier_id)
    if event.courier_id == courier_id and event.courier_name:
        return event.courier_name
    if event.actor_role == "courier" and event.actor_name:
        return event.actor_name
    return (configured.name if configured else None) or _courier_name(order)


def _report_courier(
    order: Order,
    events: list[OrderEvent],
    report_day: date,
) -> tuple[int | None, str]:
    current_courier_id = _courier_id(order)
    is_current_report = report_day >= datetime.now(TASHKENT).date()
    if is_current_report and order.status in ACTIVE_STATUSES and current_courier_id is not None:
        configured = COURIERS_BY_ID.get(current_courier_id)
        current_name = (
            order.courier_name
            if order.courier_id == current_courier_id
            else order.assigned_courier_name
        )
        return (
            current_courier_id,
            current_name or (configured.name if configured else None) or "Не назначен",
        )
    courier_events = [
        event
        for event in events
        if (
            event.courier_id in COURIERS_BY_ID
            or (event.actor_role == "courier" and event.actor_id in COURIERS_BY_ID)
        )
        and (
            "assigned_courier_id" in event.changed_fields
            or event.event_type == "courier_read"
            or (
                _status_changed(event)
                and event.to_status in {"picked_up", "on_way", "completed", "cancelled"}
            )
            or (
                _status_changed(event)
                and event.from_status in {"picked_up", "on_way", "completed"}
            )
        )
    ]
    if courier_events:
        event = courier_events[-1]
        courier_id = (
            event.courier_id
            if event.courier_id in COURIERS_BY_ID
            else event.actor_id
        )
        configured = COURIERS_BY_ID.get(courier_id)
        courier_name = (
            event.courier_name
            if event.courier_id == courier_id and event.courier_name
            else event.actor_name
        )
        return courier_id, courier_name or configured.name
    if not is_current_report and events:
        # Do not retroactively assign an old report to today's courier merely
        # because an order was reopened/reassigned later.
        return None, "Не назначен"
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
        or event.event_type == "courier_read"
        or (
            _status_changed(event)
            and event.to_status in {"picked_up", "on_way", "completed", "cancelled"}
        )
        or (
            _status_changed(event)
            and event.from_status in {"picked_up", "on_way", "completed", "cancelled"}
            and event.to_status in {"pending", "picked_up", "on_way"}
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
    actor_name = event.actor_name or courier_name
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
    if event.event_type == "courier_read":
        return {
            **base,
            "kind": "read",
            "icon": "👀",
            "text": f"{courier_name} прочитал заказ №{order.order_number} и выехал на склад",
        }
    if not _status_changed(event):
        return None
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
    if event.from_status == "cancelled" and event.to_status in {"pending", "picked_up", "on_way"}:
        return {
            **base,
            "kind": "restored",
            "icon": "↩️",
            "text": (
                f"{actor_name} вернул заказ №{order.order_number} в доставку"
            ),
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
            "text": f"{actor_name} отменил заказ №{order.order_number}",
        }
    return None


def _order_times(
    order: Order,
    events: list[OrderEvent],
    report_day: date,
) -> tuple[datetime | None, datetime | None, datetime | None, datetime | None, datetime | None]:
    created = local_datetime(order.created_at)
    reads = [
        occurred
        for event in events
        if event.event_type == "courier_read"
        and (occurred := _event_time(event))
    ]
    pickups = [
        occurred
        for event in events
        if _transitioned_to(event, "picked_up") and (occurred := _event_time(event))
    ]
    starts = [
        occurred
        for event in events
        if _transitioned_to(event, "on_way") and (occurred := _event_time(event))
    ]
    completions = [
        occurred
        for event in events
        if _transitioned_to(event, "completed") and (occurred := _event_time(event))
    ]
    # Preserve the explicit business timestamp when it belongs to this day.
    # Otherwise use the day's immutable audit event instead of a mutable field
    # that may now point at a later read after courier reassignment.
    mutable_read_at = local_datetime(order.courier_read_at)
    read_at = (
        mutable_read_at
        if mutable_read_at and mutable_read_at.date() == report_day
        else (reads[-1] if reads else mutable_read_at)
    )
    picked_up = pickups[-1] if pickups else local_datetime(order.picked_up_at)
    started = starts[-1] if starts else local_datetime(order.time_started)
    completed = completions[-1] if completions else local_datetime(order.delivered_at)
    if created and created.date() != report_day:
        created = None
    if read_at and read_at.date() != report_day:
        read_at = None
    if picked_up and picked_up.date() != report_day:
        picked_up = None
    if started and started.date() != report_day:
        started = None
    if completed and completed.date() != report_day:
        completed = None
    return created, read_at, picked_up, started, completed


def _duration_minutes(started: datetime | None, completed: datetime | None) -> int | None:
    if not started or not completed or completed < started:
        return None
    return round((completed - started).total_seconds() / 60)


def _money_text(usd: int, uzs: int) -> str:
    # Build each currency exactly once. Keeping the two branches explicit also
    # prevents accidental duplicated USD fragments when more summaries are
    # composed from this helper.
    usd_text = f"{usd:,}$".replace(",", " ") if usd else None
    uzs_text = f"{uzs:,} сум".replace(",", " ") if uzs else None
    return " · ".join(value for value in (usd_text, uzs_text) if value) or "0"


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
        read_at = local_datetime(order.courier_read_at)
        picked_up = local_datetime(order.picked_up_at)
        started = local_datetime(order.time_started)
        delivered = local_datetime(order.delivered_at)
        has_day_field = any(
            value and value.date() == report_day
            for value in (created, read_at, picked_up, started, delivered)
        )
        activity_events = activity_events_by_order.get(order.id, [])
        if not has_day_field and not activity_events:
            continue
        if courier_id is not None:
            report_courier_id, _ = _report_courier(
                order,
                all_day_events_by_order.get(order.id, []),
                report_day,
            )
            if report_courier_id != courier_id:
                continue
        relevant.append(order)

    rows: list[dict[str, Any]] = []
    completed_ids: set[int] = set()
    cancelled_ids: set[int] = set()
    created_ids: set[int] = set()
    durations: list[int] = []
    pickup_durations: list[int] = []
    response_durations: list[int] = []
    warehouse_durations: list[int] = []
    assignment_pickup_durations: list[int] = []
    created_usd = created_uzs = delivered_usd = delivered_uzs = 0
    received_usd = received_uzs = paid_usd = paid_uzs = 0
    paid_at_assembly_count = missing_received_confirmation = 0

    for order in relevant:
        day_events = all_day_events_by_order.get(order.id, [])
        activity_events = activity_events_by_order.get(order.id, [])
        created, read_at, picked_up, started, completed = _order_times(order, day_events, report_day)
        report_status = _status_at_day_end(order, day_events, report_day)
        created_today = bool(created) or any(
            event.event_type == "order_created" for event in activity_events
        )
        # An undo is a correction, not another final result. Count money and
        # completion/cancellation only when that state still holds at day end.
        completed_today = report_status == "completed" and (
            bool(completed)
            or any(_transitioned_to(event, "completed") for event in activity_events)
        )
        cancelled_today = report_status == "cancelled" and any(
            _transitioned_to(event, "cancelled") for event in activity_events
        )
        if created_today:
            created_ids.add(order.id)
        if completed_today:
            completed_ids.add(order.id)
        if cancelled_today:
            cancelled_ids.add(order.id)
        duration_completed = completed if completed_today else None
        # A trip may start before midnight and finish on the report day. The
        # mutable fields retain the current delivery cycle and are a safe
        # fallback only when they precede this final completion.
        duration_started = started
        if duration_completed and not duration_started:
            candidate = local_datetime(order.time_started)
            if candidate and candidate <= duration_completed:
                duration_started = candidate
        duration_picked_up = picked_up
        if duration_completed and not duration_picked_up:
            candidate = local_datetime(order.picked_up_at)
            if candidate and candidate <= duration_completed:
                duration_picked_up = candidate
        duration = _duration_minutes(duration_started, duration_completed)
        pickup_duration = _duration_minutes(duration_picked_up, duration_completed)
        assignment_times = [
            occurred
            for event in day_events
            if "assigned_courier_id" in event.changed_fields
            and (occurred := _event_time(event))
            and read_at
            and occurred <= read_at
        ]
        pickup_assignment_times = [
            occurred
            for event in day_events
            if "assigned_courier_id" in event.changed_fields
            and (occurred := _event_time(event))
            and picked_up
            and occurred <= picked_up
        ]
        response_duration = _duration_minutes(
            assignment_times[-1] if assignment_times else None,
            read_at,
        )
        warehouse_duration = _duration_minutes(read_at, picked_up)
        assignment_pickup_duration = _duration_minutes(
            pickup_assignment_times[-1] if pickup_assignment_times else None,
            picked_up,
        )
        if duration is not None:
            durations.append(duration)
        if pickup_duration is not None:
            pickup_durations.append(pickup_duration)
        if response_duration is not None:
            response_durations.append(response_duration)
        if warehouse_duration is not None:
            warehouse_durations.append(warehouse_duration)
        if assignment_pickup_duration is not None:
            assignment_pickup_durations.append(assignment_pickup_duration)

        if created_today:
            created_usd += order.amount_usd or 0
            created_uzs += order.amount_uzs or 0
        if completed_today:
            delivered_usd += order.amount_usd or 0
            delivered_uzs += order.amount_uzs or 0
            if order.payment_status == PAID_AT_ASSEMBLY:
                paid_at_assembly_count += 1
                paid_usd += order.amount_usd or 0
                paid_uzs += order.amount_uzs or 0
            elif order.received_usd is not None or order.received_uzs is not None:
                received_usd += order.received_usd or 0
                received_uzs += order.received_uzs or 0
            else:
                missing_received_confirmation += 1

        courier, courier_name = _report_courier(order, day_events, report_day)
        phones = [display_phone(order.client_phone)]
        if order.client_phone_2 and order.client_phone_2 != order.client_phone:
            phones.append(display_phone(order.client_phone_2))
        sort_time = completed or started or picked_up or read_at or created or local_datetime(order.updated_at) or start
        rows.append({
            "id": order.id,
            "order_number": order.order_number,
            "product": order.product,
            "seller_name": order.seller_name or "—",
            "manager_name": order.manager_name or "—",
            "payment_status": order.payment_status or COLLECT_ON_DELIVERY,
            "payment_label": payment_label(order.payment_status or COLLECT_ON_DELIVERY),
            "courier_id": courier,
            "courier_name": courier_name,
            "courier_color": _courier_color(courier),
            "status": report_status,
            "phones": phones,
            "address": short_address(order),
            "delivery_time": order.delivery_time,
            "comment": order.comment,
            "created_time": created.strftime("%H:%M") if created else None,
            "read_time": read_at.strftime("%H:%M") if read_at else None,
            "picked_up_time": picked_up.strftime("%H:%M") if picked_up else None,
            "started_time": started.strftime("%H:%M") if started else None,
            "completed_time": completed.strftime("%H:%M") if completed_today and completed else None,
            "created_at": created.isoformat() if created else None,
            "read_at": read_at.isoformat() if read_at else None,
            "picked_up_at": picked_up.isoformat() if picked_up else None,
            "started_at": started.isoformat() if started else None,
            "completed_at": completed.isoformat() if completed_today and completed else None,
            "duration_minutes": duration,
            "pickup_duration_minutes": pickup_duration,
            "response_minutes": response_duration,
            "warehouse_minutes": warehouse_duration,
            "assignment_pickup_minutes": assignment_pickup_duration,
            "amount_usd": order.amount_usd or 0,
            "amount_uzs": order.amount_uzs or 0,
            "amount_text": _money_text(order.amount_usd or 0, order.amount_uzs or 0),
            "received_usd": order.received_usd,
            "received_uzs": order.received_uzs,
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
        elif row["status"] == "cancelled":
            route_state, priority = "cancelled", 4
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
            "delivery_time": order.delivery_time,
            "comment": order.comment,
            "courier_id": row["courier_id"],
            "courier_name": row["courier_name"],
            "color": row["courier_color"],
            "completed": row["completed_today"],
            "state": route_state,
            "time": row["completed_time"] or row["started_time"] or row["picked_up_time"] or row["created_time"],
            "map_url": _order_map_url(order, location_number),
        })

    routes: list[dict[str, Any]] = []
    order_by_id = {order.id: order for order in relevant}
    warehouse_point = [41.337420, 69.272104]

    def location_points(order_id: int) -> list[list[float]]:
        order = order_by_id[order_id]
        points: list[list[float]] = []
        if order.latitude is not None and order.longitude is not None:
            points.append([order.latitude, order.longitude])
        if order.second_latitude is not None and order.second_longitude is not None:
            points.append([order.second_latitude, order.second_longitude])
        return points

    grouped_stops: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for stop in stops:
        grouped_stops[stop["courier_id"]].append(stop)
    for route_courier_id, route_stops in grouped_stops.items():
        configured = COURIERS_BY_ID.get(route_courier_id)
        courier_rows = [row for row in rows if row["courier_id"] == route_courier_id]
        completed_rows = sorted(
            (row for row in courier_rows if row["completed_today"]),
            key=lambda row: (row["completed_at"] or row["sort_timestamp"], row["order_number"]),
        )
        completed_paths: list[list[list[float]]] = []
        completed_path: list[list[float]] = [warehouse_point]
        last_completed: datetime | None = None
        last_point = warehouse_point
        for row in completed_rows:
            pickup = local_datetime(row["picked_up_at"])
            if last_completed and pickup and pickup > last_completed:
                if completed_path[-1] != warehouse_point:
                    completed_path.append(warehouse_point)
                if len(completed_path) > 1:
                    completed_paths.append(completed_path)
                completed_path = [warehouse_point]
                last_point = warehouse_point
            for point in location_points(row["id"]):
                completed_path.append(point)
                last_point = point
            completed_at = local_datetime(row["completed_at"])
            if completed_at:
                last_completed = completed_at
        if len(completed_path) > 1:
            completed_paths.append(completed_path)

        current_rows = [row for row in courier_rows if row["status"] == "on_way"]
        current_row = current_rows[-1] if current_rows else None
        current_path: list[list[float]] = []
        if current_row:
            pickup = local_datetime(current_row["picked_up_at"])
            current_origin = last_point
            if not last_completed or (pickup and pickup > last_completed):
                current_origin = warehouse_point
                if completed_paths and completed_paths[-1][-1] != warehouse_point:
                    completed_paths[-1].append(warehouse_point)
            current_path = [current_origin, *location_points(current_row["id"])]

        carried_rows = [
            row
            for row in courier_rows
            if row["status"] in {"picked_up", "awaiting_photo", "awaiting_amount"}
            and row is not current_row
        ]
        carried_rows.sort(key=lambda row: (row["sort_timestamp"], row["order_number"]))
        pending_rows = [
            row
            for row in courier_rows
            if row["status"] in {"draft", "pending"}
        ]
        pending_rows.sort(key=lambda row: (row["sort_timestamp"], row["order_number"]))
        latest_carried_pickup = max(
            (
                parsed
                for row in carried_rows
                if (parsed := local_datetime(row["picked_up_at"]))
            ),
            default=None,
        )
        return_path: list[list[float]] = []
        if (
            last_completed
            and not current_path
            and not carried_rows
            and last_point != warehouse_point
        ):
            return_path = [last_point, warehouse_point]
        planned_path: list[list[float]] = []
        planned_order_ids: list[int] = []
        if carried_rows or pending_rows:
            plan_origin = current_path[-1] if current_path else warehouse_point
            if (
                not current_path
                and carried_rows
                and last_completed
                and (
                    latest_carried_pickup is None
                    or latest_carried_pickup <= last_completed
                )
            ):
                plan_origin = last_point
            planned_path = [plan_origin]
            for row in carried_rows:
                points = location_points(row["id"])
                if points:
                    planned_path.extend(points)
                    planned_order_ids.append(row["id"])
            if pending_rows and planned_path[-1] != warehouse_point:
                planned_path.append(warehouse_point)
            for row in pending_rows:
                points = location_points(row["id"])
                if points:
                    planned_path.extend(points)
                    planned_order_ids.append(row["id"])
            if len(planned_path) > 1 and planned_path[-1] != warehouse_point:
                planned_path.append(warehouse_point)
            if len(planned_path) < 2:
                planned_path = []
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
            "completed_paths": completed_paths,
            "current_path": current_path,
            "current_started_at": current_row["started_at"] if current_row else None,
            "return_path": return_path,
            "planned_path": planned_path,
            "planned_order_ids": planned_order_ids,
            "return_started_at": (
                last_completed.isoformat() if return_path and last_completed else None
            ),
        })

    timeline = []
    for event in events:
        order = order_by_id.get(event.order_id)
        if not order:
            continue
        item = _timeline_item(event, order)
        if item and (courier_id is None or item["courier_id"] == courier_id):
            timeline.append(item)
    timeline.sort(key=lambda item: (item["timestamp"], item["order_number"]))

    mapped_order_ids = {stop["order_id"] for stop in stops}
    unmapped_orders: list[dict[str, Any]] = []
    for order in relevant:
        row = row_by_id[order.id]
        location_values = (
            (
                1,
                order.latitude,
                order.longitude,
                order.address_text,
                order.location_url,
            ),
            (
                2,
                order.second_latitude,
                order.second_longitude,
                order.second_address_text,
                order.second_location_url,
            ),
        )
        added = False
        for number, latitude, longitude, address_text, location_url in location_values:
            if latitude is not None and longitude is not None:
                continue
            if number == 2 and not (address_text or location_url):
                continue
            if number == 1 and not (address_text or location_url) and order.id in mapped_order_ids:
                continue
            unmapped_orders.append({
                **row,
                "location_number": number,
                "address": short_address(order, number),
                "map_url": _order_map_url(order, number),
            })
            added = True
        if not added and order.id not in mapped_order_ids:
            unmapped_orders.append({
                **row,
                "location_number": 1,
                "address": short_address(order),
                "map_url": _order_map_url(order),
            })

    def breakdown(field: str, *, labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            raw_name = str(row.get(field) or "—")
            grouped[(labels or {}).get(raw_name, raw_name)].append(row)
        result = []
        for name, grouped_rows in grouped.items():
            amount_usd = sum(
                row["amount_usd"] for row in grouped_rows if row["created_today"]
            )
            amount_uzs = sum(
                row["amount_uzs"] for row in grouped_rows if row["created_today"]
            )
            delivered_amount_usd = sum(
                row["amount_usd"] for row in grouped_rows if row["completed_today"]
            )
            delivered_amount_uzs = sum(
                row["amount_uzs"] for row in grouped_rows if row["completed_today"]
            )
            result.append({
                "name": name,
                "orders": len(grouped_rows),
                "created": sum(1 for row in grouped_rows if row["created_today"]),
                "completed": sum(1 for row in grouped_rows if row["completed_today"]),
                "cancelled": sum(1 for row in grouped_rows if row["cancelled_today"]),
                "amount_text": _money_text(amount_usd, amount_uzs),
                "delivered_amount_text": _money_text(
                    delivered_amount_usd,
                    delivered_amount_uzs,
                ),
            })
        return sorted(result, key=lambda item: (-item["orders"], item["name"]))

    payment_labels = {
        PAID_AT_ASSEMBLY: payment_label(PAID_AT_ASSEMBLY),
        COLLECT_ON_DELIVERY: payment_label(COLLECT_ON_DELIVERY),
    }
    breakdowns = {
        "managers": breakdown("manager_name"),
        "sellers": breakdown("seller_name"),
        "payments": breakdown("payment_status", labels=payment_labels),
    }

    courier_summaries = []
    for courier in COURIERS:
        courier_rows = [row for row in rows if row["courier_id"] == courier.user_id]
        courier_durations = [
            row["duration_minutes"]
            for row in courier_rows
            if row["duration_minutes"] is not None
        ]
        courier_response_durations = [
            row["response_minutes"]
            for row in courier_rows
            if row["response_minutes"] is not None
        ]
        courier_warehouse_durations = [
            row["warehouse_minutes"]
            for row in courier_rows
            if row["warehouse_minutes"] is not None
        ]
        courier_assignment_pickup_durations = [
            row["assignment_pickup_minutes"]
            for row in courier_rows
            if row["assignment_pickup_minutes"] is not None
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
            "average_response_minutes": (
                round(sum(courier_response_durations) / len(courier_response_durations))
                if courier_response_durations
                else None
            ),
            "average_warehouse_minutes": (
                round(sum(courier_warehouse_durations) / len(courier_warehouse_durations))
                if courier_warehouse_durations
                else None
            ),
            "average_assignment_pickup_minutes": (
                round(
                    sum(courier_assignment_pickup_durations)
                    / len(courier_assignment_pickup_durations)
                )
                if courier_assignment_pickup_durations
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
            "mapped_orders": len(mapped_order_ids),
            "unmapped": len(unmapped_orders),
            "average_minutes": round(sum(durations) / len(durations)) if durations else None,
            "average_pickup_minutes": (
                round(sum(pickup_durations) / len(pickup_durations))
                if pickup_durations
                else None
            ),
            "average_response_minutes": (
                round(sum(response_durations) / len(response_durations))
                if response_durations
                else None
            ),
            "average_warehouse_minutes": (
                round(sum(warehouse_durations) / len(warehouse_durations))
                if warehouse_durations
                else None
            ),
            "average_assignment_pickup_minutes": (
                round(
                    sum(assignment_pickup_durations)
                    / len(assignment_pickup_durations)
                )
                if assignment_pickup_durations
                else None
            ),
            # Backwards-compatible amount fields now have one precise meaning:
            # value of orders created on the selected day.
            "amount_usd": created_usd,
            "amount_uzs": created_uzs,
            "amount_text": _money_text(created_usd, created_uzs),
            "created_value_usd": created_usd,
            "created_value_uzs": created_uzs,
            "created_value_text": _money_text(created_usd, created_uzs),
            "delivered_value_usd": delivered_usd,
            "delivered_value_uzs": delivered_uzs,
            "delivered_value_text": _money_text(delivered_usd, delivered_uzs),
            "received_usd": received_usd,
            "received_uzs": received_uzs,
            "received_text": _money_text(received_usd, received_uzs),
            "paid_at_assembly_count": paid_at_assembly_count,
            "paid_at_assembly_usd": paid_usd,
            "paid_at_assembly_uzs": paid_uzs,
            "paid_at_assembly_text": _money_text(paid_usd, paid_uzs),
            "missing_received_confirmation": missing_received_confirmation,
        },
        "couriers": courier_summaries,
        "orders": rows,
        "breakdowns": breakdowns,
        "unmapped_orders": unmapped_orders,
        "stops": stops,
        "routes": routes,
        "timeline": timeline,
        "warehouse": {
            "name": "Склад · Малика",
            "latitude": 41.337420,
            "longitude": 69.272104,
        },
    }
