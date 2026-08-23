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
