from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.models import Order
from app.utils.formatters import telegram_location_url, yandex_map_url, yandex_route_url
from app.utils.payments import PAYMENT_LABELS
from app.utils.sellers import SELLERS


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("➕ Новый заказ"), KeyboardButton("📋 Мои заказы")]],
        resize_keyboard=True,
    )


def seller_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(SELLERS[0]), KeyboardButton(SELLERS[1])],
         [KeyboardButton(SELLERS[2]), KeyboardButton(SELLERS[3])]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def payment_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label)] for label in PAYMENT_LABELS.values()],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    keyboard = _edit_rows(order_id)
    keyboard += [[InlineKeyboardButton("🚚 Отправить курьеру", callback_data=f"send:{order_id}")],
                 [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"manager_cancel:{order_id}")]]
    return InlineKeyboardMarkup(keyboard)


def _edit_rows(order_id: int) -> list[list[InlineKeyboardButton]]:
    rows = [
        [("👤 Изменить продавца", "seller"), ("💳 Изменить оплату", "payment_status")],
        [("✏️ Изменить товар", "product"), ("📞 Изменить номер", "phone")],
        [("📍 Изменить локацию", "location"), ("💰 Изменить сумму", "amount")],
        [("🕒 Изменить время", "delivery_time"), ("💬 Изменить комментарий", "comment")],
    ]
    return [[InlineKeyboardButton(label, callback_data=f"edit:{order_id}:{field}") for label, field in row] for row in rows]


def manager_sent_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_edit_rows(order_id))


def _location_rows(order: Order) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    telegram_url = telegram_location_url(order)
    if telegram_url:
        rows.append([InlineKeyboardButton("📍 Выбрать навигатор", url=telegram_url)])
    row = []
    route_url = yandex_route_url(order)
    map_url = yandex_map_url(order)
    if route_url:
        row.append(InlineKeyboardButton("🧭 Маршрут", url=route_url))
    if map_url:
        row.append(InlineKeyboardButton("🗺 Карта", url=map_url))
    if row:
        rows.append(row)
    return rows


def location_channel_keyboard(order: Order, marker: str = "🆕") -> InlineKeyboardMarkup:
    product = " ".join(order.product.split())
    if len(product) > 36:
        product = product[:33] + "…"
    label = f"{marker} №{order.order_number} · {product}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"location_info:{order.id}")
    ]])


def courier_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    rows += [
        [InlineKeyboardButton("🚗 Еду к заказу", callback_data=f"onway:{order.id}")],
        [InlineKeyboardButton("✅ Доставлено", callback_data=f"complete:{order.id}"),
         InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
        [InlineKeyboardButton("🗺 Все активные заказы", callback_data="map:all")],
    ]
    return InlineKeyboardMarkup(rows)


def on_way_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    rows += [
        [InlineKeyboardButton("↩️ Отменить выезд", callback_data=f"undo_onway:{order.id}")],
        [InlineKeyboardButton("✅ Доставлено", callback_data=f"complete:{order.id}"),
         InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
        [InlineKeyboardButton("🗺 Все активные заказы", callback_data="map:all")],
    ]
    return InlineKeyboardMarkup(rows)


def delivery_pending_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    rows += [
        [InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
        [InlineKeyboardButton("🗺 Все активные заказы", callback_data="map:all")],
    ]
    return InlineKeyboardMarkup(rows)


def all_locations_keyboard(map_url: str | None) -> InlineKeyboardMarkup | None:
    if not map_url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📌 Открыть общую карту", url=map_url)]])


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Пропустить")]], resize_keyboard=True, one_time_keyboard=True)
