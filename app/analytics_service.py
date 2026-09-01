from __future__ import annotations

import calendar
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app.database import OrderRepository
from app.models import Order, OrderEvent
from app.utils.couriers import COURIERS, COURIERS_BY_ID
from app.utils.geocoding import extract_text_address, normalize_district
from app.stats_service import (
    TASHKENT,
    _courier_color,
    _courier_id,
    _status_changed,
    local_datetime,
)


MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
WEEKDAY_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTH_NAMES = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


def parse_month(value: str | None, *, today: date | None = None) -> date:
    current = today or datetime.now(TASHKENT).date()
    cleaned = (value or current.strftime("%Y-%m")).strip()
    match = MONTH_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("Неверный месяц. Используйте формат ГГГГ-ММ.")
    year, month = map(int, match.groups())
    if not 2024 <= year <= current.year + 1 or not 1 <= month <= 12:
        raise ValueError("Месяц находится вне доступного диапазона.")
    return date(year, month, 1)


def parse_week(value: str | None, *, today: date | None = None) -> date:
    current = today or datetime.now(TASHKENT).date()
    default = current - timedelta(days=current.weekday())
    cleaned = (value or f"{default.isocalendar().year}-W{default.isocalendar().week:02d}").strip()
    match = WEEK_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("Неверная неделя. Используйте формат ГГГГ-WНН.")
    try:
        monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as error:
        raise ValueError("Такой недели не существует.") from error
    if monday.year < 2023 or monday > current + timedelta(days=370):
        raise ValueError("Неделя находится вне доступного диапазона.")
    return monday


