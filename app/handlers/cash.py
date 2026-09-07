import logging
from datetime import datetime
from html import escape
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.config import Settings
from app.database import OrderRepository
from app.models import CourierCashEntry
from app.utils.couriers import courier_option
from app.utils.parsers import contains_cash_keyword, parse_courier_cash

logger = logging.getLogger(__name__)
TASHKENT = ZoneInfo("Asia/Tashkent")


def _name(user) -> str:
    return user.full_name or user.username or str(user.id)


def _number(value: int) -> str:
    return f"{abs(value):,}".replace(",", " ")


def _signed(value: int, suffix: str) -> str:
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{_number(value)} {suffix}"


def _amount_lines(usd: int, uzs: int, *, signed: bool) -> list[str]:
    render = _signed if signed else lambda value, suffix: f"{_number(value)} {suffix}"
    lines = []
    if usd:
        lines.append(f"💵 {render(usd, '$')}")
    if uzs:
        lines.append(f"🇺🇿 {render(uzs, 'сум')}")
    return lines or ["—"]


def _balance_lines(balance: tuple[int, int]) -> str:
    usd, uzs = balance
    usd_sign = "−" if usd < 0 else ""
    uzs_sign = "−" if uzs < 0 else ""
    return (
        f"💵 {usd_sign}{_number(usd)} $\n"
        f"🇺🇿 {uzs_sign}{_number(uzs)} сум"
    )


def _local_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TASHKENT)
    return parsed.strftime("%H:%M")


def cash_review_keyboard(entry: CourierCashEntry) -> InlineKeyboardMarkup:
    suffix = f"{entry.id}:{entry.revision}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Получил", callback_data=f"cash_ok:{suffix}"),
            InlineKeyboardButton("❌ Не получил", callback_data=f"cash_no:{suffix}"),
        ],
        [InlineKeyboardButton("✏️ Другая сумма", callback_data=f"cash_other:{suffix}")],
    ])


def cash_notification_text(
    entry: CourierCashEntry,
    balance: tuple[int, int],
) -> str:
    balance_lines = _balance_lines(balance)
    courier = escape(entry.courier_name)
    if entry.entry_type == "receipt":
        delta_lines = "\n".join(_amount_lines(entry.delta_usd, entry.delta_uzs, signed=True))
        created = _local_time(entry.created_at)
        time_line = f"\n🕒 Записано: {created}" if created else ""
        return (
            "🧾 <b>Сумма учтена</b>\n"
            f"🚚 Курьер: <b>{courier}</b>\n\n"
            f"Изменение кассы:\n{delta_lines}\n\n"
            f"💼 <b>Сейчас у курьера</b>\n{balance_lines}{time_line}"
        )

    amount_lines = "\n".join(_amount_lines(entry.amount_usd, entry.amount_uzs, signed=False))
    heading = f"🏦 <b>Касса на подтверждение №K-{entry.id}</b>"
    footer = "⏳ Ожидает подтверждения администратора"
    if entry.status == "confirmed":
        heading = f"✅ <b>Касса получена №K-{entry.id}</b>"
        footer = f"👤 Подтвердил: <b>{escape(entry.reviewed_by_name or '—')}</b>"
    elif entry.status == "rejected":
        heading = f"❌ <b>Касса не получена №K-{entry.id}</b>"
        footer = f"👤 Отметил: <b>{escape(entry.reviewed_by_name or '—')}</b>"
    action_time = _local_time(entry.reviewed_at or entry.created_at)
    if action_time:
        footer += f"\n🕒 {action_time}"
    corrected = "\n✏️ Сумма исправлена администратором" if entry.corrected_at else ""
    return (
        f"{heading}\n"
        f"🚚 Курьер: <b>{courier}</b>\n\n"
        f"Передаёт:\n{amount_lines}{corrected}\n\n"
        f"💼 <b>Сейчас у курьера</b>\n{balance_lines}\n\n"
        f"{footer}"
    )


def _message_missing(error: Exception) -> bool:
    if not isinstance(error, BadRequest):
        return False
    text = str(error).casefold()
    return "message to delete not found" in text or "message not found" in text


async def _delete_cash_source(context: ContextTypes.DEFAULT_TYPE, entry: CourierCashEntry) -> bool:
    if entry.entry_type != "handover" or entry.source_deleted_at:
        return True
    repo: OrderRepository = context.application.bot_data["repo"]
    try:
        await context.bot.delete_message(entry.source_chat_id, entry.source_message_id)
    except RetryAfter:
        raise
    except Forbidden as error:
        repo.record_cash_source_delete_failure(entry.id, str(error), terminal=True)
        logger.error(
            "Cash handover source cannot be deleted %s/%s: %s",
            entry.source_chat_id,
            entry.source_message_id,
            error,
        )
        return False
    except BadRequest as error:
        if _message_missing(error):
            repo.mark_cash_source_deleted(entry.id)
            return True
        repo.record_cash_source_delete_failure(entry.id, str(error), terminal=True)
        logger.error(
            "Cash handover source permanently rejected deletion %s/%s: %s",
            entry.source_chat_id,
            entry.source_message_id,
            error,
        )
        return False
    except Exception as error:
        failed = repo.record_cash_source_delete_failure(entry.id, str(error))
        logger.warning(
            "Could not delete cash handover source %s/%s (attempt %s): %s",
            entry.source_chat_id,
            entry.source_message_id,
            failed.source_delete_attempts if failed else "?",
            error,
        )
        return False
    repo.mark_cash_source_deleted(entry.id)
    return True


