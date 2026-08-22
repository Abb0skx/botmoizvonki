import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from app.bot.keyboards import (
    all_locations_keyboard, completed_keyboard, courier_cancelled_keyboard,
    courier_keyboard, delivery_pending_keyboard, location_channel_keyboard, main_keyboard,
    manager_cancelled_keyboard, manager_sent_keyboard, on_way_keyboard,
    payment_keyboard, review_keyboard, seller_keyboard, second_location_keyboard,
    skip_keyboard,
)
from app.config import Settings
from app.database import OrderRepository
from app.utils import (
    completed_card, courier_card, enrich_location, manager_card, normalize_phone,
    normalize_payment, normalize_seller, parse_amount,
    parse_order_details,
)
from app.utils.formatters import all_locations_card, money, short_address

logger = logging.getLogger(__name__)
SELLER, PRODUCT, DETAILS, SECOND_LOCATION, PAYMENT, DELIVERY_TIME, COMMENT = range(7)
MANAGER_EDITABLE_STATUSES = {"draft", "pending", "on_way"}
LOCATION_STATUS_MARKERS = {
    "pending": "🆕",
    "on_way": "🚗",
    "awaiting_photo": "📸",
    "awaiting_amount": "💰",
    "completed": "✅",
    "cancelled": "❌",
}


def _message_is_not_modified(error: Exception) -> bool:
    return isinstance(error, BadRequest) and "message is not modified" in str(error).casefold()


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
    except Exception as error:
        if _message_is_not_modified(error):
            return True
        logger.exception("Could not refresh delivery message for order %s", order.id)
        return False


async def _set_location_marker(
    context: ContextTypes.DEFAULT_TYPE,
    order,
    marker: str | None = None,
    location_number: int | None = None,
) -> bool:
    numbers = (location_number,) if location_number else (1, 2)
    success = True
    for number in numbers:
        if number == 2:
            chat_id = order.second_location_chat_id
            message_id = order.second_location_message_id
        else:
            chat_id = order.location_chat_id
            message_id = order.location_message_id
        if not chat_id or not message_id:
            continue
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=location_channel_keyboard(
                    order,
                    marker or LOCATION_STATUS_MARKERS.get(order.status, "📍"),
                    number,
                ),
            )
        except Exception as error:
            if _message_is_not_modified(error):
                continue
            success = False
            logger.exception(
                "Could not update location %s channel message for order %s",
                number,
                order.id,
            )
    return success


async def _publish_location(
    context: ContextTypes.DEFAULT_TYPE,
    repo: OrderRepository,
    order,
    location_number: int = 1,
):
    if location_number == 2:
        latitude = order.second_latitude
        longitude = order.second_longitude
        update_fields = {
            "second_location_chat_id": None,
            "second_location_message_id": None,
        }
    else:
        latitude = order.latitude
        longitude = order.longitude
        update_fields = {"location_chat_id": None, "location_message_id": None}
    if latitude is None or longitude is None:
        raise ValueError("Order has no coordinates")
    settings: Settings = context.application.bot_data["settings"]
    sent = await context.bot.send_location(
        chat_id=settings.location_channel_id,
        latitude=latitude,
        longitude=longitude,
        reply_markup=location_channel_keyboard(
            order,
            LOCATION_STATUS_MARKERS.get(order.status, "📍"),
            location_number,
        ),
    )
    if location_number == 2:
        update_fields.update(
            second_location_chat_id=sent.chat_id,
            second_location_message_id=sent.message_id,
        )
    else:
        update_fields.update(
            location_chat_id=sent.chat_id,
            location_message_id=sent.message_id,
        )
    return repo.update(order.id, **update_fields)


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
    orders = repo.list_all()
    if not orders:
        await update.message.reply_text("Заказов пока нет.", reply_markup=main_keyboard())
        return
    await update.message.reply_text(f"📋 Все заказы: {len(orders)}")
    for order in orders:
        editable = order.status in MANAGER_EDITABLE_STATUSES
        keyboard = review_keyboard(order.id) if order.status == "draft" else manager_sent_keyboard(order) if editable else None
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
    await update.message.reply_text("1/7. Выберите, кому принадлежит заказ:", reply_markup=seller_keyboard())
    return SELLER


