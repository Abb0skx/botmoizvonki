from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_COMPETITORS = {
    213962560: "Mobilon",
    6243942320: "MixMobiles_1",
    1780333654: "MixMobiles_2",
}


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _competitors() -> dict[int, str]:
    raw = os.getenv("TELEGRAM_MARKET_COMPETITORS_JSON", "").strip()
    if not raw:
        return dict(DEFAULT_COMPETITORS)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("TELEGRAM_MARKET_COMPETITORS_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("TELEGRAM_MARKET_COMPETITORS_JSON must be an object")
    result: dict[int, str] = {}
    for raw_id, raw_label in parsed.items():
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("competitor IDs must be integers") from exc
        label = str(raw_label).strip()
        if user_id <= 0 or not label or len(label) > 80:
            raise ValueError("competitor entries must contain a positive ID and label")
        result[user_id] = label
    return result


@dataclass(frozen=True, slots=True)
class MarketStatsSettings:
    enabled: bool
    group_id: int
    group_title: str
    db_path: Path
    catalog_path: Path
    poll_seconds: int
    backfill_days: int
    backfill_limit: int
    batch_size: int
    edit_rescan_messages: int
    competitors: dict[int, str]

    @classmethod
    def load(cls) -> "MarketStatsSettings":
        raw_group = os.getenv("TELEGRAM_MARKET_GROUP_ID", "-1002188560435").strip()
        try:
            group_id = int(raw_group)
        except ValueError as exc:
            raise ValueError("TELEGRAM_MARKET_GROUP_ID must be an integer") from exc
        settings = cls(
            enabled=_bool("TELEGRAM_MARKET_STATS_ENABLED"),
            group_id=group_id,
            group_title=os.getenv(
                "TELEGRAM_MARKET_GROUP_TITLE", "Malika bozor N1"
            ).strip(),
            db_path=Path(os.getenv(
                "TELEGRAM_MARKET_STATS_DB_PATH",
                "/app/data/telegram_market_stats.db",
            )),
            catalog_path=Path(os.getenv(
                "TELEGRAM_MARKET_CATALOG_PATH", "/app/data/Bot_URLS.xlsx"
            )),
            poll_seconds=_int("TELEGRAM_MARKET_POLL_SECONDS", 30, 10, 3600),
            backfill_days=_int("TELEGRAM_MARKET_BACKFILL_DAYS", 30, 0, 3650),
            backfill_limit=_int(
                "TELEGRAM_MARKET_BACKFILL_LIMIT", 100_000, 100, 2_000_000
            ),
            batch_size=_int("TELEGRAM_MARKET_BATCH_SIZE", 1000, 10, 5000),
            edit_rescan_messages=_int(
                "TELEGRAM_MARKET_EDIT_RESCAN_MESSAGES", 500, 0, 5000
            ),
            competitors=_competitors(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.enabled:
            return
        if self.group_id >= 0:
            raise ValueError("TELEGRAM_MARKET_GROUP_ID must be a negative Telegram chat ID")
        if not self.group_title or len(self.group_title) > 255:
            raise ValueError("TELEGRAM_MARKET_GROUP_TITLE is required")
        if not self.db_path.is_absolute():
            raise ValueError("TELEGRAM_MARKET_STATS_DB_PATH must be absolute")
        if not self.catalog_path.is_absolute():
            raise ValueError("TELEGRAM_MARKET_CATALOG_PATH must be absolute")
        if self.db_path == Path(os.getenv(
            "BUSINESS_DB_PATH", "/app/data/business_telegram.db"
        )):
            raise ValueError("market statistics must use a separate SQLite database")
