import asyncio
import hashlib
import logging
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta
from html import escape
from math import ceil
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, TypeHandler, filters,
)

from app.bot.keyboards import (
    all_locations_keyboard, completed_keyboard, courier_cancelled_keyboard,
    courier_keyboard, courier_reassignment_confirmation_keyboard,
    courier_selection_keyboard,
    delivery_pending_keyboard, delivery_time_keyboard, edit_input_keyboard,
    location_channel_keyboard, log_location_keyboard, log_order_keyboard,
    main_keyboard,
    manager_cancelled_keyboard, manager_sent_keyboard, on_way_keyboard,
    orders_channel_keyboard, orders_page_keyboard, payment_keyboard, review_keyboard, seller_keyboard,
    product_photo_keyboard, readonly_order_keyboard, sales_card_confirmation_keyboard,
    sales_card_result_keyboard, skip_keyboard, statistics_keyboard, text_location_keyboard,
)
from app.config import Settings
from app.database import OrderRepository
from app.monitor_service import build_delivery_monitor
from app.utils import (
    completed_card, courier_card, enrich_location, extract_text_address, manager_card,
    map_url_provider, normalize_payment, normalize_seller, parse_amount,
    parse_order_details,
)
from app.utils.formatters import (
    STATUS_LABELS, all_locations_card, amount_text,
    money, orders_channel_card, short_address,
)
from app.utils.couriers import (
    courier_group_id, courier_option,
)
from app.utils.parsers import display_phone, extract_http_urls

logger = logging.getLogger(__name__)
SELLER, PRODUCT, PRODUCT_PHOTO, DETAILS, SECOND_LOCATION, PAYMENT, DELIVERY_TIME, COMMENT, EDIT_VALUE = range(9)
MANAGER_EDITABLE_STATUSES = {"draft", "pending", "picked_up", "on_way"}
DELIVERY_ACTIVE_STATUSES = {"pending", "picked_up", "on_way", "awaiting_photo", "awaiting_amount"}
LOCATION_SEPARATOR = "\n".join(["📍" * 11] * 3)
ORDER_PAGE_SIZE = 10
EDIT_CANCEL_TEXT = "❌ Отменить изменение"
TEXT_LOCATION_BUTTON = "📝 Локация текстом"
DELETE_SECOND_LOCATION_TEXT = "🗑 Удалить доп. локацию"
TELEGRAM_SAFE_TEXT_LIMIT = 3800
MAIN_MENU_TEXTS = {
    "➕ Новый заказ",
    "📋 Активные заказы",
    "📋 Мои заказы",
    "📦 Все активные заказы",
    "📚 Все заказы",
    "📋 Все заказы",
    "📊 Статистика",
}
LOCATION_INPUT_FILTER = filters.LOCATION | filters.VENUE | filters.TEXT | filters.CAPTION


def _orders_channel_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    settings = context.application.bot_data.get("settings")
    return getattr(settings, "orders_channel_id", None)


def _message_is_not_modified(error: Exception) -> bool:
    return isinstance(error, BadRequest) and "message is not modified" in str(error).casefold()


def _message_is_missing(error: Exception) -> bool:
    if not isinstance(error, BadRequest):
        return False
    value = str(error).casefold()
    return any(
        marker in value
        for marker in (
            "message to edit not found",
            "message to delete not found",
            "message not found",
            "message_id_invalid",
            "message can't be edited",
        )
    )


def _cleanup_error_is_permanent(error: Exception) -> bool:
    if isinstance(error, Forbidden):
        return True
    if not isinstance(error, BadRequest):
        return False
    value = str(error).casefold()
    return any(marker in value for marker in (
        "message can't be deleted",
        "message cannot be deleted",
        "not enough rights",
        "need administrator rights",
        "chat not found",
        "bot was kicked",
    ))


def _order_sync_lock(application: Application, order_id: int) -> asyncio.Lock:
    locks: dict[int, asyncio.Lock] = application.bot_data.setdefault("order_sync_locks", {})
    return locks.setdefault(order_id, asyncio.Lock())


async def _delete_message_quietly(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as error:
        if not _message_is_missing(error):
            logger.warning("Could not remove superseded Telegram message %s/%s: %s", chat_id, message_id, error)


def _name(user) -> str:
    return user.full_name or user.username or str(user.id)


def _allowed(user_id: int, allowed: frozenset[int]) -> bool:
    return user_id in allowed


def _text(message, *, maximum: int, required: bool = True) -> str | None:
    value = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
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


async def _access_guard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Silently discard updates outside the configured users and work chats."""
    settings: Settings = context.application.bot_data["settings"]
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type == "private":
        if user and user.id in (settings.manager_ids | _allowed_courier_ids(settings)):
            return
        raise ApplicationHandlerStop
    # Channel posts and anonymous service updates have no accountable actor.
    # They must never enter manager conversations or command handlers.
    if user is None:
        raise ApplicationHandlerStop
    allowed_chats = _known_delivery_groups(settings) | frozenset({
        settings.location_channel_id,
        settings.orders_channel_id,
    })
    if chat and chat.id in allowed_chats:
        return
    raise ApplicationHandlerStop


async def _require_manager_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Revalidate persisted manager conversations before every state write."""
    settings: Settings = context.application.bot_data["settings"]
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if getattr(chat, "type", None) == "private" and user and user.id in settings.manager_ids:
        return True
    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, dict):
        user_data.pop("draft", None)
        user_data.pop("edit", None)
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message and getattr(chat, "type", None) == "private":
        await message.reply_text(
            "Доступ менеджера отозван. Незавершённое действие закрыто.",
            reply_markup=ReplyKeyboardRemove(),
        )
    return False


async def _notify_log(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup=None,
) -> None:
    """Publish a lifecycle notification to the shared delivery Log channel."""
    settings: Settings = context.application.bot_data["settings"]
    channel_id = getattr(settings, "orders_channel_id", None)
    if not channel_id:
        return
    send_message = getattr(context.bot, "send_message", None)
    if not callable(send_message):
        return
    try:
        kwargs = {
            "chat_id": channel_id,
            "text": text,
            "parse_mode": ParseMode.HTML,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        await send_message(
            **kwargs,
        )
    except Exception:
        # A notification is secondary to the durable order/event update. The
        # canonical Log card is still synchronized by _sync_order.
        logger.exception("Could not publish delivery notification to Log channel")


_EDIT_FIELD_LABELS = {
    "seller": "владелец заказа",
    "payment_status": "оплата",
    "product": "товар",
    "product_photo": "фото товара",
    "phone": "телефон клиента",
    "location": "основная локация",
    "second_location": "дополнительная локация",
    "amount": "сумма",
    "delivery_time": "время доставки",
    "comment": "комментарий",
}


def _edited_value_text(order, field: str) -> str:
    if field == "seller":
        return order.seller_name or "—"
    if field == "payment_status":
        return "оплачено" if order.payment_status == "paid_at_assembly" else "оплата при доставке"
    if field == "product":
        return order.product
    if field == "phone":
        phones = [display_phone(order.client_phone)]
        if order.client_phone_2 and order.client_phone_2 != order.client_phone:
            phones.append(display_phone(order.client_phone_2))
        return " · ".join(phones)
    if field == "amount":
        return money(order.amount_usd, order.amount_uzs)
    if field == "location":
        return short_address(order)
    if field == "second_location":
        return (
            short_address(order, 2)
            if order.second_address_text or order.second_location_url
            else "удалена"
        )
    if field == "delivery_time":
        return order.delivery_time or "не указано"
    if field == "comment":
        return order.comment or "удалён"
    return "обновлено"


def _courier_may_have_seen_order(order) -> bool:
    """Treat every published pending card as acknowledged for safety checks."""
    return bool(
        order.courier_read_at
        or order.status in {"picked_up", "on_way"}
        or (
            order.status == "pending"
            and order.delivery_chat_id
            and order.delivery_message_id
        )
    )


async def _notify_active_order_edit(
    context: ContextTypes.DEFAULT_TYPE,
    order,
    field: str,
    actor_name: str,
) -> None:
    """Record an active-order edit in Log without pinging the courier group."""
    label = _EDIT_FIELD_LABELS.get(field, "данные заказа")
    value = escape(_edited_value_text(order, field)).replace("\n", " · ")
    text = (
        f"⚠️ <b>Заказ №{order.order_number} изменён менеджером</b>\n"
        f"✏️ {escape(label).capitalize()}: <b>{value}</b>\n"
        f"👤 {escape(actor_name)}\n"
        "Актуальные данные — в карточке заказа."
    )
    await _notify_log(context, text, reply_markup=log_order_keyboard(order))


async def _estimated_delivery_time(
    context: ContextTypes.DEFAULT_TYPE,
    order,
) -> str | None:
    """Estimate arrival using road time plus a 20% and seven-minute reserve."""
    if order.estimated_delivery_at:
        try:
            stored = datetime.fromisoformat(order.estimated_delivery_at)
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=ZoneInfo("Asia/Tashkent"))
            return stored.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M")
        except ValueError:
            logger.warning(
                "Invalid stored delivery estimate for order %s: %r",
                order.id,
                order.estimated_delivery_at,
            )
    routing = context.application.bot_data.get("routing_service")
    if routing is None or not order.time_started:
        return None
    try:
        monitor = build_delivery_monitor(context.application.bot_data["repo"])
        courier_id = order.courier_id or order.assigned_courier_id
        route = next(
            item
            for item in monitor["routes"]
            if item["courier_id"] == courier_id
        )
        points = route.get("current_path") or []
        if len(points) < 2:
            return None
        road_route = await routing.route(points)
        route_seconds = max(60, int(road_route.get("duration_s") or 0))
        duration_seconds = round(route_seconds * 1.20) + 7 * 60
        started = datetime.fromisoformat(order.time_started)
        if started.tzinfo is None:
            started = started.replace(tzinfo=ZoneInfo("Asia/Tashkent"))
        arrival = started + timedelta(seconds=duration_seconds)
        return arrival.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M")
    except Exception:
        logger.exception("Could not estimate arrival time for order %s", order.id)
        return None


async def _store_estimated_delivery_time(
    context: ContextTypes.DEFAULT_TYPE,
    order,
):
    """Calculate and persist the estimate once so every card shows one value."""
    arrival_time = await _estimated_delivery_time(context, order)
    if not arrival_time or order.estimated_delivery_at:
        return order
    started = datetime.fromisoformat(order.time_started)
    if started.tzinfo is None:
        started = started.replace(tzinfo=ZoneInfo("Asia/Tashkent"))
    local_started = started.astimezone(ZoneInfo("Asia/Tashkent"))
    hours, minutes = (int(value) for value in arrival_time.split(":"))
    arrival = local_started.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if arrival < local_started:
        arrival += timedelta(days=1)
    repo: OrderRepository = context.application.bot_data["repo"]
    updated = repo.update(
        order.id,
        expected_updated_at=order.updated_at,
        estimated_delivery_at=arrival.isoformat(timespec="seconds"),
    )
    return updated or repo.get(order.id)


