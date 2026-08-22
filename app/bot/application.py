import logging
from telegram import Update
from telegram.ext import Application, ContextTypes

from app.config import Settings
from app.database import OrderRepository
from app.handlers import register_handlers

logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try: await update.effective_message.reply_text("Произошла ошибка. Попробуйте ещё раз или используйте /cancel.")
        except Exception: logger.exception("Could not notify user")


def build_application(settings: Settings) -> Application:
    repo = OrderRepository(settings.database_path)
    repo.initialize()
    application = Application.builder().token(settings.bot_token).build()
    application.bot_data.update(settings=settings, repo=repo)
    register_handlers(application)
    application.add_error_handler(error_handler)
    return application
