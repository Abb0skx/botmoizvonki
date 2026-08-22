import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from app.bot.keyboards import (
    all_locations_keyboard, courier_keyboard, delivery_pending_keyboard,
    location_channel_keyboard, main_keyboard, manager_sent_keyboard,
    on_way_keyboard, payment_keyboard, review_keyboard, seller_keyboard,
    skip_keyboard,
)
from app.config import Settings
from app.database import OrderRepository
from app.utils import (
    completed_card, courier_card, enrich_location, manager_card, normalize_phone,
    normalize_payment, normalize_seller, PAID_AT_ASSEMBLY, parse_amount,
    parse_order_details,
)
from app.utils.formatters import all_locations_card

logger = logging.getLogger(__name__)
SELLER, PRODUCT, DETAILS, PAYMENT, DELIVERY_TIME, COMMENT = range(6)
MANAGER_EDITABLE_STATUSES = {"draft", "pending", "on_way"}
LOCATION_STATUS_MARKERS = {
    "pending": "🆕",
    "on_way": "🚗",
    "awaiting_photo": "📸",
    "awaiting_amount": "💰",
    "completed": "✅",
    "cancelled": "❌",
}


def _name(user) -> str:
    return user.full_name or user.username or str(user.id)


def _allowed(user_id: int, allowed: frozenset[int]) -> bool:
    return user_id in allowed


def _text(message, *, maximum: int, required: bool = True) -> str | None:
    value = (message.text or "").strip()
    if value.casefold() == "пропустить" and not required:
        return None
    if required and not value:
        raise ValueError("Поле не может быть пустым.")
    if len(value) > maximum:
        raise ValueError(f"Слишком длинный текст. Максимум: {maximum} символов.")
    return value or None


