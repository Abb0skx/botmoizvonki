from __future__ import annotations

import logging

from telegram import Bot, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .keyboards import CALLBACK_PREFIX
from .recognition import GeminiProductRecognizer, ProductRecognizer
from .repository import SalesPhotoRepository
from .search import SerperProductSearch
from .service import SalesPhotoService


logger = logging.getLogger(__name__)


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
    service.start_maintenance(bot)


def build_application(
    settings: Settings,
    repository: SalesPhotoRepository | None = None,
    recognizer: ProductRecognizer | None = None,
) -> Application:
    repo = repository or SalesPhotoRepository(settings.db_path)
    product_recognizer = recognizer or GeminiProductRecognizer(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        timeout_seconds=settings.recognition_timeout_seconds,
        minimum_confidence=settings.recognition_min_confidence,
        cache_days=settings.cache_days,
        search=SerperProductSearch(
            api_key=settings.serper_api_key,
            timeout_seconds=settings.search_timeout_seconds,
            country=settings.serper_country,
            language=settings.serper_language,
        ),
        repository=repo,
    )
    service = SalesPhotoService(settings, repo, product_recognizer)

    async def post_init(application: Application) -> None:
        await _prepare_polling(settings, repo, service, application.bot)

    async def post_shutdown(application: Application) -> None:
        await service.stop()

    application = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(settings.concurrent_updates)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["sales_photo_service"] = service
    application.add_handler(
        MessageHandler(
            filters.Chat(chat_id=settings.chat_id) & filters.PHOTO,
            service.on_photo,
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
        allowed_updates=["message", "channel_post", "callback_query"],
        drop_pending_updates=False,
    )
