from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


FOLDER_CODES = ("NEW", "OLMAS", "OTABEK", "ALI", "ABBOS", "DONE")
DEFAULT_COLORS = {
    "NEW": 1,       # orange
    "OLMAS": 3,     # green
    "OTABEK": 5,    # blue
    "ALI": 2,       # violet
    "ABBOS": 4,     # cyan
    "DONE": 6,      # pink
}


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class FolderSpec:
    code: str
    title: str
    color: int

    def validate(self) -> None:
        if self.code not in FOLDER_CODES:
            raise ValueError(f"unsupported folder code: {self.code}")
        if not self.title or len(self.title) > 12 or any(ord(c) < 32 for c in self.title):
            raise ValueError(f"folder title for {self.code} must be 1-12 safe characters")
        if not 0 <= self.color <= 6:
            raise ValueError(f"folder color for {self.code} must be 0-6")


@dataclass(frozen=True)
class FolderSettings:
    enabled: bool
    api_id: int
    api_hash: str
    session_path: Path
    db_path: Path
    poll_seconds: int
    reconcile_seconds: int
    lease_seconds: int
    max_attempts: int
    backfill_existing: bool
    reopen_done: bool
    folders: tuple[FolderSpec, ...]

    @classmethod
    def load(cls) -> "FolderSettings":
        enabled = _bool("TELEGRAM_FOLDER_SYNC_ENABLED")
        api_id_raw = os.getenv("TELEGRAM_USER_API_ID", "").strip()
        try:
            api_id = int(api_id_raw) if api_id_raw else 0
        except ValueError as exc:
            if enabled:
                raise ValueError("TELEGRAM_USER_API_ID must be numeric") from exc
            api_id = 0

        folders = tuple(
            FolderSpec(
                code=code,
                title=os.getenv(f"TELEGRAM_FOLDER_{code}", code).strip(),
                color=_int(
                    f"TELEGRAM_FOLDER_{code}_COLOR",
                    DEFAULT_COLORS[code],
                    minimum=0,
                    maximum=6,
                ),
            )
            for code in FOLDER_CODES
        )
        settings = cls(
            enabled=enabled,
            api_id=api_id,
            api_hash=os.getenv("TELEGRAM_USER_API_HASH", "").strip(),
            session_path=Path(
                os.getenv(
                    "TELEGRAM_USER_SESSION_PATH",
                    "/app/data/texnikach-user.session",
                )
            ),
            db_path=Path(
                os.getenv("BUSINESS_DB_PATH", "/app/data/business_telegram.db")
            ),
            poll_seconds=_int(
                "TELEGRAM_FOLDER_POLL_SECONDS", 5, minimum=2, maximum=300
            ),
            reconcile_seconds=_int(
                "TELEGRAM_FOLDER_RECONCILE_SECONDS", 30,
                minimum=10, maximum=3600,
            ),
            lease_seconds=_int(
                "TELEGRAM_FOLDER_LEASE_SECONDS", 120,
                minimum=30, maximum=900,
            ),
            max_attempts=_int(
                "TELEGRAM_FOLDER_MAX_ATTEMPTS", 12,
                minimum=1, maximum=100,
            ),
            backfill_existing=_bool("TELEGRAM_FOLDER_BACKFILL_EXISTING"),
            reopen_done=_bool("TELEGRAM_FOLDER_REOPEN_DONE", True),
            folders=folders,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        for folder in self.folders:
            folder.validate()
        titles = [folder.title.casefold() for folder in self.folders]
        if len(set(titles)) != len(titles):
            raise ValueError("managed Telegram folder titles must be unique")
        if not self.enabled:
            return
        if self.api_id <= 0:
            raise RuntimeError("TELEGRAM_USER_API_ID is required when folder sync is enabled")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.api_hash):
            raise RuntimeError(
                "TELEGRAM_USER_API_HASH must be a 32-character hexadecimal secret"
            )
        if not self.session_path.is_absolute():
            raise RuntimeError("TELEGRAM_USER_SESSION_PATH must be absolute")
        if not self.db_path.is_absolute():
            raise RuntimeError("BUSINESS_DB_PATH must be absolute")

    @property
    def by_code(self) -> dict[str, FolderSpec]:
        return {folder.code: folder for folder in self.folders}
