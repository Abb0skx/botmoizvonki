from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from cryptography.fernet import Fernet, InvalidToken


UTC = timezone.utc
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
    client_caption: str | None
    message_thread_id: int | None
    attempts: int
    updated_at: datetime


@dataclass(frozen=True)
class PendingDuplicate:
    chat_id: int
    message_id: int
    attempts: int
    updated_at: datetime


@dataclass(frozen=True)
class CachedName:
    model_name: str
    confidence: float
    source_count: int


class SalesPhotoRepository:
    """Small durable ledger for repost idempotency and automatic name cache."""

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
    ) -> bytes:
        payload = json.dumps(
            {
                "file_id": str(source_file_id or "")[:300],
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

    def _decrypt_payload(self, payload: bytes) -> tuple[str, str | None, int | None]:
        try:
            value = json.loads(self._cipher.decrypt(bytes(payload)).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Повреждён retry payload") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Повреждён retry payload")
        file_id = str(value.get("file_id") or "")[:300]
        caption_value = value.get("caption")
        caption = str(caption_value)[:300] if caption_value else None
        thread_value = value.get("thread_id")
        thread_id = int(thread_value) if thread_value is not None else None
        if not file_id:
            raise RuntimeError("Retry payload не содержит Telegram file_id")
        return file_id, caption, thread_id

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

                CREATE TABLE IF NOT EXISTS sales_photo_name_cache (
                    cache_key TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_count INTEGER NOT NULL,
                    provider_model TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                """
            )
            db.commit()

    @staticmethod
    def _bootstrap_key(bot_id: int, chat_id: int) -> str:
        return f"polling_bootstrapped:{int(bot_id)}:{int(chat_id)}"

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
    ) -> bool:
        now = _iso(at or utc_now())
        encrypted_payload = (
            self._encrypt_payload(
                source_file_id,
                client_caption,
                message_thread_id,
            )
            if source_file_id
            else None
        )
        with self._connect() as db:
            try:
                db.execute(
                    """INSERT INTO sales_photo_jobs(
                           chat_id,source_message_id,source_file_unique_id,
                           encrypted_payload,status,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        int(chat_id),
                        int(source_message_id),
                        str(source_file_unique_id or "unknown")[:200],
                        encrypted_payload,
                        "processing",
                        now,
                        now,
                    ),
                )
                db.commit()
                return True
            except sqlite3.IntegrityError:
                cursor = db.execute(
                    """UPDATE sales_photo_jobs
                       SET status='processing',last_error_code=NULL,
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
                db.commit()
                return cursor.rowcount == 1

    def record_replacement(
        self,
        chat_id: int,
        source_message_id: int,
        replacement_message_id: int,
        at: datetime | None = None,
    ) -> str:
        """Atomically reconcile a send response or a signed generated post.

        Returns ``recorded``, ``same``, ``conflict`` or ``missing``. A conflict
        identifies a duplicate Telegram send that the caller should delete.
        """

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT replacement_message_id,status FROM sales_photo_jobs
                   WHERE chat_id=? AND source_message_id=?""",
                (int(chat_id), int(source_message_id)),
            ).fetchone()
            if row is None:
                db.rollback()
                return "missing"
            existing = row["replacement_message_id"]
            if existing is not None:
                if int(existing) == int(replacement_message_id):
                    db.rollback()
                    return "same"
                now = _iso(at or utc_now())
                db.execute(
                    """INSERT INTO sales_photo_duplicate_cleanup(
                           chat_id,message_id,created_at,updated_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(chat_id,message_id) DO NOTHING""",
                    (int(chat_id), int(replacement_message_id), now, now),
                )
                db.commit()
                return "conflict"
            if str(row["status"]) not in {"processing", "failed"}:
                now = _iso(at or utc_now())
                db.execute(
                    """INSERT INTO sales_photo_duplicate_cleanup(
                           chat_id,message_id,created_at,updated_at
                       ) VALUES (?,?,?,?)
                       ON CONFLICT(chat_id,message_id) DO NOTHING""",
                    (int(chat_id), int(replacement_message_id), now, now),
                )
                db.commit()
                return "conflict"
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET replacement_message_id=?,status='reposted',
                       encrypted_payload=NULL,last_error_code=NULL,updated_at=?
                   WHERE chat_id=? AND source_message_id=?
                     AND replacement_message_id IS NULL
                     AND status IN ('processing','failed')""",
                (
                    int(replacement_message_id),
                    _iso(at or utc_now()),
                    int(chat_id),
                    int(source_message_id),
                ),
            )
            if cursor.rowcount != 1:
                db.rollback()
                return "conflict"
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
                   WHERE chat_id=? AND source_message_id=? AND status='processing'""",
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

    def fail_stale_processing(
        self,
        chat_id: int,
        older_than: datetime,
        at: datetime | None = None,
    ) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE sales_photo_jobs
                   SET status='failed',processing_attempts=3,
                       last_error_code='stale_processing_ambiguous',updated_at=?,
                       encrypted_payload=NULL
                   WHERE chat_id=? AND status='processing'
                     AND replacement_message_id IS NULL
                     AND updated_at<?""",
                (
                    _iso(at or utc_now()),
                    int(chat_id),
                    _iso(older_than),
                ),
            )
            db.commit()
            return int(cursor.rowcount)

    def retryable_photos(
        self,
        chat_id: int,
        limit: int = 20,
    ) -> tuple[PendingPhoto, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id,source_message_id,encrypted_payload,
                          processing_attempts,updated_at
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
                file_id, caption, thread_id = self._decrypt_payload(
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
            jobs.append(
                PendingPhoto(
                    chat_id=int(row["chat_id"]),
                    source_message_id=int(row["source_message_id"]),
                    source_file_id=file_id,
                    client_caption=caption,
                    message_thread_id=thread_id,
                    attempts=int(row["processing_attempts"]),
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
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
                       last_error_code=NULL,updated_at=?
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
                   WHERE chat_id=? AND replacement_message_id=?""",
                (int(chat_id), int(message_id)),
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

    def cached_name(
        self,
        keys: Sequence[str],
        at: datetime | None = None,
    ) -> CachedName | None:
        normalized = tuple(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))
        if not normalized:
            return None
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as db:
            row = db.execute(
                f"""SELECT model_name,confidence,source_count
                    FROM sales_photo_name_cache
                    WHERE cache_key IN ({placeholders}) AND expires_at>?
                    ORDER BY confidence DESC, source_count DESC
                    LIMIT 1""",
                (*normalized, _iso(at or utc_now())),
            ).fetchone()
        if row is None:
            return None
        return CachedName(
            model_name=str(row["model_name"]),
            confidence=float(row["confidence"]),
            source_count=int(row["source_count"]),
        )

    def cache_name(
        self,
        keys: Sequence[str],
        model_name: str,
        confidence: float,
        source_count: int,
        provider_model: str,
        expires_at: datetime,
        at: datetime | None = None,
    ) -> None:
        normalized = tuple(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))
        if not normalized:
            return
        now = _iso(at or utc_now())
        with self._connect() as db:
            db.executemany(
                """INSERT INTO sales_photo_name_cache(
                       cache_key,model_name,confidence,source_count,provider_model,
                       expires_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       model_name=excluded.model_name,
                       confidence=excluded.confidence,
                       source_count=excluded.source_count,
                       provider_model=excluded.provider_model,
                       expires_at=excluded.expires_at,
                       updated_at=excluded.updated_at""",
                [
                    (
                        key,
                        str(model_name)[:160],
                        float(confidence),
                        int(source_count),
                        str(provider_model)[:100],
                        _iso(expires_at),
                        now,
                    )
                    for key in normalized
                ],
            )
            db.commit()
