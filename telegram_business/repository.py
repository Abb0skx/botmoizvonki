from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable

from .migrations import connect, migrate
from .security import redact_payment_data, redact_sensitive_data, sanitize_telegram_payload
from .timeutils import business_date, is_night, manager_due_at, work_seconds


TERMINAL_UPDATE_STATUSES = {"processed", "ignored", "rejected", "duplicate", "failed"}
_MANUAL_MESSAGE_FIELDS = {
    "text", "caption", "location", "venue", "contact", "photo", "animation",
    "audio", "document", "paid_media", "sticker", "story", "video",
    "video_note", "voice", "dice", "game", "poll",
}
_REQUEST_ACTIVE_STATUSES = ("collecting", "ready", "submitted")
_REQUEST_EDITABLE_STATUSES = ("collecting", "ready")
_REQUEST_EDITABLE_FIELDS = {
    "status", "wizard_state", "language", "exact_model", "model_key",
    "model_url", "option_kind", "option_value", "color", "color_any",
    "fulfillment_method", "phone", "contact_method", "location_url",
    "address", "preferred_time", "database_price", "source_updated_at",
    "needs_manager_reply",
}
_REQUEST_FIELD_ALIASES = {
    "state": "wizard_state",
    "current_step": "wizard_state",
    "expected_input": "wizard_state",
    "fulfillment_mode": "fulfillment_method",
    "fulfillment": "fulfillment_method",
    "model_name": "exact_model",
    "model": "exact_model",
    "matched_model": "exact_model",
    "attribute_kind": "option_kind",
    "attribute_value": "option_value",
    "any_color": "color_any",
    "location_text": "address",
    "price": "database_price",
    "price_uzs": "database_price",
    "price_source_updated_at": "source_updated_at",
}
_UPDATE_CLAIM: ContextVar[tuple[str, int, str] | None] = ContextVar(
    "telegram_business_update_claim", default=None
)
_ACTION_CLAIM: ContextVar[tuple[str, int, str, int] | None] = ContextVar(
    "telegram_business_action_claim", default=None
)


def iso(value: datetime) -> str:
    return value.isoformat()


def telegram_datetime(message: dict[str, Any], fallback: datetime, *, edited: bool = False) -> datetime:
    raw = message.get("edit_date" if edited else "date")
    if not isinstance(raw, (int, float)):
        return fallback
    return datetime.fromtimestamp(raw, tz=fallback.tzinfo or timezone.utc)


def _backoff_seconds(attempts: int, minimum: float = 2.0, maximum: float = 3600.0) -> float:
    return min(maximum, max(minimum, float(2 ** min(max(attempts, 1), 11))))


class PendingBusinessCallbackError(RuntimeError):
    """A pre-boundary customer click must finish before draft expiry."""

    retryable = True
    retry_after = 2.0
    # The matching callback update owns the real finite retry budget. Request
    # expiry must stay recoverable until that update is applied or terminal.
    max_attempts = 1_000_000


