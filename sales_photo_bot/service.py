from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Protocol

from telegram import Bot, InputMediaPhoto, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ContextTypes

from .auto_correction import (
    EVENT_QUIET_PERIOD_SECONDS,
    latest_auto_correction_slot,
    next_auto_correction_slot,
)
from .config import Settings
from .dates import (
    TASHKENT_TZ,
    extract_sale_date,
    normalize_card_sale_date,
    remove_sale_date,
    tashkent_today,
)
from .formatting import build_caption
from .keyboards import (
    BACK_CALLBACK,
    MANAGER_CALLBACK_PREFIX,
    SELLER_BY_KEY,
    back_keyboard,
    manager_keyboard,
)
from .models import EMPTY_IDENTIFIERS, ProductIdentifiers
from .orders import card_order_id, ensure_card_order_id
from .phones import extract_product_label, normalize_caption_phone_field
from .prices import normalize_card_prices
from .repository import SalesPhotoRepository, utc_now


logger = logging.getLogger(__name__)
UTC = timezone.utc
BOT_CARD_MARKER = "\u2063\u2063"
CAPTION_REVISION_TTL_SECONDS = 600.0
CAPTION_REVISION_LIMIT = 4096
PROCESSING_STALE_SECONDS = 180
ALBUM_SETTLE_SECONDS = 1.0
ORDER_AUDIT_INTERVAL_SECONDS = 60.0
AUTO_CORRECTION_RETRY_SECONDS = 60.0
TEXT_SOURCE_FILE_ID = "sales-photo:text"
_SOURCE_CALLBACK_RE = re.compile(
    r"^sp:(?:m:[a-z]+|b):(\d+):(\d+):([0-9a-f]{12})$"
)


@dataclass(frozen=True)
class _PhotoClaim:
    chat_id: int
    source_message_id: int
    source_message_ids: tuple[int, ...]
    file_ids: tuple[str, ...]
    source_kind: str
    client_caption: str | None
    message_thread_id: int | None
    sale_date: date

    @property
    def file_id(self) -> str:
        return self.file_ids[0]


@dataclass(frozen=True)
class _CardNormalization:
    body: str
    entities: tuple[Any, ...]
    changed: bool = False


def _normalize_card_fields(
    body: str,
    entities: tuple[Any, ...],
    *,
    sale_date: date,
    order_id: int,
    max_length: int,
) -> _CardNormalization:
    dated = normalize_card_sale_date(
        body,
        entities,
        sale_date,
        max_length=max_length,
    )
    ordered = ensure_card_order_id(
        dated.body,
        dated.entities,
        order_id,
        max_length=max_length,
    )
    phoned = normalize_caption_phone_field(
        ordered.body,
        ordered.entities,
        max_length=max_length,
    )
    priced = normalize_card_prices(
        phoned.caption,
        phoned.entities,
        max_length=max_length,
    )
    return _CardNormalization(
        priced.body,
        priced.entities,
        dated.changed or ordered.changed or phoned.changed or priced.changed,
    )


class _RetryWaitCancelled(asyncio.CancelledError):
    """Cancellation while Telegram has definitively rejected the last send."""


class IdentifierRecognizer(Protocol):
    """Optional test hook; production does not configure image recognition."""

    async def recognize(
        self, image_bytes: bytes, mime_type: str
    ) -> ProductIdentifiers: ...


def _error_code(error: BaseException) -> str:
    return type(error).__name__[:80]


def _already_deleted(error: BaseException) -> bool:
    if not isinstance(error, BadRequest):
        return False
    message = str(error).casefold()
    return "message to delete not found" in message or "message_id_invalid" in message


def _message_to_forward_missing(error: BaseException) -> bool:
    return isinstance(error, BadRequest) and (
        "message to forward not found" in str(error).casefold()
    )


def _message_to_edit_missing(error: BaseException) -> bool:
    if not isinstance(error, BadRequest):
        return False
    message = str(error).casefold()
    return "message to edit not found" in message or "message_id_invalid" in message


