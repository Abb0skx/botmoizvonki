from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from cryptography.fernet import Fernet, InvalidToken


UTC = timezone.utc
TASHKENT_TZ = timezone(timedelta(hours=5))
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class PendingDeletion:
    chat_id: int
    source_message_id: int
    replacement_message_id: int
    attempts: int
    updated_at: datetime


@dataclass(frozen=True)
class PendingPhoto:
    chat_id: int
    source_message_id: int
    source_file_id: str
    source_file_ids: tuple[str, ...]
    source_message_ids: tuple[int, ...]
    source_kind: str
    client_caption: str | None
    message_thread_id: int | None
    attempts: int
    updated_at: datetime
    sale_date: date


@dataclass(frozen=True)
class PendingOrderBackfill:
    chat_id: int
    source_message_id: int
    replacement_message_id: int
    sale_date: date
    order_id: int
    attempts: int
    updated_at: datetime


@dataclass(frozen=True)
class PendingPriceBackfill:
    chat_id: int
    source_message_id: int
    replacement_message_id: int
    attempts: int
    updated_at: datetime


@dataclass(frozen=True)
class OrderAuditCandidate:
    chat_id: int
    source_message_id: int
    replacement_message_id: int
    sale_date: date
    order_id: int


@dataclass(frozen=True)
class UnreconciledPhoto:
    chat_id: int
    source_message_id: int
    source_file_unique_id: str


@dataclass(frozen=True)
class DeliveryLink:
    chat_id: int
    source_message_id: int
    replacement_message_id: int
    delivery_order_id: int
    delivery_order_number: int
    auto_manager_name: str | None
    manager_manual_override: bool


@dataclass(frozen=True)
class AutoCorrectionState:
    last_new_at: datetime | None = None
    event_generation: int = 0
    completed_event_generation: int = 0
    schedule_done_through: datetime | None = None


@dataclass(frozen=True)
class PendingDuplicate:
    chat_id: int
    message_id: int
    attempts: int
    updated_at: datetime


