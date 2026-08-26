from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class PriceSettings:
    enabled: bool
    db_path: Path
    legacy_html_path: Path
    admin_username: str
    admin_password: str
    sync_api_key: str
    telegram_bot_token: str
    telegram_channel_id: str
    telegram_channel_username: str
    product_sort_sheet_id: str
    posts_sheet_name: str
    timezone: str
    scheduler_poll_seconds: int
    sync_max_bytes: int
    telegram_preview_channel_id: str = ""
    post_index_sheet_name: str = "Price Post IDs"
    telegram_updates_limit: int = 100

    @classmethod
    def load(cls) -> "PriceSettings":
        return cls(
            enabled=_bool("PRICE_SERVER_ENABLED", False),
            db_path=Path(
                os.getenv(
                    "PRICE_DB_PATH",
                    "/app/data/price_server.db",
                )
            ),
            legacy_html_path=Path(
                os.getenv(
                    "PRICE_LEGACY_HTML_PATH",
                    "/app/price/index.html",
                )
            ),
            admin_username=(
                os.getenv("PRICE_ADMIN_USERNAME", "admin").strip()
                or "admin"
            ),
            admin_password=os.getenv("PRICE_ADMIN_PASSWORD", ""),
            sync_api_key=os.getenv("PRICE_SYNC_API_KEY", "").strip(),
            telegram_bot_token=os.getenv(
                "PRICE_TELEGRAM_BOT_TOKEN", ""
            ).strip(),
            telegram_channel_id=os.getenv(
                "PRICE_TELEGRAM_CHANNEL_ID", ""
            ).strip(),
            telegram_channel_username=os.getenv(
                "PRICE_TELEGRAM_CHANNEL_USERNAME", ""
            ).strip().lstrip("@"),
            product_sort_sheet_id=os.getenv(
                "PRICE_PRODUCT_SORT_SHEET_ID",
                "1Hiq-ccGGo3skcp0Imyw0Jum4OPilEs8lBJGApK6vlrs",
            ).strip(),
            posts_sheet_name=(
                os.getenv("PRICE_POSTS_SHEET_NAME", "Telegram Posts").strip()
                or "Telegram Posts"
            ),
            timezone=(
                os.getenv("PRICE_TIMEZONE", "Asia/Tashkent").strip()
                or "Asia/Tashkent"
            ),
            scheduler_poll_seconds=_int(
                "PRICE_SCHEDULER_POLL_SECONDS", 5
            ),
            sync_max_bytes=_int(
                "PRICE_SYNC_MAX_BYTES", 10 * 1024 * 1024
            ),
            telegram_preview_channel_id=os.getenv(
                "PRICE_TELEGRAM_PREVIEW_CHANNEL_ID", ""
            ).strip(),
            post_index_sheet_name=(
                os.getenv(
                    "PRICE_POST_INDEX_SHEET_NAME", "Price Post IDs"
                ).strip()
                or "Price Post IDs"
            ),
            telegram_updates_limit=_int(
                "PRICE_TELEGRAM_UPDATES_LIMIT", 100
            ),
        )

    def validate_runtime(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                "PRICE_TIMEZONE must be a valid IANA timezone"
            ) from exc

        if not self.admin_password:
            raise RuntimeError("PRICE_ADMIN_PASSWORD is not configured")
        if not self.sync_api_key:
            raise RuntimeError("PRICE_SYNC_API_KEY is not configured")
        if self.telegram_preview_channel_id:
            preview = self.telegram_preview_channel_id
            if not (preview.startswith("-100") and preview[4:].isdigit()):
                raise RuntimeError(
                    "PRICE_TELEGRAM_PREVIEW_CHANNEL_ID must be a -100 channel ID"
                )
            if preview == self.telegram_channel_id:
                raise RuntimeError(
                    "Preview and publication channels must be different"
                )
        if len(self.post_index_sheet_name) > 100:
            raise RuntimeError(
                "PRICE_POST_INDEX_SHEET_NAME must be at most 100 characters"
            )

    @property
    def telegram_configured(self) -> bool:
        return bool(
            self.telegram_bot_token
            and self.telegram_channel_id
        )

    @property
    def preview_configured(self) -> bool:
        return bool(
            self.telegram_bot_token
            and self.telegram_preview_channel_id
        )
