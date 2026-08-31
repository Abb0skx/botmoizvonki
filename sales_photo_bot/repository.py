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
from typing import Iterator

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
    source_file_ids: tuple[str, ...]
    source_message_ids: tuple[int, ...]
    source_kind: str
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
        *,
        source_file_ids: tuple[str, ...] | None = None,
        source_message_ids: tuple[int, ...] | None = None,
        source_kind: str = "photo",
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
