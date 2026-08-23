import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from html import escape
from math import ceil
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from app.bot.keyboards import (
    all_locations_keyboard, completed_keyboard, courier_cancelled_keyboard,
    courier_keyboard, courier_selection_keyboard, delivery_day_log_keyboard,
    delivery_pending_keyboard, edit_input_keyboard,
    location_channel_keyboard, main_keyboard,
    manager_cancelled_keyboard, manager_sent_keyboard, on_way_keyboard,
    orders_channel_keyboard, orders_page_keyboard, payment_keyboard, review_keyboard, seller_keyboard,
    skip_keyboard, statistics_keyboard, text_location_keyboard,
)
from app.config import Settings
from app.database import OrderRepository
from app.utils import (
    completed_card, courier_card, enrich_location, manager_card,
    normalize_payment, normalize_seller, parse_amount,
    parse_order_details,
)
from app.utils.formatters import (
    STATUS_LABELS, all_locations_card, amount_text, daily_delivery_report,
    money, orders_channel_card,
)
from app.utils.couriers import (
    courier_group_id, courier_group_ids, courier_ids, courier_option,
)
from app.utils.parsers import display_phone
from app.utils.static_map import render_active_orders_map

logger = logging.getLogger(__name__)
SELLER, PRODUCT, DETAILS, SECOND_LOCATION, PAYMENT, DELIVERY_TIME, COMMENT, EDIT_VALUE = range(8)
MANAGER_EDITABLE_STATUSES = {"draft", "pending", "on_way"}
DELIVERY_ACTIVE_STATUSES = {"pending", "on_way", "awaiting_photo", "awaiting_amount"}
LOCATION_SEPARATOR = "\n".join(["📍" * 11] * 3)
ORDER_PAGE_SIZE = 10
EDIT_CANCEL_TEXT = "❌ Отменить изменение"
TEXT_LOCATION_BUTTON = "📝 Локация текстом"
MAIN_MENU_TEXTS = {
    "➕ Новый заказ",
    "📋 Активные заказы",
    "📋 Мои заказы",
    "📦 Все активные заказы",
    "📚 Все заказы",
    "📋 Все заказы",
    "📊 Статистика",
}


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


async def _send_courier_map_log(
    application: Application,
    *,
    completed_order_id: int,
    courier_id: int,
    courier_name: str,
) -> None:
    """Append a delivery summary and current active-order map to the journal."""
    repo: OrderRepository = application.bot_data["repo"]
    settings: Settings = application.bot_data["settings"]
    channel_id = getattr(settings, "orders_channel_id", None)
    if not channel_id:
        return
    completed_order = repo.get(completed_order_id)
    if not completed_order or completed_order.status != "completed":
        return

    active_orders = repo.list_active()
    courier_active = [
        order
        for order in active_orders
        if order.assigned_courier_id == courier_id or order.courier_id == courier_id
    ]
    now_tashkent = datetime.now(ZoneInfo("Asia/Tashkent"))
    today_start = now_tashkent.replace(hour=0, minute=0, second=0, microsecond=0)
    delivered_today = repo.count_completed_for_courier_since(courier_id, today_start)
    mapped_orders = [
        order
        for order in active_orders
        if (order.latitude is not None and order.longitude is not None)
        or (order.second_latitude is not None and order.second_longitude is not None)
    ]
    caption = (
        f"🗺 <b>Карта после доставки заказа №{completed_order.order_number}</b>\n"
        f"👤 Курьер: <b>{escape(courier_name)}</b>\n"
        f"✅ Доставлено сегодня: <b>{delivered_today}</b>\n"
        f"🚚 Осталось у курьера: <b>{len(courier_active)}</b>\n"
        f"📦 Всего активных заказов: <b>{len(active_orders)}</b>\n"
        f"📍 На карте: <b>{len(mapped_orders)}</b>"
    )

    image = None
    try:
        image = await render_active_orders_map(
            active_orders,
            cache_dir=settings.database_path.parent / "map-tiles",
        )
    except Exception:
        logger.exception("Could not render active-order map after order %s", completed_order_id)

    latest = repo.get(completed_order_id)
    if not latest or latest.status != "completed":
        return

    for attempt in range(2):
        try:
            if image is not None:
                image.seek(0)
                await application.bot.send_photo(
                    chat_id=channel_id,
                    photo=image,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=delivery_day_log_keyboard(now_tashkent.date().isoformat()),
                )
            else:
                suffix = "\n\nНе удалось создать фотографию карты."
                await application.bot.send_message(
                    chat_id=channel_id,
                    text=caption + suffix,
                    parse_mode=ParseMode.HTML,
                    reply_markup=delivery_day_log_keyboard(now_tashkent.date().isoformat()),
                )
            return
        except asyncio.CancelledError:
            raise
        except RetryAfter as error:
            if attempt == 0:
                retry_after = error.retry_after
                delay = (
                    retry_after.total_seconds()
                    if hasattr(retry_after, "total_seconds")
                    else float(retry_after)
                )
                await asyncio.sleep(max(1.0, delay))
                continue
            logger.exception("Courier map log remained rate-limited after order %s", completed_order_id)
            return
        except Exception:
            # A network timeout can mean Telegram accepted the photo but the
            # acknowledgement was lost. Do not blindly retry and create a
            # duplicate journal post.
            logger.exception("Could not publish courier map log after order %s", completed_order_id)
            return