class BusinessRepository:
    def __init__(self, path):
        self.path = path
        self._identity = str(path)
        migrate(path)

    @contextmanager
    def bind_update_claim(self, update_id: int, lease_token: str):
        marker = _UPDATE_CLAIM.set((self._identity, update_id, lease_token))
        try:
            yield
        finally:
            _UPDATE_CLAIM.reset(marker)

    @contextmanager
    def bind_action_claim(self, action_id: int, lease_token: str, generation: int):
        marker = _ACTION_CLAIM.set(
            (self._identity, action_id, lease_token, generation)
        )
        try:
            yield
        finally:
            _ACTION_CLAIM.reset(marker)

    # Updates are first persisted and only then claimed by a worker.
    @staticmethod
    def _is_known_manual_manager(
        db: sqlite3.Connection,
        connection_id: str | None,
        event: dict[str, Any],
    ) -> bool:
        if (
            not connection_id
            or event.get("sender_business_bot")
            or event.get("is_from_offline") is True
            or not _MANUAL_MESSAGE_FIELDS.intersection(event)
        ):
            return False
        connection = db.execute(
            "SELECT business_user_id FROM business_connections WHERE connection_id=?",
            (connection_id,),
        ).fetchone()
        return bool(
            connection
            and connection["business_user_id"]
            and str((event.get("from") or {}).get("id", ""))
            == str(connection["business_user_id"])
        )

    @staticmethod
    def _manager_event_is_current(
        db: sqlite3.Connection,
        chat_id: str,
        answer_at: datetime,
        manager_message_id: int | None,
    ) -> bool:
        """Whether this manual event is not older than the newest client event."""
        latest_client = db.execute(
            """SELECT telegram_date,message_id FROM business_messages
               WHERE chat_id=? AND sender_type='client' AND telegram_date IS NOT NULL
               ORDER BY julianday(telegram_date) DESC,message_id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
        if not latest_client:
            return True
        try:
            latest_client_at = datetime.fromisoformat(latest_client["telegram_date"])
        except (TypeError, ValueError):
            return False
        if latest_client_at < answer_at:
            return True
        if latest_client_at > answer_at:
            return False
        return manager_message_id is None or (
            int(latest_client["message_id"]) <= int(manager_message_id)
        )

    def save_update(
        self,
        update: dict[str, Any],
        now: datetime,
        *,
        allowed_connection_id: str | None = None,
    ) -> bool:
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise ValueError("Telegram update_id must be a non-negative integer")
        event_type = next(
            (key for key in (
                "business_connection", "business_message",
                "edited_business_message", "deleted_business_messages",
                "callback_query",
            ) if key in update),
            "unknown",
        )
        event = update.get(event_type) or {}
        callback_message = (
            event.get("message") or {}
            if event_type == "callback_query" else {}
        )
        connection_id = (
            event.get("id")
            if event_type == "business_connection"
            else callback_message.get("business_connection_id")
            if event_type == "callback_query"
            else event.get("business_connection_id")
        )
        # Production ingestion always supplies the configured allowlist.  Keep
        # the legacy ``None`` behavior for direct repository callers/tests; an
        # explicitly supplied mismatched ID is always fail-closed.
        allowed_connection = bool(
            connection_id
            and (
                allowed_connection_id is None
                or str(connection_id) == str(allowed_connection_id)
            )
        )
        chat_id = (
            (callback_message.get("chat") or {}).get("id")
            if event_type == "callback_query"
            else (event.get("chat") or {}).get("id") or event.get("chat_id")
        )
        # Edit revisions share the Telegram message id. Idempotency for revisions is
        # update_id; keeping message_id here would reject every edit after the first.
        message_id = event.get("message_id") if event_type == "business_message" else None
        callback_query_id = (
            str(event.get("id")) if event_type == "callback_query" and event.get("id")
            else None
        )
        callback_message_id = (
            callback_message.get("message_id") if event_type == "callback_query"
            else None
        )
        safe_update = sanitize_telegram_payload(update)
        try:
            with connect(self.path) as db:
                db.execute(
                    """INSERT INTO business_updates(
                         update_id,event_type,business_connection_id,chat_id,message_id,
                         raw_payload,received_at,next_attempt_at,
                         callback_query_id,callback_message_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (update_id, event_type, connection_id, str(chat_id) if chat_id is not None else None,
                     message_id, json.dumps(safe_update, ensure_ascii=False), iso(now), iso(now),
                     callback_query_id, callback_message_id),
                )
                # A revocation must fence replies before the durable worker gets a
                # turn.  Persist it in the same transaction as the webhook row so
                # another scheduler/replica cannot claim an action using stale
                # ``can_reply`` rights after this webhook has been acknowledged.
                if event_type == "business_connection" and allowed_connection:
                    rights = event.get("rights") or {}
                    is_enabled = int(bool(event.get("is_enabled", True)))
                    can_reply = int(bool(rights.get("can_reply", False)))
                    if not is_enabled or not can_reply:
                        business_user_id = str((event.get("user") or {}).get("id", ""))
                        db.execute(
                            """INSERT INTO business_connections(
                               connection_id,business_user_id,is_enabled,can_reply,
                               created_at,updated_at) VALUES(?,?,?,?,?,?)
                               ON CONFLICT(connection_id) DO UPDATE SET
                               business_user_id=CASE
                                 WHEN excluded.business_user_id!=''
                                 THEN excluded.business_user_id
                                 ELSE business_connections.business_user_id END,
                               is_enabled=excluded.is_enabled,
                               can_reply=excluded.can_reply,
                               updated_at=excluded.updated_at""",
                            (
                                str(connection_id),
                                business_user_id,
                                is_enabled,
                                can_reply,
                                iso(now),
                                iso(now),
                            ),
                        )
                        db.execute(
                            """UPDATE scheduled_actions SET status='cancelled',
                               generation=generation+1,lease_token=NULL,
                               lease_expires_at=NULL,executed_at=?
                               WHERE status IN ('pending','running')""",
                            (iso(now),),
                        )
                        self._expire_requests_for_connection(
                            db, str(connection_id), now,
                        )
                if (
                    event_type == "business_message"
                    and allowed_connection
                    and self._is_known_manual_manager(db, connection_id, event)
                    and chat_id is not None
                    and event.get("message_id") is not None
                ):
                    manager_at = telegram_datetime(event, now)
                    db.execute(
                        """INSERT OR IGNORE INTO business_manager_fences(
                           business_connection_id,chat_id,message_id,telegram_date,
                           update_id,received_at) VALUES(?,?,?,?,?,?)""",
                        (
                            connection_id,
                            str(chat_id),
                            int(event["message_id"]),
                            iso(manager_at),
                            update_id,
                            iso(now),
                        ),
                    )
                    # A manual reply has priority even before the durable worker
                    # reaches its full manager_answer transaction. An old delayed
                    # manager webhook, however, must not fence a newer client cycle.
                    if self._manager_event_is_current(
                        db, str(chat_id), manager_at, int(event["message_id"])
                    ):
                        db.execute(
                            """UPDATE scheduled_actions SET status='cancelled',
                               generation=generation+1,lease_token=NULL,
                               lease_expires_at=NULL,executed_at=? WHERE chat_id=?
                               AND status IN ('pending','running')""",
                            (iso(now), str(chat_id)),
                        )
            return True
        except sqlite3.IntegrityError:
            return False

    def update(self, update_id: int):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM business_updates WHERE update_id=?", (update_id,)).fetchone()

    def mark_update(self, update_id: int, status: str, now: datetime, error: str | None = None) -> bool:
        terminal = status in TERMINAL_UPDATE_STATUSES
        safe_error = (redact_sensitive_data(error) or "")[:500] or None
        claim = _UPDATE_CLAIM.get()
        fenced_token = (
            claim[2]
            if claim and claim[0] == self._identity and claim[1] == update_id
            else None
        )
        # An error produced inside a claimed worker remains leased until the
        # scheduler applies backoff. This prevents another replica from stealing
        # it in the small gap between process_update() and retry_update().
        preserve_lease = bool(fenced_token and not terminal)
        sql = """UPDATE business_updates SET status=?,processed_at=?,error=?,
                 lease_token=CASE WHEN ? THEN lease_token ELSE NULL END,
                 lease_expires_at=CASE WHEN ? THEN lease_expires_at ELSE NULL END,
                 next_attempt_at=CASE WHEN ? THEN next_attempt_at ELSE ? END
                 WHERE update_id=?"""
        params: list[Any] = [
            status,
            iso(now) if terminal else None,
            safe_error,
            int(preserve_lease),
            int(preserve_lease),
            int(terminal),
            iso(now),
            update_id,
        ]
        if fenced_token:
            sql += " AND status='running' AND lease_token=?"
            params.append(fenced_token)
        with connect(self.path) as db:
            return db.execute(sql, params).rowcount == 1

    def complete_update_claim(
        self, update_id: int, now: datetime, lease_token: str
    ) -> bool:
        with connect(self.path) as db:
            return db.execute(
                """UPDATE business_updates SET status='processed',processed_at=?,error=NULL,
                   lease_token=NULL,lease_expires_at=NULL WHERE update_id=?
                   AND status='running' AND lease_token=?""",
                (iso(now), update_id, lease_token),
            ).rowcount == 1

    def claim_due_updates(self, now: datetime, limit: int = 50, lease_seconds: int = 60) -> list[sqlite3.Row]:
        claimed: list[sqlite3.Row] = []
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_stale_updates(db, now)
            candidates = db.execute(
                """SELECT update_id FROM business_updates WHERE status IN ('new','retry','error')
                   AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
                   AND (lease_token IS NULL OR lease_expires_at IS NULL
                        OR julianday(lease_expires_at)<=julianday(?))
                   ORDER BY received_at,update_id LIMIT ?""", (iso(now), iso(now), limit),
            ).fetchall()
            for candidate in candidates:
                token = uuid.uuid4().hex
                changed = db.execute(
                    """UPDATE business_updates SET status='running',attempts=attempts+1,
                       last_attempt_at=?,lease_token=?,lease_expires_at=?,error=NULL
                       WHERE update_id=? AND status IN ('new','retry','error')
                       AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
                       AND lease_token IS NULL""",
                    (iso(now), token, iso(now + timedelta(seconds=lease_seconds)), candidate["update_id"], iso(now)),
                ).rowcount
                if changed:
                    claimed.append(db.execute("SELECT * FROM business_updates WHERE update_id=?", (candidate["update_id"],)).fetchone())
        return claimed

    def retry_update(self, update_id: int, now: datetime, error: str, *, lease_token: str | None = None,
                     retry_after: float | None = None, max_attempts: int = 12) -> bool:
        safe_error = (redact_sensitive_data(error) or "update processing failed")[:500]
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status,attempts,lease_token,callback_query_id
                   FROM business_updates WHERE update_id=?""",
                (update_id,),
            ).fetchone()
            if not row or row["status"] in TERMINAL_UPDATE_STATUSES:
                return False
            if lease_token and row["lease_token"] not in {None, lease_token}:
                return False
            if row["attempts"] >= max_attempts:
                db.execute("UPDATE business_updates SET status='failed',processed_at=?,error=?,lease_token=NULL,lease_expires_at=NULL WHERE update_id=?",
                           (iso(now), safe_error, update_id))
                if row["callback_query_id"]:
                    receipt = db.execute(
                        """SELECT request_id FROM business_callback_receipts
                           WHERE callback_query_id=? AND status='claimed'""",
                        (row["callback_query_id"],),
                    ).fetchone()
                    if receipt and receipt["request_id"]:
                        db.execute(
                            """UPDATE business_callback_receipts
                               SET status='rejected',outcome='update_failed',
                                   processed_at=?
                               WHERE callback_query_id=? AND status='claimed'""",
                            (iso(now), row["callback_query_id"]),
                        )
                        db.execute(
                            """UPDATE scheduled_actions SET status='pending',
                               attempts=0,generation=generation+1,execute_at=?,
                               next_attempt_at=?,lease_token=NULL,
                               lease_expires_at=NULL,last_error=NULL
                               WHERE dedupe_key=?
                               AND status IN ('pending','running','failed')
                               AND (status='failed' OR attempts>0)""",
                            (
                                iso(now),
                                iso(now),
                                f"request-expire:{receipt['request_id']}",
                            ),
                        )
                return True
            delay = max(_backoff_seconds(row["attempts"]), float(retry_after or 0))
            db.execute("UPDATE business_updates SET status='retry',next_attempt_at=?,error=?,lease_token=NULL,lease_expires_at=NULL WHERE update_id=?",
                       (iso(now + timedelta(seconds=delay)), safe_error, update_id))
            return True

    def _recover_stale_updates(self, db: sqlite3.Connection, now: datetime) -> int:
        return db.execute(
            """UPDATE business_updates SET status='retry',next_attempt_at=?,lease_token=NULL,
               lease_expires_at=NULL,error=COALESCE(error,'worker lease expired')
               WHERE status IN ('running','error') AND (
                 lease_expires_at IS NULL OR julianday(lease_expires_at) IS NULL
                 OR julianday(lease_expires_at)<=julianday(?))""",
            (iso(now), iso(now)),
        ).rowcount

    # Connections and clients.
    def _expire_requests_for_connection(
        self,
        db: sqlite3.Connection,
        connection_id: str,
        now: datetime,
        reason: str = "connection_disabled",
    ) -> None:
        rows = db.execute(
            """SELECT * FROM business_requests
               WHERE business_connection_id=?
               AND status IN ('collecting','ready')""",
            (str(connection_id),),
        ).fetchall()
        for row in rows:
            new_revision = int(row["revision"]) + 1
            db.execute(
                """UPDATE business_requests SET status='expired',
                   wizard_state='closed',revision=?,closed_at=?,close_reason=?,
                   needs_manager_reply=1,updated_at=? WHERE request_id=?
                   AND revision=? AND status IN ('collecting','ready')""",
                (
                    new_revision,
                    iso(now),
                    reason,
                    iso(now),
                    row["request_id"],
                    int(row["revision"]),
                ),
            )
            self._revoke_request_callbacks(
                db, row["request_id"], now, reason,
            )
            self._append_request_event(
                db,
                row["request_id"],
                new_revision,
                "expired",
                "system",
                now,
                event_key=f"{reason}:{new_revision}",
                payload={"reason": reason},
            )
            self._queue_request_outbox(db, row["request_id"], now)

    def _expire_requests_for_chat(
        self,
        db: sqlite3.Connection,
        chat_id: str,
        now: datetime,
        reason: str,
    ) -> None:
        rows = db.execute(
            """SELECT * FROM business_requests WHERE chat_id=?
               AND status IN ('collecting','ready')""",
            (str(chat_id),),
        ).fetchall()
        for row in rows:
            new_revision = int(row["revision"]) + 1
            changed = db.execute(
                """UPDATE business_requests SET status='expired',
                   wizard_state='closed',revision=?,closed_at=?,close_reason=?,
                   needs_manager_reply=1,updated_at=? WHERE request_id=?
                   AND revision=? AND status IN ('collecting','ready')""",
                (
                    new_revision,
                    iso(now),
                    str(reason)[:80],
                    iso(now),
                    row["request_id"],
                    int(row["revision"]),
                ),
            ).rowcount
            if changed != 1:
                continue
            self._revoke_request_callbacks(
                db, row["request_id"], now, str(reason)[:80],
            )
            self._append_request_event(
                db,
                row["request_id"],
                new_revision,
                "expired",
                "system",
                now,
                event_key=f"{str(reason)[:80]}:{new_revision}",
                payload={"reason": str(reason)[:80]},
            )
            self._queue_request_outbox(db, row["request_id"], now)

    def upsert_connection(self, event: dict, now: datetime) -> None:
        rights = event.get("rights") or {}
        is_enabled = int(event.get("is_enabled", True))
        can_reply = int(rights.get("can_reply", False))
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO business_connections VALUES(?,?,?,?,?,?)
                   ON CONFLICT(connection_id) DO UPDATE SET business_user_id=excluded.business_user_id,
                   is_enabled=excluded.is_enabled,can_reply=excluded.can_reply,updated_at=excluded.updated_at""",
                (event["id"], str((event.get("user") or {}).get("id", "")), is_enabled,
                 can_reply, iso(now), iso(now)),
            )
            if not is_enabled or not can_reply:
                # This repository is scoped to one allowed Business connection.
                # Fence already claimed replies as soon as Telegram revokes it.
                db.execute(
                    """UPDATE scheduled_actions SET status='cancelled',generation=generation+1,
                       lease_token=NULL,lease_expires_at=NULL,executed_at=?
                       WHERE status IN ('pending','running')""",
                    (iso(now),),
                )
                self._expire_requests_for_connection(
                    db, str(event["id"]), now,
                )

    def connection(self, connection_id: str):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM business_connections WHERE connection_id=?", (connection_id,)).fetchone()

    def connection_can_reply(self, connection_id: str) -> bool:
        row = self.connection(connection_id)
        return bool(row and row["is_enabled"] and row["can_reply"])

    def client(self, chat_id: str):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM business_clients WHERE chat_id=?", (chat_id,)).fetchone()

    def upsert_client(self, chat_id: str, user: dict, now: datetime) -> None:
        with connect(self.path) as db:
            db.execute(
                """INSERT INTO business_clients(chat_id,telegram_user_id,first_name,last_name,username,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                   telegram_user_id=excluded.telegram_user_id,first_name=excluded.first_name,
                   last_name=excluded.last_name,username=excluded.username,updated_at=excluded.updated_at""",
                (chat_id, str(user.get("id", "")), user.get("first_name"), user.get("last_name"),
                 user.get("username"), iso(now), iso(now)),
            )

    # Night sessions.
    @staticmethod
    def _session_key(value: datetime, night_start: time, night_end: time) -> str:
        if is_night(value, night_start, night_end):
            return f"{business_date(value, night_start).isoformat()}:night"
        return f"{value.date().isoformat()}:day"

    def session(
        self,
        chat_id: str,
        now: datetime,
        night_start: time = time(20),
        night_end: time = time(9, 30),
    ):
        key = self._session_key(now, night_start, night_end)
        session_id = f"{chat_id}:{key}"
        with connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO business_sessions(session_id,chat_id,business_date,started_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (session_id, chat_id, key, iso(now), iso(now), iso(now)))
            return db.execute("SELECT * FROM business_sessions WHERE session_id=?", (session_id,)).fetchone()

    def session_by_id(self, session_id: str):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM business_sessions WHERE session_id=?", (session_id,)).fetchone()

    # Message persistence and Sheets natural-key outbox.
    @staticmethod
    def _message_content(message: dict) -> tuple[str, str | None]:
        kind, file_id = "text", None
        for name in (
            "location", "voice", "video_note", "video", "animation", "photo",
            "document", "audio", "sticker", "paid_media", "story",
        ):
            if name not in message:
                continue
            kind, value = name, message[name]
            if name == "photo" and value:
                value = value[-1]
            elif name == "paid_media" and isinstance(value, dict):
                # PaidMediaInfo contains a list of typed media objects. Keep a
                # Telegram file_id when the concrete item exposes one, without
                # fetching or opening the media.
                paid_items = value.get("paid_media") or ()
                value = None
                for item in paid_items:
                    if not isinstance(item, dict):
                        continue
                    candidate = item.get("video") or item.get("photo")
                    if isinstance(candidate, list) and candidate:
                        candidate = candidate[-1]
                    if isinstance(candidate, dict) and candidate.get("file_id"):
                        value = candidate
                        break
            if isinstance(value, dict):
                file_id = value.get("file_id")
            break
        return kind, file_id

    def saved_message(self, connection_id: str, chat_id: str, message_id: int):
        """Return the canonical persisted Telegram message row."""
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM business_messages WHERE business_connection_id=?
                   AND chat_id=? AND message_id=?""",
                (connection_id, str(chat_id), int(message_id)),
            ).fetchone()

    @staticmethod
    def _message_entity_id(connection_id: str, chat_id: str, message_id: int) -> str:
        return f"{connection_id}:{chat_id}:{message_id}"

    def _surrogate_message_id(
        self, db: sqlite3.Connection, connection_id: str, chat_id: str, original: int
    ) -> int:
        candidate = -max(1, abs(original))
        while db.execute(
            """SELECT 1 FROM business_messages WHERE business_connection_id=?
               AND chat_id=? AND message_id=?""",
            (connection_id, chat_id, candidate),
        ).fetchone():
            candidate -= 1
        return candidate

    def _relocate_bot_collision(
        self, db: sqlite3.Connection, connection_id: str, chat_id: str, message_id: int, now: datetime
    ) -> bool:
        """Preserve an inbound message if a test/proxy supplied a colliding bot id.

        Telegram normally allocates one message-id namespace per chat, so this is a
        defensive path. The customer message keeps its real id; the already stored
        bot row receives a negative local surrogate and remains auditable.
        """
        existing = db.execute(
            """SELECT id,sender_type FROM business_messages WHERE business_connection_id=?
               AND chat_id=? AND message_id=?""",
            (connection_id, chat_id, message_id),
        ).fetchone()
        if not existing or existing["sender_type"] != "business_bot":
            return False
        surrogate = self._surrogate_message_id(db, connection_id, chat_id, message_id)
        old_key = self._message_entity_id(connection_id, chat_id, message_id)
        new_key = self._message_entity_id(connection_id, chat_id, surrogate)
        db.execute("UPDATE business_messages SET message_id=? WHERE id=?", (surrogate, existing["id"]))
        db.execute(
            """UPDATE sheets_outbox SET entity_id=? WHERE entity_type='message'
               AND entity_id=? AND operation='upsert'""",
            (new_key, old_key),
        )
        self._refresh_message_outbox(db, existing["id"], now, "sent")
        return True

    def _queue_outbox(self, db: sqlite3.Connection, entity_type: str, entity_id: str,
                      payload: dict[str, Any], now: datetime, operation: str = "upsert") -> None:
        db.execute(
            """INSERT INTO sheets_outbox(entity_type,entity_id,operation,payload,next_attempt_at,created_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,operation) DO UPDATE SET
               payload=excluded.payload,status='pending',
               attempts=CASE WHEN sheets_outbox.status='synced' THEN 0
                             ELSE sheets_outbox.attempts END,
               next_attempt_at=CASE
                 WHEN sheets_outbox.status='synced'
                   OR julianday(sheets_outbox.next_attempt_at)<=julianday(excluded.next_attempt_at)
                 THEN excluded.next_attempt_at ELSE sheets_outbox.next_attempt_at END,
               last_error=CASE WHEN sheets_outbox.status='synced' THEN NULL
                               ELSE sheets_outbox.last_error END,
               synced_at=NULL,lease_token=NULL,lease_expires_at=NULL,
               generation=sheets_outbox.generation+1""",
            (entity_type, entity_id, operation, json.dumps(payload, ensure_ascii=False), iso(now), iso(now)),
        )

    def _message_outbox_payload(self, row: sqlite3.Row, processed_status: str) -> dict[str, Any]:
        return {
            "event_id": self._message_entity_id(row["business_connection_id"], row["chat_id"], row["message_id"]),
            "update_id": str(row["update_id"] or ""), "business_connection_id": row["business_connection_id"],
            "chat_id": row["chat_id"], "message_id": str(row["message_id"]),
            "session_id": row["session_id"] or "", "cycle_id": row["cycle_id"] or "",
            "direction": row["direction"], "sender_type": row["sender_type"],
            "telegram_date_uz": row["telegram_date"] or "", "message_type": row["message_type"],
            "text": row["text"] or row["caption"] or "", "language": row["language"] or "",
            "intent": row["intent"] or "", "model_query": row["model_query"] or "",
            "template_code": row["template_code"] or "",
            "reply_to_message_id": str(row["reply_to_message_id"] or ""),
            "processed_status": processed_status,
            "created_at_utc": datetime.fromisoformat(row["created_at"]).astimezone(timezone.utc).isoformat(),
        }

    def _refresh_message_outbox(self, db: sqlite3.Connection, row_id: int, now: datetime, status: str) -> None:
        row = db.execute("SELECT * FROM business_messages WHERE id=?", (row_id,)).fetchone()
        if row:
            key = self._message_entity_id(row["business_connection_id"], row["chat_id"], row["message_id"])
            self._queue_outbox(db, "message", key, self._message_outbox_payload(row, status), now)

    @staticmethod
    def _as_utc(value: str | None) -> str:
        if not value:
            return ""
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    def _queue_dialog_outbox(
        self,
        db: sqlite3.Connection,
        session_id: str,
        now: datetime,
        cycle_id: str | None = None,
    ) -> None:
        session = db.execute(
            "SELECT * FROM business_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not session:
            return
        if cycle_id:
            cycle = db.execute(
                "SELECT * FROM response_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        else:
            cycle = db.execute(
                """SELECT * FROM response_cycles WHERE session_id=?
                   ORDER BY first_client_at DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        if not cycle:
            return
        client = db.execute(
            "SELECT * FROM business_clients WHERE chat_id=?", (session["chat_id"],)
        ).fetchone()
        name = " ".join(
            value for value in (
                client["first_name"] if client else None,
                client["last_name"] if client else None,
            ) if value
        )
        payload = {
            "session_id": session_id,
            "cycle_id": cycle["cycle_id"],
            "business_date": str(session["business_date"]).split(":", 1)[0],
            "chat_id": session["chat_id"],
            "telegram_user_id": client["telegram_user_id"] if client else "",
            "name": name,
            "username": client["username"] if client else "",
            "language": client["language"] if client else "",
            "first_client_at_uz": cycle["first_client_at"] or "",
            "last_client_at_uz": cycle["last_client_at"] or "",
            "first_bot_at_uz": cycle["first_bot_at"] or "",
            "bot_response_seconds": cycle["bot_response_seconds"] if cycle["bot_response_seconds"] is not None else "",
            "manager_due_at_uz": cycle["manager_due_at"] or "",
            "first_manager_at_uz": cycle["first_manager_at"] or "",
            "calendar_response_seconds": cycle["calendar_response_seconds"] if cycle["calendar_response_seconds"] is not None else "",
            "work_response_seconds": cycle["work_response_seconds"] if cycle["work_response_seconds"] is not None else "",
            "model_query": session["model_query"] or "",
            "matched_model": session["matched_model"] or "",
            "memory": session["memory"] or "",
            "color": session["color"] or "",
            "location_received": bool(session["location_received"]),
            "location_url": session["location_url"] or "",
            "preferred_time": session["preferred_time"] or "",
            "price_sent": bool(session["price_sent"]),
            "order_intent": bool(session["order_intent"]),
            "credit_intent": bool(session["credit_intent"]),
            "final_sent": bool(session["final_sent"]),
            "needs_manager_reply": bool(cycle["needs_manager_reply"]),
            "priority": bool(session["priority"]),
            "handoff_reason": session["handoff_reason"] or "",
            "status": cycle["status"] if cycle["status"] != "waiting_manager" else session["status"],
            "created_at_utc": self._as_utc(session["created_at"]),
            "updated_at_utc": now.astimezone(timezone.utc).isoformat(),
        }
        entity_id = f"{session_id}:{cycle['cycle_id']}"
        self._queue_outbox(db, "dialog", entity_id, payload, now)

    def save_message(self, connection_id: str, message: dict, session_id: str | None,
                     sender_type: str, now: datetime, update_id: int | None = None) -> bool:
        chat_id, message_id = str(message["chat"]["id"]), int(message["message_id"])
        if message.get("edit_date"):
            self.edit_message(connection_id, message, now, update_id=update_id)
            return False
        text, caption = redact_payment_data(message.get("text")), redact_payment_data(message.get("caption"))
        kind, file_id = self._message_content(message)
        direction = "incoming" if sender_type == "client" else "outgoing"
        try:
            with connect(self.path) as db:
                existing = db.execute(
                    """SELECT id,update_id,sender_type,original_received
                       FROM business_messages WHERE business_connection_id=?
                       AND chat_id=? AND message_id=?""",
                    (connection_id, chat_id, message_id),
                ).fetchone()
                if existing and not bool(existing["original_received"]):
                    # Telegram may deliver an edit revision before the original
                    # webhook after a retry/restart.  Keep the newer edited
                    # content, but attach it to the authoritative session and
                    # allow the original event to open the response cycle once.
                    db.execute(
                        """UPDATE business_messages SET session_id=?,direction=?,
                           sender_type=?,telegram_date=?,update_id=?,
                           original_received=1 WHERE id=?""",
                        (
                            session_id,
                            direction,
                            sender_type,
                            iso(telegram_datetime(message, now)),
                            update_id,
                            existing["id"],
                        ),
                    )
                    self._refresh_message_outbox(db, existing["id"], now, "stored")
                    return True
                # A retry of the same durable update must continue after the
                # message insert (touch/schedule may not have happened yet).
                # A different update carrying the same Telegram message remains
                # a real duplicate and must not start automation twice.
                if existing and update_id is not None and existing["update_id"] == update_id:
                    return True
                if (
                    existing and sender_type == "business_bot"
                    and existing["sender_type"] == "business_bot"
                    and existing["update_id"] is None and update_id is not None
                ):
                    db.execute(
                        "UPDATE business_messages SET update_id=? WHERE id=?",
                        (update_id, existing["id"]),
                    )
                    self._refresh_message_outbox(db, existing["id"], now, "received")
                    return False
                if sender_type == "client":
                    self._relocate_bot_collision(db, connection_id, chat_id, message_id, now)
                cursor = db.execute(
                    """INSERT INTO business_messages(business_connection_id,chat_id,message_id,session_id,
                       direction,sender_type,message_type,text,caption,file_id,reply_to_message_id,
                       telegram_date,created_at,update_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (connection_id, chat_id, message_id, session_id, direction, sender_type, kind, text, caption,
                     file_id, (message.get("reply_to_message") or {}).get("message_id"),
                     iso(telegram_datetime(message, now)), iso(now), update_id),
                )
                self._refresh_message_outbox(db, cursor.lastrowid, now, "stored")
            return True
        except sqlite3.IntegrityError:
            return False

    def _infer_sender_type(self, db: sqlite3.Connection, connection_id: str, message: dict) -> str:
        if message.get("sender_business_bot"):
            return "business_bot"
        if message.get("is_from_offline") is True:
            return "telegram_auto"
        connection = db.execute("SELECT business_user_id FROM business_connections WHERE connection_id=?", (connection_id,)).fetchone()
        if connection and str((message.get("from") or {}).get("id")) == connection["business_user_id"]:
            return "manager"
        return "client"

    def edit_message(
        self,
        connection_id: str,
        message: dict,
        event_at: datetime,
        update_id: int | None = None,
        night_start: time = time(20),
        night_end: time = time(9, 30),
    ) -> bool:
        chat_id, message_id = str(message["chat"]["id"]), int(message["message_id"])
        text, caption = redact_payment_data(message.get("text")), redact_payment_data(message.get("caption"))
        kind, file_id = self._message_content(message)
        edited_at, telegram_at = telegram_datetime(message, event_at, edited=True), telegram_datetime(message, event_at)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT id,edited_at,deleted_at,update_id,edit_update_id
                              FROM business_messages
                              WHERE business_connection_id=? AND chat_id=? AND message_id=?""",
                             (connection_id, chat_id, message_id)).fetchone()
            if row:
                # Deletion is monotonic, and an older edit revision must never
                # overwrite a newer one when multiple workers finish out of order.
                if row["deleted_at"]:
                    return False
                previous_edit = (
                    datetime.fromisoformat(row["edited_at"])
                    if row["edited_at"] else None
                )
                previous_edit_update_id = int(row["edit_update_id"] or -1)
                if previous_edit:
                    # A retry of the same durable edit must be allowed to
                    # continue into scheduling after a crash between the two
                    # transactions. Older/different revisions stay inert.
                    if (
                        edited_at == previous_edit
                        and update_id is not None
                        and update_id == previous_edit_update_id
                    ):
                        return True
                    if (
                        edited_at < previous_edit
                        or (edited_at == previous_edit and update_id is not None
                            and update_id < previous_edit_update_id)
                    ):
                        return False
                db.execute("""UPDATE business_messages SET message_type=?,text=?,caption=?,file_id=?,
                           reply_to_message_id=?,edited_at=?,
                           update_id=COALESCE(?,update_id),
                           edit_update_id=COALESCE(?,edit_update_id) WHERE id=?""",
                           (kind, text, caption, file_id, (message.get("reply_to_message") or {}).get("message_id"),
                            iso(edited_at), update_id, update_id, row["id"]))
                row_id = row["id"]
            else:
                sender = self._infer_sender_type(db, connection_id, message)
                direction = "incoming" if sender == "client" else "outgoing"
                key = self._session_key(telegram_at, night_start, night_end)
                session_id = f"{chat_id}:{key}"
                db.execute("INSERT OR IGNORE INTO business_sessions(session_id,chat_id,business_date,started_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (session_id, chat_id, key, iso(telegram_at), iso(event_at), iso(event_at)))
                cursor = db.execute("""INSERT INTO business_messages(business_connection_id,chat_id,message_id,
                    session_id,direction,sender_type,message_type,text,caption,file_id,reply_to_message_id,
                    telegram_date,created_at,edited_at,update_id,edit_update_id,
                    original_received)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (connection_id, chat_id, message_id, session_id, direction, sender, kind, text, caption, file_id,
                     (message.get("reply_to_message") or {}).get("message_id"), iso(telegram_at), iso(event_at),
                     iso(edited_at), update_id, update_id, 0))
                row_id = cursor.lastrowid
            self._refresh_message_outbox(db, row_id, event_at, "edited")
        return True

    def mark_deleted_messages(self, connection_id: str, chat_id: str, message_ids: Iterable[int],
                              event_at: datetime) -> int:
        affected = 0
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            for raw_id in message_ids:
                message_id = int(raw_id)
                row = db.execute("SELECT id FROM business_messages WHERE business_connection_id=? AND chat_id=? AND message_id=?",
                                 (connection_id, str(chat_id), message_id)).fetchone()
                if row:
                    db.execute("UPDATE business_messages SET deleted_at=? WHERE id=?", (iso(event_at), row["id"]))
                    row_id = row["id"]
                else:
                    cursor = db.execute("""INSERT INTO business_messages(business_connection_id,chat_id,message_id,
                        direction,sender_type,message_type,telegram_date,created_at,deleted_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (connection_id, str(chat_id), message_id, "unknown", "unknown",
                                                       "deleted", iso(event_at), iso(event_at), iso(event_at)))
                    row_id = cursor.lastrowid
                affected += 1
                self._refresh_message_outbox(db, row_id, event_at, "deleted")
        return affected

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        with connect(self.path) as db:
            rows = db.execute(
                """SELECT * FROM business_messages WHERE session_id=?
                   AND sender_type='client' AND deleted_at IS NULL
                   ORDER BY COALESCE(edited_at,telegram_date),message_id""",
                (session_id,),
            ).fetchall()
            # Automation treats an edit revision as the effective customer
            # event time. The canonical telegram_date remains unchanged in
            # SQLite/Sheets for response-time metrics and auditing.
            return [
                {
                    **dict(row),
                    "telegram_date": row["edited_at"] or row["telegram_date"],
                }
                for row in rows
            ]

    def latest_client_message(self, session_id: str):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM business_messages WHERE session_id=? AND sender_type='client' AND deleted_at IS NULL ORDER BY telegram_date DESC,message_id DESC LIMIT 1",
                              (session_id,)).fetchone()

    def annotate_message(
        self,
        connection_id: str,
        chat_id: str,
        message_id: int,
        now: datetime,
        *,
        language: str | None = None,
        intent: str | None = None,
        model_query: str | None = None,
    ) -> bool:
        fields = {
            "language": language,
            "intent": intent,
            "model_query": model_query,
        }
        values = {key: value for key, value in fields.items() if value is not None}
        if not values:
            return False
        assignments = ",".join(f"{key}=?" for key in values)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT id FROM business_messages WHERE business_connection_id=?
                   AND chat_id=? AND message_id=?""",
                (connection_id, str(chat_id), int(message_id)),
            ).fetchone()
            if not row:
                return False
            db.execute(
                f"UPDATE business_messages SET {assignments} WHERE id=?",
                (*values.values(), row["id"]),
            )
            self._refresh_message_outbox(db, row["id"], now, "processed")
            return True

    def annotate_session_messages(
        self,
        session_id: str,
        now: datetime,
        *,
        language: str | None = None,
        intent: str | None = None,
        model_query: str | None = None,
        message_ids: Iterable[int] | None = None,
    ) -> int:
        fields = {"language": language, "intent": intent, "model_query": model_query}
        values = {key: value for key, value in fields.items() if value is not None}
        if not values:
            return 0
        assignments = ",".join(f"{key}=?" for key in values)
        ids = tuple(int(value) for value in message_ids) if message_ids is not None else ()
        sql = "SELECT id FROM business_messages WHERE session_id=? AND sender_type='client'"
        params: list[Any] = [session_id]
        if message_ids is not None:
            if not ids:
                return 0
            sql += f" AND message_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(sql, params).fetchall()
            for row in rows:
                db.execute(
                    f"UPDATE business_messages SET {assignments} WHERE id=?",
                    (*values.values(), row["id"]),
                )
                self._refresh_message_outbox(db, row["id"], now, "processed")
            return len(rows)

    # Response-cycle timestamps are Telegram event timestamps, not webhook processing time.
    def touch_client_message(self, chat_id: str, session_id: str, now: datetime,
                             event_at: datetime | None = None, message_id: int | None = None,
                             manager_start: time = time(10), manager_end: time = time(20),
                             workdays=frozenset(range(7))) -> str:
        client_at = event_at or now
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            session_row = db.execute("SELECT last_client_message_at FROM business_sessions WHERE session_id=?", (session_id,)).fetchone()
            old = datetime.fromisoformat(session_row["last_client_message_at"]) if session_row and session_row["last_client_message_at"] else None
            db.execute("UPDATE business_sessions SET last_client_message_at=?,updated_at=? WHERE session_id=?",
                       (iso(max(old, client_at) if old else client_at), iso(now), session_id))
            client_row = db.execute("SELECT last_client_message_at FROM business_clients WHERE chat_id=?", (chat_id,)).fetchone()
            old_client = datetime.fromisoformat(client_row["last_client_message_at"]) if client_row and client_row["last_client_message_at"] else None
            db.execute("UPDATE business_clients SET last_client_message_at=?,updated_at=? WHERE chat_id=?",
                       (iso(max(old_client, client_at) if old_client else client_at), iso(now), chat_id))

            # Retrying a durable update after the manager has already answered it
            # must not open a second response cycle.  The message->cycle link is
            # the strongest idempotency marker; the manager message timestamp is
            # the fallback for an original client update that arrived late.
            linked_cycle = None
            if message_id is not None:
                linked_cycle = db.execute(
                    """SELECT c.* FROM business_messages m
                       JOIN response_cycles c ON c.cycle_id=m.cycle_id
                       WHERE m.chat_id=? AND m.message_id=? AND m.sender_type='client'
                       LIMIT 1""",
                    (chat_id, message_id),
                ).fetchone()
            covered_by_manager = self._manager_covers_client(
                db, chat_id, client_at, message_id
            )
            if (
                linked_cycle is not None
                and linked_cycle["status"] != "waiting_manager"
            ) or covered_by_manager:
                waiting = db.execute(
                    """SELECT 1 FROM response_cycles WHERE session_id=?
                       AND status='waiting_manager' LIMIT 1""",
                    (session_id,),
                ).fetchone()
                if waiting is None:
                    db.execute(
                        """UPDATE business_sessions SET needs_manager_reply=0,
                           status='manager_answered',updated_at=? WHERE session_id=?""",
                        (iso(now), session_id),
                    )
                return linked_cycle["cycle_id"] if linked_cycle is not None else ""

            db.execute(
                """UPDATE business_sessions SET needs_manager_reply=1,
                   status='waiting_manager',updated_at=? WHERE session_id=?""",
                (iso(now), session_id),
            )
            cycle = db.execute("SELECT * FROM response_cycles WHERE chat_id=? AND status='waiting_manager' ORDER BY first_client_at LIMIT 1", (chat_id,)).fetchone()
            if cycle:
                first, last = min(datetime.fromisoformat(cycle["first_client_at"]), client_at), max(datetime.fromisoformat(cycle["last_client_at"]), client_at)
                db.execute("UPDATE response_cycles SET first_client_at=?,last_client_at=? WHERE cycle_id=?", (iso(first), iso(last), cycle["cycle_id"]))
                cycle_id = cycle["cycle_id"]
            else:
                cycle_id = uuid.uuid4().hex
                due_at = manager_due_at(
                    client_at, manager_start, manager_end, workdays,
                )
                db.execute("""INSERT INTO response_cycles(
                    cycle_id,chat_id,session_id,first_client_at,last_client_at,manager_due_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (cycle_id, chat_id, session_id, iso(client_at), iso(client_at), iso(due_at)))
            if message_id is None:
                rows = db.execute("SELECT id FROM business_messages WHERE chat_id=? AND session_id=? AND sender_type='client' AND cycle_id IS NULL", (chat_id, session_id)).fetchall()
            else:
                rows = db.execute("SELECT id FROM business_messages WHERE chat_id=? AND message_id=? AND sender_type='client'", (chat_id, message_id)).fetchall()
            for row in rows:
                db.execute("UPDATE business_messages SET cycle_id=? WHERE id=?", (cycle_id, row["id"]))
                self._refresh_message_outbox(db, row["id"], now, "stored")
            self._queue_dialog_outbox(db, session_id, now, cycle_id)
            return cycle_id

    @staticmethod
    def _manager_covers_client(
        db: sqlite3.Connection,
        chat_id: str,
        client_at: datetime,
        message_id: int | None,
    ) -> bool:
        manager = db.execute(
            """SELECT telegram_date,message_id FROM (
                   SELECT telegram_date,message_id FROM business_messages
                    WHERE chat_id=? AND sender_type='manager'
                      AND telegram_date IS NOT NULL
                   UNION ALL
                   SELECT telegram_date,message_id FROM business_manager_fences
                    WHERE chat_id=? AND telegram_date IS NOT NULL
               ) ORDER BY julianday(telegram_date) DESC,message_id DESC LIMIT 1""",
            (chat_id, chat_id),
        ).fetchone()
        if not manager:
            return False
        try:
            manager_at = datetime.fromisoformat(manager["telegram_date"])
        except (TypeError, ValueError):
            return False
        if manager_at > client_at:
            return True
        if manager_at < client_at:
            return False
        return message_id is None or int(manager["message_id"]) >= int(message_id)

    def manager_replied_after(
        self, chat_id: str, event_at: datetime, message_id: int | None = None
    ) -> bool:
        """Return whether a persisted manual answer covers this client event."""
        with connect(self.path) as db:
            return self._manager_covers_client(db, chat_id, event_at, message_id)

    def manager_fence_active(
        self, chat_id: str, now: datetime, lock_minutes: int = 120
    ) -> bool:
        """Honor a received manual reply before its queued update is processed."""
        with connect(self.path) as db:
            row = db.execute(
                """SELECT telegram_date,message_id FROM business_manager_fences
                   WHERE chat_id=? ORDER BY julianday(telegram_date) DESC,
                   message_id DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
            if not row:
                return False
            try:
                manager_at = datetime.fromisoformat(row["telegram_date"])
            except (TypeError, ValueError):
                return False
            return (
                self._manager_event_is_current(
                    db, chat_id, manager_at, int(row["message_id"])
                )
                and manager_at + timedelta(minutes=max(1, int(lock_minutes))) > now
            )

    def manager_answer(self, chat_id: str, now: datetime, lock_minutes: int,
                       event_at: datetime | None = None, message_id: int | None = None,
                       session_id: str | None = None, manager_start: time = time(10),
                       manager_end: time = time(20), workdays=frozenset(range(7))) -> None:
        answer_at = event_at or now
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            manager_is_current = self._manager_event_is_current(
                db, chat_id, answer_at, message_id
            )
            def manager_covers_message(row: sqlite3.Row) -> bool:
                try:
                    client_at = datetime.fromisoformat(row["telegram_date"])
                except (TypeError, ValueError):
                    return False
                if client_at < answer_at:
                    return True
                if client_at > answer_at:
                    return False
                return message_id is None or int(row["message_id"]) <= int(message_id)

            # Webhooks are not guaranteed to be processed in Telegram event
            # order. A waiting cycle may therefore already contain a client
            # message that happened after this manager answer. Split that tail
            # into a fresh waiting cycle instead of losing either chronology.
            cycle = None
            covered_messages: list[sqlite3.Row] = []
            later_messages: list[sqlite3.Row] = []
            candidates = db.execute(
                """SELECT * FROM response_cycles WHERE chat_id=?
                   AND status='waiting_manager'
                   AND julianday(first_client_at)<=julianday(?)
                   ORDER BY julianday(first_client_at),cycle_id""",
                (chat_id, iso(answer_at)),
            ).fetchall()
            for candidate in candidates:
                client_messages = db.execute(
                    """SELECT id,session_id,telegram_date,message_id
                       FROM business_messages WHERE cycle_id=?
                       AND sender_type='client' AND telegram_date IS NOT NULL
                       ORDER BY julianday(telegram_date),message_id,id""",
                    (candidate["cycle_id"],),
                ).fetchall()
                if client_messages:
                    covered = [row for row in client_messages if manager_covers_message(row)]
                    if not covered:
                        continue
                    cycle = candidate
                    covered_messages = covered
                    later_messages = [
                        row for row in client_messages if not manager_covers_message(row)
                    ]
                    break
                # Compatibility for legacy/direct callers where response cycle
                # timestamps exist but the individual message row does not.
                if datetime.fromisoformat(candidate["last_client_at"]) <= answer_at:
                    cycle = candidate
                    break

            cycle_id = cycle["cycle_id"] if cycle else None
            successor_cycle_id: str | None = None
            successor_sessions: set[str] = set()
            if cycle:
                if covered_messages:
                    covered_times = [
                        datetime.fromisoformat(row["telegram_date"])
                        for row in covered_messages
                    ]
                    first, last = min(covered_times), max(covered_times)
                else:
                    first = datetime.fromisoformat(cycle["first_client_at"])
                    last = datetime.fromisoformat(cycle["last_client_at"])
                calendar = max(0, int((answer_at - first).total_seconds()))
                working = work_seconds(first, answer_at, manager_start, manager_end, workdays) if answer_at >= first else 0
                db.execute("""UPDATE response_cycles SET first_client_at=?,last_client_at=?,
                    first_manager_at=?,calendar_response_seconds=?,
                    work_response_seconds=?,needs_manager_reply=0,closed_at=?,status='manager_answered' WHERE cycle_id=?""",
                    (iso(first), iso(last), iso(answer_at), calendar, working,
                     iso(answer_at), cycle_id))
                if later_messages:
                    successor_cycle_id = uuid.uuid4().hex
                    later_times = [
                        datetime.fromisoformat(row["telegram_date"])
                        for row in later_messages
                    ]
                    successor_first, successor_last = min(later_times), max(later_times)
                    successor_session = next(
                        (row["session_id"] for row in later_messages if row["session_id"]),
                        cycle["session_id"],
                    )
                    if successor_session:
                        successor_sessions.add(successor_session)
                    successor_sessions.update(
                        row["session_id"] for row in later_messages if row["session_id"]
                    )
                    db.execute(
                        """INSERT INTO response_cycles(
                           cycle_id,chat_id,session_id,first_client_at,last_client_at,
                           manager_due_at) VALUES(?,?,?,?,?,?)""",
                        (
                            successor_cycle_id,
                            chat_id,
                            successor_session,
                            iso(successor_first),
                            iso(successor_last),
                            iso(manager_due_at(
                                successor_first, manager_start, manager_end, workdays,
                            )),
                        ),
                    )
                    for row in later_messages:
                        db.execute(
                            "UPDATE business_messages SET cycle_id=? WHERE id=?",
                            (successor_cycle_id, row["id"]),
                        )
                        self._refresh_message_outbox(db, row["id"], now, "stored")
            lock_until = iso(answer_at + timedelta(minutes=lock_minutes))
            # A manager update can legitimately be processed before a delayed
            # original client update.  Persist the lock even when no client row
            # exists yet; upsert_client() will later fill the identity fields.
            # Every genuine manual answer starts the configured quiet period
            # from its Telegram event time. Even when a newer client webhook was
            # processed first, that newer event occurred inside this lock and
            # must not resume automation after a delayed worker restart.
            db.execute(
                """INSERT INTO business_clients(chat_id,manager_lock_until,created_at,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                   manager_lock_until=CASE
                     WHEN business_clients.manager_lock_until IS NULL
                       OR julianday(business_clients.manager_lock_until)<julianday(excluded.manager_lock_until)
                     THEN excluded.manager_lock_until
                     ELSE business_clients.manager_lock_until END,
                   updated_at=excluded.updated_at""",
                (chat_id, lock_until, iso(now), iso(now)),
            )
            affected_sessions: set[str] = set()
            if cycle:
                if cycle["session_id"]:
                    affected_sessions.add(cycle["session_id"])
                affected_sessions.update(
                    row["session_id"]
                    for row in db.execute(
                        """SELECT DISTINCT session_id FROM business_messages
                           WHERE cycle_id=? AND session_id IS NOT NULL""",
                        (cycle_id,),
                    ).fetchall()
                )
            elif session_id and manager_is_current:
                affected_sessions.add(session_id)
            # A manual answer resolves the session-scoped handoff. Keep the
            # reason/priority as audit data, but permit a future response cycle
            # after manager_lock expires. Permanent client bot_paused is a
            # separate flag and is deliberately never changed here.
            for affected_session in affected_sessions:
                db.execute(
                    """UPDATE business_sessions SET automation_handoff=0,
                       search_disabled=0,failed_searches=0,updated_at=?
                       WHERE session_id=?""",
                    (iso(now), affected_session),
                )
            safe_sessions: set[str] = set()
            for affected_session in affected_sessions:
                if manager_is_current:
                    safe_sessions.add(affected_session)
                    continue
                params: list[Any] = [affected_session, iso(answer_at)]
                newer_sql = """SELECT 1 FROM business_messages WHERE session_id=?
                    AND sender_type='client' AND (
                      julianday(telegram_date)>julianday(?)"""
                if message_id is not None:
                    newer_sql += " OR (julianday(telegram_date)=julianday(?) AND message_id>?)"
                    params.extend((iso(answer_at), int(message_id)))
                newer_sql += ") LIMIT 1"
                if db.execute(newer_sql, params).fetchone() is None:
                    safe_sessions.add(affected_session)
            for affected_session in safe_sessions:
                db.execute(
                    """UPDATE business_sessions SET needs_manager_reply=0,status='manager_answered',
                       updated_at=? WHERE session_id=?""",
                    (iso(now), affected_session),
                )
            for successor_session in successor_sessions:
                db.execute(
                    """UPDATE business_sessions SET needs_manager_reply=1,
                       status='waiting_manager',updated_at=? WHERE session_id=?""",
                    (iso(now), successor_session),
                )
            if manager_is_current:
                db.execute("""UPDATE scheduled_actions SET status='cancelled',generation=generation+1,
                    lease_token=NULL,lease_expires_at=NULL,executed_at=? WHERE chat_id=? AND status IN ('pending','running')""",
                    (iso(now), chat_id))
            else:
                # The answer can still close an older, fully covered cycle, but it
                # must not cancel automation for a newer client event.  Cancelling
                # an older session remains safe only when that session itself has
                # no client message after the manual answer.
                for affected_session in safe_sessions:
                    db.execute(
                        """UPDATE scheduled_actions SET status='cancelled',
                           generation=generation+1,lease_token=NULL,
                           lease_expires_at=NULL,executed_at=? WHERE session_id=?
                           AND status IN ('pending','running')""",
                        (iso(now), affected_session),
                    )
            if cycle_id:
                if message_id is None:
                    rows = db.execute("SELECT id FROM business_messages WHERE chat_id=? AND sender_type='manager' AND cycle_id IS NULL ORDER BY telegram_date DESC LIMIT 1", (chat_id,)).fetchall()
                else:
                    rows = db.execute("SELECT id FROM business_messages WHERE chat_id=? AND message_id=? AND sender_type='manager'", (chat_id, message_id)).fetchall()
                for row in rows:
                    db.execute("UPDATE business_messages SET cycle_id=? WHERE id=?", (cycle_id, row["id"]))
                    self._refresh_message_outbox(db, row["id"], now, "stored")
                for affected_session in affected_sessions:
                    self._queue_dialog_outbox(db, affected_session, now, cycle_id)
                if successor_cycle_id:
                    for successor_session in successor_sessions:
                        self._queue_dialog_outbox(
                            db, successor_session, now, successor_cycle_id,
                        )
            # Request state and callback buttons are fenced in the same SQLite
            # transaction as the response-cycle closure. A process crash cannot
            # leave a submitted/collecting request active after its manager reply.
            self._close_business_requests_for_manager_in_db(
                db,
                chat_id,
                answer_at,
                now,
                manager_message_id=message_id,
                cycle_id=cycle_id,
            )

    # Durable scheduled actions use generation + lease fencing.
    @staticmethod
    def _debounce_payload_bounds(
        payload: str | dict[str, Any], fallback: datetime
    ) -> tuple[datetime, datetime, dict[str, Any]]:
        try:
            values = json.loads(payload) if isinstance(payload, str) else dict(payload)
            if not isinstance(values, dict):
                values = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            values = {}

        def parsed(name: str, default: datetime) -> datetime:
            try:
                value = datetime.fromisoformat(str(values.get(name) or ""))
                if value.tzinfo is None and default.tzinfo is not None:
                    value = value.replace(tzinfo=default.tzinfo)
                return value
            except (TypeError, ValueError):
                return default

        last = parsed("event_at", fallback)
        first = parsed("burst_start_at", last)
        return min(first, last), max(first, last), values

    def schedule_debounce(
        self,
        chat_id: str,
        session_id: str,
        event_at: datetime,
        execute_at: datetime,
        payload: dict[str, Any],
        now: datetime,
        debounce_seconds: int,
    ) -> str:
        """Atomically extend one Telegram-time burst or create a distinct one.

        Processing time is deliberately not used for grouping: after a restart a
        ten-minute backlog can be drained in milliseconds, but its messages must
        still produce separate, chronologically ordered debounce actions.
        """
        gap = timedelta(seconds=max(1, int(debounce_seconds)))
        incoming = dict(payload)
        incoming["event_at"] = iso(event_at)
        incoming.setdefault("burst_start_at", iso(event_at))
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            edit_update_id = incoming.get("edit_update_id")
            if edit_update_id is not None:
                # The update worker may restart after persisting the revision or
                # after creating/executing its action. A revision marker in any
                # durable action state is an exact idempotency receipt: a retry
                # must neither create a second action nor resurrect a terminal
                # one cancelled by a manager fence.
                revision_actions = db.execute(
                    """SELECT dedupe_key,payload FROM scheduled_actions
                       WHERE chat_id=? AND session_id=? AND action_type='debounce'""",
                    (chat_id, session_id),
                ).fetchall()
                for action in revision_actions:
                    try:
                        saved_payload = json.loads(action["payload"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if str(saved_payload.get("edit_update_id")) == str(edit_update_id):
                        return action["dedupe_key"]
            active = db.execute(
                """SELECT * FROM scheduled_actions WHERE chat_id=? AND session_id=?
                   AND action_type='debounce' AND status IN ('pending','running')""",
                (chat_id, session_id),
            ).fetchall()

            def fence_running_later(
                first: datetime, last: datetime, action_id: int
            ) -> None:
                current_order = (first, last, action_id)
                running = db.execute(
                    """SELECT * FROM scheduled_actions WHERE chat_id=? AND session_id=?
                       AND action_type='debounce' AND status='running'""",
                    (chat_id, session_id),
                ).fetchall()
                for later in running:
                    later_first, later_last, _ = self._debounce_payload_bounds(
                        later["payload"], event_at
                    )
                    if (later_first, later_last, int(later["action_id"])) <= current_order:
                        continue
                    # A delayed older update can arrive while a newer burst is
                    # already being classified in another worker. Requeue and
                    # invalidate that worker so its next may_automate/send fence
                    # observes the older burst first.
                    db.execute(
                        """UPDATE scheduled_actions SET status='pending',
                           generation=generation+1,lease_token=NULL,
                           lease_expires_at=NULL WHERE action_id=? AND status='running'""",
                        (later["action_id"],),
                    )
            candidates: list[tuple[sqlite3.Row, datetime, datetime, dict[str, Any]]] = []
            for row in active:
                first, last, values = self._debounce_payload_bounds(
                    row["payload"], event_at
                )
                if first - gap <= event_at <= last + gap:
                    candidates.append((row, first, last, values))

            if candidates:
                candidates.sort(key=lambda item: (item[1], item[0]["action_id"]))
                canonical, _, _, canonical_payload = candidates[0]
                first = min([event_at, *(item[1] for item in candidates)])
                last = max([event_at, *(item[2] for item in candidates)])
                latest_payload = max(candidates, key=lambda item: item[2])[3]
                merged = dict(canonical_payload)
                merged.update(latest_payload)
                if event_at >= max(item[2] for item in candidates):
                    merged.update(incoming)
                merged["burst_start_at"] = iso(first)
                merged["event_at"] = iso(last)
                key = canonical["dedupe_key"]
                db.execute(
                    """UPDATE scheduled_actions SET execute_at=?,next_attempt_at=?,payload=?,
                       status='pending',attempts=0,created_at=?,executed_at=NULL,last_error=NULL,
                       generation=generation+1,lease_token=NULL,lease_expires_at=NULL
                       WHERE action_id=?""",
                    (iso(execute_at), iso(execute_at), json.dumps(merged, ensure_ascii=False),
                     iso(now), canonical["action_id"]),
                )
                for duplicate, *_ in candidates[1:]:
                    db.execute(
                        """UPDATE scheduled_actions SET status='cancelled',
                           generation=generation+1,lease_token=NULL,lease_expires_at=NULL,
                           executed_at=? WHERE action_id=?""",
                        (iso(now), duplicate["action_id"]),
                    )
                fence_running_later(first, last, int(canonical["action_id"]))
                return key

            discriminator = (
                f"edit-{edit_update_id}"
                if edit_update_id is not None
                else incoming.get("message_id") or incoming.get("update_id")
            )
            if discriminator is None:
                discriminator = int(event_at.timestamp() * 1_000_000)
            key = f"debounce:{chat_id}:{session_id}:{discriminator}"
            db.execute(
                """INSERT INTO scheduled_actions(dedupe_key,chat_id,session_id,action_type,
                   execute_at,next_attempt_at,payload,created_at) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(dedupe_key) DO UPDATE SET execute_at=excluded.execute_at,
                   next_attempt_at=excluded.next_attempt_at,payload=excluded.payload,
                   created_at=excluded.created_at,status=CASE
                     WHEN scheduled_actions.status='done' THEN scheduled_actions.status
                     ELSE 'pending' END,
                   attempts=CASE WHEN scheduled_actions.status='done'
                     THEN scheduled_actions.attempts ELSE 0 END,
                   executed_at=CASE WHEN scheduled_actions.status='done'
                     THEN scheduled_actions.executed_at ELSE NULL END,
                   last_error=CASE WHEN scheduled_actions.status='done'
                     THEN scheduled_actions.last_error ELSE NULL END,
                   generation=CASE WHEN scheduled_actions.status='done'
                     THEN scheduled_actions.generation ELSE scheduled_actions.generation+1 END,
                   lease_token=NULL,lease_expires_at=NULL""",
                (key, chat_id, session_id, "debounce", iso(execute_at), iso(execute_at),
                 json.dumps(incoming, ensure_ascii=False), iso(now)),
            )
            saved = db.execute(
                "SELECT action_id FROM scheduled_actions WHERE dedupe_key=?", (key,)
            ).fetchone()
            fence_running_later(event_at, event_at, int(saved["action_id"]))
            return key

    def schedule(self, key: str, chat_id: str, session_id: str | None, action_type: str,
                 execute_at: datetime, payload: dict, now: datetime) -> None:
        with connect(self.path) as db:
            db.execute("""INSERT INTO scheduled_actions(dedupe_key,chat_id,session_id,action_type,
                execute_at,next_attempt_at,payload,created_at) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET chat_id=excluded.chat_id,session_id=excluded.session_id,
                action_type=excluded.action_type,execute_at=excluded.execute_at,next_attempt_at=excluded.next_attempt_at,
                payload=excluded.payload,created_at=excluded.created_at,status='pending',attempts=0,
                executed_at=NULL,last_error=NULL,
                generation=scheduled_actions.generation+1,lease_token=NULL,lease_expires_at=NULL""",
                (key, chat_id, session_id, action_type, iso(execute_at), iso(execute_at),
                 json.dumps(payload, ensure_ascii=False), iso(now)))

    def due_actions(self, now: datetime, limit: int = 50):
        with connect(self.path) as db:
            return db.execute("""SELECT * FROM scheduled_actions WHERE status='pending'
                              AND julianday(execute_at)<=julianday(?)
                              AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
                              ORDER BY execute_at,action_id LIMIT ?""",
                              (iso(now), iso(now), limit)).fetchall()

    def claim_action(self, action_id: int, now: datetime | None = None, lease_seconds: int = 60,
                     expected_generation: int | None = None) -> bool:
        now, token = now or datetime.now(timezone.utc), uuid.uuid4().hex
        sql = "UPDATE scheduled_actions SET status='running',attempts=attempts+1,lease_token=?,lease_expires_at=? WHERE action_id=? AND status='pending'"
        params: list[Any] = [token, iso(now + timedelta(seconds=lease_seconds)), action_id]
        if expected_generation is not None:
            sql += " AND generation=?"; params.append(expected_generation)
        with connect(self.path) as db:
            return db.execute(sql, params).rowcount == 1

    def claim_due_actions(self, now: datetime, limit: int = 50, lease_seconds: int = 60) -> list[sqlite3.Row]:
        claimed: list[sqlite3.Row] = []
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE"); self._recover_stale_actions(db, now)
            rows = db.execute("""SELECT * FROM scheduled_actions
                              WHERE status='pending' AND julianday(execute_at)<=julianday(?)
                              AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
                              ORDER BY execute_at,action_id""",
                              (iso(now), iso(now))).fetchall()
            active_debounces = db.execute(
                """SELECT * FROM scheduled_actions WHERE action_type='debounce'
                   AND status IN ('pending','running')"""
            ).fetchall()

            def debounce_order(row: sqlite3.Row) -> tuple[datetime, datetime, int]:
                first, last, _ = self._debounce_payload_bounds(row["payload"], now)
                return first, last, int(row["action_id"])

            for row in rows:
                if len(claimed) >= limit:
                    break
                if row["action_type"] == "debounce":
                    order = debounce_order(row)
                    if any(
                        other["action_id"] != row["action_id"]
                        and other["chat_id"] == row["chat_id"]
                        and other["session_id"] == row["session_id"]
                        and debounce_order(other) < order
                        for other in active_debounces
                    ):
                        # A later burst is not allowed to overtake an older retry
                        # or a currently running handoff classification.
                        continue
                token = uuid.uuid4().hex
                changed = db.execute("UPDATE scheduled_actions SET status='running',attempts=attempts+1,lease_token=?,lease_expires_at=? WHERE action_id=? AND status='pending' AND generation=?",
                                     (token, iso(now + timedelta(seconds=lease_seconds)), row["action_id"], row["generation"])).rowcount
                if changed:
                    claimed.append(db.execute("SELECT * FROM scheduled_actions WHERE action_id=?", (row["action_id"],)).fetchone())
        return claimed

    def action_is_current(
        self, action_id: int, lease_token: str, generation: int,
        now: datetime | None = None,
    ) -> bool:
        sql = """SELECT 1 FROM scheduled_actions WHERE action_id=? AND status='running'
                 AND lease_token=? AND generation=?"""
        params: list[Any] = [action_id, lease_token, generation]
        if now is not None:
            sql += " AND julianday(lease_expires_at)>julianday(?)"
            params.append(iso(now))
        with connect(self.path) as db:
            return db.execute(sql, params).fetchone() is not None

    def finish_action(self, action_id: int, now: datetime, error: str | None = None, *,
                      lease_token: str | None = None, generation: int | None = None,
                      retry_after: float | None = None, max_attempts: int = 8) -> bool:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scheduled_actions WHERE action_id=?", (action_id,)).fetchone()
            if not row or row["status"] != "running" or (lease_token is not None and row["lease_token"] != lease_token) or (generation is not None and row["generation"] != generation):
                return False
            if not error:
                db.execute("UPDATE scheduled_actions SET status='done',executed_at=?,last_error=NULL,lease_token=NULL,lease_expires_at=NULL WHERE action_id=?", (iso(now), action_id))
                return True
            safe_error = (redact_sensitive_data(error) or "scheduled action failed")[:500]
            if row["attempts"] >= max_attempts:
                db.execute("UPDATE scheduled_actions SET status='failed',executed_at=?,last_error=?,lease_token=NULL,lease_expires_at=NULL WHERE action_id=?",
                           (iso(now), safe_error, action_id)); return True
            delay = max(_backoff_seconds(row["attempts"]), float(retry_after or 0)); retry_at = now + timedelta(seconds=delay)
            db.execute("UPDATE scheduled_actions SET status='pending',execute_at=?,next_attempt_at=?,last_error=?,lease_token=NULL,lease_expires_at=NULL WHERE action_id=?",
                       (iso(retry_at), iso(retry_at), safe_error, action_id)); return True

    def _recover_stale_actions(self, db: sqlite3.Connection, now: datetime) -> int:
        stale = """status='running' AND (
            lease_expires_at IS NULL OR julianday(lease_expires_at) IS NULL
            OR julianday(lease_expires_at)<=julianday(?))"""
        if db.execute(
            f"SELECT 1 FROM scheduled_actions WHERE {stale} LIMIT 1", (iso(now),)
        ).fetchone() is None:
            return 0
        self._repair_sent_state(db, now)
        # If Telegram returned a message and it reached the local ledger before a
        # crash, final/credit actions are complete. Repair their flags and do not
        # send them a second time after restart.
        completed = db.execute(
            f"""UPDATE scheduled_actions SET status='done',generation=generation+1,
                executed_at=?,lease_token=NULL,lease_expires_at=NULL,last_error=NULL
                WHERE {stale} AND (
                  (action_type='final' AND EXISTS(
                    SELECT 1 FROM business_messages m WHERE m.session_id=scheduled_actions.session_id
                    AND m.sender_type='business_bot' AND m.template_code='final'))
                  OR (action_type='credit' AND EXISTS(
                    SELECT 1 FROM business_messages m WHERE m.chat_id=scheduled_actions.chat_id
                    AND m.sender_type='business_bot' AND m.template_code='credit'
                    AND julianday(m.created_at)>=julianday(scheduled_actions.created_at)))
                )""",
            (iso(now), iso(now)),
        )
        requeued = db.execute(f"""UPDATE scheduled_actions SET status='pending',generation=generation+1,
            execute_at=?,next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,
            last_error=COALESCE(last_error,'worker lease expired') WHERE {stale}""",
            (iso(now), iso(now), iso(now))).rowcount
        return completed.rowcount + requeued

    def _repair_sent_state(self, db: sqlite3.Connection, now: datetime) -> None:
        db.execute("""UPDATE business_sessions SET greeting_sent=1 WHERE EXISTS(
            SELECT 1 FROM business_messages m WHERE m.session_id=business_sessions.session_id
            AND m.sender_type='business_bot'
            AND m.template_code IN ('greeting_model','greeting_no_model'))""")
        db.execute("""UPDATE business_sessions SET price_sent=1 WHERE EXISTS(
            SELECT 1 FROM business_messages m WHERE m.session_id=business_sessions.session_id
            AND m.sender_type='business_bot' AND m.template_code='product_result')""")
        db.execute("""UPDATE business_sessions SET final_sent=1 WHERE EXISTS(
            SELECT 1 FROM business_messages m WHERE m.session_id=business_sessions.session_id
            AND m.sender_type='business_bot' AND m.template_code='final')""")
        searches = db.execute("""SELECT session_id,model_query,created_at FROM business_messages
            WHERE sender_type='business_bot' AND template_code='product_result'
            AND model_query IS NOT NULL ORDER BY created_at""").fetchall()
        for row in searches:
            db.execute("""UPDATE business_sessions SET last_search_hash=?,last_search_at=?
                       WHERE session_id=? AND (last_search_at IS NULL
                       OR julianday(last_search_at)<=julianday(?))""",
                       (self._search_hash(row["model_query"]), row["created_at"],
                        row["session_id"], row["created_at"]))
        credits = db.execute("""SELECT chat_id,max(created_at) AS sent_at FROM business_messages
            WHERE sender_type='business_bot' AND template_code='credit' GROUP BY chat_id""").fetchall()
        for row in credits:
            db.execute("""UPDATE business_clients SET last_credit_reply_at=? WHERE chat_id=?
                       AND (last_credit_reply_at IS NULL
                       OR julianday(last_credit_reply_at)<julianday(?))""",
                       (row["sent_at"], row["chat_id"], row["sent_at"]))

    def cancel(self, key: str) -> None:
        with connect(self.path) as db:
            db.execute("UPDATE scheduled_actions SET status='cancelled',generation=generation+1,lease_token=NULL,lease_expires_at=NULL WHERE dedupe_key=? AND status IN ('pending','running')", (key,))

    def cancel_chat_actions(self, chat_id: str, now: datetime) -> int:
        with connect(self.path) as db:
            return db.execute("UPDATE scheduled_actions SET status='cancelled',generation=generation+1,lease_token=NULL,lease_expires_at=NULL,executed_at=? WHERE chat_id=? AND status IN ('pending','running')",
                              (iso(now), chat_id)).rowcount

    # Automation state and audited anti-spam hooks.
    def update_language(self, chat_id: str, language: str, confidence: float, now: datetime) -> None:
        with connect(self.path) as db:
            db.execute("UPDATE business_clients SET language=?,language_confidence=?,updated_at=? WHERE chat_id=?", (language, confidence, iso(now), chat_id))

    def patch_session(self, session_id: str, now: datetime, **fields) -> None:
        allowed = {"greeting_sent", "price_sent", "final_sent", "location_received", "order_intent",
                   "credit_intent", "needs_manager_reply", "priority", "handoff_reason", "model_query",
                   "matched_model", "memory", "color", "location_url", "preferred_time", "failed_searches",
                   "search_disabled", "automation_handoff", "last_search_hash", "last_search_at", "status"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values: return
        sql = ",".join(f"{key}=?" for key in values) + ",updated_at=?"
        with connect(self.path) as db:
            db.execute(f"UPDATE business_sessions SET {sql} WHERE session_id=?", (*values.values(), iso(now), session_id))
            self._queue_dialog_outbox(db, session_id, now)

    def may_automate(self, chat_id: str, now: datetime) -> bool:
        claim = _ACTION_CLAIM.get()
        if claim and claim[0] == self._identity and not self.action_is_current(
            claim[1], claim[2], claim[3], now
        ):
            return False
        row = self.client(chat_id)
        if not row or row["bot_paused"]: return False
        if row["manager_lock_until"] and datetime.fromisoformat(row["manager_lock_until"]) > now: return False
        return bool(row["last_client_message_at"] and datetime.fromisoformat(row["last_client_message_at"]) >= now - timedelta(hours=24))

    def manager_lock_covers_event(
        self,
        chat_id: str,
        event_at: datetime,
    ) -> bool:
        """Whether this inbound event occurred before the stored lock ended.

        Durable workers may process a webhook after the wall-clock lock has
        expired. Such a delayed event is not the new post-lock inbound message
        required to resume automation.
        """

        row = self.client(chat_id)
        if not row or not row["manager_lock_until"]:
            return False
        try:
            return datetime.fromisoformat(row["manager_lock_until"]) > event_at
        except (TypeError, ValueError):
            return True

    def set_bot_paused(
        self, chat_id: str, paused: bool, now: datetime, reason: str | None = None
    ) -> bool:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE business_clients SET bot_paused=?,pause_reason=?,updated_at=?
                   WHERE chat_id=?""",
                (int(paused), reason if paused else None, iso(now), chat_id),
            ).rowcount
            if paused:
                db.execute(
                    """UPDATE scheduled_actions SET status='cancelled',generation=generation+1,
                       lease_token=NULL,lease_expires_at=NULL,executed_at=?
                       WHERE chat_id=? AND status IN ('pending','running')""",
                    (iso(now), chat_id),
                )
                self._expire_requests_for_chat(
                    db,
                    chat_id,
                    now,
                    f"bot_paused:{str(reason or 'manual')[:60]}",
                )
            return changed == 1

    def is_bot_paused(self, chat_id: str) -> bool:
        row = self.client(chat_id)
        return bool(row and row["bot_paused"])

    def session_may_automate(self, session_id: str) -> bool:
        row = self.session_by_id(session_id)
        return bool(row and not row["automation_handoff"])

    def within_reply_window(self, chat_id: str, now: datetime, hours: int = 24) -> bool:
        row = self.client(chat_id)
        return bool(row and row["last_client_message_at"] and datetime.fromisoformat(row["last_client_message_at"]) >= now - timedelta(hours=hours))

    def stop_session_automation(self, session_id: str, now: datetime, reason: str, *, priority: bool = True) -> None:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE business_sessions SET automation_handoff=1,search_disabled=1,
                   needs_manager_reply=1,priority=?,handoff_reason=?,status='human_handoff',
                   updated_at=? WHERE session_id=?""",
                (int(priority), reason, iso(now), session_id),
            )
            db.execute(
                """UPDATE scheduled_actions SET status='cancelled',generation=generation+1,
                   lease_token=NULL,lease_expires_at=NULL,executed_at=?
                   WHERE session_id=? AND status IN ('pending','running')""",
                (iso(now), session_id),
            )
            requests = db.execute(
                """SELECT * FROM business_requests WHERE session_id=?
                   AND status IN ('collecting','ready')""",
                (session_id,),
            ).fetchall()
            for request in requests:
                new_revision = int(request["revision"]) + 1
                db.execute(
                    """UPDATE business_requests SET status='expired',
                       wizard_state='closed',revision=?,closed_at=?,
                       close_reason=?,needs_manager_reply=1,updated_at=?
                       WHERE request_id=? AND revision=?
                       AND status IN ('collecting','ready')""",
                    (
                        new_revision,
                        iso(now),
                        f"handoff:{str(reason)[:70]}",
                        iso(now),
                        request["request_id"],
                        int(request["revision"]),
                    ),
                )
                self._revoke_request_callbacks(
                    db, request["request_id"], now, "automation_handoff",
                )
                self._append_request_event(
                    db,
                    request["request_id"],
                    new_revision,
                    "expired",
                    "system",
                    now,
                    event_key=f"handoff:{new_revision}",
                    payload={"reason": str(reason)[:80]},
                )
                self._queue_request_outbox(db, request["request_id"], now)
            self._queue_dialog_outbox(db, session_id, now)

    def credit_allowed(self, chat_id: str, now: datetime, cooldown_minutes: int) -> bool:
        row = self.client(chat_id)
        if (
            row
            and row["last_credit_reply_at"]
            and datetime.fromisoformat(row["last_credit_reply_at"])
            + timedelta(minutes=cooldown_minutes)
            > now
        ):
            return False
        with connect(self.path) as db:
            outbound = db.execute(
                """SELECT created_at FROM business_outbound_deliveries
                   WHERE chat_id=? AND template_code='credit'
                   AND state IN ('sending','sent','uncertain')
                   ORDER BY julianday(created_at) DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
        if not outbound:
            return True
        try:
            attempted_at = datetime.fromisoformat(outbound["created_at"])
        except (TypeError, ValueError):
            return False
        return attempted_at + timedelta(minutes=cooldown_minutes) <= now

    def mark_credit(self, chat_id: str, now: datetime) -> None:
        with connect(self.path) as db:
            db.execute("UPDATE business_clients SET last_credit_reply_at=?,updated_at=? WHERE chat_id=?", (iso(now), iso(now), chat_id))

    def acknowledgement_allowed(self, chat_id: str, now: datetime, cooldown_seconds: int = 30) -> bool:
        row = self.client(chat_id)
        return not row or not row["last_ack_at"] or datetime.fromisoformat(row["last_ack_at"]) + timedelta(seconds=cooldown_seconds) <= now

    def mark_acknowledgement(self, chat_id: str, now: datetime) -> None:
        with connect(self.path) as db:
            db.execute("UPDATE business_clients SET last_ack_at=?,updated_at=? WHERE chat_id=?", (iso(now), iso(now), chat_id))

    @staticmethod
    def _search_hash(result_key: str) -> str:
        return hashlib.sha256(result_key.strip().casefold().encode()).hexdigest()

    def search_was_recent(self, session_id: str, result_key: str, now: datetime, cooldown_minutes: int = 10) -> bool:
        row = self.session_by_id(session_id)
        return bool(row and row["last_search_hash"] == self._search_hash(result_key) and row["last_search_at"]
                    and datetime.fromisoformat(row["last_search_at"]) + timedelta(minutes=cooldown_minutes) > now)

    def mark_search_result(self, session_id: str, result_key: str, now: datetime) -> None:
        self.patch_session(session_id, now, last_search_hash=self._search_hash(result_key), last_search_at=iso(now))

    def recent_bot_template(self, chat_id: str, template_code: str, now: datetime,
                            cooldown_seconds: int, model_query: str | None = None) -> bool:
        cutoff = iso(now - timedelta(seconds=cooldown_seconds))
        sql = "SELECT 1 FROM business_messages WHERE chat_id=? AND sender_type='business_bot' AND template_code=? AND created_at>=?"
        params: list[Any] = [chat_id, template_code, cutoff]
        if model_query is not None:
            sql += " AND model_query=?"; params.append(model_query)
        sql += " LIMIT 1"
        with connect(self.path) as db:
            return db.execute(sql, params).fetchone() is not None

    def bot_message_count(self, chat_id: str, session_id: str, now: datetime) -> tuple[int, int]:
        with connect(self.path) as db:
            ten = db.execute("SELECT count(*) FROM business_messages WHERE chat_id=? AND sender_type='business_bot' AND created_at>=?", (chat_id, iso(now - timedelta(minutes=10)))).fetchone()[0]
            session = db.execute("SELECT count(*) FROM business_messages WHERE session_id=? AND sender_type='business_bot'", (session_id,)).fetchone()[0]
            return ten, session

    def begin_outbound_delivery(
        self,
        dedupe_key: str,
        chat_id: str,
        session_id: str,
        template_code: str,
        content_hash: str,
        now: datetime,
    ) -> str:
        """Return ``send``, ``assumed`` or ``blocked`` for a critical reply.

        A leftover ``sending`` row is deliberately treated as assumed delivery:
        Telegram has no idempotency key for sendMessage, so retrying that
        ambiguous request could create a duplicate final/credit answer.
        """
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM business_outbound_deliveries WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO business_outbound_deliveries(
                       dedupe_key,chat_id,session_id,template_code,content_hash,
                       state,created_at,updated_at) VALUES(?,?,?,?,?,'sending',?,?)""",
                    (
                        dedupe_key,
                        chat_id,
                        session_id,
                        template_code,
                        content_hash,
                        iso(now),
                        iso(now),
                    ),
                )
                return "send"
            if row["state"] == "retry":
                db.execute(
                    """UPDATE business_outbound_deliveries SET state='sending',
                       attempts=attempts+1,last_error=NULL,updated_at=?
                       WHERE dedupe_key=? AND state='retry'""",
                    (iso(now), dedupe_key),
                )
                return "send"
            if row["state"] in {"sending", "sent", "uncertain"}:
                return "assumed"
            return "blocked"

    def outbound_delivery(self, dedupe_key: str):
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM business_outbound_deliveries
                   WHERE dedupe_key=?""",
                (dedupe_key,),
            ).fetchone()

    def finish_outbound_delivery(
        self,
        dedupe_key: str,
        now: datetime,
        *,
        telegram_message_id: int | None = None,
        error: Exception | str | None = None,
        safe_to_retry: bool = False,
        ambiguous: bool = False,
    ) -> bool:
        if error is None:
            state = "sent"
        elif ambiguous:
            state = "uncertain"
        elif safe_to_retry:
            state = "retry"
        else:
            state = "failed"
        safe_error = (redact_sensitive_data(str(error)) or "")[:500] or None
        with connect(self.path) as db:
            return db.execute(
                """UPDATE business_outbound_deliveries SET state=?,
                   telegram_message_id=COALESCE(?,telegram_message_id),
                   last_error=?,updated_at=? WHERE dedupe_key=?
                   AND state='sending'""",
                (
                    state,
                    telegram_message_id,
                    safe_error,
                    iso(now),
                    dedupe_key,
                ),
            ).rowcount == 1

    def record_bot_message(self, connection_id: str, chat_id: str, session_id: str, message_id: int,
                           text: str, template_code: str, now: datetime, model_query: str | None = None) -> None:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            # Bind the reply to the cycle that existed for this session when
            # sendMessage started. A newer client cycle may be opened while the
            # network request is in flight and must not inherit the older bot
            # response.
            cycle = db.execute(
                """SELECT cycle_id,first_client_at,first_bot_at
                   FROM response_cycles WHERE chat_id=? AND session_id=?
                   AND julianday(first_client_at)<=julianday(?)
                   ORDER BY julianday(first_client_at) DESC LIMIT 1""",
                (chat_id, session_id, iso(now)),
            ).fetchone()
            stored_message_id = message_id
            existing = db.execute(
                """SELECT * FROM business_messages WHERE business_connection_id=?
                   AND chat_id=? AND message_id=?""",
                (connection_id, chat_id, message_id),
            ).fetchone()
            if existing and existing["sender_type"] != "business_bot":
                stored_message_id = self._surrogate_message_id(
                    db, connection_id, chat_id, message_id
                )
            db.execute("""INSERT OR IGNORE INTO business_messages(business_connection_id,chat_id,message_id,
                session_id,cycle_id,direction,sender_type,message_type,text,template_code,model_query,telegram_date,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (connection_id, chat_id, stored_message_id, session_id,
                cycle["cycle_id"] if cycle else None, "outgoing", "business_bot", "text",
                redact_payment_data(text), template_code, model_query, iso(now), iso(now)))
            row = db.execute(
                """SELECT * FROM business_messages WHERE business_connection_id=?
                   AND chat_id=? AND message_id=?""",
                (connection_id, chat_id, stored_message_id),
            ).fetchone()
            if not row or row["sender_type"] != "business_bot":
                return

            # The outgoing Business webhook can win the race with the local
            # sendMessage continuation. In that order INSERT OR IGNORE alone
            # leaves the row without the template/cycle metadata needed for
            # cooldowns, restart recovery and Sheets. Enrich the same natural-key
            # row while preserving its Telegram update_id and event timestamp.
            db.execute(
                """UPDATE business_messages SET session_id=?,
                   cycle_id=COALESCE(cycle_id,?),direction='outgoing',
                   sender_type='business_bot',message_type='text',text=?,
                   template_code=?,model_query=COALESCE(?,model_query),
                   telegram_date=COALESCE(telegram_date,?) WHERE id=?""",
                (
                    session_id,
                    cycle["cycle_id"] if cycle else None,
                    redact_payment_data(text),
                    template_code,
                    model_query,
                    iso(now),
                    row["id"],
                ),
            )
            row = db.execute(
                "SELECT * FROM business_messages WHERE id=?", (row["id"],)
            ).fetchone()
            self._refresh_message_outbox(db, row["id"], now, "sent")

            actual_cycle = None
            if row["cycle_id"]:
                actual_cycle = db.execute(
                    """SELECT cycle_id,first_client_at,first_bot_at
                       FROM response_cycles WHERE cycle_id=?""",
                    (row["cycle_id"],),
                ).fetchone()
            if actual_cycle and not actual_cycle["first_bot_at"]:
                first = datetime.fromisoformat(actual_cycle["first_client_at"])
                try:
                    bot_at = datetime.fromisoformat(row["telegram_date"])
                except (TypeError, ValueError):
                    bot_at = now
                db.execute(
                    """UPDATE response_cycles SET first_bot_at=?,bot_response_seconds=?
                       WHERE cycle_id=? AND first_bot_at IS NULL""",
                    (
                        iso(bot_at),
                        max(0, int((bot_at - first).total_seconds())),
                        actual_cycle["cycle_id"],
                    ),
                )
                actual_cycle = db.execute(
                    """SELECT cycle_id,first_client_at,first_bot_at
                       FROM response_cycles WHERE cycle_id=?""",
                    (row["cycle_id"],),
                ).fetchone()
            if actual_cycle:
                self._queue_dialog_outbox(
                    db, row["session_id"] or session_id, now,
                    actual_cycle["cycle_id"],
                )

    # Durable request wizard state. request_id is an internal correlation UUID;
    # it is never an order number and must not be presented as one to clients.
    @staticmethod
    def _request_json(value: Any, *, max_bytes: int = 16_384) -> str:
        def safe(item: Any, depth: int = 0) -> Any:
            if depth > 4:
                raise ValueError("request payload is too deeply nested")
            if item is None or isinstance(item, (bool, int, float)):
                return item
            if isinstance(item, str):
                return (redact_payment_data(item) or "")[:2_000]
            if isinstance(item, (list, tuple)):
                if len(item) > 50:
                    raise ValueError("request payload list is too large")
                return [safe(child, depth + 1) for child in item]
            if isinstance(item, dict):
                if len(item) > 50:
                    raise ValueError("request payload object is too large")
                result: dict[str, Any] = {}
                for raw_key, child in item.items():
                    key = str(raw_key)
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", key):
                        raise ValueError("invalid request payload key")
                    result[key] = safe(child, depth + 1)
                return result
            raise ValueError("request payload contains an unsupported value")

        encoded = json.dumps(safe(value), ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError("request payload is too large")
        return encoded

    @staticmethod
    def _request_phone_masked(phone: str | None) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if not digits:
            return ""
        return f"***{digits[-4:]}"

    @staticmethod
    def _request_safe_location(value: str | None, *, url: bool = False) -> str:
        text = (redact_payment_data(value) or "").strip()[:500]
        if not text:
            return ""
        # A customer may paste contact details together with an address. Sheets
        # receives only the dedicated masked phone field, never another full
        # phone-shaped sequence hidden in the map/address text.
        text = re.sub(
            r"(?<!\d)(?:\+?\d[\s()\-]*){7,15}(?!\d)",
            "[PHONE_REDACTED]",
            text,
        )
        if url:
            match = re.match(r"^https://([^/]+)", text, flags=re.IGNORECASE)
            if not match:
                return ""
            host = match.group(1).split(":", 1)[0].casefold()
            allowed = (
                host == "goo.gl" or host == "maps.app.goo.gl"
                or host.endswith(".google.com") or host == "google.com"
                or host.startswith("yandex.") or ".yandex." in host
                or host == "2gis.uz" or host.endswith(".2gis.uz")
            )
            if not allowed:
                return ""

        # Sheets receives a useful map/address hint, not exact raw coordinates.
        def rounded(match: re.Match[str]) -> str:
            try:
                return f"{float(match.group(0)):.3f}"
            except ValueError:
                return "[coordinate]"

        return re.sub(r"(?<![\d.])-?\d{1,3}\.\d{4,}(?![\d.])", rounded, text)

    def _append_request_event(
        self,
        db: sqlite3.Connection,
        request_id: str,
        revision: int,
        event_type: str,
        actor_type: str,
        now: datetime,
        *,
        event_key: str | None = None,
        payload: dict[str, Any] | None = None,
        telegram_update_id: int | None = None,
        telegram_message_id: int | None = None,
    ) -> bool:
        try:
            return db.execute(
                """INSERT INTO business_request_events(
                   request_id,request_revision,event_type,actor_type,event_key,
                   payload,telegram_update_id,telegram_message_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    int(revision),
                    str(event_type)[:64],
                    str(actor_type)[:32],
                    str(event_key)[:160] if event_key else None,
                    self._request_json(payload or {}),
                    telegram_update_id,
                    telegram_message_id,
                    iso(now),
                ),
            ).rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def _queue_request_outbox(
        self, db: sqlite3.Connection, request_id: str, now: datetime
    ) -> None:
        row = db.execute(
            "SELECT * FROM business_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            return
        client = db.execute(
            "SELECT telegram_user_id FROM business_clients WHERE chat_id=?",
            (str(row["chat_id"]),),
        ).fetchone()
        try:
            selection_fields = json.loads(row["selection_fields"] or "{}")
            if not isinstance(selection_fields, dict):
                selection_fields = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            selection_fields = {}
        payload = {
            "request_id": row["request_id"],
            "session_id": row["session_id"] or "",
            "cycle_id": row["cycle_id"] or "",
            "business_date": row["business_date"] or "",
            "chat_id": str(row["chat_id"]),
            "telegram_user_id": str(client["telegram_user_id"] or "") if client else "",
            "language": row["language"] or "",
            "state": row["wizard_state"],
            "status": row["status"],
            "exact_model": row["exact_model"] or "",
            "model_query": selection_fields.get("model_query") or row["exact_model"] or "",
            "matched_model": row["exact_model"] or "",
            "option_kind": row["option_kind"] or "",
            "attribute_kind": row["option_kind"] or "",
            "option_value": row["option_value"] or "",
            "attribute_value": row["option_value"] or "",
            "color": row["color"] or "",
            "color_any": bool(row["color_any"]),
            "fulfillment_method": row["fulfillment_method"] or "",
            "phone_masked": self._request_phone_masked(row["phone"]),
            "contact_method": row["contact_method"] or "",
            "location_url": self._request_safe_location(
                row["location_url"], url=True,
            ),
            "address": self._request_safe_location(row["address"]),
            "preferred_time": row["preferred_time"] or "",
            "database_price": row["database_price"] or "",
            "price_uzs": row["database_price"] or "",
            "source_updated_at": row["source_updated_at"] or "",
            "price_source_updated_at": row["source_updated_at"] or "",
            "expected_input": row["wizard_state"],
            "items": selection_fields.get("items", []),
            "location_received": bool(row["location_url"] or row["address"]),
            "needs_manager_reply": bool(row["needs_manager_reply"]),
            "created_at_utc": self._as_utc(row["created_at"]),
            "updated_at_utc": self._as_utc(row["updated_at"]),
        }
        self._queue_outbox(db, "request", request_id, payload, now)

    @staticmethod
    def _revoke_request_callbacks(
        db: sqlite3.Connection,
        request_id: str,
        now: datetime,
        reason: str,
        *,
        before_revision: int | None = None,
    ) -> int:
        sql = """UPDATE business_callback_tokens SET state='revoked',
                 revoked_at=?,revoke_reason=? WHERE request_id=? AND state='issued'"""
        params: list[Any] = [iso(now), str(reason)[:80], request_id]
        if before_revision is not None:
            sql += " AND request_revision<?"
            params.append(int(before_revision))
        return db.execute(sql, params).rowcount

    def business_request(self, request_id: str):
        with connect(self.path) as db:
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    def active_business_request(
        self, chat_id: str, session_id: str | None = None
    ):
        sql = """SELECT * FROM business_requests WHERE chat_id=?
                 AND status IN ('collecting','ready','submitted')"""
        params: list[Any] = [str(chat_id)]
        if session_id is not None:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with connect(self.path) as db:
            return db.execute(sql, params).fetchone()

    def get_or_create_business_request(
        self,
        chat_id: str,
        session_id: str | None,
        now: datetime,
        *,
        business_connection_id: str | None = None,
        cycle_id: str | None = None,
        business_date: str | None = None,
        language: str | None = None,
        event_at: datetime | None = None,
        message_id: int | None = None,
        telegram_update_id: int | None = None,
    ):
        chat_id = str(chat_id)
        opened_at = event_at or now
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if telegram_update_id is not None:
                original = db.execute(
                    "SELECT * FROM business_requests WHERE origin_update_id=?",
                    (int(telegram_update_id),),
                ).fetchone()
                if original:
                    if str(original["chat_id"]) != chat_id:
                        raise ValueError(
                            "request origin update belongs to another chat"
                        )
                    return original
            existing = db.execute(
                """SELECT * FROM business_requests WHERE chat_id=?
                   AND status IN ('collecting','ready')
                   ORDER BY created_at DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
            if existing:
                if session_id is None or existing["session_id"] == session_id:
                    return existing
                old_revision = int(existing["revision"])
                new_revision = old_revision + 1
                db.execute(
                    """UPDATE business_requests SET status='expired',
                       wizard_state='closed',revision=?,closed_at=?,
                       close_reason='session_changed',needs_manager_reply=1,
                       updated_at=? WHERE request_id=? AND revision=?""",
                    (
                        new_revision,
                        iso(now),
                        iso(now),
                        existing["request_id"],
                        old_revision,
                    ),
                )
                self._revoke_request_callbacks(
                    db,
                    existing["request_id"],
                    now,
                    "session_changed",
                )
                self._append_request_event(
                    db,
                    existing["request_id"],
                    new_revision,
                    "expired",
                    "system",
                    now,
                    event_key=f"session-changed:{session_id or 'none'}",
                    payload={"reason": "session_changed"},
                )
                self._queue_request_outbox(db, existing["request_id"], now)
            if session_id and not business_date:
                session = db.execute(
                    "SELECT business_date FROM business_sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                business_date = session["business_date"] if session else None
            if not cycle_id:
                cycle = db.execute(
                    """SELECT cycle_id FROM response_cycles WHERE chat_id=?
                       AND status='waiting_manager' ORDER BY first_client_at LIMIT 1""",
                    (chat_id,),
                ).fetchone()
                cycle_id = cycle["cycle_id"] if cycle else None
            request_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO business_requests(
                   request_id,business_connection_id,chat_id,session_id,cycle_id,
                   business_date,origin_update_id,language,opened_at,last_client_at,
                   last_client_message_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    business_connection_id,
                    chat_id,
                    session_id,
                    cycle_id,
                    business_date,
                    telegram_update_id,
                    language,
                    iso(opened_at),
                    iso(opened_at),
                    message_id,
                    iso(now),
                    iso(now),
                ),
            )
            self._append_request_event(
                db, request_id, 1, "created", "system", now,
                event_key="created",
                payload={"wizard_state": "model"},
                telegram_update_id=telegram_update_id,
                telegram_message_id=message_id,
            )
            self._queue_request_outbox(db, request_id, now)
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    def update_business_request(
        self,
        request_id: str,
        expected_revision: int,
        now: datetime,
        changes: dict[str, Any] | None = None,
        *,
        selections: dict[str, Any] | None = None,
        clear_selections: Iterable[str] = (),
        event_type: str = "updated",
        actor_type: str = "client",
        event_key: str | None = None,
        telegram_update_id: int | None = None,
        telegram_message_id: int | None = None,
        client_at: datetime | None = None,
        **fields: Any,
    ):
        clear_keys = tuple(str(key) for key in clear_selections)
        supplied = dict(changes or {})
        supplied.update(fields)
        normalized: dict[str, Any] = {}
        for raw_key, value in supplied.items():
            key = _REQUEST_FIELD_ALIASES.get(raw_key, raw_key)
            if key not in _REQUEST_EDITABLE_FIELDS:
                raise ValueError(f"unsupported request field: {raw_key}")
            normalized[key] = value
        if "status" in normalized and normalized["status"] not in _REQUEST_EDITABLE_STATUSES:
            raise ValueError("request status is not editable through this method")
        method = normalized.get("fulfillment_method")
        if method is not None and method not in {"delivery", "pickup"}:
            raise ValueError("invalid fulfillment method")
        for key in (
            "wizard_state", "language", "exact_model", "model_key", "model_url",
            "option_kind", "option_value", "color", "fulfillment_method",
            "phone", "contact_method", "location_url", "address",
            "preferred_time", "database_price", "source_updated_at",
        ):
            if key in normalized and normalized[key] is not None:
                limit = 1_000 if key in {"address", "location_url", "model_url"} else 300
                normalized[key] = str(normalized[key])[:limit]
        if "phone" in normalized and normalized["phone"]:
            if not re.fullmatch(r"[+\d\s().-]+", normalized["phone"]):
                raise ValueError("invalid_phone")
            digits = re.sub(r"\D", "", normalized["phone"])
            # E.164 permits at most 15 digits. Rejecting longer values also
            # prevents card/account-number-shaped input from being persisted in
            # the dedicated phone field if service validation is bypassed.
            if not 7 <= len(digits) <= 15:
                raise ValueError("invalid_phone")
        for key in ("color_any", "needs_manager_reply"):
            if key in normalized:
                normalized[key] = int(bool(normalized[key]))

        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if event_key:
                prior = db.execute(
                    """SELECT 1 FROM business_request_events
                       WHERE request_id=? AND event_key=?""",
                    (request_id, str(event_key)[:160]),
                ).fetchone()
                if prior:
                    return db.execute(
                        "SELECT * FROM business_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
            row = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                not row or row["status"] not in _REQUEST_EDITABLE_STATUSES
                or int(row["revision"]) != int(expected_revision)
            ):
                return None
            if actor_type == "client":
                client = db.execute(
                    """SELECT bot_paused,manager_lock_until
                       FROM business_clients WHERE chat_id=?""",
                    (row["chat_id"],),
                ).fetchone()
                if client and client["bot_paused"]:
                    return None
                if client and client["manager_lock_until"]:
                    try:
                        if datetime.fromisoformat(client["manager_lock_until"]) > now:
                            return None
                    except (TypeError, ValueError):
                        return None
                if row["session_id"]:
                    session = db.execute(
                        """SELECT automation_handoff FROM business_sessions
                           WHERE session_id=?""",
                        (row["session_id"],),
                    ).fetchone()
                    if session and session["automation_handoff"]:
                        return None
                fence_at = client_at
                fence_message_id = telegram_message_id
                if fence_at is None and row["last_client_at"]:
                    try:
                        fence_at = datetime.fromisoformat(row["last_client_at"])
                        fence_message_id = row["last_client_message_id"]
                    except (TypeError, ValueError):
                        return None
                if fence_at is not None and self._manager_covers_client(
                    db,
                    str(row["chat_id"]),
                    fence_at,
                    fence_message_id,
                ):
                    return None
            selection_values = json.loads(row["selection_fields"] or "{}")
            for key in clear_keys:
                selection_values.pop(key, None)
            if selections:
                for raw_key, value in selections.items():
                    key = str(raw_key)
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", key):
                        raise ValueError("invalid dynamic selection field")
                    selection_values[key] = value
            if selections is not None or clear_keys:
                normalized["selection_fields"] = self._request_json(selection_values)
            if client_at is not None:
                old_client_at = (
                    datetime.fromisoformat(row["last_client_at"])
                    if row["last_client_at"] else None
                )
                if old_client_at is None or client_at >= old_client_at:
                    normalized["last_client_at"] = iso(client_at)
                    normalized["last_client_message_id"] = telegram_message_id
            new_revision = int(row["revision"]) + 1
            assignments = [f"{key}=?" for key in normalized]
            assignments.extend(("revision=?", "updated_at=?"))
            values = [*normalized.values(), new_revision, iso(now)]
            changed = db.execute(
                f"""UPDATE business_requests SET {','.join(assignments)}
                    WHERE request_id=? AND revision=?
                    AND status IN ('collecting','ready')""",
                (*values, request_id, int(expected_revision)),
            ).rowcount
            if changed != 1:
                return None
            self._revoke_request_callbacks(
                db, request_id, now, "revision_changed",
                before_revision=new_revision,
            )
            event_payload: dict[str, Any] = {
                "changed": sorted(normalized),
            }
            if selections:
                event_payload["selections"] = selections
            self._append_request_event(
                db, request_id, new_revision, event_type, actor_type, now,
                event_key=event_key or f"revision:{new_revision}",
                payload=event_payload,
                telegram_update_id=telegram_update_id,
                telegram_message_id=telegram_message_id,
            )
            self._queue_request_outbox(db, request_id, now)
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    def complete_business_request(
        self,
        request_id: str,
        completion: str,
        expected_revision: int,
        now: datetime,
        *,
        event_key: str | None = None,
        telegram_update_id: int | None = None,
        telegram_message_id: int | None = None,
    ):
        methods = {
            "complete_delivery": "delivery",
            "complete_pickup": "pickup",
            "delivery": "delivery",
            "pickup": "pickup",
        }
        if completion not in methods:
            raise ValueError("invalid request completion")
        fulfillment = methods[completion]
        stable_key = event_key or f"complete:{fulfillment}:{expected_revision}"
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                """SELECT 1 FROM business_request_events
                   WHERE request_id=? AND event_key=?""",
                (request_id, stable_key),
            ).fetchone():
                return db.execute(
                    "SELECT * FROM business_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            row = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "submitted" and row["fulfillment_method"] == fulfillment:
                return row
            if (
                row["status"] not in _REQUEST_EDITABLE_STATUSES
                or int(row["revision"]) != int(expected_revision)
            ):
                return None
            client = db.execute(
                """SELECT bot_paused,manager_lock_until FROM business_clients
                   WHERE chat_id=?""",
                (row["chat_id"],),
            ).fetchone()
            if client and client["bot_paused"]:
                return None
            if client and client["manager_lock_until"]:
                try:
                    if datetime.fromisoformat(client["manager_lock_until"]) > now:
                        return None
                except (TypeError, ValueError):
                    return None
            if row["session_id"]:
                session = db.execute(
                    """SELECT automation_handoff FROM business_sessions
                       WHERE session_id=?""",
                    (row["session_id"],),
                ).fetchone()
                if session and session["automation_handoff"]:
                    return None
            if row["last_client_at"]:
                try:
                    if self._manager_covers_client(
                        db,
                        str(row["chat_id"]),
                        datetime.fromisoformat(row["last_client_at"]),
                        row["last_client_message_id"],
                    ):
                        return None
                except (TypeError, ValueError):
                    return None
            if fulfillment == "delivery":
                if not ((row["location_url"] or "").strip() or (row["address"] or "").strip()):
                    raise ValueError("location_required_for_complete_delivery")
            try:
                request_fields = json.loads(row["selection_fields"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                request_fields = {}
            items = (
                [item for item in request_fields.get("items", []) if isinstance(item, dict)]
                if isinstance(request_fields, dict)
                else []
            )
            if not items and not (row["exact_model"] or "").strip():
                raise ValueError("model_required_for_completion")
            new_revision = int(row["revision"]) + 1
            db.execute(
                """UPDATE business_requests SET status='submitted',
                   wizard_state=?,fulfillment_method=?,revision=?,submitted_at=?,
                   needs_manager_reply=1,updated_at=? WHERE request_id=?
                   AND revision=? AND status IN ('collecting','ready')""",
                (
                    f"complete_{fulfillment}", fulfillment, new_revision,
                    iso(now), iso(now), request_id, int(expected_revision),
                ),
            )
            self._revoke_request_callbacks(
                db, request_id, now, "request_submitted",
            )
            self._append_request_event(
                db, request_id, new_revision, "submitted", "client", now,
                event_key=stable_key,
                payload={"fulfillment_method": fulfillment},
                telegram_update_id=telegram_update_id,
                telegram_message_id=telegram_message_id,
            )
            if row["session_id"]:
                selected_color = (
                    row["color"]
                    or ("any" if int(row["color_any"] or 0) else None)
                )
                db.execute(
                    """UPDATE business_sessions SET matched_model=?,memory=?,
                       color=?,location_received=?,location_url=?,order_intent=1,
                       needs_manager_reply=1,status='waiting_manager',updated_at=?
                       WHERE session_id=?""",
                    (
                        (items[0].get("model") if items else row["exact_model"]),
                        (items[0].get("option_value") if items else row["option_value"]),
                        (
                            items[0].get("color")
                            or ("any" if items[0].get("color_any") else None)
                            if items else selected_color
                        ),
                        int(bool(row["location_url"] or row["address"])),
                        row["location_url"],
                        iso(now),
                        row["session_id"],
                    ),
                )
                self._queue_dialog_outbox(db, row["session_id"], now)
            self._queue_request_outbox(db, request_id, now)
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    def cancel_business_request(
        self,
        request_id: str,
        expected_revision: int,
        now: datetime,
        reason: str = "client_cancelled",
        *,
        event_key: str | None = None,
    ):
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "cancelled":
                return row
            if (
                row["status"] not in _REQUEST_ACTIVE_STATUSES
                or int(row["revision"]) != int(expected_revision)
            ):
                return None
            new_revision = int(row["revision"]) + 1
            db.execute(
                """UPDATE business_requests SET status='cancelled',
                   wizard_state='closed',revision=?,closed_at=?,close_reason=?,
                   phone=NULL,contact_method=NULL,location_url=NULL,address=NULL,
                   needs_manager_reply=0,updated_at=? WHERE request_id=?""",
                (new_revision, iso(now), str(reason)[:80], iso(now), request_id),
            )
            self._revoke_request_callbacks(db, request_id, now, reason)
            self._append_request_event(
                db, request_id, new_revision, "cancelled", "client", now,
                event_key=event_key or f"cancelled:{expected_revision}",
                payload={"reason": str(reason)[:80]},
            )
            self._queue_request_outbox(db, request_id, now)
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    def expire_business_request(
        self,
        request_id: str,
        expected_revision: int,
        now: datetime,
        reason: str = "night_ended",
        *,
        event_key: str | None = None,
    ):
        """Close an unfinished draft at a durable schedule boundary.

        Submitted requests are deliberately not expired: they remain waiting for
        a manager. The draft's collected details stay available for that manager.
        """
        stable_key = event_key or f"expired:{reason}:{expected_revision}"
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                """SELECT 1 FROM business_request_events
                   WHERE request_id=? AND event_key=?""",
                (request_id, str(stable_key)[:160]),
            ).fetchone()
            if prior:
                return db.execute(
                    "SELECT * FROM business_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            row = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "expired":
                return row
            if (
                row["status"] not in _REQUEST_EDITABLE_STATUSES
                or int(row["revision"]) != int(expected_revision)
            ):
                return None
            callback_claims = db.execute(
                """SELECT r.callback_query_id,r.created_at,u.status,
                          u.lease_expires_at
                   FROM business_callback_receipts r
                   LEFT JOIN business_updates u
                     ON u.callback_query_id=r.callback_query_id
                   WHERE r.request_id=? AND r.status='claimed'
                   AND julianday(r.created_at)<=julianday(?)""",
                (request_id, iso(now)),
            ).fetchall()
            live_callback = False
            for claim in callback_claims:
                update_status = claim["status"]
                if update_status in {"new", "retry", "error"}:
                    live_callback = True
                    break
                if update_status == "running":
                    try:
                        live_callback = (
                            not claim["lease_expires_at"]
                            or datetime.fromisoformat(claim["lease_expires_at"])
                            > now
                        )
                    except (TypeError, ValueError):
                        live_callback = False
                    if live_callback:
                        break
                if update_status is None:
                    try:
                        live_callback = (
                            datetime.fromisoformat(claim["created_at"])
                            + timedelta(minutes=5)
                            > now
                        )
                    except (TypeError, ValueError):
                        live_callback = False
                    if live_callback:
                        break
            if live_callback:
                # The callback webhook arrived before this scheduled boundary,
                # but its durable update may still be replaying after a crash.
                # Let the action scheduler retry expiry instead of discarding
                # an already accepted customer choice.
                raise PendingBusinessCallbackError(
                    "business callback is still being applied"
                )
            if callback_claims:
                # A terminal/missing update cannot apply this consumed token
                # anymore. Close the orphan receipt so it cannot keep the
                # request alive forever or be replayed after expiry.
                db.execute(
                    """UPDATE business_callback_receipts SET status='rejected',
                       outcome='request_expired',processed_at=?
                       WHERE request_id=? AND status='claimed'""",
                    (iso(now), request_id),
                )
            new_revision = int(row["revision"]) + 1
            changed = db.execute(
                """UPDATE business_requests SET status='expired',
                   wizard_state='closed',revision=?,closed_at=?,close_reason=?,
                   needs_manager_reply=1,updated_at=? WHERE request_id=?
                   AND revision=? AND status IN ('collecting','ready')""",
                (
                    new_revision,
                    iso(now),
                    str(reason)[:80],
                    iso(now),
                    request_id,
                    int(expected_revision),
                ),
            ).rowcount
            if changed != 1:
                return None
            self._revoke_request_callbacks(db, request_id, now, reason)
            self._append_request_event(
                db,
                request_id,
                new_revision,
                "expired",
                "system",
                now,
                event_key=stable_key,
                payload={"reason": str(reason)[:80]},
            )
            self._queue_request_outbox(db, request_id, now)
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    def business_request_events(self, request_id: str):
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM business_request_events WHERE request_id=?
                   ORDER BY event_id""",
                (request_id,),
            ).fetchall()

    def business_request_event(self, request_id: str, event_key: str):
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM business_request_events
                   WHERE request_id=? AND event_key=? LIMIT 1""",
                (request_id, str(event_key)[:160]),
            ).fetchone()

    def bind_business_request_message(
        self,
        request_id: str,
        expected_revision: int,
        message_id: int,
        now: datetime,
    ):
        """Attach the visible wizard message without invalidating callbacks."""

        if isinstance(message_id, bool) or int(message_id) <= 0:
            raise ValueError("invalid wizard message id")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                not row
                or row["status"] not in _REQUEST_EDITABLE_STATUSES
                or int(row["revision"]) != int(expected_revision)
            ):
                return None
            fields = json.loads(row["selection_fields"] or "{}")
            if fields.get("wizard_message_id") == int(message_id):
                return row
            fields["wizard_message_id"] = int(message_id)
            db.execute(
                """UPDATE business_requests SET selection_fields=?,updated_at=?
                   WHERE request_id=? AND revision=?
                   AND status IN ('collecting','ready')""",
                (
                    self._request_json(fields),
                    iso(now),
                    request_id,
                    int(expected_revision),
                ),
            )
            self._queue_request_outbox(db, request_id, now)
            return db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()

    @staticmethod
    def _business_callback_hash(token: str) -> str:
        opaque = str(token)
        for prefix in ("nr1:", "brq:"):
            if opaque.startswith(prefix):
                opaque = opaque[len(prefix):]
                break
        return hashlib.sha256(opaque.encode("utf-8")).hexdigest()

    def issue_business_callback(
        self,
        request_id: str,
        expected_revision: int,
        action: str,
        payload: dict[str, Any] | None,
        now: datetime,
        *,
        ttl_seconds: int = 900,
    ) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,48}", str(action)):
            raise ValueError("invalid callback action")
        encoded_payload = self._request_json(payload or {}, max_bytes=4_096)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute(
                "SELECT * FROM business_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                not request or request["status"] not in _REQUEST_EDITABLE_STATUSES
                or int(request["revision"]) != int(expected_revision)
            ):
                return None
            for _ in range(5):
                token = secrets.token_urlsafe(18)
                token_hash = self._business_callback_hash(token)
                try:
                    db.execute(
                        """INSERT INTO business_callback_tokens(
                           token_hash,request_id,request_revision,action,payload,
                           expires_at,created_at) VALUES(?,?,?,?,?,?,?)""",
                        (
                            token_hash, request_id, int(expected_revision),
                            action, encoded_payload,
                            iso(now + timedelta(seconds=max(1, int(ttl_seconds)))),
                            iso(now),
                        ),
                    )
                    return token
                except sqlite3.IntegrityError:
                    continue
            raise RuntimeError("could not allocate callback token")

    def business_callback_receipt(self, callback_query_id: str):
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM business_callback_receipts
                   WHERE callback_query_id=?""",
                (str(callback_query_id),),
            ).fetchone()

    def consume_business_callback(
        self,
        token: str,
        callback_query_id: str,
        chat_id: str,
        now: datetime,
    ):
        """Claim one opaque callback for its private-chat customer.

        ``chat_id`` is the callback actor id for direct callers. For a durable
        Telegram update the repository derives ``callback_query.from.id`` and
        the message chat from the stored payload instead, so a manager clicking
        a customer's keyboard cannot authorize itself by reusing message.chat.id.
        """
        callback_query_id = str(callback_query_id)
        if not callback_query_id:
            raise ValueError("callback_query_id is required")
        token_hash = self._business_callback_hash(token)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            callback_at = now
            callback_actor_id = str(chat_id)
            callback_message_chat_id: str | None = None
            callback_connection_id: str | None = None
            durable_update = db.execute(
                """SELECT received_at,raw_payload,chat_id,business_connection_id
                   FROM business_updates
                   WHERE callback_query_id=?""",
                (callback_query_id,),
            ).fetchone()
            if durable_update:
                try:
                    callback_at = datetime.fromisoformat(
                        durable_update["received_at"]
                    )
                except (TypeError, ValueError):
                    callback_at = now
                callback_message_chat_id = (
                    str(durable_update["chat_id"])
                    if durable_update["chat_id"] is not None else None
                )
                callback_connection_id = (
                    str(durable_update["business_connection_id"])
                    if durable_update["business_connection_id"] is not None
                    else None
                )
                try:
                    durable_payload = json.loads(durable_update["raw_payload"])
                    callback_actor_id = str(
                        ((durable_payload.get("callback_query") or {}).get("from") or {})
                        .get("id", "")
                    )
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    callback_actor_id = ""
            prior = db.execute(
                """SELECT * FROM business_callback_receipts
                   WHERE callback_query_id=?""",
                (callback_query_id,),
            ).fetchone()
            if prior:
                return prior
            saved_token = db.execute(
                """SELECT * FROM business_callback_tokens
                   WHERE token_hash=?""",
                (token_hash,),
            ).fetchone()
            request = (
                db.execute(
                    "SELECT * FROM business_requests WHERE request_id=?",
                    (saved_token["request_id"],),
                ).fetchone()
                if saved_token else None
            )
            request_connection = (
                db.execute(
                    """SELECT is_enabled,can_reply FROM business_connections
                       WHERE connection_id=?""",
                    (request["business_connection_id"],),
                ).fetchone()
                if request and request["business_connection_id"] else None
            )
            request_client = (
                db.execute(
                    """SELECT bot_paused,manager_lock_until
                       FROM business_clients WHERE chat_id=?""",
                    (request["chat_id"],),
                ).fetchone()
                if request else None
            )
            request_session = (
                db.execute(
                    """SELECT automation_handoff FROM business_sessions
                       WHERE session_id=?""",
                    (request["session_id"],),
                ).fetchone()
                if request and request["session_id"] else None
            )
            outcome = "invalid"
            receipt_status = "rejected"
            callback_locked = False
            manager_fenced = False
            if request_client and request_client["manager_lock_until"]:
                try:
                    callback_locked = (
                        datetime.fromisoformat(request_client["manager_lock_until"])
                        > callback_at
                    )
                except (TypeError, ValueError):
                    callback_locked = True
            if request and request["last_client_at"]:
                try:
                    request_client_at = datetime.fromisoformat(
                        request["last_client_at"]
                    )
                    manager_fenced = self._manager_covers_client(
                        db,
                        str(request["chat_id"]),
                        request_client_at,
                        request["last_client_message_id"],
                    )
                except (TypeError, ValueError):
                    manager_fenced = True
            if saved_token:
                expires_at = datetime.fromisoformat(saved_token["expires_at"])
                if saved_token["state"] == "consumed":
                    outcome = "already_used"
                elif saved_token["state"] == "revoked":
                    outcome = "stale"
                elif expires_at <= callback_at:
                    outcome = "expired"
                    db.execute(
                        """UPDATE business_callback_tokens SET state='expired',
                           revoked_at=?,revoke_reason='expired'
                           WHERE token_hash=? AND state='issued'""",
                        (iso(now), token_hash),
                    )
                elif (
                    not request
                    or str(request["chat_id"]) != callback_actor_id
                    or (
                        callback_message_chat_id is not None
                        and str(request["chat_id"]) != callback_message_chat_id
                    )
                ):
                    outcome = "wrong_chat"
                elif (
                    request["business_connection_id"]
                    and durable_update is not None
                    and callback_connection_id != str(request["business_connection_id"])
                ):
                    outcome = "wrong_connection"
                elif request_connection and (
                    not request_connection["is_enabled"]
                    or not request_connection["can_reply"]
                ):
                    outcome = "connection_disabled"
                elif request_client and request_client["bot_paused"]:
                    outcome = "bot_paused"
                elif request_session and request_session["automation_handoff"]:
                    outcome = "human_handoff"
                elif manager_fenced:
                    outcome = "manager_locked"
                elif callback_locked:
                    outcome = "manager_locked"
                elif request["status"] not in _REQUEST_EDITABLE_STATUSES:
                    outcome = "closed"
                elif int(request["revision"]) != int(saved_token["request_revision"]):
                    outcome = "stale"
                else:
                    changed = db.execute(
                        """UPDATE business_callback_tokens SET state='consumed',
                           consumed_at=? WHERE token_hash=?
                           AND state IN ('issued','expired')""",
                        (iso(callback_at), token_hash),
                    ).rowcount
                    if changed == 1:
                        outcome = "accepted"
                        receipt_status = "claimed"
                        db.execute(
                            """UPDATE business_requests SET
                               last_client_at=CASE
                                 WHEN last_client_at IS NULL
                                   OR julianday(last_client_at)<julianday(?)
                                 THEN ? ELSE last_client_at END,
                               updated_at=? WHERE request_id=?""",
                            (
                                iso(callback_at),
                                iso(callback_at),
                                iso(now),
                                saved_token["request_id"],
                            ),
                        )
                    else:
                        outcome = "already_used"
            cursor = db.execute(
                """INSERT INTO business_callback_receipts(
                   callback_query_id,token_hash,request_id,request_revision,
                   chat_id,action,payload,status,outcome,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    callback_query_id,
                    token_hash,
                    saved_token["request_id"] if saved_token else None,
                    saved_token["request_revision"] if saved_token else None,
                    callback_actor_id,
                    saved_token["action"] if saved_token else None,
                    saved_token["payload"] if saved_token else "{}",
                    receipt_status,
                    outcome,
                    iso(callback_at),
                ),
            )
            return db.execute(
                """SELECT * FROM business_callback_receipts
                   WHERE receipt_id=?""",
                (cursor.lastrowid,),
            ).fetchone()

    def finish_business_callback(
        self,
        callback_query_id: str,
        now: datetime,
        *,
        applied: bool,
        outcome: str | None = None,
        result_revision: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        final_status = "applied" if applied else "rejected"
        final_outcome = str(outcome or final_status)[:80]
        encoded_result = self._request_json(result or {}, max_bytes=4_096)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute(
                """SELECT request_id FROM business_callback_receipts
                   WHERE callback_query_id=?""",
                (str(callback_query_id),),
            ).fetchone()
            changed = db.execute(
                """UPDATE business_callback_receipts SET status=?,outcome=?,
                   result_revision=?,result=?,processed_at=? WHERE callback_query_id=?
                   AND status='claimed'""",
                (
                    final_status, final_outcome, result_revision, encoded_result, iso(now),
                    str(callback_query_id),
                ),
            ).rowcount == 1
            if changed and receipt and receipt["request_id"]:
                # The 09:30 action may have exhausted an older generic retry
                # budget while this durable callback update was still live.
                # Wake a post-boundary retry. A fresh pending action has zero
                # attempts and must retain its original 09:30 boundary.
                db.execute(
                    """UPDATE scheduled_actions SET status='pending',attempts=0,
                       generation=generation+1,execute_at=?,next_attempt_at=?,
                       lease_token=NULL,lease_expires_at=NULL,last_error=NULL
                       WHERE dedupe_key=?
                       AND status IN ('pending','running','failed')
                       AND (status='failed' OR attempts>0)""",
                    (
                        iso(now),
                        iso(now),
                        f"request-expire:{receipt['request_id']}",
                    ),
                )
            return changed

    def _close_business_requests_for_manager_in_db(
        self,
        db: sqlite3.Connection,
        chat_id: str,
        answer_at: datetime,
        now: datetime,
        *,
        manager_message_id: int | None = None,
        cycle_id: str | None = None,
    ) -> int:
        closed = 0
        requests = db.execute(
            """SELECT * FROM business_requests WHERE chat_id=?
               AND status IN ('collecting','ready','submitted','expired')
               AND needs_manager_reply=1""",
            (str(chat_id),),
        ).fetchall()
        for request in requests:
            raw_activity = request["last_client_at"] or request["opened_at"]
            try:
                activity_at = datetime.fromisoformat(raw_activity)
            except (TypeError, ValueError):
                continue
            activity_message_id = request["last_client_message_id"]
            # The client message row is committed before wizard processing. If
            # the process crashes between those transactions, chronology still
            # fences a delayed manager webhook from closing the active request.
            latest_client = db.execute(
                """SELECT COALESCE(edited_at,telegram_date) AS activity_date,
                          message_id FROM business_messages
                   WHERE chat_id=? AND sender_type='client'
                   AND telegram_date IS NOT NULL
                   AND julianday(COALESCE(edited_at,telegram_date))>=julianday(?)
                   ORDER BY julianday(COALESCE(edited_at,telegram_date)) DESC,
                            message_id DESC LIMIT 1""",
                (str(chat_id), request["opened_at"]),
            ).fetchone()
            if latest_client:
                try:
                    saved_activity_at = datetime.fromisoformat(
                        latest_client["activity_date"]
                    )
                except (TypeError, ValueError):
                    saved_activity_at = None
                if saved_activity_at is not None and (
                    saved_activity_at > activity_at
                    or (
                        saved_activity_at == activity_at
                        and (
                            activity_message_id is None
                            or int(latest_client["message_id"])
                            > int(activity_message_id)
                        )
                    )
                ):
                    activity_at = saved_activity_at
                    activity_message_id = latest_client["message_id"]
            covered = activity_at < answer_at
            if activity_at == answer_at:
                covered = (
                    manager_message_id is None
                    or activity_message_id is None
                    or int(activity_message_id) <= int(manager_message_id)
                )
            if not covered:
                continue
            new_revision = int(request["revision"]) + 1
            db.execute(
                """UPDATE business_requests SET status='manager_closed',
                   wizard_state='closed',revision=?,needs_manager_reply=0,
                   closed_at=?,close_reason='manager_answer',
                   closed_by_message_id=?,updated_at=? WHERE request_id=?
                   AND status IN ('collecting','ready','submitted','expired')
                   AND needs_manager_reply=1""",
                (
                    new_revision, iso(answer_at), manager_message_id,
                    iso(now), request["request_id"],
                ),
            )
            self._revoke_request_callbacks(
                db, request["request_id"], now, "manager_answer",
            )
            self._append_request_event(
                db, request["request_id"], new_revision, "manager_closed",
                "manager", now,
                event_key=(
                    f"manager:{manager_message_id}"
                    if manager_message_id is not None
                    else f"manager:{iso(answer_at)}"
                ),
                payload={
                    "cycle_id": cycle_id or "",
                    "closed_at": iso(answer_at),
                },
                telegram_message_id=manager_message_id,
            )
            self._queue_request_outbox(db, request["request_id"], now)
            closed += 1
        return closed

    def close_business_requests_for_manager(
        self,
        chat_id: str,
        answer_at: datetime,
        now: datetime,
        *,
        manager_message_id: int | None = None,
        cycle_id: str | None = None,
    ) -> int:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            return self._close_business_requests_for_manager_in_db(
                db, chat_id, answer_at, now,
                manager_message_id=manager_message_id,
                cycle_id=cycle_id,
            )

    def replace_model_choices(self, session_id: str, choices, now: datetime) -> None:
        with connect(self.path) as db:
            db.execute("DELETE FROM business_model_choices WHERE session_id=?", (session_id,))
            db.executemany("INSERT INTO business_model_choices(session_id,choice_number,model_name,model_url,created_at) VALUES(?,?,?,?,?)",
                           [(session_id, n, model, url, iso(now)) for n, (model, url) in enumerate(choices, 1)])

    def model_choice(self, session_id: str, number: int):
        with connect(self.path) as db:
            return db.execute("SELECT * FROM business_model_choices WHERE session_id=? AND choice_number=?", (session_id, number)).fetchone()

    # Atomic Sheets outbox claim/retry.
    def acquire_sheets_sync_lease(
        self, now: datetime, lease_seconds: int = 600
    ) -> str | None:
        """Acquire the cross-process lease guarding Google read/modify/write.

        Row-level outbox leases prevent duplicate processing of one entity, but
        two workers claiming different entities can still choose the same empty
        Google row.  This lease serializes the external read/append transaction
        across every process sharing the SQLite database.
        """
        token = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=max(30, int(lease_seconds)))
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """INSERT INTO business_runtime_leases(
                   lease_name,lease_token,lease_expires_at,updated_at)
                   VALUES('google_sheets_sync',?,?,?)
                   ON CONFLICT(lease_name) DO UPDATE SET
                   lease_token=excluded.lease_token,
                   lease_expires_at=excluded.lease_expires_at,
                   updated_at=excluded.updated_at
                   WHERE julianday(business_runtime_leases.lease_expires_at) IS NULL
                      OR julianday(business_runtime_leases.lease_expires_at)
                         <=julianday(excluded.updated_at)""",
                (token, iso(expires_at), iso(now)),
            ).rowcount
        return token if changed == 1 else None

    def release_sheets_sync_lease(self, token: str) -> bool:
        if not token:
            return False
        with connect(self.path) as db:
            return db.execute(
                """DELETE FROM business_runtime_leases
                   WHERE lease_name='google_sheets_sync' AND lease_token=?""",
                (token,),
            ).rowcount == 1

    def _recover_stale_outbox(self, db: sqlite3.Connection, now: datetime) -> int:
        return db.execute("""UPDATE sheets_outbox SET status='pending',generation=generation+1,
            next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,last_error=COALESCE(last_error,'sync lease expired')
            WHERE status='running' AND (lease_expires_at IS NULL
            OR julianday(lease_expires_at) IS NULL
            OR julianday(lease_expires_at)<=julianday(?))""", (iso(now), iso(now))).rowcount

    def outbox_due(self, now: datetime, limit: int = 100, lease_seconds: int = 120):
        claimed: list[sqlite3.Row] = []
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE"); self._recover_stale_outbox(db, now)
            rows = db.execute("""SELECT id,generation FROM sheets_outbox WHERE status='pending'
                              AND julianday(next_attempt_at)<=julianday(?)
                              ORDER BY CASE entity_type
                                WHEN 'message' THEN 0
                                WHEN 'dialog' THEN 1
                                WHEN 'error' THEN 2
                                WHEN 'statistic' THEN 4
                                ELSE 3 END,
                              id LIMIT ?""", (iso(now), limit)).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                changed = db.execute("UPDATE sheets_outbox SET status='running',attempts=attempts+1,lease_token=?,lease_expires_at=? WHERE id=? AND status='pending' AND generation=?",
                                     (token, iso(now + timedelta(seconds=lease_seconds)), row["id"], row["generation"])).rowcount
                if changed: claimed.append(db.execute("SELECT * FROM sheets_outbox WHERE id=?", (row["id"],)).fetchone())
        return claimed

    def outbox_done(self, row_id: int, now: datetime, lease_token: str | None = None,
                    generation: int | None = None) -> bool:
        sql = "UPDATE sheets_outbox SET status='synced',synced_at=?,last_error=NULL,lease_token=NULL,lease_expires_at=NULL WHERE id=? AND status='running'"
        params: list[Any] = [iso(now), row_id]
        if lease_token is not None: sql += " AND lease_token=?"; params.append(lease_token)
        if generation is not None: sql += " AND generation=?"; params.append(generation)
        with connect(self.path) as db: return db.execute(sql, params).rowcount == 1

    def outbox_retry(self, row_id: int, now: datetime, attempts: int, error: str,
                     lease_token: str | None = None, generation: int | None = None,
                     retry_after: float | None = None) -> bool:
        delay = max(_backoff_seconds(attempts), float(retry_after or 0))
        sql = "UPDATE sheets_outbox SET status='pending',attempts=?,next_attempt_at=?,last_error=?,lease_token=NULL,lease_expires_at=NULL WHERE id=? AND status='running'"
        params: list[Any] = [attempts, iso(now + timedelta(seconds=delay)), (redact_sensitive_data(error) or "sheet sync failed")[:500], row_id]
        if lease_token is not None: sql += " AND lease_token=?"; params.append(lease_token)
        if generation is not None: sql += " AND generation=?"; params.append(generation)
        with connect(self.path) as db: return db.execute(sql, params).rowcount == 1

    def record_error(self, now: datetime, source: str, operation: str, error: Exception | str, *,
                     chat_id: str | None = None, session_id: str | None = None, attempts: int = 0) -> int:
        message, error_type = (redact_sensitive_data(str(error)) or "error")[:500], type(error).__name__ if isinstance(error, Exception) else "Error"
        with connect(self.path) as db:
            cursor = db.execute("INSERT INTO business_errors(source,operation,chat_id,session_id,error_type,message,attempts,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                (source, operation, chat_id, session_id, error_type, message, attempts, iso(now)))
            error_id = cursor.lastrowid
            self._queue_outbox(db, "error", str(error_id), {"error_id": str(error_id), "date_uz": iso(now),
                "source": source, "operation": operation, "chat_id": chat_id or "", "session_id": session_id or "",
                "error_type": error_type, "message": message, "attempts": attempts, "resolved": False,
                "created_at_utc": now.astimezone(timezone.utc).isoformat()}, now)
            return error_id

    def queue_statistics(self, rows: Iterable[Any], now: datetime) -> int:
        """Queue calculated metrics with a stable period/metric natural key."""
        queued = 0
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            for raw in rows:
                if isinstance(raw, dict):
                    period = str(raw.get("period", "")).strip()
                    metric = str(raw.get("metric", "")).strip()
                    value = raw.get("value", "")
                    updated_at = raw.get("updated_at_utc")
                else:
                    values = list(raw)
                    period = str(values[0] if len(values) > 0 else "").strip()
                    metric = str(values[1] if len(values) > 1 else "").strip()
                    value = values[2] if len(values) > 2 else ""
                    updated_at = values[3] if len(values) > 3 else None
                if not period or not metric:
                    continue
                payload = {
                    "period": period,
                    "metric": metric,
                    "value": value,
                    "updated_at_utc": updated_at or now.astimezone(timezone.utc).isoformat(),
                }
                entity_id = f"{period}:{metric}"
                existing = db.execute(
                    """SELECT payload FROM sheets_outbox WHERE entity_type='statistic'
                       AND entity_id=? AND operation='upsert'""",
                    (entity_id,),
                ).fetchone()
                unchanged = False
                if existing:
                    try:
                        previous = json.loads(existing["payload"])
                        unchanged = (
                            isinstance(previous, dict)
                            and previous.get("period") == period
                            and previous.get("metric") == metric
                            and previous.get("value") == value
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        unchanged = False
                # Recalculating the same snapshot every minute must not turn 76
                # already-synced rows back into pending work.  An existing pending
                # or running row is likewise left intact, including its backoff or
                # lease, because it already carries this value.
                if not unchanged:
                    self._queue_outbox(
                        db, "statistic", entity_id, payload, now
                    )
                queued += 1
        return queued

    def recover_stale(self, now: datetime) -> dict[str, int]:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            return {"updates": self._recover_stale_updates(db, now),
                    "actions": self._recover_stale_actions(db, now),
                    "outbox": self._recover_stale_outbox(db, now)}
