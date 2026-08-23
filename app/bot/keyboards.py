from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.models import Order
from app.utils.formatters import (
    delivery_order_message_url,
    short_address,
    telegram_location_url,
)
from app.utils.parsers import display_phone
from app.utils.payments import PAYMENT_LABELS
from app.utils.sellers import SELLERS


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Новый заказ"), KeyboardButton("📋 Активные заказы")],
            [KeyboardButton("📚 Все заказы")],
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


def edit_input_keyboard(field: str | None = None) -> ReplyKeyboardMarkup:
    """Keyboard for a single edit value with an always-visible cancel action."""
    rows: list[list[KeyboardButton]] = []
    if field == "seller":
        rows.extend([
            [KeyboardButton(SELLERS[0]), KeyboardButton(SELLERS[1])],
            [KeyboardButton(SELLERS[2]), KeyboardButton(SELLERS[3])],
        ])
    elif field in {"payment", "payment_status"}:
        rows.extend([[KeyboardButton(label)] for label in PAYMENT_LABELS.values()])
    rows.append([KeyboardButton("❌ Отменить изменение")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def review_keyboard(order_id: int, *, expanded: bool = False) -> InlineKeyboardMarkup:
    keyboard = _edit_rows(order_id) if expanded else [[
        InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_menu:{order_id}"),
    ]]
    if expanded:
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data=f"edit_close:{order_id}")])
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


def manager_sent_keyboard(order: Order, *, expanded: bool = False) -> InlineKeyboardMarkup:
    edit_rows = _edit_rows(order.id) if expanded else [[
        InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_menu:{order.id}"),
    ]]
    if expanded:
        edit_rows.append([InlineKeyboardButton("↩️ Назад", callback_data=f"edit_close:{order.id}")])
    return InlineKeyboardMarkup(
        _location_rows(order)
        + edit_rows
        + [[InlineKeyboardButton("🔄 Синхронизировать", callback_data=f"sync:{order.id}")]]
    )


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
    target_url = delivery_order_message_url(order)
    if not order.delivery_chat_id or not order.delivery_message_id:
        raise ValueError("The delivery-group message must be published before its location")

    def label(value: str) -> str:
        compact = " ".join(value.split())
        return compact if len(compact) <= 64 else compact[:63].rstrip() + "…"

    address = label(f"📍 {short_address(order, location_number)}")
    order_line = label(
        f"📦 {order.product} · №{order.order_number} · {order.seller_name or '—'}"
    )
    phone = label(f"📱 {display_phone(order.client_phone)}")

    def order_button(text: str) -> InlineKeyboardButton:
        if target_url:
            return InlineKeyboardButton(text, url=target_url)
        return InlineKeyboardButton(text, callback_data=f"location_order:{order.id}")

    return InlineKeyboardMarkup([
        [order_button(address)],
        [order_button(order_line)],
        [order_button(phone)],
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
        [InlineKeyboardButton("↩️ Назад", callback_data=f"undo_complete:{order.id}")],
        [InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order.id}")],
    ]
    return InlineKeyboardMarkup(rows)


def completed_keyboard(order: Order) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_location_rows(order) + [[
        InlineKeyboardButton("↩️ Назад", callback_data=f"undo_complete:{order.id}"),
    ]])


def courier_cancelled_keyboard(order: Order) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_location_rows(order) + [[
        InlineKeyboardButton("↩️ Назад", callback_data=f"undo_cancel:{order.id}"),
    ]])


def all_locations_keyboard(map_url: str | None) -> InlineKeyboardMarkup | None:
    if not map_url:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗺 Все локации на карте", url=map_url),
    ]])


def orders_page_keyboard(
    kind: str,
    page: int,
    total_pages: int,
    map_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Navigation for zero-based active/all order-list pages."""
    if kind not in {"active", "all"}:
        raise ValueError("kind must be 'active' or 'all'")
    if total_pages < 1:
        raise ValueError("total_pages must be positive")
    if page < 0 or page >= total_pages:
        raise ValueError("page is outside total_pages")

    rows: list[list[InlineKeyboardButton]] = []
    if map_url:
        rows.append([
            InlineKeyboardButton("🗺 Все локации на карте", url=map_url),
        ])

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            "⬅️",
            callback_data=f"orders_page:{kind}:{page - 1}",
        ))
    navigation.append(InlineKeyboardButton(
        f"{page + 1}/{total_pages}",
        callback_data=f"orders_page:{kind}:{page}",
    ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            "➡️",
            callback_data=f"orders_page:{kind}:{page + 1}",
        ))
    rows.append(navigation)
    return InlineKeyboardMarkup(rows)


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Пропустить")]], resize_keyboard=True, one_time_keyboard=True)


def second_location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Продолжить без второй локации")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