def _schedule_courier_map_log(context: ContextTypes.DEFAULT_TYPE, order) -> None:
    create_task = getattr(context.application, "create_task", None)
    if not callable(create_task) or not order.courier_id:
        return
    create_task(
        _send_courier_map_log(
            context.application,
            completed_order_id=order.id,
            courier_id=order.courier_id,
            courier_name=order.courier_name or "—",
        ),
        name=f"delivery-map-log-{order.id}",
    )


async def _send_post_delivery_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    order,
) -> None:
    """Ask for optional evidence without reopening or blocking the order."""
    if not order.delivery_chat_id:
        return
    try:
        await context.bot.send_message(
            chat_id=order.delivery_chat_id,
            text=f"{order.courier_name or order.assigned_courier_name or 'Курьер'}, "
            "отправьте фото и цену товара 📸💰",
        )
    except Exception:
        logger.exception("Could not send optional delivery prompt for order %s", order.id)


async def daily_delivery_log_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    settings: Settings = context.application.bot_data["settings"]
    if not _allowed(
        query.from_user.id,
        settings.manager_ids | _allowed_courier_ids(settings),
    ):
        await query.answer("Нет доступа", show_alert=True)
        return
    if query.message.chat_id != settings.orders_channel_id:
        await query.answer("Кнопка работает только в канале Log", show_alert=True)
        return

    raw_day = query.data.partition(":")[2]
    try:
        report_day = (
            datetime.now(ZoneInfo("Asia/Tashkent")).date()
            if raw_day == "today"
            else datetime.strptime(raw_day, "%Y-%m-%d").date()
        )
    except ValueError:
        await query.answer("Неверная дата", show_alert=True)
        return

    await query.answer("Формирую хронологию…")
    repo: OrderRepository = context.application.bot_data["repo"]
    tashkent = ZoneInfo("Asia/Tashkent")
    day_start = datetime(
        report_day.year,
        report_day.month,
        report_day.day,
        tzinfo=tashkent,
    )
    reports = daily_delivery_report(
        repo.list_all(),
        report_day,
        repo.list_events_between(day_start, day_start + timedelta(days=1)),
    )
    try:
        for report in reports:
            await context.bot.send_message(
                chat_id=settings.orders_channel_id,
                text=report,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception:
        # A timeout can mean that Telegram accepted a report. Blind retries
        # would create duplicate journal messages.
        logger.exception("Could not publish daily delivery log for %s", report_day)


async def _location_values(message) -> dict:
    if message.location:
        latitude, longitude = message.location.latitude, message.location.longitude
        url = f"https://yandex.uz/maps/?ll={longitude:.6f}%2C{latitude:.6f}&z=17"
    else:
        url = message.text or ""
        latitude = longitude = None
    values = await enrich_location(latitude, longitude, url)
    return _validated_location(values)


def _validated_location(values: dict) -> dict:
    if values["latitude"] is None or values["longitude"] is None:
        raise ValueError(
            "Не удалось определить координаты. Отправьте Telegram Location или полную ссылку Яндекс Карт."
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
    """Refresh the pin buttons and the two visual separator messages."""
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
            footer_field = "second_location_footer_message_id"
        else:
            chat_id = order.location_chat_id
            message_id = order.location_message_id
            details_message_id = order.location_details_message_id
            footer_message_id = order.location_footer_message_id
            details_field = "location_details_message_id"
            footer_field = "location_footer_message_id"
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

        for separator_id, separator_field in (
            (details_message_id, details_field),
            (footer_message_id, footer_field),
        ):
            if not separator_id:
                continue
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=separator_id,
                    text=LOCATION_SEPARATOR,
                )
            except Exception as error:
                if _message_is_not_modified(error):
                    continue
                if _message_is_missing(error):
                    latest = repo.get(order.id)
                    if latest:
                        repo.update(
                            latest.id,
                            expected_updated_at=latest.updated_at,
                            **{separator_field: None},
                        )
                else:
                    logger.exception(
                        "Could not refresh location separator %s for order %s",
                        separator_field,
                        order.id,
                    )
                success = False
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
        except Exception as error:
            if _message_is_missing(error):
                repo.mark_cleanup_done(item["id"])
            else:
                success = False
                repo.mark_cleanup_failed(item["id"], str(error))
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
    assigned_group = courier_group_id(order.assigned_courier_id)
    if assigned_group is not None:
        return assigned_group
    return settings.delivery_group_id


def _allowed_courier_ids(settings: Settings) -> frozenset[int]:
    return settings.courier_ids | courier_ids()


def _known_delivery_groups(settings: Settings) -> frozenset[int]:
    return courier_group_ids() | frozenset({settings.delivery_group_id})


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
        if delivery_member.status in {"left", "kicked"}:
            raise RuntimeError(f"The delivery bot must be a member of group {delivery_group_id}")
        if delivery_member.status == "restricted" and not getattr(
            delivery_member,
            "can_send_messages",
            False,
        ):
            raise RuntimeError(f"The delivery bot cannot send to group {delivery_group_id}")
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
        "🔒 При открытии сайт запросит логин и пароль.",
        parse_mode=ParseMode.HTML,
        reply_markup=statistics_keyboard(settings.stats_url),
    )


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
        _map_text, map_url = all_locations_card(repo.list_open())
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


