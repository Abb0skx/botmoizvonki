from urllib.parse import urlencode

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.models import Order
from app.utils.formatters import (
    delivery_order_message_url,
    short_address,
    telegram_location_url,
)
from app.utils.parsers import display_phone
from app.utils.couriers import COURIERS
from app.utils.payments import PAYMENT_LABELS
from app.utils.sellers import SELLERS


DELIVERY_TIME_QUICK_CHOICES = (
    "Срочно 🚨🚨🚨",
    "2 часа",
    "2–3 часа",
)
DELIVERY_TIME_SLOTS = (
    "11:00", "11:30", "12:00", "12:30",
    "13:00", "13:30", "14:00", "14:30",
    "15:00", "15:30", "16:00", "16:30",
    "17:00", "17:30", "18:00", "18:30",
    "19:00", "19:30", "20:00", "20:30",
    "21:00", "21:30", "22:00",
)


def _delivery_time_rows(*, include_skip: bool) -> list[list[KeyboardButton]]:
    rows = [
        [KeyboardButton(DELIVERY_TIME_QUICK_CHOICES[0])],
        [
            KeyboardButton(DELIVERY_TIME_QUICK_CHOICES[1]),
            KeyboardButton(DELIVERY_TIME_QUICK_CHOICES[2]),
        ],
    ]
    rows.extend([
        [KeyboardButton(value) for value in DELIVERY_TIME_SLOTS[index:index + 4]]
        for index in range(0, len(DELIVERY_TIME_SLOTS), 4)
    ])
    if include_skip:
        rows.append([KeyboardButton("Пропустить")])
    return rows


def delivery_time_keyboard() -> ReplyKeyboardMarkup:
    """Quick delivery-time presets while keeping free text available."""
    return ReplyKeyboardMarkup(
        _delivery_time_rows(include_skip=True),
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или напишите время текстом",
    )


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


def text_location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📝 Локация текстом")]],
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
    elif field == "delivery_time":
        rows.extend(_delivery_time_rows(include_skip=True))
    rows.append([KeyboardButton("❌ Отменить изменение")])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        input_field_placeholder=(
            "Или напишите время текстом" if field == "delivery_time" else None
        ),
    )


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
        + _pickup_rows(order)
        + [[InlineKeyboardButton("🚚 Изменить курьера", callback_data=f"courier_menu:{order.id}")]]
        + edit_rows
        + [[InlineKeyboardButton("🔄 Синхронизировать", callback_data=f"sync:{order.id}")]]
    )


def courier_selection_keyboard(order: Order, *, source: str = "manager") -> InlineKeyboardMarkup:
    if source not in {"manager", "orders_channel"}:
        raise ValueError("unsupported courier selection source")
    prefix = "control_courier" if source == "orders_channel" else "courier"
    rows = []
    for courier in COURIERS:
        selected = "✅ " if order.assigned_courier_id == courier.user_id else "🚚 "
        rows.append([InlineKeyboardButton(
            selected + courier.name,
            callback_data=f"{prefix}_assign:{order.id}:{courier.user_id}",
        )])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data=f"{prefix}_close:{order.id}")])
    return InlineKeyboardMarkup(rows)


def _location_rows(order: Order) -> list[list[InlineKeyboardButton]]:
    row: list[InlineKeyboardButton] = []
    first_url = telegram_location_url(order) or order.location_url
    second_url = telegram_location_url(order, 2) or order.second_location_url
    if first_url:
        row.append(InlineKeyboardButton("📍 Локация", url=first_url))
    if second_url:
        row.append(InlineKeyboardButton("📍 Доп. локация", url=second_url))
    return [row] if row else []


def _pickup_rows(order: Order) -> list[list[InlineKeyboardButton]]:
    courier_name = order.assigned_courier_name or order.courier_name or "Курьер"
    if (
        order.status == "pending"
        and order.assigned_courier_id
        and order.courier_read_at
    ):
        return [[InlineKeyboardButton(
            f"📦 {courier_name} забрал товар",
            callback_data=f"pickup:{order.id}",
        )]]
    if order.status == "picked_up":
        return [[InlineKeyboardButton(
            f"↩️ Отменить: {courier_name} забрал товар",
            callback_data=f"undo_pickup:{order.id}",
        )]]
    return []


