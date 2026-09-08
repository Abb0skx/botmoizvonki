from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.utils.couriers import COURIERS_BY_ID
from telegram_business.delivery_store import DeliveryNotificationStore
from telegram_business.migrations import connect, migrate
from telegram_business.repository import BusinessRepository


TZ = ZoneInfo("Asia/Tashkent")
FEED_ID = "11111111-1111-4111-8111-111111111111"
DEFAULT_COURIER = COURIERS_BY_ID[1799690992]


def _insert_notification(
    path: Path,
    now: datetime,
    *,
    event_id: int,
    order_id: int,
    status: str,
    state: str,
    message_id: int | None = None,
    connection_id: str | None = None,
    chat_id: str | None = None,
    with_sent_ledger: bool = False,
) -> None:
    template_code = f"delivery_status_{status}"
    with connect(path) as db:
        db.execute(
            """INSERT INTO delivery_status_notifications(
               source_event_id,order_id,order_number,public_status,
               phones_json,source_created_at,state,next_attempt_at,
               business_connection_id,chat_id,session_id,template_code,
               telegram_message_id,created_at,updated_at,processed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                order_id,
                str(order_id),
                status,
                "[]",
                now.isoformat(),
                state,
                now.isoformat(),
                connection_id,
                chat_id,
                "session" if chat_id else None,
                template_code if message_id else None,
                message_id,
                now.isoformat(),
                now.isoformat(),
                now.isoformat() if state == "sent" else None,
            ),
        )
        if with_sent_ledger:
            assert message_id and connection_id and chat_id
            db.execute(
                """INSERT INTO business_outbound_deliveries(
                   dedupe_key,chat_id,session_id,business_connection_id,
                   template_code,content_hash,
                   state,telegram_message_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'sent',?,?,?)""",
                (
                    f"delivery-status:{order_id}:{status}:{chat_id}",
                    chat_id,
                    "session",
                    connection_id,
                    template_code,
                    f"hash-{event_id}",
                    message_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )


def _claim_and_confirm_current(
    store: DeliveryNotificationStore,
    path: Path,
    now: datetime,
    *,
    event_id: int,
    order_id: int,
    status: str,
    message_id: int,
) -> bool:
    claimed = store.claim_due(now, limit=10)
    current = next(row for row in claimed if row["source_event_id"] == event_id)
    with connect(path) as db:
        db.execute(
            """INSERT INTO business_outbound_deliveries(
               dedupe_key,chat_id,session_id,business_connection_id,
               template_code,content_hash,
               state,telegram_message_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'sent',?,?,?)""",
            (
                f"delivery-status:{order_id}:{status}:200",
                "200",
                "session",
                "connection",
                f"delivery_status_{status}",
                f"hash-{event_id}",
                message_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )
    return store.finish_sent_and_queue_cleanup(
        event_id,
        current["lease_token"],
        now,
        connection_id="connection",
        chat_id="200",
        session_id="session",
        template_code=f"delivery_status_{status}",
        telegram_message_id=message_id,
    )


def test_confirmed_replacement_queues_only_older_proven_bot_statuses(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    store = DeliveryNotificationStore(path)

    _insert_notification(
        path,
        now,
        event_id=1,
        order_id=10,
        status="pending",
        state="sent",
        message_id=101,
        connection_id="connection",
        chat_id="200",
        with_sent_ledger=True,
    )
    _insert_notification(
        path,
        now,
        event_id=2,
        order_id=10,
        status="picked_up",
        state="sent",
        message_id=102,
        connection_id="connection",
        chat_id="200",
        with_sent_ledger=True,
    )
    # A row without the authoritative outbound-send ledger is not an eligible
    # deletion target, even if its status row was corrupted to look sent.
    _insert_notification(
        path,
        now,
        event_id=3,
        order_id=10,
        status="on_way",
        state="sent",
        message_id=103,
        connection_id="connection",
        chat_id="200",
    )
    _insert_notification(
        path,
        now,
        event_id=4,
        order_id=11,
        status="pending",
        state="sent",
        message_id=104,
        connection_id="connection",
        chat_id="200",
        with_sent_ledger=True,
    )
    _insert_notification(
        path,
        now,
        event_id=5,
        order_id=10,
        status="completed",
        state="pending",
    )

    assert _claim_and_confirm_current(
        store,
        path,
        now,
        event_id=5,
        order_id=10,
        status="completed",
        message_id=105,
    )

    assert store.notification(5)["state"] == "sent"
    with connect(path) as db:
        rows = db.execute(
            """SELECT telegram_message_id,target_source_event_id,
                      replacement_source_event_id,state
               FROM delivery_status_message_deletions
               ORDER BY telegram_message_id"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (101, 1, 5, "pending"),
        (102, 2, 5, "pending"),
    ]


