from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from .dates import TASHKENT_TZ
from .phones import normalize_uzbek_phone


MANAGERS_BY_CODE = {
    "olmas": "Olmas",
    "otabek": "Otabek",
    "ali": "Ali",
    "abbos": "Abbos",
}


def _phone_key(value: object) -> str | None:
    normalized = normalize_uzbek_phone(value)
    if normalized is None:
        return None
    return "".join(character for character in normalized if character.isdigit())


def _manager(code: object, name: object) -> str | None:
    by_code = MANAGERS_BY_CODE.get(str(code or "").strip().casefold())
    if by_code is not None:
        return by_code
    candidate = str(name or "").strip().casefold()
    return next(
        (
            manager
            for manager in MANAGERS_BY_CODE.values()
            if manager.casefold() == candidate
        ),
        None,
    )


@dataclass(frozen=True)
class CallRecord:
    id: int
    phone: str
    call_date: date
    manager: str


@dataclass(frozen=True)
class CallMatch:
    manager: str | None
    call_ids: tuple[int, ...]
    matched_count: int
    ambiguous: bool


@dataclass(frozen=True)
class CallIndex:
    records: tuple[CallRecord, ...]

    def match(self, phones: Iterable[str], sale_date: date) -> CallMatch:
        keys = frozenset(
            key for value in phones if (key := _phone_key(value)) is not None
        )
        matched = tuple(
            record
            for record in self.records
            if record.call_date == sale_date and record.phone in keys
        )
        managers = {record.manager for record in matched}
        return CallMatch(
            manager=next(iter(managers)) if len(managers) == 1 else None,
            call_ids=tuple(sorted({record.id for record in matched})),
            matched_count=len(matched),
            ambiguous=len(managers) > 1,
        )


class CallReader:
    """Read qualified client conversations from the live calls database."""

    REQUIRED_COLUMNS = {
        "id",
        "client_number",
        "client_key",
        "answered",
        "duration",
        "start_time",
        "duplicate_of_call_id",
        "is_internal_contact",
        "talk_manager_code",
        "talk_manager_name",
    }

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path.resolve()))}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA query_only=ON")
        return db

    def validate(self) -> None:
        with self._connect() as db:
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(calls)")
            }
        missing = self.REQUIRED_COLUMNS.difference(columns)
        if missing:
            raise RuntimeError("calls database schema is incomplete")

    def index(self, date_from: date, date_to: date) -> CallIndex:
        start = datetime.combine(date_from, time.min, tzinfo=TASHKENT_TZ)
        end = datetime.combine(
            date_to + timedelta(days=1),
            time.min,
            tzinfo=TASHKENT_TZ,
        )
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,client_number,client_key,start_time,
                          talk_manager_code,talk_manager_name
                   FROM calls
                   WHERE start_time>=? AND start_time<?
                     AND answered=1 AND duration>0
                     AND COALESCE(is_internal_contact,0)=0
                     AND duplicate_of_call_id IS NULL
                   ORDER BY start_time,id
                   LIMIT 10000""",
                (int(start.timestamp()), int(end.timestamp())),
            ).fetchall()
        records = []
        for row in rows:
            phone = _phone_key(row["client_key"]) or _phone_key(
                row["client_number"]
            )
            manager = _manager(
                row["talk_manager_code"],
                row["talk_manager_name"],
            )
            try:
                timestamp = int(row["start_time"])
            except (TypeError, ValueError):
                continue
            if phone is None or manager is None or timestamp <= 0:
                continue
            call_date = datetime.fromtimestamp(
                timestamp,
                tz=TASHKENT_TZ,
            ).date()
            records.append(
                CallRecord(
                    id=int(row["id"]),
                    phone=phone,
                    call_date=call_date,
                    manager=manager,
                )
            )
        return CallIndex(tuple(records))
