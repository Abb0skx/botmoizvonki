from html import escape
from urllib.parse import urlencode

from app.models import Order
from .parsers import display_phone


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


def location(order: Order) -> str:
    details: list[str] = []
    if order.district:
        details.append(f"🏙 Район: {escape(order.district)}")
    if order.mahalla and order.mahalla != order.district:
        details.append(f"🏘 Махалля: {escape(order.mahalla)}")
    if order.address_text:
        details.append(f"🏠 Адрес: {escape(order.address_text)}")
        details.append("<i>Адресные данные: OpenStreetMap</i>")

    links = []
    route_url = yandex_route_url(order)
    map_url = yandex_map_url(order)
    if route_url:
        links.append(f'<a href="{escape(route_url, quote=True)}">🧭 Построить маршрут</a>')
    if map_url:
        links.append(f'<a href="{escape(map_url, quote=True)}">🗺 Открыть на карте</a>')
    if not details and not links:
        return "—"
    return "\n".join([*details, *links])


def manager_card(order: Order, *, sent: bool = False) -> str:
    title = "✅ <b>Заказ отправлен курьерам</b>" if sent else "✅ <b>Заказ создан</b>"
    return (
        f"{title}\n\n🚚 <b>Заказ №{order.order_number}</b>\n\n"
        f"👤 Продавец:\n{escape(order.seller_name or '—')}\n\n"
        f"📱 Телефон:\n{display_phone(order.client_phone)}\n\n📦 Товар:\n{escape(order.product)}\n\n"
        f"💰 Сумма:\n{money(order.amount_usd, order.amount_uzs)}\n\n📍 Локация:\n{location(order)}\n\n"
        f"🕒 Время:\n{escape(order.delivery_time or '—')}\n\n💬 Комментарий:\n{escape(order.comment or '—')}"
    )


def courier_card(order: Order, state: str = "") -> str:
    heading = f"{state}\n\n" if state else ""
    return (
        f"{heading}🚚 <b>Заказ №{order.order_number}</b>\n\n📱 {display_phone(order.client_phone)}\n\n"
        f"📦 {escape(order.product)}\n\n💰 {money(order.amount_usd, order.amount_uzs)}\n\n"
        f"📍 {location(order)}\n\n🕒 {escape(order.delivery_time or '—')}\n\n"
        f"💬 {escape(order.comment or '—')}\n\n👤 Продавец:\n{escape(order.seller_name or '—')}\n\n"
        f"🧑‍💼 Создал заказ:\n{escape(order.manager_name)}"
    )


def completed_card(order: Order, local_time: str) -> str:
    return (
        f"✅ <b>Заказ №{order.order_number} доставлен</b>\n\n📸 Фото получено\n\n"
        f"💰 Получено:\n{money(order.received_usd, order.received_uzs)}\n\n"
        f"👤 Курьер:\n{escape(order.courier_name or '—')}\n\n🕒 Время:\n{local_time}"
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
        return "🗺 <b>Активных заказов сейчас нет.</b>", None

    lines = [f"🗺 <b>Активные заказы: {len(orders)}</b>"]
    for order in orders[:20]:
        area = (order.mahalla or order.district or "район не определён")[:100]
        point_url = yandex_map_url(order)
        model = escape(order.product[:100])
        model_text = f'<a href="{escape(point_url, quote=True)}">{model}</a>' if point_url else model
        marker = f"📌 {marker_numbers[order.id]} · " if order.id in marker_numbers else ""
        lines.append(
            f"\n{marker}<b>№{order.order_number}</b> · {model_text}\n"
            f"{STATUS_LABELS.get(order.status, order.status)} · {escape(area)} · "
            f"{escape(order.seller_name or 'продавец не указан')}"
        )
    if len(orders) > 20:
        lines.append(f"\n…и ещё {len(orders) - 20}")

    if not located:
        return "\n".join(lines), None
    center_lat = sum(order.latitude for order in visible) / len(visible)
    center_lon = sum(order.longitude for order in visible) / len(visible)
    span = max(
        max(order.latitude for order in visible) - min(order.latitude for order in visible),
        max(order.longitude for order in visible) - min(order.longitude for order in visible),
    )
    zoom = 14 if span < 0.02 else 12 if span < 0.05 else 10 if span < 0.15 else 8 if span < 0.5 else 6
    points = "~".join(
        f"{order.longitude:.6f},{order.latitude:.6f},pm2rdm{index}"
        for index, order in enumerate(visible, start=1)
    )
    map_url = "https://yandex.uz/maps/?" + urlencode({
        "ll": f"{center_lon:.6f},{center_lat:.6f}",
        "z": str(zoom),
        "pt": points,
    })
    lines.append(f'\n<a href="{escape(map_url, quote=True)}">📌 Показать все точки на одной карте</a>')
    if len(located) > 25:
        lines.append("На общей карте показаны первые 25 точек.")
    return "\n".join(lines), map_url