def _month_shift(value: date, delta: int) -> date:
    total = value.year * 12 + value.month - 1 + delta
    return date(total // 12, total % 12 + 1, 1)


def _week_value(monday: date) -> str:
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _final_delivery(
    order: Order,
    events: list[OrderEvent],
) -> tuple[datetime, int | None, str] | None:
    """Return the current valid completion and its immutable courier snapshot."""
    if order.status != "completed":
        return None
    completion = next(
        (
            event
            for event in reversed(events)
            if _status_changed(event) and event.to_status == "completed"
        ),
        None,
    )
    completed_at = local_datetime(order.delivered_at)
    if completed_at is None and completion is not None:
        completed_at = local_datetime(completion.created_at)
    if completed_at is None:
        return None

    courier_id: int | None = None
    courier_name: str | None = None
    if completion is not None:
        if completion.courier_id is not None:
            courier_id = completion.courier_id
            courier_name = completion.courier_name
        elif completion.actor_role == "courier" and completion.actor_id is not None:
            courier_id = completion.actor_id
            courier_name = completion.actor_name
    if courier_id is None:
        courier_id = _courier_id(order)
        courier_name = order.courier_name or order.assigned_courier_name
    configured = COURIERS_BY_ID.get(courier_id)
    courier_name = (
        courier_name
        or (configured.name if configured else None)
        or (f"Курьер {courier_id}" if courier_id is not None else "Не назначен")
    )
    return completed_at, courier_id, courier_name


def _forecast(
    target: date,
    daily_counts: Counter[date],
    history_start: date | None,
    *,
    as_of: date,
) -> dict[str, Any]:
    # For a future target, walk back to the latest already observed occurrence
    # of the same weekday. This prevents future zeroes from depressing forecasts
    # when a manager opens a later month or week.
    latest_same_weekday = target - timedelta(days=7)
    while latest_same_weekday > as_of:
        latest_same_weekday -= timedelta(days=7)
    same_weekday_dates = [
        latest_same_weekday - timedelta(days=7 * offset)
        for offset in range(8)
    ]
    same_weekday = [
        daily_counts[day]
        for day in same_weekday_dates
        if history_start is not None and day >= history_start
    ]
    recent_days = [
        daily_counts[as_of - timedelta(days=offset)]
        for offset in range(28)
        if history_start is not None and as_of - timedelta(days=offset) >= history_start
    ]
    baseline = (
        sum(same_weekday) / len(same_weekday)
        if same_weekday
        else (sum(recent_days) / len(recent_days) if recent_days else 0.0)
    )
    recent_window = recent_days[:14]
    previous_window = recent_days[14:]
    recent = sum(recent_window) / len(recent_window) if recent_window else 0.0
    previous = sum(previous_window) / len(previous_window) if previous_window else recent
    trend = (recent + 0.25) / (previous + 0.25)
    trend = max(0.65, min(1.5, trend))
    expected = max(0.0, baseline * trend)
    probability = (1 - math.exp(-expected)) * 100
    sample = len(same_weekday)
    confidence = "низкая" if sample < 4 else ("средняя" if sample < 8 else "выше средней")
    return {
        "expected": round(expected, 1),
        "probability": round(probability),
        "sample": sample,
        "confidence": confidence,
    }


def build_delivery_analytics(
    repo: OrderRepository,
    *,
    month: str | None = None,
    week: str | None = None,
    courier_id: int | None = None,
) -> dict[str, Any]:
    today = datetime.now(TASHKENT).date()
    month_start = parse_month(month, today=today)
    week_start = parse_week(week, today=today)
    orders, events = repo.list_orders_with_events()
    events_by_order: dict[int, list[OrderEvent]] = defaultdict(list)
    for event in events:
        events_by_order[event.order_id].append(event)

    def analytics_courier(order) -> int | None:
        events = [
            event
            for event in events_by_order.get(order.id, [])
            if event.courier_id is not None
            or (event.actor_role == "courier" and event.actor_id is not None)
        ]
        completed = [
            event
            for event in events
            if _status_changed(event) and event.to_status == "completed"
        ]
        if completed:
            # The first actual fulfilment is immutable attribution for the
            # order's creation-period analytics, even if it is later reopened.
            return completed[0].courier_id or completed[0].actor_id
        lifecycle = [
            event
            for event in events
            if "assigned_courier_id" in event.changed_fields
            or event.event_type == "courier_read"
            or (
                _status_changed(event)
                and event.to_status in {"picked_up", "on_way", "cancelled"}
            )
        ]
        return (
            lifecycle[-1].courier_id or lifecycle[-1].actor_id
            if lifecycle
            else _courier_id(order)
        )

    created_orders = [
        (order, created.date())
        for order in orders
        if (created := local_datetime(order.created_at))
        and (courier_id is None or analytics_courier(order) == courier_id)
    ]
    daily_counts: Counter[date] = Counter(day for _, day in created_orders)
    history_start = min(daily_counts, default=None)
    # Today is still incomplete and must not be treated as a full low-volume
    # day in trend calculations.
    forecast_as_of = today - timedelta(days=1)

    month_days = calendar.monthrange(month_start.year, month_start.month)[1]
    month_dates = [month_start + timedelta(days=index) for index in range(month_days)]
    month_series = []
    for day in month_dates:
        forecast = (
            _forecast(day, daily_counts, history_start, as_of=forecast_as_of)
            if day > today
            else None
        )
        month_series.append({
            "date": day.isoformat(),
            "label": str(day.day),
            "weekday": WEEKDAY_NAMES[day.weekday()],
            "actual": daily_counts[day] if day <= today else None,
            "forecast": forecast["expected"] if forecast else None,
            "probability": forecast["probability"] if forecast else None,
        })

    previous_month = _month_shift(month_start, -1)
    next_month = _month_shift(month_start, 1)

    completed_by_courier: dict[int | None, Counter[date]] = {
        courier.user_id: Counter() for courier in COURIERS
    }
    courier_names: dict[int | None, str] = {
        courier.user_id: courier.name for courier in COURIERS
    }
    for order in orders:
        delivery = _final_delivery(order, events_by_order.get(order.id, []))
        if delivery is None:
            continue
        completed_at, completed_courier_id, completed_courier_name = delivery
        completed_day = completed_at.date()
        if not month_start <= completed_day < next_month or completed_day > today:
            continue
        completed_by_courier.setdefault(completed_courier_id, Counter())[completed_day] += 1
        courier_names.setdefault(completed_courier_id, completed_courier_name)

    configured_ids = [courier.user_id for courier in COURIERS]
    extra_ids = sorted(
        (value for value in completed_by_courier if value not in configured_ids),
        key=lambda value: (value is None, courier_names[value].casefold()),
    )
    courier_delivery_rows = []
    for completed_courier_id in [*configured_ids, *extra_ids]:
        counts = completed_by_courier[completed_courier_id]
        completed_total = sum(counts.values())
        courier_delivery_rows.append({
            "id": completed_courier_id,
            "name": courier_names[completed_courier_id],
            "color": _courier_color(completed_courier_id),
            "completed": completed_total,
            "active_days": sum(1 for count in counts.values() if count),
            "daily": [
                {
                    "date": day.isoformat(),
                    "day": day.day,
                    "weekday": day.weekday(),
                    "completed": counts[day],
                    "future": day > today,
                }
                for day in month_dates
            ],
        })
    monthly_delivery_total = sum(
        row["completed"] for row in courier_delivery_rows
    )
    for row in courier_delivery_rows:
        row["share"] = (
            round(row["completed"] / monthly_delivery_total * 100, 1)
            if monthly_delivery_total
            else 0
        )

    previous_month_days = calendar.monthrange(previous_month.year, previous_month.month)[1]
    is_current_month = (
        month_start.year == today.year and month_start.month == today.month
    )
    if is_current_month:
        comparison_days = min(today.day, previous_month_days)
        comparison_label = f"с первыми {comparison_days} дн. прошлого месяца"
    else:
        comparison_days = previous_month_days
        comparison_label = "с прошлым месяцем"
    current_total = sum(daily_counts[day] for day in month_dates)
    current_comparison_total = (
        sum(
            daily_counts[month_start + timedelta(days=index)]
            for index in range(comparison_days)
        )
        if is_current_month
        else current_total
    )
    previous_total = sum(
        daily_counts[previous_month + timedelta(days=index)]
        for index in range(comparison_days)
    )

    def district_name(order) -> str:
        stored = normalize_district(order.district)
        inferred = extract_text_address(order.address_text or "").get("district")
        return stored or inferred or "Район не определён"

    districts = Counter(
        district_name(order)
        for order, day in created_orders
        if month_start <= day < next_month
    )
    previous_districts = Counter(
        district_name(order)
        for order, day in created_orders
        if previous_month <= day < previous_month + timedelta(days=comparison_days)
    )
    comparison_districts = (
        Counter(
            district_name(order)
            for order, day in created_orders
            if month_start <= day < month_start + timedelta(days=comparison_days)
        )
        if is_current_month
        else districts
    )
    district_rows = [
        {
            "name": name,
            "orders": count,
            "previous": previous_districts.get(name, 0),
            "change": comparison_districts.get(name, 0) - previous_districts.get(name, 0),
            "share": round(count / current_total * 100, 1) if current_total else 0,
        }
        for name, count in districts.most_common()
    ]

    week_dates = [week_start + timedelta(days=index) for index in range(7)]
    week_series = []
    for day in week_dates:
        forecast = (
            _forecast(day, daily_counts, history_start, as_of=forecast_as_of)
            if day > today
            else None
        )
        week_series.append({
            "date": day.isoformat(),
            "label": WEEKDAY_NAMES[day.weekday()],
            "day_label": day.strftime("%d.%m"),
            "actual": daily_counts[day] if day <= today else None,
            "forecast": forecast["expected"] if forecast else None,
            "probability": forecast["probability"] if forecast else None,
        })

    weekday_history = []
    for weekday, name in enumerate(WEEKDAY_NAMES):
        samples = [
            daily_counts[today - timedelta(days=offset)]
            for offset in range(1, 85)
            if (today - timedelta(days=offset)).weekday() == weekday
            and (history_start is None or today - timedelta(days=offset) >= history_start)
        ]
        weekday_history.append({
            "label": name,
            "average": round(sum(samples) / len(samples), 1) if samples else 0,
            "sample": len(samples),
        })
    for item, history in zip(week_series, weekday_history):
        item["baseline"] = history["average"]
        item["baseline_sample"] = history["sample"]

    next_forecast = []
    for offset in range(1, 8):
        day = today + timedelta(days=offset)
        forecast = _forecast(day, daily_counts, history_start, as_of=forecast_as_of)
        next_forecast.append({
            "date": day.isoformat(),
            "label": f"{WEEKDAY_NAMES[day.weekday()]} · {day:%d.%m}",
            **forecast,
        })

    current_week_total = sum(daily_counts[day] for day in week_dates)
    previous_week_start = week_start - timedelta(days=7)
    previous_week_total = sum(
        daily_counts[previous_week_start + timedelta(days=index)]
        for index in range(7)
    )
    return {
        "generated_at": datetime.now(TASHKENT).isoformat(timespec="seconds"),
        "selected_courier_id": courier_id,
        "month": {
            "value": month_start.strftime("%Y-%m"),
            "label": f"{MONTH_NAMES[month_start.month]} {month_start.year}",
            "previous_value": previous_month.strftime("%Y-%m"),
            "next_value": next_month.strftime("%Y-%m"),
            "orders": current_total,
            "previous_orders": previous_total,
            "change": current_comparison_total - previous_total,
            "comparison_label": comparison_label,
            "series": month_series,
            "districts": district_rows,
            "courier_deliveries": {
                "total": monthly_delivery_total,
                "couriers": courier_delivery_rows,
            },
        },
        "week": {
            "value": _week_value(week_start),
            "label": f"{week_start:%d.%m}–{week_dates[-1]:%d.%m.%Y}",
            "previous_value": _week_value(previous_week_start),
            "next_value": _week_value(week_start + timedelta(days=7)),
            "orders": current_week_total,
            "previous_orders": previous_week_total,
            "change": current_week_total - previous_week_total,
            "series": week_series,
            "weekday_history": weekday_history,
        },
        "next_7_days": next_forecast,
        "forecast_note": (
            "Прогноз экспериментальный: среднее по таким же дням недели за последние "
            "8 недель с поправкой на динамику последних 28 завершённых дней. "
            "Текущий незавершённый день не занижает прогноз. Это вероятность, а не гарантия."
        ),
    }