def test_replacement_is_not_finished_without_confirmed_send_ledger(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    store = DeliveryNotificationStore(path)
    _insert_notification(
        path,
        now,
        event_id=1,
        order_id=10,
        status="pending",
        state="pending",
    )
    claimed = store.claim_due(now, limit=1)[0]

    assert not store.finish_sent_and_queue_cleanup(
        1,
        claimed["lease_token"],
        now,
        connection_id="connection",
        chat_id="200",
        session_id="session",
        template_code="delivery_status_pending",
        telegram_message_id=101,
    )
    assert store.notification(1)["state"] == "running"
    with connect(path) as db:
        assert db.execute(
            "SELECT count(*) FROM delivery_status_message_deletions"
        ).fetchone()[0] == 0


def test_legacy_null_connection_receipt_is_not_reused_for_current_send(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    repo = BusinessRepository(path)
    delivery_key = "delivery-status:10:pending:200"

    assert repo.begin_outbound_delivery(
        delivery_key,
        "200",
        "session",
        "delivery_status_pending",
        "hash",
        now,
    ) == "send"
    assert repo.outbound_delivery(delivery_key)["business_connection_id"] is None

    assert repo.begin_outbound_delivery(
        delivery_key,
        "200",
        "session",
        "delivery_status_pending",
        "hash",
        now,
        business_connection_id="connection",
    ) == "blocked"


def test_crashed_confirmed_send_is_discovered_after_source_supersedes_row(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    store = DeliveryNotificationStore(path)

    # Telegram accepted the old status and its outbound ledger was written, but
    # the process died before the message audit and notification were finalized.
    _insert_notification(
        path,
        now,
        event_id=1,
        order_id=10,
        status="pending",
        state="superseded",
    )
    with connect(path) as db:
        db.execute(
            """INSERT INTO business_outbound_deliveries(
               dedupe_key,chat_id,session_id,business_connection_id,
               template_code,content_hash,state,
               telegram_message_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'sent',?,?,?)""",
            (
                "delivery-status:10:pending:200",
                "200",
                "session",
                "connection",
                "delivery_status_pending",
                "hash-old",
                101,
                now.isoformat(),
                now.isoformat(),
            ),
        )
    _insert_notification(
        path,
        now,
        event_id=2,
        order_id=10,
        status="picked_up",
        state="pending",
    )

    assert _claim_and_confirm_current(
        store,
        path,
        now,
        event_id=2,
        order_id=10,
        status="picked_up",
        message_id=102,
    )

    with connect(path) as db:
        cleanup = db.execute(
            """SELECT telegram_message_id,state
               FROM delivery_status_message_deletions"""
        ).fetchall()
    assert [tuple(row) for row in cleanup] == [(101, "pending")]


def test_deletion_lease_retry_and_restart_recovery_are_durable(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    store = DeliveryNotificationStore(path)
    _insert_notification(
        path,
        now,
        event_id=1,
        order_id=10,
        status="pending",
        state="sent",
        message_id=101,
        connection_id="connection",
        chat_id="200",
        with_sent_ledger=True,
    )
    _insert_notification(
        path,
        now,
        event_id=2,
        order_id=10,
        status="picked_up",
        state="pending",
    )
    assert _claim_and_confirm_current(
        store,
        path,
        now,
        event_id=2,
        order_id=10,
        status="picked_up",
        message_id=102,
    )

    first = store.claim_due_deletions(now, limit=1, lease_seconds=30)[0]
    deletion_id = first["deletion_id"]
    restarted = DeliveryNotificationStore(path)
    assert restarted.claim_due_deletions(now + timedelta(seconds=29)) == []
    recovered = restarted.claim_due_deletions(
        now + timedelta(seconds=31), limit=1, lease_seconds=30
    )[0]
    assert recovered["deletion_id"] == deletion_id
    assert recovered["attempts"] == 2

    retry_at = now + timedelta(seconds=31)
    assert restarted.finish_deletion(
        deletion_id,
        recovered["lease_token"],
        retry_at,
        "retry",
        error="temporary timeout",
        retry_after=17,
    )
    assert restarted.claim_due_deletions(retry_at + timedelta(seconds=16)) == []
    final_claim = restarted.claim_due_deletions(
        retry_at + timedelta(seconds=17), limit=1
    )[0]
    assert not restarted.finish_deletion(
        deletion_id,
        "wrong-lease",
        retry_at + timedelta(seconds=17),
        "deleted",
    )
    assert restarted.finish_deletion(
        deletion_id,
        final_claim["lease_token"],
        retry_at + timedelta(seconds=17),
        "deleted",
    )
    finished = restarted.deletion(deletion_id)
    assert finished["state"] == "deleted"
    assert finished["processed_at"] is not None
    assert restarted.claim_due_deletions(retry_at + timedelta(hours=1)) == []


def test_feed_reset_preserves_already_queued_message_deletions(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    store = DeliveryNotificationStore(path)
    assert store.reconcile_feed(FEED_ID, 0, False, now) is False
    _insert_notification(
        path,
        now,
        event_id=1,
        order_id=10,
        status="pending",
        state="sent",
        message_id=101,
        connection_id="connection",
        chat_id="200",
        with_sent_ledger=True,
    )
    _insert_notification(
        path,
        now,
        event_id=2,
        order_id=10,
        status="completed",
        state="pending",
    )
    assert _claim_and_confirm_current(
        store,
        path,
        now,
        event_id=2,
        order_id=10,
        status="completed",
        message_id=102,
    )

    replacement_feed = "22222222-2222-4222-8222-222222222222"
    assert store.reconcile_feed(
        replacement_feed,
        0,
        True,
        now + timedelta(seconds=1),
    ) is False

    with connect(path) as db:
        assert db.execute(
            "SELECT count(*) FROM delivery_status_notifications"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM business_outbound_deliveries"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM delivery_status_message_deletions"
        ).fetchone()[0] == 1
    claimed = DeliveryNotificationStore(path).claim_due_deletions(
        now + timedelta(seconds=1), limit=1
    )
    assert claimed[0]["telegram_message_id"] == 101


def test_cleanup_migration_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    migrate(path)
    with connect(path) as db:
        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(delivery_status_message_deletions)"
            )
        }
        assert {
            "deletion_id",
            "order_id",
            "business_connection_id",
            "chat_id",
            "telegram_message_id",
            "target_source_event_id",
            "replacement_source_event_id",
            "state",
            "attempts",
            "next_attempt_at",
            "lease_token",
            "lease_expires_at",
            "last_error",
            "processed_at",
        } <= columns
        outbound_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(business_outbound_deliveries)"
            )
        }
        assert "business_connection_id" in outbound_columns


def test_event_identity_migration_preserves_legacy_sent_row_and_dedupe(tmp_path):
    path = tmp_path / "business.db"
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE delivery_status_notifications (
               source_event_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
               order_number TEXT NOT NULL, public_status TEXT NOT NULL,
               product TEXT, phones_json TEXT NOT NULL DEFAULT '[]',
               source_created_at TEXT, courier_id INTEGER, courier_name TEXT,
               courier_phone TEXT, outbound_dedupe_key TEXT,
               state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
               next_attempt_at TEXT NOT NULL, lease_token TEXT, lease_expires_at TEXT,
               match_outcome TEXT, business_connection_id TEXT, chat_id TEXT,
               session_id TEXT, template_code TEXT, telegram_message_id INTEGER,
               last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
               processed_at TEXT, UNIQUE(order_id, public_status))"""
        )
        db.execute(
            """INSERT INTO delivery_status_notifications(
               source_event_id,order_id,order_number,public_status,product,
               phones_json,source_created_at,courier_id,courier_name,courier_phone,
               outbound_dedupe_key,state,attempts,next_attempt_at,match_outcome,
               business_connection_id,chat_id,session_id,template_code,
               telegram_message_id,created_at,updated_at,processed_at)
               VALUES(7,44,'private-order','on_way','phone','["+998901112233"]',
                      ?,?,?,?,?,'sent',2,?,'sent','connection','200','session',
                      'delivery_status_on_way',7011,?,?,?)""",
            (
                now,
                DEFAULT_COURIER.user_id,
                DEFAULT_COURIER.name,
                DEFAULT_COURIER.phone,
                "delivery-status:44:on_way:200",
                now,
                now,
                now,
                now,
            ),
        )

    migrate(path)
    migrate(path)

    with connect(path) as db:
        row = db.execute(
            """SELECT * FROM delivery_status_notifications
               WHERE source_event_id=7"""
        ).fetchone()
        assert row["state"] == "sent"
        assert row["telegram_message_id"] == 7011
        assert row["courier_id"] == DEFAULT_COURIER.user_id
        assert row["courier_name"] == DEFAULT_COURIER.name
        assert row["courier_phone"] == DEFAULT_COURIER.phone
        assert row["outbound_dedupe_key"] == "delivery-status:44:on_way:200"
        unique_indexes = []
        for index in db.execute(
            "PRAGMA index_list(delivery_status_notifications)"
        ):
            if int(index["unique"]):
                unique_indexes.append(
                    tuple(
                        item["name"]
                        for item in db.execute(
                            f"PRAGMA index_info({index['name']})"
                        )
                    )
                )
        assert ("order_id", "public_status") not in unique_indexes
        db.execute(
            """INSERT INTO delivery_status_notifications(
               source_event_id,order_id,order_number,public_status,phones_json,
               state,next_attempt_at,created_at,updated_at)
               VALUES(8,44,'private-order','on_way','[]','pending',?,?,?)""",
            (now, now, now),
        )

    store = DeliveryNotificationStore(path)
    assert store.outbound_key_for(7, 44, "on_way", "200") == (
        "delivery-status:44:on_way:200"
    )
    assert store.outbound_key_for(8, 44, "on_way", "200") == (
        "delivery-status:44:on_way:8:200"
    )


def test_event_identity_migration_upgrades_exact_previous_release_schema(tmp_path):
    path = tmp_path / "business.db"
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE business_integration_state (
               key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        db.execute(
            """INSERT INTO business_integration_state(key,value,updated_at)
               VALUES('delivery_status_event_cursor','20',?)""",
            (now,),
        )
        db.execute(
            """CREATE TABLE delivery_status_notifications (
               source_event_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
               order_number TEXT NOT NULL, public_status TEXT NOT NULL,
               product TEXT, phones_json TEXT NOT NULL DEFAULT '[]',
               source_created_at TEXT,
               state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
               next_attempt_at TEXT NOT NULL, lease_token TEXT, lease_expires_at TEXT,
               match_outcome TEXT, business_connection_id TEXT, chat_id TEXT,
               session_id TEXT, template_code TEXT, telegram_message_id INTEGER,
               last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
               processed_at TEXT, UNIQUE(order_id, public_status))"""
        )
        db.execute(
            """INSERT INTO delivery_status_notifications(
               source_event_id,order_id,order_number,public_status,phones_json,
               state,next_attempt_at,match_outcome,business_connection_id,
               chat_id,session_id,template_code,telegram_message_id,
               created_at,updated_at,processed_at)
               VALUES(9,45,'private-order','picked_up','[]','retry',?,NULL,
                      'connection','201','session','delivery_status_picked_up',
                      NULL,?,?,NULL)""",
            (now, now, now),
        )

    migrate(path)

    with connect(path) as db:
        row = db.execute(
            """SELECT * FROM delivery_status_notifications
               WHERE source_event_id=9"""
        ).fetchone()
        assert row["state"] == "retry"
        assert row["telegram_message_id"] is None
        assert row["courier_id"] is None
        assert row["courier_name"] is None
        assert row["courier_phone"] is None
        assert row["outbound_dedupe_key"] == (
            "delivery-status:45:picked_up:201"
        )
        cursor = db.execute(
            """SELECT value FROM business_integration_state
               WHERE key='delivery_status_event_cursor'"""
        ).fetchone()
        assert cursor["value"] == "8"

    store = DeliveryNotificationStore(path)
    assert store.import_page(
        8,
        9,
        [
            {
                "event_id": 9,
                "order_id": 45,
                "order_number": "private-order",
                "status": "picked_up",
                "current_status": "picked_up",
                "product": "phone",
                "phones": (),
                "courier_id": DEFAULT_COURIER.user_id,
                "courier_name": DEFAULT_COURIER.name,
                "courier_phone": DEFAULT_COURIER.phone,
                "created_at": now,
            }
        ],
        datetime.fromisoformat(now),
    ) == 0
    refreshed = store.notification(9)
    assert refreshed["state"] == "retry"
    assert refreshed["courier_id"] == DEFAULT_COURIER.user_id
    assert refreshed["courier_name"] == DEFAULT_COURIER.name
    assert refreshed["courier_phone"] == DEFAULT_COURIER.phone


def test_local_cleanup_audit_cannot_tombstone_client_or_manager_messages(tmp_path):
    path = tmp_path / "business.db"
    migrate(path)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    rows = (
        (101, "incoming", "client", None, None),
        (102, "outgoing", "manager", None, None),
        (103, "outgoing", "business_bot", "greeting_model", None),
        (
            104,
            "outgoing",
            "business_bot",
            "delivery_status_pending",
            "delivery_order:10",
        ),
        (
            105,
            "outgoing",
            "business_bot",
            "delivery_status_pending",
            "delivery_order:11",
        ),
    )
    with connect(path) as db:
        for message_id, direction, sender, template, model_query in rows:
            db.execute(
                """INSERT INTO business_messages(
                   business_connection_id,chat_id,message_id,direction,
                   sender_type,message_type,template_code,model_query,
                   telegram_date,created_at)
                   VALUES('connection','200',?,?,?,?,?,?,?,?)""",
                (
                    message_id,
                    direction,
                    sender,
                    "text",
                    template,
                    model_query,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    repo = BusinessRepository(path)
    assert not repo.mark_delivery_status_message_deleted(
        "connection", "200", 101, 10, now
    )
    assert not repo.mark_delivery_status_message_deleted(
        "connection", "200", 102, 10, now
    )
    assert not repo.mark_delivery_status_message_deleted(
        "connection", "200", 103, 10, now
    )
    assert not repo.mark_delivery_status_message_deleted(
        "connection", "200", 105, 10, now
    )
    assert repo.mark_delivery_status_message_deleted(
        "connection", "200", 104, 10, now
    )

    with connect(path) as db:
        saved = db.execute(
            """SELECT message_id,deleted_at FROM business_messages
               ORDER BY message_id"""
        ).fetchall()
    assert [row["deleted_at"] is not None for row in saved] == [
        False,
        False,
        False,
        True,
        False,
    ]
