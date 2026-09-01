from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS business_connections (
 connection_id TEXT PRIMARY KEY, business_user_id TEXT, is_enabled INTEGER NOT NULL DEFAULT 1,
 can_reply INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_updates (
 update_id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, business_connection_id TEXT, chat_id TEXT,
 message_id INTEGER, raw_payload TEXT NOT NULL, received_at TEXT NOT NULL, processed_at TEXT,
 status TEXT NOT NULL DEFAULT 'new', error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT, lease_token TEXT, lease_expires_at TEXT, last_attempt_at TEXT,
 callback_query_id TEXT, callback_message_id INTEGER,
 UNIQUE(business_connection_id, chat_id, message_id, event_type));
CREATE TABLE IF NOT EXISTS business_clients (
 chat_id TEXT PRIMARY KEY, telegram_user_id TEXT, first_name TEXT, last_name TEXT, username TEXT,
 language TEXT, language_confidence REAL, bot_paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT,
 manager_lock_until TEXT, last_credit_reply_at TEXT, last_ack_at TEXT, last_client_message_at TEXT,
 phone TEXT, phone_verified INTEGER NOT NULL DEFAULT 0, phone_owner_user_id TEXT,
 phone_source TEXT, phone_verified_at TEXT, location_phone_pending INTEGER NOT NULL DEFAULT 0,
 location_phone_requested_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_sessions (
 session_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, business_date TEXT NOT NULL, started_at TEXT NOT NULL,
 last_client_message_at TEXT, greeting_sent INTEGER NOT NULL DEFAULT 0, price_sent INTEGER NOT NULL DEFAULT 0,
 final_sent INTEGER NOT NULL DEFAULT 0, location_received INTEGER NOT NULL DEFAULT 0,
 order_intent INTEGER NOT NULL DEFAULT 0, credit_intent INTEGER NOT NULL DEFAULT 0,
 needs_manager_reply INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 0, handoff_reason TEXT,
 model_query TEXT, matched_model TEXT, memory TEXT, color TEXT, location_url TEXT, preferred_time TEXT,
 failed_searches INTEGER NOT NULL DEFAULT 0, search_disabled INTEGER NOT NULL DEFAULT 0,
 automation_handoff INTEGER NOT NULL DEFAULT 0, last_search_hash TEXT, last_search_at TEXT,
 status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(chat_id, business_date));
CREATE TABLE IF NOT EXISTS business_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, business_connection_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 message_id INTEGER NOT NULL, session_id TEXT, cycle_id TEXT, direction TEXT NOT NULL, sender_type TEXT NOT NULL,
 message_type TEXT NOT NULL, text TEXT, caption TEXT, file_id TEXT, reply_to_message_id INTEGER,
 language TEXT, intent TEXT, model_query TEXT, template_code TEXT, telegram_date TEXT,
 created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT, update_id INTEGER,
 edit_update_id INTEGER,
 original_received INTEGER NOT NULL DEFAULT 1,
 UNIQUE(business_connection_id, chat_id, message_id));
CREATE TABLE IF NOT EXISTS business_manager_fences (
 business_connection_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
 telegram_date TEXT NOT NULL, update_id INTEGER NOT NULL UNIQUE, received_at TEXT NOT NULL,
 PRIMARY KEY(business_connection_id, chat_id, message_id));
CREATE TABLE IF NOT EXISTS business_outbound_deliveries (
 dedupe_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, session_id TEXT,
 template_code TEXT NOT NULL, content_hash TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
 telegram_message_id INTEGER, last_error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS response_cycles (
 cycle_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, session_id TEXT, first_client_at TEXT NOT NULL,
 last_client_at TEXT NOT NULL, first_bot_at TEXT, first_manager_at TEXT, bot_response_seconds INTEGER,
 calendar_response_seconds INTEGER, work_response_seconds INTEGER, needs_manager_reply INTEGER NOT NULL DEFAULT 1,
 manager_due_at TEXT, closed_at TEXT, status TEXT NOT NULL DEFAULT 'waiting_manager');
CREATE TABLE IF NOT EXISTS scheduled_actions (
 action_id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE, chat_id TEXT NOT NULL,
 session_id TEXT, action_type TEXT NOT NULL, execute_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 executed_at TEXT, last_error TEXT, generation INTEGER NOT NULL DEFAULT 1,
 lease_token TEXT, lease_expires_at TEXT, next_attempt_at TEXT);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_actions(status, execute_at);
CREATE TABLE IF NOT EXISTS sheets_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 operation TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL, synced_at TEXT,
 lease_token TEXT, lease_expires_at TEXT, generation INTEGER NOT NULL DEFAULT 1,
 UNIQUE(entity_type, entity_id, operation));
CREATE TABLE IF NOT EXISTS business_runtime_leases (
 lease_name TEXT PRIMARY KEY, lease_token TEXT NOT NULL,
 lease_expires_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_requests (
 request_id TEXT PRIMARY KEY, business_connection_id TEXT, chat_id TEXT NOT NULL,
 session_id TEXT, cycle_id TEXT, business_date TEXT, origin_update_id INTEGER,
 status TEXT NOT NULL DEFAULT 'collecting', wizard_state TEXT NOT NULL DEFAULT 'model',
 revision INTEGER NOT NULL DEFAULT 1, language TEXT,
 exact_model TEXT, model_key TEXT, model_url TEXT,
 option_kind TEXT, option_value TEXT, color TEXT,
 color_any INTEGER NOT NULL DEFAULT 0,
 selection_fields TEXT NOT NULL DEFAULT '{}',
 fulfillment_method TEXT, phone TEXT, contact_method TEXT,
 location_url TEXT, address TEXT, preferred_time TEXT,
 database_price TEXT, source_updated_at TEXT,
 needs_manager_reply INTEGER NOT NULL DEFAULT 1,
 opened_at TEXT NOT NULL, last_client_at TEXT, last_client_message_id INTEGER,
 submitted_at TEXT, closed_at TEXT, close_reason TEXT,
 closed_by_message_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(status IN ('collecting','ready','submitted','manager_closed','cancelled','expired')),
 CHECK(fulfillment_method IS NULL OR fulfillment_method IN ('delivery','pickup')));
CREATE UNIQUE INDEX IF NOT EXISTS idx_business_requests_one_active_chat
 ON business_requests(chat_id)
 WHERE status IN ('collecting','ready');
CREATE INDEX IF NOT EXISTS idx_business_requests_session
 ON business_requests(session_id,status,updated_at);
CREATE TABLE IF NOT EXISTS business_request_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
 request_revision INTEGER NOT NULL, event_type TEXT NOT NULL,
 actor_type TEXT NOT NULL, event_key TEXT, payload TEXT NOT NULL DEFAULT '{}',
 telegram_update_id INTEGER, telegram_message_id INTEGER,
 created_at TEXT NOT NULL,
 FOREIGN KEY(request_id) REFERENCES business_requests(request_id),
 UNIQUE(request_id,event_key));
CREATE INDEX IF NOT EXISTS idx_business_request_events_request
 ON business_request_events(request_id,event_id);
CREATE TABLE IF NOT EXISTS business_callback_tokens (
 token_hash TEXT PRIMARY KEY, request_id TEXT NOT NULL,
 request_revision INTEGER NOT NULL, action TEXT NOT NULL,
 payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'issued',
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
 consumed_at TEXT, revoked_at TEXT, revoke_reason TEXT,
 FOREIGN KEY(request_id) REFERENCES business_requests(request_id),
 CHECK(state IN ('issued','consumed','revoked','expired')));
CREATE INDEX IF NOT EXISTS idx_business_callback_tokens_request
 ON business_callback_tokens(request_id,state,request_revision,expires_at);
CREATE TABLE IF NOT EXISTS business_callback_receipts (
 receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
 callback_query_id TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL,
 request_id TEXT, request_revision INTEGER, chat_id TEXT,
 action TEXT, payload TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL, outcome TEXT NOT NULL,
 result_revision INTEGER, result TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, processed_at TEXT,
 CHECK(status IN ('claimed','applied','rejected')));
CREATE INDEX IF NOT EXISTS idx_business_callback_receipts_request
 ON business_callback_receipts(request_id,receipt_id);
CREATE TABLE IF NOT EXISTS business_errors (
 error_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, operation TEXT, chat_id TEXT, session_id TEXT,
 error_type TEXT, message TEXT, attempts INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_model_choices (
 session_id TEXT NOT NULL, choice_number INTEGER NOT NULL, model_name TEXT NOT NULL,
 model_url TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(session_id, choice_number));
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def migrate(path: Path | str) -> None:
    with connect(path) as db:
        db.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS cannot evolve databases created by an older
        # release.  Additive migrations keep existing calls/messages untouched.
        _ensure_columns(
            db,
            "business_updates",
            {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "TEXT",
                "lease_token": "TEXT",
                "lease_expires_at": "TEXT",
                "last_attempt_at": "TEXT",
                "callback_query_id": "TEXT",
                "callback_message_id": "INTEGER",
            },
        )
        _ensure_columns(
            db,
            "business_messages",
            {
                "update_id": "INTEGER",
                "edit_update_id": "INTEGER",
                "original_received": "INTEGER NOT NULL DEFAULT 1",
            },
        )
        _ensure_columns(db, "response_cycles", {"manager_due_at": "TEXT"})
        _ensure_columns(
            db,
            "business_clients",
            {
                "last_ack_at": "TEXT",
                "last_client_message_at": "TEXT",
                "phone": "TEXT",
                "phone_verified": "INTEGER NOT NULL DEFAULT 0",
                "phone_owner_user_id": "TEXT",
                "phone_source": "TEXT",
                "phone_verified_at": "TEXT",
                "location_phone_pending": "INTEGER NOT NULL DEFAULT 0",
                "location_phone_requested_at": "TEXT",
            },
        )
        _ensure_columns(
            db,
            "business_sessions",
            {
                "automation_handoff": "INTEGER NOT NULL DEFAULT 0",
                "last_search_hash": "TEXT",
                "last_search_at": "TEXT",
            },
        )
        _ensure_columns(
            db,
            "scheduled_actions",
            {
                "generation": "INTEGER NOT NULL DEFAULT 1",
                "lease_token": "TEXT",
                "lease_expires_at": "TEXT",
                "next_attempt_at": "TEXT",
            },
        )
        _ensure_columns(
            db,
            "sheets_outbox",
            {
                "lease_token": "TEXT",
                "lease_expires_at": "TEXT",
                "generation": "INTEGER NOT NULL DEFAULT 1",
            },
        )
        _ensure_columns(
            db,
            "business_callback_receipts",
            {"result": "TEXT NOT NULL DEFAULT '{}'"},
        )
        _ensure_columns(
            db,
            "business_requests",
            {"origin_update_id": "INTEGER"},
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_business_updates_due "
            "ON business_updates(status, next_attempt_at, received_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sheets_outbox_due "
            "ON sheets_outbox(status, next_attempt_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_business_messages_bot_ledger "
            "ON business_messages(sender_type, template_code, session_id, chat_id, created_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_business_manager_fences_chat_date "
            "ON business_manager_fences(chat_id, telegram_date, message_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_business_outbound_credit "
            "ON business_outbound_deliveries(chat_id, template_code, created_at, state)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_response_cycles_waiting "
            "ON response_cycles(chat_id, status, first_client_at)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_business_updates_callback_query "
            "ON business_updates(callback_query_id) WHERE callback_query_id IS NOT NULL"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_business_requests_origin_update "
            "ON business_requests(origin_update_id) WHERE origin_update_id IS NOT NULL"
        )
        # Submitted requests remain manager work, but must not prevent the same
        # customer from starting a new draft in a later night session.
        db.execute("DROP INDEX IF EXISTS idx_business_requests_one_active_chat")
        db.execute(
            "CREATE UNIQUE INDEX idx_business_requests_one_active_chat "
            "ON business_requests(chat_id) "
            "WHERE status IN ('collecting','ready')"
        )


def _ensure_columns(
    db: sqlite3.Connection, table: str, definitions: dict[str, str]
) -> None:
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in definitions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
