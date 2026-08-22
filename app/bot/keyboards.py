from telegram import (
    CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.models import Order
from app.utils.formatters import telegram_location_url
from app.utils.payments import PAYMENT_LABELS
from app.utils.sellers import SELLERS


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Новый заказ"), KeyboardButton("📋 Мои заказы")],
            [KeyboardButton("📦 Все активные заказы")],
        ],
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


def manager_cancelled_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Назад", callback_data=f"manager_restore:{order_id}"),
    ]])


def _edit_rows(order_id: int) -> list[list[InlineKeyboardButton]]:
    rows = [
        [("👤 Изменить владельца", "seller"), ("💳 Изменить оплату", "payment_status")],
        [("✏️ Изменить товар", "product"), ("📞 Изменить номер", "phone")],
        [("📍 Основная локация", "location"), ("📍 Доп. локация", "second_location")],
        [("💰 Изменить сумму", "amount")],
        [("🕒 Изменить время", "delivery_time"), ("💬 Изменить комментарий", "comment")],
    ]
    return [[InlineKeyboardButton(label, callback_data=f"edit:{order_id}:{field}") for label, field in row] for row in rows]


def manager_sent_keyboard(order: Order) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_location_rows(order) + _edit_rows(order.id))


def _location_rows(order: Order) -> list[list[InlineKeyboardButton]]:
    row: list[InlineKeyboardButton] = []
    first_url = telegram_location_url(order)
    second_url = telegram_location_url(order, 2)
    if first_url:
        row.append(InlineKeyboardButton("📍 Локация", url=first_url))
    if second_url:
        row.append(InlineKeyboardButton("📍 Доп. локация", url=second_url))
    return [row] if row else []


def location_channel_keyboard(
    order: Order,
    marker: str = "🆕",
    location_number: int = 1,
) -> InlineKeyboardMarkup:
    product = " ".join(order.product.split())
    if len(product) > 36:
        product = product[:33] + "…"
    has_second = order.second_latitude is not None and order.second_longitude is not None
    location_label = f" · Локация {location_number}" if has_second else ""
    label = f"{marker} №{order.order_number}{location_label} · {product}"
    phone_digits = "".join(character for character in order.client_phone if character.isdigit())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            label,
            callback_data=f"location_info:{order.id}:{location_number}",
        )],
        [
            InlineKeyboardButton("📞 Позвонить", url=f"tg://resolve?phone={phone_digits}"),
            InlineKeyboardButton(
                "📋 Скопировать номер",
                copy_text=CopyTextButton(order.client_phone),
            ),
        ],
    ])


def courier_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    rows += [
        [InlineKeyboardButton("🚗 Еду к заказу", callback_data=f"onway:{order.id}")],
        [InlineKeyboardButton("✅ Доставлено", callback_data=f"complete:{order.id}"),
         InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
    ]
    return InlineKeyboardMarkup(rows)


def on_way_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    rows += [
        [InlineKeyboardButton("↩️ Отменить выезд", callback_data=f"undo_onway:{order.id}")],
        [InlineKeyboardButton("✅ Доставлено", callback_data=f"complete:{order.id}"),
         InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
    ]
    return InlineKeyboardMarkup(rows)


def delivery_pending_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    rows += [
        [InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
    ]
    return InlineKeyboardMarkup(rows)


def courier_cancelled_keyboard(order: Order) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_location_rows(order) + [[
        InlineKeyboardButton("↩️ Назад", callback_data=f"undo_cancel:{order.id}"),
    ]])


def all_locations_keyboard(map_url: str | None) -> InlineKeyboardMarkup | None:
    if not map_url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("📌 Открыть общую карту", url=map_url)]])


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Пропустить")]], resize_keyboard=True, one_time_keyboard=True)


def second_location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Продолжить без второй локации")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
