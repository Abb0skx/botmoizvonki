from html import escape
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.models import Order, OrderEvent
from .parsers import display_phone
from .payments import PAID_AT_ASSEMBLY


def money(usd: int | None, uzs: int | None) -> str:
    parts = []
    if usd is not None:
        parts.append(f"{usd:,}$".replace(",", " "))
    if uzs is not None:
        parts.append(f"{uzs:,} сум".replace(",", " "))
    return "\n".join(parts) or "—"


def yandex_map_url(order: Order) -> str | None:
    if order.latitude is None or order.longitude is None:
        return order.location_url
    query = urlencode({
        "ll": f"{order.longitude:.6f},{order.latitude:.6f}",
        "z": "17",
        "pt": f"{order.longitude:.6f},{order.latitude:.6f},pm2rdm",
    })
    return f"https://yandex.uz/maps/?{query}"


def yandex_route_url(order: Order) -> str | None:
    if order.latitude is None or order.longitude is None:
        return None
    query = urlencode({"rtext": f"~{order.latitude:.6f},{order.longitude:.6f}", "rtt": "auto"})
    return f"https://yandex.uz/maps/?{query}"


def telegram_message_url(chat_id: int | None, message_id: int | None) -> str | None:
    """Build a private supergroup/channel message link from Telegram numeric IDs."""
    if chat_id is None or message_id is None or message_id <= 0:
        return None
    raw_chat_id = str(chat_id)
    if not raw_chat_id.startswith("-100"):
        return None
    return f"https://t.me/c/{raw_chat_id[4:]}/{message_id}"


def delivery_order_message_url(order: Order) -> str | None:
    """Return a supported direct link to the canonical delivery-group post.

    Telegram only exposes message links for channels and supergroups. Basic
    groups use a callback fallback handled by the bot instead.
    """
    if not order.delivery_chat_id or not order.delivery_message_id:
        return None
    public_link = telegram_message_url(
        order.delivery_chat_id,
        order.delivery_message_id,
    )
    return public_link


def telegram_location_url(order: Order, location_number: int = 1) -> str | None:
    if location_number == 2:
        return telegram_message_url(
            order.second_location_chat_id,
            order.second_location_message_id,
        )
    return telegram_message_url(order.location_chat_id, order.location_message_id)


def _short_address_parts(
    address_text: str | None,
    district: str | None,
    mahalla: str | None,
) -> str:
    result: list[str] = []
    for value in (mahalla, district):
        cleaned = " ".join((value or "").split()).strip(" ,")
        if cleaned and cleaned.casefold() not in {item.casefold() for item in result}:
            result.append(cleaned)

    excluded = {
        "узбекистан", "o‘zbekiston", "ташкент", "toshkent",
        "город ташкент", "toshkent shahri",
    }
    for part in (address_text or "").split(","):
        cleaned = " ".join(part.split()).strip(" ,")
        if not cleaned or cleaned.casefold() in excluded or cleaned.isdigit():
            continue
        if any(cleaned.casefold() in existing.casefold() or existing.casefold() in cleaned.casefold() for existing in result):
            continue
        result.append(cleaned)
        if len(result) >= 3:
            break
    value = ", ".join(result) or "Адрес не определён"
    return value[:180]


def short_address(order: Order, location_number: int = 1) -> str:
    if location_number == 2:
        return _short_address_parts(
            order.second_address_text,
            order.second_district,
            order.second_mahalla,
        )
    return _short_address_parts(order.address_text, order.district, order.mahalla)


def _tashkent_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    tashkent = ZoneInfo("Asia/Tashkent")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tashkent)
    return parsed.astimezone(tashkent)


