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
from calendar import monthrange
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import PriceSettings
from .calendar_plan import CALENDAR_PLAN_ENTRIES


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TELEGRAM_POST_URL_RE = re.compile(
    r"^https://t\.me/(?P<username>[A-Za-z0-9_]{5,32})/(?P<message_id>[1-9]\d*)$"
)
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

CREATE TABLE IF NOT EXISTS telegram_deletion_queue (
 deletion_id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_key TEXT NOT NULL UNIQUE,
 section_key TEXT NOT NULL,
 channel_id TEXT NOT NULL,
 message_id INTEGER NOT NULL CHECK(message_id>0),
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 12,
 next_attempt_at TEXT,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 completed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_telegram_deletions_due
 ON telegram_deletion_queue(status,next_attempt_at,deletion_id);

CREATE TABLE IF NOT EXISTS manual_deletion_requests (
 deletion_id INTEGER PRIMARY KEY,
 section_key TEXT NOT NULL,
 target_channel_id TEXT NOT NULL,
 target_message_id INTEGER NOT NULL CHECK(target_message_id>0),
 target_post_url TEXT NOT NULL DEFAULT '',
 request_channel_id TEXT NOT NULL,
 request_message_id INTEGER NOT NULL CHECK(request_message_id>0),
 status TEXT NOT NULL DEFAULT 'active',
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 completed_at TEXT,
 UNIQUE(request_channel_id,request_message_id),
 FOREIGN KEY(deletion_id) REFERENCES telegram_deletion_queue(deletion_id)
   ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_manual_deletion_requests_status
 ON manual_deletion_requests(status,deletion_id);

CREATE TABLE IF NOT EXISTS telegram_quick_link_posts (
 quick_post_key TEXT PRIMARY KEY,
 title TEXT NOT NULL,
 channel_id TEXT NOT NULL,
 channel_username TEXT NOT NULL DEFAULT '',
 message_id INTEGER NOT NULL CHECK(message_id>0),
 post_url TEXT NOT NULL,
 template_html TEXT NOT NULL,
 template_hash TEXT NOT NULL,
 last_rendered_html TEXT NOT NULL DEFAULT '',
 last_render_hash TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active',
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 last_edited_at TEXT,
 UNIQUE(channel_id,message_id));

CREATE TABLE IF NOT EXISTS telegram_quick_link_targets (
 quick_post_key TEXT NOT NULL,
 link_key TEXT NOT NULL,
 section_key TEXT NOT NULL,
 priority INTEGER NOT NULL DEFAULT 1 CHECK(priority>=1),
 fallback_url TEXT NOT NULL,
 PRIMARY KEY(quick_post_key,link_key,section_key),
 FOREIGN KEY(quick_post_key) REFERENCES telegram_quick_link_posts(quick_post_key)
   ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_quick_link_targets_section
 ON telegram_quick_link_targets(section_key,quick_post_key,link_key);

CREATE TABLE IF NOT EXISTS telegram_quick_link_applied_targets (
 quick_post_key TEXT NOT NULL,
 link_key TEXT NOT NULL,
 target_channel_id TEXT NOT NULL,
 target_message_id INTEGER NOT NULL CHECK(target_message_id>0),
 target_publication_id TEXT NOT NULL DEFAULT '',
 target_url TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(quick_post_key,link_key),
 FOREIGN KEY(quick_post_key) REFERENCES telegram_quick_link_posts(quick_post_key)
   ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_quick_link_applied_message
 ON telegram_quick_link_applied_targets(target_channel_id,target_message_id);

CREATE TABLE IF NOT EXISTS telegram_quick_link_queue (
 quick_post_key TEXT PRIMARY KEY,
 desired_revision INTEGER NOT NULL DEFAULT 1 CHECK(desired_revision>=1),
 claimed_revision INTEGER NOT NULL DEFAULT 0 CHECK(claimed_revision>=0),
 applied_revision INTEGER NOT NULL DEFAULT 0 CHECK(applied_revision>=0),
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 12,
 next_attempt_at TEXT,
 lease_token TEXT,
 lease_expires_at TEXT,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 completed_at TEXT,
 FOREIGN KEY(quick_post_key) REFERENCES telegram_quick_link_posts(quick_post_key)
   ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_quick_link_queue_due
 ON telegram_quick_link_queue(status,next_attempt_at,quick_post_key);

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

CREATE TABLE IF NOT EXISTS scheduled_preview_posts (
 job_id INTEGER NOT NULL,
 part_no INTEGER NOT NULL CHECK(part_no>=1),
 part_count INTEGER NOT NULL CHECK(part_count>=1),
 section_key TEXT NOT NULL,
 channel_id TEXT NOT NULL,
 message_id INTEGER NOT NULL CHECK(message_id>0),
 post_url TEXT NOT NULL DEFAULT '',
 content_hash TEXT NOT NULL DEFAULT '',
 html_text TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active',
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(job_id,part_no),
 UNIQUE(channel_id,message_id),
 FOREIGN KEY(job_id) REFERENCES publication_jobs(job_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_scheduled_previews_status
 ON scheduled_preview_posts(status,job_id,part_no);

CREATE TABLE IF NOT EXISTS price_runtime_state (
 state_key TEXT PRIMARY KEY,
 state_value TEXT NOT NULL,
 updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS calendar_publication_plan (
 day_of_month INTEGER NOT NULL CHECK(day_of_month BETWEEN 1 AND 30),
 slot INTEGER NOT NULL CHECK(slot>=1),
 subposition INTEGER NOT NULL CHECK(subposition>=1),
 requested_label TEXT NOT NULL,
 section_key TEXT NOT NULL,
 publish_time TEXT NOT NULL DEFAULT '09:30',
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 updated_at TEXT NOT NULL,
 PRIMARY KEY(day_of_month,slot,subposition));
CREATE INDEX IF NOT EXISTS idx_calendar_plan_day
 ON calendar_publication_plan(day_of_month,slot,subposition);

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
            now = _iso(_now())
            db.execute("DELETE FROM calendar_publication_plan")
            db.executemany(
                """INSERT INTO calendar_publication_plan(
                     day_of_month,slot,subposition,requested_label,section_key,
                     publish_time,enabled,updated_at)
                     VALUES(?,?,?,?,?,'09:30',1,?)""",
                [
                    (
                        entry.day,
                        entry.slot,
                        entry.subposition,
                        entry.requested_label,
                        entry.section_key,
                        now,
                    )
                    for entry in CALENDAR_PLAN_ENTRIES
                ],
            )
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

    def _retire_shared_post_aliases_tx(
        self,
        db: sqlite3.Connection,
        section_key: str,
        channel_id: str,
        message_ids: Sequence[int],
        at: datetime,
        *,
        enqueue_sheet_sync: bool,
    ) -> int:
        """Retire other current rows that point at the same Telegram post."""
        ids = sorted({int(message_id) for message_id in message_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            f"""SELECT * FROM telegram_posts
                WHERE channel_id=? AND section_key!=? AND is_current=1
                  AND message_id IN ({placeholders})""",
            (str(channel_id), _key(section_key), *ids),
        ).fetchall()
        for row in rows:
            db.execute(
                """UPDATE telegram_posts SET is_current=0,
                   status='superseded',updated_at=? WHERE record_key=?""",
                (_iso(at), row["record_key"]),
            )
            if enqueue_sheet_sync:
                payload = dict(row)
                payload["is_current"] = 0
                payload["status"] = "superseded"
                payload["updated_at"] = _iso(at)
                self._queue_outbox_tx(
                    db,
                    "telegram_post",
                    row["record_key"],
                    "upsert",
                    payload,
                    f"telegram_post:{row['record_key']}:shared_archived:{_iso(at)}",
                    at,
                    at,
                    12,
                )
        return len(rows)

    def retire_shared_telegram_post_aliases(
        self,
        section_key: str,
        channel_id: str,
        message_ids: Sequence[int],
        *,
        enqueue_sheet_sync: bool = True,
        now: datetime | str | None = None,
    ) -> int:
        """Invalidate sibling section mappings after a shared post changes."""
        key = _key(section_key)
        channel = str(channel_id).strip()
        if not channel:
            raise ValueError("channel_id is required")
        at = _dt(now, "now", default=True)
        with self._tx(True) as db:
            retired = self._retire_shared_post_aliases_tx(
                db,
                key,
                channel,
                message_ids,
                at,
                enqueue_sheet_sync=enqueue_sheet_sync,
            )
            if retired:
                self._audit(
                    db,
                    "telegram_shared_post_aliases_retired",
                    "telegram_section",
                    f"{channel}:{key}",
                    {"message_ids": sorted({int(item) for item in message_ids}),
                     "retired": retired},
                    at,
                )
            return retired

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
            new_keys = {item["record_key"] for item in results}
            new_messages = {
                (str(item["channel_id"]), int(item["message_id"]))
                for item in results
            }
            for old in old_rows:
                if old["record_key"] in new_keys:
                    continue
                db.execute(
                    """UPDATE telegram_posts SET status='superseded',
                       updated_at=? WHERE record_key=?""",
                    (_iso(at), old["record_key"]),
                )
                if (
                    str(old["channel_id"]), int(old["message_id"])
                ) not in new_messages:
                    queued = db.execute(
                        """SELECT 1 FROM telegram_deletion_queue
                           WHERE channel_id=? AND message_id=? LIMIT 1""",
                        (old["channel_id"], old["message_id"]),
                    ).fetchone()
                    if queued is None:
                        db.execute(
                            """INSERT INTO telegram_deletion_queue(
                                 record_key,section_key,channel_id,message_id,
                                 status,attempts,max_attempts,created_at,updated_at)
                                 VALUES(?,?,?,?,'pending',0,12,?,?)
                                 ON CONFLICT(record_key) DO NOTHING""",
                            (
                                old["record_key"], old["section_key"],
                                old["channel_id"], old["message_id"],
                                _iso(at), _iso(at),
                            ),
                        )
                if enqueue_sheet_sync:
                    old_payload = dict(old)
                    old_payload["is_current"] = 0
                    old_payload["status"] = "superseded"
                    old_payload["updated_at"] = _iso(at)
                    self._queue_outbox_tx(
                        db, "telegram_post", old["record_key"], "upsert", old_payload,
                        f"telegram_post:{old['record_key']}:archived:{_iso(at)}",
                        at, at, 12,
                    )
            self._retire_shared_post_aliases_tx(
                db,
                key,
                channel,
                [int(old["message_id"]) for old in old_rows],
                at,
                enqueue_sheet_sync=enqueue_sheet_sync,
            )
            quick_updates = self._enqueue_quick_link_updates_tx(
                db, key, channel, at
            )
            self._audit(db, "telegram_post_set_replaced", "telegram_section",
                        f"{channel}:{key}",
                        {"parts": len(results), "quick_updates": quick_updates},
                        at)
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

    def has_current_telegram_post(
        self,
        section_key: str,
        channel_id: str,
    ) -> bool:
        with self._read() as db:
            row = db.execute(
                """SELECT 1 FROM telegram_posts
                   WHERE section_key=? AND channel_id=? AND is_current=1
                   LIMIT 1""",
                (_key(section_key), str(channel_id)),
            ).fetchone()
        return row is not None

    def claim_telegram_deletions(
        self,
        now_utc: datetime | str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Claim durable deletions for superseded Telegram posts."""
        now = _dt(now_utc, "now_utc")
        bounded = min(100, max(1, int(limit)))
        with self._tx(True) as db:
            db.execute(
                """UPDATE telegram_deletion_queue SET status='pending',
                   updated_at=? WHERE status='running'""",
                (_iso(now),),
            )
            rows = db.execute(
                """SELECT * FROM telegram_deletion_queue
                   WHERE status='pending'
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                     AND NOT EXISTS(
                       SELECT 1 FROM telegram_quick_link_applied_targets AS a
                       JOIN telegram_quick_link_posts AS p
                         ON p.quick_post_key=a.quick_post_key
                       WHERE p.status!='disabled'
                         AND (
                           (
                             a.target_channel_id=telegram_deletion_queue.channel_id
                             AND a.target_message_id=telegram_deletion_queue.message_id
                           )
                           OR (
                             a.target_publication_id!=''
                             AND EXISTS(
                               SELECT 1 FROM telegram_posts AS doomed
                               WHERE doomed.channel_id=telegram_deletion_queue.channel_id
                                 AND doomed.message_id=telegram_deletion_queue.message_id
                                 AND doomed.publication_id=a.target_publication_id
                             )
                           )
                         )
                     )
                     AND NOT EXISTS(
                       SELECT 1 FROM telegram_quick_link_queue AS q
                       JOIN telegram_quick_link_posts AS p
                         ON p.quick_post_key=q.quick_post_key
                       JOIN telegram_quick_link_targets AS t
                         ON t.quick_post_key=q.quick_post_key
                       WHERE p.status!='disabled'
                         AND q.applied_revision<q.desired_revision
                         AND p.channel_id=telegram_deletion_queue.channel_id
                         AND t.section_key=telegram_deletion_queue.section_key
                     )
                   ORDER BY deletion_id LIMIT ?""",
                (_iso(now), bounded),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                cursor = db.execute(
                    """UPDATE telegram_deletion_queue SET status='running',
                       attempts=attempts+1,updated_at=?
                       WHERE deletion_id=? AND status='pending'""",
                    (_iso(now), row["deletion_id"]),
                )
                if cursor.rowcount == 1:
                    updated = db.execute(
                        "SELECT * FROM telegram_deletion_queue WHERE deletion_id=?",
                        (row["deletion_id"],),
                    ).fetchone()
                    if updated is not None:
                        claimed.append(dict(updated))
            return claimed

    def complete_telegram_deletion(
        self,
        deletion_id: int,
        now_utc: datetime | str,
    ) -> bool:
        at = _dt(now_utc, "now_utc")
        with self._tx(True) as db:
            row = db.execute(
                """SELECT * FROM telegram_deletion_queue
                   WHERE deletion_id=? AND status='running'""",
                (int(deletion_id),),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                """UPDATE telegram_deletion_queue SET status='done',
                   last_error=NULL,next_attempt_at=NULL,updated_at=?,completed_at=?
                   WHERE deletion_id=? AND status='running'""",
                (_iso(at), _iso(at), int(deletion_id)),
            )
            db.execute(
                """UPDATE telegram_posts SET status='deleted',is_current=0,
                   last_error=NULL,updated_at=?
                   WHERE channel_id=? AND message_id=?""",
                (_iso(at), row["channel_id"], row["message_id"]),
            )
            posts = db.execute(
                """SELECT * FROM telegram_posts
                   WHERE channel_id=? AND message_id=?""",
                (row["channel_id"], row["message_id"]),
            ).fetchall()
            for post in posts:
                payload = dict(post)
                digest = hashlib.sha256(_dump(payload).encode()).hexdigest()
                self._queue_outbox_tx(
                    db, "telegram_post", post["record_key"], "upsert", payload,
                    f"telegram_post:{post['record_key']}:deleted:{digest}",
                    at, at, 12,
                )
            self._audit(
                db, "telegram_post_deleted", "telegram_post",
                f"{row['channel_id']}:{row['message_id']}",
                {"message_id": row["message_id"], "aliases": len(posts)}, at,
            )
            return True

    def retry_telegram_deletion(
        self,
        deletion_id: int,
        now_utc: datetime | str,
        error: Any,
        *,
        permanent: bool = False,
    ) -> bool:
        at = _dt(now_utc, "now_utc")
        safe_error = str(error or "Telegram deletion failed")[:1000]
        with self._tx(True) as db:
            row = db.execute(
                """SELECT * FROM telegram_deletion_queue
                   WHERE deletion_id=? AND status='running'""",
                (int(deletion_id),),
            ).fetchone()
            if row is None:
                return False
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if permanent or exhausted else "pending"
            next_attempt = None if exhausted else _iso(
                at + timedelta(seconds=min(3600, 2 ** min(int(row["attempts"]), 11)))
            )
            db.execute(
                """UPDATE telegram_deletion_queue SET status=?,last_error=?,
                   next_attempt_at=?,updated_at=?,completed_at=?
                   WHERE deletion_id=? AND status='running'""",
                (
                    status, safe_error, next_attempt, _iso(at),
                    _iso(at) if exhausted else None, int(deletion_id),
                ),
            )
            db.execute(
                """UPDATE telegram_posts SET status=CASE WHEN ?='failed'
                   THEN 'delete_failed' ELSE status END,last_error=?,updated_at=?
                   WHERE channel_id=? AND message_id=?""",
                (
                    status, safe_error, _iso(at),
                    row["channel_id"], row["message_id"],
                ),
            )
            if status == "failed":
                posts = db.execute(
                    """SELECT * FROM telegram_posts
                       WHERE channel_id=? AND message_id=?""",
                    (row["channel_id"], row["message_id"]),
                ).fetchall()
                for post in posts:
                    payload = dict(post)
                    digest = hashlib.sha256(_dump(payload).encode()).hexdigest()
                    self._queue_outbox_tx(
                        db, "telegram_post", post["record_key"], "upsert",
                        payload,
                        f"telegram_post:{post['record_key']}:delete_failed:{digest}",
                        at, at, 12,
                    )
            return True

    def list_telegram_deletions(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                """SELECT * FROM telegram_deletion_queue
                   ORDER BY deletion_id DESC LIMIT ?""",
                (min(5000, max(1, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_unreported_manual_deletions(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return permanent Telegram failures not yet sent for manual cleanup."""
        with self._read() as db:
            rows = db.execute(
                """SELECT q.*,
                          COALESCE(MAX(p.section_name),q.section_key) AS section_name,
                          COALESCE(MAX(p.post_url),'') AS post_url
                   FROM telegram_deletion_queue AS q
                   LEFT JOIN manual_deletion_requests AS r
                     ON r.deletion_id=q.deletion_id
                   LEFT JOIN telegram_posts AS p
                     ON p.channel_id=q.channel_id
                    AND p.message_id=q.message_id
                   WHERE q.status='failed' AND r.deletion_id IS NULL
                     AND NOT EXISTS(
                       SELECT 1 FROM telegram_quick_link_applied_targets AS a
                       JOIN telegram_quick_link_posts AS quick_post
                         ON quick_post.quick_post_key=a.quick_post_key
                       WHERE quick_post.status!='disabled'
                         AND (
                           (a.target_channel_id=q.channel_id
                            AND a.target_message_id=q.message_id)
                           OR (
                             a.target_publication_id!=''
                             AND EXISTS(
                               SELECT 1 FROM telegram_posts AS doomed
                               WHERE doomed.channel_id=q.channel_id
                                 AND doomed.message_id=q.message_id
                                 AND doomed.publication_id=a.target_publication_id
                             )
                           )
                         )
                     )
                     AND NOT EXISTS(
                       SELECT 1 FROM telegram_quick_link_queue AS quick_queue
                       JOIN telegram_quick_link_posts AS quick_post
                         ON quick_post.quick_post_key=quick_queue.quick_post_key
                       JOIN telegram_quick_link_targets AS target
                         ON target.quick_post_key=quick_queue.quick_post_key
                       WHERE quick_post.status!='disabled'
                         AND quick_queue.applied_revision
                             <quick_queue.desired_revision
                         AND quick_post.channel_id=q.channel_id
                         AND target.section_key=q.section_key
                     )
                   GROUP BY q.deletion_id
                   ORDER BY q.deletion_id
                   LIMIT ?""",
                (min(500, max(1, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_manual_deletion_request(
        self,
        deletion_id: int,
        *,
        request_channel_id: str,
        request_message_id: int,
        target_post_url: str = "",
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        at = _dt(now, "now", default=True)
        with self._tx(True) as db:
            deletion = db.execute(
                """SELECT * FROM telegram_deletion_queue
                   WHERE deletion_id=? AND status='failed'""",
                (int(deletion_id),),
            ).fetchone()
            if deletion is None:
                raise ValueError("failed Telegram deletion does not exist")
            db.execute(
                """INSERT INTO manual_deletion_requests(
                     deletion_id,section_key,target_channel_id,target_message_id,
                     target_post_url,request_channel_id,request_message_id,status,
                     created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'active',?,?)
                   ON CONFLICT(deletion_id) DO NOTHING""",
                (
                    int(deletion_id), deletion["section_key"],
                    deletion["channel_id"], deletion["message_id"],
                    str(target_post_url or ""), str(request_channel_id),
                    int(request_message_id), _iso(at), _iso(at),
                ),
            )
            row = db.execute(
                "SELECT * FROM manual_deletion_requests WHERE deletion_id=?",
                (int(deletion_id),),
            ).fetchone()
            assert row is not None
            self._audit(
                db, "manual_deletion_requested", "telegram_deletion",
                deletion_id,
                {
                    "target_message_id": deletion["message_id"],
                    "request_message_id": int(request_message_id),
                },
                at,
            )
            return dict(row)

    def get_manual_deletion_request(
        self,
        deletion_id: int,
    ) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute(
                """SELECT r.*,
                          (EXISTS(
                            SELECT 1
                            FROM telegram_quick_link_applied_targets AS a
                            JOIN telegram_quick_link_posts AS p
                              ON p.quick_post_key=a.quick_post_key
                            WHERE p.status!='disabled'
                              AND (
                                (a.target_channel_id=r.target_channel_id
                                 AND a.target_message_id=r.target_message_id)
                                OR (
                                  a.target_publication_id!=''
                                  AND EXISTS(
                                    SELECT 1 FROM telegram_posts AS doomed
                                    WHERE doomed.channel_id=r.target_channel_id
                                      AND doomed.message_id=r.target_message_id
                                      AND doomed.publication_id=a.target_publication_id
                                  )
                                )
                              )
                          ) OR EXISTS(
                            SELECT 1
                            FROM telegram_quick_link_queue AS q
                            JOIN telegram_quick_link_posts AS p
                              ON p.quick_post_key=q.quick_post_key
                            JOIN telegram_quick_link_targets AS t
                              ON t.quick_post_key=q.quick_post_key
                            WHERE p.status!='disabled'
                              AND q.applied_revision<q.desired_revision
                              AND p.channel_id=r.target_channel_id
                              AND t.section_key=r.section_key
                          )) AS quick_link_blocked
                   FROM manual_deletion_requests AS r
                   WHERE r.deletion_id=?""",
                (int(deletion_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_manual_deletion(
        self,
        deletion_id: int,
        now_utc: datetime | str,
    ) -> bool:
        """Record an administrator-confirmed deletion and sync post aliases."""
        at = _dt(now_utc, "now_utc")
        with self._tx(True) as db:
            request = db.execute(
                """SELECT * FROM manual_deletion_requests
                   WHERE deletion_id=? AND status='active'""",
                (int(deletion_id),),
            ).fetchone()
            if request is None:
                return False
            blocked = db.execute(
                """SELECT 1 WHERE EXISTS(
                     SELECT 1
                     FROM telegram_quick_link_applied_targets AS a
                     JOIN telegram_quick_link_posts AS p
                       ON p.quick_post_key=a.quick_post_key
                     WHERE p.status!='disabled'
                       AND (
                         (a.target_channel_id=? AND a.target_message_id=?)
                         OR (
                           a.target_publication_id!=''
                           AND EXISTS(
                             SELECT 1 FROM telegram_posts AS doomed
                             WHERE doomed.channel_id=? AND doomed.message_id=?
                               AND doomed.publication_id=a.target_publication_id
                           )
                         )
                       )
                   ) OR EXISTS(
                     SELECT 1 FROM telegram_quick_link_queue AS q
                     JOIN telegram_quick_link_posts AS p
                       ON p.quick_post_key=q.quick_post_key
                     JOIN telegram_quick_link_targets AS t
                       ON t.quick_post_key=q.quick_post_key
                     WHERE p.status!='disabled'
                       AND q.applied_revision<q.desired_revision
                       AND p.channel_id=? AND t.section_key=?
                   )""",
                (
                    request["target_channel_id"],
                    request["target_message_id"],
                    request["target_channel_id"],
                    request["target_message_id"],
                    request["target_channel_id"],
                    request["section_key"],
                ),
            ).fetchone()
            if blocked is not None:
                return False
            db.execute(
                """UPDATE manual_deletion_requests SET status='completed',
                   last_error=NULL,updated_at=?,completed_at=?
                   WHERE deletion_id=? AND status='active'""",
                (_iso(at), _iso(at), int(deletion_id)),
            )
            db.execute(
                """UPDATE telegram_deletion_queue SET status='manual_done',
                   last_error=NULL,next_attempt_at=NULL,updated_at=?,completed_at=?
                   WHERE deletion_id=?""",
                (_iso(at), _iso(at), int(deletion_id)),
            )
            db.execute(
                """UPDATE telegram_posts SET status='deleted',is_current=0,
                   last_error=NULL,updated_at=?
                   WHERE channel_id=? AND message_id=?""",
                (
                    _iso(at), request["target_channel_id"],
                    request["target_message_id"],
                ),
            )
            posts = db.execute(
                """SELECT * FROM telegram_posts
                   WHERE channel_id=? AND message_id=?""",
                (request["target_channel_id"], request["target_message_id"]),
            ).fetchall()
            for post in posts:
                payload = dict(post)
                digest = hashlib.sha256(_dump(payload).encode()).hexdigest()
                self._queue_outbox_tx(
                    db, "telegram_post", post["record_key"], "upsert", payload,
                    f"telegram_post:{post['record_key']}:manual_deleted:{digest}",
                    at, at, 12,
                )
            self._audit(
                db, "manual_deletion_completed", "telegram_deletion",
                deletion_id,
                {
                    "target_message_id": request["target_message_id"],
                    "aliases": len(posts),
                },
                at,
            )
            return True

    def list_completed_manual_deletion_requests(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return completed requests whose helper message still needs removal."""
        with self._read() as db:
            rows = db.execute(
                """SELECT * FROM manual_deletion_requests
                   WHERE status='completed'
                   ORDER BY deletion_id
                   LIMIT ?""",
                (min(500, max(1, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_manual_deletion_request_removed(
        self,
        deletion_id: int,
        *,
        request_channel_id: str,
        request_message_id: int,
        now: datetime | str | None = None,
    ) -> bool:
        """Mark the helper message removed after Telegram confirms deletion."""
        at = _dt(now, "now", default=True)
        with self._tx(True) as db:
            cursor = db.execute(
                """UPDATE manual_deletion_requests
                   SET status='removed',last_error=NULL,updated_at=?
                   WHERE deletion_id=? AND status='completed'
                     AND request_channel_id=? AND request_message_id=?""",
                (
                    _iso(at), int(deletion_id), str(request_channel_id),
                    int(request_message_id),
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._audit(
                db, "manual_deletion_request_removed", "telegram_deletion",
                deletion_id,
                {"request_message_id": int(request_message_id)},
                at,
            )
            return True

    @staticmethod
    def _quick_link_marker(link_key: str) -> str:
        return "{{post_url:" + _key(link_key) + "}}"

    @staticmethod
    def _telegram_message_id_from_url(url: str) -> int:
        match = _TELEGRAM_POST_URL_RE.fullmatch(str(url or "").strip())
        if match is None:
            raise ValueError("quick-link target must be a public Telegram post URL")
        return int(match.group("message_id"))

    @staticmethod
    def _queue_quick_link_post_tx(
        db: sqlite3.Connection,
        quick_post_key: str,
        at: datetime,
    ) -> None:
        key = _key(quick_post_key)
        row = db.execute(
            "SELECT * FROM telegram_quick_link_queue WHERE quick_post_key=?",
            (key,),
        ).fetchone()
        if row is None:
            db.execute(
                """INSERT INTO telegram_quick_link_queue(
                     quick_post_key,desired_revision,claimed_revision,
                     applied_revision,status,attempts,max_attempts,
                     next_attempt_at,created_at,updated_at)
                   VALUES(?,1,0,0,'pending',0,12,?,?,?)""",
                (key, _iso(at), _iso(at), _iso(at)),
            )
            return
        running = str(row["status"]) == "running"
        db.execute(
            """UPDATE telegram_quick_link_queue
               SET desired_revision=desired_revision+1,
                   status=?,attempts=?,next_attempt_at=?,last_error=NULL,
                   completed_at=NULL,updated_at=?
               WHERE quick_post_key=?""",
            (
                "running" if running else "pending",
                int(row["attempts"]) if running else 0,
                row["next_attempt_at"] if running else _iso(at),
                _iso(at), key,
            ),
        )

    def _enqueue_quick_link_updates_tx(
        self,
        db: sqlite3.Connection,
        section_key: str,
        channel_id: str,
        at: datetime,
    ) -> int:
        rows = db.execute(
            """SELECT DISTINCT p.quick_post_key
               FROM telegram_quick_link_posts AS p
               JOIN telegram_quick_link_targets AS t
                 ON t.quick_post_key=p.quick_post_key
               WHERE t.section_key=? AND p.channel_id=?
                 AND p.status!='disabled'
               ORDER BY p.quick_post_key""",
            (_key(section_key), str(channel_id)),
        ).fetchall()
        for row in rows:
            self._queue_quick_link_post_tx(
                db, row["quick_post_key"], at
            )
        return len(rows)

    def ensure_quick_link_posts(
        self,
        specs: Sequence[Mapping[str, Any]],
        *,
        channel_id: str,
        channel_username: str,
        enqueue_initial: bool = True,
        now: datetime | str | None = None,
    ) -> int:
        """Idempotently install approved quick-link templates and bindings."""
        if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)):
            raise ValueError("quick-link specs must be a list")
        channel = str(channel_id).strip()
        username = str(channel_username).strip().lstrip("@")
        if not channel or not username:
            raise ValueError("quick-link channel and username are required")
        at = _dt(now, "now", default=True)
        normalized: list[dict[str, Any]] = []
        seen_posts: set[str] = set()
        for raw in specs:
            spec = dict(raw)
            quick_post_key = _key(spec.get("quick_post_key"))
            if quick_post_key in seen_posts:
                raise ValueError("duplicate quick_post_key")
            seen_posts.add(quick_post_key)
            title = str(spec.get("title") or quick_post_key).strip()
            template_html = str(spec.get("template_html") or "")
            reconcile_on_install = bool(spec.get("reconcile_on_install", False))
            try:
                message_id = int(spec.get("message_id"))
            except (TypeError, ValueError) as exc:
                raise ValueError("quick-link message_id must be positive") from exc
            if message_id <= 0 or not title or not template_html:
                raise ValueError("quick-link title, template and message ID are required")
            raw_targets = spec.get("targets", [])
            if not isinstance(raw_targets, Sequence) or isinstance(
                raw_targets, (str, bytes)
            ):
                raise ValueError("quick-link targets must be a list")
            targets: list[dict[str, Any]] = []
            seen_target_sections: set[tuple[str, str]] = set()
            seen_link_keys: set[str] = set()
            for raw_target in raw_targets:
                target = dict(raw_target)
                link_key = _key(target.get("link_key"))
                fallback_url = str(target.get("fallback_url") or "").strip()
                self._telegram_message_id_from_url(fallback_url)
                initial_url = str(
                    target.get("initial_url") or fallback_url
                ).strip()
                self._telegram_message_id_from_url(initial_url)
                section_keys = target.get("section_keys")
                if section_keys is None:
                    section_keys = [target.get("section_key")]
                if not isinstance(section_keys, Sequence) or isinstance(
                    section_keys, (str, bytes)
                ) or not section_keys:
                    raise ValueError("quick-link target needs section_keys")
                marker = self._quick_link_marker(link_key)
                if marker not in template_html:
                    raise ValueError(f"quick-link template misses {marker}")
                seen_link_keys.add(link_key)
                for priority, section_key in enumerate(section_keys, start=1):
                    section = _key(section_key)
                    unique = (link_key, section)
                    if unique in seen_target_sections:
                        raise ValueError("duplicate quick-link target section")
                    seen_target_sections.add(unique)
                    targets.append({
                        "link_key": link_key,
                        "section_key": section,
                        "priority": priority,
                        "fallback_url": fallback_url,
                        "initial_url": initial_url,
                    })
            markers = set(re.findall(
                r"\{\{post_url:([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\}\}",
                template_html,
            ))
            if markers != seen_link_keys:
                raise ValueError("quick-link template has undeclared placeholders")
            definition = {
                "quick_post_key": quick_post_key,
                "title": title,
                "message_id": message_id,
                "template_html": template_html,
                "reconcile_on_install": reconcile_on_install,
                "targets": targets,
            }
            definition["template_hash"] = hashlib.sha256(
                _dump(definition).encode("utf-8")
            ).hexdigest()
            normalized.append(definition)

        changed_count = 0
        with self._tx(True) as db:
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                db.execute(
                    f"""UPDATE telegram_quick_link_posts SET status='disabled',
                           updated_at=?
                         WHERE channel_id=? AND quick_post_key NOT IN (
                           {placeholders}
                         ) AND status!='disabled'""",
                    (
                        _iso(at), channel,
                        *(spec["quick_post_key"] for spec in normalized),
                    ),
                )
            for spec in normalized:
                key = spec["quick_post_key"]
                existing = db.execute(
                    "SELECT * FROM telegram_quick_link_posts WHERE quick_post_key=?",
                    (key,),
                ).fetchone()
                installing = existing is None
                changed = existing is None or any((
                    str(existing["title"]) != spec["title"],
                    str(existing["channel_id"]) != channel,
                    str(existing["channel_username"]) != username,
                    int(existing["message_id"]) != spec["message_id"],
                    str(existing["template_hash"]) != spec["template_hash"],
                    str(existing["status"]) == "disabled",
                ))
                post_url = f"https://t.me/{username}/{spec['message_id']}"
                if not changed:
                    continue
                db.execute(
                    """INSERT INTO telegram_quick_link_posts(
                         quick_post_key,title,channel_id,channel_username,
                         message_id,post_url,template_html,template_hash,status,
                         created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,'active',?,?)
                       ON CONFLICT(quick_post_key) DO UPDATE SET
                        title=excluded.title,channel_id=excluded.channel_id,
                        channel_username=excluded.channel_username,
                        message_id=excluded.message_id,post_url=excluded.post_url,
                        template_html=excluded.template_html,
                        template_hash=excluded.template_hash,
                        status=CASE WHEN telegram_quick_link_posts.status='disabled'
                              THEN 'active' ELSE telegram_quick_link_posts.status END,
                        updated_at=excluded.updated_at""",
                    (
                        key, spec["title"], channel, username,
                        spec["message_id"], post_url, spec["template_html"],
                        spec["template_hash"], _iso(at), _iso(at),
                    ),
                )
                db.execute(
                    "DELETE FROM telegram_quick_link_targets WHERE quick_post_key=?",
                    (key,),
                )
                db.executemany(
                    """INSERT INTO telegram_quick_link_targets(
                         quick_post_key,link_key,section_key,priority,fallback_url)
                       VALUES(?,?,?,?,?)""",
                    [
                        (
                            key, target["link_key"], target["section_key"],
                            target["priority"], target["fallback_url"],
                        )
                        for target in spec["targets"]
                    ],
                )
                db.execute(
                    """DELETE FROM telegram_quick_link_applied_targets
                       WHERE quick_post_key=? AND link_key NOT IN (
                         SELECT link_key FROM telegram_quick_link_targets
                         WHERE quick_post_key=?)""",
                    (key, key),
                )
                for target in spec["targets"]:
                    message_id = self._telegram_message_id_from_url(
                        target["initial_url"]
                    )
                    initial_post = db.execute(
                        """SELECT publication_id FROM telegram_posts
                           WHERE channel_id=? AND message_id=?
                           ORDER BY is_current DESC,updated_at DESC,post_id DESC
                           LIMIT 1""",
                        (channel, message_id),
                    ).fetchone()
                    initial_publication_id = (
                        str(initial_post["publication_id"] or "")
                        if initial_post is not None else ""
                    )
                    db.execute(
                        """INSERT INTO telegram_quick_link_applied_targets(
                             quick_post_key,link_key,target_channel_id,
                             target_message_id,target_publication_id,target_url,
                             updated_at)
                           VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(quick_post_key,link_key) DO NOTHING""",
                        (
                            key, target["link_key"], channel, message_id,
                            initial_publication_id, target["initial_url"],
                            _iso(at),
                        ),
                    )
                if changed:
                    changed_count += 1
                    should_queue = enqueue_initial and (
                        not installing or spec["reconcile_on_install"]
                    )
                    if should_queue:
                        self._queue_quick_link_post_tx(db, key, at)
                    self._audit(
                        db, "quick_link_post_configured", "quick_link_post",
                        key,
                        {
                            "message_id": spec["message_id"],
                            "targets": len(spec["targets"]),
                            "queued": should_queue,
                        },
                        at,
                    )
        return changed_count

    def list_quick_link_posts(self) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                """SELECT * FROM telegram_quick_link_posts
                   ORDER BY quick_post_key"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_quick_link_post(self, quick_post_key: str) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute(
                "SELECT * FROM telegram_quick_link_posts WHERE quick_post_key=?",
                (_key(quick_post_key),),
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_quick_link_post(
        self,
        quick_post_key: str,
    ) -> dict[str, Any]:
        """Resolve placeholders from the latest durable part-one post URLs."""
        key = _key(quick_post_key)
        with self._read() as db:
            post = db.execute(
                "SELECT * FROM telegram_quick_link_posts WHERE quick_post_key=?",
                (key,),
            ).fetchone()
            if post is None:
                raise ValueError("quick-link post does not exist")
            targets = db.execute(
                """SELECT * FROM telegram_quick_link_targets
                   WHERE quick_post_key=? ORDER BY link_key,priority,section_key""",
                (key,),
            ).fetchall()
            current = db.execute(
                """SELECT p.section_key,p.channel_id,p.message_id,p.post_url,
                          p.publication_id
                   FROM telegram_posts AS p
                   JOIN telegram_quick_link_targets AS t
                     ON t.section_key=p.section_key
                    AND t.quick_post_key=?
                   WHERE p.channel_id=? AND p.is_current=1 AND p.part_no=1
                   ORDER BY p.section_key,p.updated_at DESC,p.post_id DESC""",
                (key, post["channel_id"]),
            ).fetchall()
        current_by_section: dict[str, sqlite3.Row] = {}
        for row in current:
            current_by_section.setdefault(row["section_key"], row)
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in targets:
            grouped.setdefault(row["link_key"], []).append(row)
        resolved: list[dict[str, Any]] = []
        for link_key, candidates in grouped.items():
            selected_post = None
            selected_target = candidates[0]
            for candidate in candidates:
                found = current_by_section.get(candidate["section_key"])
                if found is not None:
                    selected_post = found
                    selected_target = candidate
                    break
            if selected_post is None:
                target_url = str(selected_target["fallback_url"])
                target_channel_id = str(post["channel_id"])
                target_message_id = self._telegram_message_id_from_url(target_url)
                target_publication_id = ""
            else:
                target_url = str(selected_post["post_url"] or "").strip()
                if not target_url:
                    target_url = (
                        f"https://t.me/{post['channel_username']}/"
                        f"{selected_post['message_id']}"
                    )
                target_channel_id = str(selected_post["channel_id"])
                target_message_id = int(selected_post["message_id"])
                target_publication_id = str(
                    selected_post["publication_id"] or ""
                )
            if self._telegram_message_id_from_url(target_url) != target_message_id:
                raise ValueError("quick-link target URL does not match message ID")
            resolved.append({
                "link_key": link_key,
                "section_key": selected_target["section_key"],
                "target_channel_id": target_channel_id,
                "target_message_id": target_message_id,
                "target_publication_id": target_publication_id,
                "target_url": target_url,
            })
        payload = dict(post)
        payload["resolved_targets"] = resolved
        return payload

    def claim_quick_link_updates(
        self,
        now_utc: datetime | str,
        *,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        now = _dt(now_utc, "now_utc")
        expires = now + timedelta(
            seconds=min(3600, max(10, int(lease_seconds)))
        )
        with self._tx(True) as db:
            db.execute(
                """UPDATE telegram_quick_link_queue SET status='pending',
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE status='running'
                     AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
                (_iso(now), _iso(now)),
            )
            rows = db.execute(
                """SELECT q.* FROM telegram_quick_link_queue AS q
                   JOIN telegram_quick_link_posts AS p
                     ON p.quick_post_key=q.quick_post_key
                   WHERE q.status='pending' AND p.status!='disabled'
                     AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=?)
                   ORDER BY q.updated_at,q.quick_post_key LIMIT ?""",
                (_iso(now), min(100, max(1, int(limit)))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                token = secrets.token_urlsafe(24)
                cursor = db.execute(
                    """UPDATE telegram_quick_link_queue SET status='running',
                       attempts=attempts+1,claimed_revision=desired_revision,
                       lease_token=?,lease_expires_at=?,updated_at=?
                       WHERE quick_post_key=? AND status='pending'""",
                    (token, _iso(expires), _iso(now), row["quick_post_key"]),
                )
                if cursor.rowcount == 1:
                    updated = db.execute(
                        """SELECT * FROM telegram_quick_link_queue
                           WHERE quick_post_key=?""",
                        (row["quick_post_key"],),
                    ).fetchone()
                    if updated is not None:
                        claimed.append(dict(updated))
            return claimed

    def complete_quick_link_update(
        self,
        quick_post_key: str,
        lease_token: str,
        now_utc: datetime | str,
        *,
        rendered_html: str,
        render_hash: str,
        resolved_targets: Sequence[Mapping[str, Any]],
    ) -> bool:
        now = _dt(now_utc, "now_utc")
        key = _key(quick_post_key)
        if _HASH_RE.fullmatch(str(render_hash)) is None:
            raise ValueError("quick-link render_hash is invalid")
        with self._tx(True) as db:
            queue = db.execute(
                """SELECT * FROM telegram_quick_link_queue
                   WHERE quick_post_key=? AND status='running'
                     AND lease_token=?""",
                (key, str(lease_token)),
            ).fetchone()
            if queue is None:
                return False
            expected = {
                row["link_key"]
                for row in db.execute(
                    """SELECT DISTINCT link_key
                       FROM telegram_quick_link_targets
                       WHERE quick_post_key=?""",
                    (key,),
                ).fetchall()
            }
            supplied = {str(item.get("link_key") or "") for item in resolved_targets}
            if supplied != expected:
                raise ValueError("quick-link resolved targets are incomplete")
            for item in resolved_targets:
                link_key = _key(item.get("link_key"))
                target_url = str(item.get("target_url") or "").strip()
                parsed_id = self._telegram_message_id_from_url(target_url)
                target_message_id = int(item.get("target_message_id") or 0)
                if target_message_id != parsed_id:
                    raise ValueError("quick-link target message ID mismatch")
                db.execute(
                    """INSERT INTO telegram_quick_link_applied_targets(
                         quick_post_key,link_key,target_channel_id,
                         target_message_id,target_publication_id,target_url,
                         updated_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(quick_post_key,link_key) DO UPDATE SET
                        target_channel_id=excluded.target_channel_id,
                        target_message_id=excluded.target_message_id,
                        target_publication_id=excluded.target_publication_id,
                        target_url=excluded.target_url,
                        updated_at=excluded.updated_at""",
                    (
                        key, link_key, str(item.get("target_channel_id") or ""),
                        target_message_id,
                        str(item.get("target_publication_id") or ""),
                        target_url, _iso(now),
                    ),
                )
            db.execute(
                """UPDATE telegram_quick_link_posts SET status='active',
                   last_rendered_html=?,last_render_hash=?,last_error=NULL,
                   updated_at=?,last_edited_at=? WHERE quick_post_key=?""",
                (
                    str(rendered_html), str(render_hash), _iso(now),
                    _iso(now), key,
                ),
            )
            has_newer = int(queue["desired_revision"]) > int(
                queue["claimed_revision"]
            )
            db.execute(
                """UPDATE telegram_quick_link_queue SET status=?,
                   applied_revision=?,attempts=?,next_attempt_at=?,
                   lease_token=NULL,lease_expires_at=NULL,last_error=NULL,
                   updated_at=?,completed_at=? WHERE quick_post_key=?
                     AND status='running' AND lease_token=?""",
                (
                    "pending" if has_newer else "done",
                    int(queue["claimed_revision"]),
                    0 if has_newer else int(queue["attempts"]),
                    _iso(now) if has_newer else None,
                    _iso(now), None if has_newer else _iso(now), key,
                    str(lease_token),
                ),
            )
            self._audit(
                db, "quick_link_post_updated", "quick_link_post", key,
                {
                    "revision": int(queue["claimed_revision"]),
                    "targets": len(resolved_targets),
                    "newer_pending": has_newer,
                },
                now,
            )
            return True

    def retry_quick_link_update(
        self,
        quick_post_key: str,
        lease_token: str,
        now_utc: datetime | str,
        error: Any,
        *,
        retry_after_seconds: float | int | None = None,
        permanent: bool = False,
    ) -> bool:
        now = _dt(now_utc, "now_utc")
        key = _key(quick_post_key)
        safe_error = str(error or "quick-link update failed").replace(
            "\n", " "
        )[:1000]
        with self._tx(True) as db:
            row = db.execute(
                """SELECT * FROM telegram_quick_link_queue
                   WHERE quick_post_key=? AND status='running'
                     AND lease_token=?""",
                (key, str(lease_token)),
            ).fetchone()
            if row is None:
                return False
            has_newer = int(row["desired_revision"]) > int(
                row["claimed_revision"]
            )
            terminal = (
                not has_newer
                and (
                    permanent
                    or int(row["attempts"]) >= int(row["max_attempts"])
                )
            )
            if terminal:
                status, next_attempt = "failed", None
            else:
                delay = (
                    2 ** min(int(row["attempts"]), 11)
                    if retry_after_seconds is None
                    else float(retry_after_seconds)
                )
                status = "pending"
                next_attempt = _iso(now if has_newer else (
                    now + timedelta(seconds=min(86400, max(0, delay)))
                ))
            db.execute(
                """UPDATE telegram_quick_link_queue SET status=?,
                   attempts=?,next_attempt_at=?,lease_token=NULL,
                   lease_expires_at=NULL,
                   last_error=?,updated_at=?,completed_at=?
                   WHERE quick_post_key=? AND status='running'
                     AND lease_token=?""",
                (
                    status, 0 if has_newer else int(row["attempts"]),
                    next_attempt, safe_error, _iso(now),
                    _iso(now) if terminal else None, key, str(lease_token),
                ),
            )
            db.execute(
                """UPDATE telegram_quick_link_posts SET status='update_error',
                   last_error=?,updated_at=? WHERE quick_post_key=?""",
                (safe_error, _iso(now), key),
            )
            return True

    def list_quick_link_updates(self) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                """SELECT * FROM telegram_quick_link_queue
                   ORDER BY quick_post_key"""
            ).fetchall()
        return [dict(row) for row in rows]

    def build_quick_link_registry_records(self) -> list[dict[str, Any]]:
        with self._read() as db:
            posts = db.execute(
                """SELECT * FROM telegram_quick_link_posts
                   ORDER BY quick_post_key"""
            ).fetchall()
            targets = db.execute(
                """SELECT t.*,a.target_message_id,a.target_url
                   FROM telegram_quick_link_targets AS t
                   LEFT JOIN telegram_quick_link_applied_targets AS a
                     ON a.quick_post_key=t.quick_post_key
                    AND a.link_key=t.link_key
                   ORDER BY t.quick_post_key,t.link_key,t.priority"""
            ).fetchall()
            queues = {
                row["quick_post_key"]: row
                for row in db.execute(
                    "SELECT * FROM telegram_quick_link_queue"
                ).fetchall()
            }
        by_post: dict[str, list[sqlite3.Row]] = {}
        for target in targets:
            by_post.setdefault(target["quick_post_key"], []).append(target)
        records: list[dict[str, Any]] = []
        for post in posts:
            key = post["quick_post_key"]
            linked = by_post.get(key, [])
            queue = queues.get(key)
            records.append({
                "quick_post_key": key,
                "title": post["title"],
                "channel_id": post["channel_id"],
                "channel_username": post["channel_username"],
                "message_id": post["message_id"],
                "post_url": post["post_url"],
                "linked_section_keys": list(dict.fromkeys(
                    row["section_key"] for row in linked
                )),
                "target_message_ids": {
                    row["link_key"]: row["target_message_id"]
                    for row in linked if row["target_message_id"] is not None
                },
                "target_post_urls": {
                    row["link_key"]: row["target_url"]
                    for row in linked if row["target_url"]
                },
                "desired_revision": (
                    queue["desired_revision"] if queue is not None else 0
                ),
                "applied_revision": (
                    queue["applied_revision"] if queue is not None else 0
                ),
                "status": post["status"],
                "last_render_hash": post["last_render_hash"],
                "last_edited_at": post["last_edited_at"],
                "updated_at": post["updated_at"],
                "last_error": post["last_error"] or (
                    queue["last_error"] if queue is not None else None
                ),
            })
        return records

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

    def list_jobs_for_preview(
        self,
        now_utc: datetime | str,
        *,
        horizon_hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return pending delayed jobs entering the preview window."""
        now = _dt(now_utc, "now_utc")
        horizon = now + timedelta(hours=max(1, min(24, int(horizon_hours))))
        with self._read() as db:
            rows = db.execute(
                """SELECT j.* FROM publication_jobs AS j
                   WHERE j.status='pending' AND j.execute_at>?
                     AND j.execute_at<=?
                     AND NOT EXISTS (
                       SELECT 1 FROM scheduled_preview_posts AS p
                       WHERE p.job_id=j.job_id AND p.status='active')
                   ORDER BY j.execute_at,j.job_id LIMIT ?""",
                (_iso(now), _iso(horizon), min(500, max(1, int(limit)))),
            ).fetchall()
        return [item for row in rows if (item := self._job(row)) is not None]

    def record_scheduled_preview(
        self,
        job_id: int,
        section_key: str,
        channel_id: str,
        *,
        part_no: int,
        part_count: int,
        message_id: int,
        post_url: str = "",
        content_hash: str = "",
        html_text: str = "",
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        at = _dt(now, "now", default=True)
        values = (
            int(job_id), int(part_no), int(part_count), _key(section_key),
            str(channel_id), int(message_id), str(post_url)[:2048],
            str(content_hash)[:128], str(html_text), _iso(at), _iso(at),
        )
        if values[1] < 1 or values[2] < values[1] or values[5] < 1:
            raise ValueError("preview part and message IDs must be positive")
        with self._tx(True) as db:
            if db.execute(
                "SELECT 1 FROM publication_jobs WHERE job_id=?", (values[0],)
            ).fetchone() is None:
                raise ValueError("preview job does not exist")
            db.execute(
                """INSERT INTO scheduled_preview_posts(
                     job_id,part_no,part_count,section_key,channel_id,message_id,
                     post_url,content_hash,html_text,status,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)
                     ON CONFLICT(job_id,part_no) DO UPDATE SET
                      part_count=excluded.part_count,section_key=excluded.section_key,
                      channel_id=excluded.channel_id,message_id=excluded.message_id,
                      post_url=excluded.post_url,content_hash=excluded.content_hash,
                      html_text=excluded.html_text,status='active',last_error=NULL,
                      updated_at=excluded.updated_at""",
                values,
            )
            row = db.execute(
                "SELECT * FROM scheduled_preview_posts WHERE job_id=? AND part_no=?",
                (values[0], values[1]),
            ).fetchone()
            assert row is not None
            self._audit(
                db, "scheduled_preview_recorded", "publication_job", values[0],
                {"part_no": values[1], "message_id": values[5]}, at,
            )
            return dict(row)

    def list_scheduled_previews(
        self,
        *,
        job_id: int | None = None,
        status: str | Sequence[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if job_id is not None:
            where.append("job_id=?")
            params.append(int(job_id))
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            if statuses:
                where.append(
                    "status IN (" + ",".join("?" for _ in statuses) + ")"
                )
                params.extend(str(item) for item in statuses)
        clause = " WHERE " + " AND ".join(where) if where else ""
        params.append(min(5000, max(1, int(limit))))
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM scheduled_preview_posts" + clause
                + " ORDER BY job_id,part_no LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_scheduled_preview_status(
        self,
        job_id: int,
        status: str,
        *,
        error: str = "",
        now: datetime | str | None = None,
    ) -> int:
        at = _dt(now, "now", default=True)
        normalized = str(status).strip()[:50]
        if not normalized:
            raise ValueError("preview status is required")
        with self._tx(True) as db:
            cursor = db.execute(
                """UPDATE scheduled_preview_posts
                   SET status=?,last_error=?,updated_at=? WHERE job_id=?""",
                (normalized, str(error)[:1000] or None, _iso(at), int(job_id)),
            )
            if cursor.rowcount:
                self._audit(
                    db, "scheduled_preview_status_changed", "publication_job",
                    int(job_id), {"status": normalized}, at,
                )
            return max(0, cursor.rowcount)

    def list_terminal_previews(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                """SELECT p.* FROM scheduled_preview_posts AS p
                   JOIN publication_jobs AS j ON j.job_id=p.job_id
                   WHERE p.status='active'
                     AND j.status IN ('done','failed','cancelled','needs_review')
                   ORDER BY p.job_id,p.part_no LIMIT ?""",
                (min(1000, max(1, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_runtime_state(self, key: str, default: str = "") -> str:
        with self._read() as db:
            row = db.execute(
                "SELECT state_value FROM price_runtime_state WHERE state_key=?",
                (str(key),),
            ).fetchone()
        return str(row["state_value"]) if row is not None else str(default)

    def set_runtime_state(
        self,
        key: str,
        value: Any,
        *,
        now: datetime | str | None = None,
    ) -> None:
        at = _dt(now, "now", default=True)
        with self._tx(True) as db:
            db.execute(
                """INSERT INTO price_runtime_state(state_key,state_value,updated_at)
                   VALUES(?,?,?) ON CONFLICT(state_key) DO UPDATE SET
                   state_value=excluded.state_value,updated_at=excluded.updated_at""",
                (str(key)[:200], str(value), _iso(at)),
            )

    def build_post_index_records(
        self,
        main_channel_id: str,
        preview_channel_id: str = "",
    ) -> list[dict[str, Any]]:
        """Build one current-ID registry row for every snapshot section."""
        main_channel = str(main_channel_id)
        preview_channel = str(preview_channel_id)
        with self._read() as db:
            sections = db.execute(
                """SELECT s.section_key,s.title,s.position
                   FROM price_sections AS s JOIN price_snapshots AS p
                     ON p.snapshot_id=s.snapshot_id
                   WHERE p.is_current=1 ORDER BY s.position,s.section_key"""
            ).fetchall()
            main_posts = db.execute(
                """SELECT section_key,message_id,post_url,updated_at
                   FROM telegram_posts WHERE channel_id=? AND is_current=1
                   ORDER BY section_key,part_no""",
                (main_channel,),
            ).fetchall()
            previews = db.execute(
                """SELECT p.section_key,p.job_id,p.message_id,j.execute_at
                   FROM scheduled_preview_posts AS p
                   JOIN publication_jobs AS j ON j.job_id=p.job_id
                   WHERE p.channel_id=? AND p.status='active'
                     AND j.status='pending'
                   ORDER BY p.section_key,j.execute_at,p.job_id,p.part_no""",
                (preview_channel,),
            ).fetchall() if preview_channel else []
        main_by_section: dict[str, list[sqlite3.Row]] = {}
        for row in main_posts:
            main_by_section.setdefault(row["section_key"], []).append(row)
        preview_by_section: dict[str, list[sqlite3.Row]] = {}
        for row in previews:
            preview_by_section.setdefault(row["section_key"], []).append(row)
        records: list[dict[str, Any]] = []
        for section in sections:
            key = section["section_key"]
            current = main_by_section.get(key, [])
            pending = preview_by_section.get(key, [])
            records.append({
                "section_key": key,
                "section_name": section["title"],
                "position": section["position"],
                "main_channel_id": main_channel,
                "main_message_ids": [row["message_id"] for row in current],
                "main_post_urls": [row["post_url"] for row in current],
                "has_current_post": bool(current),
                "preview_channel_id": preview_channel,
                "preview_job_ids": list(dict.fromkeys(
                    row["job_id"] for row in pending
                )),
                "preview_message_ids": [row["message_id"] for row in pending],
                "preview_execute_at": list(dict.fromkeys(
                    row["execute_at"] for row in pending
                )),
            })
        return records

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
                                  horizon_days: int = 2) -> int:
        """Create durable 09:30 jobs from the monthly calendar plan."""
        now = _dt(now_utc, "now_utc")
        if not 0 <= int(horizon_days) <= 31:
            raise ValueError("horizon_days must be between 0 and 31")
        if self.settings is None or not self.settings.telegram_channel_id:
            return 0
        zone = ZoneInfo(self.settings.timezone)
        horizon = now + timedelta(days=int(horizon_days))
        local_date = now.astimezone(zone).date()
        last_date = horizon.astimezone(zone).date()
        dates: list[date] = []
        while local_date <= last_date:
            dates.append(local_date)
            local_date += timedelta(days=1)

        created = 0
        with self._tx(True) as db:
            for target_date in dates:
                execute = datetime.combine(
                    target_date,
                    time(9, 30),
                    tzinfo=zone,
                ).astimezone(timezone.utc)
                if execute <= now or execute > horizon:
                    continue
                plan_days: list[int] = []
                if 1 <= target_date.day <= 30:
                    plan_days.append(target_date.day)
                if target_date.day == 1:
                    previous = target_date - timedelta(days=1)
                    previous_last_day = monthrange(
                        previous.year, previous.month
                    )[1]
                    plan_days.extend(range(previous_last_day + 1, 31))
                for plan_day in plan_days:
                    rows = db.execute(
                        """SELECT * FROM calendar_publication_plan
                           WHERE day_of_month=? AND enabled=1
                           ORDER BY slot,subposition""",
                        (plan_day,),
                    ).fetchall()
                    for row in rows:
                        section_exists = db.execute(
                            """SELECT 1 FROM price_sections AS s
                               JOIN price_snapshots AS p
                                 ON p.snapshot_id=s.snapshot_id
                               WHERE p.is_current=1 AND s.section_key=?""",
                            (row["section_key"],),
                        ).fetchone()
                        if section_exists is None:
                            continue
                        execute_iso = _iso(execute)
                        existing = db.execute(
                            """SELECT 1 FROM publication_jobs
                               WHERE section_key=? AND action='send'
                                 AND execute_at=? AND status!='cancelled'
                               LIMIT 1""",
                            (row["section_key"], execute_iso),
                        ).fetchone()
                        if existing is not None:
                            continue
                        unique = (
                            f"calendar:{target_date.isoformat()}:{plan_day}:"
                            f"{row['slot']}:{row['subposition']}:"
                            f"{row['section_key']}"
                        )
                        payload = {
                            "source": "price_calendar",
                            "calendar_date": target_date.isoformat(),
                            "plan_day": plan_day,
                            "slot": row["slot"],
                            "subposition": row["subposition"],
                        }
                        cursor = db.execute(
                            """INSERT INTO publication_jobs(
                                 dedupe_key,action,section_key,channel_id,
                                 channel_key,snapshot_policy,execute_at,
                                 schedule_type,schedule_json,status,attempts,
                                 max_attempts,payload_json,created_at,updated_at)
                                 VALUES(?,'send',?,?,?,'latest',?,'once','{}',
                                        'pending',0,8,?,?,?)
                                 ON CONFLICT(dedupe_key) DO NOTHING""",
                            (
                                unique,
                                row["section_key"],
                                self.settings.telegram_channel_id,
                                self.settings.telegram_channel_username,
                                execute_iso,
                                _dump(payload),
                                _iso(now),
                                _iso(now),
                            ),
                        )
                        if cursor.rowcount == 1:
                            job_id = int(cursor.lastrowid)
                            created += 1
                            self._audit(
                                db, "calendar_job_materialized",
                                "publication_job", job_id, payload, now,
                            )
        return created

    def list_calendar_plan(self) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                """SELECT plan.day_of_month,plan.slot,plan.subposition,
                          plan.requested_label,plan.section_key,
                          section.title AS section_name,plan.publish_time,
                          plan.enabled
                   FROM calendar_publication_plan AS plan
                   LEFT JOIN price_sections AS section
                     ON section.section_key=plan.section_key
                    AND section.snapshot_id=(
                      SELECT snapshot_id FROM price_snapshots WHERE is_current=1
                    )
                   ORDER BY plan.day_of_month,plan.slot,plan.subposition"""
            ).fetchall()
        return [
            {
                **dict(row),
                "enabled": bool(row["enabled"]),
                "timezone": self.timezone,
            }
            for row in rows
        ]

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
