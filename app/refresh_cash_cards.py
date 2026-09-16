"""One-off, idempotent Telegram edit of already published cash cards.

Run ``python -m app.refresh_cash_cards`` to inspect the count, then add
``--apply`` to edit existing bot messages in place. No messages are sent or
deleted, and no ledger values are changed.
"""

import argparse
import asyncio
import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError

from app.config import Settings
from app.database import OrderRepository
from app.handlers.cash import cash_notification_text, cash_review_keyboard

logger = logging.getLogger(__name__)


def _retry_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


async def refresh(*, apply: bool) -> tuple[int, int]:
    settings = Settings.load()
    repo = OrderRepository(settings.database_path)
    with repo.connect() as db:
        ids = [row[0] for row in db.execute(
            "SELECT id FROM courier_cash_entries "
            "WHERE notification_chat_id IS NOT NULL AND notification_message_id IS NOT NULL "
            "ORDER BY id"
        )]
    print(f"Карточек для проверки: {len(ids)}")
    if not apply:
        print("Без --apply сообщения не меняются.")
        return 0, 0

    edited = failed = 0
    async with Bot(settings.bot_token) as bot:
        for entry_id in ids:
            entry = repo.get_cash_entry(entry_id)
            if not entry or not entry.notification_chat_id or not entry.notification_message_id:
                continue
            for attempt in range(4):
                try:
                    await bot.edit_message_text(
                        chat_id=entry.notification_chat_id,
                        message_id=entry.notification_message_id,
                        text=cash_notification_text(entry),
                        parse_mode=ParseMode.HTML,
                        reply_markup=cash_review_keyboard(entry) if entry.status == "pending" else None,
                    )
                    edited += 1
                    break
                except BadRequest as error:
                    if "message is not modified" in str(error).casefold():
                        break
                    failed += 1
                    logger.warning("Карточка кассы %s: %s", entry_id, error)
                    break
                except RetryAfter as error:
                    if attempt == 3:
                        failed += 1
                        logger.warning("Карточка кассы %s: слишком много попыток", entry_id)
                        break
                    await asyncio.sleep(_retry_seconds(error) + 1)
                except TelegramError as error:
                    failed += 1
                    logger.warning("Карточка кассы %s: %s", entry_id, error)
                    break
            await asyncio.sleep(0.7)
    print(f"Обновлено: {edited}; не удалось: {failed}")
    return edited, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="изменить прежние карточки в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _, failed = asyncio.run(refresh(apply=args.apply))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