async def new_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_chat.type != "private" or not _allowed(update.effective_user.id, settings.manager_ids):
        await update.message.reply_text("Создание заказа доступно менеджерам в личном чате с ботом.")
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
    await update.message.reply_text("1/6. Выберите, кому принадлежит заказ:", reply_markup=seller_keyboard())
    return SELLER


async def seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    await update.message.reply_text("2/6. Введите модель товара:", reply_markup=ReplyKeyboardRemove())
    return PRODUCT


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        "3/6. Отправьте телефон, цену и локацию. Можно одним сообщением или отдельно, в любом порядке.\n\n"
        "Пример одного сообщения:\n"
        "Телефон: 90 133 39 99\n"
        "Цена: 100$ 1 920 000\n"
        "Локация: https://yandex.uz/maps/…\n\n"
        "Можно указать два телефона. Две Telegram Location отправьте подряд, "
        "либо пришлите две ссылки в одном сообщении — бот сохранит обе без отдельного вопроса. "
        "Если координат нет, после телефона и цены появится кнопка «📝 Локация текстом»."
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
    if draft.get("latitude") is None or draft.get("longitude") is None:
        draft.update(values)
        return 1
    if _same_location(draft, values):
        return 1
    if draft.get("second_latitude") is None or draft.get("second_longitude") is None:
        draft.update(_as_second_location(values))
        return 2
    if _same_location(draft, values, "second_"):
        return 2
    raise ValueError("У заказа уже сохранены две локации")


async def _capture_order_details(message, draft: dict) -> list[str]:
    recognized: list[str] = []
    if message.location:
        location_number = _merge_location(draft, await _location_values(message))
        recognized.append(f"локация {location_number}")
        return recognized

    parsed = parse_order_details(message.text or "")
    phones = list(parsed.get("client_phones") or [])
    if phones:
        count = _merge_phones(draft, phones)
        recognized.append("два номера" if count > 1 else "номер")
    if "amount_usd" in parsed or "amount_uzs" in parsed:
        draft["amount_usd"] = parsed.get("amount_usd")
        draft["amount_uzs"] = parsed.get("amount_uzs")
        recognized.append("цена")
    for raw_url in list(parsed.get("location_urls") or []):
        values = _validated_location(await enrich_location(None, None, str(raw_url)))
        location_number = _merge_location(draft, values)
        label = f"локация {location_number}"
        if label not in recognized:
            recognized.append(label)
    return recognized


