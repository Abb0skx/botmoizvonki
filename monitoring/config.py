from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _ids(name: str, *, fallback: str | None = None) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw and fallback:
        raw = os.getenv(fallback, "").strip()
    try:
        return frozenset(
            int(item.strip()) for item in raw.split(",") if item.strip()
        )
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must contain comma-separated Telegram user IDs"
        ) from exc


def _https_url(name: str, value: str, *, required: bool) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        if required:
            raise RuntimeError(f"{name} is required")
        return ""
    parsed = urlparse(cleaned)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError(f"{name} must be an HTTPS URL without credentials")
    return cleaned


def _http_url(name: str, value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError(f"{name} must be an HTTP(S) URL without credentials")
    return cleaned


@dataclass(frozen=True)
class MonitoringSettings:
    enabled: bool
    base_url: str
    session_db_path: Path
    session_ttl_seconds: int
    idle_ttl_seconds: int
    manager_ids: frozenset[int]
    admin_ids: frozenset[int]
    telegram_client_id: str
    telegram_client_secret: str
    telegram_redirect_uri: str
    delivery_base_url: str = ""
    delivery_service_token: str = ""
    price_base_url: str = ""
    price_service_token: str = ""
    go_api_url: str = ""
    go_api_token: str = ""

    @classmethod
    def load(cls) -> "MonitoringSettings":
        enabled = _bool("MONITORING_ENABLED", False)
        base_url = _https_url(
            "MONITORING_BASE_URL",
            os.getenv(
                "MONITORING_BASE_URL",
                "https://bot.texnikach.uz/monitoring",
            ),
            required=True,
        )
        redirect_uri = _https_url(
            "MONITORING_TELEGRAM_REDIRECT_URI",
            os.getenv(
                "MONITORING_TELEGRAM_REDIRECT_URI",
                base_url + "/auth/callback",
            ),
            required=True,
        )
        delivery_base = _http_url(
            "MONITORING_DELIVERY_BASE_URL",
            os.getenv("MONITORING_DELIVERY_BASE_URL", ""),
        )
        price_base = _http_url(
            "MONITORING_PRICE_BASE_URL",
            os.getenv("MONITORING_PRICE_BASE_URL", ""),
        )
        return cls(
            enabled=enabled,
            base_url=base_url,
            session_db_path=Path(
                os.getenv(
                    "MONITORING_SESSION_DB_PATH",
                    "/app/data/monitoring_sessions.db",
                )
            ),
            session_ttl_seconds=_positive_int(
                "MONITORING_SESSION_TTL_SECONDS", 43_200, minimum=300
            ),
            idle_ttl_seconds=_positive_int(
                "MONITORING_IDLE_TTL_SECONDS", 7_200, minimum=300
            ),
            manager_ids=_ids(
                "MONITORING_MANAGER_IDS", fallback="DELIVERY_MANAGER_IDS"
            ),
            admin_ids=_ids("MONITORING_ADMIN_IDS"),
            telegram_client_id=os.getenv(
                "MONITORING_TELEGRAM_CLIENT_ID", ""
            ).strip(),
            telegram_client_secret=os.getenv(
                "MONITORING_TELEGRAM_CLIENT_SECRET", ""
            ).strip(),
            telegram_redirect_uri=redirect_uri,
            delivery_base_url=delivery_base,
            delivery_service_token=os.getenv(
                "MONITORING_DELIVERY_SERVICE_TOKEN", ""
            ).strip(),
            price_base_url=price_base,
            price_service_token=os.getenv(
                "MONITORING_PRICE_SERVICE_TOKEN", ""
            ).strip(),
            go_api_url=_https_url(
                "MONITORING_GO_API_URL",
                os.getenv("MONITORING_GO_API_URL", ""),
                required=False,
            ),
            go_api_token=os.getenv("MONITORING_GO_API_TOKEN", "").strip(),
        )

    def validate_auth(self) -> None:
        if not self.enabled:
            raise RuntimeError("monitoring_disabled")
        missing = []
        if not (self.manager_ids or self.admin_ids):
            missing.append("MONITORING_MANAGER_IDS|MONITORING_ADMIN_IDS")
        if not self.telegram_client_id:
            missing.append("MONITORING_TELEGRAM_CLIENT_ID")
        if not self.telegram_client_secret:
            missing.append("MONITORING_TELEGRAM_CLIENT_SECRET")
        if self.idle_ttl_seconds > self.session_ttl_seconds:
            missing.append("MONITORING_IDLE_TTL_SECONDS<=MONITORING_SESSION_TTL_SECONDS")
        if missing:
            raise RuntimeError("monitoring_auth_not_configured:" + ",".join(missing))

    def role_for(self, telegram_user_id: int) -> str | None:
        if telegram_user_id in self.admin_ids:
            return "admin"
        if telegram_user_id in self.manager_ids:
            return "manager"
        return None
