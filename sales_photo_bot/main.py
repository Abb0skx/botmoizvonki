from __future__ import annotations

import logging

from .application import run
from .config import Settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HTTP clients may otherwise include the bot token in request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.request").setLevel(logging.WARNING)


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    run(settings)


if __name__ == "__main__":
    main()