async def details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END

    incoming_text = (update.message.text or "").strip()
    if draft.get("awaiting_text_location"):
        if update.message.location:
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
                address_text=address,
                district=None,
                mahalla=None,
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

    await update.message.reply_text("4/6. Выберите вариант оплаты:", reply_markup=payment_keyboard())
    return PAYMENT


def _as_second_location(values: dict) -> dict:
    return {f"second_{key}": value for key, value in values.items()}


async def second_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compatibility path for conversations started by the previous release."""
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    text = (update.message.text or "").strip().casefold()
    if text == "продолжить без второй локации":
        await update.message.reply_text(
            "4/6. Выберите вариант оплаты:",
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
        f"✅ Сохранено: {', '.join(recognized)}.\n\n4/6. Выберите вариант оплаты:",
        reply_markup=payment_keyboard(),
    )
    return PAYMENT


async def payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        "5/6. Укажите время доставки (например, До 17:00) или пропустите:",
        reply_markup=skip_keyboard(),
    )
    return DELIVERY_TIME


async def delivery_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = context.user_data.get("draft")
    if draft is None:
        await update.message.reply_text("Начните новый заказ заново.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        value = _text(update.message, maximum=100, required=False)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return DELIVERY_TIME
    draft["delivery_time"] = value
    await update.message.reply_text("6/6. Добавьте комментарий или пропустите:", reply_markup=skip_keyboard())
    return COMMENT


async def comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    await update.message.reply_text("Проверьте данные заказа.", reply_markup=main_keyboard())
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
        "phone": "Введите один или два новых номера:",
        "location": "Отправьте новую основную локацию или ссылку:",
        "second_location": "Отправьте дополнительную локацию или ссылку:",
        "amount": "Введите новую сумму. Например: 120$ 1 536 000",
        "delivery_time": "Введите новое время (или Пропустить):",
        "comment": "Введите новый комментарий (или Пропустить):",
    }
    await query.message.reply_text(prompts[field], reply_markup=edit_input_keyboard(field))
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


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit", None)
    await update.message.reply_text("Изменение отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        elif field == "payment_status": values["payment_status"] = normalize_payment(update.message.text or "")
        elif field == "phone":
            phones = list(parse_order_details(update.message.text or "").get("client_phones") or [])
            if not phones:
                raise ValueError("Введите один или два узбекских номера")
            values["client_phone"] = phones[0]
            values["client_phone_2"] = phones[1] if len(phones) > 1 else None
        elif field == "amount": values["amount_usd"], values["amount_uzs"] = parse_amount(update.message.text or "")
        elif field in {"location", "second_location"}:
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

    sent = previous.status != "draft"
    location_number = 2 if field == "second_location" else 1
    publication_fields: dict = {}
    if sent and field in {"location", "second_location"}:
        candidate = replace(previous, **values)
        try:
            publication_fields = await _send_location_messages(context, candidate, location_number)
        except Exception:
            logger.exception("Could not publish replacement location for order %s", previous.id)
            await update.message.reply_text(
                "⚠️ Новая локация не сохранена: Telegram-канал недоступен. Старая точка осталась рабочей. Попробуйте ещё раз.",
                reply_markup=edit_input_keyboard(field),
            )
            return EDIT_VALUE
        values.update(publication_fields)

    actor = getattr(update, "effective_user", None)
    cleanup_messages: list[tuple[int, int]] = []
    if publication_fields:
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
    if publication_fields:
        await _process_cleanup_messages(context, order_id=order.id)

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
        if not refreshed:
            _schedule_sync_retry(context, order.id)
    else:
        refreshed = manager_refreshed
        refreshed_order = repo.get(order.id)
        if manager_refreshed and refreshed_order.sync_needed:
            repo.mark_synced(refreshed_order.id, expected_updated_at=refreshed_order.updated_at)
        elif not manager_refreshed:
            _schedule_sync_retry(context, order.id)
    context.user_data.pop("edit", None)
    if not manager_refreshed:
        result = "⚠️ Данные сохранены в базе, но карточку менеджера обновить не удалось."
    elif not refreshed:
        result = "⚠️ Данные сохранены, но карточку в группе обновить не удалось."
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
            time_started=None,
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
    await query.edit_message_reply_markup(reply_markup=courier_selection_keyboard(order))
    await query.answer("Выберите курьера")


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
    if orders_channel_source:
        source_is_current = bool(
            order.orders_channel_chat_id
            and order.orders_channel_message_id
            and query.message.chat_id == order.orders_channel_chat_id
            and query.message.message_id == order.orders_channel_message_id
            and query.message.chat_id == getattr(settings, "orders_channel_id", None)
        )
    else:
        source_is_current = bool(
            not order.manager_message_id
            or (
                query.message.chat_id == order.manager_chat_id
                and query.message.message_id == order.manager_message_id
            )
        )
    if not source_is_current:
        await query.answer("Эта карточка устарела. Откройте актуальную карточку заказа.", show_alert=True)
        return
    if order.status not in {"draft", "pending", "on_way"}:
        await query.answer("Для закрытого заказа курьера изменить нельзя", show_alert=True)
        return
    if action == "courier_menu":
        source = "orders_channel" if orders_channel_source else "manager"
        await query.edit_message_reply_markup(
            reply_markup=courier_selection_keyboard(order, source=source)
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
    if not selected:
        await query.answer("Курьер не найден", show_alert=True)
        return
    if order.status != "draft" and order.assigned_courier_id == selected.user_id:
        keyboard = orders_channel_keyboard(order) if orders_channel_source else manager_sent_keyboard(order)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer(f"Курьер {selected.name} уже выбран")
        return

    # Publish first. Only after Telegram confirms the new card do we switch
    # SQLite and enqueue the previous group's card for deletion atomically.
    candidate = replace(
        order,
        status="pending",
        assigned_courier_id=selected.user_id,
        assigned_courier_name=selected.name,
        courier_id=None,
        courier_name=None,
        time_started=None,
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
        {"draft", "pending", "on_way"},
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
        time_started=None,
        delivery_photo=None,
        received_usd=None,
        received_uzs=None,
        delivered_at=None,
        delivery_chat_id=sent.chat_id,
        delivery_message_id=sent.message_id,
    )
    if not updated:
        repo.enqueue_cleanup_messages(order.id, [(sent.chat_id, sent.message_id)])
        await _process_cleanup_messages(context, order_id=order.id)
        await _notify_manager(
            context,
            query.from_user.id,
            "⚠️ Заказ уже изменился. Новая карточка курьера удалена; откройте актуальный заказ.",
        )
        return

    await _process_cleanup_messages(context, order_id=updated.id)
    updated, synchronized = await _sync_order(context, updated.id)
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
        await query.answer("Заказ возвращён")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, "↩️ <b>Отмена снята, заказ снова активен</b>"),
            courier_keyboard(order),
        )
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
            reset.update(courier_id=None, courier_name=None, time_started=None)
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
        await _notify_manager(
            context,
            order.manager_id,
            f"↩️ Подтверждение доставки заказа №{order.order_number} отменено. Заказ снова активен.",
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
        await query.answer("Заказ возвращён в очередь")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, "↩️ <b>Выезд отменён, заказ снова свободен</b>"),
            courier_keyboard(order),
        )
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
        await query.answer("Статус обновлён")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, "🚗 <b>Курьер едет</b>"),
            on_way_keyboard(order),
        )
        await _notify_manager(
            context,
            order.manager_id,
            f"🚗 Курьер едет к заказу №{order.order_number}.",
        )
    elif action == "cancel":
        order = repo.transition(
            order.id, {"pending", "on_way", "awaiting_photo", "awaiting_amount"},
            status="cancelled", guard_courier_id=query.from_user.id,
            require_unassigned_or_same=True, **courier,
        )
        if not order:
            await query.answer("Заказ уже обработан другим курьером", show_alert=True); return
        await query.answer("Заказ отменён")
        await _finish_status_change(
            context,
            query,
            order,
            courier_card(order, "❌ <b>Заказ отменён</b>"),
            courier_cancelled_keyboard(order),
        )
        await _notify_manager(
            context,
            order.manager_id,
            f"❌ Заказ №{order.order_number} отменён курьером {escape(_name(query.from_user))}.",
        )
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
        await query.answer("Заказ доставлен")
        await _finish_status_change(
            context,
            query,
            order,
            result_text,
            completed_keyboard(order),
        )
        await _notify_manager(context, order.manager_id, result_text)
        await _send_post_delivery_prompt(context, order)
        _schedule_courier_map_log(context, order)


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
    _schedule_courier_map_log(context, order)


async def delivery_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if update.effective_user.id not in _allowed_courier_ids(settings):
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    order = repo.get_active_delivery(update.effective_user.id)
    if not order:
        return
    if update.effective_chat.id != order.delivery_chat_id:
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
    user_id = update.effective_user.id
    if update.effective_chat.type != "private" or user_id not in settings.manager_ids:
        if update.effective_message:
            await update.effective_message.reply_text("Все активные заказы доступны менеджерам в личном чате с ботом.")
        return
    context.user_data.pop("edit", None)
    repo: OrderRepository = context.application.bot_data["repo"]
    text, map_url = all_locations_card(repo.list_open())
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
            DETAILS: [creation_menu, creation_stats, MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, details)],
            SECOND_LOCATION: [creation_menu, creation_stats, MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, second_location)],
            PAYMENT: [creation_menu, creation_stats, MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, payment)],
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
            EDIT_VALUE: [MessageHandler((filters.LOCATION | filters.TEXT) & ~filters.COMMAND, save_edit)],
        },
        fallbacks=[
            CommandHandler("start", _end_edit_on_global_command),
            CommandHandler("cancel", _end_edit_on_global_command),
            CommandHandler("map", _end_edit_on_global_command),
            MessageHandler(filters.Regex(r"^❌ Отменить изменение$"), cancel_edit),
            CallbackQueryHandler(
                _end_edit_on_global_command,
                pattern=(
                    r"^(?:(?:edit_(?:menu|close)|send|manager_cancel|manager_restore|sync):\d+"
                    r"|courier_(?:menu|close|assign):\d+(?::\d+)?"
                    r"|orders_page:(?:active|all):\d+)$"
                ),
            ),
        ],
        allow_reentry=True,
        name="delivery_order_edit",
        persistent=True,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("map", show_all_locations))
    application.add_handler(MessageHandler(filters.Regex(r"^(?:📋 (?:Мои|Активные) заказы|📦 Все активные заказы)$") & filters.ChatType.PRIVATE, active_orders))
    application.add_handler(MessageHandler(filters.Regex(r"^(?:📋|📚) Все заказы$") & filters.ChatType.PRIVATE, my_orders))
    application.add_handler(MessageHandler(filters.Regex(r"^📊 Статистика$") & filters.ChatType.PRIVATE, show_statistics))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_conversation))
    application.add_handler(CallbackQueryHandler(
        daily_delivery_log_action,
        pattern=r"^daily_log:(?:today|\d{4}-\d{2}-\d{2})$",
    ))
    application.add_handler(CallbackQueryHandler(orders_page, pattern=r"^orders_page:(?:active|all):\d+$"))
    application.add_handler(CallbackQueryHandler(
        location_label_action,
        pattern=r"^location_(?:label|order):\d+$",
    ))
    application.add_handler(CallbackQueryHandler(toggle_edit_menu, pattern=r"^edit_(?:menu|close):\d+$"))
    application.add_handler(CallbackQueryHandler(manager_action, pattern=r"^(send|manager_cancel|manager_restore):\d+$"))
    application.add_handler(CallbackQueryHandler(
        courier_assignment_action,
        pattern=r"^(?:control_)?courier_(?:menu|close|assign):\d+(?::\d+)?$",
    ))
    application.add_handler(CallbackQueryHandler(manager_sync_action, pattern=r"^sync:\d+$"))
    application.add_handler(CallbackQueryHandler(courier_action, pattern=r"^(onway|undo_onway|complete|cancel|undo_cancel|undo_complete):\d+$"))
    application.add_handler(edit_conversation, group=1)
    application.add_handler(MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), delivery_input), group=2)