def daily_delivery_report(
    orders: list[Order],
    report_day: date,
    events: list[OrderEvent] | None = None,
) -> list[str]:
    """Build compact chronological Telegram messages for one local day."""
    timeline: list[tuple[datetime, str, int]] = []
    arrived_orders: set[int] = set()
    delivered_orders: set[int] = set()
    event_created_orders: set[int] = set()
    event_read_orders: set[int] = set()
    event_picked_orders: set[int] = set()
    event_started_orders: set[int] = set()
    event_delivered_orders: set[int] = set()
    orders_by_id = {order.id: order for order in orders}

    for event in events or []:
        occurred = _tashkent_datetime(event.created_at)
        order = orders_by_id.get(event.order_id)
        if not occurred or occurred.date() != report_day or not order:
            continue
        product = escape(" ".join(order.product.split())[:80])
        seller = escape(order.seller_name or "—")
        courier = escape(
            event.actor_name
            or order.courier_name
            or order.assigned_courier_name
            or "Курьер не назначен"
        )
        address = escape(short_address(order))

        if event.event_type == "order_created":
            event_created_orders.add(order.id)
            arrived_orders.add(order.id)
            line = (
                f"📥 <b>{occurred:%H:%M}</b> · Пришёл заказ №{order.order_number}"
                f" · {product} · продавец {seller}"
            )
            priority = 0
        elif event.event_type == "courier_read":
            event_read_orders.add(order.id)
            line = (
                f"👀 <b>{occurred:%H:%M}</b> · {courier} прочитал заказ"
                f" №{order.order_number} и выехал на склад"
            )
            priority = 1
        elif event.from_status == "completed" and event.to_status in {"pending", "picked_up", "on_way"}:
            line = (
                f"↩️ <b>{occurred:%H:%M}</b> · {courier} отменил завершение"
                f" заказа №{order.order_number}"
            )
            priority = 4
        elif event.from_status == "cancelled" and event.to_status in {"pending", "picked_up"}:
            line = (
                f"↩️ <b>{occurred:%H:%M}</b> · Заказ №{order.order_number}"
                " возвращён в доставку"
            )
            priority = 4
        elif event.from_status == "picked_up" and event.to_status == "pending":
            line = (
                f"↩️ <b>{occurred:%H:%M}</b> · Отметка «товар забран» снята"
                f" с заказа №{order.order_number}"
            )
            priority = 4
        elif event.from_status == "on_way" and event.to_status in {"pending", "picked_up"}:
            line = (
                f"↩️ <b>{occurred:%H:%M}</b> · {courier} отменил выезд"
                f" к заказу №{order.order_number}"
            )
            priority = 4
        elif event.to_status == "picked_up":
            event_picked_orders.add(order.id)
            pickup_courier = escape(
                order.courier_name
                or order.assigned_courier_name
                or "Курьер не назначен"
            )
            line = (
                f"📦 <b>{occurred:%H:%M}</b> · {pickup_courier} забрал товар"
                f" для заказа №{order.order_number}"
            )
            priority = 1
        elif event.to_status == "on_way":
            event_started_orders.add(order.id)
            line = (
                f"🚗 <b>{occurred:%H:%M}</b> · {courier} выехал к заказу"
                f" №{order.order_number} → {address}"
            )
            priority = 2
        elif event.to_status == "completed":
            event_delivered_orders.add(order.id)
            delivered_orders.add(order.id)
            line = (
                f"✅ <b>{occurred:%H:%M}</b> · {courier} приехал и доставил заказ"
                f" №{order.order_number} · {address}"
            )
            priority = 3
        elif event.to_status == "cancelled":
            line = (
                f"❌ <b>{occurred:%H:%M}</b> · {courier} отменил заказ"
                f" №{order.order_number}"
            )
            priority = 4
        else:
            continue
        timeline.append((occurred, line, priority))

    for order in orders:
        product = escape(" ".join(order.product.split())[:80])
        seller = escape(order.seller_name or "—")
        courier = escape(order.courier_name or order.assigned_courier_name or "Курьер не назначен")
        address = escape(short_address(order))

        created = _tashkent_datetime(order.created_at)
        if created and created.date() == report_day and order.id not in event_created_orders:
            arrived_orders.add(order.id)
            timeline.append((
                created,
                f"📥 <b>{created:%H:%M}</b> · Пришёл заказ №{order.order_number}"
                f" · {product} · продавец {seller}",
                0,
            ))

        read_at = _tashkent_datetime(order.courier_read_at)
        if read_at and read_at.date() == report_day and order.id not in event_read_orders:
            timeline.append((
                read_at,
                f"👀 <b>{read_at:%H:%M}</b> · {courier} прочитал заказ"
                f" №{order.order_number} и выехал на склад",
                1,
            ))

        picked_up = _tashkent_datetime(order.picked_up_at)
        if picked_up and picked_up.date() == report_day and order.id not in event_picked_orders:
            timeline.append((
                picked_up,
                f"📦 <b>{picked_up:%H:%M}</b> · {courier} забрал товар"
                f" для заказа №{order.order_number}",
                1,
            ))

        started = _tashkent_datetime(order.time_started)
        if started and started.date() == report_day and order.id not in event_started_orders:
            timeline.append((
                started,
                f"🚗 <b>{started:%H:%M}</b> · {courier} выехал к заказу"
                f" №{order.order_number} → {address}",
                2,
            ))

        delivered = _tashkent_datetime(order.delivered_at)
        if delivered and delivered.date() == report_day and order.id not in event_delivered_orders:
            delivered_orders.add(order.id)
            timeline.append((
                delivered,
                f"✅ <b>{delivered:%H:%M}</b> · {courier} приехал и доставил заказ"
                f" №{order.order_number} · {address}",
                3,
            ))

    timeline.sort(key=lambda item: (item[0], item[2], item[1]))
    heading = (
        f"📅 <b>Хронология доставок за {report_day:%d.%m.%Y}</b>\n"
        f"📥 Пришло заказов: <b>{len(arrived_orders)}</b> · "
        f"✅ Доставлено: <b>{len(delivered_orders)}</b>"
    )
    if not timeline:
        return [heading + "\n\nЗа этот день действий пока нет."]

    messages: list[str] = []
    current = heading
    for _, line, _ in timeline:
        candidate = current + "\n\n" + line
        if len(candidate) > 3800 and current != heading:
            messages.append(current)
            current = f"📅 <b>{report_day:%d.%m.%Y} · продолжение</b>\n\n{line}"
        else:
            current = candidate
    messages.append(current)
    return messages


