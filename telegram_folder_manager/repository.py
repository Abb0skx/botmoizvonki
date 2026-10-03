from __future__ import annotations

import re
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_business.migrations import connect, migrate

from .config import FOLDER_CODES, SUPPLIER_CODES
from .daily import DAILY_CODES, day_key, stamp_day


CURSOR_KEY = "incoming_client_message_cursor"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _chat_id(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        raise ValueError("chat_id must be a positive Telegram user ID")
    return text


def _folder_code(value: object) -> str:
    code = str(value or "").strip().upper()
    if code not in FOLDER_CODES:
        raise ValueError(f"folder_code must be one of {', '.join(FOLDER_CODES)}")
    return code


def safe_error(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        value = f"{type(error).__name__}: {error}"
    else:
        value = str(error)
    value = re.sub(r"\b[0-9a-fA-F]{32,}\b", "[redacted]", value)
    value = re.sub(r"\b\d{7,}:[A-Za-z0-9_-]{20,}\b", "[redacted]", value)
    return " ".join(value.replace("\x00", "").split())[:500]


class FolderRepository:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        migrate(self.path)

    def _assign_in_db(
        self,
        db: sqlite3.Connection,
        chat_id: str,
        folder_code: str,
        now: datetime,
        *,
        source: str,
        assigned_by_id: str | None = None,
        assigned_by_name: str | None = None,
        queue: bool = True,
    ) -> tuple[int, bool]:
        row = db.execute(
            "SELECT folder_code,revision FROM telegram_folder_assignments WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        changed = row is None or row["folder_code"] != folder_code
        # Daily clearing removes the current assignment, never its durable
        # jobs/cards. Revisions must not be reused on the next day.
        previous = db.execute(
            """SELECT MAX(revision) FROM (
                 SELECT revision FROM telegram_folder_jobs WHERE chat_id=?
                 UNION ALL SELECT assignment_revision FROM telegram_manager_cards WHERE chat_id=?)""",
            (chat_id, chat_id),
        ).fetchone()[0] if row is None else row["revision"]
        revision = int(previous or 0) + (1 if changed else 0)
        stamp = iso(now)
        if row is None:
            db.execute(
                """INSERT INTO telegram_folder_assignments(
                     chat_id,folder_code,revision,source,assigned_by_id,
                     assigned_by_name,assigned_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    chat_id, folder_code, revision, source, assigned_by_id,
                    assigned_by_name, stamp, stamp,
                ),
            )
        elif changed:
            db.execute(
                """UPDATE telegram_folder_assignments
                      SET folder_code=?,revision=?,source=?,assigned_by_id=?,
                          assigned_by_name=?,assigned_at=?,updated_at=?
                    WHERE chat_id=?""",
                (
                    folder_code, revision, source, assigned_by_id,
                    assigned_by_name, stamp, stamp, chat_id,
                ),
            )
            db.execute(
                """UPDATE telegram_folder_jobs
                      SET state='superseded',updated_at=?,lease_token=NULL,
                          lease_expires_at=NULL
                    WHERE chat_id=? AND state IN ('pending','retry','running')""",
                (stamp, chat_id),
            )
        elif source == "telegram_manual":
            db.execute(
                """UPDATE telegram_folder_assignments
                      SET source=?,updated_at=? WHERE chat_id=?""",
                (source, stamp, chat_id),
            )

        if queue and (changed or not self._has_live_job(db, chat_id, revision)):
            db.execute(
                """INSERT OR IGNORE INTO telegram_folder_jobs(
                     chat_id,folder_code,revision,state,attempts,next_attempt_at,
                     created_at,updated_at)
                   VALUES(?,?,?,'pending',0,?,?,?)""",
                (chat_id, folder_code, revision, stamp, stamp, stamp),
            )
        return revision, changed

    @staticmethod
    def _has_live_job(db: sqlite3.Connection, chat_id: str, revision: int) -> bool:
        return db.execute(
            """SELECT 1 FROM telegram_folder_jobs
                WHERE chat_id=? AND revision=?
                  AND state IN ('pending','retry','running','done')""",
            (chat_id, revision),
        ).fetchone() is not None

    def assign(
        self,
        chat_id: object,
        folder_code: object,
        now: datetime | None = None,
        *,
        source: str = "operator",
        assigned_by_id: str | None = None,
        assigned_by_name: str | None = None,
        force_sync: bool = False,
    ):
        chat = _chat_id(chat_id)
        code = _folder_code(folder_code)
        when = now or utcnow()
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            revision, changed = self._assign_in_db(
                db, chat, code, when,
                source=str(source or "operator")[:40],
                assigned_by_id=(str(assigned_by_id)[:32] if assigned_by_id else None),
                assigned_by_name=(str(assigned_by_name)[:80] if assigned_by_name else None),
                queue=True,
            )
            if force_sync and not changed:
                stamp = iso(when)
                db.execute(
                    """UPDATE telegram_folder_jobs
                          SET state='superseded',updated_at=?
                        WHERE chat_id=? AND revision=?
                          AND state IN ('failed','done')""",
                    (stamp, chat, revision),
                )
                revision += 1
                db.execute(
                    """UPDATE telegram_folder_assignments
                          SET revision=?,updated_at=? WHERE chat_id=?""",
                    (revision, stamp, chat),
                )
                db.execute(
                    """INSERT INTO telegram_folder_jobs(
                         chat_id,folder_code,revision,state,attempts,next_attempt_at,
                         created_at,updated_at)
                       VALUES(?,?,?,'pending',0,?,?,?)""",
                    (chat, code, revision, stamp, stamp, stamp),
                )
            return db.execute(
                "SELECT * FROM telegram_folder_assignments WHERE chat_id=?",
                (chat,),
            ).fetchone()

    def accept_remote(
        self, chat_id: object, folder_code: object,
        now: datetime | None = None,
    ):
        chat = _chat_id(chat_id)
        code = _folder_code(folder_code)
        when = now or utcnow()
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._assign_in_db(
                db, chat, code, when,
                source="telegram_manual", queue=False,
            )
            return db.execute(
                "SELECT * FROM telegram_folder_assignments WHERE chat_id=?",
                (chat,),
            ).fetchone()

    def assignment(self, chat_id: object):
        chat = _chat_id(chat_id)
        with connect(self.path) as db:
            return db.execute(
                "SELECT * FROM telegram_folder_assignments WHERE chat_id=?",
                (chat,),
            ).fetchone()

    def assignments(self):
        with connect(self.path) as db:
            return db.execute(
                "SELECT * FROM telegram_folder_assignments ORDER BY updated_at DESC"
            ).fetchall()

    def begin_day(self, now: datetime) -> str | None:
        """Durably expire yesterday, then request an idempotent remote clear.

        First installation keeps today's existing folders. A restart after a
        missed midnight catches up before processing any new client messages.
        """
        today, stamp = day_key(now), iso(now)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                "SELECT value FROM telegram_folder_state WHERE key='daily_queue_day'"
            ).fetchone()
            if state is None:
                db.execute(
                    "INSERT INTO telegram_folder_state VALUES('daily_queue_day',?,?)",
                    (today, stamp),
                )
            elif state["value"] < today:
                placeholders = ",".join("?" for _ in DAILY_CODES)
                rows = db.execute(
                    f"SELECT * FROM telegram_folder_assignments WHERE folder_code IN ({placeholders})",
                    DAILY_CODES,
                ).fetchall()
                for row in rows:
                    db.execute(
                        "INSERT OR IGNORE INTO telegram_folder_daily_history VALUES(?,?,?,?)",
                        (state["value"], row["chat_id"], json.dumps(dict(row)), stamp),
                    )
                db.execute(
                    f"DELETE FROM telegram_folder_assignments WHERE folder_code IN ({placeholders})",
                    DAILY_CODES,
                )
                db.execute(
                    f"""UPDATE telegram_folder_jobs SET state='superseded',updated_at=?,
                        lease_token=NULL,lease_expires_at=NULL
                        WHERE folder_code IN ({placeholders}) AND state IN ('pending','retry','running')""",
                    (stamp, *DAILY_CODES),
                )
                db.execute(
                    """UPDATE telegram_manager_cards SET status='cancelled',updated_at=?,
                       lease_token=NULL,lease_expires_at=NULL WHERE status!='cancelled'""",
                    (stamp,),
                )
                db.execute(
                    "UPDATE telegram_folder_state SET value=?,updated_at=? WHERE key='daily_queue_day'",
                    (today, stamp),
                )
                db.execute(
                    """INSERT INTO telegram_folder_state VALUES('daily_clear_pending',?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                    (today, stamp),
                )
            pending = db.execute(
                "SELECT value FROM telegram_folder_state WHERE key='daily_clear_pending'"
            ).fetchone()
            return str(pending["value"]) if pending else None

    def complete_day_clear(self, day: str) -> None:
        with connect(self.path) as db:
            db.execute(
                "DELETE FROM telegram_folder_state WHERE key='daily_clear_pending' AND value=?",
                (day,),
            )
            db.execute("DELETE FROM telegram_folder_state WHERE key='daily_clear_retry_at'")

    def day_clear_due(self, now: datetime) -> bool:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT value FROM telegram_folder_state WHERE key='daily_clear_retry_at'"
            ).fetchone()
            return row is None or row["value"] <= iso(now)

    def defer_day_clear(self, now: datetime, seconds: float) -> None:
        with connect(self.path) as db:
            db.execute(
                """INSERT INTO telegram_folder_state VALUES('daily_clear_retry_at',?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (iso(now + timedelta(seconds=max(5, seconds))), iso(now)),
            )

    @staticmethod
    def _supplier_slot(db: sqlite3.Connection, capacity: int) -> str | None:
        placeholders = ",".join("?" for _ in SUPPLIER_CODES)
        counts = dict(db.execute(
            "SELECT folder_code,COUNT(*) FROM telegram_folder_assignments "
            f"WHERE folder_code IN ({placeholders}) GROUP BY folder_code",
            SUPPLIER_CODES,
        ).fetchall())
        return next(
            (code for code in SUPPLIER_CODES if counts.get(code, 0) < capacity), None
        )

    def sync_supplier_members(
        self, members: dict[str, str], now: datetime,
        *, complete: bool, folder_capacity: int,
        private_dialog_ids: set[str] | None = None,
    ) -> tuple[list[str], list[str], list[str], list[str], int]:
        """Cache verified group membership and queue existing Business chats."""
        stamp = iso(now)
        normalized = {
            _chat_id(user_id): str(group_id)
            for user_id, group_id in members.items()
        }
        newly_assigned: list[str] = []
        released: list[str] = []
        overflow: list[str] = []
        private_dialog_ids = {
            _chat_id(chat_id) for chat_id in (private_dialog_ids or ())
        }
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if complete:
                db.execute("DELETE FROM telegram_supplier_group_members")
            db.executemany(
                """INSERT INTO telegram_supplier_group_members(user_id,group_id,updated_at)
                   VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                   group_id=excluded.group_id,updated_at=excluded.updated_at""",
                ((user_id, group_id, stamp) for user_id, group_id in normalized.items()),
            )
            if complete:
                former = db.execute(
                    """SELECT chat_id FROM telegram_folder_assignments a
                       WHERE a.folder_code IN ('SUPPLIER','SUPPLIER2','SUPPLIER3','SUPPLIER4')
                         AND a.source='supplier_group'
                         AND NOT EXISTS (
                           SELECT 1 FROM telegram_supplier_group_members m
                           WHERE m.user_id=a.chat_id)"""
                ).fetchall()
                for row in former:
                    chat_id = str(row["chat_id"])
                    self._assign_in_db(db, chat_id, "NEW", now, source="supplier_group_exit")
                    released.append(chat_id)
            business_matches = [
                str(row[0]) for row in db.execute(
                    """SELECT c.chat_id FROM business_clients c
                       JOIN telegram_supplier_group_members m ON m.user_id=c.chat_id
                       ORDER BY c.updated_at DESC,c.chat_id"""
                )
            ]
            cached_members = {
                str(row[0]) for row in db.execute(
                    "SELECT user_id FROM telegram_supplier_group_members"
                )
            }
            matches = list(dict.fromkeys([
                *business_matches, *sorted(private_dialog_ids & cached_members)
            ]))
            to_pause = [
                str(row[0]) for row in db.execute(
                    """SELECT c.chat_id FROM business_clients c
                       JOIN telegram_supplier_group_members m ON m.user_id=c.chat_id
                       WHERE c.bot_paused=0"""
                )
            ]
            for chat_id in matches:
                existing = db.execute(
                    "SELECT folder_code FROM telegram_folder_assignments WHERE chat_id=?",
                    (chat_id,),
                ).fetchone()
                if existing and existing["folder_code"] in SUPPLIER_CODES:
                    continue
                slot = self._supplier_slot(db, folder_capacity)
                if slot is None:
                    overflow.append(chat_id)
                    continue
                self._assign_in_db(db, chat_id, slot, now, source="supplier_group")
                newly_assigned.append(chat_id)
        return newly_assigned, released, overflow, to_pause, len(matches)

    def is_supplier_member(self, chat_id: object) -> bool:
        chat = _chat_id(chat_id)
        with connect(self.path) as db:
            return db.execute(
                "SELECT 1 FROM telegram_supplier_group_members WHERE user_id=?",
                (chat,),
            ).fetchone() is not None

    def discard_reserved_self(self, self_user_id: object, now: datetime) -> bool:
        """Saved Messages is a folder sentinel, never a supplier conversation."""
        chat = _chat_id(self_user_id)
        stamp = iso(now)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM telegram_supplier_group_members WHERE user_id=?",
                (chat,),
            )
            row = db.execute(
                """SELECT 1 FROM telegram_folder_assignments
                   WHERE chat_id=? AND source='supplier_group'
                     AND folder_code IN ('SUPPLIER','SUPPLIER2','SUPPLIER3','SUPPLIER4')""",
                (chat,),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                """UPDATE telegram_folder_jobs SET state='superseded',
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE chat_id=? AND state IN ('pending','retry','running')""",
                (stamp, chat),
            )
            db.execute(
                "DELETE FROM telegram_folder_assignments WHERE chat_id=?",
                (chat,),
            )
            return True

    def has_unfinished_current_job(self, chat_id: object) -> bool:
        chat = _chat_id(chat_id)
        with connect(self.path) as db:
            return db.execute(
                """SELECT 1 FROM telegram_folder_assignments a
                   JOIN telegram_folder_jobs j ON j.chat_id=a.chat_id
                    AND j.revision=a.revision
                   WHERE a.chat_id=? AND j.state IN ('pending','retry','running')""",
                (chat,),
            ).fetchone() is not None

    def seed_new_clients(
        self,
        now: datetime | None = None,
        *,
        backfill_existing: bool = False,
        reopen_done: bool = True,
        supplier_folder_capacity: int = 199,
        manager_cards_chat_id: str = "",
        limit: int = 500,
    ) -> int:
        """Queue NEW only for messages arriving after the durable cursor.

        The first production run establishes a boundary instead of unexpectedly
        tagging every historical chat.  Explicit backfill remains available.
        """
        when = now or utcnow()
        stamp = iso(when)
        created = 0
        newly_identified_suppliers: list[str] = []
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM telegram_folder_state WHERE key='daily_clear_pending'"
            ).fetchone():
                return 0
            state = db.execute(
                "SELECT value FROM telegram_folder_state WHERE key=?", (CURSOR_KEY,)
            ).fetchone()
            maximum = int(
                db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM business_messages"
                ).fetchone()[0]
            )
            if state is None:
                cursor = 0 if backfill_existing else maximum
                db.execute(
                    """INSERT INTO telegram_folder_state(key,value,updated_at)
                       VALUES(?,?,?)""",
                    (CURSOR_KEY, str(cursor), stamp),
                )
                if not backfill_existing:
                    return 0
            else:
                try:
                    cursor = max(0, int(state["value"]))
                except (TypeError, ValueError):
                    cursor = maximum

            rows = db.execute(
                """SELECT id,chat_id,telegram_date,created_at FROM business_messages
                    WHERE id>? AND sender_type='client' AND deleted_at IS NULL
                    ORDER BY id LIMIT ?""",
                (cursor, max(1, min(int(limit), 5000))),
            ).fetchall()
            for row in rows:
                message_day = stamp_day(row["telegram_date"] or row["created_at"])
                if message_day and message_day > day_key(when):
                    # The DB lock may have crossed midnight after the caller
                    # sampled its clock. Leave this event for the next cycle.
                    break
                cursor = int(row["id"])
                # Delayed/replayed updates from yesterday cannot repopulate
                # today's queues. The original Telegram date wins over receipt.
                if message_day != day_key(when):
                    continue
                chat = str(row["chat_id"] or "")
                if not re.fullmatch(r"[1-9][0-9]{0,19}", chat):
                    continue
                current = db.execute(
                    """SELECT folder_code FROM telegram_folder_assignments
                       WHERE chat_id=?""",
                    (chat,),
                ).fetchone()
                supplier_member = db.execute(
                    "SELECT 1 FROM telegram_supplier_group_members WHERE user_id=?",
                    (chat,),
                ).fetchone()
                if supplier_member and (current is None or current["folder_code"] not in SUPPLIER_CODES):
                    slot = self._supplier_slot(db, supplier_folder_capacity)
                    if slot is not None:
                        self._assign_in_db(
                            db, chat, slot, when, source="supplier_group"
                        )
                        newly_identified_suppliers.append(chat)
                        created += 1
                    continue
                if current is None or (reopen_done and current["folder_code"] == "DONE"):
                    revision, changed = self._assign_in_db(
                        db, chat, "NEW", when,
                        source="new_client_message", queue=True,
                    )
                    created += int(changed)
                    if changed and manager_cards_chat_id:
                        db.execute(
                            """INSERT OR IGNORE INTO telegram_manager_cards(
                                 chat_id,assignment_revision,group_chat_id,
                                 next_attempt_at,created_at,updated_at)
                               VALUES(?,?,?,?,?,?)""",
                            (chat, revision, manager_cards_chat_id, stamp, stamp, stamp),
                        )
            db.execute(
                """UPDATE telegram_folder_state SET value=?,updated_at=?
                   WHERE key=?""",
                (str(cursor), stamp, CURSOR_KEY),
            )
        if newly_identified_suppliers:
            from telegram_business.repository import BusinessRepository

            business = BusinessRepository(self.path)
            for chat in newly_identified_suppliers:
                client = business.client(chat)
                if client and not client["bot_paused"]:
                    business.set_bot_paused(chat, True, when, "supplier_group")
        return created

    def claim_due(
        self, now: datetime | None = None, *, limit: int = 20,
        lease_seconds: int = 120,
    ):
        when = now or utcnow()
        stamp = iso(when)
        expires = iso(when + timedelta(seconds=max(30, lease_seconds)))
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE telegram_folder_jobs
                      SET state='retry',lease_token=NULL,lease_expires_at=NULL,
                          next_attempt_at=?,updated_at=?
                    WHERE state='running' AND lease_expires_at<=?""",
                (stamp, stamp, stamp),
            )
            rows = db.execute(
                """SELECT job_id FROM telegram_folder_jobs
                    WHERE state IN ('pending','retry') AND next_attempt_at<=?
                    ORDER BY job_id LIMIT ?""",
                (stamp, max(1, min(int(limit), 100))),
            ).fetchall()
            claimed = []
            for row in rows:
                token = secrets.token_urlsafe(24)
                changed = db.execute(
                    """UPDATE telegram_folder_jobs
                          SET state='running',attempts=attempts+1,
                              lease_token=?,lease_expires_at=?,updated_at=?
                        WHERE job_id=? AND state IN ('pending','retry')""",
                    (token, expires, stamp, row["job_id"]),
                ).rowcount
                if changed:
                    claimed.append(
                        db.execute(
                            "SELECT * FROM telegram_folder_jobs WHERE job_id=?",
                            (row["job_id"],),
                        ).fetchone()
                    )
            return claimed

    def is_current(self, job) -> bool:
        with connect(self.path) as db:
            row = db.execute(
                """SELECT folder_code,revision FROM telegram_folder_assignments
                   WHERE chat_id=?""",
                (job["chat_id"],),
            ).fetchone()
            return bool(
                row
                and row["folder_code"] == job["folder_code"]
                and int(row["revision"]) == int(job["revision"])
            )

    def finish(self, job_id: int, lease_token: str, now: datetime | None = None) -> bool:
        stamp = iso(now or utcnow())
        with connect(self.path) as db:
            return bool(db.execute(
                """UPDATE telegram_folder_jobs
                      SET state='done',completed_at=?,updated_at=?,
                          lease_token=NULL,lease_expires_at=NULL,last_error=NULL
                    WHERE job_id=? AND state='running' AND lease_token=?""",
                (stamp, stamp, int(job_id), lease_token),
            ).rowcount)

    def supersede(self, job_id: int, lease_token: str, now: datetime | None = None) -> bool:
        stamp = iso(now or utcnow())
        with connect(self.path) as db:
            return bool(db.execute(
                """UPDATE telegram_folder_jobs
                      SET state='superseded',updated_at=?,lease_token=NULL,
                          lease_expires_at=NULL
                    WHERE job_id=? AND state='running' AND lease_token=?""",
                (stamp, int(job_id), lease_token),
            ).rowcount)

    def retry(
        self, job_id: int, lease_token: str, error: BaseException | str,
        now: datetime | None = None, *, max_attempts: int = 12,
        retry_after: float | None = None,
    ) -> bool:
        when = now or utcnow()
        with connect(self.path) as db:
            row = db.execute(
                """SELECT attempts FROM telegram_folder_jobs
                   WHERE job_id=? AND state='running' AND lease_token=?""",
                (int(job_id), lease_token),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            terminal = attempts >= max(1, int(max_attempts))
            delay = (
                max(1.0, float(retry_after))
                if retry_after is not None
                else min(3600.0, float(2 ** min(attempts, 11)))
            )
            return bool(db.execute(
                """UPDATE telegram_folder_jobs
                      SET state=?,next_attempt_at=?,last_error=?,updated_at=?,
                          lease_token=NULL,lease_expires_at=NULL
                    WHERE job_id=? AND state='running' AND lease_token=?""",
                (
                    "failed" if terminal else "retry",
                    iso(when + timedelta(seconds=delay)),
                    safe_error(error), iso(when), int(job_id), lease_token,
                ),
            ).rowcount)

    def jobs(self):
        with connect(self.path) as db:
            return db.execute(
                "SELECT * FROM telegram_folder_jobs ORDER BY job_id"
            ).fetchall()