def _chat_id(message: object) -> int | None:
    value = getattr(message, "chat_id", None)
    if value is None:
        chat = getattr(message, "chat", None)
        value = getattr(chat, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _message_content(
    message: object,
) -> tuple[str, str, tuple[Any, ...]] | None:
    caption = getattr(message, "caption", None)
    if caption is not None:
        return (
            "caption",
            str(caption),
            tuple(getattr(message, "caption_entities", None) or ()),
        )
    text = getattr(message, "text", None)
    if text is not None:
        return (
            "text",
            str(text),
            tuple(getattr(message, "entities", None) or ()),
        )
    return None


def _callback_source_claim(data: object) -> tuple[int, int, str] | None:
    match = _SOURCE_CALLBACK_RE.fullmatch(str(data or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), match.group(3)


def _source_claim_from_markup(message: object) -> tuple[int, int, str] | None:
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or ()
    for row in rows:
        for button in row:
            claim = _callback_source_claim(getattr(button, "callback_data", None))
            if claim is not None:
                return claim
    return None


def _selected_manager_from_markup(message: object) -> str | None:
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or ()
    for row in rows:
        for button in row:
            data = str(getattr(button, "callback_data", "") or "")
            text = str(getattr(button, "text", "") or "")
            if not data.startswith(f"{BACK_CALLBACK}:"):
                continue
            for manager in SELLER_BY_KEY.values():
                if f"👤 {manager}" in text:
                    return manager
    return None


def _retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    else:
        seconds = float(value)
    return min(86_400.0, max(0.1, seconds))


def _recent_sale_window(reference: date | None = None) -> tuple[date, date]:
    end = reference or tashkent_today()
    return end - timedelta(days=2), end


class SalesPhotoService:
    def __init__(
        self,
        settings: Settings,
        repository: SalesPhotoRepository,
        recognizer: IdentifierRecognizer | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.recognizer = recognizer
        self.bot_id: int | None = None
        self._photo_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._photo_lock_users: dict[tuple[int, int], int] = {}
        self._card_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._card_lock_users: dict[tuple[int, int], int] = {}
        self._caption_update_ids: OrderedDict[
            tuple[int, int], tuple[int, float]
        ] = OrderedDict()
        self._pending_managers: dict[tuple[int, int], str] = {}
        self._ui_generations: dict[tuple[int, int], int] = {}
        self._photo_tasks: set[asyncio.Task[None]] = set()
        self._album_items: dict[tuple[int, str], dict[int, Any]] = {}
        self._album_bots: dict[tuple[int, str], Any] = {}
        self._album_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._active_sources: set[tuple[int, int]] = set()
        self._cancelled_sources: set[tuple[int, int]] = set()
        self._order_backfill_forward_sources: dict[int, float] = {}
        self._maintenance_forward_message_ids: dict[int, float] = {}
        self._order_audit_lock = asyncio.Lock()
        self._auto_correction_lock = asyncio.Lock()
        self._auto_correction_wake = asyncio.Event()
        self._last_order_audit = 0.0
        self._startup_gate_active = False
        self._maintenance_task: asyncio.Task[None] | None = None
        self._auto_correction_task: asyncio.Task[None] | None = None

    async def preflight(self, bot: Bot) -> int:
        me = await bot.get_me()
        self.bot_id = int(me.id)
        chat = await bot.get_chat(self.settings.chat_id)
        actual_id = int(chat.id)
        if actual_id != self.settings.chat_id:
            raise RuntimeError("Telegram вернул другой ID целевого чата")
        chat_type = str(chat.type)
        if chat_type != str(ChatType.CHANNEL):
            raise RuntimeError(
                "Целевой Telegram-чат должен быть каналом: в группе сотрудники "
                "не смогут редактировать финансовые поля сообщения бота"
            )

        membership = await bot.get_chat_member(self.settings.chat_id, self.bot_id)
        status = str(getattr(membership, "status", ""))
        if status not in {"administrator", "creator", "owner"}:
            raise RuntimeError("Бот должен быть администратором целевого чата")
        if status not in {"creator", "owner"} and not bool(
            getattr(membership, "can_delete_messages", False)
        ):
            raise RuntimeError("Боту требуется право удаления сообщений")
        if chat_type == str(ChatType.CHANNEL) and status not in {"creator", "owner"}:
            if not bool(getattr(membership, "can_post_messages", False)):
                raise RuntimeError("Боту требуется право публикации в канале")
        provider_preflight = getattr(self.recognizer, "preflight", None)
        if provider_preflight is not None:
            await provider_preflight()
        logger.info("sales_photo_preflight_ok chat_id=%s chat_type=%s", actual_id, chat_type)
        return self.bot_id

    async def on_photo(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        if message is None:
            return
        if self._is_order_backfill_forward(message):
            chat_id = _chat_id(message)
            message_id = getattr(message, "message_id", None)
            if chat_id is not None and message_id is not None:
                await self._delete_duplicate(
                    context.bot,
                    chat_id,
                    int(message_id),
                )
            return
        if str(getattr(message, "caption", "") or "").startswith(
            BOT_CARD_MARKER
        ):
            await self._reconcile_generated_post(
                message,
                context.bot,
                allow_source_delete=not self._startup_gate_active,
            )
            return
        media_group_id = str(getattr(message, "media_group_id", "") or "")
        if media_group_id:
            self._queue_album(message, context.bot, media_group_id)
            return
        claim = self._claim_photo(message)
        if claim is None:
            return
        self._schedule_claim(claim, context.bot)

    async def on_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        if message is None:
            return
        content = _message_content(message)
        if content is None:
            return
        _, body, _ = content
        if self._is_order_backfill_forward(message):
            chat_id = _chat_id(message)
            message_id = getattr(message, "message_id", None)
            if chat_id is not None and message_id is not None:
                await self._delete_duplicate(
                    context.bot,
                    chat_id,
                    int(message_id),
                )
            return
        if body.startswith(BOT_CARD_MARKER):
            await self._reconcile_generated_post(
                message,
                context.bot,
                allow_source_delete=not self._startup_gate_active,
            )
            return
        claim = self._claim_text(message)
        if claim is not None:
            self._schedule_claim(claim, context.bot)

    def _schedule_claim(self, claim: _PhotoClaim, bot: Bot | Any) -> None:
        source_key = (claim.chat_id, claim.source_message_id)
        if self._startup_gate_active:
            try:
                self.repository.mark_failed(
                    claim.chat_id,
                    claim.source_message_id,
                    "startup_drain",
                    # Maintenance itself is gated, so make this safe deferred
                    # item immediately eligible when the gate finally opens.
                    at=utc_now()
                    - timedelta(seconds=self.settings.delete_retry_seconds),
                )
            except Exception as exc:
                logger.warning(
                    "sales_photo_startup_defer_failed chat_id=%s "
                    "source_message_id=%s error_type=%s",
                    claim.chat_id,
                    claim.source_message_id,
                    _error_code(exc),
                )
            self._active_sources.discard(source_key)
            return
        for finished in tuple(self._photo_tasks):
            if finished.done():
                self._photo_tasks.discard(finished)
        if len(self._photo_tasks) >= self.settings.concurrent_updates:
            self.repository.mark_failed(
                claim.chat_id,
                claim.source_message_id,
                "worker_capacity",
            )
            self._active_sources.discard(source_key)
            return
        task = asyncio.create_task(
            self._run_photo_claim(claim, bot),
            name=f"sales-photo-{claim.source_message_id}",
        )
        self._photo_tasks.add(task)
        task.add_done_callback(
            lambda finished, key=source_key: self._photo_task_done(
                finished,
                key,
            )
        )

    @staticmethod
    def _forward_origin_message_id(message: object) -> int | None:
        origin = getattr(message, "forward_origin", None)
        value = getattr(origin, "message_id", None)
        if value is None:
            value = getattr(message, "forward_from_message_id", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _is_order_backfill_forward(self, message: object) -> bool:
        now = time.monotonic()
        for source_id, expires_at in tuple(
            self._order_backfill_forward_sources.items()
        ):
            if expires_at < now:
                self._order_backfill_forward_sources.pop(source_id, None)
        for message_id, expires_at in tuple(
            self._maintenance_forward_message_ids.items()
        ):
            if expires_at < now:
                self._maintenance_forward_message_ids.pop(message_id, None)
        message_id = getattr(message, "message_id", None)
        try:
            exact_message_id = int(message_id) if message_id is not None else None
        except (TypeError, ValueError):
            exact_message_id = None
        if (
            exact_message_id is not None
            and exact_message_id in self._maintenance_forward_message_ids
        ):
            self._maintenance_forward_message_ids.pop(exact_message_id, None)
            return True
        source_message_id = self._forward_origin_message_id(message)
        return bool(
            source_message_id is not None
            and source_message_id in self._order_backfill_forward_sources
        )

    def _track_maintenance_forward(
        self,
        source_message_id: int,
        forwarded: object,
    ) -> None:
        temporary_message_id = getattr(forwarded, "message_id", None)
        try:
            message_id = (
                int(temporary_message_id)
                if temporary_message_id is not None
                else None
            )
        except (TypeError, ValueError):
            message_id = None
        if message_id is not None:
            self._maintenance_forward_message_ids[message_id] = (
                time.monotonic() + 600
            )
        self._order_backfill_forward_sources.pop(
            int(source_message_id),
            None,
        )

    def _queue_album(
        self,
        message: Message | Any,
        bot: Bot | Any,
        media_group_id: str,
    ) -> None:
        chat_id = _chat_id(message)
        message_id = getattr(message, "message_id", None)
        if chat_id != self.settings.chat_id or message_id is None:
            return
        key = (chat_id, str(media_group_id))
        self._album_items.setdefault(key, {})[int(message_id)] = message
        self._album_bots[key] = bot
        task = self._album_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._drain_album(key),
                name=f"sales-photo-album-{media_group_id}",
            )
            self._album_tasks[key] = task

    async def _drain_album(self, key: tuple[int, str]) -> None:
        try:
            previous_size = -1
            while True:
                current_size = len(self._album_items.get(key, {}))
                if current_size == previous_size:
                    break
                previous_size = current_size
                await asyncio.sleep(ALBUM_SETTLE_SECONDS)
            messages = tuple(
                message
                for _, message in sorted(
                    self._album_items.pop(key, {}).items()
                )
            )
            bot = self._album_bots.pop(key, None)
            if messages and bot is not None:
                claim = self._claim_album(messages, key[1])
                if claim is not None:
                    self._schedule_claim(claim, bot)
        finally:
            self._album_items.pop(key, None)
            self._album_bots.pop(key, None)
            self._album_tasks.pop(key, None)

    def _photo_task_done(
        self,
        task: asyncio.Task[None],
        source_key: tuple[int, int] | None,
    ) -> None:
        self._photo_tasks.discard(task)
        if source_key is not None:
            self._active_sources.discard(source_key)
            self._cancelled_sources.discard(source_key)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "sales_photo_background_failed error_type=%s",
                _error_code(error),
            )

    async def on_edited_photo(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.edited_channel_post
        if message is None:
            return
        await self.handle_edited_photo(
            message,
            context.bot,
            update_id=getattr(update, "update_id", None),
        )

    @asynccontextmanager
    async def _card_lock(self, key: tuple[int, int]):
        lock = self._card_locks.setdefault(key, asyncio.Lock())
        self._card_lock_users[key] = self._card_lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._card_lock_users.get(key, 1) - 1
            if remaining <= 0:
                self._card_lock_users.pop(key, None)
                if self._card_locks.get(key) is lock:
                    self._card_locks.pop(key, None)
            else:
                self._card_lock_users[key] = remaining

    def _register_caption_revision(
        self,
        key: tuple[int, int],
        revision: int,
    ) -> bool:
        """Keep a short-lived per-card ordering watermark for edited posts."""

        now = time.monotonic()
        cutoff = now - CAPTION_REVISION_TTL_SECONDS
        for candidate, (_, seen_at) in tuple(self._caption_update_ids.items()):
            if seen_at >= cutoff or self._card_lock_users.get(candidate, 0):
                continue
            self._caption_update_ids.pop(candidate, None)

        previous = self._caption_update_ids.get(key)
        if previous is not None and previous[1] >= cutoff:
            previous_revision, _ = previous
            if revision <= previous_revision:
                self._caption_update_ids.move_to_end(key)
                return False

        self._caption_update_ids[key] = (revision, now)
        self._caption_update_ids.move_to_end(key)
        while len(self._caption_update_ids) > CAPTION_REVISION_LIMIT:
            evicted = False
            for candidate in tuple(self._caption_update_ids):
                if candidate == key or self._card_lock_users.get(candidate, 0):
                    continue
                self._caption_update_ids.pop(candidate, None)
                evicted = True
                break
            if not evicted:
                break
        return True

    async def handle_edited_photo(
        self,
        message: Message | Any,
        bot: Bot | Any,
        update_id: object = None,
    ) -> None:
        chat_id = _chat_id(message)
        message_id = getattr(message, "message_id", None)
        if chat_id != self.settings.chat_id or message_id is None:
            return
        message_id = int(message_id)
        try:
            primary_source_id = (
                self.repository.primary_source_for_member(chat_id, message_id)
                or message_id
            )
        except Exception:
            primary_source_id = message_id
        source_key = (chat_id, primary_source_id)
        try:
            is_replacement = self.repository.is_replacement(chat_id, message_id)
        except Exception as exc:
            if source_key in self._active_sources:
                self._cancelled_sources.add(source_key)
            logger.warning(
                "sales_photo_edit_classify_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        if not is_replacement:
            try:
                cancelled, replacement_id = self.repository.cancel_edited_source(
                    chat_id,
                    primary_source_id,
                )
            except Exception as exc:
                logger.warning(
                    "sales_photo_source_edit_quarantine_failed chat_id=%s "
                    "message_id=%s error_type=%s",
                    chat_id,
                    message_id,
                    _error_code(exc),
                )
                if source_key in self._active_sources:
                    self._cancelled_sources.add(source_key)
                return
            if cancelled:
                self._cancelled_sources.add(source_key)
                logger.info(
                    "sales_photo_source_edit_quarantined chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
                if replacement_id is not None:
                    output_ids = self.repository.output_message_ids(
                        chat_id,
                        primary_source_id,
                    ) or (replacement_id,)
                    await self._delete_known_outputs(bot, chat_id, output_ids)
            elif source_key in self._active_sources:
                self._cancelled_sources.add(source_key)
            return

        content = _message_content(message)
        if content is None:
            return
        content_kind, body, entities = content

        key = (chat_id, message_id)
        logger.info(
            "sales_photo_card_edit_received chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )
        try:
            revision = int(update_id) if update_id is not None else None
        except (TypeError, ValueError):
            revision = None
        if revision is not None and not self._register_caption_revision(
            key, revision
        ):
            return

        async with self._card_lock(key):
            if revision is not None:
                latest = self._caption_update_ids.get(key)
                if latest is not None and revision < latest[0]:
                    return
            daily_order = self.repository.daily_order_for_replacement(
                chat_id,
                message_id,
            )
            max_length = 1024 if content_kind == "caption" else 4096
            if daily_order is not None:
                final = _normalize_card_fields(
                    body,
                    entities,
                    sale_date=daily_order[0],
                    order_id=daily_order[1],
                    max_length=max_length,
                )
            else:
                phoned = normalize_caption_phone_field(
                    body,
                    entities,
                    max_length=max_length,
                )
                priced = normalize_card_prices(
                    phoned.caption,
                    phoned.entities,
                    max_length=max_length,
                )
                final = _CardNormalization(
                    priced.body,
                    priced.entities,
                    phoned.changed or priced.changed,
                )
            try:
                edit_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": self._current_card_markup(
                        chat_id,
                        message_id,
                        message,
                    ),
                }
                if content_kind == "caption":
                    edit_kwargs.update(
                        caption=final.body,
                        caption_entities=final.entities,
                    )
                    await bot.edit_message_caption(**edit_kwargs)
                else:
                    edit_kwargs.update(
                        text=final.body,
                        entities=final.entities,
                    )
                    await bot.edit_message_text(**edit_kwargs)
                if daily_order is not None:
                    self._mark_order_applied_for_replacement(
                        chat_id,
                        message_id,
                    )
                self._mark_price_applied_for_replacement(chat_id, message_id)
                if final.changed:
                    logger.info(
                        "sales_photo_card_normalized_from_edit chat_id=%s "
                        "message_id=%s",
                        chat_id,
                        message_id,
                    )
            except asyncio.CancelledError:
                raise
            except BadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    if daily_order is not None and card_order_id(body) == daily_order[1]:
                        self._mark_order_applied_for_replacement(
                            chat_id,
                            message_id,
                        )
                    self._mark_price_applied_for_replacement(chat_id, message_id)
                    return
                logger.warning(
                    "sales_photo_phone_normalize_failed chat_id=%s message_id=%s "
                    "error_type=%s",
                    chat_id,
                    message_id,
                    _error_code(exc),
                )
            except Exception as exc:
                # A transport error is ambiguous; do not retry a full caption
                # replacement because a newer manual edit may already exist.
                logger.warning(
                    "sales_photo_phone_normalize_failed chat_id=%s message_id=%s "
                    "error_type=%s",
                    chat_id,
                    message_id,
                    _error_code(exc),
                )

    def _current_card_markup(
        self,
        chat_id: int,
        message_id: int,
        message: object,
    ):
        source_message_id = self.repository.source_for_replacement(
            chat_id, message_id
        )
        generation = self.repository.ui_generation_for_replacement(
            chat_id, message_id
        )
        if source_message_id is None or generation is None:
            return getattr(message, "reply_markup", None)
        signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            generation,
        )
        if generation % 2:
            manager = (
                self.repository.selected_manager(chat_id, message_id)
                or self._pending_managers.get((chat_id, message_id))
                or _selected_manager_from_markup(message)
            )
            return back_keyboard(
                source_message_id=source_message_id,
                generation=generation,
                signature=signature,
                manager=manager,
            )
        return manager_keyboard(
            source_message_id=source_message_id,
            generation=generation,
            signature=signature,
        )

    def _mark_order_applied_for_replacement(
        self,
        chat_id: int,
        replacement_message_id: int,
    ) -> None:
        try:
            source_id = self.repository.source_for_replacement(
                chat_id,
                replacement_message_id,
            )
            if source_id is not None:
                self.repository.mark_order_card_applied(chat_id, source_id)
        except Exception as exc:
            logger.warning(
                "sales_photo_order_applied_commit_failed chat_id=%s "
                "message_id=%s error_type=%s",
                chat_id,
                replacement_message_id,
                _error_code(exc),
            )

    def _mark_price_applied_for_replacement(
        self,
        chat_id: int,
        replacement_message_id: int,
    ) -> None:
        try:
            source_id = self.repository.source_for_replacement(
                chat_id,
                replacement_message_id,
            )
            if source_id is not None:
                self.repository.mark_price_card_applied(chat_id, source_id)
        except Exception as exc:
            logger.warning(
                "sales_photo_price_applied_commit_failed chat_id=%s "
                "message_id=%s error_type=%s",
                chat_id,
                replacement_message_id,
                _error_code(exc),
            )

    def _claim_photo(self, message: Message | Any) -> _PhotoClaim | None:
        chat_id = _chat_id(message)
        source_message_id = getattr(message, "message_id", None)
        photos = tuple(getattr(message, "photo", None) or ())
        if chat_id != self.settings.chat_id or source_message_id is None or not photos:
            return None
        source_message_id = int(source_message_id)
        if self.repository.is_replacement(chat_id, source_message_id):
            return None
        sender = getattr(message, "from_user", None)
        if bool(getattr(sender, "is_bot", False)):
            return None
        if self.bot_id is not None and getattr(sender, "id", None) == self.bot_id:
            return None

        photo = max(
            photos,
            key=lambda item: (
                int(getattr(item, "file_size", 0) or 0),
                int(getattr(item, "width", 0) or 0)
                * int(getattr(item, "height", 0) or 0),
            ),
        )
        file_id = str(getattr(photo, "file_id", "") or "")
        file_unique_id = str(getattr(photo, "file_unique_id", "") or file_id)
        if not file_id:
            return None
        client_caption = getattr(message, "caption", None)
        sale_date_match = extract_sale_date(client_caption)
        effective_sale_date = (
            sale_date_match.value if sale_date_match else tashkent_today()
        )

        key = (chat_id, source_message_id)
        if key in self._cancelled_sources:
            return None
        if not self.repository.claim_photo(
            chat_id,
            source_message_id,
            file_unique_id,
            source_file_id=file_id,
            client_caption=client_caption,
            message_thread_id=getattr(message, "message_thread_id", None),
            sale_date=effective_sale_date,
            allocate_order=False,
        ):
            return None
        self._active_sources.add(key)
        return _PhotoClaim(
            chat_id=chat_id,
            source_message_id=source_message_id,
            source_message_ids=(source_message_id,),
            file_ids=(file_id,),
            source_kind="photo",
            client_caption=client_caption,
            message_thread_id=getattr(message, "message_thread_id", None),
            sale_date=effective_sale_date,
        )

    def _claim_text(self, message: Message | Any) -> _PhotoClaim | None:
        chat_id = _chat_id(message)
        source_message_id = getattr(message, "message_id", None)
        text = str(getattr(message, "text", "") or "").strip()
        sale_date = extract_sale_date(text)
        product_text = remove_sale_date(text, sale_date)
        effective_sale_date = sale_date.value if sale_date else tashkent_today()
        if (
            chat_id != self.settings.chat_id
            or source_message_id is None
            or not text
            or extract_product_label(product_text) is None
        ):
            return None
        source_message_id = int(source_message_id)
        if self.repository.is_replacement(chat_id, source_message_id):
            return None
        sender = getattr(message, "from_user", None)
        if bool(getattr(sender, "is_bot", False)):
            return None
        if self.bot_id is not None and getattr(sender, "id", None) == self.bot_id:
            return None

        key = (chat_id, source_message_id)
        if key in self._cancelled_sources:
            return None
        unique_id = "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        if not self.repository.claim_photo(
            chat_id,
            source_message_id,
            unique_id,
            source_file_id=TEXT_SOURCE_FILE_ID,
            client_caption=text,
            message_thread_id=getattr(message, "message_thread_id", None),
            source_message_ids=(source_message_id,),
            source_kind="text",
            sale_date=effective_sale_date,
            allocate_order=False,
        ):
            return None
        self._active_sources.add(key)
        return _PhotoClaim(
            chat_id=chat_id,
            source_message_id=source_message_id,
            source_message_ids=(source_message_id,),
            file_ids=(TEXT_SOURCE_FILE_ID,),
            source_kind="text",
            client_caption=text,
            message_thread_id=getattr(message, "message_thread_id", None),
            sale_date=effective_sale_date,
        )

    def _claim_album(
        self,
        messages: tuple[Message | Any, ...],
        media_group_id: str,
    ) -> _PhotoClaim | None:
        ordered = tuple(
            sorted(
                messages,
                key=lambda item: int(getattr(item, "message_id", 0) or 0),
            )
        )
        if len(ordered) == 1:
            return self._claim_photo(ordered[0])
        if not 2 <= len(ordered) <= 10:
            return None

        chat_id = _chat_id(ordered[0])
        source_message_ids: list[int] = []
        file_ids: list[str] = []
        unique_ids: list[str] = []
        client_caption: str | None = None
        for message in ordered:
            message_id = getattr(message, "message_id", None)
            photos = tuple(getattr(message, "photo", None) or ())
            if chat_id != self.settings.chat_id or message_id is None or not photos:
                return None
            if _chat_id(message) != chat_id:
                return None
            sender = getattr(message, "from_user", None)
            if bool(getattr(sender, "is_bot", False)):
                return None
            if self.bot_id is not None and getattr(sender, "id", None) == self.bot_id:
                return None
            photo = max(
                photos,
                key=lambda item: (
                    int(getattr(item, "file_size", 0) or 0),
                    int(getattr(item, "width", 0) or 0)
                    * int(getattr(item, "height", 0) or 0),
                ),
            )
            file_id = str(getattr(photo, "file_id", "") or "")
            if not file_id:
                return None
            source_message_ids.append(int(message_id))
            file_ids.append(file_id)
            unique_ids.append(
                str(getattr(photo, "file_unique_id", "") or file_id)
            )
            candidate_caption = getattr(message, "caption", None)
            if client_caption is None and candidate_caption:
                client_caption = str(candidate_caption)

        source_message_id = source_message_ids[0]
        sale_date_match = extract_sale_date(client_caption)
        effective_sale_date = (
            sale_date_match.value if sale_date_match else tashkent_today()
        )
        key = (chat_id, source_message_id)
        if key in self._cancelled_sources:
            return None
        unique_material = "\x1f".join(
            (str(media_group_id), *unique_ids)
        ).encode("utf-8")
        unique_id = "album:" + hashlib.sha256(unique_material).hexdigest()[:32]
        if not self.repository.claim_photo(
            chat_id,
            source_message_id,
            unique_id,
            source_file_id=file_ids[0],
            source_file_ids=tuple(file_ids),
            source_message_ids=tuple(source_message_ids),
            source_kind="album",
            client_caption=client_caption,
            message_thread_id=getattr(ordered[0], "message_thread_id", None),
            sale_date=effective_sale_date,
            allocate_order=False,
        ):
            return None
        self._active_sources.add(key)
        return _PhotoClaim(
            chat_id=chat_id,
            source_message_id=source_message_id,
            source_message_ids=tuple(source_message_ids),
            file_ids=tuple(file_ids),
            source_kind="album",
            client_caption=client_caption,
            message_thread_id=getattr(ordered[0], "message_thread_id", None),
            sale_date=effective_sale_date,
        )

    async def _run_photo_claim(
        self,
        claim: _PhotoClaim,
        bot: Bot | Any,
    ) -> None:
        key = (claim.chat_id, claim.source_message_id)
        lock = self._photo_locks.setdefault(key, asyncio.Lock())
        self._photo_lock_users[key] = self._photo_lock_users.get(key, 0) + 1
        try:
            async with lock:
                if key in self._cancelled_sources:
                    try:
                        self.repository.preserve_after_source_edit(*key)
                    except Exception:
                        pass
                    return
                await self._process_claimed_photo(
                    bot,
                    claim,
                )
        finally:
            remaining = self._photo_lock_users.get(key, 1) - 1
            if remaining <= 0:
                self._photo_lock_users.pop(key, None)
                if self._photo_locks.get(key) is lock:
                    self._photo_locks.pop(key, None)
            else:
                self._photo_lock_users[key] = remaining

    async def handle_photo(self, message: Message | Any, bot: Bot | Any) -> None:
        if str(getattr(message, "caption", "") or "").startswith(
            BOT_CARD_MARKER
        ):
            await self._reconcile_generated_post(message, bot)
            return
        claim = self._claim_photo(message)
        if claim is None:
            return
        try:
            await self._run_photo_claim(claim, bot)
        finally:
            key = (claim.chat_id, claim.source_message_id)
            self._active_sources.discard(key)
            self._cancelled_sources.discard(key)

    async def _process_claimed_photo(
        self,
        bot: Bot | Any,
        claim: _PhotoClaim,
    ) -> None:
        chat_id = claim.chat_id
        source_message_id = claim.source_message_id
        file_id = claim.file_id
        client_caption = claim.client_caption
        message_thread_id = claim.message_thread_id
        started = time.monotonic()
        logger.info(
            "sales_photo_processing_started chat_id=%s source_message_id=%s",
            chat_id,
            source_message_id,
        )
        if claim.source_kind == "text" or self.recognizer is None:
            identifiers = EMPTY_IDENTIFIERS
            logger.info(
                "sales_photo_content_passthrough chat_id=%s source_message_id=%s "
                "source_kind=%s",
                chat_id,
                source_message_id,
                claim.source_kind,
            )
        else:
            try:
                identifiers = await self._run_optional_recognizer(file_id, bot)
            except asyncio.CancelledError:
                try:
                    self.repository.mark_failed(
                        chat_id,
                        source_message_id,
                        "cancelled_before_send",
                    )
                except Exception:
                    pass
                raise
        identifier_count = sum(
            value is not None
            for value in (
                identifiers.imei,
                identifiers.imei2,
                identifiers.serial_number,
            )
        )
        key = (chat_id, source_message_id)
        if key in self._cancelled_sources:
            try:
                self.repository.preserve_after_source_edit(*key)
            except Exception:
                pass
            return
        try:
            if not self.repository.source_accepts_replacement(
                chat_id,
                source_message_id,
            ):
                logger.info(
                    "sales_photo_processing_cancelled chat_id=%s "
                    "source_message_id=%s",
                    chat_id,
                    source_message_id,
                )
                return
        except Exception as exc:
            logger.warning(
                "sales_photo_publish_guard_failed chat_id=%s source_message_id=%s "
                "error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return
        try:
            # Do not scan existing cards in the publish path. The requested
            # trailing debounce must reset for every new model and start one
            # consolidated repair only after five quiet minutes.
            _, order_id = self.repository.ensure_daily_order(
                chat_id,
                source_message_id,
                claim.sale_date,
            )
        except Exception as exc:
            try:
                self.repository.mark_failed(
                    chat_id,
                    source_message_id,
                    "order_assignment_failed",
                )
            except Exception:
                pass
            logger.error(
                "sales_photo_order_assignment_failed chat_id=%s "
                "source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return
        sale_date_match = extract_sale_date(client_caption)
        cleaned_caption = remove_sale_date(client_caption, sale_date_match)
        product_label = (
            extract_product_label(cleaned_caption)
            if claim.source_kind == "text"
            else None
        )
        caption = BOT_CARD_MARKER + build_caption(
            cleaned_caption,
            identifiers,
            product_label=product_label,
            sale_date=(sale_date_match.value if sale_date_match else None),
            order_id=order_id,
        )
        initial_generation = 0
        source_signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            initial_generation,
        )
        reply_markup = manager_keyboard(
            source_message_id=source_message_id,
            generation=initial_generation,
            signature=source_signature,
        )

        if key in self._cancelled_sources:
            try:
                self.repository.preserve_after_source_edit(*key)
            except Exception:
                pass
            return
        try:
            if not self.repository.mark_send_started(chat_id, source_message_id):
                return
        except Exception as exc:
            logger.warning(
                "sales_photo_send_guard_failed chat_id=%s source_message_id=%s "
                "error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return

        known_output_ids: tuple[int, ...] = ()
        try:
            if claim.source_kind == "album":
                media_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "media": tuple(
                        InputMediaPhoto(
                            media=album_file_id,
                            caption=BOT_CARD_MARKER,
                        )
                        for album_file_id in claim.file_ids
                    ),
                }
                if message_thread_id is not None:
                    media_kwargs["message_thread_id"] = int(message_thread_id)
                album_messages = await self._send_media_group(bot, media_kwargs)
                known_output_ids = tuple(
                    int(message.message_id) for message in album_messages
                )
                card_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": caption,
                    "parse_mode": ParseMode.HTML,
                    "reply_markup": reply_markup,
                }
                if message_thread_id is not None:
                    card_kwargs["message_thread_id"] = int(message_thread_id)
                replacement = await self._send_message(bot, card_kwargs)
            elif claim.source_kind == "text":
                text_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": caption,
                    "parse_mode": ParseMode.HTML,
                    "reply_markup": reply_markup,
                }
                if message_thread_id is not None:
                    text_kwargs["message_thread_id"] = int(message_thread_id)
                replacement = await self._send_message(bot, text_kwargs)
            else:
                photo_kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "photo": file_id,
                    "caption": caption,
                    "parse_mode": ParseMode.HTML,
                    "reply_markup": reply_markup,
                }
                if message_thread_id is not None:
                    photo_kwargs["message_thread_id"] = int(message_thread_id)
                replacement = await self._send_photo(bot, photo_kwargs)
        except _RetryWaitCancelled:
            cleanup_complete = await self._delete_known_outputs(
                bot,
                chat_id,
                known_output_ids,
            )
            try:
                if cleanup_complete:
                    self.repository.mark_send_rejected(
                        chat_id,
                        source_message_id,
                        "cancelled_retry_wait",
                    )
                else:
                    self.repository.mark_ambiguous_send(
                        chat_id,
                        source_message_id,
                        "cancelled_retry_cleanup",
                    )
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            await self._delete_known_outputs(bot, chat_id, known_output_ids)
            try:
                self.repository.mark_ambiguous_send(
                    chat_id,
                    source_message_id,
                    "cancelled_during_send",
                )
            except Exception:
                pass
            raise
        except (RetryAfter, BadRequest) as exc:
            cleanup_complete = await self._delete_known_outputs(
                bot,
                chat_id,
                known_output_ids,
            )
            try:
                if cleanup_complete:
                    self.repository.mark_send_rejected(
                        chat_id,
                        source_message_id,
                        _error_code(exc),
                    )
                else:
                    self.repository.mark_ambiguous_send(
                        chat_id,
                        source_message_id,
                        "partial_output_cleanup",
                    )
            except Exception:
                pass
            logger.warning(
                "sales_photo_repost_deferred chat_id=%s source_message_id=%s "
                "error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return
        except Exception as exc:
            # A transport failure can mean that Telegram accepted the current
            # request but its response was lost. Never retry an ambiguous send.
            await self._delete_known_outputs(bot, chat_id, known_output_ids)
            self.repository.mark_ambiguous_send(
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            logger.error(
                "sales_photo_repost_failed chat_id=%s source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return

        replacement_message_id = int(replacement.message_id)
        output_message_ids = (*known_output_ids, replacement_message_id)
        try:
            outcome = self.repository.record_replacement(
                chat_id,
                source_message_id,
                replacement_message_id,
                output_message_ids=output_message_ids,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The database commit may have succeeded even though its outcome is
            # ambiguous. Never delete the new card in an unknown ledger state:
            # keeping both photos is recoverable, while deleting it could lose
            # the only canonical copy.
            try:
                self.repository.mark_ambiguous_send(
                    chat_id, source_message_id, _error_code(exc)
                )
            except Exception:
                pass
            logger.error(
                "sales_photo_ledger_failed chat_id=%s source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return

        if outcome == "conflict":
            await self._delete_known_outputs(bot, chat_id, output_message_ids)
            return
        if outcome == "missing":
            logger.error(
                "sales_photo_job_missing chat_id=%s source_message_id=%s "
                "replacement_message_id=%s action=kept",
                chat_id,
                source_message_id,
                replacement_message_id,
            )
            return

        if outcome == "recorded":
            self._auto_correction_wake.set()

        try:
            self.repository.mark_order_card_applied(chat_id, source_message_id)
            self.repository.mark_price_card_applied(chat_id, source_message_id)
        except Exception as exc:
            logger.warning(
                "sales_photo_card_applied_commit_failed chat_id=%s "
                "source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )

        logger.info(
            "sales_photo_reposted chat_id=%s source_message_id=%s "
            "replacement_message_id=%s identifiers=%s elapsed_ms=%s",
            chat_id,
            source_message_id,
            replacement_message_id,
            identifier_count,
            round((time.monotonic() - started) * 1000),
        )

        # Keep the original briefly so Telegram can dispatch a source edit that
        # happened while card creation or sendPhoto was running. on_photo runs this
        # work in a background task, leaving update-processor slots available.
        await asyncio.sleep(max(0, self.settings.source_edit_grace_seconds))
        if key in self._cancelled_sources:
            try:
                self.repository.preserve_after_source_edit(*key)
            except Exception:
                pass
            return
        if not self.repository.source_pending_deletion(
            chat_id,
            source_message_id,
            replacement_message_id,
        ):
            return
        await self._delete_source(
            bot,
            chat_id=chat_id,
            source_message_id=source_message_id,
        )

    async def _send_photo(self, bot: Bot | Any, kwargs: dict[str, Any]) -> Any:
        return await self._send_with_retry(bot.send_photo, kwargs)

    async def _send_message(self, bot: Bot | Any, kwargs: dict[str, Any]) -> Any:
        return await self._send_with_retry(bot.send_message, kwargs)

    async def _send_media_group(
        self,
        bot: Bot | Any,
        kwargs: dict[str, Any],
    ) -> tuple[Any, ...]:
        result = await self._send_with_retry(bot.send_media_group, kwargs)
        return tuple(result)

    async def _send_with_retry(
        self,
        sender: Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> Any:
        for attempt in range(2):
            try:
                return await sender(**kwargs)
            except asyncio.CancelledError:
                raise
            except RetryAfter as exc:
                if attempt:
                    raise
                retry_seconds = _retry_after_seconds(exc)
                if retry_seconds > 60:
                    raise
                try:
                    await asyncio.sleep(retry_seconds)
                except asyncio.CancelledError as cancelled:
                    raise _RetryWaitCancelled() from cancelled
        raise RuntimeError("unreachable send retry state")

    async def _reconcile_generated_post(
        self,
        message: Message | Any,
        bot: Bot | Any,
        *,
        allow_source_delete: bool = True,
    ) -> None:
        chat_id = _chat_id(message)
        replacement_message_id = getattr(message, "message_id", None)
        source_claim = _source_claim_from_markup(message)
        if (
            chat_id != self.settings.chat_id
            or replacement_message_id is None
            or source_claim is None
        ):
            return
        source_message_id, generation, signature = source_claim
        if generation != 0 or not self.repository.valid_callback_signature(
            chat_id, source_message_id, generation, signature
        ):
            logger.warning(
                "sales_photo_reconcile_signature_invalid chat_id=%s "
                "source_message_id=%s",
                chat_id,
                source_message_id,
            )
            return
        replacement_message_id = int(replacement_message_id)
        try:
            outcome = self.repository.record_replacement(
                chat_id,
                source_message_id,
                replacement_message_id,
            )
        except Exception as exc:
            logger.error(
                "sales_photo_reconcile_failed chat_id=%s source_message_id=%s "
                "replacement_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                replacement_message_id,
                _error_code(exc),
            )
            return
        if outcome == "conflict":
            await self._delete_duplicate(bot, chat_id, replacement_message_id)
            return
        if outcome == "missing":
            logger.error(
                "sales_photo_reconcile_missing chat_id=%s source_message_id=%s "
                "replacement_message_id=%s action=kept",
                chat_id,
                source_message_id,
                replacement_message_id,
            )
            return
        if outcome == "recorded":
            self._auto_correction_wake.set()
        try:
            self.repository.mark_order_card_applied(chat_id, source_message_id)
            self.repository.mark_price_card_applied(chat_id, source_message_id)
        except Exception as exc:
            logger.warning(
                "sales_photo_reconcile_card_commit_failed chat_id=%s "
                "source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
        if not allow_source_delete:
            logger.info(
                "sales_photo_reconcile_delete_deferred chat_id=%s "
                "source_message_id=%s",
                chat_id,
                source_message_id,
            )
            return
        await asyncio.sleep(max(0, self.settings.source_edit_grace_seconds))
        if not self.repository.source_pending_deletion(
            chat_id,
            source_message_id,
            replacement_message_id,
        ):
            return
        await self._delete_source(
            bot,
            chat_id=chat_id,
            source_message_id=source_message_id,
        )

    async def _run_optional_recognizer(
        self,
        file_id: str,
        bot: Bot | Any,
    ) -> ProductIdentifiers:
        recognizer = self.recognizer
        if recognizer is None:
            return EMPTY_IDENTIFIERS
        try:
            telegram_file = await bot.get_file(file_id)
            data = bytes(await telegram_file.download_as_bytearray())
            return await recognizer.recognize(data, "image/jpeg")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "sales_photo_optional_recognizer_failed error_type=%s",
                _error_code(exc),
            )
            return EMPTY_IDENTIFIERS

    async def _delete_source(
        self,
        bot: Bot | Any,
        chat_id: int,
        source_message_id: int,
    ) -> None:
        try:
            if not self.repository.begin_source_delete(
                chat_id,
                source_message_id,
            ):
                return
        except Exception as exc:
            logger.warning(
                "sales_photo_source_delete_guard_failed chat_id=%s "
                "source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return
        members = self.repository.pending_source_members(
            chat_id,
            source_message_id,
        )
        for member_message_id in members:
            try:
                await bot.delete_message(chat_id, member_message_id)
            except Exception as exc:
                if not _already_deleted(exc):
                    self.repository.mark_delete_pending(
                        chat_id,
                        source_message_id,
                        _error_code(exc),
                    )
                    logger.warning(
                        "sales_photo_source_delete_pending chat_id=%s "
                        "source_message_id=%s member_message_id=%s error_type=%s",
                        chat_id,
                        source_message_id,
                        member_message_id,
                        _error_code(exc),
                    )
                    continue
            self.repository.mark_source_member_deleted(
                chat_id,
                source_message_id,
                member_message_id,
            )
        if self.repository.pending_source_members(chat_id, source_message_id):
            return
        self.repository.mark_complete(chat_id, source_message_id)

    async def _delete_duplicate(
        self,
        bot: Bot | Any,
        chat_id: int,
        message_id: int,
    ) -> bool:
        # Persist intent before the Telegram side effect. A crash or failed delete
        # can then be retried without risking the canonical replacement.
        try:
            self.repository.queue_duplicate_cleanup(chat_id, message_id)
        except Exception as exc:
            logger.error(
                "sales_photo_duplicate_queue_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return False
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as exc:
            if _already_deleted(exc):
                try:
                    self.repository.complete_duplicate_cleanup(chat_id, message_id)
                except Exception:
                    pass
                return True
            try:
                self.repository.mark_duplicate_cleanup_failed(chat_id, message_id)
            except Exception:
                pass
            logger.warning(
                "sales_photo_duplicate_delete_pending chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return False
        try:
            self.repository.complete_duplicate_cleanup(chat_id, message_id)
        except Exception as exc:
            logger.warning(
                "sales_photo_duplicate_cleanup_commit_failed chat_id=%s "
                "message_id=%s error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return False
        return True

    async def _delete_known_outputs(
        self,
        bot: Bot | Any,
        chat_id: int,
        message_ids: tuple[int, ...],
    ) -> bool:
        outcomes = [
            await self._delete_duplicate(bot, chat_id, message_id)
            for message_id in message_ids
        ]
        return all(outcomes)

    async def on_manager_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        data = str(query.data or "")
        message = query.message
        chat_id = _chat_id(message) if message is not None else None
        message_id = getattr(message, "message_id", None) if message is not None else None
        actor_id = getattr(getattr(query, "from_user", None), "id", None)
        source_claim = _callback_source_claim(data)
        if (
            chat_id != self.settings.chat_id
            or message_id is None
            or source_claim is None
        ):
            await query.answer("Кнопка устарела", show_alert=True)
            return
        source_id, callback_generation, source_signature = source_claim
        if (
            not self.repository.valid_callback_signature(
                chat_id,
                source_id,
                callback_generation,
                source_signature,
            )
            or self.repository.source_for_replacement(
                chat_id,
                int(message_id),
            )
            != source_id
        ):
            await query.answer("Кнопка устарела", show_alert=True)
            return
        if not await self._manager_actor_allowed(context.bot, actor_id):
            await query.answer("У вас нет доступа к выбору менеджера", show_alert=True)
            return

        callback_key = (chat_id, int(message_id))
        async with self._card_lock(callback_key):
            ledger_generation = self.repository.ui_generation_for_replacement(
                chat_id,
                int(message_id),
            )
            known_generation = max(
                int(ledger_generation or 0),
                self._ui_generations.get(callback_key, 0),
            )
            is_back = data == BACK_CALLBACK or data.startswith(
                f"{BACK_CALLBACK}:"
            )
            if ledger_generation is None or is_back != bool(
                callback_generation % 2
            ):
                await query.answer("Кнопка устарела", show_alert=True)
                return
            if callback_generation < known_generation:
                try:
                    await self._edit_callback_markup(
                        query,
                        self._current_card_markup(
                            chat_id,
                            int(message_id),
                            message,
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "sales_photo_stale_keyboard_repair_failed chat_id=%s "
                        "message_id=%s error_type=%s",
                        chat_id,
                        message_id,
                        _error_code(exc),
                    )
                    await query.answer("Кнопка устарела", show_alert=True)
                    return
                await query.answer(
                    "Клавиатура обновлена. Нажмите ещё раз.",
                    show_alert=True,
                )
                return
            if is_back:
                await self._handle_back(
                    query,
                    message,
                    chat_id,
                    int(message_id),
                    source_id,
                    callback_generation,
                )
            elif data.startswith(MANAGER_CALLBACK_PREFIX):
                seller_key = data[len(MANAGER_CALLBACK_PREFIX) :].split(":", 1)[0]
                manager = SELLER_BY_KEY.get(seller_key)
                if not manager:
                    await query.answer("Неизвестный менеджер", show_alert=True)
                    return
                await self._handle_manager(
                    query,
                    message,
                    chat_id,
                    int(message_id),
                    manager,
                    source_id,
                    callback_generation,
                )
            else:
                await query.answer("Кнопка устарела", show_alert=True)

    async def _manager_actor_allowed(self, bot: Bot | Any, actor_id: object) -> bool:
        try:
            actor = int(actor_id)
        except (TypeError, ValueError):
            return False
        if actor in self.settings.allowed_user_ids:
            return True
        try:
            membership = await bot.get_chat_member(self.settings.chat_id, actor)
        except Exception as exc:
            logger.warning(
                "sales_photo_manager_auth_failed error_type=%s", _error_code(exc)
            )
            return False
        return str(getattr(membership, "status", "")) in {
            "administrator",
            "creator",
            "owner",
        }

    async def _edit_callback_markup(self, query: Any, reply_markup: Any) -> None:
        """Retry an idempotent keyboard edit after one ambiguous network error."""

        for attempt in range(2):
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    return
                raise
            except NetworkError:
                if attempt:
                    raise
        raise RuntimeError("unreachable reply-markup retry state")

    async def _edit_callback_card(
        self,
        query: Any,
        message: Any,
        reply_markup: Any,
    ) -> None:
        """Refresh a card's keyboard and normalize its current editable fields.

        Telegram does not always deliver a channel-post edit back to the bot
        that originally created the post. A callback query does contain the
        current message snapshot, so manager/back clicks are a reliable second
        opportunity to normalize a manually entered phone number.
        """

        content = _message_content(message)
        if content is None:
            await self._edit_callback_markup(query, reply_markup)
            return
        content_kind, body, entities = content
        chat_id = _chat_id(message)
        message_id = getattr(message, "message_id", None)
        daily_order = (
            self.repository.daily_order_for_replacement(chat_id, int(message_id))
            if chat_id is not None and message_id is not None
            else None
        )
        max_length = 1024 if content_kind == "caption" else 4096
        if daily_order is not None:
            final = _normalize_card_fields(
                body,
                entities,
                sale_date=daily_order[0],
                order_id=daily_order[1],
                max_length=max_length,
            )
        else:
            phoned = normalize_caption_phone_field(
                body,
                entities,
                max_length=max_length,
            )
            priced = normalize_card_prices(
                phoned.caption,
                phoned.entities,
                max_length=max_length,
            )
            final = _CardNormalization(
                priced.body,
                priced.entities,
                phoned.changed or priced.changed,
            )
        if not final.changed:
            await self._edit_callback_markup(query, reply_markup)
            if daily_order is not None and card_order_id(body) == daily_order[1]:
                self._mark_order_applied_for_replacement(
                    int(chat_id),
                    int(message_id),
                )
            if chat_id is not None and message_id is not None:
                self._mark_price_applied_for_replacement(
                    int(chat_id),
                    int(message_id),
                )
            return

        for attempt in range(2):
            try:
                if content_kind == "caption":
                    await query.edit_message_caption(
                        caption=final.body,
                        caption_entities=final.entities,
                        reply_markup=reply_markup,
                    )
                else:
                    await query.edit_message_text(
                        text=final.body,
                        entities=final.entities,
                        reply_markup=reply_markup,
                    )
                logger.info(
                    "sales_photo_card_normalized_via_callback chat_id=%s "
                    "message_id=%s",
                    chat_id,
                    message_id,
                )
                if daily_order is not None:
                    self._mark_order_applied_for_replacement(
                        int(chat_id),
                        int(message_id),
                    )
                if chat_id is not None and message_id is not None:
                    self._mark_price_applied_for_replacement(
                        int(chat_id),
                        int(message_id),
                    )
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    if daily_order is not None:
                        self._mark_order_applied_for_replacement(
                            int(chat_id),
                            int(message_id),
                        )
                    if chat_id is not None and message_id is not None:
                        self._mark_price_applied_for_replacement(
                            int(chat_id),
                            int(message_id),
                        )
                    return
                raise
            except NetworkError:
                if attempt:
                    raise
        raise RuntimeError("unreachable caption retry state")

    async def _handle_manager(
        self,
        query: Any,
        message: Any,
        chat_id: int,
        message_id: int,
        manager: str,
        source_message_id: int,
        callback_generation: int,
    ) -> None:
        next_generation = callback_generation + 1
        next_signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            next_generation,
        )
        callback_key = (chat_id, message_id)
        try:
            manager_before = self.repository.selected_manager(chat_id, message_id)
            reserved = self.repository.reserve_ui_transition(
                chat_id,
                message_id,
                callback_generation,
            )
        except Exception as exc:
            await query.answer("Не удалось обновить карточку", show_alert=True)
            logger.warning(
                "sales_photo_manager_reserve_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        if not reserved:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        self._ui_generations[callback_key] = next_generation
        self._pending_managers[callback_key] = manager
        try:
            persisted = self.repository.commit_reserved_manager_selection(
                chat_id,
                message_id,
                manager,
                callback_generation,
            )
        except Exception as exc:
            persisted = False
            logger.warning(
                "sales_photo_manager_ledger_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
        if not persisted:
            try:
                rolled_back = self.repository.rollback_ui_transition(
                    chat_id,
                    message_id,
                    callback_generation,
                    manager_before,
                )
            except Exception:
                rolled_back = False
            if rolled_back:
                self._ui_generations.pop(callback_key, None)
                self._pending_managers.pop(callback_key, None)
            await query.answer("Не удалось обновить карточку", show_alert=True)
            return
        desired_markup = back_keyboard(
            source_message_id=source_message_id,
            generation=next_generation,
            signature=next_signature,
            manager=manager,
        )
        try:
            await self._edit_callback_card(query, message, desired_markup)
        except Exception as exc:
            ambiguous = isinstance(exc, NetworkError) and not isinstance(
                exc, BadRequest
            )
            if not ambiguous:
                try:
                    rolled_back = self.repository.rollback_ui_transition(
                        chat_id,
                        message_id,
                        callback_generation,
                        manager_before,
                    )
                except Exception:
                    rolled_back = False
                if rolled_back:
                    self._ui_generations.pop(callback_key, None)
                    self._pending_managers.pop(callback_key, None)
            else:
                self._ui_generations.pop(callback_key, None)
                self._pending_managers.pop(callback_key, None)
            await query.answer("Не удалось обновить карточку", show_alert=True)
            logger.warning(
                "sales_photo_manager_edit_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        self._ui_generations.pop(callback_key, None)
        self._pending_managers.pop(callback_key, None)
        await query.answer(f"Выбран менеджер: {manager}")

    async def _handle_back(
        self,
        query: Any,
        message: Any,
        chat_id: int,
        message_id: int,
        source_message_id: int,
        callback_generation: int,
    ) -> None:
        next_generation = callback_generation + 1
        next_signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            next_generation,
        )
        callback_key = (chat_id, message_id)
        try:
            manager_before = self.repository.selected_manager(chat_id, message_id)
            reserved = self.repository.reserve_ui_transition(
                chat_id,
                message_id,
                callback_generation,
            )
        except Exception as exc:
            await query.answer("Не удалось вернуть список", show_alert=True)
            logger.warning(
                "sales_photo_back_reserve_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        if not reserved:
            await query.answer("Кнопка устарела", show_alert=True)
            return
        self._ui_generations[callback_key] = next_generation
        try:
            persisted = self.repository.commit_reserved_manager_clear(
                chat_id,
                message_id,
                callback_generation,
            )
        except Exception as exc:
            persisted = False
            logger.warning(
                "sales_photo_back_ledger_failed chat_id=%s message_id=%s "
                "error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
        if not persisted:
            try:
                rolled_back = self.repository.rollback_ui_transition(
                    chat_id,
                    message_id,
                    callback_generation,
                    manager_before,
                )
            except Exception:
                rolled_back = False
            if rolled_back:
                self._ui_generations.pop(callback_key, None)
            await query.answer("Не удалось вернуть список", show_alert=True)
            return
        desired_markup = manager_keyboard(
            source_message_id=source_message_id,
            generation=next_generation,
            signature=next_signature,
        )
        try:
            await self._edit_callback_card(query, message, desired_markup)
        except Exception as exc:
            ambiguous = isinstance(exc, NetworkError) and not isinstance(
                exc, BadRequest
            )
            if not ambiguous:
                try:
                    rolled_back = self.repository.rollback_ui_transition(
                        chat_id,
                        message_id,
                        callback_generation,
                        manager_before,
                    )
                except Exception:
                    rolled_back = False
                if rolled_back:
                    self._ui_generations.pop(callback_key, None)
            else:
                self._ui_generations.pop(callback_key, None)
            await query.answer("Не удалось вернуть список", show_alert=True)
            logger.warning(
                "sales_photo_back_edit_failed chat_id=%s message_id=%s error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        self._ui_generations.pop(callback_key, None)
        self._pending_managers.pop(callback_key, None)
        await query.answer()

    async def retry_pending_deletions(self, bot: Bot | Any) -> None:
        now = utc_now()
        for job in self.repository.pending_deletions(
            self.settings.chat_id,
            limit=10,
        ):
            delay = min(
                3600,
                self.settings.delete_retry_seconds * (2 ** min(job.attempts, 7)),
            )
            updated_at = job.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if (now - updated_at).total_seconds() < delay:
                continue
            if not self.repository.source_pending_deletion(
                job.chat_id,
                job.source_message_id,
                job.replacement_message_id,
            ):
                continue
            await self._delete_source(
                bot,
                chat_id=job.chat_id,
                source_message_id=job.source_message_id,
            )
            self._touch_heartbeat()

    async def retry_failed_photos(self, bot: Bot | Any) -> None:
        now = utc_now()
        for job in self.repository.retryable_photos(
            self.settings.chat_id,
            limit=10,
        ):
            updated_at = job.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            delay = min(
                300,
                self.settings.delete_retry_seconds * (2 ** max(0, job.attempts - 1)),
            )
            if (now - updated_at).total_seconds() < delay:
                continue
            key = (job.chat_id, job.source_message_id)
            lock = self._photo_locks.setdefault(key, asyncio.Lock())
            self._photo_lock_users[key] = self._photo_lock_users.get(key, 0) + 1
            try:
                async with lock:
                    if not self.repository.claim_retry(
                        job.chat_id,
                        job.source_message_id,
                        job.attempts,
                    ):
                        continue
                    self._active_sources.add(key)
                    try:
                        await self._process_claimed_photo(
                            bot,
                            _PhotoClaim(
                                chat_id=job.chat_id,
                                source_message_id=job.source_message_id,
                                source_message_ids=job.source_message_ids,
                                file_ids=job.source_file_ids,
                                source_kind=job.source_kind,
                                client_caption=job.client_caption,
                                message_thread_id=job.message_thread_id,
                                sale_date=job.sale_date,
                            ),
                        )
                    finally:
                        self._active_sources.discard(key)
                        self._cancelled_sources.discard(key)
                    self._touch_heartbeat()
                    return
            finally:
                remaining = self._photo_lock_users.get(key, 1) - 1
                if remaining <= 0:
                    self._photo_lock_users.pop(key, None)
                    if self._photo_locks.get(key) is lock:
                        self._photo_locks.pop(key, None)
                else:
                    self._photo_lock_users[key] = remaining

    async def backfill_order_cards(
        self,
        bot: Bot | Any,
        *,
        ignore_delay: bool = False,
    ) -> None:
        """Add IDs to cards created before daily numbering was introduced."""

        now = utc_now()
        sale_date_from, sale_date_to = _recent_sale_window()
        for job in self.repository.pending_order_backfills(
            self.settings.chat_id,
            limit=50,
            sale_date_from=sale_date_from,
            sale_date_to=sale_date_to,
        ):
            updated_at = job.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            delay = min(3600, 5 * (2 ** min(job.attempts, 8)))
            if not ignore_delay and (now - updated_at).total_seconds() < delay:
                continue

            forwarded = None
            try:
                self._order_backfill_forward_sources[
                    job.replacement_message_id
                ] = time.monotonic() + 600
                forwarded = await self._send_with_retry(
                    bot.forward_message,
                    {
                        "chat_id": job.chat_id,
                        "from_chat_id": job.chat_id,
                        "message_id": job.replacement_message_id,
                        "disable_notification": True,
                    },
                )
                self._track_maintenance_forward(
                    job.replacement_message_id,
                    forwarded,
                )
                content = _message_content(forwarded)
                if content is None:
                    raise RuntimeError("forwarded_card_has_no_content")
                content_kind, body, entities = content
                ordered = ensure_card_order_id(
                    body,
                    entities,
                    job.order_id,
                    max_length=(1024 if content_kind == "caption" else 4096),
                )
                if not ordered.changed:
                    if card_order_id(body) != job.order_id:
                        raise RuntimeError("order_card_cannot_be_extended")
                else:
                    edit_kwargs: dict[str, Any] = {
                        "chat_id": job.chat_id,
                        "message_id": job.replacement_message_id,
                        "reply_markup": self._current_card_markup(
                            job.chat_id,
                            job.replacement_message_id,
                            forwarded,
                        ),
                    }
                    if content_kind == "caption":
                        edit_kwargs.update(
                            caption=ordered.body,
                            caption_entities=ordered.entities,
                        )
                        await bot.edit_message_caption(**edit_kwargs)
                    else:
                        edit_kwargs.update(
                            text=ordered.body,
                            entities=ordered.entities,
                        )
                        await bot.edit_message_text(**edit_kwargs)
                self.repository.mark_order_card_applied(
                    job.chat_id,
                    job.source_message_id,
                )
                logger.info(
                    "sales_photo_order_backfill_complete chat_id=%s "
                    "message_id=%s sale_date=%s order_id=%s",
                    job.chat_id,
                    job.replacement_message_id,
                    job.sale_date.isoformat(),
                    job.order_id,
                )
            except asyncio.CancelledError:
                raise
            except BadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    self.repository.mark_order_card_applied(
                        job.chat_id,
                        job.source_message_id,
                    )
                elif _message_to_forward_missing(exc):
                    sale_day, changed = self.repository.mark_order_card_removed(
                        job.chat_id,
                        job.source_message_id,
                    )
                    logger.info(
                        "sales_photo_order_backfill_removed chat_id=%s "
                        "message_id=%s sale_date=%s changed=%s",
                        job.chat_id,
                        job.replacement_message_id,
                        sale_day.isoformat() if sale_day else "unknown",
                        changed,
                    )
                    return
                else:
                    self.repository.mark_order_backfill_failed(
                        job.chat_id,
                        job.source_message_id,
                    )
                    logger.warning(
                        "sales_photo_order_backfill_failed chat_id=%s "
                        "message_id=%s error_type=%s",
                        job.chat_id,
                        job.replacement_message_id,
                        _error_code(exc),
                    )
            except Exception as exc:
                self.repository.mark_order_backfill_failed(
                    job.chat_id,
                    job.source_message_id,
                )
                logger.warning(
                    "sales_photo_order_backfill_failed chat_id=%s message_id=%s "
                    "error_type=%s",
                    job.chat_id,
                    job.replacement_message_id,
                    _error_code(exc),
                )
            finally:
                if forwarded is not None:
                    temporary_message_id = getattr(forwarded, "message_id", None)
                    if temporary_message_id is not None:
                        await self._delete_duplicate(
                            bot,
                            job.chat_id,
                            int(temporary_message_id),
                        )
            self._touch_heartbeat()

    async def backfill_price_cards(
        self,
        bot: Bot | Any,
        *,
        ignore_delay: bool = False,
    ) -> None:
        """Normalize prices in active cards created before this rule existed."""

        now = utc_now()
        sale_date_from, sale_date_to = _recent_sale_window()
        for job in self.repository.pending_price_backfills(
            self.settings.chat_id,
            limit=50,
            sale_date_from=sale_date_from,
            sale_date_to=sale_date_to,
        ):
            updated_at = job.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            delay = min(3600, 5 * (2 ** min(job.attempts, 8)))
            if not ignore_delay and (now - updated_at).total_seconds() < delay:
                continue

            forwarded = None
            try:
                # Telegram has no get-message endpoint. A quiet self-forward is
                # the only reliable way to fetch the current manager-edited card.
                # The update handlers recognize and immediately remove this copy.
                self._order_backfill_forward_sources[
                    job.replacement_message_id
                ] = time.monotonic() + 600
                forwarded = await self._send_with_retry(
                    bot.forward_message,
                    {
                        "chat_id": job.chat_id,
                        "from_chat_id": job.chat_id,
                        "message_id": job.replacement_message_id,
                        "disable_notification": True,
                    },
                )
                self._track_maintenance_forward(
                    job.replacement_message_id,
                    forwarded,
                )
                content = _message_content(forwarded)
                if content is None:
                    raise RuntimeError("forwarded_card_has_no_content")
                content_kind, body, entities = content
                priced = normalize_card_prices(
                    body,
                    entities,
                    max_length=(1024 if content_kind == "caption" else 4096),
                )
                if priced.changed:
                    edit_kwargs: dict[str, Any] = {
                        "chat_id": job.chat_id,
                        "message_id": job.replacement_message_id,
                        "reply_markup": self._current_card_markup(
                            job.chat_id,
                            job.replacement_message_id,
                            forwarded,
                        ),
                    }
                    if content_kind == "caption":
                        edit_kwargs.update(
                            caption=priced.body,
                            caption_entities=priced.entities,
                        )
                        await bot.edit_message_caption(**edit_kwargs)
                    else:
                        edit_kwargs.update(
                            text=priced.body,
                            entities=priced.entities,
                        )
                        await bot.edit_message_text(**edit_kwargs)
                self.repository.mark_price_card_applied(
                    job.chat_id,
                    job.source_message_id,
                )
                logger.info(
                    "sales_photo_price_backfill_complete chat_id=%s "
                    "message_id=%s changed=%s",
                    job.chat_id,
                    job.replacement_message_id,
                    priced.changed,
                )
            except asyncio.CancelledError:
                raise
            except BadRequest as exc:
                if "message is not modified" in str(exc).casefold():
                    self.repository.mark_price_card_applied(
                        job.chat_id,
                        job.source_message_id,
                    )
                elif _message_to_forward_missing(exc):
                    sale_day, changed = self.repository.mark_order_card_removed(
                        job.chat_id,
                        job.source_message_id,
                    )
                    logger.info(
                        "sales_photo_price_backfill_removed chat_id=%s "
                        "message_id=%s sale_date=%s changed=%s",
                        job.chat_id,
                        job.replacement_message_id,
                        sale_day.isoformat() if sale_day else "unknown",
                        changed,
                    )
                    return
                else:
                    self.repository.mark_price_backfill_failed(
                        job.chat_id,
                        job.source_message_id,
                    )
                    logger.warning(
                        "sales_photo_price_backfill_failed chat_id=%s "
                        "message_id=%s error_type=%s",
                        job.chat_id,
                        job.replacement_message_id,
                        _error_code(exc),
                    )
            except Exception as exc:
                self.repository.mark_price_backfill_failed(
                    job.chat_id,
                    job.source_message_id,
                )
                logger.warning(
                    "sales_photo_price_backfill_failed chat_id=%s message_id=%s "
                    "error_type=%s",
                    job.chat_id,
                    job.replacement_message_id,
                    _error_code(exc),
                )
            finally:
                if forwarded is not None:
                    temporary_message_id = getattr(forwarded, "message_id", None)
                    if temporary_message_id is not None:
                        await self._delete_duplicate(
                            bot,
                            job.chat_id,
                            int(temporary_message_id),
                        )
            self._touch_heartbeat()

    async def audit_deleted_order_cards(
        self,
        bot: Bot | Any,
        *,
        force: bool = False,
        sale_date_from: date | None = None,
        sale_date_to: date | None = None,
    ) -> int:
        """Detect manually deleted Telegram cards and compact their daily IDs."""

        async with self._order_audit_lock:
            now = time.monotonic()
            if (
                not force
                and now - self._last_order_audit < ORDER_AUDIT_INTERVAL_SECONDS
            ):
                return 0
            self._last_order_audit = now
            removed_count = 0
            default_from, default_to = _recent_sale_window()
            for candidate in self.repository.order_audit_candidates(
                self.settings.chat_id,
                sale_date_from=sale_date_from or default_from,
                sale_date_to=sale_date_to or default_to,
            ):
                placeholder = SimpleNamespace(reply_markup=None)
                try:
                    await self._send_with_retry(
                        bot.edit_message_reply_markup,
                        {
                            "chat_id": candidate.chat_id,
                            "message_id": candidate.replacement_message_id,
                            "reply_markup": self._current_card_markup(
                                candidate.chat_id,
                                candidate.replacement_message_id,
                                placeholder,
                            ),
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except BadRequest as exc:
                    if "message is not modified" in str(exc).casefold():
                        continue
                    if not _message_to_edit_missing(exc):
                        logger.warning(
                            "sales_photo_order_audit_failed chat_id=%s "
                            "message_id=%s error_type=%s",
                            candidate.chat_id,
                            candidate.replacement_message_id,
                            _error_code(exc),
                        )
                        continue
                    sale_day, changed = self.repository.mark_order_card_removed(
                        candidate.chat_id,
                        candidate.source_message_id,
                    )
                    removed_count += int(sale_day is not None)
                    logger.info(
                        "sales_photo_order_removed chat_id=%s message_id=%s "
                        "sale_date=%s changed=%s",
                        candidate.chat_id,
                        candidate.replacement_message_id,
                        sale_day.isoformat() if sale_day else "unknown",
                        changed,
                    )
                except Exception as exc:
                    logger.warning(
                        "sales_photo_order_audit_failed chat_id=%s message_id=%s "
                        "error_type=%s",
                        candidate.chat_id,
                        candidate.replacement_message_id,
                        _error_code(exc),
                    )
            return removed_count

    async def auto_correct_recent_cards(
        self,
        bot: Bot | Any,
        *,
        reason: str,
        reference_date: date | None = None,
    ) -> bool:
        """Repair current cards for Tashkent today, yesterday and day before."""

        async with self._auto_correction_lock:
            sale_date_from, sale_date_to = _recent_sale_window(reference_date)
            candidates = self.repository.auto_correction_candidates(
                self.settings.chat_id,
                sale_date_from,
                sale_date_to,
            )
            checked = 0
            corrected = 0
            removed = 0
            failures = 0
            for candidate in candidates:
                forwarded = None
                key = (candidate.chat_id, candidate.replacement_message_id)
                try:
                    async with self._card_lock(key):
                        self._order_backfill_forward_sources[
                            candidate.replacement_message_id
                        ] = time.monotonic() + 600
                        forwarded = await self._send_with_retry(
                            bot.forward_message,
                            {
                                "chat_id": candidate.chat_id,
                                "from_chat_id": candidate.chat_id,
                                "message_id": candidate.replacement_message_id,
                                "disable_notification": True,
                            },
                        )
                        self._track_maintenance_forward(
                            candidate.replacement_message_id,
                            forwarded,
                        )
                        content = _message_content(forwarded)
                        if content is None:
                            raise RuntimeError("forwarded_card_has_no_content")
                        content_kind, body, entities = content
                        current_order = self.repository.daily_order_for_replacement(
                            candidate.chat_id,
                            candidate.replacement_message_id,
                        )
                        if current_order is None:
                            continue
                        final = _normalize_card_fields(
                            body,
                            entities,
                            sale_date=current_order[0],
                            order_id=current_order[1],
                            max_length=(
                                1024 if content_kind == "caption" else 4096
                            ),
                        )
                        if final.changed:
                            edit_kwargs: dict[str, Any] = {
                                "chat_id": candidate.chat_id,
                                "message_id": candidate.replacement_message_id,
                                "reply_markup": self._current_card_markup(
                                    candidate.chat_id,
                                    candidate.replacement_message_id,
                                    forwarded,
                                ),
                            }
                            if content_kind == "caption":
                                edit_kwargs.update(
                                    caption=final.body,
                                    caption_entities=final.entities,
                                )
                                await bot.edit_message_caption(**edit_kwargs)
                            else:
                                edit_kwargs.update(
                                    text=final.body,
                                    entities=final.entities,
                                )
                                await bot.edit_message_text(**edit_kwargs)
                            corrected += 1
                        else:
                            await bot.edit_message_reply_markup(
                                chat_id=candidate.chat_id,
                                message_id=candidate.replacement_message_id,
                                reply_markup=self._current_card_markup(
                                    candidate.chat_id,
                                    candidate.replacement_message_id,
                                    forwarded,
                                ),
                            )
                        self.repository.mark_order_card_applied(
                            candidate.chat_id,
                            candidate.source_message_id,
                        )
                        self.repository.mark_price_card_applied(
                            candidate.chat_id,
                            candidate.source_message_id,
                        )
                        checked += 1
                except asyncio.CancelledError:
                    raise
                except BadRequest as exc:
                    if "message is not modified" in str(exc).casefold():
                        checked += 1
                    elif _message_to_forward_missing(exc) or _message_to_edit_missing(
                        exc
                    ):
                        sale_day, _ = self.repository.mark_order_card_removed(
                            candidate.chat_id,
                            candidate.source_message_id,
                        )
                        removed += int(sale_day is not None)
                    else:
                        failures += 1
                        logger.warning(
                            "sales_photo_auto_correction_card_failed reason=%s "
                            "chat_id=%s message_id=%s error_type=%s",
                            reason,
                            candidate.chat_id,
                            candidate.replacement_message_id,
                            _error_code(exc),
                        )
                except Exception as exc:
                    failures += 1
                    logger.warning(
                        "sales_photo_auto_correction_card_failed reason=%s "
                        "chat_id=%s message_id=%s error_type=%s",
                        reason,
                        candidate.chat_id,
                        candidate.replacement_message_id,
                        _error_code(exc),
                    )
                finally:
                    if forwarded is not None:
                        temporary_message_id = getattr(
                            forwarded,
                            "message_id",
                            None,
                        )
                        if temporary_message_id is not None:
                            await self._delete_duplicate(
                                bot,
                                candidate.chat_id,
                                int(temporary_message_id),
                            )
                    self._safe_touch_heartbeat()
            logger.info(
                "sales_photo_auto_correction_complete reason=%s "
                "sale_date_from=%s sale_date_to=%s candidates=%s checked=%s "
                "corrected=%s removed=%s failures=%s",
                reason,
                sale_date_from.isoformat(),
                sale_date_to.isoformat(),
                len(candidates),
                checked,
                corrected,
                removed,
                failures,
            )
            # A per-card Telegram failure must not turn one requested trigger
            # into a full three-day scan every minute forever. Failed cards are
            # retried by the next scheduled or quiet-period trigger. Exceptions
            # that prevent the pass itself from starting still propagate to the
            # scheduler and use its short retry.
            return True

    async def _wait_for_auto_correction_wake(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(
                self._auto_correction_wake.wait(),
                timeout=max(0.1, float(timeout)),
            )
        except asyncio.TimeoutError:
            return

    async def _auto_correction_scheduler(self, bot: Bot | Any) -> None:
        while True:
            self._auto_correction_wake.clear()
            now = utc_now()
            local_now = now.astimezone(TASHKENT_TZ)
            state = self.repository.auto_correction_state(
                self.settings.chat_id
            )
            latest_slot = latest_auto_correction_slot(local_now)
            latest_slot_utc = latest_slot.astimezone(UTC)
            schedule_due = (
                latest_slot.date() == local_now.date()
                and (
                    state.schedule_done_through is None
                    or state.schedule_done_through < latest_slot_utc
                )
            )
            event_pending = (
                state.event_generation > state.completed_event_generation
                and state.last_new_at is not None
            )
            event_due_at = (
                state.last_new_at
                + timedelta(seconds=EVENT_QUIET_PERIOD_SECONDS)
                if event_pending and state.last_new_at is not None
                else None
            )
            event_due = bool(event_due_at is not None and event_due_at <= now)

            if schedule_due or event_due:
                reasons = []
                if schedule_due:
                    reasons.append(f"time:{latest_slot:%H:%M}")
                if event_due:
                    reasons.append("event:quiet-5m")
                success = False
                try:
                    success = await self.auto_correct_recent_cards(
                        bot,
                        reason="+".join(reasons),
                        reference_date=local_now.date(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "sales_photo_auto_correction_failed reason=%s "
                        "error_type=%s",
                        "+".join(reasons),
                        _error_code(exc),
                    )
                if success:
                    self.repository.mark_auto_correction_complete(
                        self.settings.chat_id,
                        completed_event_generation=(
                            state.event_generation if event_due else None
                        ),
                        schedule_done_through=(
                            latest_slot_utc if schedule_due else None
                        ),
                    )
                    continue
                await self._wait_for_auto_correction_wake(
                    AUTO_CORRECTION_RETRY_SECONDS
                )
                continue

            next_due = next_auto_correction_slot(local_now).astimezone(UTC)
            if event_due_at is not None and event_due_at < next_due:
                next_due = event_due_at
            await self._wait_for_auto_correction_wake(
                max(0.1, (next_due - now).total_seconds())
            )

    async def retry_duplicate_cleanups(self, bot: Bot | Any) -> None:
        now = utc_now()
        for job in self.repository.pending_duplicate_cleanups(
            self.settings.chat_id,
            limit=10,
        ):
            updated_at = job.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            delay = min(
                3600,
                self.settings.delete_retry_seconds * (2 ** min(job.attempts, 7)),
            )
            if (now - updated_at).total_seconds() < delay:
                continue
            await self._delete_duplicate(bot, job.chat_id, job.message_id)
            self._touch_heartbeat()

    def _touch_heartbeat(self) -> None:
        path = Path(self.settings.heartbeat_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")

    def _safe_touch_heartbeat(self) -> None:
        try:
            self._touch_heartbeat()
        except Exception as exc:
            logger.warning(
                "sales_photo_heartbeat_failed error_type=%s", _error_code(exc)
            )

    async def _wait_for_startup_drain(
        self,
        startup_ready: Callable[[], bool] | None,
        update_queue: asyncio.Queue[Any] | Any | None,
    ) -> None:
        if startup_ready is None:
            return

        # post_init runs before Updater.start_polling and Application.start.
        # Stay fail-closed until the polling bot has observed a successful empty
        # long-poll response, proving that the pre-start Telegram backlog was
        # fetched. Keep the heartbeat fresh while Telegram is unreachable.
        while True:
            self._safe_touch_heartbeat()
            try:
                if startup_ready():
                    break
            except Exception as exc:
                logger.warning(
                    "sales_photo_startup_gate_failed error_type=%s",
                    _error_code(exc),
                )
            await asyncio.sleep(1)

        if update_queue is not None:
            await update_queue.join()

        # Close the small hand-off window between the successful empty poll and
        # the next Bot API request, then wait for everything fetched in that
        # window to finish before enabling retries or deletions.
        self._safe_touch_heartbeat()
        await asyncio.sleep(self.settings.startup_drain_seconds)
        if update_queue is not None:
            await update_queue.join()
        self._safe_touch_heartbeat()
        logger.info("sales_photo_startup_drain_complete")

    async def _maintenance(
        self,
        bot: Bot | Any,
        startup_ready: Callable[[], bool] | None = None,
        update_queue: asyncio.Queue[Any] | Any | None = None,
    ) -> None:
        await self._wait_for_startup_drain(startup_ready, update_queue)
        try:
            await self.backfill_order_cards(bot, ignore_delay=True)
            await self.backfill_price_cards(bot, ignore_delay=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "sales_photo_order_startup_backfill_failed error_type=%s",
                _error_code(exc),
            )
        self._startup_gate_active = False
        self._ensure_auto_correction_scheduler(bot)
        while True:
            self._safe_touch_heartbeat()
            self._ensure_auto_correction_scheduler(bot)
            try:
                stale_before = utc_now() - timedelta(
                    seconds=PROCESSING_STALE_SECONDS
                )
                stale_count = self.repository.fail_stale_processing(
                    self.settings.chat_id,
                    stale_before,
                )
                if stale_count:
                    logger.warning(
                        "sales_photo_stale_jobs_quarantined count=%s", stale_count
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "sales_photo_stale_release_failed error_type=%s", _error_code(exc)
                )
            for stage_name, stage in (
                ("source_delete", self.retry_pending_deletions),
                ("duplicate_delete", self.retry_duplicate_cleanups),
                ("photo_retry", self.retry_failed_photos),
            ):
                try:
                    await stage(bot)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "sales_photo_maintenance_stage_failed stage=%s error_type=%s",
                        stage_name,
                        _error_code(exc),
                    )
                try:
                    self._touch_heartbeat()
                except Exception as exc:
                    logger.warning(
                        "sales_photo_heartbeat_failed error_type=%s",
                        _error_code(exc),
                    )
            await asyncio.sleep(min(self.settings.delete_retry_seconds, 60))

    def _ensure_auto_correction_scheduler(self, bot: Bot | Any) -> None:
        task = self._auto_correction_task
        if task is not None and not task.done():
            return
        if task is not None and not task.cancelled():
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = None
            if error is not None:
                logger.warning(
                    "sales_photo_auto_correction_scheduler_restarted "
                    "error_type=%s",
                    _error_code(error),
                )
        self._auto_correction_task = asyncio.create_task(
            self._auto_correction_scheduler(bot),
            name="sales-photo-auto-correction",
        )

    def start_maintenance(
        self,
        bot: Bot | Any,
        *,
        startup_ready: Callable[[], bool] | None = None,
        update_queue: asyncio.Queue[Any] | Any | None = None,
    ) -> None:
        if self._maintenance_task is None or self._maintenance_task.done():
            self._startup_gate_active = startup_ready is not None
            self._maintenance_task = asyncio.create_task(
                self._maintenance(bot, startup_ready, update_queue),
                name="sales-photo-maintenance",
            )

    async def stop(self) -> None:
        task = self._maintenance_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        auto_correction_task = self._auto_correction_task
        if auto_correction_task is not None:
            auto_correction_task.cancel()
            try:
                await auto_correction_task
            except asyncio.CancelledError:
                pass
            self._auto_correction_task = None
        album_tasks = tuple(self._album_tasks.values())
        for album_task in album_tasks:
            album_task.cancel()
        if album_tasks:
            await asyncio.gather(*album_tasks, return_exceptions=True)
        self._album_tasks.clear()
        self._album_items.clear()
        self._album_bots.clear()
        tasks = tuple(self._photo_tasks)
        active_before_stop = tuple(self._active_sources)
        for photo_task in tasks:
            photo_task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._photo_tasks.clear()
        for chat_id, source_message_id in active_before_stop:
            try:
                self.repository.mark_failed(
                    chat_id,
                    source_message_id,
                    "cancelled_before_worker_start",
                )
            except Exception:
                pass
        self._active_sources.clear()
        self._cancelled_sources.clear()
        self._startup_gate_active = False
        close = getattr(self.recognizer, "aclose", None)
        if close is not None:
            await close()