class SalesPhotoRepository:
    """Small durable ledger for safe reposting and callback idempotency."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher, self._signing_key = self._load_cipher()
        self._migrate()

    def _database_has_key_dependent_state(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with sqlite3.connect(self.path, timeout=5) as db:
                table = db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='sales_photo_jobs'"
                ).fetchone()
                if table is None:
                    return False
                meta_table = db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='sales_photo_meta'"
                ).fetchone()
                if meta_table is not None and db.execute(
                    "SELECT 1 FROM sales_photo_meta "
                    "WHERE key='cipher_key_fingerprint'"
                ).fetchone() is not None:
                    return True
                columns = {
                    str(row[1])
                    for row in db.execute(
                        "PRAGMA table_info(sales_photo_jobs)"
                    ).fetchall()
                }
                if "encrypted_payload" in columns and db.execute(
                    "SELECT 1 FROM sales_photo_jobs "
                    "WHERE encrypted_payload IS NOT NULL LIMIT 1"
                ).fetchone() is not None:
                    return True
                if "ui_generation" not in columns:
                    return False
                return (
                    db.execute(
                        "SELECT 1 FROM sales_photo_jobs "
                        "LIMIT 1"
                    ).fetchone()
                    is not None
                )
        except sqlite3.Error as exc:
            raise RuntimeError(
                "Не удалось проверить retry-хранилище без ключа"
            ) from exc

    @staticmethod
    def _read_key(key_path: Path) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(key_path, flags)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 128)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > 128:
                    raise RuntimeError("Некорректный размер ключа retry-хранилища")
            return b"".join(chunks).strip()
        finally:
            os.close(descriptor)

    def _load_cipher(self) -> tuple[Fernet, bytes]:
        key_path = self.path.with_name(f"{self.path.name}.key")
        try:
            key = self._read_key(key_path)
        except FileNotFoundError:
            if self._database_has_key_dependent_state():
                raise RuntimeError(
                    "Отсутствует ключ существующего sales-photo хранилища"
                )
            key = Fernet.generate_key()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(key_path, flags, 0o600)
            except FileExistsError:
                key = self._read_key(key_path)
            else:
                try:
                    remaining = memoryview(key)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("Не удалось полностью записать ключ")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory_fd = os.open(key_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        try:
            os.chmod(key_path, 0o600)
            return Fernet(key), key
        except (OSError, ValueError) as exc:
            raise RuntimeError("Не удалось загрузить ключ retry-хранилища") from exc

    def callback_signature(
        self,
        chat_id: int,
        source_message_id: int,
        generation: int,
    ) -> str:
        payload = (
            f"sales-photo:{int(chat_id)}:{int(source_message_id)}:"
            f"{max(0, int(generation))}"
        ).encode()
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()[:12]

    def valid_callback_signature(
        self,
        chat_id: int,
        source_message_id: int,
        generation: int,
        signature: str,
    ) -> bool:
        expected = self.callback_signature(chat_id, source_message_id, generation)
        return hmac.compare_digest(expected, str(signature or ""))

    def _encrypt_payload(
        self,
        source_file_id: str,
        client_caption: str | None,
        message_thread_id: int | None,
        *,
        source_file_ids: tuple[str, ...] | None = None,
        source_message_ids: tuple[int, ...] | None = None,
        source_kind: str = "photo",
    ) -> bytes:
        file_ids = tuple(
            str(value or "")[:300]
            for value in (source_file_ids or (source_file_id,))
            if value
        )
        payload = json.dumps(
            {
                # Keep file_id for backwards readability while the tuple fields
                # make album retries durable.
                "file_id": file_ids[0] if file_ids else "",
                "file_ids": file_ids,
                "source_message_ids": tuple(
                    int(value) for value in (source_message_ids or ())
                ),
                "kind": str(source_kind or "photo")[:20],
                "caption": (
                    " ".join(str(client_caption or "").split())[:300]
                    if client_caption
                    else None
                ),
                "thread_id": (
                    int(message_thread_id) if message_thread_id is not None else None
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._cipher.encrypt(payload)

    def _decrypt_payload(
        self,
        payload: bytes,
    ) -> tuple[tuple[str, ...], str | None, int | None, str, tuple[int, ...]]:
        try:
            value = json.loads(self._cipher.decrypt(bytes(payload)).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Повреждён retry payload") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Повреждён retry payload")
        raw_file_ids = value.get("file_ids")
        if isinstance(raw_file_ids, list):
            file_ids = tuple(
                str(item or "")[:300] for item in raw_file_ids if item
            )
        else:
            legacy_file_id = str(value.get("file_id") or "")[:300]
            file_ids = (legacy_file_id,) if legacy_file_id else ()
        caption_value = value.get("caption")
        caption = str(caption_value)[:300] if caption_value else None
        thread_value = value.get("thread_id")
        thread_id = int(thread_value) if thread_value is not None else None
        source_kind = str(value.get("kind") or "photo")[:20]
        raw_source_ids = value.get("source_message_ids")
        source_message_ids = (
            tuple(int(item) for item in raw_source_ids)
            if isinstance(raw_source_ids, list)
            else ()
        )
        if not file_ids:
            raise RuntimeError("Retry payload не содержит Telegram file_id")
        return file_ids, caption, thread_id, source_kind, source_message_ids

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sales_photo_jobs (
                    chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    source_file_unique_id TEXT NOT NULL,
                    encrypted_payload BLOB,
                    replacement_message_id INTEGER,
                    manager TEXT,
                    ui_generation INTEGER NOT NULL DEFAULT 0,
                    sale_date TEXT,
                    daily_order_id INTEGER,
                    order_card_applied INTEGER NOT NULL DEFAULT 0,
                    order_backfill_attempts INTEGER NOT NULL DEFAULT 0,
                    price_card_applied INTEGER NOT NULL DEFAULT 0,
                    price_backfill_attempts INTEGER NOT NULL DEFAULT 0,
                    order_removed INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN (
                        'processing','reposted','delete_pending','complete','failed'
                    )),
                    delete_attempts INTEGER NOT NULL DEFAULT 0,
                    processing_attempts INTEGER NOT NULL DEFAULT 1,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, source_message_id),
                    UNIQUE(chat_id, replacement_message_id)
                );

                CREATE TABLE IF NOT EXISTS sales_photo_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sales_photo_duplicate_cleanup (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,message_id)
                );

                CREATE TABLE IF NOT EXISTS sales_photo_source_members (
                    chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    member_message_id INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,source_message_id,member_message_id),
                    UNIQUE(chat_id,member_message_id)
                );

                CREATE TABLE IF NOT EXISTS sales_photo_output_members (
                    chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,source_message_id,message_id),
                    UNIQUE(chat_id,message_id)
                );

                CREATE TABLE IF NOT EXISTS sales_photo_delivery_links (
                    chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    delivery_order_id INTEGER NOT NULL,
                    delivery_order_number INTEGER NOT NULL,
                    matched_phone TEXT NOT NULL,
                    sale_date TEXT NOT NULL,
                    auto_manager_name TEXT,
                    manager_manual_override INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,source_message_id),
                    FOREIGN KEY(chat_id,source_message_id)
                        REFERENCES sales_photo_jobs(chat_id,source_message_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sales_photo_delivery_order
                ON sales_photo_delivery_links(chat_id,delivery_order_id);

                DROP TABLE IF EXISTS sales_photo_name_cache;
                """
            )
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(sales_photo_jobs)").fetchall()
            }
            if "manager" not in columns:
                db.execute("ALTER TABLE sales_photo_jobs ADD COLUMN manager TEXT")
            if "ui_generation" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "ui_generation INTEGER NOT NULL DEFAULT 0"
                )
            if "encrypted_payload" not in columns:
                db.execute("ALTER TABLE sales_photo_jobs ADD COLUMN encrypted_payload BLOB")
            if "processing_attempts" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "processing_attempts INTEGER NOT NULL DEFAULT 1"
                )
            if "sale_date" not in columns:
                db.execute("ALTER TABLE sales_photo_jobs ADD COLUMN sale_date TEXT")
            if "daily_order_id" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN daily_order_id INTEGER"
                )
            if "order_card_applied" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "order_card_applied INTEGER NOT NULL DEFAULT 0"
                )
            if "order_backfill_attempts" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "order_backfill_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "price_card_applied" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "price_card_applied INTEGER NOT NULL DEFAULT 0"
                )
            if "price_backfill_attempts" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "price_backfill_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "order_removed" not in columns:
                db.execute(
                    "ALTER TABLE sales_photo_jobs ADD COLUMN "
                    "order_removed INTEGER NOT NULL DEFAULT 0"
                )
            legacy_rows = db.execute(
                """SELECT chat_id,source_message_id,created_at
                   FROM sales_photo_jobs
                   WHERE replacement_message_id IS NOT NULL
                     AND order_removed=0
                     AND (sale_date IS NULL OR daily_order_id IS NULL)
                   ORDER BY created_at,source_message_id"""
            ).fetchall()
            counters = {
                (int(row["chat_id"]), str(row["sale_date"])): int(row["maximum"])
                for row in db.execute(
                    """SELECT chat_id,sale_date,MAX(daily_order_id) AS maximum
                       FROM sales_photo_jobs
                       WHERE sale_date IS NOT NULL AND daily_order_id IS NOT NULL
                         AND order_removed=0
                       GROUP BY chat_id,sale_date"""
                ).fetchall()
            }
            for legacy_row in legacy_rows:
                try:
                    created_at = datetime.fromisoformat(
                        str(legacy_row["created_at"])
                    )
                except ValueError:
                    created_at = utc_now()
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                sale_day = created_at.astimezone(TASHKENT_TZ).date().isoformat()
                counter_key = (int(legacy_row["chat_id"]), sale_day)
                next_id = counters.get(counter_key, 0) + 1
                counters[counter_key] = next_id
                db.execute(
                    """UPDATE sales_photo_jobs
                       SET sale_date=?,daily_order_id=?,order_card_applied=0
                       WHERE chat_id=? AND source_message_id=?""",
                    (
                        sale_day,
                        next_id,
                        int(legacy_row["chat_id"]),
                        int(legacy_row["source_message_id"]),
                    ),
                )
            migration_time = _iso(utc_now())
            db.execute(
                """INSERT OR IGNORE INTO sales_photo_source_members(
                       chat_id,source_message_id,member_message_id,
                       deleted,created_at,updated_at
                   )
                   SELECT chat_id,source_message_id,source_message_id,
                          CASE WHEN status='complete' THEN 1 ELSE 0 END,?,?
                   FROM sales_photo_jobs""",
                (migration_time, migration_time),
            )
            db.execute(
                """INSERT OR IGNORE INTO sales_photo_output_members(
                       chat_id,source_message_id,message_id,created_at
                   )
                   SELECT chat_id,source_message_id,replacement_message_id,?
                   FROM sales_photo_jobs
                   WHERE replacement_message_id IS NOT NULL""",
                (migration_time,),
            )
            key_fingerprint = hashlib.sha256(self._signing_key).hexdigest()
            fingerprint_row = db.execute(
                "SELECT value FROM sales_photo_meta WHERE key='cipher_key_fingerprint'"
            ).fetchone()
            if fingerprint_row is not None and not hmac.compare_digest(
                str(fingerprint_row["value"]),
                key_fingerprint,
            ):
                raise RuntimeError("Ключ sales-photo хранилища не совпадает")
            if fingerprint_row is None:
                encrypted_rows = db.execute(
                    "SELECT encrypted_payload FROM sales_photo_jobs "
                    "WHERE encrypted_payload IS NOT NULL"
                ).fetchall()
                try:
                    for encrypted_row in encrypted_rows:
                        self._cipher.decrypt(bytes(encrypted_row["encrypted_payload"]))
                except InvalidToken as exc:
                    raise RuntimeError(
                        "Нельзя подтвердить ключ существующего sales-photo хранилища"
                    ) from exc
                db.execute(
                    """INSERT INTO sales_photo_meta(key,value,updated_at)
                       VALUES('cipher_key_fingerprint',?,?)""",
                    (key_fingerprint, _iso(utc_now())),
                )
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_sales_photo_jobs_delete
                ON sales_photo_jobs(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_sales_photo_jobs_retry
                ON sales_photo_jobs(status, processing_attempts, updated_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_photo_daily_order
                ON sales_photo_jobs(chat_id,sale_date,daily_order_id)
                WHERE sale_date IS NOT NULL AND daily_order_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_sales_photo_order_backfill
                ON sales_photo_jobs(order_card_applied,order_backfill_attempts,updated_at);

                CREATE INDEX IF NOT EXISTS idx_sales_photo_price_backfill
                ON sales_photo_jobs(price_card_applied,price_backfill_attempts,updated_at);

                CREATE INDEX IF NOT EXISTS idx_sales_photo_recent_cards
                ON sales_photo_jobs(chat_id,sale_date,order_removed,daily_order_id);
                """
            )
            db.commit()

    @staticmethod
    def _bootstrap_key(bot_id: int, chat_id: int) -> str:
        return f"polling_bootstrapped:{int(bot_id)}:{int(chat_id)}"

    @staticmethod
    def _auto_correction_key(kind: str, chat_id: int) -> str:
        return f"auto_correction:{str(kind)}:{int(chat_id)}"

    @staticmethod
    def _state_datetime(value: object) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def auto_correction_state(self, chat_id: int) -> AutoCorrectionState:
        keys = {
            "last_new": self._auto_correction_key("last_new", chat_id),
            "event_generation": self._auto_correction_key(
                "event_generation", chat_id
            ),
            "completed_event_generation": self._auto_correction_key(
                "completed_event_generation", chat_id
            ),
            "schedule_done": self._auto_correction_key("schedule_done", chat_id),
        }
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT key,value FROM sales_photo_meta WHERE key IN ({placeholders})",
                tuple(keys.values()),
            ).fetchall()
        by_key = {str(row["key"]): row["value"] for row in rows}
        try:
            event_generation = max(
                0,
                int(by_key.get(keys["event_generation"], 0)),
            )
        except (TypeError, ValueError):
            event_generation = 0
        try:
            completed_event_generation = max(
                0,
                int(by_key.get(keys["completed_event_generation"], 0)),
            )
        except (TypeError, ValueError):
            completed_event_generation = 0
        return AutoCorrectionState(
            last_new_at=self._state_datetime(by_key.get(keys["last_new"])),
            event_generation=event_generation,
            completed_event_generation=completed_event_generation,
            schedule_done_through=self._state_datetime(
                by_key.get(keys["schedule_done"])
            ),
        )

    def _advance_auto_correction_state(
        self,
        kind: str,
        chat_id: int,
        value: datetime,
    ) -> None:
        canonical = _iso(value)
        key = self._auto_correction_key(kind, chat_id)
        with self._connect() as db:
            db.execute(
                """INSERT INTO sales_photo_meta(key,value,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,updated_at=excluded.updated_at
                   WHERE sales_photo_meta.value < excluded.value""",
                (key, canonical, _iso(utc_now())),
            )
            db.commit()

    def note_auto_correction_new_card(
        self,
        chat_id: int,
        at: datetime | None = None,
    ) -> datetime:
        event_at = at or utc_now()
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=UTC)
        event_at = event_at.astimezone(UTC)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._note_auto_correction_new_card_locked(db, chat_id, event_at)
            db.commit()
        return event_at

    def _note_auto_correction_new_card_locked(
        self,
        db: sqlite3.Connection,
        chat_id: int,
        event_at: datetime,
    ) -> None:
        now = _iso(utc_now())
        db.execute(
            """INSERT INTO sales_photo_meta(key,value,updated_at)
               VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,updated_at=excluded.updated_at""",
            (
                self._auto_correction_key("last_new", chat_id),
                _iso(event_at),
                now,
            ),
        )
        db.execute(
            """INSERT INTO sales_photo_meta(key,value,updated_at)
               VALUES(?, '1', ?)
               ON CONFLICT(key) DO UPDATE SET
                   value=CAST(sales_photo_meta.value AS INTEGER)+1,
                   updated_at=excluded.updated_at""",
            (
                self._auto_correction_key("event_generation", chat_id),
                now,
            ),
        )

    def mark_auto_correction_complete(
        self,
        chat_id: int,
        *,
        completed_event_generation: int | None = None,
        schedule_done_through: datetime | None = None,
    ) -> None:
        if completed_event_generation is not None:
            generation = max(0, int(completed_event_generation))
            key = self._auto_correction_key(
                "completed_event_generation",
                chat_id,
            )
            with self._connect() as db:
                db.execute(
                    """INSERT INTO sales_photo_meta(key,value,updated_at)
                       VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value,updated_at=excluded.updated_at
                       WHERE CAST(sales_photo_meta.value AS INTEGER)
                             < CAST(excluded.value AS INTEGER)""",
                    (key, str(generation), _iso(utc_now())),
                )
                db.commit()
        if schedule_done_through is not None:
            self._advance_auto_correction_state(
                "schedule_done",
                chat_id,
                schedule_done_through,
            )

    def delivery_event_cursor(self, chat_id: int) -> int | None:
        key = f"delivery:last_event_id:{int(chat_id)}"
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM sales_photo_meta WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return max(0, int(row["value"]))
        except (TypeError, ValueError):
            return None

    def set_delivery_event_cursor(self, chat_id: int, event_id: int) -> None:
        key = f"delivery:last_event_id:{int(chat_id)}"
        with self._connect() as db:
            db.execute(
                """INSERT INTO sales_photo_meta(key,value,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,updated_at=excluded.updated_at
                   WHERE CAST(sales_photo_meta.value AS INTEGER)
                         < CAST(excluded.value AS INTEGER)""",
                (key, str(max(0, int(event_id))), _iso(utc_now())),
            )
            db.commit()

    def is_bootstrapped(self, bot_id: int, chat_id: int) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM sales_photo_meta WHERE key=?",
                (self._bootstrap_key(bot_id, chat_id),),
            ).fetchone()
        return row is not None

    def mark_bootstrapped(
        self,
        bot_id: int,
        chat_id: int,
        at: datetime | None = None,
    ) -> None:
        now = _iso(at or utc_now())
        key = self._bootstrap_key(bot_id, chat_id)
        with self._connect() as db:
            db.execute(
                """INSERT INTO sales_photo_meta(key,value,updated_at)
                   VALUES(?,'1',?)
                   ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at""",
                (key, now),
            )
            db.commit()

    def claim_photo(
        self,
        chat_id: int,
        source_message_id: int,
        source_file_unique_id: str,
        source_file_id: str | None = None,
        client_caption: str | None = None,
        message_thread_id: int | None = None,
        at: datetime | None = None,
        *,
        source_file_ids: tuple[str, ...] | None = None,
        source_message_ids: tuple[int, ...] | None = None,
        source_kind: str = "photo",
        sale_date: date | None = None,
        allocate_order: bool = True,
    ) -> bool:
        now = _iso(at or utc_now())
        member_ids = tuple(
            dict.fromkeys(
                int(value)
                for value in (source_message_ids or (source_message_id,))
            )
        )
        if int(source_message_id) not in member_ids:
            member_ids = (int(source_message_id), *member_ids)
        file_ids = tuple(
            str(value or "")[:300]
            for value in (source_file_ids or ())
            if value
        )
        if not file_ids and source_file_id:
            file_ids = (str(source_file_id)[:300],)
        encrypted_payload = (
            self._encrypt_payload(
                file_ids[0],
                client_caption,
                message_thread_id,
                source_file_ids=file_ids,
                source_message_ids=member_ids,
                source_kind=source_kind,
            )
            if file_ids
            else None
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in member_ids)
            conflicting_member = db.execute(
                f"""SELECT 1 FROM sales_photo_source_members
                       WHERE chat_id=? AND member_message_id IN ({placeholders})
                         AND source_message_id<>? LIMIT 1""",
                (int(chat_id), *member_ids, int(source_message_id)),
            ).fetchone()
            if conflicting_member is not None:
                return False
            try:
                db.execute(
                    """INSERT INTO sales_photo_jobs(
                           chat_id,source_message_id,source_file_unique_id,
                           encrypted_payload,status,last_error_code,
                           created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        int(chat_id),
                        int(source_message_id),
                        str(source_file_unique_id or "unknown")[:200],
                        encrypted_payload,
                        "processing",
                        "before_send",
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                cursor = db.execute(
                    """UPDATE sales_photo_jobs
                       SET status='processing',last_error_code='before_send',
                           encrypted_payload=COALESCE(?,encrypted_payload),
                           processing_attempts=processing_attempts+1,updated_at=?
                       WHERE chat_id=? AND source_message_id=?
                         AND source_file_unique_id=? AND status='failed'
                         AND replacement_message_id IS NULL
                         AND processing_attempts<3""",
                    (
                        encrypted_payload,
                        now,
                        int(chat_id),
                        int(source_message_id),
                        str(source_file_unique_id or "unknown")[:200],
                    ),
                )
                if cursor.rowcount != 1:
                    db.rollback()
                    return False
            if sale_date is not None and allocate_order:
                self._ensure_daily_order_locked(
                    db,
                    int(chat_id),
                    int(source_message_id),
                    sale_date,
                )
            elif sale_date is not None:
                db.execute(
                    """UPDATE sales_photo_jobs
                       SET sale_date=COALESCE(sale_date,?)
                       WHERE chat_id=? AND source_message_id=?""",
                    (
                        sale_date.isoformat(),
                        int(chat_id),
                        int(source_message_id),
                    ),
                )
            try:
                db.executemany(
                    """INSERT INTO sales_photo_source_members(
                           chat_id,source_message_id,member_message_id,
                           deleted,created_at,updated_at
                       ) VALUES (?,?,?,0,?,?)
                       ON CONFLICT(chat_id,source_message_id,member_message_id)
                       DO UPDATE SET deleted=0,updated_at=excluded.updated_at""",
                    (
                        (
                            int(chat_id),
                            int(source_message_id),
                            member_id,
                            now,
                            now,
                        )
                        for member_id in member_ids
                    ),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                return False
            db.commit()
            return True

    @staticmethod
    def _ensure_daily_order_locked(
        db: sqlite3.Connection,
        chat_id: int,
        source_message_id: int,
        sale_date: date,
    ) -> tuple[date, int]:
        row = db.execute(
            """SELECT sale_date,daily_order_id FROM sales_photo_jobs
               WHERE chat_id=? AND source_message_id=?""",
            (int(chat_id), int(source_message_id)),
        ).fetchone()
        if row is None:
            raise RuntimeError("Не найдена карточка для ежедневного номера")
        effective_date = (
            date.fromisoformat(str(row["sale_date"]))
            if row["sale_date"] is not None
            else sale_date
        )
        sale_day = effective_date.isoformat()
        cursor = db.execute(
            """UPDATE sales_photo_jobs
               SET sale_date=?
               WHERE chat_id=? AND source_message_id=?
                 AND (sale_date IS NULL OR sale_date=?)""",
            (
                sale_day,
                int(chat_id),
                int(source_message_id),
                sale_day,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Не удалось назначить ежедневный номер")

        # Failed/unpublished attempts are not sold items and must never reserve
        # a visible number for the next successful card.
        db.execute(
            """UPDATE sales_photo_jobs
               SET daily_order_id=NULL,order_card_applied=0
               WHERE chat_id=? AND sale_date=? AND order_removed=0
                 AND replacement_message_id IS NULL AND status<>'processing'
                 AND daily_order_id IS NOT NULL""",
            (int(chat_id), sale_day),
        )
        active_rows = db.execute(
            """SELECT source_message_id,daily_order_id,order_card_applied
               FROM sales_photo_jobs
               WHERE chat_id=? AND sale_date=? AND order_removed=0
                 AND (replacement_message_id IS NOT NULL OR status='processing')
               ORDER BY CASE WHEN daily_order_id IS NULL THEN 1 ELSE 0 END,
                        daily_order_id,created_at,source_message_id""",
            (int(chat_id), sale_day),
        ).fetchall()
        target_order: int | None = None
        needs_compaction = any(
            active_row["daily_order_id"] is None
            or int(active_row["daily_order_id"]) != expected
            for expected, active_row in enumerate(active_rows, start=1)
        )
        if needs_compaction:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET daily_order_id=NULL
                   WHERE chat_id=? AND sale_date=? AND order_removed=0
                     AND (replacement_message_id IS NOT NULL OR status='processing')""",
                (int(chat_id), sale_day),
            )
            for expected, active_row in enumerate(active_rows, start=1):
                db.execute(
                    """UPDATE sales_photo_jobs
                       SET daily_order_id=?,order_card_applied=?
                       WHERE chat_id=? AND source_message_id=?
                         AND order_removed=0
                         AND (replacement_message_id IS NOT NULL OR status='processing')""",
                    (
                        expected,
                        (
                            0
                            if active_row["daily_order_id"] is None
                            or int(active_row["daily_order_id"]) != expected
                            else int(active_row["order_card_applied"])
                        ),
                        int(chat_id),
                        int(active_row["source_message_id"]),
                    ),
                )
                if int(active_row["source_message_id"]) == int(source_message_id):
                    target_order = expected
        else:
            for expected, active_row in enumerate(active_rows, start=1):
                if int(active_row["source_message_id"]) == int(source_message_id):
                    target_order = expected
                    break
        if target_order is None:
            raise RuntimeError("Не удалось включить карточку в дневной счётчик")
        return effective_date, target_order

    def ensure_daily_order(
        self,
        chat_id: int,
        source_message_id: int,
        sale_date: date,
    ) -> tuple[date, int]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._ensure_daily_order_locked(
                db,
                int(chat_id),
                int(source_message_id),
                sale_date,
            )
            db.commit()
        return result

    def daily_order_for_source(
        self,
        chat_id: int,
        source_message_id: int,
    ) -> tuple[date, int] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT sale_date,daily_order_id FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
        if row is None or row["sale_date"] is None or row["daily_order_id"] is None:
            return None
        return date.fromisoformat(str(row["sale_date"])), int(row["daily_order_id"])

    def daily_order_for_replacement(
        self,
        chat_id: int,
        replacement_message_id: int,
    ) -> tuple[date, int] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT sale_date,daily_order_id FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id=?""",
                (int(chat_id), int(replacement_message_id)),
            ).fetchone()
        if row is None or row["sale_date"] is None or row["daily_order_id"] is None:
            return None
        return date.fromisoformat(str(row["sale_date"])), int(row["daily_order_id"])

    def mark_order_card_applied(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET order_card_applied=1,order_backfill_attempts=0,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND replacement_message_id IS NOT NULL
                     AND daily_order_id IS NOT NULL""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
        return cursor.rowcount == 1

    def pending_order_backfills(
        self,
        chat_id: int,
        limit: int = 50,
        *,
        sale_date_from: date | None = None,
        sale_date_to: date | None = None,
    ) -> tuple[PendingOrderBackfill, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,replacement_message_id,
                          sale_date,daily_order_id,order_backfill_attempts,updated_at
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id IS NOT NULL
                     AND sale_date IS NOT NULL AND daily_order_id IS NOT NULL
                     AND order_removed=0
                     AND order_card_applied=0 AND order_backfill_attempts<8
                     AND (? IS NULL OR sale_date>=?)
                     AND (? IS NULL OR sale_date<=?)
                   ORDER BY created_at,source_message_id
                   LIMIT ?""",
                (
                    int(chat_id),
                    sale_date_from.isoformat() if sale_date_from else None,
                    sale_date_from.isoformat() if sale_date_from else None,
                    sale_date_to.isoformat() if sale_date_to else None,
                    sale_date_to.isoformat() if sale_date_to else None,
                    max(1, min(int(limit), 200)),
                ),
            ).fetchall()
        return tuple(
            PendingOrderBackfill(
                chat_id=int(row["chat_id"]),
                source_message_id=int(row["source_message_id"]),
                replacement_message_id=int(row["replacement_message_id"]),
                sale_date=date.fromisoformat(str(row["sale_date"])),
                order_id=int(row["daily_order_id"]),
                attempts=int(row["order_backfill_attempts"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        )

    def mark_price_card_applied(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET price_card_applied=1,price_backfill_attempts=0,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND replacement_message_id IS NOT NULL""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
        return cursor.rowcount == 1

    def pending_price_backfills(
        self,
        chat_id: int,
        limit: int = 50,
        *,
        sale_date_from: date | None = None,
        sale_date_to: date | None = None,
    ) -> tuple[PendingPriceBackfill, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,replacement_message_id,
                          price_backfill_attempts,updated_at
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id IS NOT NULL
                     AND order_removed=0
                     AND price_card_applied=0 AND price_backfill_attempts<8
                     AND (? IS NULL OR sale_date>=?)
                     AND (? IS NULL OR sale_date<=?)
                   ORDER BY created_at,source_message_id
                   LIMIT ?""",
                (
                    int(chat_id),
                    sale_date_from.isoformat() if sale_date_from else None,
                    sale_date_from.isoformat() if sale_date_from else None,
                    sale_date_to.isoformat() if sale_date_to else None,
                    sale_date_to.isoformat() if sale_date_to else None,
                    max(1, min(int(limit), 200)),
                ),
            ).fetchall()
        return tuple(
            PendingPriceBackfill(
                chat_id=int(row["chat_id"]),
                source_message_id=int(row["source_message_id"]),
                replacement_message_id=int(row["replacement_message_id"]),
                attempts=int(row["price_backfill_attempts"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        )

    def order_audit_candidates(
        self,
        chat_id: int,
        limit: int = 500,
        *,
        sale_date_from: date | None = None,
        sale_date_to: date | None = None,
    ) -> tuple[OrderAuditCandidate, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,replacement_message_id,
                          sale_date,daily_order_id
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id IS NOT NULL
                     AND sale_date IS NOT NULL AND daily_order_id IS NOT NULL
                     AND order_removed=0
                     AND (? IS NULL OR sale_date>=?)
                     AND (? IS NULL OR sale_date<=?)
                   ORDER BY sale_date,daily_order_id,created_at,source_message_id
                   LIMIT ?""",
                (
                    int(chat_id),
                    sale_date_from.isoformat() if sale_date_from else None,
                    sale_date_from.isoformat() if sale_date_from else None,
                    sale_date_to.isoformat() if sale_date_to else None,
                    sale_date_to.isoformat() if sale_date_to else None,
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
        return tuple(
            OrderAuditCandidate(
                chat_id=int(row["chat_id"]),
                source_message_id=int(row["source_message_id"]),
                replacement_message_id=int(row["replacement_message_id"]),
                sale_date=date.fromisoformat(str(row["sale_date"])),
                order_id=int(row["daily_order_id"]),
            )
            for row in rows
        )

    def auto_correction_candidates(
        self,
        chat_id: int,
        sale_date_from: date,
        sale_date_to: date,
        limit: int = 2000,
    ) -> tuple[OrderAuditCandidate, ...]:
        return self.order_audit_candidates(
            chat_id,
            limit=limit,
            sale_date_from=sale_date_from,
            sale_date_to=sale_date_to,
        )

    def unreconciled_recent_photos(
        self,
        chat_id: int,
        sale_date_from: date,
        sale_date_to: date,
        limit: int = 100,
    ) -> tuple[UnreconciledPhoto, ...]:
        """Return photo sends whose Telegram result may have been lost."""

        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,source_file_unique_id
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id IS NULL
                     AND order_removed=0 AND status IN ('processing','failed')
                     AND sale_date>=? AND sale_date<=?
                     AND source_file_unique_id NOT LIKE 'text:%'
                   ORDER BY created_at,source_message_id
                   LIMIT ?""",
                (
                    int(chat_id),
                    sale_date_from.isoformat(),
                    sale_date_to.isoformat(),
                    max(1, min(int(limit), 500)),
                ),
            ).fetchall()
        return tuple(
            UnreconciledPhoto(
                chat_id=int(row["chat_id"]),
                source_message_id=int(row["source_message_id"]),
                source_file_unique_id=str(row["source_file_unique_id"]),
            )
            for row in rows
        )

    def tracked_message_ids(self, chat_id: int) -> frozenset[int]:
        """Return message IDs already owned by the durable ledger."""

        with self._connect() as db:
            rows = db.execute(
                """SELECT source_message_id AS message_id
                     FROM sales_photo_jobs WHERE chat_id=?
                   UNION
                   SELECT replacement_message_id AS message_id
                     FROM sales_photo_jobs
                     WHERE chat_id=? AND replacement_message_id IS NOT NULL
                   UNION
                   SELECT member_message_id AS message_id
                     FROM sales_photo_source_members WHERE chat_id=?
                   UNION
                   SELECT message_id FROM sales_photo_output_members
                     WHERE chat_id=?""",
                (int(chat_id), int(chat_id), int(chat_id), int(chat_id)),
            ).fetchall()
        return frozenset(int(row["message_id"]) for row in rows)

    def upsert_delivery_link(
        self,
        chat_id: int,
        source_message_id: int,
        *,
        delivery_order_id: int,
        delivery_order_number: int,
        matched_phone: str,
        sale_date: date,
        at: datetime | None = None,
    ) -> None:
        now = _iso(at or utc_now())
        with self._connect() as db:
            db.execute(
                """INSERT INTO sales_photo_delivery_links(
                       chat_id,source_message_id,delivery_order_id,
                       delivery_order_number,matched_phone,sale_date,
                       created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(chat_id,source_message_id) DO UPDATE SET
                       delivery_order_id=excluded.delivery_order_id,
                       delivery_order_number=excluded.delivery_order_number,
                       matched_phone=excluded.matched_phone,
                       sale_date=excluded.sale_date,
                       updated_at=excluded.updated_at""",
                (
                    int(chat_id),
                    int(source_message_id),
                    int(delivery_order_id),
                    int(delivery_order_number),
                    str(matched_phone)[:32],
                    sale_date.isoformat(),
                    now,
                    now,
                ),
            )
            db.commit()

    def sync_auto_delivery_manager(
        self,
        chat_id: int,
        source_message_id: int,
        desired_manager: str | None,
        at: datetime | None = None,
    ) -> tuple[int | None, bool]:
        """Apply a delivery manager unless a human has taken ownership."""

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT job.replacement_message_id,job.manager,
                          job.ui_generation,link.auto_manager_name,
                          link.manager_manual_override
                   FROM sales_photo_jobs AS job
                   JOIN sales_photo_delivery_links AS link
                     ON link.chat_id=job.chat_id
                    AND link.source_message_id=job.source_message_id
                   WHERE job.chat_id=? AND job.source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
            if row is None or row["replacement_message_id"] is None:
                db.rollback()
                return None, False
            replacement_id = int(row["replacement_message_id"])
            if int(row["manager_manual_override"]):
                db.rollback()
                return replacement_id, False
            current = str(row["manager"]) if row["manager"] else None
            previous_auto = (
                str(row["auto_manager_name"])
                if row["auto_manager_name"]
                else None
            )
            if current is not None and previous_auto is None:
                db.execute(
                    """UPDATE sales_photo_delivery_links
                       SET manager_manual_override=1,updated_at=?
                       WHERE chat_id=? AND source_message_id=?""",
                    (_iso(at or utc_now()), int(chat_id), int(source_message_id)),
                )
                db.commit()
                return replacement_id, False
            if previous_auto is not None and current not in {None, previous_auto}:
                db.execute(
                    """UPDATE sales_photo_delivery_links
                       SET auto_manager_name=NULL,manager_manual_override=1,
                           updated_at=?
                       WHERE chat_id=? AND source_message_id=?""",
                    (_iso(at or utc_now()), int(chat_id), int(source_message_id)),
                )
                db.commit()
                return replacement_id, False
            desired = str(desired_manager or "").strip() or None
            if desired is None:
                generation = int(row["ui_generation"])
                clear_manager = previous_auto is not None and current == previous_auto
                choices_generation = (
                    generation if generation % 2 == 0 else generation + 1
                )
                if clear_manager:
                    db.execute(
                        """UPDATE sales_photo_jobs
                           SET manager=NULL,ui_generation=?,updated_at=?
                           WHERE chat_id=? AND source_message_id=?""",
                        (
                            choices_generation,
                            _iso(at or utc_now()),
                            int(chat_id),
                            int(source_message_id),
                        ),
                    )
                db.execute(
                    """UPDATE sales_photo_delivery_links
                       SET auto_manager_name=NULL,updated_at=?
                       WHERE chat_id=? AND source_message_id=?""",
                    (
                        _iso(at or utc_now()),
                        int(chat_id),
                        int(source_message_id),
                    ),
                )
                db.commit()
                return replacement_id, clear_manager
            generation = int(row["ui_generation"])
            selected_generation = generation if generation % 2 else generation + 1
            changed = current != desired or generation != selected_generation
            db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=?,ui_generation=?,updated_at=?
                   WHERE chat_id=? AND source_message_id=?""",
                (
                    desired[:64],
                    selected_generation,
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.execute(
                """UPDATE sales_photo_delivery_links
                   SET auto_manager_name=?,updated_at=?
                   WHERE chat_id=? AND source_message_id=?""",
                (
                    desired[:64],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
            return replacement_id, changed

    def unlink_delivery(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> tuple[int | None, bool]:
        """Remove an automatic link and only undo its automatic manager."""

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT job.replacement_message_id,job.manager,
                          job.ui_generation,link.auto_manager_name,
                          link.manager_manual_override
                   FROM sales_photo_delivery_links AS link
                   JOIN sales_photo_jobs AS job
                     ON job.chat_id=link.chat_id
                    AND job.source_message_id=link.source_message_id
                   WHERE link.chat_id=? AND link.source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
            if row is None:
                db.rollback()
                return None, False
            replacement_id = (
                int(row["replacement_message_id"])
                if row["replacement_message_id"] is not None
                else None
            )
            auto_manager = (
                str(row["auto_manager_name"])
                if row["auto_manager_name"]
                else None
            )
            current = str(row["manager"]) if row["manager"] else None
            clear_manager = bool(
                not int(row["manager_manual_override"])
                and auto_manager is not None
                and current == auto_manager
            )
            if clear_manager:
                generation = int(row["ui_generation"])
                choices_generation = generation if generation % 2 == 0 else generation + 1
                db.execute(
                    """UPDATE sales_photo_jobs
                       SET manager=NULL,ui_generation=?,updated_at=?
                       WHERE chat_id=? AND source_message_id=?""",
                    (
                        choices_generation,
                        _iso(at or utc_now()),
                        int(chat_id),
                        int(source_message_id),
                    ),
                )
            db.execute(
                """DELETE FROM sales_photo_delivery_links
                   WHERE chat_id=? AND source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            )
            db.commit()
            return replacement_id, clear_manager

    def mark_delivery_manager_manual(
        self,
        chat_id: int,
        replacement_message_id: int,
        at: datetime | None = None,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_delivery_links
                   SET auto_manager_name=NULL,manager_manual_override=1,
                       updated_at=?
                   WHERE chat_id=? AND source_message_id=(
                       SELECT source_message_id FROM sales_photo_jobs
                       WHERE chat_id=? AND replacement_message_id=?
                   )""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(chat_id),
                    int(replacement_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def delivery_links_for_orders(
        self,
        chat_id: int,
        delivery_order_ids: Iterable[int],
    ) -> tuple[DeliveryLink, ...]:
        values = tuple(sorted({int(value) for value in delivery_order_ids}))
        if not values:
            return ()
        placeholders = ",".join("?" for _ in values)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT link.chat_id,link.source_message_id,
                           job.replacement_message_id,link.delivery_order_id,
                           link.delivery_order_number,link.auto_manager_name,
                           link.manager_manual_override
                    FROM sales_photo_delivery_links AS link
                    JOIN sales_photo_jobs AS job
                      ON job.chat_id=link.chat_id
                     AND job.source_message_id=link.source_message_id
                    WHERE link.chat_id=?
                      AND link.delivery_order_id IN ({placeholders})
                      AND job.replacement_message_id IS NOT NULL
                      AND job.order_removed=0""",
                (int(chat_id), *values),
            ).fetchall()
        return tuple(
            DeliveryLink(
                chat_id=int(row["chat_id"]),
                source_message_id=int(row["source_message_id"]),
                replacement_message_id=int(row["replacement_message_id"]),
                delivery_order_id=int(row["delivery_order_id"]),
                delivery_order_number=int(row["delivery_order_number"]),
                auto_manager_name=(
                    str(row["auto_manager_name"])
                    if row["auto_manager_name"]
                    else None
                ),
                manager_manual_override=bool(row["manager_manual_override"]),
            )
            for row in rows
        )

    def delivery_source_for_order(
        self,
        chat_id: int,
        delivery_order_id: int,
    ) -> tuple[int, int | None] | None:
        """Resolve an explicit link even while its Telegram card is unfinished."""
        with self._connect() as db:
            row = db.execute(
                """SELECT link.source_message_id,job.replacement_message_id
                   FROM sales_photo_delivery_links AS link
                   JOIN sales_photo_jobs AS job
                     ON job.chat_id=link.chat_id
                    AND job.source_message_id=link.source_message_id
                   WHERE link.chat_id=? AND link.delivery_order_id=?
                   ORDER BY link.updated_at DESC LIMIT 1""",
                (int(chat_id), int(delivery_order_id)),
            ).fetchone()
        if row is None:
            return None
        return (
            int(row["source_message_id"]),
            int(row["replacement_message_id"])
            if row["replacement_message_id"] is not None
            else None,
        )

    def compact_recent_daily_orders(
        self,
        chat_id: int,
        sale_date_from: date,
        sale_date_to: date,
        at: datetime | None = None,
    ) -> tuple[int, int]:
        """Release unpublished reservations and compact published card IDs.

        An unsuccessful send is not a sold item and must not leave a visible
        gap. A processing job can safely lose its reservation here: if its
        Telegram send later succeeds, ``record_replacement`` assigns the next
        authoritative ID in the same transaction and the debounce sweep repairs
        the already-sent caption when necessary.
        """

        now = _iso(at or utc_now())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            released = db.execute(
                """UPDATE sales_photo_jobs
                   SET daily_order_id=NULL,order_card_applied=0,updated_at=?
                   WHERE chat_id=? AND replacement_message_id IS NULL
                     AND order_removed=0 AND daily_order_id IS NOT NULL
                     AND sale_date>=? AND sale_date<=?""",
                (
                    now,
                    int(chat_id),
                    sale_date_from.isoformat(),
                    sale_date_to.isoformat(),
                ),
            ).rowcount
            days = db.execute(
                """SELECT DISTINCT sale_date FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id IS NOT NULL
                     AND order_removed=0 AND sale_date>=? AND sale_date<=?
                   ORDER BY sale_date""",
                (
                    int(chat_id),
                    sale_date_from.isoformat(),
                    sale_date_to.isoformat(),
                ),
            ).fetchall()
            changed = 0
            for day_row in days:
                sale_day = str(day_row["sale_date"])
                rows = db.execute(
                    """SELECT source_message_id,daily_order_id
                       FROM sales_photo_jobs
                       WHERE chat_id=? AND sale_date=? AND order_removed=0
                         AND replacement_message_id IS NOT NULL
                       ORDER BY created_at,source_message_id""",
                    (int(chat_id), sale_day),
                ).fetchall()
                if all(
                    row["daily_order_id"] is not None
                    and int(row["daily_order_id"]) == expected
                    for expected, row in enumerate(rows, start=1)
                ):
                    continue
                changed += sum(
                    row["daily_order_id"] is None
                    or int(row["daily_order_id"]) != expected
                    for expected, row in enumerate(rows, start=1)
                )
                # NULL first avoids unique-index collisions for arbitrary gaps
                # or reordered legacy rows while the transaction stays atomic.
                db.execute(
                    """UPDATE sales_photo_jobs
                       SET daily_order_id=NULL,order_card_applied=0,updated_at=?
                       WHERE chat_id=? AND sale_date=? AND order_removed=0
                         AND replacement_message_id IS NOT NULL""",
                    (now, int(chat_id), sale_day),
                )
                for expected, row in enumerate(rows, start=1):
                    db.execute(
                        """UPDATE sales_photo_jobs
                           SET daily_order_id=?,order_card_applied=0,updated_at=?
                           WHERE chat_id=? AND source_message_id=?
                             AND replacement_message_id IS NOT NULL
                             AND order_removed=0""",
                        (
                            expected,
                            now,
                            int(chat_id),
                            int(row["source_message_id"]),
                        ),
                    )
            db.commit()
        return int(released), int(changed)

    def mark_order_card_removed(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> tuple[date | None, int]:
        """Remove one sold item from its day and compact all later IDs."""

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT sale_date FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?
                     AND order_removed=0""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
            if row is None or row["sale_date"] is None:
                db.rollback()
                return None, 0
            sale_day = date.fromisoformat(str(row["sale_date"]))
            db.execute(
                """UPDATE sales_photo_jobs
                   SET daily_order_id=NULL,order_removed=1,
                       order_card_applied=1,order_backfill_attempts=0,
                       price_card_applied=1,price_backfill_attempts=0,
                       updated_at=?
                   WHERE chat_id=? AND source_message_id=?""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            active_rows = db.execute(
                """SELECT source_message_id,daily_order_id
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND sale_date=? AND order_removed=0
                     AND replacement_message_id IS NOT NULL
                     AND daily_order_id IS NOT NULL
                   ORDER BY daily_order_id,created_at,source_message_id""",
                (int(chat_id), sale_day.isoformat()),
            ).fetchall()
            changed = 0
            for expected_id, active_row in enumerate(active_rows, start=1):
                current_id = int(active_row["daily_order_id"])
                if current_id == expected_id:
                    continue
                cursor = db.execute(
                    """UPDATE sales_photo_jobs
                       SET daily_order_id=?,order_card_applied=0,
                           order_backfill_attempts=0,updated_at=?
                       WHERE chat_id=? AND source_message_id=?
                         AND order_removed=0""",
                    (
                        expected_id,
                        _iso(at or utc_now()),
                        int(chat_id),
                        int(active_row["source_message_id"]),
                    ),
                )
                changed += int(cursor.rowcount)
            db.commit()
        return sale_day, changed

    def mark_order_backfill_failed(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET order_backfill_attempts=order_backfill_attempts+1,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND order_card_applied=0""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()

    def mark_price_backfill_failed(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET price_backfill_attempts=price_backfill_attempts+1,
                       updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND price_card_applied=0""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()

    def record_replacement(
        self,
        chat_id: int,
        source_message_id: int,
        replacement_message_id: int,
        at: datetime | None = None,
        *,
        output_message_ids: tuple[int, ...] | None = None,
    ) -> str:
        """Atomically reconcile a send response or a signed generated post.

        Returns ``recorded``, ``same``, ``conflict`` or ``missing``. A conflict
        identifies a duplicate Telegram send that the caller should delete.
        """

        output_ids = tuple(
            dict.fromkeys(
                int(value)
                for value in (
                    output_message_ids or (int(replacement_message_id),)
                )
            )
        )
        if int(replacement_message_id) not in output_ids:
            output_ids = (*output_ids, int(replacement_message_id))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT replacement_message_id,status,sale_date,daily_order_id
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
            if row is None:
                db.rollback()
                return "missing"
            existing = row["replacement_message_id"]
            if existing is not None:
                if int(existing) == int(replacement_message_id):
                    now = _iso(at or utc_now())
                    db.executemany(
                        """INSERT OR IGNORE INTO sales_photo_output_members(
                               chat_id,source_message_id,message_id,created_at
                           ) VALUES (?,?,?,?)""",
                        (
                            (
                                int(chat_id),
                                int(source_message_id),
                                message_id,
                                now,
                            )
                            for message_id in output_ids
                        ),
                    )
                    db.commit()
                    return "same"
                now = _iso(at or utc_now())
                db.executemany(
                    """INSERT INTO sales_photo_duplicate_cleanup(
                           chat_id,message_id,created_at,updated_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(chat_id,message_id) DO NOTHING""",
                    (
                        (int(chat_id), message_id, now, now)
                        for message_id in output_ids
                    ),
                )
                db.commit()
                return "conflict"
            if str(row["status"]) not in {"processing", "failed"}:
                now = _iso(at or utc_now())
                db.executemany(
                    """INSERT INTO sales_photo_duplicate_cleanup(
                           chat_id,message_id,created_at,updated_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(chat_id,message_id) DO NOTHING""",
                    (
                        (int(chat_id), message_id, now, now)
                        for message_id in output_ids
                    ),
                )
                db.commit()
                return "conflict"
            recorded_at = at or utc_now()
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            recorded_at = recorded_at.astimezone(UTC)
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET replacement_message_id=?,status='reposted',
                       encrypted_payload=NULL,last_error_code=NULL,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND replacement_message_id IS NULL
                     AND status IN ('processing','failed')""",
                (
                    int(replacement_message_id),
                    _iso(recorded_at),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return "conflict"
            if row["sale_date"] is not None:
                self._ensure_daily_order_locked(
                    db,
                    int(chat_id),
                    int(source_message_id),
                    date.fromisoformat(str(row["sale_date"])),
                )
            self._note_auto_correction_new_card_locked(
                db,
                int(chat_id),
                recorded_at,
            )
            now = _iso(at or utc_now())
            db.executemany(
                """INSERT INTO sales_photo_output_members(
                       chat_id,source_message_id,message_id,created_at
                   ) VALUES (?,?,?,?)
                   ON CONFLICT(chat_id,source_message_id,message_id) DO NOTHING""",
                (
                    (
                        int(chat_id),
                        int(source_message_id),
                        message_id,
                        now,
                    )
                    for message_id in output_ids
                ),
            )
            db.commit()
            return "recorded"

    def mark_reposted(
        self,
        chat_id: int,
        source_message_id: int,
        replacement_message_id: int,
        at: datetime | None = None,
    ) -> None:
        outcome = self.record_replacement(
            chat_id,
            source_message_id,
            replacement_message_id,
            at=at,
        )
        if outcome not in {"recorded", "same"}:
            raise RuntimeError(f"photo replacement could not be recorded: {outcome}")

    def mark_complete(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET status='complete',encrypted_payload=NULL,
                       last_error_code=NULL,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND status IN ('reposted','delete_pending')""",
                (_iso(at or utc_now()), int(chat_id), int(source_message_id)),
            )
            db.commit()

    def mark_delete_pending(
        self,
        chat_id: int,
        source_message_id: int,
        error_code: str,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET status='delete_pending',delete_attempts=delete_attempts+1,
                       last_error_code=?,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND status IN ('reposted','delete_pending')""",
                (
                    str(error_code or "delete_failed")[:80],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()

    def begin_source_delete(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> bool:
        """Persist that source deletion may now be in flight."""

        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='delete_pending',last_error_code=NULL,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND replacement_message_id IS NOT NULL
                     AND status IN ('reposted','delete_pending')""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def mark_failed(
        self,
        chat_id: int,
        source_message_id: int,
        error_code: str,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET status='failed',last_error_code=?,updated_at=?,
                       encrypted_payload=CASE
                           WHEN processing_attempts>=3 THEN NULL
                           ELSE encrypted_payload
                       END
                   WHERE chat_id=? AND source_message_id=? AND status='processing'
                     AND last_error_code='before_send'""",
                (
                    str(error_code or "send_failed")[:80],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()

    def mark_ambiguous_send(
        self,
        chat_id: int,
        source_message_id: int,
        error_code: str,
        at: datetime | None = None,
    ) -> None:
        """Stop automatic retries when Telegram may already have posted a card."""

        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_jobs
                   SET status='failed',processing_attempts=3,
                       encrypted_payload=NULL,last_error_code=?,updated_at=?
                   WHERE chat_id=? AND source_message_id=? AND status='processing'""",
                (
                    str(error_code or "ambiguous_send")[:80],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()

    def mark_send_started(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> bool:
        """Persist the point after which a send cancellation is ambiguous."""

        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET last_error_code='send_started',updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND status='processing'
                     AND replacement_message_id IS NULL
                     AND last_error_code='before_send'""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def mark_send_rejected(
        self,
        chat_id: int,
        source_message_id: int,
        error_code: str,
        at: datetime | None = None,
    ) -> bool:
        """Make a definitively rejected Telegram send safe to retry."""

        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='failed',last_error_code=?,updated_at=?,
                       encrypted_payload=CASE
                           WHEN processing_attempts>=3 THEN NULL
                           ELSE encrypted_payload
                       END
                   WHERE chat_id=? AND source_message_id=?
                     AND status='processing'
                     AND replacement_message_id IS NULL
                     AND last_error_code='send_started'""",
                (
                    str(error_code or "send_rejected")[:80],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def cancel_edited_source(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> tuple[bool, int | None]:
        """Quarantine a source that changed after its processing was claimed.

        If a replacement was already recorded but the source still exists, its
        cleanup is queued and the replacement mapping is removed atomically.
        The caller should also attempt that cleanup immediately.
        """

        now = _iso(at or utc_now())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status,replacement_message_id
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
            if row is None:
                # Telegram can deliver an edited_channel_post before the
                # corresponding channel_post is dispatched locally after a
                # restart. Message IDs are never reused inside a chat, so a
                # durable tombstone safely prevents the stale original from
                # being claimed later.
                db.execute(
                    """INSERT INTO sales_photo_jobs(
                           chat_id,source_message_id,source_file_unique_id,
                           encrypted_payload,replacement_message_id,manager,
                           ui_generation,status,delete_attempts,
                           processing_attempts,last_error_code,
                           created_at,updated_at
                       ) VALUES (?,?,?,NULL,NULL,NULL,0,'complete',0,3,?,?,?)""",
                    (
                        int(chat_id),
                        int(source_message_id),
                        "edited-before-claim",
                        "source_edited_before_claim",
                        now,
                        now,
                    ),
                )
                db.execute(
                    """INSERT OR IGNORE INTO sales_photo_source_members(
                           chat_id,source_message_id,member_message_id,
                           deleted,created_at,updated_at
                       ) VALUES (?,?,?,0,?,?)""",
                    (
                        int(chat_id),
                        int(source_message_id),
                        int(source_message_id),
                        now,
                        now,
                    ),
                )
                db.commit()
                return True, None
            status = str(row["status"])
            replacement_value = row["replacement_message_id"]
            replacement_id = (
                int(replacement_value) if replacement_value is not None else None
            )
            if status not in {
                "processing",
                "failed",
                "reposted",
                "delete_pending",
            }:
                db.rollback()
                return False, None
            if status == "delete_pending" and replacement_id is not None:
                # A delete request may already have reached Telegram. Keep the
                # replacement as a fallback, stop retries, and never risk
                # deleting both copies after a late edited_channel_post.
                cursor = db.execute(
                    """UPDATE sales_photo_jobs
                       SET status='complete',encrypted_payload=NULL,
                           processing_attempts=3,
                           last_error_code='source_edited_delete_ambiguous',
                           updated_at=?
                       WHERE chat_id=? AND source_message_id=?
                         AND status='delete_pending'
                         AND replacement_message_id=?""",
                    (
                        now,
                        int(chat_id),
                        int(source_message_id),
                        replacement_id,
                    ),
                )
                if cursor.rowcount != 1:
                    db.rollback()
                    return False, None
                db.commit()
                return True, None
            if replacement_id is not None:
                output_rows = db.execute(
                    """SELECT message_id FROM sales_photo_output_members
                       WHERE chat_id=? AND source_message_id=?""",
                    (int(chat_id), int(source_message_id)),
                ).fetchall()
                output_ids = tuple(
                    int(output_row["message_id"]) for output_row in output_rows
                ) or (replacement_id,)
                db.executemany(
                    """INSERT INTO sales_photo_duplicate_cleanup(
                           chat_id,message_id,created_at,updated_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(chat_id,message_id) DO NOTHING""",
                    (
                        (int(chat_id), output_id, now, now)
                        for output_id in output_ids
                    ),
                )
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='complete',replacement_message_id=NULL,
                       encrypted_payload=NULL,manager=NULL,ui_generation=0,
                       processing_attempts=3,last_error_code='source_edited',
                       updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND status=?""",
                (
                    now,
                    int(chat_id),
                    int(source_message_id),
                    status,
                ),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return False, None
            db.commit()
            return True, replacement_id

    def preserve_after_source_edit(
        self,
        chat_id: int,
        source_message_id: int,
        at: datetime | None = None,
    ) -> bool:
        """Stop stale work while preserving any replacement as a safe fallback."""

        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='complete',encrypted_payload=NULL,
                       processing_attempts=3,
                       last_error_code='source_edit_fail_safe',updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND status IN (
                         'processing','failed','reposted','delete_pending'
                     )""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def source_accepts_replacement(
        self,
        chat_id: int,
        source_message_id: int,
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?
                     AND status='processing'
                     AND replacement_message_id IS NULL""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
        return row is not None

    def source_pending_deletion(
        self,
        chat_id: int,
        source_message_id: int,
        replacement_message_id: int,
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?
                     AND replacement_message_id=?
                     AND status IN ('reposted','delete_pending')""",
                (
                    int(chat_id),
                    int(source_message_id),
                    int(replacement_message_id),
                ),
            ).fetchone()
        return row is not None

    def primary_source_for_member(
        self,
        chat_id: int,
        member_message_id: int,
    ) -> int | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT source_message_id FROM sales_photo_source_members
                   WHERE chat_id=? AND member_message_id=?""",
                (int(chat_id), int(member_message_id)),
            ).fetchone()
        return int(row["source_message_id"]) if row is not None else None

    def pending_source_members(
        self,
        chat_id: int,
        source_message_id: int,
    ) -> tuple[int, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT member_message_id FROM sales_photo_source_members
                   WHERE chat_id=? AND source_message_id=? AND deleted=0
                   ORDER BY member_message_id""",
                (int(chat_id), int(source_message_id)),
            ).fetchall()
        if rows:
            return tuple(int(row["member_message_id"]) for row in rows)
        return ()

    def mark_source_member_deleted(
        self,
        chat_id: int,
        source_message_id: int,
        member_message_id: int,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_source_members
                   SET deleted=1,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND member_message_id=?""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                    int(member_message_id),
                ),
            )
            db.commit()

    def output_message_ids(
        self,
        chat_id: int,
        source_message_id: int,
    ) -> tuple[int, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT message_id FROM sales_photo_output_members
                   WHERE chat_id=? AND source_message_id=?
                   ORDER BY message_id""",
                (int(chat_id), int(source_message_id)),
            ).fetchall()
        return tuple(int(row["message_id"]) for row in rows)

    def fail_stale_processing(
        self,
        chat_id: int,
        older_than: datetime,
        at: datetime | None = None,
    ) -> int:
        with self._connect() as db:
            now = _iso(at or utc_now())
            retryable = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='failed',last_error_code='stale_before_send',
                       updated_at=?
                   WHERE chat_id=? AND status='processing'
                     AND replacement_message_id IS NULL
                     AND last_error_code='before_send'
                     AND updated_at<?""",
                (now, int(chat_id), _iso(older_than)),
            )
            ambiguous = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='failed',processing_attempts=3,
                       last_error_code='stale_processing_ambiguous',updated_at=?,
                       encrypted_payload=NULL
                   WHERE chat_id=? AND status='processing'
                     AND replacement_message_id IS NULL
                     AND updated_at<?""",
                (
                    now,
                    int(chat_id),
                    _iso(older_than),
                ),
            )
            db.commit()
            return int(retryable.rowcount) + int(ambiguous.rowcount)

    def retryable_photos(
        self,
        chat_id: int,
        limit: int = 20,
    ) -> tuple[PendingPhoto, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,encrypted_payload,
                          processing_attempts,created_at,updated_at,sale_date
                   FROM sales_photo_jobs
                   WHERE chat_id=? AND status='failed'
                     AND replacement_message_id IS NULL
                     AND encrypted_payload IS NOT NULL
                     AND processing_attempts<3
                   ORDER BY updated_at
                   LIMIT ?""",
                (int(chat_id), max(1, min(int(limit), 100))),
            ).fetchall()
        jobs: list[PendingPhoto] = []
        for row in rows:
            try:
                file_ids, caption, thread_id, source_kind, source_ids = self._decrypt_payload(
                    bytes(row["encrypted_payload"])
                )
            except RuntimeError:
                with self._connect() as db:
                    db.execute(
                        """UPDATE sales_photo_jobs
                           SET processing_attempts=3,
                               last_error_code='retry_payload_invalid',updated_at=?
                           WHERE chat_id=? AND source_message_id=?
                             AND status='failed'""",
                        (
                            _iso(utc_now()),
                            int(row["chat_id"]),
                            int(row["source_message_id"]),
                        ),
                    )
                    db.commit()
                logger.error(
                    "sales_photo_retry_payload_quarantined chat_id=%s "
                    "source_message_id=%s",
                    int(row["chat_id"]),
                    int(row["source_message_id"]),
                )
                continue
            created_at = datetime.fromisoformat(str(row["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            effective_sale_date = (
                date.fromisoformat(str(row["sale_date"]))
                if row["sale_date"] is not None
                else created_at.astimezone(TASHKENT_TZ).date()
            )
            jobs.append(
                PendingPhoto(
                    chat_id=int(row["chat_id"]),
                    source_message_id=int(row["source_message_id"]),
                    source_file_id=file_ids[0],
                    source_file_ids=file_ids,
                    source_message_ids=(
                        source_ids
                        or self.pending_source_members(
                            int(row["chat_id"]),
                            int(row["source_message_id"]),
                        )
                        or (int(row["source_message_id"]),)
                    ),
                    source_kind=source_kind,
                    client_caption=caption,
                    message_thread_id=thread_id,
                    attempts=int(row["processing_attempts"]),
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
                    sale_date=effective_sale_date,
                )
            )
        return tuple(jobs)

    def claim_retry(
        self,
        chat_id: int,
        source_message_id: int,
        expected_attempts: int,
        at: datetime | None = None,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='processing',processing_attempts=processing_attempts+1,
                       last_error_code='before_send',updated_at=?
                   WHERE chat_id=? AND source_message_id=? AND status='failed'
                     AND replacement_message_id IS NULL
                     AND encrypted_payload IS NOT NULL
                     AND processing_attempts=? AND processing_attempts<3""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                    int(expected_attempts),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def queue_duplicate_cleanup(
        self,
        chat_id: int,
        message_id: int,
        at: datetime | None = None,
    ) -> None:
        now = _iso(at or utc_now())
        with self._connect() as db:
            db.execute(
                """INSERT INTO sales_photo_duplicate_cleanup(
                       chat_id,message_id,created_at,updated_at
                   ) VALUES (?,?,?,?)
                   ON CONFLICT(chat_id,message_id) DO NOTHING""",
                (int(chat_id), int(message_id), now, now),
            )
            db.commit()

    def pending_duplicate_cleanups(
        self,
        chat_id: int,
        limit: int = 50,
    ) -> tuple[PendingDuplicate, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,message_id,attempts,updated_at
                   FROM sales_photo_duplicate_cleanup
                   WHERE chat_id=?
                   ORDER BY updated_at LIMIT ?""",
                (int(chat_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return tuple(
            PendingDuplicate(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                attempts=int(row["attempts"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        )

    def complete_duplicate_cleanup(self, chat_id: int, message_id: int) -> None:
        with self._connect() as db:
            db.execute(
                """DELETE FROM sales_photo_duplicate_cleanup
                   WHERE chat_id=? AND message_id=?""",
                (int(chat_id), int(message_id)),
            )
            db.commit()

    def mark_duplicate_cleanup_failed(
        self,
        chat_id: int,
        message_id: int,
        at: datetime | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE sales_photo_duplicate_cleanup
                   SET attempts=attempts+1,updated_at=?
                   WHERE chat_id=? AND message_id=?""",
                (_iso(at or utc_now()), int(chat_id), int(message_id)),
            )
            db.commit()

    def is_replacement(self, chat_id: int, message_id: int) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id=?
                   UNION ALL
                   SELECT 1 FROM sales_photo_output_members AS output
                   JOIN sales_photo_jobs AS job
                     ON job.chat_id=output.chat_id
                    AND job.source_message_id=output.source_message_id
                   WHERE output.chat_id=? AND output.message_id=?
                     AND job.replacement_message_id IS NOT NULL
                   LIMIT 1""",
                (
                    int(chat_id),
                    int(message_id),
                    int(chat_id),
                    int(message_id),
                ),
            ).fetchone()
            return row is not None

    def source_for_replacement(
        self,
        chat_id: int,
        replacement_message_id: int,
    ) -> int | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT source_message_id FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id=?""",
                (int(chat_id), int(replacement_message_id)),
            ).fetchone()
        return int(row["source_message_id"]) if row is not None else None

    def replacement_for_source(self, chat_id: int, source_message_id: int) -> int | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT replacement_message_id FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
        if row is None or row["replacement_message_id"] is None:
            return None
        return int(row["replacement_message_id"])

    def pending_deletions(
        self,
        chat_id: int,
        limit: int = 50,
    ) -> tuple[PendingDeletion, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,replacement_message_id,
                          delete_attempts,updated_at
                   FROM sales_photo_jobs
                   WHERE chat_id=?
                     AND status IN ('reposted','delete_pending')
                     AND replacement_message_id IS NOT NULL
                   ORDER BY updated_at
                   LIMIT ?""",
                (int(chat_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return tuple(
            PendingDeletion(
                chat_id=int(row["chat_id"]),
                source_message_id=int(row["source_message_id"]),
                replacement_message_id=int(row["replacement_message_id"]),
                attempts=int(row["delete_attempts"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        )

    def select_manager(
        self,
        chat_id: int,
        replacement_message_id: int,
        manager: str,
        at: datetime | None = None,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND manager IS NULL
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    str(manager)[:64],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def set_manager(
        self,
        chat_id: int,
        replacement_message_id: int,
        manager: str,
        at: datetime | None = None,
    ) -> bool:
        """Record Telegram's already-applied UI state.

        Telegram is edited first; this ledger update is intentionally idempotent so
        an ambiguous API timeout cannot permanently lock the Back button.
        """

        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    str(manager)[:64],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def selected_manager(self, chat_id: int, replacement_message_id: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT manager FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id=?""",
                (int(chat_id), int(replacement_message_id)),
            ).fetchone()
        if row is None or not row["manager"]:
            return None
        return str(row["manager"])

    def ui_generation_for_replacement(
        self,
        chat_id: int,
        replacement_message_id: int,
    ) -> int | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT ui_generation FROM sales_photo_jobs
                   WHERE chat_id=? AND replacement_message_id=?""",
                (int(chat_id), int(replacement_message_id)),
            ).fetchone()
        return int(row["ui_generation"]) if row is not None else None

    def reserve_ui_transition(
        self,
        chat_id: int,
        replacement_message_id: int,
        callback_generation: int,
        at: datetime | None = None,
    ) -> bool:
        """Durably invalidate a callback before editing Telegram state."""

        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET ui_generation=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    current_generation + 1,
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def release_ui_transition(
        self,
        chat_id: int,
        replacement_message_id: int,
        callback_generation: int,
        at: datetime | None = None,
    ) -> bool:
        """Release a reservation only after a definite Telegram rejection."""

        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET ui_generation=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    current_generation,
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation + 1,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def rollback_ui_transition(
        self,
        chat_id: int,
        replacement_message_id: int,
        callback_generation: int,
        manager_before: str | None,
        at: datetime | None = None,
    ) -> bool:
        """Atomically restore both generation and manager after a definite reject."""

        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=?,ui_generation=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    str(manager_before)[:64] if manager_before else None,
                    current_generation,
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation + 1,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def commit_reserved_manager_selection(
        self,
        chat_id: int,
        replacement_message_id: int,
        manager: str,
        callback_generation: int,
        at: datetime | None = None,
    ) -> bool:
        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    str(manager)[:64],
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation + 1,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def commit_reserved_manager_clear(
        self,
        chat_id: int,
        replacement_message_id: int,
        callback_generation: int,
        at: datetime | None = None,
    ) -> bool:
        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=NULL,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation + 1,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def apply_manager_selection(
        self,
        chat_id: int,
        replacement_message_id: int,
        manager: str,
        callback_generation: int,
        at: datetime | None = None,
    ) -> bool:
        """Persist the UI transition after Telegram accepted the caption edit.

        A callback can be one generation ahead when the preceding Telegram edit
        succeeded but its SQLite write failed. Older callbacks can never lower the
        monotonic generation, which prevents a delayed double-click from replacing
        a newer manager choice.
        """

        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=?,ui_generation=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation<=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    str(manager)[:64],
                    current_generation + 1,
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def apply_manager_clear(
        self,
        chat_id: int,
        replacement_message_id: int,
        callback_generation: int,
        at: datetime | None = None,
    ) -> bool:
        current_generation = max(0, int(callback_generation))
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET manager=NULL,ui_generation=?,updated_at=?
                   WHERE chat_id=? AND replacement_message_id=?
                     AND ui_generation<=?
                     AND status IN ('reposted','delete_pending','complete')""",
                (
                    current_generation + 1,
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(replacement_message_id),
                    current_generation,
                ),
            )
            db.commit()
            return cursor.rowcount == 1

    def clear_manager(
        self,
        chat_id: int,
        replacement_message_id: int,
        expected_manager: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        sql = """UPDATE sales_photo_jobs SET manager=NULL,updated_at=?
                 WHERE chat_id=? AND replacement_message_id=?"""
        values: list[object] = [
            _iso(at or utc_now()),
            int(chat_id),
            int(replacement_message_id),
        ]
        if expected_manager is not None:
            sql += " AND manager=?"
            values.append(str(expected_manager))
        with self._connect() as db:
            cursor = db.execute(sql, values)
            db.commit()
            return cursor.rowcount == 1
