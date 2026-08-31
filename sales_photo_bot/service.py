from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram import Bot, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ContextTypes

from .config import Settings
from .formatting import (
    add_manager_selection,
    build_caption,
    remove_manager_selection,
    selected_manager_from_caption,
)
from .keyboards import (
    BACK_CALLBACK,
    MANAGER_CALLBACK_PREFIX,
    SELLER_BY_KEY,
    back_keyboard,
    manager_keyboard,
)
from .models import EMPTY_RECOGNITION, Recognition
from .recognition import ProductRecognizer
from .repository import SalesPhotoRepository, utc_now


logger = logging.getLogger(__name__)
UTC = timezone.utc
BOT_CARD_MARKER = "\u2063\u2063"
_SOURCE_CALLBACK_RE = re.compile(
    r"^sp:(?:m:[a-z]+|b):(\d+):(\d+):([0-9a-f]{12})$"
)


def _error_code(error: BaseException) -> str:
    return type(error).__name__[:80]


def _already_deleted(error: BaseException) -> bool:
    if not isinstance(error, BadRequest):
        return False
    message = str(error).casefold()
    return "message to delete not found" in message or "message_id_invalid" in message


def _chat_id(message: object) -> int | None:
    value = getattr(message, "chat_id", None)
    if value is None:
        chat = getattr(message, "chat", None)
        value = getattr(chat, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _caption_html(message: object) -> str:
    rendered = getattr(message, "caption_html", None)
    if rendered:
        return str(rendered)
    return html.escape(str(getattr(message, "caption", "") or ""), quote=True)


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


def _retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
    else:
        seconds = float(value)
    return min(86_400.0, max(0.1, seconds))


class SalesPhotoService:
    def __init__(
        self,
        settings: Settings,
        repository: SalesPhotoRepository,
        recognizer: ProductRecognizer,
    ):
        self.settings = settings
        self.repository = repository
        self.recognizer = recognizer
        self.bot_id: int | None = None
        self._photo_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._photo_lock_users: dict[tuple[int, int], int] = {}
        self._callback_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._callback_lock_users: dict[tuple[int, int], int] = {}
        self._ui_generations: dict[tuple[int, int], int] = {}
        self._maintenance_task: asyncio.Task[None] | None = None

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
        await self.handle_photo(message, context.bot)

    async def handle_photo(self, message: Message | Any, bot: Bot | Any) -> None:
        chat_id = _chat_id(message)
        source_message_id = getattr(message, "message_id", None)
        photos = tuple(getattr(message, "photo", None) or ())
        if chat_id != self.settings.chat_id or source_message_id is None or not photos:
            return
        if str(getattr(message, "caption", "") or "").startswith(BOT_CARD_MARKER):
            await self._reconcile_generated_post(message, bot)
            return
        source_message_id = int(source_message_id)
        if self.repository.is_replacement(chat_id, source_message_id):
            return
        sender = getattr(message, "from_user", None)
        if bool(getattr(sender, "is_bot", False)):
            return
        if self.bot_id is not None and getattr(sender, "id", None) == self.bot_id:
            return

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
            return

        key = (chat_id, source_message_id)
        lock = self._photo_locks.setdefault(key, asyncio.Lock())
        self._photo_lock_users[key] = self._photo_lock_users.get(key, 0) + 1
        try:
            async with lock:
                if not self.repository.claim_photo(
                    chat_id,
                    source_message_id,
                    file_unique_id,
                    source_file_id=file_id,
                    client_caption=getattr(message, "caption", None),
                    message_thread_id=getattr(message, "message_thread_id", None),
                ):
                    return
                await self._process_claimed_photo(
                    bot,
                    chat_id,
                    source_message_id,
                    file_id,
                    getattr(message, "caption", None),
                    getattr(message, "message_thread_id", None),
                    photo,
                )
        finally:
            remaining = self._photo_lock_users.get(key, 1) - 1
            if remaining <= 0:
                self._photo_lock_users.pop(key, None)
                if self._photo_locks.get(key) is lock:
                    self._photo_locks.pop(key, None)
            else:
                self._photo_lock_users[key] = remaining

    async def _process_claimed_photo(
        self,
        bot: Bot | Any,
        chat_id: int,
        source_message_id: int,
        file_id: str,
        client_caption: str | None,
        message_thread_id: int | None,
        photo: object | None = None,
    ) -> None:
        recognition = await self._recognize_photo(photo, file_id, bot)
        caption = BOT_CARD_MARKER + build_caption(client_caption, recognition)
        initial_generation = 0
        source_signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            initial_generation,
        )
        send_kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": file_id,
            "caption": caption,
            "parse_mode": ParseMode.HTML,
            "reply_markup": manager_keyboard(
                source_message_id=source_message_id,
                generation=initial_generation,
                signature=source_signature,
            ),
        }
        if message_thread_id is not None:
            send_kwargs["message_thread_id"] = int(message_thread_id)

        try:
            replacement = await self._send_photo(bot, send_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Once sendPhoto has been invoked, a transport failure can mean that
            # Telegram accepted the post but the response was lost. Bots do not
            # receive channel_post updates for their own outgoing posts, so an
            # automatic retry could create an orphan duplicate.
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
        try:
            outcome = self.repository.record_replacement(
                chat_id,
                source_message_id,
                replacement_message_id,
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
            await self._delete_duplicate(bot, chat_id, replacement_message_id)
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

        await self._delete_source(
            bot,
            chat_id=chat_id,
            source_message_id=source_message_id,
        )

    async def _send_photo(self, bot: Bot | Any, kwargs: dict[str, Any]) -> Any:
        for attempt in range(2):
            try:
                return await bot.send_photo(**kwargs)
            except asyncio.CancelledError:
                raise
            except RetryAfter as exc:
                if attempt:
                    raise
                retry_seconds = _retry_after_seconds(exc)
                if retry_seconds > 60:
                    raise
                await asyncio.sleep(retry_seconds)
        raise RuntimeError("unreachable send retry state")

    async def _reconcile_generated_post(
        self,
        message: Message | Any,
        bot: Bot | Any,
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
        await self._delete_source(
            bot,
            chat_id=chat_id,
            source_message_id=source_message_id,
        )

    async def _recognize_photo(
        self,
        photo: object,
        file_id: str,
        bot: Bot | Any,
    ) -> Recognition:
        file_size = int(getattr(photo, "file_size", 0) or 0)
        if file_size > self.settings.recognition_max_bytes:
            logger.warning("sales_photo_recognition_skipped reason=file_too_large")
            return EMPTY_RECOGNITION
        try:
            telegram_file = await bot.get_file(file_id)
            data = bytes(await telegram_file.download_as_bytearray())
            if len(data) > self.settings.recognition_max_bytes:
                return EMPTY_RECOGNITION
            return await self.recognizer.recognize(data, "image/jpeg")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "sales_photo_recognition_failed error_type=%s", _error_code(exc)
            )
            return EMPTY_RECOGNITION

    async def _delete_source(
        self,
        bot: Bot | Any,
        chat_id: int,
        source_message_id: int,
    ) -> None:
        try:
            await bot.delete_message(chat_id, source_message_id)
        except Exception as exc:
            if _already_deleted(exc):
                self.repository.mark_complete(chat_id, source_message_id)
                return
            self.repository.mark_delete_pending(
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            logger.warning(
                "sales_photo_source_delete_pending chat_id=%s source_message_id=%s error_type=%s",
                chat_id,
                source_message_id,
                _error_code(exc),
            )
            return
        self.repository.mark_complete(chat_id, source_message_id)

    async def _delete_duplicate(
        self,
        bot: Bot | Any,
        chat_id: int,
        message_id: int,
    ) -> None:
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
            return
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as exc:
            if _already_deleted(exc):
                try:
                    self.repository.complete_duplicate_cleanup(chat_id, message_id)
                except Exception:
                    pass
                return
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
            return
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
        lock = self._callback_locks.setdefault(callback_key, asyncio.Lock())
        self._callback_lock_users[callback_key] = (
            self._callback_lock_users.get(callback_key, 0) + 1
        )
        try:
            async with lock:
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
                if (
                    ledger_generation is None
                    or callback_generation < known_generation
                    or is_back != bool(callback_generation % 2)
                ):
                    await query.answer("Кнопка устарела", show_alert=True)
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
        finally:
            remaining = self._callback_lock_users.get(callback_key, 1) - 1
            if remaining <= 0:
                self._callback_lock_users.pop(callback_key, None)
                if self._callback_locks.get(callback_key) is lock:
                    self._callback_locks.pop(callback_key, None)
            else:
                self._callback_lock_users[callback_key] = remaining

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
        selected = selected_manager_from_caption(_caption_html(message))
        if selected:
            await query.answer(
                f"Уже выбран менеджер: {selected}",
                show_alert=True,
            )
            return
        caption = add_manager_selection(_caption_html(message), manager)
        next_generation = callback_generation + 1
        next_signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            next_generation,
        )
        callback_key = (chat_id, message_id)
        try:
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
        try:
            await query.edit_message_caption(
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard(
                    source_message_id=source_message_id,
                    generation=next_generation,
                    signature=next_signature,
                ),
            )
        except Exception as exc:
            if not isinstance(exc, NetworkError):
                try:
                    released = self.repository.release_ui_transition(
                        chat_id,
                        message_id,
                        callback_generation,
                    )
                except Exception:
                    released = False
                if released:
                    self._ui_generations.pop(callback_key, None)
            await query.answer("Не удалось обновить карточку", show_alert=True)
            logger.warning(
                "sales_photo_manager_edit_failed chat_id=%s message_id=%s error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        try:
            persisted = self.repository.commit_reserved_manager_selection(
                chat_id,
                message_id,
                manager,
                callback_generation,
            )
            if not persisted:
                logger.warning(
                    "sales_photo_manager_ledger_stale chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
            else:
                self._ui_generations.pop(callback_key, None)
        except Exception as exc:
            logger.warning(
                "sales_photo_manager_ledger_failed chat_id=%s message_id=%s error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
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
        selected = selected_manager_from_caption(_caption_html(message))
        if not selected:
            await query.answer("Менеджер ещё не выбран")
            return
        caption = remove_manager_selection(_caption_html(message))
        next_generation = callback_generation + 1
        next_signature = self.repository.callback_signature(
            chat_id,
            source_message_id,
            next_generation,
        )
        callback_key = (chat_id, message_id)
        try:
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
            await query.edit_message_caption(
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=manager_keyboard(
                    source_message_id=source_message_id,
                    generation=next_generation,
                    signature=next_signature,
                ),
            )
        except Exception as exc:
            if not isinstance(exc, NetworkError):
                try:
                    released = self.repository.release_ui_transition(
                        chat_id,
                        message_id,
                        callback_generation,
                    )
                except Exception:
                    released = False
                if released:
                    self._ui_generations.pop(callback_key, None)
            await query.answer("Не удалось вернуть список", show_alert=True)
            logger.warning(
                "sales_photo_back_edit_failed chat_id=%s message_id=%s error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
            return
        try:
            persisted = self.repository.commit_reserved_manager_clear(
                chat_id,
                message_id,
                callback_generation,
            )
            if not persisted:
                logger.warning(
                    "sales_photo_back_ledger_stale chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
            else:
                self._ui_generations.pop(callback_key, None)
        except Exception as exc:
            logger.warning(
                "sales_photo_back_ledger_failed chat_id=%s message_id=%s error_type=%s",
                chat_id,
                message_id,
                _error_code(exc),
            )
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
                    await self._process_claimed_photo(
                        bot,
                        job.chat_id,
                        job.source_message_id,
                        job.source_file_id,
                        job.client_caption,
                        job.message_thread_id,
                    )
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

    async def _maintenance(self, bot: Bot | Any) -> None:
        while True:
            try:
                self._touch_heartbeat()
            except Exception as exc:
                logger.warning(
                    "sales_photo_heartbeat_failed error_type=%s", _error_code(exc)
                )
            try:
                stale_before = utc_now() - timedelta(
                    seconds=self.settings.recognition_timeout_seconds + 90
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

    def start_maintenance(self, bot: Bot | Any) -> None:
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(
                self._maintenance(bot), name="sales-photo-maintenance"
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
        close = getattr(self.recognizer, "aclose", None)
        if close is not None:
            await close()