def locations_text(order: Order) -> str:
    lines = [f"📍 {escape(short_address(order))}"]
    if order.second_latitude is not None and order.second_longitude is not None:
        lines.append(f"📍 {escape(short_address(order, 2))}")
    return "\n".join(lines)


def amount_text(order: Order) -> str:
    title = "✅ Оплачено" if order.payment_status == PAID_AT_ASSEMBLY else "💰"
    return f"{title} {money(order.amount_usd, order.amount_uzs)}"


def phones_text(order: Order) -> str:
    phones = [display_phone(order.client_phone)]
    if order.client_phone_2 and order.client_phone_2 != order.client_phone:
        phones.append(display_phone(order.client_phone_2))
    return "\n".join(f"📱 {phone}" for phone in phones)


def _compact_order(order: Order, *, status: str | None = None) -> str:
    lines = [
        f"🚚 <b>Заказ №{order.order_number}</b> · <b>{escape(order.seller_name or '—')}</b>",
    ]
    if status:
        lines.append(f"🏷 {escape(status)}")
    if order.assigned_courier_name:
        lines.append(f"🚚 Курьер: {escape(order.assigned_courier_name)}")
    read_at = _local_datetime(order.courier_read_at)
    if read_at:
        reader = order.courier_name or order.assigned_courier_name or "Курьер"
        lines.append(f"👀 Прочитал: {escape(reader)} · {escape(read_at)}")
        if order.status == "pending":
            lines.append("🏬 Едет на склад за товаром")
    lines.extend([
        f"📦 {escape(order.product)}",
        amount_text(order),
        phones_text(order),
    ])
    if order.delivery_time:
        lines.append(f"🕒 {escape(order.delivery_time)}")
    if order.comment:
        lines.append(f"💬 {escape(order.comment)}")
    return "\n".join(lines) + f"\n\n{locations_text(order)}"