async def _notify_on_way_log(
    context: ContextTypes.DEFAULT_TYPE,
    order,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not getattr(settings, "orders_channel_id", None):
        return
    arrival_time = await _estimated_delivery_time(context, order)
    eta = (
        f"⏱ Примерно к <b>{arrival_time}</b> доставит"
        if arrival_time
        else "⏱ Примерное время доставки уточняется"
    )
    phones = [display_phone(order.client_phone)]
    if order.client_phone_2 and order.client_phone_2 != order.client_phone:
        phones.append(display_phone(order.client_phone_2))
    phone_text = "\n".join(f"📱 {phone}" for phone in phones)
    await _notify_log(
        context,
        f"🚗 <b>{escape(order.courier_name or order.assigned_courier_name or '—')}</b> "
        f"едет к заказу №{order.order_number}\n"
        f"📦 Модель: <b>{escape(order.product)}</b>\n"
        f"{eta}\n"
        f"{phone_text}\n"
        f"📍 {escape(short_address(order))}",
        reply_markup=log_location_keyboard(order),
    )


async def _delivery_group_membership(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> bool | None:
    """Return current delivery-group membership, or ``None`` on API failure.

    Cancellation is deliberately available to every member of the canonical
    courier group, not just configured couriers.  Telegram only guarantees
    lookups for arbitrary members while the bot is an administrator; startup
    validation enforces that prerequisite for every delivery group.
    """
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        logger.exception(
            "Could not verify delivery-group membership for user %s in chat %s",
            user_id,
            chat_id,
        )
        return None
    status = str(getattr(member, "status", ""))
    if status in {"creator", "administrator", "member"}:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


def _courier_waiting_pickup_text(
    repo: OrderRepository,
    courier_id: int,
    courier_name: str,
) -> str:
    """List the assigned products that a courier has not picked up yet."""
    waiting = [
        order
        for order in repo.list_active()
        if order.status == "pending" and order.assigned_courier_id == courier_id
    ]
    lines = [f"⏳ <b>Ждём курьера {escape(courier_name)}</b>"]
    omitted = 0
    for index, order in enumerate(waiting, start=1):
        manager = order.seller_name or order.manager_name or "—"
        line = (
            f"{index}. Заказ №{order.order_number} · "
            f"{escape(manager[:80])} · {escape(order.product[:200])}"
        )
        if len("\n".join([*lines, line])) > TELEGRAM_SAFE_TEXT_LIMIT - 80:
            omitted = len(waiting) - index + 1
            break
        lines.append(line)
    if not waiting:
        lines.append("Все товары уже забраны.")
    elif omitted:
        lines.append(f"…и ещё {omitted} заказов. Полный список — в мониторинге.")
    return "\n".join(lines)


def _waiting_pickup_reminder_messages(repo: OrderRepository) -> list[str]:
    """Build lossless, Telegram-sized Log reminders for uncollected products."""
    waiting = [
        order
        for order in repo.list_active()
        if order.status == "pending" and order.assigned_courier_id is not None
    ]
    if not waiting:
        return []
    waiting.sort(key=lambda order: (
        (order.assigned_courier_name or "").casefold(),
        order.order_number,
    ))

    entries: list[tuple[str, str]] = []
    for index, order in enumerate(waiting, start=1):
        courier = " ".join(
            (order.assigned_courier_name or order.courier_name or "Курьер").split()
        )[:60]
        seller = " ".join(
            (order.seller_name or order.manager_name or "—").split()
        )[:60]
        product = " ".join((order.product or "Без модели").split())[:140]
        entries.append(
            (
                courier,
                f"{index}. Заказ №{order.order_number} · "
                f"{escape(seller)} · {escape(product)}",
            )
        )

    # Keep enough headroom for the repeated title/footer and part counter.
    chunks: list[list[str]] = []
    current: list[str] = []
    current_courier: str | None = None
    for courier, line in entries:
        courier_changed = courier != current_courier
        addition = (
            ([""] if current else [])
            + [f"🚚 <b>{escape(courier)}</b>"]
            if courier_changed
            else []
        ) + [line]
        if current and len("\n".join([*current, *addition])) > 3300:
            chunks.append(current)
            current = [f"🚚 <b>{escape(courier)}</b>", line]
        else:
            current.extend(addition)
        current_courier = courier
    if current:
        chunks.append(current)

    messages: list[str] = []
    for part, chunk in enumerate(chunks, start=1):
        part_text = f" · часть {part}/{len(chunks)}" if len(chunks) > 1 else ""
        messages.append(
            f"⏰ <b>Заказы ещё не забраны</b>{part_text}\n\n"
            + "\n".join(chunk)
            + "\n\n❓ Курьеры, забрали товары?"
        )
    return messages


async def _send_post_delivery_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    order,
) -> None:
    """Ask for optional evidence without reopening or blocking the order."""
    if not order.delivery_chat_id:
        return
    try:
        courier = " ".join(
            (order.courier_name or order.assigned_courier_name or "Курьер").split()
        )[:100]
        product = " ".join((order.product or "Без модели").split())[:200]
        await context.bot.send_message(
            chat_id=order.delivery_chat_id,
            text=(
                f"🚚 Заказ №{order.order_number} · {product}\n"
                f"{courier}, отправьте фото и цену товара 📸💰"
            ),
        )
    except Exception:
        logger.exception("Could not send optional delivery prompt for order %s", order.id)


async def daily_delivery_log_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    # Kept only so buttons on already-published old posts stop cleanly instead
    # of spinning. New Log posts no longer include chronology controls.
    await update.callback_query.answer("Хронология в Log отключена", show_alert=True)


def _message_location_urls(message) -> list[str]:
    """Extract visible and hidden Telegram map links in message order."""
    candidates: list[str] = []
    for text in (getattr(message, "text", None), getattr(message, "caption", None)):
        if not text:
            continue
        candidates.extend(extract_http_urls(text))

    entity_groups = (
        (getattr(message, "entities", None) or [], getattr(message, "parse_entity", None)),
        (
            getattr(message, "caption_entities", None) or [],
            getattr(message, "parse_caption_entity", None),
        ),
    )
    for entities, parse_entity in entity_groups:
        for entity in entities:
            entity_type = getattr(entity, "type", "")
            entity_type = getattr(entity_type, "value", entity_type)
            candidate = getattr(entity, "url", None) if entity_type == "text_link" else None
            if entity_type == "url" and callable(parse_entity):
                try:
                    candidate = parse_entity(entity)
                except (TypeError, ValueError):
                    candidate = None
            if candidate:
                candidates.append(str(candidate))

    result: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip().rstrip(".,;:!?)>]}")
        if map_url_provider(candidate) is not None and candidate not in result:
            result.append(candidate)
    return result[:2]


async def _location_values(message) -> dict:
    native_location = getattr(message, "location", None)
    venue = getattr(message, "venue", None)
    if native_location is None and venue is not None:
        native_location = getattr(venue, "location", None)
    if native_location:
        latitude, longitude = native_location.latitude, native_location.longitude
        url = f"https://yandex.uz/maps/?ll={longitude:.6f}%2C{latitude:.6f}&z=17"
    else:
        urls = _message_location_urls(message)
        if not urls:
            raise ValueError(
                "Отправьте Telegram Location или ссылку Google, Яндекс, 2GIS, Apple Maps, "
                "OpenStreetMap или Waze."
            )
        url = urls[0]
        latitude = longitude = None
    values = await enrich_location(latitude, longitude, url)
    return _validated_location(values)


def _validated_location(values: dict) -> dict:
    if values["latitude"] is None or values["longitude"] is None:
        raise ValueError(
            "Не удалось определить координаты. Отправьте точку через «Поделиться» в приложении карт "
            "или Telegram Location."
        )
    if not (37.0 <= values["latitude"] <= 46.0 and 55.0 <= values["longitude"] <= 74.0):
        raise ValueError("Координаты находятся за пределами Узбекистана. Проверьте локацию.")
    return values


def _delivery_message(order):
    if order.status == "completed":
        try:
            delivered = datetime.fromisoformat(order.delivered_at) if order.delivered_at else datetime.now().astimezone()
        except ValueError:
            delivered = datetime.now().astimezone()
        return (
            completed_card(order, delivered.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M")),
            completed_keyboard(order),
        )
    if order.status == "cancelled":
        return (
            courier_card(order, "❌ <b>Заказ отменён</b>"),
            courier_cancelled_keyboard(order),
        )
    if order.status == "on_way":
        return courier_card(order, "🚗 <b>Курьер едет</b>"), on_way_keyboard(order)
    if order.status == "picked_up":
        return courier_card(order, "📦 <b>Товар у курьера</b>"), courier_keyboard(order)
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
    except RetryAfter:
        raise
    except Exception as error:
        if _message_is_not_modified(error):
            return True
        if _message_is_missing(error):
            repo: OrderRepository = context.application.bot_data["repo"]
            cleared = repo.update(
                order.id,
                expected_updated_at=order.updated_at,
                delivery_chat_id=None,
                delivery_message_id=None,
            )
            if order.status in {"completed", "cancelled"}:
                logger.info(
                    "Closed delivery message disappeared for order %s; it will not be recreated",
                    order.id,
                )
                return cleared is not None
            logger.warning("Delivery message disappeared for order %s; it will be recreated", order.id)
            return False
        logger.exception("Could not refresh delivery message for order %s", order.id)
        return False


def _manager_order_keyboard(order):
    if order.status == "draft":
        return review_keyboard(order.id)
    if order.status in MANAGER_EDITABLE_STATUSES:
        return manager_sent_keyboard(order)
    if order.status == "cancelled" and order.courier_id is None and not order.delivery_message_id:
        return manager_cancelled_keyboard(order.id)
    return None


async def _refresh_manager_message(context: ContextTypes.DEFAULT_TYPE, order) -> bool:
    repo: OrderRepository = context.application.bot_data["repo"]
    if not order.manager_chat_id or not order.manager_message_id:
        # Closed orders already trigger a separate manager notification. Do
        # not create an extra permanent card when an old installation has no
        # tracked manager-card reference.
        if order.status not in MANAGER_EDITABLE_STATUSES | {"draft"}:
            return True
        try:
            sent = await context.bot.send_message(
                chat_id=order.manager_id,
                text=manager_card(order, sent=order.status != "draft"),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_manager_order_keyboard(order),
            )
        except RetryAfter:
            raise
        except Exception:
            logger.exception("Could not recreate manager message for order %s", order.id)
            return False
        if not isinstance(getattr(sent, "chat_id", None), int) or not isinstance(
            getattr(sent, "message_id", None), int
        ):
            logger.error("Telegram returned an invalid manager message reference for order %s", order.id)
            return False
        updated = repo.update(
            order.id,
            expected_updated_at=order.updated_at,
            manager_chat_id=sent.chat_id,
            manager_message_id=sent.message_id,
        )
        if updated:
            return True
        repo.enqueue_cleanup_messages(order.id, [(sent.chat_id, sent.message_id)])
        await _process_cleanup_messages(context, order_id=order.id)
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=order.manager_chat_id,
            message_id=order.manager_message_id,
            text=manager_card(order, sent=order.status != "draft"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_manager_order_keyboard(order),
        )
        return True
    except RetryAfter:
        raise
    except Exception as error:
        if _message_is_not_modified(error):
            return True
        if _message_is_missing(error):
            cleared = repo.update(
                order.id,
                expected_updated_at=order.updated_at,
                manager_chat_id=None,
                manager_message_id=None,
            )
            if cleared:
                return await _refresh_manager_message(context, cleared)
            return False
        logger.exception("Could not refresh manager message for order %s", order.id)
        return False


async def _refresh_orders_channel_message(context: ContextTypes.DEFAULT_TYPE, order) -> bool:
    """Create or update the single shared manager-journal card for an order."""
    settings: Settings = context.application.bot_data["settings"]
    channel_id = getattr(settings, "orders_channel_id", None)
    if not channel_id:
        return True
    repo: OrderRepository = context.application.bot_data["repo"]
    text = orders_channel_card(order)
    keyboard = orders_channel_keyboard(order)
    if not order.orders_channel_chat_id or not order.orders_channel_message_id:
        try:
            sent = await context.bot.send_message(
                chat_id=channel_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except RetryAfter:
            raise
        except Exception:
            logger.exception("Could not publish orders-channel card for order %s", order.id)
            return False
        if not isinstance(getattr(sent, "chat_id", None), int) or not isinstance(
            getattr(sent, "message_id", None), int
        ):
            logger.error("Telegram returned an invalid orders-channel reference for order %s", order.id)
            return False
        updated = repo.update(
            order.id,
            expected_updated_at=order.updated_at,
            orders_channel_chat_id=sent.chat_id,
            orders_channel_message_id=sent.message_id,
        )
        if updated:
            return True
        repo.enqueue_cleanup_messages(order.id, [(sent.chat_id, sent.message_id)])
        await _process_cleanup_messages(context, order_id=order.id)
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=order.orders_channel_chat_id,
            message_id=order.orders_channel_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return True
    except RetryAfter:
        raise
    except Exception as error:
        if _message_is_not_modified(error):
            return True
        if _message_is_missing(error):
            cleared = repo.update(
                order.id,
                expected_updated_at=order.updated_at,
                orders_channel_chat_id=None,
                orders_channel_message_id=None,
            )
            if cleared:
                return await _refresh_orders_channel_message(context, cleared)
            return False
        logger.exception("Could not refresh orders-channel card for order %s", order.id)
        return False


async def _set_location_marker(
    context: ContextTypes.DEFAULT_TYPE,
    order,
    location_number: int | None = None,
) -> bool:
    """Refresh the functional pin buttons and migrate legacy location replies."""
    numbers = (location_number,) if location_number else (1, 2)
    success = True
    repo: OrderRepository = context.application.bot_data["repo"]
    for number in numbers:
        order = repo.get(order.id)
        if not order:
            return False
        if number == 2:
            chat_id = order.second_location_chat_id
            message_id = order.second_location_message_id
            details_message_id = order.second_location_details_message_id
            footer_message_id = order.second_location_footer_message_id
            details_field = "second_location_details_message_id"
        else:
            chat_id = order.location_chat_id
            message_id = order.location_message_id
            details_message_id = order.location_details_message_id
            footer_message_id = order.location_footer_message_id
            details_field = "location_details_message_id"
        if not chat_id or not message_id:
            continue

        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=location_channel_keyboard(
                    order,
                    location_number=number,
                ),
            )
        except RetryAfter:
            raise
        except Exception as error:
            if not _message_is_not_modified(error):
                if _message_is_missing(error):
                    prefix = "second_" if number == 2 else ""
                    cleanup_messages = [
                        (chat_id, cleanup_id)
                        for cleanup_id in (details_message_id, message_id, footer_message_id)
                        if cleanup_id
                    ]
                    cleared = repo.transition(
                        order.id,
                        {order.status},
                        expected_updated_at=order.updated_at,
                        cleanup_messages=cleanup_messages,
                        **{
                            f"{prefix}location_chat_id": None,
                            f"{prefix}location_message_id": None,
                            f"{prefix}location_details_message_id": None,
                            f"{prefix}location_footer_message_id": None,
                        },
                    )
                    if cleared:
                        await _process_cleanup_messages(context, order_id=order.id)
                    logger.warning(
                        "Location pin %s disappeared for order %s; it will be recreated",
                        number,
                        order.id,
                    )
                else:
                    logger.exception(
                        "Could not validate location %s pin for order %s",
                        number,
                        order.id,
                    )
                success = False
                continue

        # Rows created before the separator feature used the details field for
        # an explanatory reply below the pin. Without a footer it is legacy
        # text, so remove it instead of turning it into a misplaced separator.
        if details_message_id and not footer_message_id:
            cleaned = repo.transition(
                order.id,
                {order.status},
                expected_updated_at=order.updated_at,
                cleanup_messages=[(chat_id, details_message_id)],
                **{details_field: None},
            )
            if not cleaned:
                success = False
                continue
            if not await _process_cleanup_messages(context, order_id=order.id):
                success = False
            details_message_id = None

        # The header and footer are static decoration. Re-editing them during
        # every status/courier change adds two Telegram API requests per pin
        # and a timeout used to make the whole order look unsynchronized even
        # though every functional card and button had already been updated.
        # They are created together with the pin and removed when the location
        # is replaced, so there is nothing to refresh here.
    return success


def _location_publication_fields(
    location_number: int,
    *,
    chat_id: int,
    message_id: int,
    details_message_id: int | None = None,
    footer_message_id: int | None = None,
) -> dict:
    prefix = "second_" if location_number == 2 else ""
    return {
        f"{prefix}location_chat_id": chat_id,
        f"{prefix}location_message_id": message_id,
        f"{prefix}location_details_message_id": details_message_id,
        f"{prefix}location_footer_message_id": footer_message_id,
    }


async def _send_location_messages(
    context: ContextTypes.DEFAULT_TYPE,
    order,
    location_number: int,
) -> dict:
    if location_number == 2:
        latitude, longitude = order.second_latitude, order.second_longitude
    else:
        latitude, longitude = order.latitude, order.longitude
    if latitude is None or longitude is None:
        raise ValueError("Order has no coordinates")
    settings: Settings = context.application.bot_data["settings"]
    published: list[tuple[int, int]] = []
    try:
        header = await context.bot.send_message(
            chat_id=settings.location_channel_id,
            text=LOCATION_SEPARATOR,
        )
        published.append((header.chat_id, header.message_id))
        pin = await context.bot.send_location(
            chat_id=settings.location_channel_id,
            latitude=latitude,
            longitude=longitude,
            reply_markup=location_channel_keyboard(order, location_number=location_number),
        )
        published.append((pin.chat_id, pin.message_id))
        footer = await context.bot.send_message(
            chat_id=settings.location_channel_id,
            text=LOCATION_SEPARATOR,
        )
        published.append((footer.chat_id, footer.message_id))
    except Exception:
        repo: OrderRepository | None = context.application.bot_data.get("repo")
        if repo and published:
            repo.enqueue_cleanup_messages(order.id, published)
            await _process_cleanup_messages(context, order_id=order.id)
        else:
            for chat_id, message_id in published:
                await _delete_message_quietly(context, chat_id, message_id)
        raise
    return _location_publication_fields(
        location_number,
        chat_id=pin.chat_id,
        message_id=pin.message_id,
        details_message_id=header.message_id,
        footer_message_id=footer.message_id,
    )


async def _publish_location(
    context: ContextTypes.DEFAULT_TYPE,
    repo: OrderRepository,
    order,
    location_number: int = 1,
):
    update_fields = await _send_location_messages(context, order, location_number)
    updated = repo.update(
        order.id,
        expected_updated_at=order.updated_at,
        **update_fields,
    )
    if updated:
        return updated
    repo.enqueue_cleanup_messages(
        order.id,
        [
            (update_fields[f"{'second_' if location_number == 2 else ''}location_chat_id"], message_id)
            for message_id in (
                update_fields[f"{'second_' if location_number == 2 else ''}location_details_message_id"],
                update_fields[f"{'second_' if location_number == 2 else ''}location_message_id"],
                update_fields[f"{'second_' if location_number == 2 else ''}location_footer_message_id"],
            )
        ],
    )
    await _process_cleanup_messages(context, order_id=order.id)
    raise RuntimeError("Order changed while its location was being published")


async def _process_cleanup_messages(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    order_id: int | None = None,
    limit: int = 100,
) -> bool:
    """Delete superseded Telegram publications from the durable outbox."""
    repo: OrderRepository = context.application.bot_data["repo"]
    success = True
    for item in repo.list_cleanup_messages(limit=limit, order_id=order_id):
        try:
            await context.bot.delete_message(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
            )
        except RetryAfter:
            raise
        except Exception as error:
            if _message_is_missing(error):
                repo.mark_cleanup_done(item["id"])
            else:
                success = False
                repo.mark_cleanup_failed(
                    item["id"],
                    str(error),
                    permanent=_cleanup_error_is_permanent(error),
                )
                logger.warning(
                    "Could not clean superseded Telegram message %s/%s: %s",
                    item["chat_id"],
                    item["message_id"],
                    error,
                )
        else:
            repo.mark_cleanup_done(item["id"])
    return success


def _order_should_be_in_delivery_group(order) -> bool:
    if order.status in DELIVERY_ACTIVE_STATUSES:
        return True
    # A closed card may still be edited in place, but a deleted/missing old
    # card must never be published again during deploy reconciliation.
    return (
        order.status in {"completed", "cancelled"}
        and bool(order.delivery_chat_id and order.delivery_message_id)
    )


def _target_delivery_group(settings: Settings, order) -> int:
    """Resolve the assigned courier group while preserving legacy orders."""
    # A known assignment must never jump to the fallback group merely because
    # an environment file was temporarily incomplete. Settings.load rejects
    # that configuration, while this remains a defence for legacy/test rows.
    assigned_group = courier_group_id(order.assigned_courier_id)
    if assigned_group is not None:
        return assigned_group
    return settings.delivery_group_id


def _allowed_courier_ids(settings: Settings) -> frozenset[int]:
    return frozenset(getattr(settings, "courier_ids", ()))


def _known_delivery_groups(settings: Settings) -> frozenset[int]:
    configured_ids = frozenset(getattr(settings, "courier_ids", ()))
    configured_groups = {
        group_id
        for courier_id in configured_ids
        if (group_id := courier_group_id(courier_id)) is not None
    }
    return frozenset(configured_groups) | frozenset({settings.delivery_group_id})


async def _sync_order(context: ContextTypes.DEFAULT_TYPE, order_id: int) -> tuple[object | None, bool]:
    async with _order_sync_lock(context.application, order_id):
        return await _sync_order_locked(context, order_id)


def _reset_mismatched_publications(
    repo: OrderRepository,
    settings: Settings,
    order,
):
    """Clear references that belong to chats replaced in environment config."""
    fields: dict[str, None] = {}
    cleanup: list[tuple[int, int]] = []
    expected_delivery_chat_id = _target_delivery_group(settings, order)
    if order.delivery_chat_id and order.delivery_chat_id != expected_delivery_chat_id:
        if order.delivery_message_id:
            cleanup.append((order.delivery_chat_id, order.delivery_message_id))
        fields.update(delivery_chat_id=None, delivery_message_id=None)
    orders_channel_id = getattr(settings, "orders_channel_id", None)
    if (
        orders_channel_id
        and order.orders_channel_chat_id
        and order.orders_channel_chat_id != orders_channel_id
    ):
        if order.orders_channel_message_id:
            cleanup.append((order.orders_channel_chat_id, order.orders_channel_message_id))
        fields.update(orders_channel_chat_id=None, orders_channel_message_id=None)
    for prefix in ("", "second_"):
        chat_id = getattr(order, f"{prefix}location_chat_id")
        if chat_id and chat_id != settings.location_channel_id:
            for suffix in (
                "location_details_message_id",
                "location_message_id",
                "location_footer_message_id",
            ):
                message_id = getattr(order, f"{prefix}{suffix}")
                if message_id:
                    cleanup.append((chat_id, message_id))
            fields.update({
                f"{prefix}location_chat_id": None,
                f"{prefix}location_message_id": None,
                f"{prefix}location_details_message_id": None,
                f"{prefix}location_footer_message_id": None,
            })
    if not fields:
        return order
    # Clear references and enqueue their cleanup in the same SQLite
    # transaction. If the optimistic guard loses a race, the still-canonical
    # old messages must not be scheduled for deletion.
    return repo.transition(
        order.id,
        {order.status},
        expected_updated_at=order.updated_at,
        cleanup_messages=cleanup,
        **fields,
    )


async def _sync_order_locked(context: ContextTypes.DEFAULT_TYPE, order_id: int) -> tuple[object | None, bool]:
    """Bring Telegram group/channel/private cards in line with SQLite state."""
    repo: OrderRepository = context.application.bot_data["repo"]
    settings: Settings = context.application.bot_data["settings"]
    order = repo.get(order_id)
    if not order:
        return None, False
    for _ in range(3):
        reset = _reset_mismatched_publications(repo, settings, order)
        if reset is order:
            break
        if reset:
            order = reset
            break
        order = repo.get(order_id)
        if not order:
            return None, False
    else:
        return order, False
    success = True
    should_publish = _order_should_be_in_delivery_group(order)
    may_create_publications = order.status in DELIVERY_ACTIVE_STATUSES

    if may_create_publications and (not order.delivery_chat_id or not order.delivery_message_id):
        text, keyboard = _delivery_message(order)
        try:
            sent = await context.bot.send_message(
                _target_delivery_group(settings, order),
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            order = repo.update(
                order.id,
                expected_updated_at=order.updated_at,
                delivery_chat_id=sent.chat_id,
                delivery_message_id=sent.message_id,
            )
            if not order:
                repo.enqueue_cleanup_messages(order_id, [(sent.chat_id, sent.message_id)])
                await _process_cleanup_messages(context, order_id=order_id)
                success = False
        except RetryAfter:
            raise
        except Exception:
            success = False
            logger.exception("Could not publish delivery message for order %s", order_id)

    if should_publish:
        for location_number in (1, 2):
            order = repo.get(order_id)
            if location_number == 2:
                has_coordinates = order.second_latitude is not None and order.second_longitude is not None
                has_pin = bool(order.second_location_chat_id and order.second_location_message_id)
            else:
                has_coordinates = order.latitude is not None and order.longitude is not None
                has_pin = bool(order.location_chat_id and order.location_message_id)
            if not has_coordinates:
                continue
            if not has_pin:
                if not may_create_publications:
                    continue
                try:
                    order = await _publish_location(context, repo, order, location_number)
                except RetryAfter:
                    raise
                except Exception:
                    success = False
                    logger.exception("Could not publish location %s for order %s", location_number, order_id)

        order = repo.get(order_id)
        if order and not await _set_location_marker(context, order):
            success = False
        order = repo.get(order_id)
        if order and order.delivery_message_id and not await _refresh_delivery_message(context, order):
            success = False

    order = repo.get(order_id)
    if not order or not await _refresh_manager_message(context, order):
        success = False
    order = repo.get(order_id)
    if not order or not await _refresh_orders_channel_message(context, order):
        success = False
    order = repo.get(order_id)
    if success and order.sync_needed:
        repo.mark_synced(order.id, expected_updated_at=order.updated_at)
        order = repo.get(order_id)
    return order, success


async def _retry_order_sync(application: Application, order_id: int) -> None:
    pending: set[int] = application.bot_data.setdefault("sync_retry_orders", set())
    try:
        context = SimpleNamespace(application=application, bot=application.bot)
        for delay in (2, 10, 30):
            await asyncio.sleep(delay)
            order = application.bot_data["repo"].get(order_id)
            if not order or not order.sync_needed:
                return
            _, success = await _sync_order(context, order_id)
            if success:
                return
    finally:
        pending.discard(order_id)


def _schedule_sync_retry(context: ContextTypes.DEFAULT_TYPE, order_id: int) -> None:
    pending: set[int] = context.application.bot_data.setdefault("sync_retry_orders", set())
    if order_id in pending:
        return
    create_task = getattr(context.application, "create_task", None)
    if not callable(create_task):
        logger.warning("Cannot schedule retry for order %s: application task runner is unavailable", order_id)
        return
    pending.add(order_id)
    create_task(
        _retry_order_sync(context.application, order_id),
        name=f"delivery-sync-{order_id}",
    )


async def _finish_status_change(
    context: ContextTypes.DEFAULT_TYPE,
    query,
    order,
    text: str,
    keyboard,
) -> bool:
    async with _order_sync_lock(context.application, order.id):
        return await _finish_status_change_locked(context, query, order, text, keyboard)


async def _finish_status_change_locked(
    context: ContextTypes.DEFAULT_TYPE,
    query,
    order,
    text: str,
    keyboard,
) -> bool:
    success = True
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except RetryAfter:
        raise
    except Exception as error:
        if not _message_is_not_modified(error):
            success = False
            logger.exception("Could not update delivery callback message for order %s", order.id)
    if not await _set_location_marker(context, order):
        success = False
    latest = context.application.bot_data["repo"].get(order.id)
    if not await _refresh_manager_message(context, latest):
        success = False
    latest = context.application.bot_data["repo"].get(order.id)
    if not await _refresh_orders_channel_message(context, latest):
        success = False
    latest = context.application.bot_data["repo"].get(order.id)
    if success and latest.sync_needed:
        context.application.bot_data["repo"].mark_synced(
            latest.id,
            expected_updated_at=latest.updated_at,
        )
    elif not success:
        _schedule_sync_retry(context, order.id)
    return success


async def validate_delivery_configuration(application: Application) -> None:
    """Validate chats and the concrete Telegram rights required by the bot."""
    settings: Settings = application.bot_data["settings"]
    for delivery_group_id in sorted(_known_delivery_groups(settings)):
        delivery_chat = await application.bot.get_chat(delivery_group_id)
        if delivery_chat.type not in {"group", "supergroup"}:
            raise RuntimeError(
                f"Delivery chat {delivery_group_id} must be a Telegram group or supergroup"
            )
        delivery_member = await application.bot.get_chat_member(
            delivery_group_id,
            application.bot.id,
        )
        if delivery_member.status not in {"administrator", "creator"}:
            raise RuntimeError(
                "The delivery bot must be an administrator in group "
                f"{delivery_group_id} to verify who cancels orders"
            )

    # A configured courier who cannot see their own group receives no order at
    # all, so own-group membership is fatal. The private location channel is
    # supplementary: direct map/navigation URLs in the courier card still
    # work, therefore a missing subscription is warned without taking the
    # whole delivery service offline.
    for courier_id in sorted(_allowed_courier_ids(settings)):
        configured = courier_option(courier_id)
        if not configured:
            raise RuntimeError(f"Courier {courier_id} has no configured name/group")
        courier_member = await application.bot.get_chat_member(
            configured.group_id,
            courier_id,
        )
        if courier_member.status in {"left", "kicked"}:
            raise RuntimeError(
                f"Courier {configured.name} must be a member of group {configured.group_id}"
            )
    location_chat = await application.bot.get_chat(settings.location_channel_id)
    if location_chat.type not in {"channel", "supergroup"}:
        raise RuntimeError("DELIVERY_LOCATION_CHANNEL_ID must point to a channel or supergroup")
    location_member = await application.bot.get_chat_member(
        settings.location_channel_id,
        application.bot.id,
    )
    if location_member.status not in {"administrator", "creator"}:
        raise RuntimeError("The delivery bot must be an administrator in the location channel")
    if location_chat.type == "channel" and location_member.status != "creator":
        if not getattr(location_member, "can_post_messages", False):
            raise RuntimeError("The delivery bot must be allowed to post in the location channel")
        if not getattr(location_member, "can_edit_messages", False):
            raise RuntimeError("The delivery bot must be allowed to edit location channel posts")
    if location_member.status != "creator" and not getattr(
        location_member,
        "can_delete_messages",
        False,
    ):
        raise RuntimeError("The delivery bot must be allowed to delete obsolete location posts")
    warned_location_members: set[int] = application.bot_data.setdefault(
        "warned_missing_location_members",
        set(),
    )
    for courier_id in sorted(_allowed_courier_ids(settings)):
        configured = courier_option(courier_id)
        courier_member = await application.bot.get_chat_member(
            settings.location_channel_id,
            courier_id,
        )
        if courier_member.status in {"left", "kicked"}:
            if courier_id not in warned_location_members:
                logger.warning(
                    "Courier %s is not a member of the location channel %s; "
                    "direct map links remain available",
                    configured.name,
                    settings.location_channel_id,
                )
                warned_location_members.add(courier_id)
        else:
            # If membership is restored, warn again on a future regression.
            warned_location_members.discard(courier_id)

    orders_channel_id = getattr(settings, "orders_channel_id", None)
    if orders_channel_id:
        orders_chat = await application.bot.get_chat(orders_channel_id)
        if orders_chat.type not in {"channel", "supergroup"}:
            raise RuntimeError("DELIVERY_ORDERS_CHANNEL_ID must point to a channel or supergroup")
        orders_member = await application.bot.get_chat_member(
            orders_channel_id,
            application.bot.id,
        )
        if orders_member.status not in {"administrator", "creator"}:
            raise RuntimeError("The delivery bot must be an administrator in the orders channel")
        if orders_chat.type == "channel" and orders_member.status != "creator":
            if not getattr(orders_member, "can_post_messages", False):
                raise RuntimeError("The delivery bot must be allowed to post in the orders channel")
            if not getattr(orders_member, "can_edit_messages", False):
                raise RuntimeError("The delivery bot must be allowed to edit orders channel posts")
        if orders_member.status != "creator" and not getattr(
            orders_member,
            "can_delete_messages",
            False,
        ):
            raise RuntimeError("The delivery bot must be allowed to delete obsolete orders posts")



async def reconcile_orders_on_start(application: Application) -> None:
    """Repair interrupted publications after a deploy or transient Telegram failure."""
    await validate_delivery_configuration(application)

    repo: OrderRepository = application.bot_data["repo"]
    candidates = {order.id: order for order in repo.list_needing_sync(limit=100)}
    # Existing installations did not have sync_needed. Validate every open
    # order, including drafts whose manager card disappeared or whose first
    # send failed after the SQLite insert.
    for order in repo.list_open():
        candidates[order.id] = order
    orders_channel_id = getattr(application.bot_data["settings"], "orders_channel_id", None)
    list_channel_reconcile = getattr(repo, "list_orders_channel_reconcile", None)
    if orders_channel_id and callable(list_channel_reconcile):
        for order in list_channel_reconcile(orders_channel_id):
            candidates[order.id] = order
    context = SimpleNamespace(application=application, bot=application.bot)
    for order_id in sorted(candidates):
        try:
            await _sync_order(context, order_id)
        except RetryAfter:
            raise
        except Exception:
            logger.exception("Startup reconciliation failed for order %s", order_id)
    await _process_cleanup_messages(context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    # This callback is also a ConversationHandler fallback. Clear both kinds
    # of persisted input before returning END, including for users whose
    # permissions changed while a conversation was stored.
    draft = context.user_data.get("draft") or {}
    repo: OrderRepository | None = context.application.bot_data.get("repo")
    committed = repo.get_by_creation_token(draft.get("creation_token")) if repo else None
    context.user_data.pop("draft", None)
    context.user_data.pop("edit", None)
    if update.effective_chat.type != "private":
        return ConversationHandler.END
    if not _allowed(update.effective_user.id, settings.manager_ids):
        await update.message.reply_text("У вас нет доступа к созданию заказов.")
        return ConversationHandler.END
    prefix = ""
    if committed and committed.status == "draft":
        _, recovered = await _sync_order(context, committed.id)
        if not recovered:
            _schedule_sync_retry(context, committed.id)
        prefix = f"Заказ №{committed.order_number} уже сохранён. Его карточка будет восстановлена автоматически.\n\n"
    await update.message.reply_text(prefix + "Бот доставки TEXNIKACH готов.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def _end_edit_on_global_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Silently finish group=1 edit after group=0 handled /start or /cancel."""
    context.user_data.pop("edit", None)
    return ConversationHandler.END


async def _end_creation_with_order_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Discard a draft when the manager intentionally opens another screen."""
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft") or {}
    repo: OrderRepository = context.application.bot_data["repo"]
    committed = repo.get_by_creation_token(draft.get("creation_token"))
    context.user_data.pop("draft", None)
    if committed and committed.status == "draft":
        _, recovered = await _sync_order(context, committed.id)
        if not recovered:
            _schedule_sync_retry(context, committed.id)
    text = (update.message.text or "").strip()
    await _show_orders(
        update,
        context,
        active_only=text in {"📋 Активные заказы", "📋 Мои заказы", "📦 Все активные заказы"},
    )
    return ConversationHandler.END


async def _end_creation_with_map(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft") or {}
    repo: OrderRepository = context.application.bot_data["repo"]
    committed = repo.get_by_creation_token(draft.get("creation_token"))
    context.user_data.pop("draft", None)
    if committed and committed.status == "draft":
        _, recovered = await _sync_order(context, committed.id)
        if not recovered:
            _schedule_sync_retry(context, committed.id)
    await show_all_locations(update, context)
    return ConversationHandler.END


async def _end_creation_with_statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft") or {}
    repo: OrderRepository = context.application.bot_data["repo"]
    committed = repo.get_by_creation_token(draft.get("creation_token"))
    context.user_data.pop("draft", None)
    if committed and committed.status == "draft":
        _, recovered = await _sync_order(context, committed.id)
        if not recovered:
            _schedule_sync_retry(context, committed.id)
    await show_statistics(update, context)
    return ConversationHandler.END


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if (
        update.effective_chat.type != "private"
        or update.effective_user.id not in settings.manager_ids
    ):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Статистика доступна менеджерам в личном чате с ботом."
            )
        return
    context.user_data.pop("edit", None)
    await update.effective_message.reply_text(
        "📊 <b>Статистика доставки TEXNIKACH</b>\n\n"
        "Выберите день или курьера. На сайте доступны показатели, "
        "хронология, интерактивная карта и PNG с очередностью доставок.\n\n"
        "🔐 Вход выполняется через Telegram без отдельного пароля.",
        parse_mode=ParseMode.HTML,
        reply_markup=statistics_keyboard(settings.stats_url),
    )


def _monitor_url(settings: Settings) -> str | None:
    stats_url = (getattr(settings, "stats_url", "") or "").strip().rstrip("/")
    if not stats_url:
        return None
    if stats_url.endswith("/monitoring"):
        return stats_url + "/delivery/live"
    return stats_url.rsplit("/", 1)[0] + "/monitor"


async def _show_orders(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    active_only: bool,
    page: int = 0,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.type != "private" or update.effective_user.id not in settings.manager_ids:
        await update.effective_message.reply_text("Список заказов доступен менеджерам в личном чате.")
        return
    # A main-menu action always exits a pending one-field edit, so its label
    # can never be written into an order by the group=1 edit conversation.
    context.user_data.pop("edit", None)
    query = update.callback_query
    if query:
        # Telegram stops showing the loading spinner immediately. Database and
        # map work below must not delay acknowledgement of a page button.
        await query.answer()
    repo: OrderRepository = context.application.bot_data["repo"]
    total = repo.count_open() if active_only else repo.count_all()
    if not total:
        text = "Активных заказов нет." if active_only else "Заказов пока нет."
        if query:
            try:
                await query.edit_message_text(text)
            except Exception as error:
                if not _message_is_not_modified(error):
                    raise
        else:
            await update.effective_message.reply_text(text, reply_markup=main_keyboard())
        return
    total_pages = max(1, ceil(total / ORDER_PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    offset = page * ORDER_PAGE_SIZE
    orders = (
        repo.list_open_page(limit=ORDER_PAGE_SIZE, offset=offset)
        if active_only
        else repo.list_all_page(limit=ORDER_PAGE_SIZE, offset=offset)
    )
    title = "📋 Активные заказы" if active_only else "📚 Все заказы"
    map_url = None
    heading = f"{title}: {total}\nСтраница {page + 1} из {total_pages}"
    if active_only:
        _map_text, fallback_map_url = all_locations_card(repo.list_open())
        map_url = _monitor_url(settings) or fallback_map_url
        if map_url:
            heading += "\n🗺 Все локации — по кнопке ниже"
    entries: list[str] = []
    for order in orders:
        model = escape(" ".join(order.product.split())[:80] or "—")
        price = escape(amount_text(order)).replace("\n", " · ")
        phones = [display_phone(order.client_phone)]
        if order.client_phone_2 and order.client_phone_2 != order.client_phone:
            phones.append(display_phone(order.client_phone_2))
        phone_text = " / ".join(phones)
        status = STATUS_LABELS.get(order.status, order.status)
        seller = escape((order.seller_name or "—")[:40])
        entries.append(
            f"<b>№{order.order_number}</b> · {model}\n"
            f"{price} · 📱 {phone_text}\n"
            f"{escape(status)} · 👤 {seller}"
        )
    heading += "\n\n" + "\n\n".join(entries)
    page_keyboard = orders_page_keyboard(
        "active" if active_only else "all",
        page,
        total_pages,
        map_url,
        orders,
    )
    if query:
        try:
            await query.edit_message_text(
                heading,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=page_keyboard,
            )
        except Exception as error:
            if not _message_is_not_modified(error):
                raise
    else:
        await update.message.reply_text(
            heading,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=page_keyboard,
        )


async def active_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_orders(update, context, active_only=True)


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_orders(update, context, active_only=False)


async def orders_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, kind, raw_page = query.data.split(":")
    await _show_orders(update, context, active_only=kind == "active", page=int(raw_page))


async def open_order_from_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open an active order as the new canonical manager card for editing."""
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if not _allowed(query.from_user.id, settings.manager_ids):
        await query.answer("Нет доступа", show_alert=True)
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(query.data.split(":", 1)[1]))
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if order.status not in MANAGER_EDITABLE_STATUSES:
        await query.answer(f"Заказ №{order.order_number}")
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=orders_channel_card(order),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=readonly_order_keyboard(order),
            )
        except Exception:
            logger.exception("Could not open archived order %s from order list", order.id)
            await _notify_manager(
                context,
                query.from_user.id,
                f"⚠️ Не удалось открыть заказ №{order.order_number}. Попробуйте ещё раз.",
            )
        return
    await query.answer(f"Открываю заказ №{order.order_number}…")
    try:
        sent = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=manager_card(order, sent=order.status != "draft"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=(
                review_keyboard(order.id, expanded=True)
                if order.status == "draft"
                else manager_sent_keyboard(order, expanded=True)
            ),
        )
    except Exception:
        logger.exception("Could not open order %s from active list", order.id)
        await _notify_manager(
            context,
            query.from_user.id,
            f"⚠️ Не удалось открыть заказ №{order.order_number}. Попробуйте ещё раз.",
        )
        return
    sent_chat_id = getattr(sent, "chat_id", None)
    sent_message_id = getattr(sent, "message_id", None)
    if not isinstance(sent_chat_id, int) or not isinstance(sent_message_id, int):
        logger.error("Telegram returned an invalid list-order card reference for order %s", order.id)
        return
    old_reference = (order.manager_chat_id, order.manager_message_id)
    updated = repo.update(
        order.id,
        expected_updated_at=order.updated_at,
        actor_id=query.from_user.id,
        actor_name=_name(query.from_user),
        actor_role="manager",
        manager_chat_id=sent_chat_id,
        manager_message_id=sent_message_id,
    )
    if not updated:
        repo.enqueue_cleanup_messages(order.id, [(sent_chat_id, sent_message_id)])
        await _process_cleanup_messages(context, order_id=order.id)
        await _notify_manager(
            context,
            query.from_user.id,
            "⚠️ Заказ уже изменился. Откройте обновлённый список.",
        )
        return
    old_chat_id, old_message_id = old_reference
    if old_chat_id and old_message_id and (old_chat_id, old_message_id) != (
        sent_chat_id,
        sent_message_id,
    ):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=old_chat_id,
                message_id=old_message_id,
                reply_markup=None,
            )
        except Exception:
            logger.info("Could not deactivate previous manager card for order %s", order.id)
    _, success = await _sync_order(context, updated.id)
    if not success:
        _schedule_sync_retry(context, updated.id)
        await _notify_manager(
            context,
            query.from_user.id,
            "⚠️ Карточка открыта, но часть сообщений обновится автоматически позже.",
        )


async def new_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    context.user_data.pop("edit", None)
    previous_draft = context.user_data.get("draft") or {}
    repo: OrderRepository = context.application.bot_data["repo"]
    committed = repo.get_by_creation_token(previous_draft.get("creation_token"))
    if committed and committed.status == "draft":
        # The final creation step commits SQLite before sending the manager
        # card. If that Telegram request times out, ConversationHandler remains
        # active and its persistent reply keyboard can still contain
        # ``➕ Новый заказ``. Never replace the creation token silently:
        # first surface the already-saved order, then let the manager start a
        # genuinely new one with a second explicit press.
        _, recovered = await _sync_order(context, committed.id)
        if not recovered:
            _schedule_sync_retry(context, committed.id)
        context.user_data.pop("draft", None)
        recovery_text = (
            "Его карточка восстановлена."
            if recovered
            else "Его карточка будет восстановлена автоматически."
        )
        await update.message.reply_text(
            f"Заказ №{committed.order_number} уже сохранён. "
            f"{recovery_text} "
            "Нажмите «➕ Новый заказ» ещё раз, чтобы создать следующий.",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END
    context.user_data["draft"] = {"creation_token": uuid4().hex}
    await update.message.reply_text("1/7. Выберите, кому принадлежит заказ:", reply_markup=seller_keyboard())
    return SELLER


async def seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        value = normalize_seller(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error), reply_markup=seller_keyboard())
        return SELLER
    draft["seller_name"] = value
    await update.message.reply_text("2/7. Введите модель товара:", reply_markup=ReplyKeyboardRemove())
    return PRODUCT


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        value = _text(update.message, maximum=200)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return PRODUCT
    draft["product"] = value
    await update.message.reply_text(
        "3/7. 📸 Фото товара?\n\n"
        "Отправьте фотографию или нажмите «Пропустить».",
        reply_markup=product_photo_keyboard(),
    )
    return PRODUCT_PHOTO


async def product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    photos = tuple(update.message.photo or ())
    text = (update.message.text or "").strip().casefold()
    if photos:
        photo = max(
            photos,
            key=lambda item: (
                int(getattr(item, "file_size", 0) or 0),
                int(getattr(item, "width", 0) or 0) * int(getattr(item, "height", 0) or 0),
            ),
        )
        draft["product_photo_file_id"] = str(photo.file_id)
        draft["product_photo_unique_id"] = str(
            getattr(photo, "file_unique_id", "") or photo.file_id
        )
    elif text in {"пропустить", "⏭ пропустить"}:
        draft["product_photo_file_id"] = None
        draft["product_photo_unique_id"] = None
    else:
        await update.message.reply_text(
            "Отправьте фотографию товара или нажмите «Пропустить».",
            reply_markup=product_photo_keyboard(),
        )
        return PRODUCT_PHOTO
    await update.message.reply_text(
        "4/7. Отправьте:\n"
        "📍 Локацию\n"
        "📱 Номер\n"
        "💰 Общую сумму",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DETAILS


def _missing_details(draft: dict) -> list[str]:
    missing = []
    if not draft.get("client_phone"):
        missing.append("номер клиента")
    if draft.get("amount_usd") is None and draft.get("amount_uzs") is None:
        missing.append("цена")
    if (
        (draft.get("latitude") is None or draft.get("longitude") is None)
        and not draft.get("address_text")
    ):
        missing.append("локация")
    return missing


def _merge_phones(draft: dict, phones: list[str]) -> int:
    combined: list[str] = []
    for phone in (draft.get("client_phone"), draft.get("client_phone_2"), *phones):
        if phone and phone not in combined:
            combined.append(phone)
    draft["client_phone"] = combined[0] if combined else None
    draft["client_phone_2"] = combined[1] if len(combined) > 1 else None
    return min(len(combined), 2)


def _same_location(draft: dict, values: dict, prefix: str = "") -> bool:
    latitude = draft.get(f"{prefix}latitude")
    longitude = draft.get(f"{prefix}longitude")
    return (
        latitude is not None
        and longitude is not None
        and abs(latitude - values["latitude"]) < 0.000001
        and abs(longitude - values["longitude"]) < 0.000001
    )


def _merge_location(draft: dict, values: dict) -> int:
    primary_occupied = bool(draft.get("address_text")) or (
        draft.get("latitude") is not None and draft.get("longitude") is not None
    )
    if not primary_occupied:
        draft.update(values)
        return 1
    if draft.get("latitude") is not None and _same_location(draft, values):
        return 1
    second_occupied = bool(draft.get("second_address_text")) or (
        draft.get("second_latitude") is not None
        and draft.get("second_longitude") is not None
    )
    if not second_occupied:
        draft.update(_as_second_location(values))
        return 2
    if draft.get("second_latitude") is not None and _same_location(draft, values, "second_"):
        return 2
    raise ValueError("У заказа уже сохранены две локации")


async def _capture_order_details(message, draft: dict) -> list[str]:
    recognized: list[str] = []
    if getattr(message, "location", None) or getattr(message, "venue", None):
        location_number = _merge_location(draft, await _location_values(message))
        recognized.append(f"локация {location_number}")
        return recognized

    parsed = parse_order_details(
        getattr(message, "text", None) or getattr(message, "caption", None) or ""
    )
    phones = list(parsed.get("client_phones") or [])
    if phones:
        count = _merge_phones(draft, phones)
        recognized.append("два номера" if count > 1 else "номер")
    if "amount_usd" in parsed or "amount_uzs" in parsed:
        draft["amount_usd"] = parsed.get("amount_usd")
        draft["amount_uzs"] = parsed.get("amount_uzs")
        recognized.append("цена")
    for raw_url in _message_location_urls(message):
        values = _validated_location(await enrich_location(None, None, str(raw_url)))
        location_number = _merge_location(draft, values)
        label = f"локация {location_number}"
        if label not in recognized:
            recognized.append(label)
    return recognized


async def details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END

    incoming_text = (update.message.text or "").strip()
    if draft.get("awaiting_text_location"):
        has_location_input = bool(
            getattr(update.message, "location", None)
            or getattr(update.message, "venue", None)
            or _message_location_urls(update.message)
        )
        if has_location_input:
            draft.pop("awaiting_text_location", None)
            try:
                recognized = await _capture_order_details(update.message, draft)
            except ValueError as error:
                await update.message.reply_text(f"Не удалось сохранить данные: {error}")
                return DETAILS
        else:
            if incoming_text == TEXT_LOCATION_BUTTON:
                await update.message.reply_text(
                    "Напишите адрес клиента текстом. Например: Яшнабадский район, махалля Алимкент, улица Кустанай, дом 15.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return DETAILS
            try:
                address = _text(update.message, maximum=1000)
            except ValueError as error:
                await update.message.reply_text(str(error), reply_markup=ReplyKeyboardRemove())
                return DETAILS
            draft.update(
                **extract_text_address(address),
                location_url=None,
                latitude=None,
                longitude=None,
            )
            draft.pop("awaiting_text_location", None)
            recognized = ["локация текстом"]
    elif incoming_text == TEXT_LOCATION_BUTTON:
        if _missing_details(draft) != ["локация"]:
            await update.message.reply_text(
                "Сначала отправьте номер клиента и цену. После этого можно будет ввести локацию текстом."
            )
            return DETAILS
        draft["awaiting_text_location"] = True
        await update.message.reply_text(
            "Напишите адрес клиента текстом. Например: Яшнабадский район, махалля Алимкент, улица Кустанай, дом 15.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return DETAILS
    else:
        try:
            recognized = await _capture_order_details(update.message, draft)
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
        reply_markup = text_location_keyboard() if missing == ["локация"] else ReplyKeyboardRemove()
        await update.message.reply_text(
            f"✅ Сохранено: {', '.join(recognized)}. Осталось отправить: {', '.join(missing)}.",
            reply_markup=reply_markup,
        )
        return DETAILS

    await update.message.reply_text("5/7. Выберите вариант оплаты:", reply_markup=payment_keyboard())
    return PAYMENT


def _as_second_location(values: dict) -> dict:
    return {f"second_{key}": value for key, value in values.items()}


async def second_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compatibility path for conversations started by the previous release."""
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    text = (update.message.text or "").strip().casefold()
    if text == "продолжить без второй локации":
        await update.message.reply_text(
            "5/7. Выберите вариант оплаты:",
            reply_markup=payment_keyboard(),
        )
        return PAYMENT
    try:
        recognized = await _capture_order_details(update.message, draft)
    except ValueError as error:
        await update.message.reply_text(
            f"Не удалось сохранить вторую локацию: {error}",
            reply_markup=payment_keyboard(),
        )
        return SECOND_LOCATION
    await update.message.reply_text(
        f"✅ Сохранено: {', '.join(recognized)}.\n\n5/7. Выберите вариант оплаты:",
        reply_markup=payment_keyboard(),
    )
    return PAYMENT


async def payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        value = normalize_payment(update.message.text or "")
    except ValueError as error:
        # A second phone/location can arrive immediately after the first
        # location, even if Telegram has already displayed the payment step.
        try:
            recognized = await _capture_order_details(update.message, draft)
        except ValueError as details_error:
            await update.message.reply_text(str(details_error), reply_markup=payment_keyboard())
            return PAYMENT
        if recognized:
            await update.message.reply_text(
                f"✅ Сохранено: {', '.join(recognized)}. Теперь выберите вариант оплаты:",
                reply_markup=payment_keyboard(),
            )
            return PAYMENT
        await update.message.reply_text(str(error), reply_markup=payment_keyboard())
        return PAYMENT
    draft["payment_status"] = value
    await update.message.reply_text(
        "6/7. Выберите время доставки или напишите свой вариант текстом:",
        reply_markup=delivery_time_keyboard(),
    )
    return DELIVERY_TIME


async def delivery_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        value = _text(update.message, maximum=100, required=False)
    except ValueError as error:
        await update.message.reply_text(str(error), reply_markup=delivery_time_keyboard())
        return DELIVERY_TIME
    draft["delivery_time"] = value
    await update.message.reply_text("7/7. Добавьте комментарий или пропустите:", reply_markup=skip_keyboard())
    return COMMENT


async def comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text(
            "Черновик заказа не найден. Начните новый заказ заново.",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END
    try:
        value = _text(update.message, maximum=1000, required=False)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return COMMENT
    draft["comment"] = value
    # Drafts restored from the release that predates creation_token still get
    # the same duplicate protection on their first final-step attempt.
    draft.setdefault("creation_token", uuid4().hex)
    repo: OrderRepository = context.application.bot_data["repo"]
    # The creation token is stored both in persistent user_data and SQLite.
    # Retrying this step after a Telegram timeout therefore returns the same
    # order instead of consuming a second order number.
    order = repo.create(
        manager_id=update.effective_user.id,
        manager_name=_name(update.effective_user),
        data=draft,
    )
    async with _order_sync_lock(context.application, order.id):
        order = repo.get(order.id)
        if not order.manager_message_id:
            try:
                card_message = await update.message.reply_text(
                    manager_card(order),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=review_keyboard(order.id),
                )
            except Exception:
                latest = repo.get(order.id)
                if latest and not latest.manager_message_id:
                    repo.mark_needs_sync(
                        latest.id,
                        expected_updated_at=latest.updated_at,
                    )
                raise
            if getattr(card_message, "message_id", None):
                order = repo.update(
                    order.id,
                    expected_updated_at=order.updated_at,
                    manager_chat_id=card_message.chat_id,
                    manager_message_id=card_message.message_id,
                )
                if order and not _orders_channel_id(context):
                    repo.mark_synced(order.id, expected_updated_at=order.updated_at)
    if order and _orders_channel_id(context):
        order, synchronized = await _sync_order(context, order.id)
        if not synchronized:
            _schedule_sync_retry(context, order.id)
    sales_queued = False
    if order and order.product_photo_file_id:
        try:
            order, sales_queued = await _queue_product_photo_sales_card(
                context,
                order,
                actor_id=update.effective_user.id,
                actor_name=_name(update.effective_user),
            )
        except Exception:
            logger.exception(
                "Could not automatically queue sales card for order %s",
                order.id,
            )
        else:
            if sales_queued:
                _, synchronized = await _sync_order(context, order.id)
                if not synchronized:
                    _schedule_sync_retry(context, order.id)
    confirmation = "Проверьте данные заказа."
    if sales_queued:
        confirmation += "\n📸 Фото нового товара отправляется в канал продаж."
    await update.message.reply_text(confirmation, reply_markup=main_keyboard())
    context.user_data.pop("draft", None)
    return ConversationHandler.END


async def begin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, raw_id, field = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    settings: Settings = context.application.bot_data["settings"]
    if not order or not _allowed(query.from_user.id, settings.manager_ids) or order.status not in MANAGER_EDITABLE_STATUSES:
        await query.answer("Этот заказ уже нельзя изменять", show_alert=True)
        return ConversationHandler.END
    clicked_message_id = getattr(query.message, "message_id", None)
    if clicked_message_id and not order.manager_message_id:
        adopted = repo.update(
            order.id,
            expected_updated_at=order.updated_at,
            manager_chat_id=query.message.chat_id,
            manager_message_id=clicked_message_id,
        )
        order = adopted or repo.get(order.id)
    if (
        clicked_message_id
        and order.manager_message_id
        and (
            order.manager_chat_id != query.message.chat_id
            or order.manager_message_id != clicked_message_id
        )
    ):
        await query.answer("Эта карточка устарела. Откройте актуальную карточку заказа.", show_alert=True)
        return ConversationHandler.END
    if context.user_data.get("draft") is not None:
        await query.answer("Сначала завершите новый заказ или нажмите /cancel", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["edit"] = {
        "order_id": order.id,
        "field": field,
        "message_id": query.message.message_id,
        "chat_id": query.message.chat_id,
        "updated_at": order.updated_at,
    }
    prompts = {
        "seller": "Выберите нового владельца заказа:",
        "payment_status": "Выберите новый вариант оплаты:",
        "product": "Введите новую модель:",
        "product_photo": "Отправьте новое фото товара или удалите текущее:",
        "phone": "Введите один или два новых номера:",
        "location": "Отправьте новую основную локацию или ссылку:",
        "second_location": "Отправьте дополнительную локацию или ссылку:",
        "amount": "Введите новую сумму. Например: 120$ 1 536 000",
        "delivery_time": "Введите новое время (или Пропустить):",
        "comment": "Введите новый комментарий (или Пропустить):",
    }
    prompt = prompts[field]
    if order.status == "on_way":
        courier_name = order.courier_name or order.assigned_courier_name or "Курьер"
        prompt = (
            f"⚠️ Курьер {courier_name} уже едет к заказу №{order.order_number}.\n"
            "Убедитесь, что он знает об изменении.\n\n"
            f"{prompt}"
        )
    await query.message.reply_text(prompt, reply_markup=edit_input_keyboard(field))
    return EDIT_VALUE


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
    clicked_message_id = getattr(query.message, "message_id", None)
    if (
        clicked_message_id
        and order.manager_message_id
        and (
            order.manager_chat_id != query.message.chat_id
            or order.manager_message_id != clicked_message_id
        )
    ):
        await query.answer("Эта карточка устарела. Откройте актуальную карточку заказа.", show_alert=True)
        return
    expanded = action == "edit_menu"
    if not expanded:
        context.user_data.pop("edit", None)
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


async def _persist_product_photo(
    context: ContextTypes.DEFAULT_TYPE,
    order,
) -> str | None:
    if not order.product_photo_file_id:
        return None
    settings: Settings = context.application.bot_data["settings"]
    photo_key = hashlib.sha256(
        (order.product_photo_unique_id or order.product_photo_file_id).encode()
    ).hexdigest()[:20]
    relative = Path("product_photos") / f"order-{order.id}-{photo_key}.jpg"
    destination = settings.database_path.parent / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        telegram_file = await context.bot.get_file(order.product_photo_file_id)
        await telegram_file.download_to_drive(custom_path=destination)
    return relative.as_posix()


async def _queue_product_photo_sales_card(
    context: ContextTypes.DEFAULT_TYPE,
    order,
    *,
    actor_id: int,
    actor_name: str,
    actor_role: str = "manager",
) -> tuple[object, bool]:
    """Persist a product photo and idempotently create its sales-card request."""
    repo: OrderRepository = context.application.bot_data["repo"]
    async with _order_sync_lock(context.application, order.id):
        latest = repo.get(order.id)
        if latest is None:
            raise RuntimeError("sales_card_order_missing")
        if (
            not latest.product_photo_file_id
            or latest.status == "cancelled"
            or latest.sales_card_status not in {"none", "failed"}
        ):
            return latest, False
        photo_path = await _persist_product_photo(context, latest)
        queued = repo.request_sales_card(
            latest.id,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            product_photo_path=photo_path,
        )
        if queued is None:
            raise RuntimeError("sales_card_order_missing")
        return queued, queued.sales_card_status == "pending"


def _sales_card_preview(order) -> str:
    phones = [display_phone(order.client_phone)]
    if order.client_phone_2 and order.client_phone_2 != order.client_phone:
        phones.append(display_phone(order.client_phone_2))
    photo = "добавлено" if order.product_photo_file_id else "не добавлено"
    return (
        "🛒 <b>Проданный товар</b>\n\n"
        f"🚚 Заказ №{order.order_number}\n"
        f"📦 {escape(order.product)}\n"
        f"👤 Продавец: {escape(order.seller_name or '—')}\n"
        f"📱 {' / '.join(escape(phone) for phone in phones)}\n"
        f"📸 Фото: {photo}\n\n"
        "Сумма доставки не будет автоматически распределена между "
        "наличными, картой и Paynet."
    )


async def sales_card_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, raw_id = query.data.split(":", 1)
    settings: Settings = context.application.bot_data["settings"]
    repo: OrderRepository = context.application.bot_data["repo"]
    if not _allowed(query.from_user.id, settings.manager_ids):
        await query.answer("Нет доступа", show_alert=True)
        return
    order = repo.get(int(raw_id))
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if action == "sales_cancel":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("Создание карточки отменено.")
        return
    if order.sales_card_status == "complete":
        await query.answer("Товар уже добавлен", show_alert=True)
        markup = sales_card_result_keyboard(order, settings.sales_photo_chat_id)
        await query.message.reply_text(
            f"✅ Товар заказа №{order.order_number} уже добавлен в канал продаж.",
            reply_markup=markup,
        )
        return
    if action == "sales_card":
        await query.answer()
        await query.message.reply_text(
            _sales_card_preview(order),
            parse_mode=ParseMode.HTML,
            reply_markup=sales_card_confirmation_keyboard(order),
        )
        return
    if order.sales_card_status in {"pending", "processing"}:
        await query.answer("Карточка уже создаётся", show_alert=True)
        return

    try:
        photo_path = await _persist_product_photo(context, order)
    except Exception:
        logger.exception("Could not persist product photo for order %s", order.id)
        await query.answer(
            "Не удалось подготовить фото. Попробуйте ещё раз.",
            show_alert=True,
        )
        return

    queued = repo.request_sales_card(
        order.id,
        actor_id=query.from_user.id,
        actor_name=_name(query.from_user),
        product_photo_path=photo_path,
    )
    if not queued:
        await query.answer("Заказ уже изменён. Попробуйте ещё раз.", show_alert=True)
        return
    await query.answer("Карточка поставлена в очередь")
    _, synchronized = await _sync_order(context, order.id)
    if not synchronized:
        _schedule_sync_retry(context, order.id)
    try:
        await query.edit_message_text(
            f"🛒 Карточка продажи для заказа №{order.order_number} создаётся.\n"
            "Повторное нажатие не создаст дубль."
        )
    except Exception as error:
        if not _message_is_not_modified(error):
            logger.warning("Could not refresh sales-card preview for order %s", order.id)


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    context.user_data.pop("edit", None)
    await update.message.reply_text("Изменение отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    edit = context.user_data.get("edit")
    if not edit or update.effective_chat.id != edit["chat_id"]:
        return ConversationHandler.END
    incoming_text = (update.message.text or "").strip()
    if incoming_text == EDIT_CANCEL_TEXT:
        return await cancel_edit(update, context)
    if incoming_text in MAIN_MENU_TEXTS:
        context.user_data.pop("edit", None)
        return ConversationHandler.END
    field, values = edit["field"], {}
    try:
        if field == "seller": values["seller_name"] = normalize_seller(update.message.text or "")
        elif field == "product_photo":
            if update.message.photo:
                photo = max(
                    update.message.photo,
                    key=lambda item: (
                        int(getattr(item, "file_size", 0) or 0),
                        int(getattr(item, "width", 0) or 0) * int(getattr(item, "height", 0) or 0),
                    ),
                )
                values.update(
                    product_photo_file_id=str(photo.file_id),
                    product_photo_unique_id=str(
                        getattr(photo, "file_unique_id", "") or photo.file_id
                    ),
                    product_photo_path=None,
                )
            elif incoming_text == "🗑 Удалить фото":
                values.update(
                    product_photo_file_id=None,
                    product_photo_unique_id=None,
                    product_photo_path=None,
                )
            else:
                raise ValueError("Отправьте фотографию или нажмите «Удалить фото»")
        elif field == "payment_status": values["payment_status"] = normalize_payment(update.message.text or "")
        elif field == "phone":
            phones = list(parse_order_details(update.message.text or "").get("client_phones") or [])
            if not phones:
                raise ValueError("Введите один или два узбекских номера")
            values["client_phone"] = phones[0]
            values["client_phone_2"] = phones[1] if len(phones) > 1 else None
        elif field == "amount": values["amount_usd"], values["amount_uzs"] = parse_amount(update.message.text or "")
        elif field in {"location", "second_location"}:
            prefix = "second_" if field == "second_location" else ""
            has_location_input = bool(
                getattr(update.message, "location", None)
                or getattr(update.message, "venue", None)
                or _message_location_urls(update.message)
            )
            if incoming_text == DELETE_SECOND_LOCATION_TEXT:
                if field != "second_location":
                    raise ValueError("Основную локацию нельзя удалить")
                values = {
                    "second_location_url": None,
                    "second_latitude": None,
                    "second_longitude": None,
                    "second_address_text": None,
                    "second_district": None,
                    "second_mahalla": None,
                }
                edit.pop("awaiting_text_location", None)
            elif incoming_text == TEXT_LOCATION_BUTTON and not has_location_input:
                edit["awaiting_text_location"] = True
                await update.message.reply_text(
                    "Напишите адрес текстом.",
                    reply_markup=edit_input_keyboard(field),
                )
                return EDIT_VALUE
            elif edit.get("awaiting_text_location") and not has_location_input:
                address = _text(update.message, maximum=1000)
                values = {
                    f"{prefix}location_url": None,
                    f"{prefix}latitude": None,
                    f"{prefix}longitude": None,
                }
                values.update({
                    f"{prefix}{key}": value
                    for key, value in extract_text_address(address).items()
                })
                edit.pop("awaiting_text_location", None)
            else:
                edit.pop("awaiting_text_location", None)
                values.update(await _location_values(update.message))
                if field == "second_location":
                    values = _as_second_location(values)
        else:
            limits = {"product": 200, "delivery_time": 100, "comment": 1000}
            values[field] = _text(update.message, maximum=limits[field], required=field == "product")
    except ValueError as error:
        await update.message.reply_text(str(error), reply_markup=edit_input_keyboard(field))
        return EDIT_VALUE
    repo: OrderRepository = context.application.bot_data["repo"]
    previous = repo.get(edit["order_id"])
    if not previous or previous.status not in MANAGER_EDITABLE_STATUSES:
        context.user_data.pop("edit", None)
        await update.message.reply_text("Этот заказ уже нельзя изменить.", reply_markup=main_keyboard())
        return ConversationHandler.END

    if all(getattr(previous, key) == value for key, value in values.items()):
        context.user_data.pop("edit", None)
        await update.message.reply_text("ℹ️ Данные не изменились.", reply_markup=main_keyboard())
        return ConversationHandler.END

    sent = previous.status != "draft"
    location_number = 2 if field == "second_location" else 1
    publication_fields: dict = {}
    actor = getattr(update, "effective_user", None)
    cleanup_messages: list[tuple[int, int]] = []
    if field in {"location", "second_location"}:
        prefix = "second_" if location_number == 2 else ""
        old_chat_id = (
            previous.second_location_chat_id
            if location_number == 2
            else previous.location_chat_id
        )
        old_details_id = (
            previous.second_location_details_message_id
            if location_number == 2
            else previous.location_details_message_id
        )
        old_footer_id = (
            previous.second_location_footer_message_id
            if location_number == 2
            else previous.location_footer_message_id
        )
        old_pin_id = (
            previous.second_location_message_id
            if location_number == 2
            else previous.location_message_id
        )
        if old_chat_id:
            cleanup_messages = [
                (old_chat_id, message_id)
                for message_id in (old_details_id, old_pin_id, old_footer_id)
                if message_id
            ]
        has_coordinates = (
            values.get(f"{prefix}latitude") is not None
            and values.get(f"{prefix}longitude") is not None
        )
        if sent and has_coordinates:
            candidate = replace(previous, **values)
            try:
                publication_fields = await _send_location_messages(
                    context,
                    candidate,
                    location_number,
                )
            except Exception:
                logger.exception("Could not publish replacement location for order %s", previous.id)
                await update.message.reply_text(
                    "⚠️ Новая локация не сохранена: Telegram-канал недоступен. "
                    "Старая точка осталась рабочей. Попробуйте ещё раз.",
                    reply_markup=edit_input_keyboard(field),
                )
                return EDIT_VALUE
            values.update(publication_fields)
        else:
            values.update({
                f"{prefix}location_chat_id": None,
                f"{prefix}location_message_id": None,
                f"{prefix}location_details_message_id": None,
                f"{prefix}location_footer_message_id": None,
            })
    order = repo.transition(
        edit["order_id"],
        MANAGER_EDITABLE_STATUSES,
        expected_updated_at=edit.get("updated_at"),
        actor_id=actor.id if actor else None,
        actor_name=_name(actor) if actor else None,
        actor_role="manager" if actor else None,
        cleanup_messages=cleanup_messages,
        **values,
    )
    if not order:
        if publication_fields:
            new_publication = replace(previous, **values)
            new_chat_id = (
                new_publication.second_location_chat_id
                if location_number == 2
                else new_publication.location_chat_id
            )
            new_details_id = (
                new_publication.second_location_details_message_id
                if location_number == 2
                else new_publication.location_details_message_id
            )
            new_footer_id = (
                new_publication.second_location_footer_message_id
                if location_number == 2
                else new_publication.location_footer_message_id
            )
            new_pin_id = (
                new_publication.second_location_message_id
                if location_number == 2
                else new_publication.location_message_id
            )
            repo.enqueue_cleanup_messages(
                previous.id,
                [
                    (new_chat_id, message_id)
                    for message_id in (new_details_id, new_pin_id, new_footer_id)
                    if new_chat_id and message_id
                ],
            )
            await _process_cleanup_messages(context, order_id=previous.id)
        context.user_data.pop("edit", None)
        await update.message.reply_text(
            "Заказ уже изменил другой менеджер или его статус изменился. Откройте свежую карточку.",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END
    if cleanup_messages:
        await _process_cleanup_messages(context, order_id=order.id)

    sales_queued = False
    sales_queue_failed = False
    if field == "product_photo" and order.product_photo_file_id:
        try:
            order, sales_queued = await _queue_product_photo_sales_card(
                context,
                order,
                actor_id=actor.id if actor else previous.manager_id,
                actor_name=_name(actor) if actor else previous.manager_name,
            )
        except Exception:
            sales_queue_failed = True
            logger.exception(
                "Could not automatically queue edited product photo for order %s",
                order.id,
            )

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
    if sent or _orders_channel_id(context):
        order, refreshed = await _sync_order(context, order.id)
        order = order or repo.get(edit["order_id"])
        if not refreshed:
            _schedule_sync_retry(context, order.id)
    else:
        refreshed = manager_refreshed
        refreshed_order = repo.get(order.id)
        if manager_refreshed and refreshed_order.sync_needed:
            repo.mark_synced(refreshed_order.id, expected_updated_at=refreshed_order.updated_at)
        elif not manager_refreshed:
            _schedule_sync_retry(context, order.id)
    if (
        sent
        and _courier_may_have_seen_order(previous)
        and field in _EDIT_FIELD_LABELS
    ):
        await _notify_active_order_edit(
            context,
            order,
            field,
            _name(actor) if actor else previous.manager_name,
        )
    context.user_data.pop("edit", None)
    if not manager_refreshed:
        result = "⚠️ Данные сохранены в базе, но карточку менеджера обновить не удалось."
    elif not refreshed:
        result = "⚠️ Данные сохранены, но карточку в группе обновить не удалось."
    elif sales_queue_failed:
        result = (
            "⚠️ Фото товара сохранено, но пока не отправлено в «Проданные». "
            "Фоновая проверка повторит отправку автоматически."
        )
    elif sales_queued:
        result = "✅ Фото товара сохранено и отправляется в канал продаж."
    elif field == "amount":
        result = f"✅ Новая цена сохранена:\n{money(order.amount_usd, order.amount_uzs)}"
    else:
        result = "✅ Данные обновлены у менеджера и курьера."
    await update.message.reply_text(result, reply_markup=main_keyboard())
    return ConversationHandler.END


async def manager_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, raw_id = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    settings: Settings = context.application.bot_data["settings"]
    order = repo.get(int(raw_id))
    if not order or not _allowed(query.from_user.id, settings.manager_ids):
        await query.answer("Заказ недоступен", show_alert=True); return
    clicked_message_id = getattr(query.message, "message_id", None)
    if (
        order.manager_message_id
        and clicked_message_id
        and (
            order.manager_chat_id != query.message.chat_id
            or order.manager_message_id != clicked_message_id
        )
    ):
        await query.answer("Эта карточка устарела. Откройте актуальную карточку заказа.", show_alert=True)
        return
    if action == "manager_restore":
        if order.delivery_message_id is not None or order.courier_id is not None:
            await query.answer("Отменённый курьером заказ возвращается из группы доставки", show_alert=True); return
        restored = repo.transition(
            order.id,
            {"cancelled"},
            status="draft",
            courier_id=None,
            courier_name=None,
            courier_read_at=None,
            time_started=None,
            estimated_delivery_at=None,
            actor_id=query.from_user.id,
            actor_name=_name(query.from_user),
            actor_role="manager",
        )
        if not restored:
            await query.answer("Заказ уже нельзя вернуть", show_alert=True); return
        await query.edit_message_text(
            manager_card(restored),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=review_keyboard(restored.id),
        )
        await query.answer("Заказ возвращён")
        if getattr(settings, "orders_channel_id", None):
            latest, success = await _sync_order(context, restored.id)
            if not success:
                _schedule_sync_retry(context, restored.id)
        else:
            latest = repo.get(restored.id)
            repo.mark_synced(latest.id, expected_updated_at=latest.updated_at)
        return
    if order.status != "draft":
        await query.answer("Заказ уже обработан или недоступен", show_alert=True); return
    if action == "manager_cancel":
        cancelled = repo.transition(
            order.id,
            {"draft"},
            status="cancelled",
            actor_id=query.from_user.id,
            actor_name=_name(query.from_user),
            actor_role="manager",
        )
        if not cancelled:
            await query.answer("Заказ уже обработан", show_alert=True); return
        await query.edit_message_text(
            f"❌ Заказ №{order.order_number} отменён менеджером",
            reply_markup=manager_cancelled_keyboard(order.id),
        )
        await query.answer()
        if getattr(settings, "orders_channel_id", None):
            latest, success = await _sync_order(context, cancelled.id)
            if not success:
                _schedule_sync_retry(context, cancelled.id)
        else:
            repo.mark_synced(cancelled.id, expected_updated_at=cancelled.updated_at)
        return
    await query.edit_message_reply_markup(
        reply_markup=courier_selection_keyboard(
            order,
            allowed_courier_ids=_allowed_courier_ids(settings),
        )
    )
    await query.answer("Выберите курьера")


def _courier_assignment_source_is_current(
    order,
    query,
    settings: Settings,
    *,
    orders_channel_source: bool,
) -> bool:
    if orders_channel_source:
        return bool(
            order.orders_channel_chat_id
            and order.orders_channel_message_id
            and query.message.chat_id == order.orders_channel_chat_id
            and query.message.message_id == order.orders_channel_message_id
            and query.message.chat_id == getattr(settings, "orders_channel_id", None)
        )
    return bool(
        not order.manager_message_id
        or (
            query.message.chat_id == order.manager_chat_id
            and query.message.message_id == order.manager_message_id
        )
    )


async def courier_assignment_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if not _allowed(query.from_user.id, settings.manager_ids):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = query.data.split(":")
    raw_action, order_id = parts[0], int(parts[1])
    orders_channel_source = raw_action.startswith("control_")
    action = raw_action.removeprefix("control_")
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(order_id)
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    source_is_current = _courier_assignment_source_is_current(
        order,
        query,
        settings,
        orders_channel_source=orders_channel_source,
    )
    if not source_is_current:
        await query.answer("Эта карточка устарела. Откройте актуальную карточку заказа.", show_alert=True)
        return
    if order.status not in {"draft", "pending", "picked_up", "on_way"}:
        await query.answer("Для закрытого заказа курьера изменить нельзя", show_alert=True)
        return
    if action == "courier_menu":
        source = "orders_channel" if orders_channel_source else "manager"
        await query.edit_message_reply_markup(
            reply_markup=courier_selection_keyboard(
                order,
                source=source,
                allowed_courier_ids=_allowed_courier_ids(settings),
            )
        )
        await query.answer("Выберите курьера")
        return
    if action == "courier_close":
        if orders_channel_source:
            keyboard = orders_channel_keyboard(order)
        else:
            keyboard = review_keyboard(order.id) if order.status == "draft" else manager_sent_keyboard(order)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer()
        return

    selected = courier_option(int(parts[2])) if len(parts) == 3 else None
    if not selected or selected.user_id not in _allowed_courier_ids(settings):
        await query.answer("Курьер не найден", show_alert=True)
        return
    if order.status != "draft" and order.assigned_courier_id == selected.user_id:
        keyboard = orders_channel_keyboard(order) if orders_channel_source else manager_sent_keyboard(order)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer(f"Курьер {selected.name} уже выбран")
        return

    force_assignment = action == "courier_force_assign"
    if _courier_may_have_seen_order(order) and not force_assignment:
        source = "orders_channel" if orders_channel_source else "manager"
        await query.edit_message_reply_markup(
            reply_markup=courier_reassignment_confirmation_keyboard(
                order,
                selected.user_id,
                selected.name,
                source=source,
            )
        )
        if order.status == "on_way":
            consequence = "Будет отменён текущий выезд"
        elif order.status == "picked_up":
            consequence = "Будет снята отметка о получении товара"
        else:
            consequence = "Старый курьер мог уже увидеть заказ"
        await query.answer(
            f"{consequence}. Нажмите подтверждение ещё раз.",
            show_alert=True,
        )
        return

    # Serialize publication with background reconciliation for this order.
    # Telegram has no idempotency key for sendMessage, but this closes the
    # avoidable race where reconciliation and reassignment could both publish.
    async with _order_sync_lock(context.application, order.id):
        current = repo.get(order.id)
        if (
            not current
            or current.updated_at != order.updated_at
            or current.status not in {"draft", "pending", "picked_up", "on_way"}
            or not _courier_assignment_source_is_current(
                current,
                query,
                settings,
                orders_channel_source=orders_channel_source,
            )
        ):
            await query.answer(
                "Заказ уже изменился. Откройте актуальную карточку.",
                show_alert=True,
            )
            return
        order = current

        # Publish first. Only after Telegram confirms the new card do we switch
        # SQLite and enqueue the previous group's card for deletion atomically.
        candidate = replace(
            order,
            status="pending",
            assigned_courier_id=selected.user_id,
            assigned_courier_name=selected.name,
            courier_id=None,
            courier_name=None,
            courier_read_at=None,
            picked_up_at=None,
            time_started=None,
            estimated_delivery_at=None,
            delivery_photo=None,
            received_usd=None,
            received_uzs=None,
            delivered_at=None,
        )
        text, keyboard = _delivery_message(candidate)
        await query.answer(f"Отправляю заказ курьеру {selected.name}…")
        try:
            sent = await context.bot.send_message(
                selected.group_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Could not assign order %s to courier %s", order.id, selected.name)
            await _notify_manager(
                context,
                query.from_user.id,
                f"⚠️ Не удалось отправить заказ №{order.order_number} курьеру {selected.name}. "
                "Старый курьер не изменён.",
            )
            return

        cleanup_messages = []
        if order.delivery_chat_id and order.delivery_message_id:
            cleanup_messages.append((order.delivery_chat_id, order.delivery_message_id))
        updated = repo.transition(
            order.id,
            {"draft", "pending", "picked_up", "on_way"},
            expected_updated_at=order.updated_at,
            actor_id=query.from_user.id,
            actor_name=_name(query.from_user),
            actor_role="manager",
            cleanup_messages=cleanup_messages,
            status="pending",
            assigned_courier_id=selected.user_id,
            assigned_courier_name=selected.name,
            courier_id=None,
            courier_name=None,
            courier_read_at=None,
            picked_up_at=None,
            time_started=None,
            estimated_delivery_at=None,
            delivery_photo=None,
            received_usd=None,
            received_uzs=None,
            delivered_at=None,
            delivery_chat_id=sent.chat_id,
            delivery_message_id=sent.message_id,
        )
        if not updated:
            repo.enqueue_cleanup_messages(order.id, [(sent.chat_id, sent.message_id)])
            await _notify_manager(
                context,
                query.from_user.id,
                "⚠️ Заказ уже изменился. Новая карточка курьера будет удалена; "
                "откройте актуальный заказ.",
            )
            # Cleanup runs after releasing the order lock, because Telegram
            # deletion may be rate-limited and needs no business-state lock.
    if not updated:
        await _process_cleanup_messages(context, order_id=order.id)
        return

    if order.delivery_chat_id and order.delivery_chat_id != sent.chat_id:
        try:
            await context.bot.send_message(
                chat_id=order.delivery_chat_id,
                text=(
                    f"⛔ <b>Заказ №{order.order_number} переназначен</b>\n"
                    f"Не выполняйте этот заказ. Новый курьер: <b>{escape(selected.name)}</b>."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception(
                "Could not warn previous courier group about reassigned order %s",
                order.id,
            )
            await _notify_manager(
                context,
                query.from_user.id,
                f"⚠️ Заказ №{order.order_number} переназначен на {escape(selected.name)}, "
                "но старую группу предупредить не удалось. Свяжитесь со старым курьером вручную.",
            )
    await _process_cleanup_messages(context, order_id=updated.id)
    synced_order, synchronized = await _sync_order(context, updated.id)
    updated = synced_order or repo.get(updated.id) or updated
    await _notify_log(
        context,
        _courier_waiting_pickup_text(repo, selected.user_id, selected.name),
    )
    if not synchronized:
        _schedule_sync_retry(context, updated.id)
        await _notify_manager(
            context,
            query.from_user.id,
            "⚠️ Курьер изменён, но часть сообщений Telegram временно не обновилась. "
            "Бот повторит автоматически.",
        )


async def manager_sync_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, raw_id = query.data.split(":")
    settings: Settings = context.application.bot_data["settings"]
    if not _allowed(query.from_user.id, settings.manager_ids):
        await query.answer("Нет доступа", show_alert=True)
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    current = repo.get(int(raw_id))
    if not current:
        await query.answer("Заказ не найден", show_alert=True)
        return
    await query.answer("Синхронизация запущена…")
    order, success = await _sync_order(context, int(raw_id))
    if not success:
        _schedule_sync_retry(context, current.id)
        await _notify_manager(
            context,
            query.from_user.id,
            "⚠️ Telegram временно недоступен. Бот повторит синхронизацию автоматически.",
        )


async def manager_pickup_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    raw_action, raw_id = parts[:2]
    settings: Settings = context.application.bot_data["settings"]
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if raw_action in {"pickup", "undo_pickup", "pickup_log", "undo_pickup_log"}:
        await query.answer(
            "Кнопка перенесена в группу назначенного курьера.",
            show_alert=True,
        )
        edit_markup = getattr(query, "edit_message_reply_markup", None)
        if raw_action in {"pickup_log", "undo_pickup_log"} and callable(edit_markup):
            try:
                await edit_markup(reply_markup=log_order_keyboard(order))
            except Exception as error:
                if not (_message_is_not_modified(error) or _message_is_missing(error)):
                    logger.exception(
                        "Could not retire legacy Log pickup button for order %s",
                        order.id,
                    )
        return
    user = getattr(query, "from_user", None)
    if not user or getattr(user, "is_bot", False):
        await query.answer(
            "Отметка доступна только менеджеру с личного аккаунта.",
            show_alert=True,
        )
        return
    if raw_action not in {"group_pickup", "group_undo_pickup"}:
        await query.answer("Неизвестное действие", show_alert=True)
        return
    if not _group_order_source_is_current(order, query, settings):
        await query.answer(
            "Эта кнопка устарела. Используйте актуальную карточку в группе курьера.",
            show_alert=True,
        )
        return
    action = "pickup" if raw_action == "group_pickup" else "undo_pickup"
    async with _order_sync_lock(context.application, order.id):
        current = repo.get(order.id)
        if not current or not _group_order_source_is_current(current, query, settings):
            await query.answer(
                "Заказ уже изменился. Используйте актуальную карточку.",
                show_alert=True,
            )
            return
        if current.assigned_courier_id == user.id:
            await query.answer(
                "Назначенный курьер не может ставить эту отметку.",
                show_alert=True,
            )
            return
        if not _allowed(user.id, settings.manager_ids):
            await query.answer(
                "Только менеджеры могут отметить получение товара.",
                show_alert=True,
            )
            return

        if action == "pickup":
            if current.status != "pending" or not current.assigned_courier_id:
                await query.answer(
                    "Заказ уже изменился или курьер не выбран",
                    show_alert=True,
                )
                return
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            updated = repo.transition(
                current.id,
                {"pending"},
                expected_updated_at=current.updated_at,
                status="picked_up",
                picked_up_at=timestamp,
                courier_id=current.assigned_courier_id,
                courier_name=current.assigned_courier_name,
                actor_id=user.id,
                actor_name=_name(user),
                actor_role="manager",
            )
            message = f"Товар у курьера {current.assigned_courier_name}"
        else:
            if current.status != "picked_up":
                await query.answer("Отметку уже нельзя отменить", show_alert=True)
                return
            updated = repo.transition(
                current.id,
                {"picked_up"},
                expected_updated_at=current.updated_at,
                status="pending",
                picked_up_at=None,
                courier_id=None,
                courier_name=None,
                time_started=None,
                estimated_delivery_at=None,
                actor_id=user.id,
                actor_name=_name(user),
                actor_role="manager",
            )
            message = "Отметка «товар забран» снята"
        if not updated:
            await query.answer(
                "Заказ уже изменился. Используйте актуальную карточку.",
                show_alert=True,
            )
            return
        await query.answer(message)
        text, keyboard = _delivery_message(updated)
        success = await _finish_status_change_locked(
            context,
            query,
            updated,
            text,
            keyboard,
        )

    actor_name = escape(_name(user))
    courier_name = escape(updated.assigned_courier_name or updated.courier_name or "—")
    if action == "pickup":
        await _notify_log(
            context,
            f"📦 <b>Заказ №{updated.order_number}</b> · товар забран\n"
            f"🚚 Курьер: <b>{courier_name}</b>\n"
            f"👤 Отметил: {actor_name}",
            reply_markup=log_order_keyboard(updated),
        )
    else:
        await _notify_log(
            context,
            f"↩️ <b>Заказ №{updated.order_number}</b> · отметка «товар забран» снята\n"
            f"👤 Отметил: {actor_name}",
            reply_markup=log_order_keyboard(updated),
        )
    if not success:
        await _notify_manager(
            context,
            user.id,
            "⚠️ Статус сохранён, но часть сообщений Telegram обновится автоматически позже.",
        )


def _group_order_source_is_current(order, query, settings: Settings) -> bool:
    message = getattr(query, "message", None)
    return bool(
        message
        and order.delivery_chat_id
        and order.delivery_message_id
        and message.chat_id == order.delivery_chat_id
        and getattr(message, "message_id", None) == order.delivery_message_id
        and order.delivery_chat_id == _target_delivery_group(settings, order)
    )


def _group_actor_role(settings: Settings, user_id: int) -> str:
    if user_id in _allowed_courier_ids(settings):
        return "courier"
    if user_id in frozenset(getattr(settings, "manager_ids", ())):
        return "manager"
    return "group_member"


def _group_actor_text(user) -> str:
    username = (getattr(user, "username", None) or "").strip().lstrip("@")
    suffix = f" (@{escape(username)})" if username else ""
    return f"<b>{escape(_name(user))}</b>{suffix}"


async def group_cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel or restore an order by any accountable member of its group."""
    query = update.callback_query
    user = getattr(query, "from_user", None)
    if not user or getattr(user, "is_bot", False):
        await query.answer(
            "Отмена доступна только участнику группы с личного аккаунта.",
            show_alert=True,
        )
        return
    action, raw_id = query.data.split(":")
    settings: Settings = context.application.bot_data["settings"]
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order:
        await query.answer("Заказ не найден", show_alert=True)
        return
    if not _group_order_source_is_current(order, query, settings):
        await query.answer(
            "Эта карточка устарела. Используйте актуальное сообщение заказа.",
            show_alert=True,
        )
        return
    actor_name = _name(user)
    actor_username = (getattr(user, "username", None) or "").strip().lstrip("@") or None
    actor_role = _group_actor_role(settings, user.id)
    async with _order_sync_lock(context.application, order.id):
        current = repo.get(order.id)
        if not current or not _group_order_source_is_current(current, query, settings):
            await query.answer(
                "Заказ уже изменился. Используйте актуальную карточку.",
                show_alert=True,
            )
            return
        membership = await _delivery_group_membership(
            context,
            current.delivery_chat_id,
            user.id,
        )
        if membership is None:
            await query.answer(
                "Не удалось проверить участие в группе. Повторите ещё раз.",
                show_alert=True,
            )
            return
        if not membership:
            await query.answer(
                "Кнопка доступна только участникам этой группы доставки.",
                show_alert=True,
            )
            return

        if action == "cancel":
            if current.status == "cancelled":
                await query.answer("Заказ уже отменён", show_alert=True)
                return
            if current.status not in DELIVERY_ACTIVE_STATUSES:
                await query.answer("Заказ уже закрыт", show_alert=True)
                return
            timestamp = datetime.now(ZoneInfo("Asia/Tashkent"))
            updated = repo.transition(
                current.id,
                DELIVERY_ACTIVE_STATUSES,
                expected_updated_at=current.updated_at,
                status="cancelled",
                cancelled_by_id=user.id,
                cancelled_by_name=actor_name,
                cancelled_by_username=actor_username,
                cancelled_at=timestamp.isoformat(timespec="seconds"),
                cancelled_from_status=current.status,
                actor_id=user.id,
                actor_name=actor_name,
                actor_username=actor_username,
                actor_role=actor_role,
                event_type="order_cancelled",
            )
            if not updated:
                await query.answer(
                    "Заказ уже изменился. Используйте актуальную карточку.",
                    show_alert=True,
                )
                return
            await query.answer("Заказ отменён")
            await _finish_status_change_locked(
                context,
                query,
                updated,
                courier_card(updated, "❌ <b>Заказ отменён</b>"),
                courier_cancelled_keyboard(updated),
            )
            await _notify_log(
                context,
                f"❌ <b>Заказ №{updated.order_number}</b> отменён\n"
                f"👤 Отменил: {_group_actor_text(user)}\n"
                f"🆔 Telegram ID: <code>{user.id}</code>\n"
                f"🕒 {timestamp:%H:%M}",
                reply_markup=log_order_keyboard(updated),
            )
            return

        if current.status != "cancelled":
            await query.answer("Этот заказ уже нельзя вернуть", show_alert=True)
            return
        target_status = current.cancelled_from_status
        if target_status in {"awaiting_photo", "awaiting_amount"}:
            target_status = "on_way" if current.time_started else (
                "picked_up" if current.picked_up_at else "pending"
            )
        if target_status not in {"pending", "picked_up", "on_way"}:
            target_status = "picked_up" if current.picked_up_at else "pending"

        transition_fields = {"status": target_status}
        transition_options = {}
        effective_courier_id = current.courier_id or current.assigned_courier_id
        if target_status == "on_way":
            if not effective_courier_id:
                target_status = "picked_up" if current.picked_up_at else "pending"
                transition_fields = {
                    "status": target_status,
                    "time_started": None,
                    "estimated_delivery_at": None,
                }
            else:
                transition_fields["courier_id"] = effective_courier_id
                transition_options = {
                    "guard_courier_id": effective_courier_id,
                    "require_no_other_on_way_for_courier": True,
                }
        updated = repo.transition(
            current.id,
            {"cancelled"},
            expected_updated_at=current.updated_at,
            actor_id=user.id,
            actor_name=actor_name,
            actor_username=actor_username,
            actor_role=actor_role,
            event_type="order_cancel_restored",
            **transition_options,
            **transition_fields,
        )
        if not updated:
            conflict = (
                repo.get_on_way_for_courier(effective_courier_id, exclude_order_id=current.id)
                if target_status == "on_way" and effective_courier_id
                else None
            )
            message = (
                f"Сначала завершите заказ №{conflict.order_number}"
                if conflict
                else "Заказ уже изменился. Используйте актуальную карточку."
            )
            await query.answer(message, show_alert=True)
            return
        await query.answer("Заказ возвращён")
        text, keyboard = _delivery_message(updated)
        await _finish_status_change_locked(context, query, updated, text, keyboard)
        await _notify_log(
            context,
            f"↩️ <b>Заказ №{updated.order_number}</b> · отмена снята\n"
            f"👤 Вернул: {_group_actor_text(user)}\n"
            f"🚚 Курьер: {escape(updated.assigned_courier_name or updated.courier_name or '—')}",
            reply_markup=log_order_keyboard(updated),
        )


async def courier_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if not _allowed(query.from_user.id, _allowed_courier_ids(settings)):
        await query.answer("Нет доступа", show_alert=True); return
    action, raw_id = query.data.split(":"); repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get(int(raw_id))
    if not order:
        await query.answer("Заказ не найден", show_alert=True); return
    if order.delivery_chat_id and query.message.chat_id != order.delivery_chat_id:
        await query.answer("Этот заказ находится в другой группе курьера", show_alert=True); return
    if order.assigned_courier_id and order.assigned_courier_id != query.from_user.id:
        await query.answer(f"Заказ назначен курьеру {order.assigned_courier_name}", show_alert=True); return
    clicked_message_id = getattr(query.message, "message_id", None)
    if clicked_message_id and not order.delivery_message_id:
        adopted = repo.update(
            order.id,
            expected_updated_at=order.updated_at,
            delivery_chat_id=query.message.chat_id,
            delivery_message_id=clicked_message_id,
        )
        order = adopted or repo.get(order.id)
    if (
        clicked_message_id
        and order.delivery_message_id
        and (
            order.delivery_chat_id != query.message.chat_id
            or order.delivery_message_id != clicked_message_id
        )
    ):
        await query.answer("Эта карточка устарела. Используйте актуальное сообщение заказа.", show_alert=True)
        return
    if action == "read":
        await query.answer("Кнопка прочтения больше не используется")
        text, keyboard = _delivery_message(order)
        await _finish_status_change(context, query, order, text, keyboard)
        return
    if action == "undo_complete":
        if order.status not in {"awaiting_photo", "awaiting_amount", "completed"} or order.courier_id != query.from_user.id:
            await query.answer("Подтверждение уже нельзя отменить", show_alert=True); return
        target_status = (
            "on_way"
            if order.time_started
            else ("picked_up" if order.picked_up_at else "pending")
        )
        reset = {
            "status": target_status,
            "delivery_photo": None,
            "received_usd": None,
            "received_uzs": None,
            "delivered_at": None,
        }
        if target_status == "pending":
            reset.update(
                courier_id=None,
                courier_name=None,
                time_started=None,
                estimated_delivery_at=None,
            )
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
        await query.answer("Возвращено назад")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, state),
            keyboard,
        )
        await _notify_log(
            context,
            f"↩️ <b>Заказ №{order.order_number}</b> · подтверждение доставки отменено",
            reply_markup=log_order_keyboard(order),
        )
        return
    if order.status in {"completed", "cancelled"}:
        await query.answer("Заказ уже закрыт", show_alert=True); return
    if order.courier_id and order.courier_id != query.from_user.id:
        await query.answer(f"Заказ уже взял {order.courier_name}", show_alert=True); return
    configured_courier = courier_option(query.from_user.id)
    courier = {
        "courier_id": query.from_user.id,
        "courier_name": configured_courier.name if configured_courier else _name(query.from_user),
    }
    if action == "undo_onway":
        target_status = "picked_up" if order.picked_up_at else "pending"
        reset = {
            "status": target_status,
            "time_started": None,
            "estimated_delivery_at": None,
        }
        if target_status == "pending":
            reset.update(courier_id=None, courier_name=None)
        order = repo.transition(
            order.id,
            {"on_way"},
            guard_courier_id=query.from_user.id,
            require_unassigned_or_same=True,
            **reset,
        )
        if not order:
            await query.answer("Выезд уже нельзя отменить", show_alert=True); return
        await query.answer("Заказ возвращён в очередь")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, "↩️ <b>Выезд отменён</b>"),
            courier_keyboard(order),
        )
        await _notify_log(
            context,
            f"↩️ Курьер {escape(order.assigned_courier_name or order.courier_name or '—')} "
            f"отменил выезд к <b>заказу №{order.order_number}</b>.",
            reply_markup=log_order_keyboard(order),
        )
    elif action == "onway":
        if order.status == "on_way" and order.courier_id == query.from_user.id:
            await query.answer("Вы уже едете к этому заказу"); return
        if not order.assigned_courier_id:
            await query.answer("Сначала назначьте курьера", show_alert=True); return
        current = repo.get_on_way_for_courier(
            query.from_user.id,
            exclude_order_id=order.id,
        )
        if current:
            await query.answer(
                f"Сначала завершите заказ №{current.order_number}",
                show_alert=True,
            )
            return
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        order = repo.transition(
            order.id, {"pending", "picked_up"}, status="on_way",
            time_started=timestamp,
            guard_courier_id=query.from_user.id,
            require_assigned_to_courier=True,
            require_no_other_on_way_for_courier=True,
            **courier,
        )
        if not order:
            await query.answer(
                "Не удалось начать поездку. Обновите карточку или завершите "
                "текущую доставку.",
                show_alert=True,
            )
            return
        order = await _store_estimated_delivery_time(context, order)
        await query.answer("Статус обновлён")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, "🚗 <b>Курьер едет</b>"),
            on_way_keyboard(order),
        )
        await _notify_on_way_log(context, order)
    elif action == "complete":
        timestamp = datetime.now().astimezone()
        order = repo.transition(
            order.id,
            {"on_way"},
            status="completed",
            delivered_at=timestamp.isoformat(timespec="seconds"),
            guard_courier_id=query.from_user.id, require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Сначала нажмите «🚗 Еду к заказу»", show_alert=True); return
        result_text = completed_card(
            order,
            timestamp.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%H:%M"),
        )
        await query.answer("Заказ доставлен")
        await _finish_status_change(
            context,
            query,
            order,
            result_text,
            completed_keyboard(order),
        )
        await _notify_log(
            context,
            f"✅ Курьер <b>{escape(order.courier_name or _name(query.from_user))}</b> доставил "
            f"<b>заказ №{order.order_number}</b> в "
            f"{timestamp.astimezone(ZoneInfo('Asia/Tashkent')):%H:%M}\n"
            f"📦 {escape(order.product)} · 👤 {escape(order.seller_name or '—')}",
            reply_markup=log_order_keyboard(order),
        )
        await _send_post_delivery_prompt(context, order)
    else:
        await query.answer("Неизвестное действие", show_alert=True)


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
    log_channel_id = getattr(
        context.application.bot_data["settings"],
        "orders_channel_id",
        None,
    )
    if log_channel_id:
        try:
            await context.bot.send_photo(
                chat_id=log_channel_id,
                photo=order.delivery_photo,
                caption=result_text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Could not publish delivery photo for order %s to Log", order.id)
    await _set_location_marker(context, order)
    await update.message.reply_text("✅ Доставка подтверждена.")


async def delivery_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    chat = update.effective_chat
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    # Channel posts and anonymous/admin messages may not have an effective
    # user.  They are valid Telegram updates, but never delivery evidence.
    if not user or not chat or not message:
        return
    if user.id not in _allowed_courier_ids(settings):
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get_active_delivery(user.id)
    if not order:
        return
    if chat.id != order.delivery_chat_id:
        return
    if order.status == "awaiting_photo":
        if not message.photo:
            await message.reply_text(
                "Отправьте фото и укажите цену в подписи к фото. Пример: 100$ 1 920 000"
            ); return
        try:
            usd, uzs = parse_amount(message.caption or "")
        except ValueError:
            await message.reply_text(
                "Цена не распознана. Отправьте фото заново и напишите цену в подписи. "
                "Пример: 100$ 1 920 000"
            )
            return
        photo_id = message.photo[-1].file_id
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
            await message.reply_text("Статус заказа уже изменился."); return
        await _publish_completed(update, context, updated, timestamp)
        return
    if message.photo:
        await message.reply_text("Фото уже получено. Теперь введите сумму текстом."); return
    try: usd, uzs = parse_amount(message.text or "")
    except ValueError as error:
        await message.reply_text(str(error)); return
    timestamp = datetime.now().astimezone()
    order = repo.transition(order.id, {"awaiting_amount"}, status="completed", received_usd=usd, received_uzs=uzs, delivered_at=timestamp.isoformat(timespec="seconds"))
    if not order:
        await message.reply_text("Заказ уже обработан."); return
    await _publish_completed(update, context, order, timestamp)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _require_manager_flow(update, context):
        return ConversationHandler.END
    draft = context.user_data.get("draft") or {}
    repo: OrderRepository | None = context.application.bot_data.get("repo")
    committed = repo.get_by_creation_token(draft.get("creation_token")) if repo else None
    context.user_data.pop("edit", None)
    result = "Действие отменено."
    if committed and committed.status == "draft":
        actor = getattr(update, "effective_user", None)
        try:
            cancelled = repo.transition(
                committed.id,
                {"draft"},
                status="cancelled",
                actor_id=actor.id if actor else None,
                actor_name=_name(actor) if actor else None,
                actor_role="manager" if actor else None,
            )
        except Exception:
            # Keep the creation token in persistent user_data. A repeated
            # /cancel can then safely retry instead of losing the only link to
            # an already committed SQLite draft.
            logger.exception("Could not cancel committed draft order %s", committed.id)
            await update.message.reply_text(
                f"⚠️ Заказ №{committed.order_number} пока не отменён из-за временной ошибки базы. "
                "Повторите /cancel.",
                reply_markup=main_keyboard(),
            )
            return ConversationHandler.END
        if cancelled:
            context.user_data.pop("draft", None)
            result = f"Заказ №{cancelled.order_number} отменён."
            if cancelled.manager_message_id:
                _, success = await _sync_order(context, cancelled.id)
                if not success:
                    _schedule_sync_retry(context, cancelled.id)
            else:
                repo.mark_synced(cancelled.id, expected_updated_at=cancelled.updated_at)
        else:
            # The state changed concurrently, so this creation payload is no
            # longer an active draft that /cancel is allowed to modify.
            context.user_data.pop("draft", None)
            result = f"Заказ №{committed.order_number} уже обработан."
    else:
        context.user_data.pop("draft", None)
    await update.message.reply_text(result, reply_markup=main_keyboard())
    return ConversationHandler.END


async def show_all_locations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if not user or getattr(chat, "type", None) != "private" or user.id not in settings.manager_ids:
        if update.effective_message:
            await update.effective_message.reply_text("Все активные заказы доступны менеджерам в личном чате с ботом.")
        return
    context.user_data.pop("edit", None)
    repo: OrderRepository = context.application.bot_data["repo"]
    text, fallback_map_url = all_locations_card(repo.list_open())
    map_url = _monitor_url(settings) or fallback_map_url
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=all_locations_keyboard(map_url),
    )


async def location_label_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acknowledge label-like location buttons without any visible action."""
    await update.callback_query.answer()


def register_handlers(application: Application) -> None:
    application.add_handler(
        TypeHandler(Update, _access_guard),
        group=-1,
    )
    creation_menu = MessageHandler(
        filters.Regex(
            r"^(?:📋 (?:Мои|Активные|Все) заказы|📦 Все активные заказы|📚 Все заказы)$"
        )
        & filters.ChatType.PRIVATE,
        _end_creation_with_order_list,
    )
    creation_stats = MessageHandler(
        filters.Regex(r"^📊 Статистика$") & filters.ChatType.PRIVATE,
        _end_creation_with_statistics,
    )
    conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^➕ Новый заказ$") & filters.ChatType.PRIVATE, new_order)],
        states={
            SELLER: [creation_menu, creation_stats, MessageHandler(filters.TEXT & ~filters.COMMAND, seller)],
            PRODUCT: [creation_menu, creation_stats, MessageHandler(filters.TEXT & ~filters.COMMAND, product)],
            PRODUCT_PHOTO: [
                creation_menu,
                creation_stats,
                MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, product_photo),
            ],
            DETAILS: [creation_menu, creation_stats, MessageHandler(LOCATION_INPUT_FILTER & ~filters.COMMAND, details)],
            SECOND_LOCATION: [creation_menu, creation_stats, MessageHandler(LOCATION_INPUT_FILTER & ~filters.COMMAND, second_location)],
            PAYMENT: [creation_menu, creation_stats, MessageHandler(LOCATION_INPUT_FILTER & ~filters.COMMAND, payment)],
            DELIVERY_TIME: [creation_menu, creation_stats, MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_time)],
            COMMENT: [creation_menu, creation_stats, MessageHandler(filters.TEXT & ~filters.COMMAND, comment)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("map", _end_creation_with_map),
        ],
        allow_reentry=True,
        name="delivery_order_creation",
        persistent=True,
    )
    edit_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_edit, pattern=r"^edit:\d+:")],
        states={
            EDIT_VALUE: [
                MessageHandler((LOCATION_INPUT_FILTER | filters.PHOTO) & ~filters.COMMAND, save_edit)
            ],
        },
        fallbacks=[
            CommandHandler("start", _end_edit_on_global_command),
            CommandHandler("cancel", _end_edit_on_global_command),
            CommandHandler("map", _end_edit_on_global_command),
            MessageHandler(filters.Regex(r"^❌ Отменить изменение$"), cancel_edit),
            CallbackQueryHandler(
                _end_edit_on_global_command,
                pattern=(
                    r"^(?:(?:edit_(?:menu|close)|send|manager_cancel|manager_restore|sync|pickup|undo_pickup|group_pickup|group_undo_pickup):\d+"
                    r"|sales_(?:card|confirm|cancel):\d+"
                    r"|courier_(?:menu|close|assign|force_assign):\d+(?::\d+)?"
                    r"|list_order:\d+"
                    r"|orders_page:(?:active|all):\d+)$"
                ),
            ),
        ],
        allow_reentry=True,
        name="delivery_order_edit",
        persistent=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("map", show_all_locations, filters=filters.ChatType.PRIVATE))
    application.add_handler(MessageHandler(filters.Regex(r"^(?:📋 (?:Мои|Активные) заказы|📦 Все активные заказы)$") & filters.ChatType.PRIVATE, active_orders))
    application.add_handler(MessageHandler(filters.Regex(r"^(?:📋|📚) Все заказы$") & filters.ChatType.PRIVATE, my_orders))
    application.add_handler(MessageHandler(filters.Regex(r"^📊 Статистика$") & filters.ChatType.PRIVATE, show_statistics))
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("cancel", cancel_conversation, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(
        daily_delivery_log_action,
        pattern=r"^daily_log:(?:today|\d{4}-\d{2}-\d{2})$",
    ))
    application.add_handler(CallbackQueryHandler(orders_page, pattern=r"^orders_page:(?:active|all):\d+$"))
    application.add_handler(CallbackQueryHandler(open_order_from_list, pattern=r"^list_order:\d+$"))
    application.add_handler(CallbackQueryHandler(
        location_label_action,
        pattern=r"^location_(?:label|order):\d+$",
    ))
    application.add_handler(CallbackQueryHandler(toggle_edit_menu, pattern=r"^edit_(?:menu|close):\d+$"))
    application.add_handler(
        CallbackQueryHandler(
            sales_card_action,
            pattern=r"^sales_(?:card|confirm|cancel):\d+$",
        )
    )
    application.add_handler(CallbackQueryHandler(manager_action, pattern=r"^(send|manager_cancel|manager_restore):\d+$"))
    application.add_handler(CallbackQueryHandler(
        courier_assignment_action,
        pattern=r"^(?:control_)?courier_(?:menu|close|assign|force_assign):\d+(?::\d+)?$",
    ))
    application.add_handler(CallbackQueryHandler(manager_sync_action, pattern=r"^sync:\d+$"))
    application.add_handler(CallbackQueryHandler(
        manager_pickup_action,
        pattern=(
            r"^(?:(?:group_pickup|group_undo_pickup|pickup|undo_pickup):\d+"
            r"|(?:pickup_log|undo_pickup_log):\d+:\d+)$"
        ),
    ))
    application.add_handler(CallbackQueryHandler(
        group_cancel_action,
        pattern=r"^(?:cancel|undo_cancel):\d+$",
    ))
    application.add_handler(CallbackQueryHandler(
        courier_action,
        pattern=r"^(?:read|onway|undo_onway|complete|undo_complete):\d+$",
    ))
    application.add_handler(edit_conversation, group=1)
    application.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), delivery_input), group=2)