async def seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = normalize_seller(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error), reply_markup=seller_keyboard())
        return SELLER
    context.user_data["draft"]["seller_name"] = value
    await update.message.reply_text("2/7. Введите модель товара:", reply_markup=ReplyKeyboardRemove())
    return PRODUCT


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = _text(update.message, maximum=200)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return PRODUCT
    context.user_data["draft"]["product"] = value
    await update.message.reply_text(
        "3/7. Отправьте телефон, цену и основную локацию. Можно одним сообщением или отдельно, в любом порядке.\n\n"
        "Пример одного сообщения:\n"
        "Телефон: 90 133 39 99\n"
        "Цена: 100$ 1 920 000\n"
        "Локация: https://yandex.uz/maps/…\n\n"
        "Telegram Location отправляется отдельным сообщением. После этого можно будет добавить вторую локацию."
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
        "4/7. Отправьте вторую локацию или продолжите без неё:",
        reply_markup=second_location_keyboard(),
    )
    return SECOND_LOCATION


def _as_second_location(values: dict) -> dict:
    return {f"second_{key}": value for key, value in values.items()}


async def second_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().casefold()
    if text == "продолжить без второй локации":
        await update.message.reply_text(
            "5/7. Выберите вариант оплаты:",
            reply_markup=payment_keyboard(),
        )
        return PAYMENT
    try:
        values = await _location_values(update.message)
    except ValueError as error:
        await update.message.reply_text(
            f"Не удалось сохранить вторую локацию: {error}",
            reply_markup=second_location_keyboard(),
        )
        return SECOND_LOCATION
    context.user_data["draft"].update(_as_second_location(values))
    await update.message.reply_text(
        "✅ Вторая локация сохранена.\n\n5/7. Выберите вариант оплаты:",
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
        "6/7. Укажите время доставки (например, До 17:00) или пропустите:",
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
    await update.message.reply_text("7/7. Добавьте комментарий или пропустите:", reply_markup=skip_keyboard())
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
    settings: Settings = context.application.bot_data["settings"]
    if not order or not _allowed(query.from_user.id, settings.manager_ids) or order.status not in MANAGER_EDITABLE_STATUSES:
        await query.answer("Этот заказ уже нельзя изменять", show_alert=True)
        return
    await query.answer()
    context.user_data["edit"] = {
        "order_id": order.id,
        "field": field,
        "message_id": query.message.message_id,
        "chat_id": query.message.chat_id,
    }
    prompts = {
        "seller": "Выберите нового владельца заказа:",
        "payment_status": "Выберите новый вариант оплаты:",
        "product": "Введите новую модель:",
        "phone": "Введите новый номер:",
        "location": "Отправьте новую основную локацию или ссылку:",
        "second_location": "Отправьте дополнительную локацию или ссылку:",
        "amount": "Введите новую сумму. Например: 120$ 1 536 000",
        "delivery_time": "Введите новое время (или Пропустить):",
        "comment": "Введите новый комментарий (или Пропустить):",
    }
    if field == "seller":
        keyboard = seller_keyboard()
    elif field == "payment_status":
        keyboard = payment_keyboard()
    else:
        keyboard = ReplyKeyboardRemove()
    await query.message.reply_text(prompts[field], reply_markup=keyboard)


async def toggle_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, raw_id = query.data.split(":")
    settings: Settings = context.application.bot_data["settings"]
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if (
        not order
        or not _allowed(query.from_user.id, settings.manager_ids)
        or order.status not in MANAGER_EDITABLE_STATUSES
    ):
        await query.answer("Этот заказ уже нельзя изменять", show_alert=True)
        return
    expanded = action == "edit_menu"
    keyboard = (
        review_keyboard(order.id, expanded=expanded)
        if order.status == "draft"
        else manager_sent_keyboard(order, expanded=expanded)
    )
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception as error:
        if not _message_is_not_modified(error):
            raise
    await query.answer()


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
        elif field in {"location", "second_location"}:
            values.update(await _location_values(update.message))
            if field == "second_location":
                values = _as_second_location(values)
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
    if sent and field in {"location", "second_location"}:
        location_number = 2 if field == "second_location" else 1
        if previous:
            await _set_location_marker(context, previous, "⚠️", location_number)
        clear_fields = (
            {"second_location_chat_id": None, "second_location_message_id": None}
            if location_number == 2
            else {"location_chat_id": None, "location_message_id": None}
        )
        order = repo.update(order.id, **clear_fields)
        try:
            order = await _publish_location(context, repo, order, location_number)
            await _set_location_marker(context, order)
        except Exception:
            location_published = False
            logger.exception("Could not publish replacement location for order %s", order.id)
    elif sent:
        await _set_location_marker(context, order)
    keyboard = manager_sent_keyboard(order) if sent else review_keyboard(order.id)
    manager_refreshed = True
    try:
        await context.bot.edit_message_text(
            chat_id=edit["chat_id"],
            message_id=edit["message_id"],
            text=manager_card(order, sent=sent),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except Exception as error:
        if not _message_is_not_modified(error):
            manager_refreshed = False
            logger.exception("Could not refresh manager message for order %s", order.id)
    refreshed = await _refresh_delivery_message(context, order) if sent else True
    context.user_data.pop("edit", None)
    if not location_published:
        result = "⚠️ Данные сохранены, но новую Telegram Location отправить не удалось. Проверьте права бота в канале локаций."
    elif not manager_refreshed:
        result = "⚠️ Данные сохранены в базе, но карточку менеджера обновить не удалось."
    elif not refreshed:
        result = "⚠️ Данные сохранены, но карточку в группе обновить не удалось."
    elif field == "amount":
        result = f"✅ Новая цена сохранена:\n{money(order.amount_usd, order.amount_uzs)}"
    else:
        result = "✅ Данные обновлены у менеджера и курьера."
    await update.message.reply_text(result, reply_markup=main_keyboard())


async def manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, raw_id = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    settings: Settings = context.application.bot_data["settings"]
    order = repo.get(int(raw_id))
    if not order or not _allowed(query.from_user.id, settings.manager_ids):
        await query.answer("Заказ недоступен", show_alert=True); return
    if action == "manager_restore":
        if order.delivery_message_id is not None:
            await query.answer("Отменённый курьером заказ возвращается из группы доставки", show_alert=True); return
        restored = repo.transition(order.id, {"cancelled"}, status="draft")
        if not restored:
            await query.answer("Заказ уже нельзя вернуть", show_alert=True); return
        await query.edit_message_text(
            manager_card(restored),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=review_keyboard(restored.id),
        )
        await query.answer("Заказ возвращён")
        return
    if order.status != "draft":
        await query.answer("Заказ уже обработан или недоступен", show_alert=True); return
    if action == "manager_cancel":
        if not repo.transition(order.id, {"draft"}, status="cancelled"):
            await query.answer("Заказ уже обработан", show_alert=True); return
        await query.edit_message_text(
            f"❌ Заказ №{order.order_number} отменён менеджером",
            reply_markup=manager_cancelled_keyboard(order.id),
        )
        await query.answer(); return
    sent = await context.bot.send_message(settings.delivery_group_id, courier_card(order), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=courier_keyboard(order))
    updated = repo.transition(order.id, {"draft"}, status="pending", delivery_chat_id=sent.chat_id, delivery_message_id=sent.message_id)
    if not updated:
        await sent.edit_text(f"⚠️ Заказ №{order.order_number} был отменён до отправки")
        await query.answer("Заказ уже обработан", show_alert=True); return
    location_published = True
    location_numbers = [1]
    if updated.second_latitude is not None and updated.second_longitude is not None:
        location_numbers.append(2)
    for location_number in location_numbers:
        try:
            updated = await _publish_location(context, repo, updated, location_number)
        except Exception:
            location_published = False
            logger.exception(
                "Could not publish location %s for order %s",
                location_number,
                updated.id,
            )
    await _refresh_delivery_message(context, updated)
    await query.edit_message_text(manager_card(updated, sent=True), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=manager_sent_keyboard(updated))
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
    if not order:
        await query.answer("Заказ не найден", show_alert=True); return
    if action == "undo_cancel":
        if order.status != "cancelled" or order.courier_id != query.from_user.id:
            await query.answer("Этот заказ уже нельзя вернуть", show_alert=True); return
        order = repo.transition(
            order.id,
            {"cancelled"},
            status="pending",
            courier_id=None,
            courier_name=None,
            time_started=None,
            delivery_photo=None,
            received_usd=None,
            received_uzs=None,
            delivered_at=None,
            guard_courier_id=query.from_user.id,
            require_unassigned_or_same=True,
        )
        if not order:
            await query.answer("Этот заказ уже нельзя вернуть", show_alert=True); return
        await query.edit_message_text(
            courier_card(order, "↩️ <b>Отмена снята, заказ снова активен</b>"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=courier_keyboard(order),
        )
        await query.answer("Заказ возвращён")
        await _set_location_marker(context, order)
        await _notify_manager(
            context,
            order.manager_id,
            f"↩️ Отмена заказа №{order.order_number} снята. Заказ снова ожидает курьера.",
        )
        return
    if action == "undo_complete":
        if order.status not in {"awaiting_photo", "awaiting_amount", "completed"} or order.courier_id != query.from_user.id:
            await query.answer("Подтверждение уже нельзя отменить", show_alert=True); return
        target_status = "on_way" if order.time_started else "pending"
        reset = {
            "status": target_status,
            "delivery_photo": None,
            "received_usd": None,
            "received_uzs": None,
            "delivered_at": None,
        }
        if target_status == "pending":
            reset.update(courier_id=None, courier_name=None)
        order = repo.transition(
            order.id,
            {"awaiting_photo", "awaiting_amount", "completed"},
            guard_courier_id=query.from_user.id,
            require_unassigned_or_same=True,
            **reset,
        )
        if not order:
            await query.answer("Подтверждение уже нельзя отменить", show_alert=True); return
        state = "↩️ <b>Подтверждение доставки отменено</b>"
        keyboard = on_way_keyboard(order) if target_status == "on_way" else courier_keyboard(order)
        await query.edit_message_text(
            courier_card(order, state),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        await query.answer("Возвращено назад")
        await _set_location_marker(context, order)
        return
    if order.status in {"completed", "cancelled"}:
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
        await query.edit_message_text(
            f"❌ <b>Заказ №{order.order_number} отменён</b>\n\n👤 Курьер: {escape(_name(query.from_user))}",
            parse_mode=ParseMode.HTML,
            reply_markup=courier_cancelled_keyboard(order),
        )
        await query.answer("Заказ отменён")
        await _set_location_marker(context, order)
    else:
        timestamp = datetime.now().astimezone()
        order = repo.transition(
            order.id,
            {"pending", "on_way"},
            status="completed",
            delivered_at=timestamp.isoformat(timespec="seconds"),
            guard_courier_id=query.from_user.id, require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Заказ уже обрабатывает другой курьер", show_alert=True); return
        result_text = completed_card(
            order,
            timestamp.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M"),
        )
        await query.edit_message_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=completed_keyboard(order),
        )
        await query.answer("Заказ доставлен")
        await _set_location_marker(context, order)
        await _notify_manager(context, order.manager_id, result_text)


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
            await update.message.reply_text(
                "Отправьте фото и укажите цену в подписи к фото. Пример: 100$ 1 920 000"
            ); return
        try:
            usd, uzs = parse_amount(update.message.caption or "")
        except ValueError:
            await update.message.reply_text(
                "Цена не распознана. Отправьте фото заново и напишите цену в подписи. "
                "Пример: 100$ 1 920 000"
            )
            return
        photo_id = update.message.photo[-1].file_id
        timestamp = datetime.now().astimezone()
        updated = repo.transition(
            order.id,
            {"awaiting_photo"},
            status="completed",
            delivery_photo=photo_id,
            received_usd=usd,
            received_uzs=uzs,
            delivered_at=timestamp.isoformat(timespec="seconds"),
        )
        if not updated:
            await update.message.reply_text("Статус заказа уже изменился."); return
        await _publish_completed(update, context, updated, timestamp)
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
    if update.effective_chat.type != "private" or user_id not in settings.manager_ids:
        if update.effective_message:
            await update.effective_message.reply_text("Все активные заказы доступны менеджерам в личном чате с ботом.")
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    text, map_url = all_locations_card(repo.list_active())
    await update.effective_message.reply_text(
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
    _, raw_id, raw_location_number = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    status = LOCATION_STATUS_MARKERS.get(order.status, "📍")
    product = " ".join(order.product.split())[:80]
    location_number = int(raw_location_number)
    address = short_address(order, location_number)
    await query.answer(
        f"{status} Заказ №{order.order_number}\n{product}\n{address}\n{order.client_phone}",
        show_alert=True,
    )


def register_handlers(application: Application) -> None:
    conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕ Новый заказ$") & filters.ChatType.PRIVATE, new_order)],
        states={
            SELLER: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller)],
            PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, product)],
            DETAILS: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, details)],
            SECOND_LOCATION: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, second_location)],
            PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment)],
            DELIVERY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_time)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)], allow_reentry=True,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("map", show_all_locations))
    application.add_handler(MessageHandler(filters.Regex(r"^📋 (?:Мои|Все) заказы$") & filters.ChatType.PRIVATE, my_orders))
    application.add_handler(MessageHandler(filters.Regex(r"^📦 Все активные заказы$") & filters.ChatType.PRIVATE, show_all_locations))
    application.add_handler(conversation)
    application.add_handler(CommandHandler("cancel", cancel_conversation))
    application.add_handler(CallbackQueryHandler(toggle_edit_menu, pattern=r"^edit_(?:menu|close):\d+$"))
    application.add_handler(CallbackQueryHandler(begin_edit, pattern=r"^edit:\d+:"))
    application.add_handler(CallbackQueryHandler(manager_action, pattern=r"^(send|manager_cancel|manager_restore):\d+$"))
    application.add_handler(CallbackQueryHandler(location_info, pattern=r"^location_info:\d+:[12]$"))
    application.add_handler(CallbackQueryHandler(courier_action, pattern=r"^(onway|undo_onway|complete|cancel|undo_cancel|undo_complete):\d+$"))
    application.add_handler(MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, save_edit), group=1)
    application.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), delivery_input), group=2)
