"""Durable SQLite persistence for the server-side price subsystem.

The repository owns persistence only.  Network calls and rendering stay in the
service layer.  Mutations use short ``BEGIN IMMEDIATE`` transactions so an
incomplete snapshot or half-claimed job can never become visible.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import PriceSettings


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ACTION_ALIASES = {
    "send": "send",
    "publish_new": "send",
    "edit": "edit",
    "edit_current": "edit",
}
_SCHEDULES = {"once", "daily", "weekly"}
_TERMINAL_JOBS = {"done", "failed", "cancelled", "needs_review"}


class SnapshotValidationError(ValueError):
    """Raised when a snapshot cannot be safely stored."""


class StaleSnapshotError(SnapshotValidationError):
    """Raised when an older snapshot attempts to replace the current one."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
 snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
 schema_version INTEGER NOT NULL,
 content_hash TEXT NOT NULL UNIQUE,
 generated_at TEXT NOT NULL,
 timezone TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 section_count INTEGER NOT NULL,
 product_count INTEGER NOT NULL,
 received_at TEXT NOT NULL,
 is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)));
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_snapshot_current
 ON price_snapshots(is_current) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS idx_price_snapshot_generated
 ON price_snapshots(generated_at DESC,snapshot_id DESC);

CREATE TABLE IF NOT EXISTS price_sections (
 snapshot_id INTEGER NOT NULL,
 section_key TEXT NOT NULL,
 position INTEGER NOT NULL,
 title TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 product_count INTEGER NOT NULL DEFAULT 0,
 changed_recent INTEGER NOT NULL DEFAULT 0 CHECK(changed_recent IN (0,1)),
 PRIMARY KEY(snapshot_id,section_key),
 FOREIGN KEY(snapshot_id) REFERENCES price_snapshots(snapshot_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_price_sections_order
 ON price_sections(snapshot_id,position,section_key);

CREATE TABLE IF NOT EXISTS telegram_posts (
 post_id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_key TEXT NOT NULL UNIQUE,
 publication_id TEXT NOT NULL DEFAULT '',
 section_key TEXT NOT NULL,
 section_name TEXT NOT NULL,
 channel_id TEXT NOT NULL,
 channel_username TEXT NOT NULL DEFAULT '',
 part_no INTEGER NOT NULL DEFAULT 1 CHECK(part_no>=1),
 part_count INTEGER NOT NULL DEFAULT 1 CHECK(part_count>=1),
 message_id INTEGER NOT NULL CHECK(message_id>0),
 post_url TEXT NOT NULL DEFAULT '',
 content_hash TEXT NOT NULL DEFAULT '',
 snapshot_id TEXT NOT NULL DEFAULT '',
 publication_mode TEXT NOT NULL DEFAULT 'send',
 sent_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'published',
 last_error TEXT,
 is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
 html_text TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_telegram_posts_section
 ON telegram_posts(section_key,channel_id,part_no);

CREATE TABLE IF NOT EXISTS publication_jobs (
 job_id INTEGER PRIMARY KEY AUTOINCREMENT,
 dedupe_key TEXT NOT NULL UNIQUE,
 action TEXT NOT NULL,
 section_key TEXT NOT NULL,
 channel_id TEXT NOT NULL,
 channel_key TEXT NOT NULL DEFAULT '',
 snapshot_policy TEXT NOT NULL DEFAULT 'latest',
 snapshot_id INTEGER,
 execute_at TEXT NOT NULL,
 schedule_type TEXT NOT NULL DEFAULT 'once',
 schedule_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 8,
 lease_token TEXT,
 lease_expires_at TEXT,
 next_attempt_at TEXT,
 payload_json TEXT NOT NULL DEFAULT '{}',
 result_json TEXT,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 completed_at TEXT,
 FOREIGN KEY(snapshot_id) REFERENCES price_snapshots(snapshot_id));
CREATE INDEX IF NOT EXISTS idx_publication_jobs_due
 ON publication_jobs(status,execute_at,next_attempt_at,job_id);
CREATE INDEX IF NOT EXISTS idx_publication_jobs_section
 ON publication_jobs(section_key,created_at DESC);

CREATE TABLE IF NOT EXISTS sheets_outbox (
 outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
 dedupe_key TEXT NOT NULL UNIQUE,
 entity_type TEXT NOT NULL,
 entity_key TEXT NOT NULL,
 operation TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 12,
 available_at TEXT NOT NULL,
 lease_token TEXT,
 lease_expires_at TEXT,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 synced_at TEXT);
CREATE INDEX IF NOT EXISTS idx_sheets_outbox_due
 ON sheets_outbox(status,available_at,outbox_id);

CREATE TABLE IF NOT EXISTS audit_log (
 audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_type TEXT NOT NULL,
 entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_audit_entity
 ON audit_log(entity_type,entity_id,audit_id DESC);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: datetime | str | None, name: str, *, default: bool = False) -> datetime:
    if value is None:
        if default:
            return _now()
        raise ValueError(f"{name} is required")
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not strict JSON") from exc


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _key(value: Any) -> str:
    text = str(value or "").strip()
    if _SECTION_RE.fullmatch(text) is None:
        raise ValueError("section_key has an invalid format")
    return text


def _canonical_hash(value: Mapping[str, Any]) -> str:
    # Keep this byte-for-byte compatible with contracts.canonical_content_hash.
    content = {
        "html_document": value.get("html_document", ""),
        "products": value.get("products", []),
        "sections": value.get("sections", []),
    }
    return hashlib.sha256(_dump(content).encode("utf-8")).hexdigest()


def _extract_sections(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    sections = payload.get("sections")
    if sections is None:
        sections = []
        groups = payload.get("catalog_groups")
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
            raise SnapshotValidationError("sections or catalog_groups are required")
        for group in groups:
            if not isinstance(group, Mapping):
                raise SnapshotValidationError("catalog group must be an object")
            entries = group.get("sections", group.get("entries", []))
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise SnapshotValidationError("catalog sections must be a list")
            sections.extend(entries)
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        raise SnapshotValidationError("sections must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fallback, raw in enumerate(sections):
        if not isinstance(raw, Mapping):
            raise SnapshotValidationError("section must be an object")
        item = dict(raw)
        try:
            section_key = _key(item.get("section_key") or item.get("section_id") or item.get("key"))
        except ValueError as exc:
            raise SnapshotValidationError(str(exc)) from exc
        if section_key in seen:
            raise SnapshotValidationError(f"duplicate section_key: {section_key}")
        seen.add(section_key)
        try:
            position = int(item.get("position", fallback))
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError("section position must be an integer") from exc
        if position < 0:
            raise SnapshotValidationError("section position cannot be negative")
        title = str(item.get("price_title") or item.get("title") or
                    item.get("catalog_label") or section_key).strip()
        if not title or len(title) > 500 or "\x00" in title:
            raise SnapshotValidationError("section title is invalid")
        products = item.get("products")
        if isinstance(products, Sequence) and not isinstance(products, (str, bytes)):
            product_count = len(products)
            changed = any(isinstance(p, Mapping) and p.get("changed_recent") for p in products)
        else:
            try:
                product_count = max(0, int(item.get("product_count", 0)))
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError("section product_count is invalid") from exc
            changed = bool(item.get("changed_recently", item.get("changed_recent")))
        section_content = {
            "title": item.get("title", ""),
            "plain_text": item.get("plain_text", ""),
            "clipboard_html": item.get("clipboard_html", ""),
            "telegram_blocks": item.get("telegram_blocks", []),
        }
        section_hash = hashlib.sha256(_dump(section_content).encode("utf-8")).hexdigest()
        supplied_hash = str(item.get("content_hash") or "")
        if supplied_hash and supplied_hash != section_hash:
            raise SnapshotValidationError(
                f"section content hash mismatch: {section_key}"
            )
        result.append({"section_key": section_key, "position": position,
                       "title": title, "content_hash": section_hash,
                       "payload_json": _dump(item), "product_count": product_count,
                       "changed_recent": int(bool(changed))})
    if not result:
        raise SnapshotValidationError("snapshot has no sections")
    return result


class PriceRepository:
    """SQLite repository for snapshots, Telegram posts, and durable queues."""

    def __init__(self, settings: PriceSettings | Path | str | None = None) -> None:
        if settings is None:
            resolved = PriceSettings.load()
            path = resolved.db_path
        elif isinstance(settings, PriceSettings):
            resolved = settings
            path = settings.db_path
        else:
            resolved = None
            path = Path(settings)
        self.settings = resolved
        self.path = Path(path)
        self.timezone = resolved.timezone if resolved else "Asia/Tashkent"
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _tx(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def migrate(self) -> None:
        """Install the idempotent schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _audit(db: sqlite3.Connection, event: str, kind: str, entity: Any,
               payload: Mapping[str, Any] | None, at: datetime) -> None:
        db.execute("INSERT INTO audit_log(event_type,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                   (event[:100], kind[:100], str(entity)[:500], _dump(dict(payload or {})), _iso(at)))

    @staticmethod
    def _snapshot(row: sqlite3.Row | None, include_payload: bool = True) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["is_current"] = bool(result["is_current"])
        raw = result.pop("payload_json")
        if include_payload:
            result["payload"] = _load(raw, {})
        return result

    @staticmethod
    def _section(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["changed_recent"] = bool(result["changed_recent"])
        result["payload"] = _load(result.pop("payload_json"), {})
        return result

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["schedule"] = _load(result.pop("schedule_json"), {})
        result["payload"] = _load(result.pop("payload_json"), {})
        result["result"] = _load(result.pop("result_json"), None)
        return result

    @staticmethod
    def _outbox(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = _load(result.pop("payload_json"), {})
        return result

    def ingest_snapshot(
        self,
        payload: Mapping[str, Any],
        *,
        content_hash: str | None = None,
        idempotency_key: str | None = None,
        received_at: datetime | str | None = None,
        reject_stale: bool = True,
    ) -> dict[str, Any]:
        """Atomically validate, persist, and activate a complete snapshot."""
        if not isinstance(payload, Mapping):
            raise SnapshotValidationError("snapshot payload must be an object")
        body = dict(payload)
        raw_version = body.get("schema_version")
        if isinstance(raw_version, bool):
            raise SnapshotValidationError("schema_version must be an integer")
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError("schema_version must be an integer") from exc
        if version < 1:
            raise SnapshotValidationError("schema_version must be positive")
        try:
            generated = _dt(body.get("generated_at"), "generated_at")
        except ValueError as exc:
            raise SnapshotValidationError(str(exc)) from exc
        zone_name = str(body.get("timezone") or self.timezone).strip()
        try:
            ZoneInfo(zone_name)
        except (KeyError, ValueError) as exc:
            raise SnapshotValidationError("snapshot timezone is invalid") from exc
        sections = _extract_sections(body)
        products = body.get("products", [])
        if not isinstance(products, Sequence) or isinstance(products, (str, bytes)):
            raise SnapshotValidationError("products must be a list")
        digest = _canonical_hash(body)
        declarations = [content_hash, idempotency_key,
                        body.get("content_sha256"), body.get("content_hash")]
        for declared in declarations:
            if declared is None or not str(declared).strip():
                continue
            value = str(declared).strip().casefold()
            if _HASH_RE.fullmatch(value) is None:
                raise SnapshotValidationError("content hash must be SHA-256")
            if not secrets.compare_digest(value, digest):
                raise SnapshotValidationError("snapshot content hash mismatch")
        received = _dt(received_at, "received_at", default=True)
        stored = dict(body)
        stored.pop("content_hash", None)
        stored["content_sha256"] = digest
        payload_json = _dump(stored)

        with self._tx(True) as db:
            current = db.execute(
                "SELECT * FROM price_snapshots WHERE is_current=1"
            ).fetchone()
            if current is not None and secrets.compare_digest(current["content_hash"], digest):
                result = self._snapshot(current)
                assert result is not None
                result.update(created=False, duplicate=True, current=True)
                return result
            if current is not None and reject_stale:
                if generated <= _dt(current["generated_at"], "current generated_at"):
                    raise StaleSnapshotError(
                        "snapshot generated_at is not newer than current snapshot"
                    )
            existing = db.execute(
                "SELECT * FROM price_snapshots WHERE content_hash=?", (digest,)
            ).fetchone()
            if existing is not None:
                result = self._snapshot(existing)
                assert result is not None
                result.update(created=False, duplicate=True,
                              current=bool(existing["is_current"]))
                return result
            db.execute("UPDATE price_snapshots SET is_current=0 WHERE is_current=1")
            cursor = db.execute(
                """INSERT INTO price_snapshots(
                     schema_version,content_hash,generated_at,timezone,payload_json,
                     section_count,product_count,received_at,is_current)
                     VALUES(?,?,?,?,?,?,?,?,1)""",
                (version, digest, _iso(generated), zone_name, payload_json,
                 len(sections), len(products), _iso(received)),
            )
            snapshot_id = int(cursor.lastrowid)
            db.executemany(
                """INSERT INTO price_sections(
                     snapshot_id,section_key,position,title,content_hash,
                     payload_json,product_count,changed_recent)
                     VALUES(?,?,?,?,?,?,?,?)""",
                [(snapshot_id, item["section_key"], item["position"], item["title"],
                  item["content_hash"], item["payload_json"],
                  item["product_count"], item["changed_recent"])
                 for item in sections],
            )
            self._audit(db, "snapshot_ingested", "price_snapshot", snapshot_id,
                        {"content_hash": digest, "generated_at": _iso(generated),
                         "section_count": len(sections), "product_count": len(products)},
                        received)
            row = db.execute(
                "SELECT * FROM price_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            result = self._snapshot(row)
            assert result is not None
            result.update(created=True, duplicate=False, current=True)
            return result

    save_snapshot = ingest_snapshot

    def get_current_snapshot(self, *, include_payload: bool = True) -> dict[str, Any] | None:
        """Return the currently active snapshot."""
        with self._read() as db:
            row = db.execute("SELECT * FROM price_snapshots WHERE is_current=1").fetchone()
        return self._snapshot(row, include_payload)

    current_snapshot = get_current_snapshot

    def get_snapshot(self, snapshot_id: int, *, include_payload: bool = True) -> dict[str, Any] | None:
        """Return an immutable snapshot by ID."""
        with self._read() as db:
            row = db.execute("SELECT * FROM price_snapshots WHERE snapshot_id=?",
                             (int(snapshot_id),)).fetchone()
        return self._snapshot(row, include_payload)

    def list_snapshots(self, *, limit: int = 50,
                       include_payload: bool = False) -> list[dict[str, Any]]:
        """List newest snapshots."""
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM price_snapshots ORDER BY generated_at DESC,snapshot_id DESC LIMIT ?",
                (min(500, max(1, int(limit))),),
            ).fetchall()
        return [item for row in rows
                if (item := self._snapshot(row, include_payload)) is not None]

    def list_sections(self, snapshot_id: int | None = None) -> list[dict[str, Any]]:
        """List a snapshot's sections in catalog order."""
        with self._read() as db:
            if snapshot_id is None:
                rows = db.execute(
                    """SELECT s.* FROM price_sections s JOIN price_snapshots p
                       ON p.snapshot_id=s.snapshot_id WHERE p.is_current=1
                       ORDER BY s.position,s.section_key"""
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM price_sections WHERE snapshot_id=? ORDER BY position,section_key",
                    (int(snapshot_id),),
                ).fetchall()
        return [item for row in rows if (item := self._section(row)) is not None]

    list_current_sections = list_sections

    def get_section(self, section_key: str,
                    snapshot_id: int | None = None) -> dict[str, Any] | None:
        """Return one section from a specified or current snapshot."""
        key = _key(section_key)
        with self._read() as db:
            if snapshot_id is None:
                row = db.execute(
                    """SELECT s.* FROM price_sections s JOIN price_snapshots p
                       ON p.snapshot_id=s.snapshot_id
                       WHERE p.is_current=1 AND s.section_key=?""", (key,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM price_sections WHERE snapshot_id=? AND section_key=?",
                    (int(snapshot_id), key),
                ).fetchone()
        return self._section(row)

    get_current_section = get_section

    @staticmethod
    def _post(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["is_current"] = bool(result["is_current"])
        return result

    def _normalize_post(self, data: Mapping[str, Any], at: datetime) -> dict[str, Any]:
        section_key = _key(data.get("section_key"))
        channel_id = str(data.get("channel_id") or "").strip()
        if not channel_id or len(channel_id) > 100:
            raise ValueError("channel_id is invalid")
        try:
            part_no = int(data.get("part_no", 1))
            message_id = int(data.get("message_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("part_no and message_id must be integers") from exc
        if part_no < 1 or message_id < 1:
            raise ValueError("part_no and message_id must be positive")
        sent = _dt(data.get("sent_at") or at, "sent_at")
        record_key = str(data.get("record_key") or
                         f"{channel_id}:{section_key}:{part_no}").strip()
        if not record_key or len(record_key) > 500:
            raise ValueError("record_key is invalid")
        section_name = str(data.get("section_name") or section_key).strip()
        if not section_name or len(section_name) > 500:
            raise ValueError("section_name is invalid")
        return {
            "record_key": record_key,
            "publication_id": str(data.get("publication_id") or "").strip()[:100],
            "section_key": section_key,
            "section_name": section_name,
            "channel_id": channel_id,
            "channel_username": str(data.get("channel_username") or "").strip().lstrip("@")[:100],
            "part_no": part_no,
            "part_count": max(part_no, int(data.get("part_count", part_no))),
            "message_id": message_id,
            "post_url": str(data.get("post_url") or "").strip()[:2048],
            "content_hash": str(data.get("content_hash") or "").strip()[:128],
            "snapshot_id": str(data.get("snapshot_id") or "").strip()[:100],
            "publication_mode": str(data.get("publication_mode") or data.get("mode") or
                                    "send").strip()[:50],
            "sent_at": _iso(sent),
            "updated_at": _iso(at),
            "status": str(data.get("status") or "published").strip()[:50],
            "last_error": str(data["last_error"])[:1000] if data.get("last_error") else None,
            "is_current": int(bool(data.get("is_current", data.get("current", True)))),
            "html_text": str(data.get("html_text") or ""),
        }

    def _upsert_post_tx(self, db: sqlite3.Connection,
                        values: Mapping[str, Any]) -> sqlite3.Row:
        db.execute(
            """INSERT INTO telegram_posts(
                 record_key,publication_id,section_key,section_name,channel_id,channel_username,
                 part_no,part_count,message_id,post_url,content_hash,snapshot_id,publication_mode,
                 sent_at,updated_at,status,last_error,is_current,html_text)
                 VALUES(:record_key,:publication_id,:section_key,:section_name,:channel_id,:channel_username,
                        :part_no,:part_count,:message_id,:post_url,:content_hash,:snapshot_id,:publication_mode,
                        :sent_at,:updated_at,:status,:last_error,:is_current,:html_text)
                 ON CONFLICT(record_key) DO UPDATE SET
                  publication_id=excluded.publication_id,section_key=excluded.section_key,
                  section_name=excluded.section_name,channel_id=excluded.channel_id,
                  channel_username=excluded.channel_username,message_id=excluded.message_id,
                  part_no=excluded.part_no,part_count=excluded.part_count,
                  post_url=excluded.post_url,content_hash=excluded.content_hash,
                  snapshot_id=excluded.snapshot_id,publication_mode=excluded.publication_mode,
                  sent_at=excluded.sent_at,
                  updated_at=excluded.updated_at,status=excluded.status,
                  last_error=excluded.last_error,is_current=excluded.is_current,
                  html_text=excluded.html_text""", values)
        row = db.execute(
            "SELECT * FROM telegram_posts WHERE record_key=?",
            (values["record_key"],),
        ).fetchone()
        assert row is not None
        return row

    def upsert_telegram_post(
        self,
        post: Mapping[str, Any] | None = None,
        *,
        enqueue_sheet_sync: bool = True,
        replace_current: bool = False,
        now: datetime | str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Upsert one Telegram registry row.

        Use :meth:`replace_current_telegram_posts` for a multi-part publication;
        it clears the old current set exactly once in the same transaction.
        """
        data = dict(post or {})
        data.update(fields)
        at = _dt(now, "now", default=True)
        values = self._normalize_post(data, at)
        with self._tx(True) as db:
            if replace_current and values["is_current"]:
                db.execute(
                    "UPDATE telegram_posts SET is_current=0,updated_at=? WHERE section_key=? AND channel_id=? AND is_current=1",
                    (_iso(at), values["section_key"], values["channel_id"]),
                )
            row = self._upsert_post_tx(db, values)
            result = self._post(row)
            assert result is not None
            if enqueue_sheet_sync:
                registry_hash = hashlib.sha256(_dump(result).encode("utf-8")).hexdigest()
                self._queue_outbox_tx(
                    db, "telegram_post", values["record_key"], "upsert", result,
                    f"telegram_post:{values['record_key']}:{registry_hash}",
                    at, at, 12,
                )
            self._audit(db, "telegram_post_upserted", "telegram_post",
                        values["record_key"],
                        {"section_key": values["section_key"],
                         "channel_id": values["channel_id"],
                         "part_no": values["part_no"],
                         "message_id": values["message_id"]}, at)
            return result

    upsert_post = upsert_telegram_post

    def replace_current_telegram_posts(
        self,
        section_key: str,
        channel_id: str,
        posts: Sequence[Mapping[str, Any]],
        *,
        enqueue_sheet_sync: bool = True,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically replace all current parts for one section/channel."""
        key = _key(section_key)
        channel = str(channel_id).strip()
        if not channel:
            raise ValueError("channel_id is required")
        if not isinstance(posts, Sequence) or isinstance(posts, (str, bytes)) or not posts:
            raise ValueError("posts must be a non-empty list")
        at = _dt(now, "now", default=True)
        normalized: list[dict[str, Any]] = []
        seen_parts: set[int] = set()
        for raw in posts:
            item = dict(raw)
            item["section_key"] = key
            item["channel_id"] = channel
            item["is_current"] = True
            values = self._normalize_post(item, at)
            if values["part_no"] in seen_parts:
                raise ValueError("posts contain duplicate part_no")
            seen_parts.add(values["part_no"])
            normalized.append(values)
        with self._tx(True) as db:
            old_rows = db.execute(
                "SELECT * FROM telegram_posts WHERE section_key=? AND channel_id=? AND is_current=1",
                (key, channel),
            ).fetchall()
            db.execute(
                "UPDATE telegram_posts SET is_current=0,updated_at=? WHERE section_key=? AND channel_id=? AND is_current=1",
                (_iso(at), key, channel),
            )
            results: list[dict[str, Any]] = []
            for values in normalized:
                row = self._upsert_post_tx(db, values)
                result = self._post(row)
                assert result is not None
                results.append(result)
                if enqueue_sheet_sync:
                    registry_hash = hashlib.sha256(_dump(result).encode("utf-8")).hexdigest()
                    self._queue_outbox_tx(
                        db, "telegram_post", values["record_key"], "upsert", result,
                        f"telegram_post:{values['record_key']}:{registry_hash}",
                        at, at, 12,
                    )
            if enqueue_sheet_sync:
                new_keys = {item["record_key"] for item in results}
                for old in old_rows:
                    if old["record_key"] in new_keys:
                        continue
                    old_payload = dict(old)
                    old_payload["is_current"] = 0
                    old_payload["updated_at"] = _iso(at)
                    self._queue_outbox_tx(
                        db, "telegram_post", old["record_key"], "upsert", old_payload,
                        f"telegram_post:{old['record_key']}:archived:{_iso(at)}",
                        at, at, 12,
                    )
            self._audit(db, "telegram_post_set_replaced", "telegram_section",
                        f"{channel}:{key}", {"parts": len(results)}, at)
            return results

    def get_telegram_post(self, section_key: str, channel_id: str,
                          part_no: int = 1, *, current_only: bool = True) -> dict[str, Any] | None:
        """Return one saved Telegram part."""
        sql = "SELECT * FROM telegram_posts WHERE section_key=? AND channel_id=? AND part_no=?"
        params: list[Any] = [_key(section_key), str(channel_id), int(part_no)]
        if current_only:
            sql += " AND is_current=1"
        sql += " ORDER BY updated_at DESC,post_id DESC LIMIT 1"
        with self._read() as db:
            row = db.execute(sql, params).fetchone()
        return self._post(row)

    get_post = get_telegram_post

    def list_telegram_posts(
        self,
        section_key: str | None = None,
        channel_id: str | None = None,
        current_only: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List Telegram registry rows with optional current filtering."""
        where: list[str] = []
        params: list[Any] = []
        if section_key is not None:
            where.append("section_key=?")
            params.append(_key(section_key))
        if channel_id is not None:
            where.append("channel_id=?")
            params.append(str(channel_id))
        if current_only:
            where.append("is_current=1")
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.append(min(5000, max(1, int(limit))))
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM telegram_posts" + clause +
                " ORDER BY section_key,channel_id,is_current DESC,part_no LIMIT ?", params
            ).fetchall()
        return [item for row in rows if (item := self._post(row)) is not None]

    list_posts = list_telegram_posts

    def enqueue_job(
        self,
        section_key: str,
        action: str,
        execute_at: datetime | str,
        *,
        channel_id: str,
        channel_key: str = "",
        snapshot_policy: str = "latest",
        snapshot_id: int | None = None,
        schedule_type: str = "once",
        schedule: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 8,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Enqueue an idempotent one-time or recurring publication job."""
        key = _key(section_key)
        normalized_action = _ACTION_ALIASES.get(str(action).strip().casefold())
        if normalized_action is None:
            raise ValueError("action must be send or edit")
        channel = str(channel_id or "").strip()
        if not channel or len(channel) > 100:
            raise ValueError("channel_id is invalid")
        policy = str(snapshot_policy or "latest").strip().casefold()
        if policy == "fixed":
            policy = "pinned"
        if policy not in {"latest", "pinned"}:
            raise ValueError("snapshot_policy must be latest or pinned")
        if policy == "pinned" and snapshot_id is None:
            raise ValueError("pinned snapshot_policy requires snapshot_id")
        schedule_kind = str(schedule_type or "once").strip().casefold()
        if schedule_kind not in _SCHEDULES:
            raise ValueError("unsupported schedule_type")
        execute = _dt(execute_at, "execute_at")
        created = _dt(now, "now", default=True)
        attempts_limit = int(max_attempts)
        if not 1 <= attempts_limit <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        schedule_data = dict(schedule or {})
        if schedule_kind != "once":
            self._validate_schedule(schedule_kind, schedule_data, execute)
        payload_data = dict(payload or {})
        unique = str(dedupe_key or f"job:{uuid.uuid4().hex}").strip()
        if not unique or len(unique) > 500:
            raise ValueError("dedupe_key is invalid")
        with self._tx(True) as db:
            existing = db.execute(
                "SELECT * FROM publication_jobs WHERE dedupe_key=?", (unique,)
            ).fetchone()
            if existing is not None:
                result = self._job(existing)
                assert result is not None
                result["duplicate"] = True
                return result
            if snapshot_id is not None and db.execute(
                "SELECT 1 FROM price_snapshots WHERE snapshot_id=?",
                (int(snapshot_id),),
            ).fetchone() is None:
                raise ValueError("snapshot_id does not exist")
            cursor = db.execute(
                """INSERT INTO publication_jobs(
                     dedupe_key,action,section_key,channel_id,channel_key,
                     snapshot_policy,snapshot_id,execute_at,schedule_type,
                     schedule_json,status,attempts,max_attempts,payload_json,
                     created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,'pending',0,?,?,?,?)""",
                (unique, normalized_action, key, channel, str(channel_key or "")[:100],
                 policy, int(snapshot_id) if snapshot_id is not None else None,
                 _iso(execute), schedule_kind, _dump(schedule_data), attempts_limit,
                 _dump(payload_data), _iso(created), _iso(created)),
            )
            job_id = int(cursor.lastrowid)
            self._audit(db, "publication_job_enqueued", "publication_job", job_id,
                        {"action": normalized_action, "section_key": key,
                         "execute_at": _iso(execute), "schedule_type": schedule_kind},
                        created)
            result = self._job(db.execute(
                "SELECT * FROM publication_jobs WHERE job_id=?", (job_id,)
            ).fetchone())
            assert result is not None
            result["duplicate"] = False
            return result

    enqueue_publication_job = enqueue_job

    @staticmethod
    def _parse_schedule_time(value: Any, fallback: time) -> time:
        if value is None or str(value).strip() == "":
            return fallback.replace(tzinfo=None)
        parts = str(value).strip().split(":")
        if len(parts) != 2:
            raise ValueError("schedule time must use HH:MM")
        try:
            return time(int(parts[0]), int(parts[1]))
        except ValueError as exc:
            raise ValueError("schedule time must use HH:MM") from exc

    def _validate_schedule(self, kind: str, schedule: Mapping[str, Any],
                           anchor: datetime) -> None:
        zone_name = str(schedule.get("timezone") or self.timezone)
        try:
            zone = ZoneInfo(zone_name)
        except (KeyError, ValueError) as exc:
            raise ValueError("schedule timezone is invalid") from exc
        self._parse_schedule_time(schedule.get("time") or schedule.get("daily_time"),
                                  anchor.astimezone(zone).time())
        if kind == "weekly":
            days = schedule.get("weekdays")
            if not isinstance(days, Sequence) or isinstance(days, (str, bytes)):
                raise ValueError("weekly schedule requires weekdays")
            try:
                normalized = {int(day) for day in days}
            except (TypeError, ValueError) as exc:
                raise ValueError("weekdays must be ISO integers") from exc
            if not normalized or any(day < 1 or day > 7 for day in normalized):
                raise ValueError("weekdays must be between 1 and 7")

    def _next_occurrence(self, kind: str, current: datetime,
                         schedule: Mapping[str, Any]) -> datetime:
        zone = ZoneInfo(str(schedule.get("timezone") or self.timezone))
        local = current.astimezone(zone)
        wall_time = self._parse_schedule_time(
            schedule.get("time") or schedule.get("daily_time"), local.time()
        )
        next_date = local.date() + timedelta(days=1)
        if kind == "weekly":
            weekdays = {int(day) for day in schedule.get("weekdays", [])}
            for _ in range(7):
                if next_date.isoweekday() in weekdays:
                    break
                next_date += timedelta(days=1)
        return datetime.combine(next_date, wall_time, tzinfo=zone).astimezone(timezone.utc)

    def materialize_due_schedules(self, now_utc: datetime | str,
                                  horizon_days: int = 1) -> int:
        """Compatibility hook; recurring rows reschedule themselves on completion."""
        _dt(now_utc, "now_utc")
        if not 0 <= int(horizon_days) <= 31:
            raise ValueError("horizon_days must be between 0 and 31")
        return 0

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        """Return one publication job."""
        with self._read() as db:
            row = db.execute("SELECT * FROM publication_jobs WHERE job_id=?",
                             (int(job_id),)).fetchone()
        return self._job(row)

    def list_jobs(self, *, status: str | Sequence[str] | None = None,
                  section_key: str | None = None,
                  limit: int = 500) -> list[dict[str, Any]]:
        """List publication jobs, newest first."""
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            if statuses:
                where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
                params.extend(str(item) for item in statuses)
        if section_key is not None:
            where.append("section_key=?")
            params.append(_key(section_key))
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.append(min(5000, max(1, int(limit))))
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM publication_jobs" + clause +
                " ORDER BY created_at DESC,job_id DESC LIMIT ?", params
            ).fetchall()
        return [item for row in rows if (item := self._job(row)) is not None]

    @staticmethod
    def _recover_jobs_tx(db: sqlite3.Connection, now: datetime) -> int:
        cursor = db.execute(
            """UPDATE publication_jobs SET status='pending',lease_token=NULL,
               lease_expires_at=NULL,updated_at=? WHERE status='running'
               AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (_iso(now), _iso(now)),
        )
        return max(0, cursor.rowcount)

    def recover_stale_jobs(self, now_utc: datetime | str) -> int:
        """Release expired worker leases after a restart or deploy."""
        now = _dt(now_utc, "now_utc")
        with self._tx(True) as db:
            return self._recover_jobs_tx(db, now)

    def claim_due_jobs(self, now_utc: datetime | str, limit: int = 1,
                       lease_seconds: int = 180) -> list[dict[str, Any]]:
        """Atomically claim due jobs and return lease-fenced mappings."""
        now = _dt(now_utc, "now_utc")
        bounded = min(100, max(1, int(limit)))
        expires = now + timedelta(seconds=min(3600, max(10, int(lease_seconds))))
        claimed: list[int] = []
        with self._tx(True) as db:
            self._recover_jobs_tx(db, now)
            rows = db.execute(
                """SELECT pending.job_id FROM publication_jobs AS pending
                   WHERE pending.status='pending' AND pending.execute_at<=?
                   AND (pending.next_attempt_at IS NULL OR pending.next_attempt_at<=?)
                   AND NOT EXISTS (
                     SELECT 1 FROM publication_jobs AS running
                     WHERE running.status='running'
                       AND running.section_key=pending.section_key
                       AND running.channel_id=pending.channel_id)
                   ORDER BY pending.execute_at,pending.job_id LIMIT ?""",
                (_iso(now), _iso(now), bounded),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(24)
                cursor = db.execute(
                    """UPDATE publication_jobs SET status='running',attempts=attempts+1,
                       lease_token=?,lease_expires_at=?,updated_at=?
                       WHERE job_id=? AND status='pending'""",
                    (token, _iso(expires), _iso(now), row["job_id"]),
                )
                if cursor.rowcount == 1:
                    claimed.append(int(row["job_id"]))
            if not claimed:
                return []
            marks = ",".join("?" for _ in claimed)
            fetched = db.execute(
                f"SELECT * FROM publication_jobs WHERE job_id IN ({marks}) ORDER BY execute_at,job_id",
                claimed,
            ).fetchall()
            return [item for row in fetched if (item := self._job(row)) is not None]

    def claim_due_job(self, now_utc: datetime | str,
                      lease_seconds: int = 180) -> dict[str, Any] | None:
        """Singular convenience wrapper used by thin schedulers."""
        jobs = self.claim_due_jobs(now_utc, limit=1, lease_seconds=lease_seconds)
        return jobs[0] if jobs else None

    def complete_job(self, job_id: int, lease_token: str,
                     now_utc: datetime | str,
                     result: Mapping[str, Any] | None = None) -> bool:
        """Complete a leased job, or advance its recurring schedule."""
        now = _dt(now_utc, "now_utc")
        with self._tx(True) as db:
            row = db.execute(
                "SELECT * FROM publication_jobs WHERE job_id=? AND status='running' AND lease_token=?",
                (int(job_id), str(lease_token)),
            ).fetchone()
            if row is None:
                return False
            if row["schedule_type"] == "once":
                status, execute_at, attempts, completed = "done", row["execute_at"], row["attempts"], _iso(now)
            else:
                schedule = _load(row["schedule_json"], {})
                next_at = self._next_occurrence(row["schedule_type"],
                                                _dt(row["execute_at"], "execute_at"), schedule)
                while next_at <= now:
                    next_at = self._next_occurrence(row["schedule_type"], next_at, schedule)
                status, execute_at, attempts, completed = "pending", _iso(next_at), 0, None
            cursor = db.execute(
                """UPDATE publication_jobs SET status=?,execute_at=?,attempts=?,
                   result_json=?,last_error=NULL,lease_token=NULL,lease_expires_at=NULL,
                   next_attempt_at=NULL,updated_at=?,completed_at=?
                   WHERE job_id=? AND status='running' AND lease_token=?""",
                (status, execute_at, attempts, _dump(dict(result or {})),
                 _iso(now), completed, int(job_id), str(lease_token)),
            )
            if cursor.rowcount != 1:
                return False
            self._audit(db, "publication_job_completed", "publication_job", job_id,
                        dict(result or {}), now)
            return True

    def retry_job(self, job_id: int, lease_token: str,
                  now_utc: datetime | str, error: Any, *,
                  retry_after_seconds: float | int | None = None,
                  permanent: bool = False,
                  needs_review: bool = False) -> bool:
        """Retry, fail, or fence an ambiguous leased publication job."""
        now = _dt(now_utc, "now_utc")
        safe_error = str(error or "publication failed").replace("\n", " ")[:1000]
        with self._tx(True) as db:
            row = db.execute(
                "SELECT * FROM publication_jobs WHERE job_id=? AND status='running' AND lease_token=?",
                (int(job_id), str(lease_token)),
            ).fetchone()
            if row is None:
                return False
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            status = "needs_review" if needs_review else (
                "failed" if permanent or exhausted else "pending"
            )
            if status == "pending":
                delay = (2 ** min(int(row["attempts"]), 11)
                         if retry_after_seconds is None else float(retry_after_seconds))
                next_attempt = _iso(now + timedelta(seconds=min(86400, max(0, delay))))
                completed = None
            else:
                next_attempt, completed = None, _iso(now)
            cursor = db.execute(
                """UPDATE publication_jobs SET status=?,next_attempt_at=?,last_error=?,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?,completed_at=?
                   WHERE job_id=? AND status='running' AND lease_token=?""",
                (status, next_attempt, safe_error, _iso(now), completed,
                 int(job_id), str(lease_token)),
            )
            if cursor.rowcount != 1:
                return False
            self._audit(db, "publication_job_retry" if status == "pending" else
                        "publication_job_failed", "publication_job", job_id,
                        {"status": status, "error": safe_error}, now)
            return True

    def fail_job(self, job_id: int, lease_token: str,
                 now_utc: datetime | str, error: Any, *,
                 retryable: bool = True,
                 retry_after_seconds: float | int | None = None,
                 needs_review: bool = False) -> bool:
        """Compatibility wrapper for scheduler implementations using fail_job."""
        return self.retry_job(job_id, lease_token, now_utc, error,
                              retry_after_seconds=retry_after_seconds,
                              permanent=not retryable,
                              needs_review=needs_review)

    def cancel_job(self, job_id: int, *,
                   now: datetime | str | None = None) -> bool:
        """Cancel a job that has not reached a terminal state."""
        at = _dt(now, "now", default=True)
        with self._tx(True) as db:
            cursor = db.execute(
                """UPDATE publication_jobs SET status='cancelled',lease_token=NULL,
                   lease_expires_at=NULL,next_attempt_at=NULL,updated_at=?,completed_at=?
                   WHERE job_id=? AND status='pending'""",
                (_iso(at), _iso(at), int(job_id)),
            )
            if cursor.rowcount != 1:
                return False
            self._audit(db, "publication_job_cancelled", "publication_job",
                        job_id, {}, at)
            return True

    @staticmethod
    def _queue_outbox_tx(
        db: sqlite3.Connection,
        entity_type: str,
        entity_key: str,
        operation: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        available_at: datetime,
        created_at: datetime,
        max_attempts: int,
    ) -> sqlite3.Row:
        db.execute(
            """INSERT INTO sheets_outbox(
                 dedupe_key,entity_type,entity_key,operation,payload_json,
                 status,attempts,max_attempts,available_at,created_at,updated_at)
                 VALUES(?,?,?,?,?,'pending',0,?,?,?,?)
                 ON CONFLICT(dedupe_key) DO NOTHING""",
            (dedupe_key[:500], entity_type[:100], entity_key[:500], operation[:50],
             _dump(dict(payload)), int(max_attempts), _iso(available_at),
             _iso(created_at), _iso(created_at)),
        )
        row = db.execute("SELECT * FROM sheets_outbox WHERE dedupe_key=?",
                         (dedupe_key[:500],)).fetchone()
        assert row is not None
        return row

    def enqueue_outbox(
        self,
        entity_type: str,
        entity_key: str,
        payload: Mapping[str, Any],
        *,
        operation: str = "upsert",
        dedupe_key: str | None = None,
        available_at: datetime | str | None = None,
        now: datetime | str | None = None,
        max_attempts: int = 12,
    ) -> dict[str, Any]:
        """Queue an idempotent Product Sort registry mutation."""
        kind = str(entity_type or "").strip()
        key = str(entity_key or "").strip()
        op = str(operation or "").strip()
        if not kind or not key or not op:
            raise ValueError("entity_type, entity_key and operation are required")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        at = _dt(now, "now", default=True)
        available = _dt(available_at, "available_at") if available_at is not None else at
        digest = hashlib.sha256(_dump(dict(payload)).encode()).hexdigest()
        unique = str(dedupe_key or f"{kind}:{key}:{op}:{digest}").strip()
        if not unique or len(unique) > 500:
            raise ValueError("dedupe_key is invalid")
        with self._tx(True) as db:
            row = self._queue_outbox_tx(
                db, kind, key, op, payload, unique, available, at,
                min(100, max(1, int(max_attempts))),
            )
            result = self._outbox(row)
            assert result is not None
            return result

    enqueue_sheets_outbox = enqueue_outbox

    @staticmethod
    def _recover_outbox_tx(db: sqlite3.Connection, now: datetime) -> int:
        cursor = db.execute(
            """UPDATE sheets_outbox SET status='pending',lease_token=NULL,
               lease_expires_at=NULL,updated_at=? WHERE status='running'
               AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
            (_iso(now), _iso(now)),
        )
        return max(0, cursor.rowcount)

    def recover_stale_outbox(self, now_utc: datetime | str) -> int:
        """Release abandoned Product Sort outbox leases."""
        now = _dt(now_utc, "now_utc")
        with self._tx(True) as db:
            return self._recover_outbox_tx(db, now)

    def claim_outbox(self, now_utc: datetime | str, limit: int = 20,
                     lease_seconds: int = 120) -> list[dict[str, Any]]:
        """Atomically claim due Product Sort updates."""
        now = _dt(now_utc, "now_utc")
        expires = now + timedelta(seconds=min(3600, max(10, int(lease_seconds))))
        claimed: list[int] = []
        with self._tx(True) as db:
            self._recover_outbox_tx(db, now)
            rows = db.execute(
                """SELECT outbox_id FROM sheets_outbox WHERE status='pending'
                   AND available_at<=? ORDER BY available_at,outbox_id LIMIT ?""",
                (_iso(now), min(100, max(1, int(limit)))),
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(24)
                cursor = db.execute(
                    """UPDATE sheets_outbox SET status='running',attempts=attempts+1,
                       lease_token=?,lease_expires_at=?,updated_at=?
                       WHERE outbox_id=? AND status='pending'""",
                    (token, _iso(expires), _iso(now), row["outbox_id"]),
                )
                if cursor.rowcount == 1:
                    claimed.append(int(row["outbox_id"]))
            if not claimed:
                return []
            marks = ",".join("?" for _ in claimed)
            fetched = db.execute(
                f"SELECT * FROM sheets_outbox WHERE outbox_id IN ({marks}) ORDER BY available_at,outbox_id",
                claimed,
            ).fetchall()
            return [item for row in fetched if (item := self._outbox(row)) is not None]

    claim_sheets_outbox = claim_outbox

    def complete_outbox(self, outbox_id: int, lease_token: str,
                        now_utc: datetime | str) -> bool:
        """Mark a leased Product Sort update as synced."""
        now = _dt(now_utc, "now_utc")
        with self._tx(True) as db:
            cursor = db.execute(
                """UPDATE sheets_outbox SET status='synced',last_error=NULL,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?,synced_at=?
                   WHERE outbox_id=? AND status='running' AND lease_token=?""",
                (_iso(now), _iso(now), int(outbox_id), str(lease_token)),
            )
            return cursor.rowcount == 1

    complete_sheets_outbox = complete_outbox

    def retry_outbox(self, outbox_id: int, lease_token: str,
                     now_utc: datetime | str, error: Any, *,
                     retry_after_seconds: float | int | None = None,
                     permanent: bool = False) -> bool:
        """Retry or terminally fail a leased Product Sort update."""
        now = _dt(now_utc, "now_utc")
        safe_error = str(error or "Sheets sync failed").replace("\n", " ")[:1000]
        with self._tx(True) as db:
            row = db.execute(
                "SELECT * FROM sheets_outbox WHERE outbox_id=? AND status='running' AND lease_token=?",
                (int(outbox_id), str(lease_token)),
            ).fetchone()
            if row is None:
                return False
            terminal = permanent or int(row["attempts"]) >= int(row["max_attempts"])
            if terminal:
                status, available = "failed", row["available_at"]
            else:
                delay = (2 ** min(int(row["attempts"]), 11)
                         if retry_after_seconds is None else float(retry_after_seconds))
                status = "pending"
                available = _iso(now + timedelta(seconds=min(86400, max(0, delay))))
            cursor = db.execute(
                """UPDATE sheets_outbox SET status=?,available_at=?,last_error=?,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE outbox_id=? AND status='running' AND lease_token=?""",
                (status, available, safe_error, _iso(now),
                 int(outbox_id), str(lease_token)),
            )
            return cursor.rowcount == 1

    fail_outbox = retry_outbox
    retry_sheets_outbox = retry_outbox

    def list_outbox(self, *, status: str | None = None,
                    limit: int = 500) -> list[dict[str, Any]]:
        """List Product Sort outbox rows for diagnostics."""
        bounded = min(5000, max(1, int(limit)))
        with self._read() as db:
            if status is None:
                rows = db.execute(
                    "SELECT * FROM sheets_outbox ORDER BY created_at DESC,outbox_id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM sheets_outbox WHERE status=? ORDER BY created_at DESC,outbox_id DESC LIMIT ?",
                    (str(status), bounded),
                ).fetchall()
        return [item for row in rows if (item := self._outbox(row)) is not None]

    list_sheets_outbox = list_outbox

    def record_audit(self, event_type: str, entity_type: str, entity_id: Any,
                     payload: Mapping[str, Any] | None = None, *,
                     now: datetime | str | None = None) -> int:
        """Append an audit event and return its ID."""
        at = _dt(now, "now", default=True)
        if not str(event_type).strip() or not str(entity_type).strip():
            raise ValueError("event_type and entity_type are required")
        with self._tx(True) as db:
            self._audit(db, str(event_type), str(entity_type), entity_id, payload, at)
            return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    def list_audit(self, *, entity_type: str | None = None,
                   entity_id: Any | None = None,
                   limit: int = 500) -> list[dict[str, Any]]:
        """List recent audit events."""
        where: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            where.append("entity_type=?")
            params.append(str(entity_type))
        if entity_id is not None:
            where.append("entity_id=?")
            params.append(str(entity_id))
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.append(min(5000, max(1, int(limit))))
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM audit_log" + clause +
                " ORDER BY created_at DESC,audit_id DESC LIMIT ?", params
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _load(item.pop("payload_json"), {})
            result.append(item)
        return result

    def healthcheck(self) -> dict[str, Any]:
        """Return a minimal database integrity report."""
        with self._read() as db:
            check = db.execute("PRAGMA quick_check").fetchone()
            current = db.execute(
                "SELECT snapshot_id FROM price_snapshots WHERE is_current=1"
            ).fetchone()
        return {"ok": bool(check and check[0] == "ok"),
                "database": check[0] if check else "missing",
                "current_snapshot_id": current[0] if current else None}


__all__ = [
    "PriceRepository",
    "SnapshotValidationError",
    "StaleSnapshotError",
]
