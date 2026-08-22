import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from app.bot.keyboards import courier_keyboard, delivery_pending_keyboard, main_keyboard, review_keyboard, skip_keyboard
from app.config import Settings
from app.database import OrderRepository
from app.utils import completed_card, courier_card, manager_card, normalize_phone, parse_amount, parse_location_url

logger = logging.getLogger(__name__)
PRODUCT, AMOUNT, PHONE, LOCATION, DELIVERY_TIME, COMMENT, EDIT_VALUE = range(7)


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _allowed(update.effective_user.id, settings.manager_ids):
        await update.message.reply_text("У вас нет доступа к созданию заказов.")
        return
    await update.message.reply_text("Бот доставки TEXNIKACH готов.", reply_markup=main_keyboard())


async def new_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.type != "private" or not _allowed(update.effective_user.id, settings.manager_ids):
        await update.message.reply_text("Создание заказа доступно менеджерам в личном чате с ботом.")
        return ConversationHandler.END
    context.user_data["draft"] = {}
    await update.message.reply_text("1/6. Введите модель товара:")
    return PRODUCT


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = _text(update.message, maximum=200)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return PRODUCT
    context.user_data["draft"]["product"] = value
    await update.message.reply_text("2/6. Введите общую сумму, например: 100$ 1920000")
    return AMOUNT


async def amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        usd, uzs = parse_amount(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error))
        return AMOUNT
    context.user_data["draft"].update(amount_usd=usd, amount_uzs=uzs)
    await update.message.reply_text("3/6. Введите номер телефона клиента:")
    return PHONE


async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        value = normalize_phone(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error))
        return PHONE
    context.user_data["draft"]["client_phone"] = value
    await update.message.reply_text("4/6. Отправьте Telegram Location или ссылку Яндекс/Google карт:")
    return LOCATION


async def location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.location:
        lat, lon = update.message.location.latitude, update.message.location.longitude
        url = f"https://maps.google.com/?q={lat},{lon}"
    else:
        try:
            lat, lon, url = parse_location_url(update.message.text or "")
        except ValueError as error:
            await update.message.reply_text(str(error))
            return LOCATION
    context.user_data["draft"].update(latitude=lat, longitude=lon, location_url=url)
    await update.message.reply_text("5/6. Укажите время доставки (например, До 17:00) или пропустите:", reply_markup=skip_keyboard())
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


async def begin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, raw_id, field = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order or order.manager_id != query.from_user.id or order.status != "draft":
        await query.answer("Заказ уже отправлен или недоступен", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["edit"] = {"order_id": order.id, "field": field, "message_id": query.message.message_id}
    prompts = {"product": "Введите новую модель:", "phone": "Введите новый номер:", "location": "Отправьте новую локацию или ссылку:", "amount": "Введите новую сумму:", "delivery_time": "Введите новое время (или Пропустить):", "comment": "Введите новый комментарий (или Пропустить):"}
    await query.message.reply_text(prompts[field])
    return EDIT_VALUE


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    edit = context.user_data.get("edit")
    if not edit:
        return ConversationHandler.END
    field, values = edit["field"], {}
    try:
        if field == "phone": values["client_phone"] = normalize_phone(update.message.text or "")
        elif field == "amount": values["amount_usd"], values["amount_uzs"] = parse_amount(update.message.text or "")
        elif field == "location":
            if update.message.location:
                lat, lon = update.message.location.latitude, update.message.location.longitude
                url = f"https://maps.google.com/?q={lat},{lon}"
            else: lat, lon, url = parse_location_url(update.message.text or "")
            values.update(latitude=lat, longitude=lon, location_url=url)
        else:
            limits = {"product": 200, "delivery_time": 100, "comment": 1000}
            values[field] = _text(update.message, maximum=limits[field], required=field == "product")
    except ValueError as error:
        await update.message.reply_text(str(error))
        return EDIT_VALUE
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.transition(edit["order_id"], {"draft"}, **values)
    if not order:
        context.user_data.pop("edit", None)
        await update.message.reply_text("Заказ уже отправлен или отменён.", reply_markup=main_keyboard())
        return ConversationHandler.END
    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=edit["message_id"], text=manager_card(order), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=review_keyboard(order.id))
    context.user_data.pop("edit", None)
    await update.message.reply_text("Данные обновлены.", reply_markup=main_keyboard())
    return ConversationHandler.END


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
    sent = await context.bot.send_message(settings.delivery_group_id, courier_card(order), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=courier_keyboard(order.id))
    updated = repo.transition(order.id, {"draft"}, status="pending", delivery_chat_id=sent.chat_id, delivery_message_id=sent.message_id)
    if not updated:
        await sent.edit_text(f"⚠️ Заказ №{order.order_number} был отменён до отправки")
        await query.answer("Заказ уже обработан", show_alert=True); return
    await query.edit_message_text(f"✅ Заказ №{order.order_number} отправлен курьерам")
    await query.answer("Заказ отправлен")


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
    if action == "onway":
        if order.status == "on_way" and order.courier_id == query.from_user.id:
            await query.answer("Вы уже едете к этому заказу"); return
        order = repo.transition(
            order.id, {"pending"}, status="on_way",
            time_started=datetime.now().astimezone().isoformat(timespec="seconds"),
            guard_courier_id=query.from_user.id, require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Заказ уже взял другой курьер", show_alert=True); return
        await query.edit_message_text(courier_card(order, "🚗 <b>Курьер едет</b>"), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=courier_keyboard(order.id))
        await query.answer("Статус обновлён")
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
            reply_markup=delivery_pending_keyboard(order.id),
        )
        await query.message.reply_text(f"Заказ №{order.order_number}: отправьте фото доставки 📸")


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
        updated = repo.transition(order.id, {"awaiting_photo"}, status="awaiting_amount", delivery_photo=update.message.photo[-1].file_id)
        if not updated:
            await update.message.reply_text("Статус заказа уже изменился."); return
        try:
            await context.bot.edit_message_text(
                chat_id=updated.delivery_chat_id,
                message_id=updated.delivery_message_id,
                text=courier_card(updated, "💰 <b>Фото получено, ожидается сумма</b>"),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=delivery_pending_keyboard(updated.id),
            )
        except Exception:
            logger.exception("Could not update photo confirmation state for order %s", updated.id)
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
    result_text = completed_card(order, timestamp.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M"))
    try:
        await context.bot.edit_message_text(chat_id=order.delivery_chat_id, message_id=order.delivery_message_id, text=result_text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Could not update delivery group message for order %s", order.id)
    try:
        await context.bot.send_photo(order.manager_id, order.delivery_photo, caption=result_text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Could not notify manager %s about order %s", order.manager_id, order.id)
    await update.message.reply_text("✅ Доставка подтверждена.")


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None); context.user_data.pop("edit", None)
    await update.message.reply_text("Действие отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


def register_handlers(application: Application) -> None:
    conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕ Новый заказ$") & filters.ChatType.PRIVATE, new_order), CallbackQueryHandler(begin_edit, pattern=r"^edit:\d+:")],
        states={PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, product)], AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount)], PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)], LOCATION: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, location)], DELIVERY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_time)], COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment)], EDIT_VALUE: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, save_edit)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)], allow_reentry=True,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conversation)
    application.add_handler(CallbackQueryHandler(manager_action, pattern=r"^(send|manager_cancel):\d+$"))
    application.add_handler(CallbackQueryHandler(courier_action, pattern=r"^(onway|complete|cancel):\d+$"))
    application.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), delivery_input), group=1)
