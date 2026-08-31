from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


def _required(environ: dict[str, str], name: str) -> str:
    value = str(environ.get(name, "")).strip()
    if not value:
        raise ConfigError(f"Не задана переменная {name}")
    return value


def _positive_int(environ: dict[str, str], name: str, default: int) -> int:
    raw = str(environ.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом") from exc
    if value <= 0:
        raise ConfigError(f"{name} должен быть больше нуля")
    return value


def _bounded_int(
    environ: dict[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = _positive_int(environ, name, default)
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} должен быть от {minimum} до {maximum}")
    return value


def _language_code(environ: dict[str, str], name: str, default: str) -> str:
    value = str(environ.get(name, default)).strip().casefold()
    if len(value) != 2 or not value.isascii() or not value.isalpha():
        raise ConfigError(f"{name} должен быть двухбуквенным кодом")
    return value


def _boolean(environ: dict[str, str], name: str, default: bool) -> bool:
    raw = str(environ.get(name, str(default))).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} должен быть true или false")


def _float_between(
    environ: dict[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = str(environ.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть числом") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} должен быть от {minimum} до {maximum}")
    return value


def _telegram_ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise ConfigError(
                "SALES_PHOTO_ALLOWED_USER_IDS должен содержать Telegram ID через запятую"
            ) from exc
        if user_id <= 0:
            raise ConfigError("Telegram user ID должен быть положительным")
        result.add(user_id)
    return frozenset(result)


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    chat_id: int
    gemini_api_key: str = field(repr=False)
    serper_api_key: str = field(repr=False)
    gemini_model: str = "gemini-2.5-flash"
    serper_country: str = "uz"
    serper_language: str = "ru"
    db_path: Path = Path("data/sales_photo.db")
    heartbeat_path: Path = Path("/tmp/sales-photo-heartbeat")
    allowed_user_ids: frozenset[int] = frozenset()
    recognition_timeout_seconds: int = 75
    search_timeout_seconds: int = 12
    recognition_max_bytes: int = 12 * 1024 * 1024
    recognition_min_confidence: float = 0.72
    cache_days: int = 30
    delete_retry_seconds: int = 30
    concurrent_updates: int = 4
    drop_pending_on_first_start: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        values = dict(os.environ if environ is None else environ)
        token = _required(values, "SALES_PHOTO_BOT_TOKEN")
        if ":" not in token or len(token) < 30:
            raise ConfigError("SALES_PHOTO_BOT_TOKEN имеет неверный формат")

        raw_chat_id = _required(values, "SALES_PHOTO_CHAT_ID")
        try:
            chat_id = int(raw_chat_id)
        except ValueError as exc:
            raise ConfigError("SALES_PHOTO_CHAT_ID должен быть целым числом") from exc
        if chat_id >= 0:
            raise ConfigError("SALES_PHOTO_CHAT_ID должен быть отрицательным ID канала")

        model = str(values.get("SALES_PHOTO_GEMINI_MODEL", "gemini-2.5-flash")).strip()
        if not model or len(model) > 80:
            raise ConfigError("SALES_PHOTO_GEMINI_MODEL имеет неверный формат")

        db_path = Path(
            str(values.get("SALES_PHOTO_DB_PATH", "data/sales_photo.db")).strip()
        ).expanduser()
        heartbeat_path = Path(
            str(
                values.get(
                    "SALES_PHOTO_HEARTBEAT_PATH",
                    "/tmp/sales-photo-heartbeat",
                )
            ).strip()
        ).expanduser()

        log_level = str(values.get("SALES_PHOTO_LOG_LEVEL", "INFO")).strip().upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigError("SALES_PHOTO_LOG_LEVEL имеет неверное значение")

        return cls(
            bot_token=token,
            chat_id=chat_id,
            gemini_api_key=_required(values, "GEMINI_API_KEY"),
            serper_api_key=_required(values, "SERPER_API_KEY"),
            gemini_model=model,
            serper_country=_language_code(
                values, "SALES_PHOTO_SEARCH_COUNTRY", "uz"
            ),
            serper_language=_language_code(
                values, "SALES_PHOTO_SEARCH_LANGUAGE", "ru"
            ),
            db_path=db_path,
            heartbeat_path=heartbeat_path,
            allowed_user_ids=_telegram_ids(
                values.get("SALES_PHOTO_ALLOWED_USER_IDS", "")
            ),
            recognition_timeout_seconds=_positive_int(
                values, "SALES_PHOTO_RECOGNITION_TIMEOUT_SECONDS", 75
            ),
            search_timeout_seconds=_bounded_int(
                values, "SALES_PHOTO_SEARCH_TIMEOUT_SECONDS", 12, 3, 30
            ),
            recognition_max_bytes=_positive_int(
                values, "SALES_PHOTO_RECOGNITION_MAX_BYTES", 12 * 1024 * 1024
            ),
            recognition_min_confidence=_float_between(
                values,
                "SALES_PHOTO_MIN_CONFIDENCE",
                0.72,
                0.0,
                1.0,
            ),
            cache_days=_positive_int(values, "SALES_PHOTO_CACHE_DAYS", 30),
            delete_retry_seconds=_positive_int(
                values, "SALES_PHOTO_DELETE_RETRY_SECONDS", 30
            ),
            concurrent_updates=_bounded_int(
                values, "SALES_PHOTO_CONCURRENT_UPDATES", 4, 2, 16
            ),
            drop_pending_on_first_start=_boolean(
                values, "SALES_PHOTO_DROP_PENDING_ON_FIRST_START", True
            ),
            log_level=log_level,
        )