def manager_card(order: Order, *, sent: bool = False) -> str:
    status = STATUS_LABELS.get(order.status, order.status)
    return _compact_order(order, status=status)


def _local_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:40]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("Asia/Tashkent"))
    return parsed.strftime("%d.%m.%Y %H:%M")


def orders_channel_card(order: Order) -> str:
    """Full, durable manager overview shown in the shared order journal."""
    status = STATUS_LABELS.get(order.status, order.status)
    heading = (
        f"✅✅✅✅✅✅✅\n✅ <b>Заказ №{order.order_number}</b>"
        if order.status == "completed"
        else f"📋 <b>Заказ №{order.order_number}</b>"
    )
    lines = [
        heading,
        f"🏷 Статус: <b>{escape(status)}</b>",
        f"👤 Продавец: <b>{escape(order.seller_name or '—')}</b>",
        f"🧑‍💼 Создал: {escape(order.manager_name or '—')}",
        "",
        f"📦 Товар: <b>{escape(order.product)}</b>",
        f"💰 Сумма: {money(order.amount_usd, order.amount_uzs)}",
        (
            "✅ Оплата: <b>оплачено при сборе товара</b>"
            if order.payment_status == PAID_AT_ASSEMBLY
            else "💵 Оплата: при доставке"
        ),
        phones_text(order),
    ]
    if order.delivery_time:
        lines.append(f"🕒 Время доставки: {escape(order.delivery_time)}")
    if order.comment:
        lines.append(f"💬 Комментарий: {escape(order.comment)}")

    lines.append("")
    if order.assigned_courier_name:
        lines.append(f"🚚 Назначен курьер: <b>{escape(order.assigned_courier_name)}</b>")
    if order.courier_name:
        lines.append(f"👤 Принял заказ: {escape(order.courier_name)}")
    read_at = _local_datetime(order.courier_read_at)
    if read_at:
        reader = order.courier_name or order.assigned_courier_name or "Курьер"
        lines.append(f"👀 Заказ прочитал {escape(reader)}: {escape(read_at)}")
    picked_up = _local_datetime(order.picked_up_at)
    if picked_up:
        lines.append(f"📦 Курьер забрал товар: {escape(picked_up)}")
    started = _local_datetime(order.time_started)
    if started:
        lines.append(f"🚗 Начал доставку: {escape(started)}")
    delivered = _local_datetime(order.delivered_at)
    if delivered:
        lines.append(f"✅ Доставлено: {escape(delivered)}")
    if order.received_usd is not None or order.received_uzs is not None:
        lines.append(f"💵 Получено: {money(order.received_usd, order.received_uzs)}")

    lines.extend(["", f"📍 Основная: {escape(short_address(order))}"])
    if order.second_latitude is not None and order.second_longitude is not None:
        lines.append(f"📍 Дополнительная: {escape(short_address(order, 2))}")

    created = _local_datetime(order.created_at)
    updated = _local_datetime(order.updated_at)
    lines.append("")
    if created:
        lines.append(f"🗓 Создан: {escape(created)}")
    if updated:
        lines.append(f"🔄 Обновлён: {escape(updated)}")
    return "\n".join(lines)


def courier_card(order: Order, state: str = "") -> str:
    heading = f"{state}\n" if state else ""
    return heading + _compact_order(order)


