from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from telegram import Bot, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    filters,
)

from .config import Settings
from .keyboards import CALLBACK_PREFIX
from .repository import SalesPhotoRepository
from .service import IdentifierRecognizer, SalesPhotoService


logger = logging.getLogger(__name__)
POLLING_ALLOWED_UPDATES = (
    "message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
)


def _poll_timeout_seconds(value: object) -> float:
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class StartupDrainBot(ExtBot[Any]):
    """Expose when polling has successfully consumed the startup backlog."""

    __slots__ = ("_startup_updates_drained",)

    def __init__(self, token: str):
        super().__init__(token=token)
        self._startup_updates_drained = asyncio.Event()

    @property
    def startup_updates_drained(self) -> asyncio.Event:
        return self._startup_updates_drained

    async def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int | timedelta | None = None,
        allowed_updates: tuple[str, ...] | list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[Update, ...]:
        updates = await super().get_updates(
            offset=offset,
            limit=limit,
            timeout=timeout,
            allowed_updates=allowed_updates,
            **kwargs,
        )
        # Updater's shutdown acknowledgement uses timeout=0 and must not open
        # the startup gate. A successful empty long poll means every update
        # that predated it has already been put into Application.update_queue.
        if not updates and _poll_timeout_seconds(timeout) > 0:
            self._startup_updates_drained.set()
        return updates


async def _error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.error(
        "sales_photo_update_failed error_type=%s",
        type(context.error).__name__,
    )


async def _prepare_polling(
    settings: Settings,
    repository: SalesPhotoRepository,
    service: SalesPhotoService,
    bot: Bot,
) -> None:
    bot_id = await service.preflight(bot)
    if not isinstance(bot_id, int) or bot_id <= 0:
        raise RuntimeError("Не удалось определить Telegram bot ID")
    if settings.drop_pending_on_first_start and not repository.is_bootstrapped(
        bot_id,
        settings.chat_id,
    ):
        flushed = await bot.delete_webhook(drop_pending_updates=True)
        if not flushed:
            raise RuntimeError("Telegram не подтвердил очистку старых updates")
    repository.mark_bootstrapped(bot_id, settings.chat_id)


def build_application(
    settings: Settings,
    repository: SalesPhotoRepository | None = None,
    recognizer: IdentifierRecognizer | None = None,
) -> Application:
    repo = repository or SalesPhotoRepository(settings.db_path)
    # Production intentionally passes no recognizer: Telegram's existing
    # file_id is reposted without downloading or inspecting the photo.
    service = SalesPhotoService(settings, repo, recognizer)

    async def post_init(application: Application) -> None:
        await _prepare_polling(settings, repo, service, application.bot)

        def startup_ready() -> bool:
            updater = application.updater
            return bool(
                application.running
                and updater is not None
                and updater.running
                and polling_bot.startup_updates_drained.is_set()
            )

        service.start_maintenance(
            application.bot,
            startup_ready=startup_ready,
            update_queue=application.update_queue,
        )

    async def post_shutdown(application: Application) -> None:
        await service.stop()

    polling_bot = StartupDrainBot(settings.bot_token)
    application = (
        Application.builder()
        .bot(polling_bot)
        .concurrent_updates(settings.concurrent_updates)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["sales_photo_service"] = service
    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST
            & filters.Chat(chat_id=settings.chat_id)
            & filters.PHOTO,
            service.on_photo,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_CHANNEL_POST
            & filters.Chat(chat_id=settings.chat_id),
            service.on_edited_photo,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            service.on_manager_callback,
            pattern=(
                rf"^{CALLBACK_PREFIX}:(?:m:[a-z]+|b):"
                r"\d+:\d+:[0-9a-f]{12}$"
            ),
        )
    )
    application.add_error_handler(_error_handler)
    return application


def run(settings: Settings) -> None:
    repository = SalesPhotoRepository(settings.db_path)
    application = build_application(settings, repository=repository)
    application.run_polling(
        allowed_updates=POLLING_ALLOWED_UPDATES,
        drop_pending_updates=False,
    )
