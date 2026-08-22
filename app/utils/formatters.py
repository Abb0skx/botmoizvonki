from html import escape
from urllib.parse import urlencode

from app.models import Order
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


def locations_text(order: Order) -> str:
    lines = [f"📍 {escape(short_address(order))}"]
    if order.second_latitude is not None and order.second_longitude is not None:
        lines.append(f"📍 {escape(short_address(order, 2))}")
    return "\n".join(lines)


def amount_text(order: Order) -> str:
    title = "✅ Оплачено" if order.payment_status == PAID_AT_ASSEMBLY else "💰"
    return f"{title} {money(order.amount_usd, order.amount_uzs)}"


def _compact_order(order: Order) -> str:
    lines = [
        f"🚚 <b>Заказ №{order.order_number}</b> · <b>{escape(order.seller_name or '—')}</b>",
        f"📦 {escape(order.product)}",
        amount_text(order),
        f"📱 {display_phone(order.client_phone)}",
    ]
    if order.delivery_time:
        lines.append(f"🕒 {escape(order.delivery_time)}")
    if order.comment:
        lines.append(f"💬 {escape(order.comment)}")
    return "\n".join(lines) + f"\n\n{locations_text(order)}"


def manager_card(order: Order, *, sent: bool = False) -> str:
    return _compact_order(order)


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
        f"✅ <b>Заказ №{order.order_number} доставлен</b>{photo_result}\n"
        f"{payment_result}\n"
        f"👤 {escape(order.courier_name or '—')}\n🕒 {local_time}"
    )


STATUS_LABELS = {
    "pending": "🆕 Ожидает курьера",
    "on_way": "🚗 Курьер едет",
    "awaiting_photo": "📸 Подтверждается",
    "awaiting_amount": "💰 Ожидается сумма",
}


def all_locations_card(orders: list[Order]) -> tuple[str, str | None]:
    located = [order for order in orders if order.latitude is not None and order.longitude is not None]
    visible = located[:25]
    marker_numbers = {order.id: index for index, order in enumerate(visible, start=1)}
    if not orders:
        return "📦 <b>Активных заказов сейчас нет.</b>", None

    lines = [f"📦 <b>Активные заказы: {len(orders)}</b>"]
    for order in orders[:20]:
        area = short_address(order)
        point_url = telegram_location_url(order) or yandex_map_url(order)
        model = escape(order.product[:100])
        model_text = f'<a href="{escape(point_url, quote=True)}">{model}</a>' if point_url else model
        marker = f"📌 {marker_numbers[order.id]} · " if order.id in marker_numbers else ""
        lines.append(
            f"\n{marker}<b>№{order.order_number}</b> · {model_text}\n"
            f"{STATUS_LABELS.get(order.status, order.status)} · {escape(area)} · "
            f"{escape(order.seller_name or 'владелец не указан')}"
        )
        second_url = telegram_location_url(order, 2)
        if second_url:
            lines.append(f'<a href="{escape(second_url, quote=True)}">📍 Доп. локация</a>')
    if len(orders) > 20:
        lines.append(f"\n…и ещё {len(orders) - 20}")

    if not located:
        return "\n".join(lines), None
    map_points: list[tuple[float, float]] = []
    for order in visible:
        map_points.append((order.latitude, order.longitude))
        if order.second_latitude is not None and order.second_longitude is not None:
            map_points.append((order.second_latitude, order.second_longitude))
    map_points = map_points[:25]
    center_lat = sum(point[0] for point in map_points) / len(map_points)
    center_lon = sum(point[1] for point in map_points) / len(map_points)
    span = max(
        max(point[0] for point in map_points) - min(point[0] for point in map_points),
        max(point[1] for point in map_points) - min(point[1] for point in map_points),
    )
    zoom = 14 if span < 0.02 else 12 if span < 0.05 else 10 if span < 0.15 else 8 if span < 0.5 else 6
    points = "~".join(
        f"{longitude:.6f},{latitude:.6f},pm2rdm{index}"
        for index, (latitude, longitude) in enumerate(map_points, start=1)
    )
    map_url = "https://yandex.uz/maps/?" + urlencode({
        "ll": f"{center_lon:.6f},{center_lat:.6f}",
        "z": str(zoom),
        "pt": points,
    })
    lines.append(f'\n<a href="{escape(map_url, quote=True)}">📌 Показать все точки на одной карте</a>')
    total_points = sum(
        1 + int(order.second_latitude is not None and order.second_longitude is not None)
        for order in located
    )
    if total_points > 25:
        lines.append("На общей карте показаны первые 25 точек.")
    return "\n".join(lines), map_url