def log_location_keyboard(order: Order) -> InlineKeyboardMarkup | None:
    """Open the native location post from a compact Log notification."""
    buttons: list[InlineKeyboardButton] = []
    first_url = telegram_location_url(order) or order.location_url
    second_url = telegram_location_url(order, 2) or order.second_location_url
    if first_url:
        buttons.append(InlineKeyboardButton("📍 Показать локацию", url=first_url))
    if second_url:
        buttons.append(InlineKeyboardButton("📍 Доп. локация", url=second_url))
    return InlineKeyboardMarkup([buttons]) if buttons else None


def orders_channel_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order) + _pickup_rows(order)
    delivery_url = delivery_order_message_url(order)
    if delivery_url:
        rows.append([InlineKeyboardButton("📦 Карточка в группе курьера", url=delivery_url)])
    if order.status in {"draft", "pending", "picked_up", "on_way"}:
        courier_name = order.assigned_courier_name or "не выбран"
        rows.append([InlineKeyboardButton(
            f"🚚 Курьер: {courier_name}",
            callback_data=f"control_courier_menu:{order.id}",
        )])
    rows.append([InlineKeyboardButton("🔄 Обновить карточку", callback_data=f"sync:{order.id}")])
    return InlineKeyboardMarkup(rows)


def statistics_keyboard(base_url: str) -> InlineKeyboardMarkup:
    base = base_url.rstrip("/")
    monitor = base.rsplit("/", 1)[0] + "/monitor"

    def report_url(day: str, courier_id: int | None = None) -> str:
        query = {"day": day}
        if courier_id is not None:
            query["courier_id"] = str(courier_id)
        return f"{base}?{urlencode(query)}"

    rows = [[
        InlineKeyboardButton("📅 Все заказы сегодня", url=report_url("today")),
        InlineKeyboardButton("⏮ Все заказы вчера", url=report_url("yesterday")),
    ]]
    courier_buttons = [
        InlineKeyboardButton(
            f"👤 {courier.name}",
            url=report_url("today", courier.user_id),
        )
        for courier in COURIERS
    ]
    rows.append(courier_buttons[:2])
    rows.append(courier_buttons[2:])
    rows.append([InlineKeyboardButton("🚦 Мониторинг курьеров", url=monitor)])
    rows.append([InlineKeyboardButton("🌐 Открыть всю статистику", url=base)])
    return InlineKeyboardMarkup(rows)


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
        # Telegram has no disabled inline buttons. A silent callback keeps the
        # compact button-like layout without showing alerts or sending posts.
        return InlineKeyboardButton(text, callback_data=f"location_label:{order.id}")

    return InlineKeyboardMarkup([
        [order_button(address)],
        [order_button(order_line)],
        [order_button(phone)],
    ])


def courier_keyboard(order: Order) -> InlineKeyboardMarkup:
    rows = _location_rows(order)
    if order.status == "pending":
        read_text = (
            "✅ Прочитано · еду на склад"
            if order.courier_read_at
            else "👀 Заказ прочитан"
        )
        rows.append([InlineKeyboardButton(read_text, callback_data=f"read:{order.id}")])
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
    orders: list[Order] | None = None,
) -> InlineKeyboardMarkup:
    """Navigation for zero-based active/all order-list pages."""
    if kind not in {"active", "all"}:
        raise ValueError("kind must be 'active' or 'all'")
    if total_pages < 1:
        raise ValueError("total_pages must be positive")
    if page < 0 or page >= total_pages:
        raise ValueError("page is outside total_pages")

    rows: list[list[InlineKeyboardButton]] = []
    if kind == "active":
        for order in orders or []:
            product = " ".join(order.product.split()) or "—"
            label = f"✏️ №{order.order_number} · {product}"
            if len(label) > 60:
                label = label[:59].rstrip() + "…"
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"list_order:{order.id}",
            )])
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