async def _notify_manager(context: ContextTypes.DEFAULT_TYPE, manager_id: int, text: str) -> None:
    try:
        await context.bot.send_message(manager_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Could not notify manager %s", manager_id)


async def _location_values(message) -> dict:
    if message.location:
        latitude, longitude = message.location.latitude, message.location.longitude
        url = f"https://yandex.uz/maps/?ll={longitude:.6f}%2C{latitude:.6f}&z=17"
    else:
        url = message.text or ""
        latitude = longitude = None
    values = await enrich_location(latitude, longitude, url)
    if values["latitude"] is None or values["longitude"] is None:
        raise ValueError(
            "Не удалось определить координаты. Отправьте Telegram Location или полную ссылку Яндекс Карт."
        )
    return values


def _delivery_message(order):
    if order.status == "on_way":
        return courier_card(order, "🚗 <b>Курьер едет</b>"), on_way_keyboard(order)
    if order.status == "awaiting_photo":
        return courier_card(order, "📸 <b>Курьер подтверждает доставку</b>"), delivery_pending_keyboard(order)
    if order.status == "awaiting_amount":
        return courier_card(order, "💰 <b>Фото получено, ожидается сумма</b>"), delivery_pending_keyboard(order)
    return courier_card(order), courier_keyboard(order)


async def _refresh_delivery_message(context: ContextTypes.DEFAULT_TYPE, order) -> bool:
    if not order.delivery_chat_id or not order.delivery_message_id:
        return True
    text, keyboard = _delivery_message(order)
    try:
        await context.bot.edit_message_text(
            chat_id=order.delivery_chat_id,
            message_id=order.delivery_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return True
    except Exception:
        logger.exception("Could not refresh delivery message for order %s", order.id)
        return False


async def _set_location_marker(
    context: ContextTypes.DEFAULT_TYPE,
    order,
    marker: str | None = None,
) -> bool:
    if not order.location_chat_id or not order.location_message_id:
        return True
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=order.location_chat_id,
            message_id=order.location_message_id,
            reply_markup=location_channel_keyboard(
                order,
                marker or LOCATION_STATUS_MARKERS.get(order.status, "📍"),
            ),
        )
        return True
    except Exception:
        logger.exception("Could not update location channel message for order %s", order.id)
        return False


async def _publish_location(
    context: ContextTypes.DEFAULT_TYPE,
    repo: OrderRepository,
    order,
):
    if order.latitude is None or order.longitude is None:
        raise ValueError("Order has no coordinates")
    settings: Settings = context.application.bot_data["settings"]
    sent = await context.bot.send_location(
        chat_id=settings.location_channel_id,
        latitude=order.latitude,
        longitude=order.longitude,
        reply_markup=location_channel_keyboard(
            order,
            LOCATION_STATUS_MARKERS.get(order.status, "📍"),
        ),
    )
    return repo.update(
        order.id,
        location_chat_id=sent.chat_id,
        location_message_id=sent.message_id,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.type != "private":
        return
    if not _allowed(update.effective_user.id, settings.manager_ids):
        await update.message.reply_text("У вас нет доступа к созданию заказов.")
        return
    await update.message.reply_text("Бот доставки TEXNIKACH готов.", reply_markup=main_keyboard())


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.type != "private" or update.effective_user.id not in settings.manager_ids:
        await update.message.reply_text("Список заказов доступен менеджерам в личном чате.")
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    orders = repo.list_manager_open(update.effective_user.id)
    if not orders:
        await update.message.reply_text("Открытых заказов нет.", reply_markup=main_keyboard())
        return
    await update.message.reply_text(f"📋 Открытые заказы: {len(orders)}")
    for order in orders:
        editable = order.status in MANAGER_EDITABLE_STATUSES
        keyboard = review_keyboard(order.id) if order.status == "draft" else manager_sent_keyboard(order.id) if editable else None
        await update.message.reply_text(
            manager_card(order, sent=order.status != "draft"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )


async def new_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.type != "private" or not _allowed(update.effective_user.id, settings.manager_ids):
        await update.message.reply_text("Создание заказа доступно менеджерам в личном чате с ботом.")
        return ConversationHandler.END
    context.user_data["draft"] = {}
    await update.message.reply_text("1/6. Выберите продавца:", reply_markup=seller_keyboard())
    return SELLER


async def seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = normalize_seller(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error), reply_markup=seller_keyboard())
        return SELLER
    context.user_data["draft"]["seller_name"] = value
    await update.message.reply_text("2/6. Введите модель товара:", reply_markup=ReplyKeyboardRemove())
    return PRODUCT


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = _text(update.message, maximum=200)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return PRODUCT
    context.user_data["draft"]["product"] = value
    await update.message.reply_text(
        "3/6. Отправьте телефон, цену и локацию. Можно одним сообщением или отдельно, в любом порядке.\n\n"
        "Пример одного сообщения:\n"
        "Телефон: 90 133 39 99\n"
        "Цена: 100$ 1 920 000\n"
        "Локация: https://yandex.uz/maps/…\n\n"
        "Telegram Location отправляется отдельным сообщением."
    )
    return DETAILS


def _missing_details(draft: dict) -> list[str]:
    missing = []
    if not draft.get("client_phone"):
        missing.append("номер клиента")
    if draft.get("amount_usd") is None and draft.get("amount_uzs") is None:
        missing.append("цена")
    if draft.get("latitude") is None or draft.get("longitude") is None:
        missing.append("локация")
    return missing


async def details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END

    recognized: list[str] = []
    try:
        if update.message.location:
            draft.update(await _location_values(update.message))
            recognized.append("локация")
        else:
            parsed = parse_order_details(update.message.text or "")
            if "client_phone" in parsed:
                draft["client_phone"] = parsed["client_phone"]
                recognized.append("номер")
            if "amount_usd" in parsed or "amount_uzs" in parsed:
                draft["amount_usd"] = parsed.get("amount_usd")
                draft["amount_uzs"] = parsed.get("amount_uzs")
                recognized.append("цена")
            if parsed.get("location_url"):
                draft.update(await enrich_location(None, None, str(parsed["location_url"])))
                if draft.get("latitude") is None or draft.get("longitude") is None:
                    raise ValueError("Не удалось определить координаты по ссылке")
                recognized.append("локация")
    except ValueError as error:
        await update.message.reply_text(f"Не удалось сохранить данные: {error}")
        return DETAILS

    missing = _missing_details(draft)
    if not recognized:
        await update.message.reply_text(
            "Не распознал данные. Отправьте номер клиента, цену или ссылку на карту."
        )
        return DETAILS
    if missing:
        await update.message.reply_text(
            f"✅ Сохранено: {', '.join(recognized)}. Осталось отправить: {', '.join(missing)}."
        )
        return DETAILS

    await update.message.reply_text(
        "4/6. Выберите вариант оплаты:",
        reply_markup=payment_keyboard(),
    )
    return PAYMENT


async def payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = normalize_payment(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error), reply_markup=payment_keyboard())
        return PAYMENT
    context.user_data["draft"]["payment_status"] = value
    await update.message.reply_text(
        "5/6. Укажите время доставки (например, До 17:00) или пропустите:",
        reply_markup=skip_keyboard(),
    )
    return DELIVERY_TIME


async def delivery_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = _text(update.message, maximum=100, required=False)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return DELIVERY_TIME
    context.user_data["draft"]["delivery_time"] = value
    await update.message.reply_text("6/6. Добавьте комментарий или пропустите:", reply_markup=skip_keyboard())
    return COMMENT


async def comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = _text(update.message, maximum=1000, required=False)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return COMMENT
    context.user_data["draft"]["comment"] = value
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.create(manager_id=update.effective_user.id, manager_name=_name(update.effective_user), data=context.user_data.pop("draft"))
    await update.message.reply_text(manager_card(order), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=review_keyboard(order.id))
    await update.message.reply_text("Проверьте данные заказа.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def begin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, raw_id, field = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order or order.manager_id != query.from_user.id or order.status not in MANAGER_EDITABLE_STATUSES:
        await query.answer("Этот заказ уже нельзя изменять", show_alert=True)
        return
    await query.answer()
    context.user_data["edit"] = {
        "order_id": order.id,
        "field": field,
        "message_id": query.message.message_id,
        "chat_id": query.message.chat_id,
    }
    prompts = {"seller": "Выберите нового продавца:", "payment_status": "Выберите новый вариант оплаты:", "product": "Введите новую модель:", "phone": "Введите новый номер:", "location": "Отправьте новую локацию или ссылку:", "amount": "Введите новую сумму:", "delivery_time": "Введите новое время (или Пропустить):", "comment": "Введите новый комментарий (или Пропустить):"}
    if field == "seller":
        keyboard = seller_keyboard()
    elif field == "payment_status":
        keyboard = payment_keyboard()
    else:
        keyboard = ReplyKeyboardRemove()
    await query.message.reply_text(prompts[field], reply_markup=keyboard)


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    edit = context.user_data.get("edit")
    if not edit or update.effective_chat.id != edit["chat_id"]:
        return
    field, values = edit["field"], {}
    try:
        if field == "seller": values["seller_name"] = normalize_seller(update.message.text or "")
        elif field == "payment_status": values["payment_status"] = normalize_payment(update.message.text or "")
        elif field == "phone": values["client_phone"] = normalize_phone(update.message.text or "")
        elif field == "amount": values["amount_usd"], values["amount_uzs"] = parse_amount(update.message.text or "")
        elif field == "location":
            values.update(await _location_values(update.message))
        else:
            limits = {"product": 200, "delivery_time": 100, "comment": 1000}
            values[field] = _text(update.message, maximum=limits[field], required=field == "product")
    except ValueError as error:
        await update.message.reply_text(str(error))
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    previous = repo.get(edit["order_id"])
    order = repo.transition(edit["order_id"], MANAGER_EDITABLE_STATUSES, **values)
    if not order:
        context.user_data.pop("edit", None)
        await update.message.reply_text("Заказ уже отправлен или отменён.", reply_markup=main_keyboard())
        return
    sent = order.status != "draft"
    location_published = True
    if sent and field == "location":
        if previous:
            await _set_location_marker(context, previous, "⚠️")
        order = repo.update(order.id, location_chat_id=None, location_message_id=None)
        try:
            order = await _publish_location(context, repo, order)
        except Exception:
            location_published = False
            logger.exception("Could not publish replacement location for order %s", order.id)
    elif sent:
        await _set_location_marker(context, order)
    keyboard = manager_sent_keyboard(order.id) if sent else review_keyboard(order.id)
    await context.bot.edit_message_text(chat_id=edit["chat_id"], message_id=edit["message_id"], text=manager_card(order, sent=sent), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=keyboard)
    refreshed = await _refresh_delivery_message(context, order) if sent else True
    context.user_data.pop("edit", None)
    if not location_published:
        result = "⚠️ Данные сохранены, но новую Telegram Location отправить не удалось. Проверьте права бота в канале локаций."
    elif not refreshed:
        result = "⚠️ Данные сохранены, но карточку в группе обновить не удалось."
    else:
        result = "✅ Данные обновлены у менеджера и курьера."
    await update.message.reply_text(result, reply_markup=main_keyboard())


async def manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, raw_id = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order or order.manager_id != query.from_user.id or order.status != "draft":
        await query.answer("Заказ уже обработан или недоступен", show_alert=True); return
    if action == "manager_cancel":
        if not repo.transition(order.id, {"draft"}, status="cancelled"):
            await query.answer("Заказ уже обработан", show_alert=True); return
        await query.edit_message_text(f"❌ Заказ №{order.order_number} отменён менеджером")
        await query.answer(); return
    settings: Settings = context.application.bot_data["settings"]
    sent = await context.bot.send_message(settings.delivery_group_id, courier_card(order), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=courier_keyboard(order))
    updated = repo.transition(order.id, {"draft"}, status="pending", delivery_chat_id=sent.chat_id, delivery_message_id=sent.message_id)
    if not updated:
        await sent.edit_text(f"⚠️ Заказ №{order.order_number} был отменён до отправки")
        await query.answer("Заказ уже обработан", show_alert=True); return
    location_published = True
    try:
        updated = await _publish_location(context, repo, updated)
    except Exception:
        location_published = False
        logger.exception("Could not publish location for order %s", updated.id)
    await _refresh_delivery_message(context, updated)
    await query.edit_message_text(manager_card(updated, sent=True), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=manager_sent_keyboard(updated.id))
    if location_published:
        await query.answer("Заказ и Telegram Location отправлены")
    else:
        await query.answer("Заказ отправлен, но канал локаций недоступен", show_alert=True)


async def courier_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if query.message.chat_id != settings.delivery_group_id or not _allowed(query.from_user.id, settings.courier_ids):
        await query.answer("Нет доступа", show_alert=True); return
    action, raw_id = query.data.split(":"); repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order or order.status in {"completed", "cancelled"}:
        await query.answer("Заказ уже закрыт", show_alert=True); return
    if order.courier_id and order.courier_id != query.from_user.id:
        await query.answer(f"Заказ уже взял {order.courier_name}", show_alert=True); return
    courier = {"courier_id": query.from_user.id, "courier_name": _name(query.from_user)}
    if action == "undo_onway":
        order = repo.transition(
            order.id,
            {"on_way"},
            status="pending",
            courier_id=None,
            courier_name=None,
            time_started=None,
            guard_courier_id=query.from_user.id,
            require_unassigned_or_same=True,
        )
        if not order:
            await query.answer("Выезд уже нельзя отменить", show_alert=True); return
        await query.edit_message_text(
            courier_card(order, "↩️ <b>Выезд отменён, заказ снова свободен</b>"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=courier_keyboard(order),
        )
        await query.answer("Заказ возвращён в очередь")
        await _set_location_marker(context, order)
        await _notify_manager(
            context,
            order.manager_id,
            f"↩️ Курьер отменил выезд к заказу №{order.order_number}. Заказ снова ожидает курьера.",
        )
    elif action == "onway":
        if order.status == "on_way" and order.courier_id == query.from_user.id:
            await query.answer("Вы уже едете к этому заказу"); return
        order = repo.transition(
            order.id, {"pending"}, status="on_way",
            time_started=datetime.now().astimezone().isoformat(timespec="seconds"),
            guard_courier_id=query.from_user.id, require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Заказ уже взял другой курьер", show_alert=True); return
        await query.edit_message_text(courier_card(order, "🚗 <b>Курьер едет</b>"), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=on_way_keyboard(order))
        await query.answer("Статус обновлён")
        await _set_location_marker(context, order)
    elif action == "cancel":
        order = repo.transition(
            order.id, {"pending", "on_way", "awaiting_photo", "awaiting_amount"},
            status="cancelled", guard_courier_id=query.from_user.id,
            require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Заказ уже обработан другим курьером", show_alert=True); return
        await query.edit_message_text(f"❌ <b>Заказ №{order.order_number} отменён</b>\n\n👤 Курьер: {escape(_name(query.from_user))}", parse_mode=ParseMode.HTML)
        await query.answer("Заказ отменён")
        await _set_location_marker(context, order)
        await _notify_manager(context, order.manager_id, f"❌ <b>Заказ №{order.order_number} отменён</b>\n\n👤 Курьер: {escape(_name(query.from_user))}")
    else:
        active = repo.get_active_delivery(query.from_user.id)
        if active and active.id != order.id:
            await query.answer(f"Сначала завершите заказ №{active.order_number}", show_alert=True); return
        if order.status in {"awaiting_photo", "awaiting_amount"} and order.courier_id == query.from_user.id:
            prompt = "Отправьте фото доставки 📸" if order.status == "awaiting_photo" else "Введите полученную сумму"
            await query.answer(prompt, show_alert=True); return
        order = repo.transition(
            order.id, {"pending", "on_way"}, status="awaiting_photo",
            guard_courier_id=query.from_user.id, require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Заказ уже обрабатывает другой курьер", show_alert=True); return
        await query.answer()
        await query.edit_message_text(
            courier_card(order, "📸 <b>Курьер подтверждает доставку</b>"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=delivery_pending_keyboard(order),
        )
        await query.message.reply_text(f"Заказ №{order.order_number}: отправьте фото доставки 📸")
        await _set_location_marker(context, order)


async def _publish_completed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order,
    timestamp: datetime,
) -> None:
    result_text = completed_card(
        order,
        timestamp.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M"),
    )
    try:
        await context.bot.edit_message_text(
            chat_id=order.delivery_chat_id,
            message_id=order.delivery_message_id,
            text=result_text,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Could not update delivery group message for order %s", order.id)
    try:
        await context.bot.send_photo(
            order.manager_id,
            order.delivery_photo,
            caption=result_text,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Could not notify manager %s about order %s", order.manager_id, order.id)
    await _set_location_marker(context, order)
    await update.message.reply_text("✅ Доставка подтверждена.")


async def delivery_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.id != settings.delivery_group_id or update.effective_user.id not in settings.courier_ids:
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get_active_delivery(update.effective_user.id)
    if not order:
        return
    if order.status == "awaiting_photo":
        if not update.message.photo:
            await update.message.reply_text("Нужно отправить фотографию."); return
        photo_id = update.message.photo[-1].file_id
        if order.payment_status == PAID_AT_ASSEMBLY:
            timestamp = datetime.now().astimezone()
            updated = repo.transition(
                order.id,
                {"awaiting_photo"},
                status="completed",
                delivery_photo=photo_id,
                delivered_at=timestamp.isoformat(timespec="seconds"),
            )
            if not updated:
                await update.message.reply_text("Статус заказа уже изменился."); return
            await _publish_completed(update, context, updated, timestamp)
            return
        updated = repo.transition(
            order.id,
            {"awaiting_photo"},
            status="awaiting_amount",
            delivery_photo=photo_id,
        )
        if not updated:
            await update.message.reply_text("Статус заказа уже изменился."); return
        try:
            await context.bot.edit_message_text(
                chat_id=updated.delivery_chat_id,
                message_id=updated.delivery_message_id,
                text=courier_card(updated, "💰 <b>Фото получено, ожидается сумма</b>"),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=delivery_pending_keyboard(updated),
            )
        except Exception:
            logger.exception("Could not update photo confirmation state for order %s", updated.id)
        await _set_location_marker(context, updated)
        await update.message.reply_text("Введите полученную сумму, например: 100$ 1920000")
        return
    if update.message.photo:
        await update.message.reply_text("Фото уже получено. Теперь введите сумму текстом."); return
    try: usd, uzs = parse_amount(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error)); return
    timestamp = datetime.now().astimezone()
    order = repo.transition(order.id, {"awaiting_amount"}, status="completed", received_usd=usd, received_uzs=uzs, delivered_at=timestamp.isoformat(timespec="seconds"))
    if not order:
        await update.message.reply_text("Заказ уже обработан."); return
    await _publish_completed(update, context, order, timestamp)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None); context.user_data.pop("edit", None)
    await update.message.reply_text("Действие отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def show_all_locations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id
    allowed = user_id in settings.manager_ids or user_id in settings.courier_ids
    if update.effective_chat.id != settings.delivery_group_id or not allowed:
        if update.callback_query:
            await update.callback_query.answer("Карта доступна сотрудникам в группе доставки", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Карта доступна сотрудникам в группе доставки.")
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    text, map_url = all_locations_card(repo.list_active())
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
    else:
        message = update.effective_message
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=all_locations_keyboard(map_url),
    )


async def location_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if (
        query.message.chat_id != settings.location_channel_id
        or query.from_user.id not in (settings.manager_ids | settings.courier_ids)
    ):
        await query.answer("Нет доступа", show_alert=True)
        return
    _, raw_id = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    status = LOCATION_STATUS_MARKERS.get(order.status, "📍")
    product = " ".join(order.product.split())[:80]
    await query.answer(
        f"{status} Заказ №{order.order_number}\n{product}\n{order.client_phone}",
        show_alert=True,
    )


def register_handlers(application: Application) -> None:
    conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕ Новый заказ$") & filters.ChatType.PRIVATE, new_order)],
        states={
            SELLER: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller)],
            PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, product)],
            DETAILS: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, details)],
            PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment)],
            DELIVERY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_time)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)], allow_reentry=True,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("map", show_all_locations))
    application.add_handler(MessageHandler(filters.Regex(r"^📋 Мои заказы$") & filters.ChatType.PRIVATE, my_orders))
    application.add_handler(conversation)
    application.add_handler(CommandHandler("cancel", cancel_conversation))
    application.add_handler(CallbackQueryHandler(begin_edit, pattern=r"^edit:\d+:"))
    application.add_handler(CallbackQueryHandler(manager_action, pattern=r"^(send|manager_cancel):\d+$"))
    application.add_handler(CallbackQueryHandler(show_all_locations, pattern=r"^map:all$"))
    application.add_handler(CallbackQueryHandler(location_info, pattern=r"^location_info:\d+$"))
    application.add_handler(CallbackQueryHandler(courier_action, pattern=r"^(onway|undo_onway|complete|cancel):\d+$"))
    application.add_handler(MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, save_edit), group=1)
    application.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), delivery_input), group=2)
