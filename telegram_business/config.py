from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _time(name: str, default: str) -> time:
    value = os.getenv(name, default).strip()
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must use HH:MM in 24-hour format") from exc


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _workdays(name: str = "BUSINESS_WORKDAYS") -> tuple[int, ...]:
    raw = os.getenv(name, "1,2,3,4,5,6,7")
    try:
        iso_days = {int(part.strip()) for part in raw.split(",") if part.strip()}
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated ISO weekdays 1-7") from exc
    if not iso_days or any(day < 1 or day > 7 for day in iso_days):
        raise ValueError(f"{name} must contain comma-separated ISO weekdays 1-7")
    return tuple(sorted(day - 1 for day in iso_days))


@dataclass(frozen=True)
class BusinessSettings:
    enabled: bool
    bot_token: str
    webhook_secret: str
    allowed_connection_id: str
    admin_chat_id: str
    timezone: str
    night_start: time
    night_end: time
    manager_start: time
    manager_end: time
    final_idle_seconds: int
    debounce_seconds: int
    manager_lock_minutes: int
    credit_cooldown_minutes: int
    max_messages_10m: int
    max_messages_session: int
    db_path: Path
    sheet_id: str
    sheets_sync_seconds: int
    template_cache_seconds: int
    product_source: str
    product_db_path: str
    product_price_max_age_minutes: int
    # New fields stay at the end to preserve the positional constructor used by
    # existing integrations and tests.
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    bot_id: str = ""
    product_urls_path: Path = Path("/app/data/Bot_URLS.xlsx")

    @classmethod
    def load(cls) -> "BusinessSettings":
        enabled = _bool("TELEGRAM_BUSINESS_ENABLED")

        def configured(loader, fallback):
            try:
                return loader()
            except ValueError:
                # A disabled, isolated integration must never prevent the calls
                # application from starting because of an unused stale value.
                if enabled:
                    raise
                return fallback

        token = os.getenv("TELEGRAM_BUSINESS_BOT_TOKEN", "").strip()
        explicit_bot_id = os.getenv("TELEGRAM_BUSINESS_BOT_ID", "").strip()
        derived_bot_id = token.partition(":")[0] if ":" in token else ""
        return cls(
            enabled=enabled,
            bot_token=token,
            webhook_secret=os.getenv("TELEGRAM_BUSINESS_WEBHOOK_SECRET", "").strip(),
            allowed_connection_id=os.getenv("TELEGRAM_BUSINESS_ALLOWED_CONNECTION_ID", "").strip(),
            admin_chat_id=os.getenv("TELEGRAM_BUSINESS_ADMIN_CHAT_ID", "").strip(),
            timezone=os.getenv("APP_TIMEZONE", "Asia/Tashkent").strip(),
            night_start=configured(lambda: _time("BUSINESS_NIGHT_START", "20:00"), time(20)),
            night_end=configured(lambda: _time("BUSINESS_NIGHT_END", "09:30"), time(9, 30)),
            manager_start=configured(lambda: _time("BUSINESS_MANAGER_START", "10:00"), time(10)),
            manager_end=configured(lambda: _time("BUSINESS_MANAGER_END", "20:00"), time(20)),
            final_idle_seconds=configured(lambda: _int("BUSINESS_FINAL_IDLE_SECONDS", 300, minimum=1), 300),
            debounce_seconds=configured(lambda: _int("BUSINESS_DEBOUNCE_SECONDS", 3, minimum=1), 3),
            manager_lock_minutes=configured(lambda: _int("BUSINESS_MANAGER_LOCK_MINUTES", 120, minimum=1), 120),
            credit_cooldown_minutes=configured(lambda: _int("BUSINESS_CREDIT_COOLDOWN_MINUTES", 720, minimum=1), 720),
            max_messages_10m=configured(lambda: _int("BUSINESS_MAX_MESSAGES_10M", 4, minimum=1), 4),
            max_messages_session=configured(lambda: _int("BUSINESS_MAX_MESSAGES_SESSION", 8, minimum=1), 8),
            db_path=Path(os.getenv("BUSINESS_DB_PATH", "/app/data/business_telegram.db")),
            sheet_id=os.getenv("GOOGLE_BUSINESS_SHEET_ID", "13ZFPrYqtV9TQxzNEWsIgw3mX90eZny6sXGvDfpnTLeE").strip(),
            sheets_sync_seconds=configured(lambda: _int("GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS", 60, minimum=1), 60),
            template_cache_seconds=configured(lambda: _int("GOOGLE_TEMPLATE_CACHE_SECONDS", 300, minimum=1), 300),
            product_source=os.getenv("PRODUCT_SOURCE", "existing_google_bot_prices").strip(),
            product_db_path=os.getenv("PRODUCT_DB_PATH", "").strip(),
            product_price_max_age_minutes=configured(lambda: _int("PRODUCT_PRICE_MAX_AGE_MINUTES", 1440, minimum=1), 1440),
            workdays=configured(_workdays, (0, 1, 2, 3, 4, 5, 6)),
            bot_id=explicit_bot_id or derived_bot_id,
            product_urls_path=Path(os.getenv("PRODUCT_URLS_PATH", "/app/data/Bot_URLS.xlsx")),
        )

    def validate_enabled(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("APP_TIMEZONE is not a valid IANA timezone") from exc
        if self.night_start <= self.night_end:
            raise RuntimeError(
                "Business night schedule must cross midnight (start later than end)"
            )
        if self.manager_start >= self.manager_end:
            raise RuntimeError("Business manager start must be earlier than manager end")
        if not self.workdays or any(day not in range(7) for day in self.workdays):
            raise RuntimeError("Business workdays are invalid")
        if not self.enabled:
            return
        if not (self.bot_token and self.webhook_secret and self.allowed_connection_id):
            raise RuntimeError(
                "Telegram Business is enabled but token, webhook secret, or allowed connection ID is missing"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.webhook_secret):
            raise RuntimeError("Telegram Business webhook secret has invalid characters")
        if not self.bot_id.isdigit():
            raise RuntimeError(
                "Telegram Business bot ID is missing; set TELEGRAM_BUSINESS_BOT_ID or use a valid bot token"
            )
        if self.product_source != "existing_google_bot_prices":
            raise RuntimeError(
                "Telegram Business product source is unsupported; use the approved existing_google_bot_prices source"
            )
        if not self.sheet_id:
            raise RuntimeError("Telegram Business Google workbook ID is missing")
