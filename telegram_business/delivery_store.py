from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .migrations import connect
from .security import redact_sensitive_data


def _iso(value: datetime) -> str:
    return value.isoformat()


def _backoff(attempts: int) -> int:
    return min(900, max(5, 2 ** min(max(int(attempts), 1), 9)))


class DeliveryNotificationStore:
    """Durable cursor and outbox for delivery-to-Business notifications."""

    CURSOR_KEY = "delivery_status_event_cursor"
    FEED_INSTANCE_KEY = "delivery_status_feed_instance_id"
    TELEGRAM_NOT_BEFORE_KEY = "delivery_telegram_not_before"
    LEASE_NAME = "delivery_notifications"

    def __init__(self, path: Path | str):
        self.path = path

    def acquire_cycle_lease(
        self, now: datetime, lease_seconds: int = 120
    ) -> str | None:
        token = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=max(30, int(lease_seconds)))
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """INSERT INTO business_runtime_leases(
                   lease_name,lease_token,lease_expires_at,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(lease_name) DO UPDATE SET
                   lease_token=excluded.lease_token,
                   lease_expires_at=excluded.lease_expires_at,
                   updated_at=excluded.updated_at
                   WHERE julianday(business_runtime_leases.lease_expires_at) IS NULL
                      OR julianday(business_runtime_leases.lease_expires_at)
                         <=julianday(excluded.updated_at)""",
                (self.LEASE_NAME, token, _iso(expires_at), _iso(now)),
            ).rowcount
        return token if changed == 1 else None

    def release_cycle_lease(self, token: str) -> bool:
        if not token:
            return False
        with connect(self.path) as db:
            return db.execute(
                """DELETE FROM business_runtime_leases
                   WHERE lease_name=? AND lease_token=?""",
                (self.LEASE_NAME, token),
            ).rowcount == 1

    def renew_cycle_lease(
        self, token: str, now: datetime, lease_seconds: int = 120
    ) -> bool:
        if not token:
            return False
        with connect(self.path) as db:
            return db.execute(
                """UPDATE business_runtime_leases SET lease_expires_at=?,updated_at=?
                   WHERE lease_name=? AND lease_token=?
                     AND julianday(lease_expires_at)>julianday(?)""",
                (
                    _iso(now + timedelta(seconds=max(30, int(lease_seconds)))),
                    _iso(now),
                    self.LEASE_NAME,
                    token,
                    _iso(now),
                ),
            ).rowcount == 1

    def cursor(self) -> int | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.CURSOR_KEY,),
            ).fetchone()
        if not row:
            return None
        try:
            value = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("delivery event cursor is corrupt") from exc
        if value < 0:
            raise RuntimeError("delivery event cursor is corrupt")
        return value

    def feed_instance_id(self) -> str | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.FEED_INSTANCE_KEY,),
            ).fetchone()
        value = str(row["value"] or "").strip() if row else ""
        return value or None

    def reconcile_feed(
        self,
        feed_instance_id: str,
        latest_event_id: int,
        cursor_reset_required: bool,
        now: datetime,
    ) -> bool:
        """Bind the cursor to one feed, safely baselining a reset source.

        ``True`` means the caller may import the fetched page. ``False`` means
        this call atomically moved the cursor to the source high-water mark and
        the caller must return without emitting historical events.
        """

        instance_id = str(feed_instance_id or "").strip()
        latest = int(latest_event_id)
        if not instance_id or len(instance_id) > 100 or latest < 0:
            raise ValueError("invalid delivery feed identity")
        if not isinstance(cursor_reset_required, bool):
            raise TypeError("cursor_reset_required must be boolean")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor_row = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.CURSOR_KEY,),
            ).fetchone()
            instance_row = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.FEED_INSTANCE_KEY,),
            ).fetchone()
            current_instance = (
                str(instance_row["value"] or "").strip() if instance_row else ""
            )
            needs_baseline = (
                cursor_row is None
                or bool(cursor_reset_required)
                or not current_instance
                or current_instance != instance_id
            )
            if needs_baseline:
                # Numeric event/order IDs can be reused after a restored or
                # replaced delivery database. Any baseline (including a cursor
                # with no pre-existing epoch) therefore starts a fresh local
                # namespace. The immutable Telegram message audit remains in
                # business_messages, while stale delivery dedupe rows must not
                # suppress an unrelated future order with a reused integer ID.
                db.execute("DELETE FROM delivery_status_notifications")
                db.execute(
                    """DELETE FROM business_outbound_deliveries
                       WHERE dedupe_key LIKE 'delivery-status:%'"""
                )
                db.execute(
                    """INSERT INTO business_integration_state(key,value,updated_at)
                       VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,updated_at=excluded.updated_at""",
                    (self.CURSOR_KEY, str(latest), _iso(now)),
                )
            db.execute(
                """INSERT INTO business_integration_state(key,value,updated_at)
                   VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,updated_at=excluded.updated_at""",
                (self.FEED_INSTANCE_KEY, instance_id, _iso(now)),
            )
            return not needs_baseline

    def initialize_cursor(self, latest_event_id: int, now: datetime) -> bool:
        value = int(latest_event_id)
        if value < 0:
            raise ValueError("latest_event_id must be non-negative")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            return db.execute(
                """INSERT OR IGNORE INTO business_integration_state(
                   key,value,updated_at) VALUES(?,?,?)""",
                (self.CURSOR_KEY, str(value), _iso(now)),
            ).rowcount == 1

    def telegram_not_before(self) -> datetime | None:
        """Return the durable global Telegram rate-limit boundary, if any."""

        with connect(self.path) as db:
            row = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.TELEGRAM_NOT_BEFORE_KEY,),
            ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(str(row["value"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("delivery Telegram not-before is corrupt") from exc

    def set_telegram_not_before(
        self,
        not_before: datetime,
        now: datetime,
    ) -> datetime:
        """Atomically extend, but never shorten, the global Telegram boundary."""

        if not isinstance(not_before, datetime) or not isinstance(now, datetime):
            raise TypeError("not_before and now must be datetimes")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO business_integration_state(key,value,updated_at)
                   VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET
                   value=CASE
                     WHEN julianday(excluded.value)>julianday(business_integration_state.value)
                     THEN excluded.value ELSE business_integration_state.value END,
                   updated_at=excluded.updated_at""",
                (
                    self.TELEGRAM_NOT_BEFORE_KEY,
                    _iso(not_before),
                    _iso(now),
                ),
            )
            row = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.TELEGRAM_NOT_BEFORE_KEY,),
            ).fetchone()
            return datetime.fromisoformat(str(row["value"]))

    @staticmethod
    def _safe_text(value: Any, maximum: int) -> str:
        return (redact_sensitive_data(str(value or "")) or "")[:maximum]

    def import_page(
        self,
        expected_cursor: int,
        next_cursor: int,
        events: Iterable[dict[str, Any]],
        now: datetime,
        *,
        invalidations: Iterable[dict[str, Any]] = (),
        feed_instance_id: str | None = None,
    ) -> int:
        """Atomically persist a page and advance its compare-and-set cursor."""

        expected = int(expected_cursor)
        following = int(next_cursor)
        if expected < 0 or following < expected:
            raise ValueError("invalid delivery feed cursor")
        prepared = tuple(events)
        prepared_invalidations = tuple(invalidations)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                "SELECT value FROM business_integration_state WHERE key=?",
                (self.CURSOR_KEY,),
            ).fetchone()
            if state is None or int(state["value"]) != expected:
                return 0
            if feed_instance_id is not None:
                source = db.execute(
                    "SELECT value FROM business_integration_state WHERE key=?",
                    (self.FEED_INSTANCE_KEY,),
                ).fetchone()
                if not source or str(source["value"]) != str(feed_instance_id):
                    return 0

            # A process can die after claiming a row. Recover expired claims in
            # this same transaction before a newer source event supersedes them.
            self._recover_stale_in_db(db, now)

            inserted = 0
            operations = [
                (int(item["event_id"]), True, item)
                for item in prepared_invalidations
            ] + [
                (int(item["event_id"]), False, item)
                for item in prepared
            ]
            operations.sort(key=lambda item: item[0])
            for event_id, is_invalidation, event in operations:
                event_id = int(event["event_id"])
                order_id = int(event["order_id"])
                # A newer source state makes an unsent older state stale. The
                # cycle lease prevents this update from racing a sender.
                db.execute(
                    """UPDATE delivery_status_notifications
                       SET state='superseded',processed_at=?,updated_at=?,
                           lease_token=NULL,lease_expires_at=NULL
                       WHERE order_id=? AND source_event_id<?
                         AND state IN ('pending','retry','deferred')""",
                    (_iso(now), _iso(now), order_id, event_id),
                )
                if is_invalidation:
                    continue

                public_status = str(event["status"])
                if event.get("public_status", event.get("status")) != event.get(
                    "current_status"
                ):
                    continue
                phones = tuple(
                    dict.fromkeys(
                        str(phone).strip()
                        for phone in event.get("phones", ())
                        if str(phone).strip()
                    )
                )[:4]
                values = (
                    self._safe_text(event.get("order_number") or order_id, 80),
                    self._safe_text(event.get("product"), 500),
                    json.dumps(phones, ensure_ascii=False),
                    self._safe_text(event.get("created_at"), 80),
                )
                existing = db.execute(
                    """SELECT source_event_id,state,chat_id
                       FROM delivery_status_notifications
                       WHERE order_id=? AND public_status=?""",
                    (order_id, public_status),
                ).fetchone()
                if existing is not None:
                    existing_id = int(existing["source_event_id"])
                    existing_state = str(existing["state"])
                    if existing_id >= event_id or existing_state in {
                        "sent", "uncertain", "running"
                    }:
                        continue
                    changed = db.execute(
                        """UPDATE delivery_status_notifications SET
                           source_event_id=?,order_number=?,product=?,phones_json=?,
                           source_created_at=?,state='pending',attempts=0,
                           next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL,
                           match_outcome=NULL,business_connection_id=NULL,chat_id=NULL,
                           session_id=NULL,template_code=NULL,telegram_message_id=NULL,
                           last_error=NULL,created_at=?,updated_at=?,processed_at=NULL
                           WHERE source_event_id=?
                             AND state NOT IN ('sent','uncertain','running')""",
                        (
                            event_id,
                            *values,
                            _iso(now),
                            _iso(now),
                            _iso(now),
                            existing_id,
                        ),
                    ).rowcount
                    if changed and existing_state == "failed" and existing["chat_id"]:
                        dedupe_key = (
                            f"delivery-status:{order_id}:{public_status}:"
                            f"{existing['chat_id']}"
                        )
                        db.execute(
                            """UPDATE business_outbound_deliveries SET state='retry',
                               last_error=NULL,updated_at=?
                               WHERE dedupe_key=? AND state='failed'""",
                            (_iso(now), dedupe_key),
                        )
                    inserted += int(changed == 1)
                    continue
                changed = db.execute(
                    """INSERT OR IGNORE INTO delivery_status_notifications(
                       source_event_id,order_id,order_number,public_status,
                       product,phones_json,source_created_at,state,
                       next_attempt_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (
                        event_id,
                        order_id,
                        values[0],
                        public_status,
                        values[1],
                        values[2],
                        values[3],
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                ).rowcount
                inserted += int(changed == 1)

            changed_cursor = db.execute(
                """UPDATE business_integration_state SET value=?,updated_at=?
                   WHERE key=? AND value=?""",
                (str(following), _iso(now), self.CURSOR_KEY, str(expected)),
            ).rowcount
            if changed_cursor != 1:
                raise sqlite3.OperationalError("delivery event cursor changed concurrently")
            return inserted

    def _recover_stale_in_db(self, db: sqlite3.Connection, now: datetime) -> int:
        return db.execute(
            """UPDATE delivery_status_notifications
               SET state='retry',next_attempt_at=?,updated_at=?,
                   lease_token=NULL,lease_expires_at=NULL,
                   last_error=COALESCE(last_error,'delivery notification lease expired')
               WHERE state='running' AND (
                   lease_expires_at IS NULL
                   OR julianday(lease_expires_at) IS NULL
                   OR julianday(lease_expires_at)<=julianday(?))""",
            (_iso(now), _iso(now), _iso(now)),
        ).rowcount

    def recover_stale(self, now: datetime) -> int:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            return self._recover_stale_in_db(db, now)

    def claim_due(
        self, now: datetime, *, limit: int = 20, lease_seconds: int = 60
    ) -> list[sqlite3.Row]:
        claimed: list[sqlite3.Row] = []
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_stale_in_db(db, now)
            rows = db.execute(
                """SELECT source_event_id FROM delivery_status_notifications
                   WHERE state IN ('pending','retry','deferred')
                     AND julianday(next_attempt_at)<=julianday(?)
                   ORDER BY source_event_id LIMIT ?""",
                (_iso(now), min(100, max(1, int(limit)))),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                changed = db.execute(
                    """UPDATE delivery_status_notifications
                       SET state='running',attempts=attempts+1,
                           lease_token=?,lease_expires_at=?,updated_at=?
                       WHERE source_event_id=?
                         AND state IN ('pending','retry','deferred')""",
                    (
                        token,
                        _iso(now + timedelta(seconds=max(30, int(lease_seconds)))),
                        _iso(now),
                        int(row["source_event_id"]),
                    ),
                ).rowcount
                if changed:
                    claimed.append(
                        db.execute(
                            """SELECT * FROM delivery_status_notifications
                               WHERE source_event_id=?""",
                            (int(row["source_event_id"]),),
                        ).fetchone()
                    )
        return claimed

    def finish(
        self,
        source_event_id: int,
        lease_token: str,
        now: datetime,
        outcome: str,
        *,
        connection_id: str | None = None,
        chat_id: str | None = None,
        session_id: str | None = None,
        template_code: str | None = None,
        telegram_message_id: int | None = None,
        error: Exception | str | None = None,
        retry_after: float | None = None,
    ) -> bool:
        terminal = {
            "sent", "unmatched", "ambiguous", "expired", "paused",
            "superseded", "uncertain", "failed",
        }
        if outcome not in terminal | {"retry", "deferred"}:
            raise ValueError("invalid delivery notification outcome")
        safe_error = (redact_sensitive_data(str(error)) or "")[:500] or None
        next_attempt_at = _iso(now)
        processed_at: str | None = _iso(now)
        state = outcome
        if outcome in {"retry", "deferred"}:
            state = outcome
            processed_at = None
            delay = max(
                float(retry_after or 0),
                float(_backoff(self._attempts(source_event_id))),
            )
            next_attempt_at = _iso(now + timedelta(seconds=delay))
        with connect(self.path) as db:
            return db.execute(
                """UPDATE delivery_status_notifications SET
                   state=?,match_outcome=?,business_connection_id=COALESCE(?,business_connection_id),
                   chat_id=COALESCE(?,chat_id),session_id=COALESCE(?,session_id),
                   template_code=COALESCE(?,template_code),
                   telegram_message_id=COALESCE(?,telegram_message_id),
                   next_attempt_at=?,last_error=?,processed_at=?,updated_at=?,
                   lease_token=NULL,lease_expires_at=NULL
                   WHERE source_event_id=? AND state='running' AND lease_token=?""",
                (
                    state,
                    outcome,
                    connection_id,
                    chat_id,
                    session_id,
                    template_code,
                    telegram_message_id,
                    next_attempt_at,
                    safe_error,
                    processed_at,
                    _iso(now),
                    int(source_event_id),
                    lease_token,
                ),
            ).rowcount == 1

    def finish_sent_and_queue_cleanup(
        self,
        source_event_id: int,
        lease_token: str,
        now: datetime,
        *,
        connection_id: str,
        chat_id: str,
        session_id: str,
        template_code: str,
        telegram_message_id: int,
    ) -> bool:
        """Finish a confirmed send and durably queue deletion of older statuses.

        The status transition and cleanup enqueue share one transaction.  This
        closes the restart window where Telegram accepted the replacement but
        the process died before recording which older messages may be removed.
        Cleanup targets are admitted only when both the delivery notification
        row and the outbound send ledger prove that the target was a status
        message sent by this bot.
        """

        event_id = int(source_event_id)
        message_id = int(telegram_message_id)
        connection = str(connection_id or "").strip()
        chat = str(chat_id or "").strip()
        template = str(template_code or "").strip()
        if event_id < 0 or message_id <= 0:
            raise ValueError("delivery event and message ids must be positive")
        if not lease_token or not connection or not chat:
            raise ValueError("delivery send identity is required")
        if not template.startswith("delivery_status_"):
            raise ValueError("delivery cleanup requires a status template")

        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT order_id,public_status FROM delivery_status_notifications
                   WHERE source_event_id=? AND state='running' AND lease_token=?""",
                (event_id, lease_token),
            ).fetchone()
            if current is None:
                return False

            order_id = int(current["order_id"])
            delivery_key = (
                f"delivery-status:{order_id}:{current['public_status']}:{chat}"
            )
            confirmed = db.execute(
                """SELECT 1 FROM business_outbound_deliveries
                   WHERE dedupe_key=? AND chat_id=? AND template_code=?
                     AND business_connection_id=?
                     AND state='sent' AND telegram_message_id=?""",
                (delivery_key, chat, template, connection, message_id),
            ).fetchone()
            if confirmed is None:
                return False

            changed = db.execute(
                """UPDATE delivery_status_notifications SET
                   state='sent',match_outcome='sent',
                   business_connection_id=?,chat_id=?,session_id=?,
                   template_code=?,telegram_message_id=?,next_attempt_at=?,
                   last_error=NULL,processed_at=?,updated_at=?,
                   lease_token=NULL,lease_expires_at=NULL
                   WHERE source_event_id=? AND state='running' AND lease_token=?""",
                (
                    connection,
                    chat,
                    session_id,
                    template,
                    message_id,
                    _iso(now),
                    _iso(now),
                    _iso(now),
                    event_id,
                    lease_token,
                ),
            ).rowcount
            if changed != 1:
                return False

            targets = db.execute(
                """SELECT previous.source_event_id,
                          outbound.telegram_message_id
                   FROM delivery_status_notifications AS previous
                   JOIN business_outbound_deliveries AS outbound
                     ON outbound.dedupe_key=(
                       'delivery-status:' || previous.order_id || ':' ||
                       previous.public_status || ':' || ?)
                    AND outbound.chat_id=?
                    AND outbound.template_code=(
                      'delivery_status_' || previous.public_status)
                    AND outbound.state='sent'
                     AND outbound.telegram_message_id IS NOT NULL
                     AND outbound.telegram_message_id>0
                   WHERE previous.order_id=?
                     AND previous.source_event_id<?
                     AND outbound.telegram_message_id<>?
                     AND (
                       outbound.business_connection_id=?
                       OR (
                         outbound.business_connection_id IS NULL
                         AND previous.state='sent'
                         AND previous.business_connection_id=?
                         AND previous.chat_id=?
                         AND previous.telegram_message_id=
                             outbound.telegram_message_id
                       )
                     )
                   ORDER BY previous.source_event_id""",
                (
                    chat,
                    chat,
                    order_id,
                    event_id,
                    message_id,
                    connection,
                    connection,
                    chat,
                ),
            ).fetchall()
            for target in targets:
                db.execute(
                    """INSERT INTO delivery_status_message_deletions(
                       order_id,business_connection_id,chat_id,
                       telegram_message_id,target_source_event_id,
                       replacement_source_event_id,state,next_attempt_at,
                       created_at,updated_at)
                       VALUES(?,?,?,?,?,?,'pending',?,?,?)
                       ON CONFLICT(business_connection_id,chat_id,
                                   telegram_message_id) DO UPDATE SET
                       replacement_source_event_id=MAX(
                         delivery_status_message_deletions.replacement_source_event_id,
                         excluded.replacement_source_event_id),
                       updated_at=excluded.updated_at""",
                    (
                        order_id,
                        connection,
                        chat,
                        int(target["telegram_message_id"]),
                        int(target["source_event_id"]),
                        event_id,
                        _iso(now),
                        _iso(now),
                        _iso(now),
                    ),
                )
            return True

    @staticmethod
    def _recover_stale_deletions_in_db(
        db: sqlite3.Connection, now: datetime
    ) -> int:
        return db.execute(
            """UPDATE delivery_status_message_deletions
               SET state='retry',next_attempt_at=?,updated_at=?,
                   lease_token=NULL,lease_expires_at=NULL,
                   last_error=COALESCE(
                     last_error,'delivery message deletion lease expired')
               WHERE state='running' AND (
                   lease_expires_at IS NULL
                   OR julianday(lease_expires_at) IS NULL
                   OR julianday(lease_expires_at)<=julianday(?))""",
            (_iso(now), _iso(now), _iso(now)),
        ).rowcount

    def recover_stale_deletions(self, now: datetime) -> int:
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            return self._recover_stale_deletions_in_db(db, now)

    def claim_due_deletions(
        self, now: datetime, *, limit: int = 20, lease_seconds: int = 60
    ) -> list[sqlite3.Row]:
        """Lease due Telegram deletion jobs for confirmed bot-sent messages."""

        claimed: list[sqlite3.Row] = []
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover_stale_deletions_in_db(db, now)
            rows = db.execute(
                """SELECT deletion_id FROM delivery_status_message_deletions
                   WHERE state IN ('pending','retry','deferred')
                     AND julianday(next_attempt_at)<=julianday(?)
                   ORDER BY deletion_id LIMIT ?""",
                (_iso(now), min(100, max(1, int(limit)))),
            ).fetchall()
            for row in rows:
                deletion_id = int(row["deletion_id"])
                token = uuid.uuid4().hex
                changed = db.execute(
                    """UPDATE delivery_status_message_deletions
                       SET state='running',attempts=attempts+1,
                           lease_token=?,lease_expires_at=?,updated_at=?
                       WHERE deletion_id=?
                         AND state IN ('pending','retry','deferred')""",
                    (
                        token,
                        _iso(now + timedelta(seconds=max(30, int(lease_seconds)))),
                        _iso(now),
                        deletion_id,
                    ),
                ).rowcount
                if changed:
                    claimed.append(
                        db.execute(
                            """SELECT * FROM delivery_status_message_deletions
                               WHERE deletion_id=?""",
                            (deletion_id,),
                        ).fetchone()
                    )
        return claimed

    def finish_deletion(
        self,
        deletion_id: int,
        lease_token: str,
        now: datetime,
        outcome: str,
        *,
        error: Exception | str | None = None,
        retry_after: float | None = None,
    ) -> bool:
        """Finish a leased deletion after the caller classifies the API result."""

        if outcome not in {"deleted", "failed", "retry", "deferred"}:
            raise ValueError("invalid delivery message deletion outcome")
        safe_error = (redact_sensitive_data(str(error)) or "")[:500] or None
        state = outcome
        next_attempt_at = _iso(now)
        processed_at: str | None = _iso(now)
        if outcome in {"retry", "deferred"}:
            processed_at = None
            delay = max(
                float(retry_after or 0),
                float(_backoff(self._deletion_attempts(deletion_id))),
            )
            next_attempt_at = _iso(now + timedelta(seconds=delay))
        with connect(self.path) as db:
            return db.execute(
                """UPDATE delivery_status_message_deletions SET
                   state=?,next_attempt_at=?,last_error=?,processed_at=?,
                   updated_at=?,lease_token=NULL,lease_expires_at=NULL
                   WHERE deletion_id=? AND state='running' AND lease_token=?""",
                (
                    state,
                    next_attempt_at,
                    safe_error,
                    processed_at,
                    _iso(now),
                    int(deletion_id),
                    lease_token,
                ),
            ).rowcount == 1

    def _deletion_attempts(self, deletion_id: int) -> int:
        with connect(self.path) as db:
            row = db.execute(
                """SELECT attempts FROM delivery_status_message_deletions
                   WHERE deletion_id=?""",
                (int(deletion_id),),
            ).fetchone()
        return int(row["attempts"] if row else 1)

    def deletion(self, deletion_id: int):
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM delivery_status_message_deletions
                   WHERE deletion_id=?""",
                (int(deletion_id),),
            ).fetchone()

    def _attempts(self, source_event_id: int) -> int:
        with connect(self.path) as db:
            row = db.execute(
                """SELECT attempts FROM delivery_status_notifications
                   WHERE source_event_id=?""",
                (int(source_event_id),),
            ).fetchone()
        return int(row["attempts"] if row else 1)

    def notification(self, source_event_id: int):
        with connect(self.path) as db:
            return db.execute(
                """SELECT * FROM delivery_status_notifications
                   WHERE source_event_id=?""",
                (int(source_event_id),),
            ).fetchone()
