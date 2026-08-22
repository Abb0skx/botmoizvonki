from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("➕ Новый заказ")]], resize_keyboard=True)


def review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    rows = [
        [("✏️ Изменить товар", "product"), ("📞 Изменить номер", "phone")],
        [("📍 Изменить локацию", "location"), ("💰 Изменить сумму", "amount")],
        [("🕒 Изменить время", "delivery_time"), ("💬 Изменить комментарий", "comment")],
    ]
    keyboard = [[InlineKeyboardButton(label, callback_data=f"edit:{order_id}:{field}") for label, field in row] for row in rows]
    keyboard += [[InlineKeyboardButton("🚚 Отправить курьеру", callback_data=f"send:{order_id}")],
                 [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"manager_cancel:{order_id}")]]
    return InlineKeyboardMarkup(keyboard)


def courier_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚗 Еду к заказу", callback_data=f"onway:{order_id}")],
        [InlineKeyboardButton("✅ Доставлено", callback_data=f"complete:{order_id}"),
         InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order_id}")],
    ])


def delivery_pending_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменён", callback_data=f"cancel:{order_id}")],
    ])


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Пропустить")]], resize_keyboard=True, one_time_keyboard=True)
