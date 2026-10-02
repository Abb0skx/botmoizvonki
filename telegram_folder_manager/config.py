from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


FOLDER_CODES = ("NEW", "OLMAS", "OTABEK", "ALI", "ABBOS", "DONE", "SUPPLIER", "SUPPLIER2", "SUPPLIER3", "SUPPLIER4")
SUPPLIER_CODES = ("SUPPLIER", "SUPPLIER2", "SUPPLIER3", "SUPPLIER4")
DEFAULT_SUPPLIER_GROUP_IDS = (
    -1002188560435,  # Malika bozor N1
    -1001173906517,  # Malika Akses N1
    -1001463992108,  # MALIKA case No1
    -1002268274885,  # Malika DASTAVKA
    -1002480123950,  # MALIKA AKSESSUAR
    -1001607065824,  # ПАКЕТЛАР Б-44
    -1002496061682,  # BM Electronics Malika
)
DEFAULT_TITLES = {
    "SUPPLIER": "Поставщики", "SUPPLIER2": "Поставщики 2",
    "SUPPLIER3": "Поставщики 3", "SUPPLIER4": "Поставщики 4",
}
DEFAULT_COLORS = {
    "NEW": 1,       # orange
    "OLMAS": 3,     # green
    "OTABEK": 5,    # blue
    "ALI": 2,       # violet
    "ABBOS": 4,     # cyan
    "DONE": 6,      # pink
    "SUPPLIER": 0,  # red
    "SUPPLIER2": 0, # red
    "SUPPLIER3": 0, # red
    "SUPPLIER4": 0, # red
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
    supplier_sync_enabled: bool = False
    supplier_group_ids: tuple[int, ...] = DEFAULT_SUPPLIER_GROUP_IDS
    supplier_scan_seconds: int = 21600
    supplier_folder_capacity: int = 199

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
                title=os.getenv(f"TELEGRAM_FOLDER_{code}", DEFAULT_TITLES.get(code, code)).strip(),
                color=_int(
                    f"TELEGRAM_FOLDER_{code}_COLOR",
                    DEFAULT_COLORS[code],
                    minimum=0,
                    maximum=6,
                ),
            )
            for code in FOLDER_CODES
        )
        group_ids_raw = os.getenv("TELEGRAM_SUPPLIER_GROUP_IDS", "").strip()
        try:
            group_ids = (
                tuple(int(part.strip()) for part in group_ids_raw.split(",") if part.strip())
                if group_ids_raw else DEFAULT_SUPPLIER_GROUP_IDS
            )
        except ValueError as exc:
            raise ValueError("TELEGRAM_SUPPLIER_GROUP_IDS must be comma-separated numeric IDs") from exc
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
            supplier_sync_enabled=_bool("TELEGRAM_SUPPLIER_SYNC_ENABLED", enabled),
            supplier_group_ids=group_ids,
            supplier_scan_seconds=_int("TELEGRAM_SUPPLIER_SCAN_SECONDS", 21600, minimum=300, maximum=86400),
            supplier_folder_capacity=_int("TELEGRAM_SUPPLIER_FOLDER_CAPACITY", 199, minimum=1, maximum=199),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        for folder in self.folders:
            folder.validate()
        titles = [folder.title.casefold() for folder in self.folders]
        if len(set(titles)) != len(titles):
            raise ValueError("managed Telegram folder titles must be unique")
        if self.supplier_sync_enabled and (
            not self.enabled or not self.supplier_group_ids
            or any(group_id >= 0 for group_id in self.supplier_group_ids)
            or len(set(self.supplier_group_ids)) != len(self.supplier_group_ids)
        ):
            raise ValueError("supplier sync needs enabled folders and unique negative group IDs")
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
