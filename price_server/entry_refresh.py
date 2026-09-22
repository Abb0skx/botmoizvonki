"""Queue one worker refresh after an authoritative price-entry save.

The price worker intentionally remains the only component that applies the
existing pricing rules and renders the public snapshot.  Production may mount
its durable control directory into the price web container and point
``PRICE_ENTRY_REFRESH_QUEUE_PATH`` at that existing SQLite queue.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


LOG = logging.getLogger(__name__)
TASK_NAME = "import-prices"
REQUEST_PREFIX = "telegram:price-entry:"
ACTIVE_WAITING_STATUSES = ("queued", "busy_wait")
REQUIRED_COLUMNS = {
    "request_id", "update_id", "task_name", "user_id", "chat_id",
    "status", "next_attempt_at", "created_at", "notification_status",
    "notified_at",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _update_id(operation_id: str) -> int:
    # Telegram update IDs are non-negative.  A deterministic negative value
    # cannot collide with real controller updates and keeps replays idempotent.
    return -((uuid.UUID(operation_id).int % (2**62 - 1)) + 1)


def enqueue_price_refresh(
    operation_id: str,
    *,
    queue_path: str | Path | None = None,
) -> dict[str, object]:
    """Queue a refresh without ever making an already-applied save ambiguous.

    At most one waiting price-entry refresh is retained.  When a worker import
    is already running, one follow-up item is allowed so edits made during that
    run cannot be lost.
    """
    try:
        parsed_operation = str(uuid.UUID(str(operation_id)))
    except (ValueError, TypeError, AttributeError):
        return {"status": "unavailable"}

    configured = str(
        queue_path
        if queue_path is not None
        else os.getenv("PRICE_ENTRY_REFRESH_QUEUE_PATH", "")
    ).strip()
    if not configured:
        return {"status": "disabled"}
    path = Path(configured)
    if not path.is_file():
        LOG.warning("price_entry_refresh_unavailable type=QueueNotFound")
        return {"status": "unavailable"}

    database: sqlite3.Connection | None = None
    try:
        database = sqlite3.connect(path, timeout=5)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=5000")
        database.execute("BEGIN IMMEDIATE")
        columns = {
            str(row[1])
            for row in database.execute("PRAGMA table_info(control_queue)")
        }
        if not REQUIRED_COLUMNS.issubset(columns):
            raise sqlite3.DatabaseError("worker queue schema mismatch")

        waiting = database.execute(
            """SELECT request_id FROM control_queue
               WHERE task_name=? AND request_id GLOB ?
                 AND status IN (?,?)
               ORDER BY created_at,request_id LIMIT 1""",
            (
                TASK_NAME,
                REQUEST_PREFIX + "*",
                *ACTIVE_WAITING_STATUSES,
            ),
        ).fetchone()
        if waiting is not None:
            database.commit()
            return {"status": "queued", "coalesced": True}

        created_at = _timestamp()
        request_id = REQUEST_PREFIX + parsed_operation
        database.execute(
            """INSERT OR IGNORE INTO control_queue(
                 request_id,update_id,task_name,user_id,chat_id,status,
                 next_attempt_at,created_at,notification_status,notified_at
               ) VALUES(?,?,?,?,?,'queued',?,?,?,?)""",
            (
                request_id,
                _update_id(parsed_operation),
                TASK_NAME,
                0,
                0,
                created_at,
                created_at,
                "sent",
                created_at,
            ),
        )
        database.commit()
        return {"status": "queued", "coalesced": False}
    except sqlite3.Error as exc:
        if database is not None:
            try:
                database.rollback()
            except sqlite3.Error:
                pass
        LOG.warning(
            "price_entry_refresh_unavailable type=%s", type(exc).__name__
        )
        return {"status": "unavailable"}
    finally:
        if database is not None:
            database.close()


__all__ = ["enqueue_price_refresh"]
