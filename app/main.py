import logging

from app.bot.application import build_application
from app.config import Settings


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # Telegram API URLs contain the bot token; never write them to INFO logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    application = build_application(Settings.load())
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