async def publish_cash_notification(
    context: ContextTypes.DEFAULT_TYPE,
    entry: CourierCashEntry,
) -> CourierCashEntry:
    repo: OrderRepository = context.application.bot_data["repo"]
    current = repo.get_cash_entry(entry.id)
    if not current:
        raise ValueError("cash entry no longer exists")
    if current.notification_message_id:
        if current.entry_type == "handover":
            await _delete_cash_source(context, current)
        return current

    publishing: set[int] = context.application.bot_data.setdefault("cash_notifications_publishing", set())
    if current.id in publishing:
        return current
    publishing.add(current.id)
    try:
        balance = repo.cash_balance(current.courier_id)
        keyboard = cash_review_keyboard(current) if current.status == "pending" else None
        settings: Settings = context.application.bot_data["settings"]
        sent = await context.bot.send_message(
            chat_id=settings.cash_notification_channel_id,
            text=cash_notification_text(current, balance),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        attached = repo.attach_cash_notification(
            current.id,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
        )
        if not attached:
            # Another concurrent publisher won. Its message is canonical.
            try:
                await context.bot.delete_message(sent.chat_id, sent.message_id)
            except Exception:
                logger.exception("Could not remove duplicate cash notification %s", sent.message_id)
            attached = repo.get_cash_entry(current.id)
        if attached and attached.entry_type == "handover":
            await _delete_cash_source(context, attached)
        return attached or current
    finally:
        publishing.discard(current.id)


async def reconcile_cash_entries(application) -> None:
    """Recover Telegram publishing/deletion after a timeout or restart."""
    repo: OrderRepository = application.bot_data["repo"]
    context = SimpleNamespace(application=application, bot=application.bot)
    for entry in repo.list_cash_needing_notification(limit=50):
        try:
            await publish_cash_notification(context, entry)
        except RetryAfter:
            raise
        except Exception:
            logger.exception("Could not recover cash notification %s", entry.id)
    for entry in repo.list_cash_notifications_needing_sync(limit=50):
        try:
            await _refresh_cash_notification(context, entry)
        except RetryAfter:
            raise
        except Exception:
            logger.exception("Could not synchronize cash notification %s", entry.id)
    for entry in repo.list_cash_sources_needing_deletion(limit=50):
        try:
            await _delete_cash_source(context, entry)
        except RetryAfter:
            raise
        except Exception:
            logger.exception("Could not recover cash source deletion %s", entry.id)


async def courier_cash_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return
    courier = courier_option(user.id)
    if courier is None or chat.id != courier.group_id:
        return
    raw = (message.text or message.caption or "").strip()
    if not raw:
        return
    try:
        parsed = parse_courier_cash(raw)
    except ValueError as error:
        if contains_cash_keyword(raw):
            await message.reply_text(str(error))
        return

    repo: OrderRepository = context.application.bot_data["repo"]
    reply = getattr(message, "reply_to_message", None)
    entry, created = repo.create_cash_entry(
        courier_id=courier.user_id,
        courier_name=courier.name,
        entry_type="handover" if parsed.is_handover else "receipt",
        usd=parsed.usd,
        uzs=parsed.uzs,
        raw_text=raw,
        source_chat_id=chat.id,
        source_message_id=message.message_id,
        source_reply_message_id=getattr(reply, "message_id", None),
    )
    if created or not entry.notification_message_id:
        try:
            entry = await publish_cash_notification(context, entry)
        except (RetryAfter, Forbidden):
            raise
        except Exception:
            logger.exception("Could not publish cash entry %s", entry.id)
    elif entry.entry_type == "handover" and not entry.source_deleted_at:
        await _delete_cash_source(context, entry)


async def _is_cash_channel_admin(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    try:
        member = await context.bot.get_chat_member(settings.cash_notification_channel_id, user_id)
    except Exception:
        logger.exception("Could not verify cash-channel administrator %s", user_id)
        return False
    return member.status in {"administrator", "creator", "owner"}


async def _refresh_cash_notification(
    context: ContextTypes.DEFAULT_TYPE,
    entry: CourierCashEntry,
) -> None:
    if not entry.notification_chat_id or not entry.notification_message_id:
        return
    repo: OrderRepository = context.application.bot_data["repo"]
    balance = repo.cash_balance(entry.courier_id)
    keyboard = cash_review_keyboard(entry) if entry.status == "pending" else None
    try:
        await context.bot.edit_message_text(
            chat_id=entry.notification_chat_id,
            message_id=entry.notification_message_id,
            text=cash_notification_text(entry, balance),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except BadRequest as error:
        error_text = str(error).casefold()
        if "message is not modified" in error_text:
            repo.mark_cash_notification_synced(
                entry.id,
                expected_updated_at=entry.updated_at,
            )
            return
        if "message to edit not found" in error_text or "message not found" in error_text:
            repo.clear_cash_notification(entry.id)
            current = repo.get_cash_entry(entry.id)
            if current:
                await publish_cash_notification(context, current)
            return
        raise
    repo.mark_cash_notification_synced(
        entry.id,
        expected_updated_at=entry.updated_at,
    )


async def cash_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    settings: Settings = context.application.bot_data["settings"]
    if query.message.chat_id != settings.cash_notification_channel_id:
        await query.answer("Эта кнопка не из канала кассы", show_alert=True)
        return
    if not await _is_cash_channel_admin(context, user.id):
        await query.answer("Подтверждать кассу может только администратор канала", show_alert=True)
        return
    action, raw_id, raw_revision = query.data.split(":")
    repo: OrderRepository = context.application.bot_data["repo"]
    entry = repo.get_cash_entry(int(raw_id))
    if not entry:
        await query.answer("Операция не найдена", show_alert=True)
        return
    if (
        entry.notification_chat_id != query.message.chat_id
        or entry.notification_message_id != query.message.message_id
    ):
        await query.answer("Карточка устарела", show_alert=True)
        return
    revision = int(raw_revision)
    if action == "cash_other":
        if entry.status != "pending" or entry.revision != revision:
            await _refresh_cash_notification(context, entry)
            await query.answer("Сумма уже изменена или операция обработана", show_alert=True)
            return
        existing = context.user_data.get("cash_correction")
        if existing and int(existing.get("entry_id", 0)) != entry.id:
            await query.answer(
                "Сначала завершите прежнее изменение суммы в личном чате или отправьте /cancel",
                show_alert=True,
            )
            return
        context.user_data["cash_correction"] = {
            "entry_id": entry.id,
            "revision": entry.revision,
        }
        try:
            await context.bot.send_message(
                user.id,
                f"✏️ Введите правильную сумму для кассы №K-{entry.id}.\n"
                "Например: 35$ 200000 сум\n\n/cancel — отменить изменение",
            )
        except Exception:
            context.user_data.pop("cash_correction", None)
            await query.answer(
                "Сначала откройте личный чат с ботом и нажмите /start",
                show_alert=True,
            )
            return
        await query.answer("Запрос отправлен вам в личный чат", show_alert=True)
        return

    decision = "confirmed" if action == "cash_ok" else "rejected"
    updated = repo.review_cash_handover(
        entry.id,
        expected_revision=revision,
        decision=decision,
        reviewer_id=user.id,
        reviewer_name=_name(user),
    )
    if not updated:
        current = repo.get_cash_entry(entry.id)
        if current:
            await _refresh_cash_notification(context, current)
        await query.answer("Операция уже обработана или сумма изменена", show_alert=True)
        return
    await _refresh_cash_notification(context, updated)
    await query.answer("Касса подтверждена" if decision == "confirmed" else "Отмечено: не получено")


async def cash_correction_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("cash_correction")
    if not pending:
        return
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or chat.type != "private" or not message:
        return
    if not await _is_cash_channel_admin(context, user.id):
        context.user_data.pop("cash_correction", None)
        await message.reply_text("Права администратора канала кассы больше не доступны.")
        raise ApplicationHandlerStop
    raw = (message.text or "").strip()
    if raw.casefold() in {
        "/start", "/map", "➕ новый заказ", "📋 активные заказы",
        "📚 все заказы", "📊 статистика",
    }:
        context.user_data.pop("cash_correction", None)
        return
    if raw.casefold() in {"/cancel", "отмена"}:
        context.user_data.pop("cash_correction", None)
        await message.reply_text("Изменение суммы кассы отменено.")
        raise ApplicationHandlerStop
    try:
        parsed = parse_courier_cash(raw)
        if parsed.usd < 0 or parsed.uzs < 0:
            raise ValueError("Введите положительную сумму, которую получили в кассу.")
    except ValueError as error:
        await message.reply_text(f"Не удалось распознать сумму: {error}")
        raise ApplicationHandlerStop

    repo: OrderRepository = context.application.bot_data["repo"]
    updated = repo.correct_cash_handover(
        int(pending["entry_id"]),
        expected_revision=int(pending["revision"]),
        usd=parsed.usd,
        uzs=parsed.uzs,
        actor_id=user.id,
        actor_name=_name(user),
    )
    context.user_data.pop("cash_correction", None)
    if not updated:
        await message.reply_text("Операция уже обработана или сумму изменил другой администратор.")
        raise ApplicationHandlerStop
    await _refresh_cash_notification(context, updated)
    await message.reply_text(
        f"✅ Сумма кассы №K-{updated.id} изменена. Теперь подтвердите её кнопкой «Получил»."
    )
    raise ApplicationHandlerStop
