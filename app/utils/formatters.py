from html import escape
from app.models import Order
from .parsers import display_phone


def money(usd: int | None, uzs: int | None) -> str:
    parts = []
    if usd is not None:
        parts.append(f"{usd:,}$".replace(",", " "))
    if uzs is not None:
        parts.append(f"{uzs:,} сум".replace(",", " "))
    return "\n".join(parts) or "—"


def location(order: Order) -> str:
    if order.location_url:
        return f'<a href="{escape(order.location_url, quote=True)}">Открыть карту</a>'
    if order.latitude is not None:
        return f'<a href="https://maps.google.com/?q={order.latitude},{order.longitude}">Открыть карту</a>'
    return "—"


def manager_card(order: Order) -> str:
    return (
        f"✅ <b>Заказ создан</b>\n\n🚚 <b>Заказ №{order.order_number}</b>\n\n"
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
        f"💬 {escape(order.comment or '—')}\n\n👤 Менеджер:\n{escape(order.manager_name)}"
    )


def completed_card(order: Order, local_time: str) -> str:
    return (
        f"✅ <b>Заказ №{order.order_number} доставлен</b>\n\n📸 Фото получено\n\n"
        f"💰 Получено:\n{money(order.received_usd, order.received_uzs)}\n\n"
        f"👤 Курьер:\n{escape(order.courier_name or '—')}\n\n🕒 Время:\n{local_time}"
    )