def completed_card(order: Order, local_time: str) -> str:
    if order.payment_status == PAID_AT_ASSEMBLY:
        payment_result = f"✅ Оплачено: {money(order.amount_usd, order.amount_uzs)}"
    elif order.received_usd is not None or order.received_uzs is not None:
        payment_result = f"💰 Получено: {money(order.received_usd, order.received_uzs)}"
    else:
        payment_result = f"💰 {money(order.amount_usd, order.amount_uzs)}"
    photo_result = "\n📸 Фото получено" if order.delivery_photo else ""
    return (
        "✅✅✅✅✅✅✅\n"
        f"✅ <b>Заказ №{order.order_number} доставлен</b>{photo_result}\n"
        f"📦 {escape(order.product)}\n"
        f"{payment_result}\n"
        f"👤 {escape(order.courier_name or '—')}\n🕒 {local_time}"
    )


STATUS_LABELS = {
    "draft": "📝 Черновик",
    "pending": "🆕 Ожидает курьера",
    "picked_up": "📦 Товар у курьера",
    "on_way": "🚗 Курьер едет",
    "awaiting_photo": "📸 Подтверждается",
    "awaiting_amount": "💰 Ожидается сумма",
    "completed": "✅ Доставлен",
    "cancelled": "❌ Отменён",
}


def all_locations_card(orders: list[Order]) -> tuple[str, str | None]:
    if not orders:
        return "📦 <b>Активных заказов сейчас нет.</b>", None

    points: list[tuple[Order, int, float, float]] = []
    for order in orders:
        if order.latitude is not None and order.longitude is not None:
            points.append((order, 1, order.latitude, order.longitude))
        if order.second_latitude is not None and order.second_longitude is not None:
            points.append((order, 2, order.second_latitude, order.second_longitude))

    lines = [f"📦 <b>Активные заказы: {len(orders)}</b>"]
    if not points:
        lines.append("\nНет заказов с координатами.")
        return "\n".join(lines), None

    visible: list[tuple[Order, int, float, float]] = []
    legend: list[str] = []
    # Keep enough room for the heading and the final truncation note. Telegram
    # limits a text message to 4096 characters, including HTML markup.
    legend_budget = 3400
    for order, location_number, latitude, longitude in points[:25]:
        marker_number = len(visible) + 1
        location_name = "основная" if location_number == 1 else "доп."
        point_url = telegram_location_url(order, location_number)
        model = escape(" ".join(order.product.split())[:32])
        model_text = f'<a href="{escape(point_url, quote=True)}">{model}</a>' if point_url else model
        area = escape(short_address(order, location_number)[:55])
        owner = escape((order.seller_name or "—")[:18])
        line = (
            f"📌 <b>{marker_number}</b> · №{order.order_number} · {location_name} · {model_text}\n"
            f"{STATUS_LABELS.get(order.status, escape(order.status))} · {area} · {owner}"
        )
        if sum(len(item) + 2 for item in legend) + len(line) > legend_budget:
            break
        visible.append((order, location_number, latitude, longitude))
        legend.append(line)

    lines.extend(f"\n{line}" for line in legend)

    map_points = [(latitude, longitude) for _order, _number, latitude, longitude in visible]
    center_lat = sum(point[0] for point in map_points) / len(map_points)
    center_lon = sum(point[1] for point in map_points) / len(map_points)
    span = max(
        max(point[0] for point in map_points) - min(point[0] for point in map_points),
        max(point[1] for point in map_points) - min(point[1] for point in map_points),
    )
    zoom = 14 if span < 0.02 else 12 if span < 0.05 else 10 if span < 0.15 else 8 if span < 0.5 else 6
    encoded_points = "~".join(
        f"{longitude:.6f},{latitude:.6f},pm2rdm{index}"
        for index, (latitude, longitude) in enumerate(map_points, start=1)
    )
    map_url = "https://yandex.uz/maps/?" + urlencode({
        "ll": f"{center_lon:.6f},{center_lat:.6f}",
        "z": str(zoom),
        "pt": encoded_points,
    })
    if len(visible) < len(points):
        lines.append(f"\nНа общей карте показаны {len(visible)} из {len(points)} точек.")
    return "\n".join(lines), map_url
