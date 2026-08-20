import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import requests

from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)

from fastapi.responses import HTMLResponse

from instagram_bot import router as instagram_router


# =========================================================
# APP
# =========================================================

app = FastAPI()

app.include_router(
    instagram_router
)


# =========================================================
# CONFIG
# =========================================================

UZ_TZ = timezone(
    timedelta(hours=5)
)

BASE_DIR = Path(
    __file__
).resolve().parent

ENV_FILE = (
    BASE_DIR
    / "data"
    / ".env"
)

load_dotenv(
    dotenv_path=ENV_FILE,
    override=True,
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID"
)

TELEGRAM_WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    "",
)

MOIZVONKI_API_URL = os.getenv(
    "MOIZVONKI_API_URL",
    "",
).rstrip("/")

MOIZVONKI_USER_NAME = os.getenv(
    "MOIZVONKI_USER_NAME",
    "",
)

MOIZVONKI_API_KEY = os.getenv(
    "MOIZVONKI_API_KEY",
    "",
)

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "",
).rstrip("/")

SMS_COOLDOWN_DAYS = 30

SMS_TEXT = """TEXNIKACH

Все актуальные цены, модели и каталог:
https://t.me/texnikach

Для заказа напишите менеджеру:
https://t.me/texnikach_admin

Полная информация о нас:
https://texnikach.uz/go

------------------------

Barcha aktual narxlar, modellar va katalog:
https://t.me/texnikach

Buyurtma berish uchun menejerga yozing:
https://t.me/texnikach_admin

Biz haqimizda to‘liq ma’lumot:
https://texnikach.uz/go"""

RATING_SMS_TEXT = """TEXNIKACH
Оцените звонок от 1 до 5.
Qo‘ng‘iroqni 1 dan 5 gacha baholang.
{rating_url}
1 — плохо/yomon, 5 — отлично/a’lo"""

DB_PATH = Path(
    os.getenv(
        "DB_PATH",
        "/app/data/calls.db",
    )
)

DB_PATH.parent.mkdir(
    parents=True,
    exist_ok=True,
)

HTTP = requests.Session()


print(
    "ENV EXISTS:",
    ENV_FILE.exists(),
)

print(
    "TOKEN EXISTS:",
    bool(
        TELEGRAM_BOT_TOKEN
    ),
)

print(
    "CHAT ID EXISTS:",
    bool(
        TELEGRAM_CHAT_ID
    ),
)

print(
    "DATABASE:",
    DB_PATH,
)

print(
    "PUBLIC BASE URL EXISTS:",
    bool(
        PUBLIC_BASE_URL
    ),
)


# =========================================================
# MANAGERS
# =========================================================

MANAGERS = {
    "olmas": "Olmas",
    "otabek": "Otabek",
    "ali": "Ali",
    "abbos": "Abbos",
}


# =========================================================
# INTERNAL CONTACTS
# =========================================================

INTERNAL_CONTACTS = {
    "998901333999": "Abbos",

    "998998342889": "Muzrob (Курьер)",
    "998948765070": "Muzrob (Курьер)",

    "998909045502": "Ali",

    "998900979898": "Olmas",

    "998998137406": "Otabek",
}


# =========================================================
# SALE RESULTS
# =========================================================

SALE_REASONS = {
    "no_stock":
        "📦 Нет товара",

    "price":
        "💰 Не устроила цена",

    "price_changed":
        "💰 Цена изменилась",

    "thinking":
        "🤔 Думает / сравнивает",

    "other_product":
        "🔎 Ищет другой товар",

    "credit":
        "🔎 Хочет на кредит",

    "visit_store":
        "🏪 Хочет прийти в магазин",

    "later":
        "⏳ Купит позже",

    "bought_elsewhere":
        "🏪 Купил в другом месте",

    "conditions":
        "🚚 Не подошли условия",

    "not_target":
        "🚫 Не целевой звонок",

    "other":
        "📝 Другая причина",
}


# =========================================================
# DATABASE
# =========================================================

def connect_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA busy_timeout = 30000"
    )

    return conn


def normalize_phone(
    phone: str | None,
) -> str:

    if not phone:
        return ""

    return "".join(
        char
        for char in str(phone)
        if char.isdigit()
    )


def get_internal_contact_name(
    phone: str | None,
):

    phone_key = normalize_phone(
        phone
    )

    if not phone_key:
        return None

    return INTERNAL_CONTACTS.get(
        phone_key
    )


def init_db():

    with connect_db() as conn:

        conn.execute(
            "PRAGMA journal_mode = WAL"
        )

        conn.execute(
            "PRAGMA synchronous = NORMAL"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                db_call_id INTEGER UNIQUE,

                event_pbx_call_id TEXT,

                client_number TEXT,
                client_key TEXT,
                client_name TEXT,

                is_internal_contact INTEGER
                    DEFAULT 0,

                internal_contact_name TEXT,

                direction INTEGER,
                answered INTEGER,

                user_id INTEGER,
                user_login TEXT,

                src_number TEXT,
                src_id INTEGER,
                src_slot INTEGER,

                event_created TEXT,

                start_time INTEGER,
                answer_time INTEGER,
                end_time INTEGER,

                duration INTEGER,
                api_duration INTEGER,
                duration_source TEXT,

                recording TEXT,

                account_id TEXT,
                account_name TEXT,

                telegram_sent INTEGER
                    DEFAULT 0,

                telegram_chat_id TEXT,
                telegram_message_id INTEGER,

                sms_sent INTEGER
                    DEFAULT 0,

                sms_sent_at INTEGER,
                sms_error TEXT,

                talk_manager_code TEXT,
                talk_manager_name TEXT,

                manager_marked_at INTEGER,
                manager_marked_by INTEGER,
                manager_marked_username TEXT,

                sale_status TEXT,

                no_sale_reason TEXT,
                no_sale_reason_code TEXT,

                sale_marked_at INTEGER,
                sale_marked_by INTEGER,
                sale_marked_username TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        columns = {
            row["name"]
            for row in conn.execute(
                """
                PRAGMA table_info(calls)
                """
            ).fetchall()
        }

        migrations = {

            "client_key":
                """
                ALTER TABLE calls
                ADD COLUMN client_key TEXT
                """,

            "api_duration":
                """
                ALTER TABLE calls
                ADD COLUMN api_duration INTEGER
                """,

            "duration_source":
                """
                ALTER TABLE calls
                ADD COLUMN duration_source TEXT
                """,

            "telegram_sent":
                """
                ALTER TABLE calls
                ADD COLUMN telegram_sent INTEGER
                DEFAULT 0
                """,

            "telegram_chat_id":
                """
                ALTER TABLE calls
                ADD COLUMN telegram_chat_id TEXT
                """,

            "telegram_message_id":
                """
                ALTER TABLE calls
                ADD COLUMN telegram_message_id INTEGER
                """,

            "sms_sent":
                """
                ALTER TABLE calls
                ADD COLUMN sms_sent INTEGER
                DEFAULT 0
                """,

            "sms_sent_at":
                """
                ALTER TABLE calls
                ADD COLUMN sms_sent_at INTEGER
                """,

            "sms_error":
                """
                ALTER TABLE calls
                ADD COLUMN sms_error TEXT
                """,

            "sale_status":
                """
                ALTER TABLE calls
                ADD COLUMN sale_status TEXT
                """,

            "no_sale_reason":
                """
                ALTER TABLE calls
                ADD COLUMN no_sale_reason TEXT
                """,

            "no_sale_reason_code":
                """
                ALTER TABLE calls
                ADD COLUMN no_sale_reason_code TEXT
                """,

            "sale_marked_at":
                """
                ALTER TABLE calls
                ADD COLUMN sale_marked_at INTEGER
                """,

            "sale_marked_by":
                """
                ALTER TABLE calls
                ADD COLUMN sale_marked_by INTEGER
                """,

            "sale_marked_username":
                """
                ALTER TABLE calls
                ADD COLUMN sale_marked_username TEXT
                """,

            "is_internal_contact":
                """
                ALTER TABLE calls
                ADD COLUMN is_internal_contact INTEGER
                DEFAULT 0
                """,

            "internal_contact_name":
                """
                ALTER TABLE calls
                ADD COLUMN internal_contact_name TEXT
                """,

            "talk_manager_code":
                """
                ALTER TABLE calls
                ADD COLUMN talk_manager_code TEXT
                """,

            "talk_manager_name":
                """
                ALTER TABLE calls
                ADD COLUMN talk_manager_name TEXT
                """,

            "manager_marked_at":
                """
                ALTER TABLE calls
                ADD COLUMN manager_marked_at INTEGER
                """,

            "manager_marked_by":
                """
                ALTER TABLE calls
                ADD COLUMN manager_marked_by INTEGER
                """,

            "manager_marked_username":
                """
                ALTER TABLE calls
                ADD COLUMN manager_marked_username TEXT
                """,
        }

        for column, sql in migrations.items():

            if column not in columns:

                conn.execute(
                    sql
                )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_history (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                call_id INTEGER,

                client_number TEXT
                    NOT NULL,

                client_key TEXT
                    NOT NULL,

                sender_user_login TEXT,

                status TEXT
                    NOT NULL,

                reserved_at INTEGER
                    NOT NULL,

                sent_at INTEGER,
                error TEXT,
                provider_response TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(call_id)
                    REFERENCES calls(id)
            )
            """
        )

        sms_history_columns = {
            row["name"]
            for row in conn.execute(
                """
                PRAGMA table_info(sms_history)
                """
            ).fetchall()
        }

        if (
            "sender_user_login"
            not in sms_history_columns
        ):
            conn.execute(
                """
                ALTER TABLE sms_history
                ADD COLUMN sender_user_login TEXT
                """
            )

        # -------------------------------------------------
        # OLD PLAIN-TEXT SUCCESS RESPONSES
        # -------------------------------------------------

        conn.execute(
            """
            UPDATE sms_history

            SET
                status = 'sent',
                sent_at = COALESCE(
                    sent_at,
                    reserved_at
                ),
                error = NULL,
                provider_response = COALESCE(
                    provider_response,
                    '{"success":true,"status":"SMS posted"}'
                )

            WHERE
                status = 'error'

                AND

                error LIKE '%SMS posted%'
            """
        )

        conn.execute(
            """
            UPDATE calls

            SET
                sms_sent = 1,
                sms_sent_at = COALESCE(
                    sms_sent_at,
                    (
                        SELECT MAX(
                            history.sent_at
                        )

                        FROM sms_history AS history

                        WHERE
                            history.call_id = calls.id
                            AND history.status = 'sent'
                    )
                ),
                sms_error = NULL

            WHERE EXISTS (
                SELECT 1

                FROM sms_history AS history

                WHERE
                    history.call_id = calls.id
                    AND history.status = 'sent'
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS call_ratings (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                call_id INTEGER
                    NOT NULL
                    UNIQUE,

                token_hash TEXT
                    NOT NULL
                    UNIQUE,

                client_key TEXT
                    NOT NULL,

                sender_user_login TEXT,

                sms_status TEXT
                    NOT NULL,

                sms_reserved_at INTEGER
                    NOT NULL,

                sms_sent_at INTEGER,
                sms_error TEXT,
                provider_response TEXT,

                score INTEGER
                    CHECK (
                        score BETWEEN 1 AND 5
                    ),

                rated_at INTEGER,
                expires_at INTEGER NOT NULL,

                first_opened_at INTEGER,
                last_opened_at INTEGER,
                open_count INTEGER DEFAULT 0,

                first_ip TEXT,
                last_ip TEXT,
                rated_ip TEXT,

                user_agent TEXT,
                accept_language TEXT,
                referer TEXT,

                request_headers_json TEXT,
                device_data_json TEXT,
                device_data_updated_at INTEGER,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(call_id)
                    REFERENCES calls(id)
            )
            """
        )

        rating_columns = {
            row["name"]
            for row in conn.execute(
                """
                PRAGMA table_info(call_ratings)
                """
            ).fetchall()
        }

        rating_migrations = {
            "first_opened_at":
                """
                ALTER TABLE call_ratings
                ADD COLUMN first_opened_at INTEGER
                """,

            "last_opened_at":
                """
                ALTER TABLE call_ratings
                ADD COLUMN last_opened_at INTEGER
                """,

            "open_count":
                """
                ALTER TABLE call_ratings
                ADD COLUMN open_count INTEGER DEFAULT 0
                """,

            "first_ip":
                """
                ALTER TABLE call_ratings
                ADD COLUMN first_ip TEXT
                """,

            "last_ip":
                """
                ALTER TABLE call_ratings
                ADD COLUMN last_ip TEXT
                """,

            "rated_ip":
                """
                ALTER TABLE call_ratings
                ADD COLUMN rated_ip TEXT
                """,

            "user_agent":
                """
                ALTER TABLE call_ratings
                ADD COLUMN user_agent TEXT
                """,

            "accept_language":
                """
                ALTER TABLE call_ratings
                ADD COLUMN accept_language TEXT
                """,

            "referer":
                """
                ALTER TABLE call_ratings
                ADD COLUMN referer TEXT
                """,

            "request_headers_json":
                """
                ALTER TABLE call_ratings
                ADD COLUMN request_headers_json TEXT
                """,

            "device_data_json":
                """
                ALTER TABLE call_ratings
                ADD COLUMN device_data_json TEXT
                """,

            "device_data_updated_at":
                """
                ALTER TABLE call_ratings
                ADD COLUMN device_data_updated_at INTEGER
                """,
        }

        for column, sql in (
            rating_migrations.items()
        ):
            if column not in rating_columns:
                conn.execute(
                    sql
                )

        # -------------------------------------------------
        # NORMALIZE OLD CLIENT NUMBERS
        # -------------------------------------------------

        old_rows = conn.execute(
            """
            SELECT
                id,
                client_number

            FROM calls

            WHERE
                client_key IS NULL

                OR

                client_key = ''
            """
        ).fetchall()

        for row in old_rows:

            conn.execute(
                """
                UPDATE calls

                SET client_key = ?

                WHERE id = ?
                """,
                (
                    normalize_phone(
                        row["client_number"]
                    ),

                    row["id"],
                ),
            )

        # -------------------------------------------------
        # MARK INTERNAL CONTACTS IN OLD CALLS
        # -------------------------------------------------

        all_rows = conn.execute(
            """
            SELECT
                id,
                client_number

            FROM calls
            """
        ).fetchall()

        for row in all_rows:

            contact_name = (
                get_internal_contact_name(
                    row["client_number"]
                )
            )

            if contact_name:

                conn.execute(
                    """
                    UPDATE calls

                    SET
                        is_internal_contact = 1,
                        internal_contact_name = ?

                    WHERE id = ?
                    """,
                    (
                        contact_name,
                        row["id"],
                    ),
                )

        # -------------------------------------------------
        # OLD TELEGRAM RECORDS
        # -------------------------------------------------

        conn.execute(
            """
            UPDATE calls

            SET telegram_sent = 1

            WHERE telegram_sent IS NULL
            """
        )

        # -------------------------------------------------
        # INDEXES
        # -------------------------------------------------

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_start_time

            ON calls(start_time)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_client_key

            ON calls(client_key)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_user_login

            ON calls(user_login)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_src_number

            ON calls(src_number)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_sale_status

            ON calls(sale_status)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_reason

            ON calls(no_sale_reason_code)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_talk_manager

            ON calls(talk_manager_code)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_internal_contact

            ON calls(is_internal_contact)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_sms_sent

            ON calls(sms_sent)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_sms_history_client_status_time

            ON sms_history(
                client_key,
                status,
                sent_at,
                reserved_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_sms_history_call_id

            ON sms_history(call_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_call_ratings_score_time

            ON call_ratings(
                score,
                rated_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_call_ratings_sms_status

            ON call_ratings(
                sms_status,
                sms_sent_at
            )
            """
        )

        conn.commit()


init_db()


# =========================================================
# SMS
# =========================================================

def reserve_client_sms(
    call_id: int,
    client_number: str,
    sender_user_login: str | None,
):

    client_key = normalize_phone(
        client_number
    )

    if not client_key:
        return {
            "reserved": False,
            "reason": "empty_number",
            "history_id": None,
        }

    if get_internal_contact_name(
        client_key
    ):
        return {
            "reserved": False,
            "reason": "internal_contact",
            "history_id": None,
        }

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    cutoff_ts = (
        now_ts
        - SMS_COOLDOWN_DAYS
        * 24
        * 60
        * 60
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        previous = conn.execute(
            """
            SELECT
                id,
                status,
                reserved_at,
                sent_at

            FROM sms_history

            WHERE
                client_key = ?

                AND

                (
                    (
                        status = 'sent'
                        AND sent_at >= ?
                    )

                    OR

                    (
                        status = 'reserved'
                        AND reserved_at >= ?
                    )
                )

            ORDER BY
                COALESCE(
                    sent_at,
                    reserved_at
                ) DESC

            LIMIT 1
            """,
            (
                client_key,
                cutoff_ts,
                cutoff_ts,
            ),
        ).fetchone()

        if previous:

            conn.commit()

            return {
                "reserved": False,
                "reason": "cooldown",
                "history_id": previous["id"],
                "previous_status": previous["status"],
                "previous_sent_at": previous["sent_at"],
                "previous_reserved_at": previous["reserved_at"],
            }

        cursor = conn.execute(
            """
            INSERT INTO sms_history (
                call_id,
                client_number,
                client_key,
                sender_user_login,
                status,
                reserved_at
            )

            VALUES (?, ?, ?, ?, 'reserved', ?)
            """,
            (
                call_id,
                client_number,
                client_key,
                sender_user_login,
                now_ts,
            ),
        )

        conn.commit()

        return {
            "reserved": True,
            "reason": "reserved",
            "history_id": cursor.lastrowid,
        }


def send_client_sms(
    client_number: str,
    sender_user_login: str | None,
    message_text: str = SMS_TEXT,
):

    if not MOIZVONKI_API_URL:
        raise RuntimeError(
            "MOIZVONKI_API_URL не указан"
        )

    user_name = (
        sender_user_login
        or MOIZVONKI_USER_NAME
    )

    if not user_name:
        raise RuntimeError(
            "Не указан user_login телефона "
            "или MOIZVONKI_USER_NAME"
        )

    if not MOIZVONKI_API_KEY:
        raise RuntimeError(
            "MOIZVONKI_API_KEY не указан"
        )

    phone = normalize_phone(
        client_number
    )

    if not phone:
        raise ValueError(
            "Пустой номер клиента"
        )

    payload = {
        "user_name": user_name,
        "api_key": MOIZVONKI_API_KEY,
        "action": "calls.send_sms",
        "to": "+" + phone,
        "text": message_text,
    }

    print(
        "MOIZVONKI SMS PHONE:",
        user_name,
    )

    response = HTTP.post(
        MOIZVONKI_API_URL,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    try:
        result = response.json()

    except ValueError as exc:

        response_text = (
            response.text
            or ""
        ).strip()

        if (
            response_text.casefold()
            == "sms posted"
        ):
            result = {
                "success": True,
                "status": response_text,
            }

        else:
            raise RuntimeError(
                "Мои Звонки вернул не JSON: "
                + response_text[:500]
            ) from exc

    if (
        isinstance(result, dict)

        and

        (
            result.get("error")
            or result.get("success") is False
            or result.get("ok") is False
        )
    ):
        raise RuntimeError(
            str(result)
        )

    return result


def mark_sms_sent(
    call_id: int,
    history_id: int,
    provider_result,
):

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    provider_response = json.dumps(
        provider_result,
        ensure_ascii=False,
        default=str,
    )[:10000]

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE sms_history

            SET
                status = 'sent',
                sent_at = ?,
                error = NULL,
                provider_response = ?

            WHERE id = ?
            """,
            (
                now_ts,
                provider_response,
                history_id,
            ),
        )

        conn.execute(
            """
            UPDATE calls

            SET
                sms_sent = 1,
                sms_sent_at = ?,
                sms_error = NULL

            WHERE id = ?
            """,
            (
                now_ts,
                call_id,
            ),
        )

        conn.commit()


def mark_sms_error(
    call_id: int,
    history_id: int,
    error,
):

    error_text = str(
        error
    )[:2000]

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE sms_history

            SET
                status = 'error',
                error = ?

            WHERE id = ?
            """,
            (
                error_text,
                history_id,
            ),
        )

        conn.execute(
            """
            UPDATE calls

            SET sms_error = ?

            WHERE id = ?
            """,
            (
                error_text,
                call_id,
            ),
        )

        conn.commit()


# =========================================================
# CUSTOMER RATINGS
# =========================================================

def hash_rating_token(
    token: str,
) -> str:

    return hashlib.sha256(
        token.encode(
            "utf-8"
        )
    ).hexdigest()


def build_rating_url(
    token: str,
) -> str:

    if not PUBLIC_BASE_URL:
        raise RuntimeError(
            "PUBLIC_BASE_URL не указан"
        )

    if not PUBLIC_BASE_URL.startswith(
        (
            "https://",
            "http://",
        )
    ):
        raise RuntimeError(
            "PUBLIC_BASE_URL должен начинаться "
            "с https:// или http://"
        )

    return (
        PUBLIC_BASE_URL
        + "/rate/"
        + token
    )


def reserve_call_rating(
    call_id: int,
    client_number: str,
    sender_user_login: str | None,
):

    client_key = normalize_phone(
        client_number
    )

    if not call_id:
        return {
            "reserved": False,
            "reason": "empty_call_id",
        }

    if not client_key:
        return {
            "reserved": False,
            "reason": "empty_number",
        }

    if get_internal_contact_name(
        client_key
    ):
        return {
            "reserved": False,
            "reason": "internal_contact",
        }

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    expires_at = (
        now_ts
        + 30
        * 24
        * 60
        * 60
    )

    raw_token = secrets.token_urlsafe(
        32
    )

    token_hash = hash_rating_token(
        raw_token
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        previous = conn.execute(
            """
            SELECT
                id,
                sms_status,
                score

            FROM call_ratings

            WHERE call_id = ?

            LIMIT 1
            """,
            (
                call_id,
            ),
        ).fetchone()

        if previous:

            conn.commit()

            return {
                "reserved": False,
                "reason": "already_created",
                "rating_id": previous["id"],
                "sms_status": previous["sms_status"],
                "score": previous["score"],
            }

        cursor = conn.execute(
            """
            INSERT INTO call_ratings (
                call_id,
                token_hash,
                client_key,
                sender_user_login,
                sms_status,
                sms_reserved_at,
                expires_at
            )

            VALUES (?, ?, ?, ?, 'reserved', ?, ?)
            """,
            (
                call_id,
                token_hash,
                client_key,
                sender_user_login,
                now_ts,
                expires_at,
            ),
        )

        conn.commit()

        return {
            "reserved": True,
            "reason": "reserved",
            "rating_id": cursor.lastrowid,
            "token": raw_token,
        }


def mark_rating_sms_sent(
    rating_id: int,
    provider_result,
):

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    provider_response = json.dumps(
        provider_result,
        ensure_ascii=False,
        default=str,
    )[:10000]

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE call_ratings

            SET
                sms_status = 'sent',
                sms_sent_at = ?,
                sms_error = NULL,
                provider_response = ?

            WHERE id = ?
            """,
            (
                now_ts,
                provider_response,
                rating_id,
            ),
        )

        conn.commit()


def mark_rating_sms_error(
    rating_id: int,
    error,
):

    error_text = str(
        error
    )[:2000]

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE call_ratings

            SET
                sms_status = 'error',
                sms_error = ?

            WHERE id = ?
            """,
            (
                error_text,
                rating_id,
            ),
        )

        conn.commit()


# =========================================================
# RECORDING
# =========================================================

def prepare_recording(
    recording_url: str,
):

    if not recording_url:

        return (
            None,
            None,
        )

    try:

        response = HTTP.get(
            recording_url,
            timeout=60,
        )

        response.raise_for_status()

    except Exception as exc:

        print(
            "RECORDING DOWNLOAD ERROR:",
            exc,
        )

        return (
            None,
            None,
        )

    with tempfile.TemporaryDirectory() as tmpdir:

        source_path = (
            Path(tmpdir)
            / "call_source"
        )

        voice_path = (
            Path(tmpdir)
            / "call.ogg"
        )

        source_path.write_bytes(
            response.content
        )

        # -------------------------------------------------
        # AUDIO DURATION
        # -------------------------------------------------

        audio_duration = None

        try:

            result = subprocess.run(
                [
                    "ffprobe",

                    "-v",
                    "error",

                    "-show_entries",
                    "format=duration",

                    "-of",
                    (
                        "default="
                        "noprint_wrappers=1:"
                        "nokey=1"
                    ),

                    str(
                        source_path
                    ),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

            value = (
                result
                .stdout
                .strip()
            )

            if value:

                seconds = float(
                    value
                )

                if seconds >= 0:

                    audio_duration = round(
                        seconds
                    )

        except Exception as exc:

            print(
                "FFPROBE ERROR:",
                exc,
            )

        # -------------------------------------------------
        # TELEGRAM VOICE
        # -------------------------------------------------

        voice_bytes = None

        try:

            subprocess.run(
                [
                    "ffmpeg",

                    "-y",

                    "-i",
                    str(
                        source_path
                    ),

                    "-vn",

                    "-c:a",
                    "libopus",

                    "-b:a",
                    "32k",

                    "-vbr",
                    "on",

                    "-application",
                    "voip",

                    str(
                        voice_path
                    ),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

            voice_bytes = (
                voice_path
                .read_bytes()
            )

        except Exception as exc:

            print(
                "FFMPEG ERROR:",
                exc,
            )

        return (
            audio_duration,
            voice_bytes,
        )


def get_talk_duration(
    event: dict,
    audio_duration: int | None = None,
):

    answered = int(
        event.get(
            "answered",
            0,
        )
        or 0
    )

    if not answered:

        return (
            0,
            "none",
        )

    if (
        audio_duration is not None

        and

        audio_duration >= 0
    ):

        return (
            audio_duration,
            "audio",
        )

    answer_time = int(
        event.get(
            "answer_time",
            0,
        )
        or 0
    )

    end_time = int(
        event.get(
            "end_time",
            0,
        )
        or 0
    )

    if (
        answer_time > 0

        and

        end_time > answer_time
    ):

        return (
            end_time
            - answer_time,

            "timestamps",
        )

    api_duration = int(
        event.get(
            "duration",
            0,
        )
        or 0
    )

    return (
        api_duration,
        "api",
    )


# =========================================================
# SAVE CALL
# =========================================================

def save_call(
    webhook: dict,
    event: dict,
    talk_duration: int,
    duration_source: str,
):

    client_number = event.get(
        "client_number"
    )

    client_key = normalize_phone(
        client_number
    )

    internal_contact_name = (
        get_internal_contact_name(
            client_number
        )
    )

    is_internal_contact = (
        1
        if internal_contact_name
        else 0
    )

    api_duration = int(
        event.get(
            "duration",
            0,
        )
        or 0
    )

    db_call_id = event.get(
        "db_call_id"
    )

    event_pbx_call_id = event.get(
        "event_pbx_call_id"
    )

    with connect_db() as conn:

        cursor = conn.execute(
            """
            INSERT INTO calls (

                db_call_id,
                event_pbx_call_id,

                client_number,
                client_key,
                client_name,

                is_internal_contact,
                internal_contact_name,

                direction,
                answered,

                user_id,
                user_login,

                src_number,
                src_id,
                src_slot,

                event_created,

                start_time,
                answer_time,
                end_time,

                duration,
                api_duration,
                duration_source,

                recording,

                account_id,
                account_name,

                telegram_sent
            )

            VALUES (

                ?, ?,

                ?, ?, ?,

                ?, ?,

                ?, ?,

                ?, ?,

                ?, ?, ?,

                ?,

                ?, ?, ?,

                ?, ?, ?,

                ?,

                ?, ?,

                0
            )

            ON CONFLICT(db_call_id)

            DO UPDATE SET

                event_pbx_call_id =
                    excluded.event_pbx_call_id,

                client_number =
                    excluded.client_number,

                client_key =
                    excluded.client_key,

                client_name =
                    excluded.client_name,

                is_internal_contact =
                    excluded.is_internal_contact,

                internal_contact_name =
                    excluded.internal_contact_name,

                direction =
                    excluded.direction,

                answered =
                    excluded.answered,

                user_id =
                    excluded.user_id,

                user_login =
                    excluded.user_login,

                src_number =
                    excluded.src_number,

                src_id =
                    excluded.src_id,

                src_slot =
                    excluded.src_slot,

                event_created =
                    excluded.event_created,

                start_time =
                    excluded.start_time,

                answer_time =
                    excluded.answer_time,

                end_time =
                    excluded.end_time,

                duration =
                    excluded.duration,

                api_duration =
                    excluded.api_duration,

                duration_source =
                    excluded.duration_source,

                recording =
                    excluded.recording,

                account_id =
                    excluded.account_id,

                account_name =
                    excluded.account_name
            """,
            (
                db_call_id,

                event_pbx_call_id,

                client_number,

                client_key,

                event.get(
                    "client_name"
                ),

                is_internal_contact,

                internal_contact_name,

                event.get(
                    "direction"
                ),

                event.get(
                    "answered"
                ),

                webhook.get(
                    "user_id"
                ),

                webhook.get(
                    "user_login"
                ),

                event.get(
                    "src_number"
                ),

                event.get(
                    "src_id"
                ),

                event.get(
                    "src_slot"
                ),

                event.get(
                    "event_created"
                ),

                event.get(
                    "start_time"
                ),

                event.get(
                    "answer_time"
                ),

                event.get(
                    "end_time"
                ),

                talk_duration,

                api_duration,

                duration_source,

                event.get(
                    "recording"
                ),

                webhook.get(
                    "account_id"
                ),

                webhook.get(
                    "account_name"
                ),
            ),
        )

        conn.commit()

        if db_call_id is not None:

            row = conn.execute(
                """
                SELECT
                    id,
                    telegram_sent,
                    is_internal_contact,
                    internal_contact_name

                FROM calls

                WHERE db_call_id = ?
                """,
                (
                    db_call_id,
                ),
            ).fetchone()

        elif event_pbx_call_id:

            row = conn.execute(
                """
                SELECT
                    id,
                    telegram_sent,
                    is_internal_contact,
                    internal_contact_name

                FROM calls

                WHERE
                    event_pbx_call_id = ?

                ORDER BY id DESC

                LIMIT 1
                """,
                (
                    event_pbx_call_id,
                ),
            ).fetchone()

        else:

            row = conn.execute(
                """
                SELECT
                    id,
                    telegram_sent,
                    is_internal_contact,
                    internal_contact_name

                FROM calls

                ORDER BY id DESC

                LIMIT 1
                """
            ).fetchone()

    return {
        "call_id":
            (
                row["id"]
                if row
                else cursor.lastrowid
            ),

        "already_sent":
            bool(
                row
                and row[
                    "telegram_sent"
                ]
            ),

        "is_internal_contact":
            bool(
                row
                and row[
                    "is_internal_contact"
                ]
            ),

        "internal_contact_name":
            (
                row[
                    "internal_contact_name"
                ]
                if row
                else internal_contact_name
            ),
    }


def mark_telegram_sent(
    call_id: int,
    telegram_result: dict | None,
):

    if not call_id:
        return

    telegram_chat_id = None
    telegram_message_id = None

    if telegram_result:

        result = telegram_result.get(
            "result",
            {},
        )

        telegram_message_id = (
            result.get(
                "message_id"
            )
        )

        telegram_chat_id = (
            result.get(
                "chat",
                {},
            )
            .get(
                "id"
            )
        )

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE calls

            SET
                telegram_sent = 1,
                telegram_chat_id = ?,
                telegram_message_id = ?

            WHERE id = ?
            """,
            (
                (
                    str(
                        telegram_chat_id
                    )
                    if telegram_chat_id
                    is not None
                    else None
                ),

                telegram_message_id,

                call_id,
            ),
        )

        conn.commit()


# =========================================================
# CLIENT HISTORY
# =========================================================

def get_client_history(
    client_number: str,
):

    client_key = normalize_phone(
        client_number
    )

    if not client_key:

        return {
            "calls_count": 0,
        }

    with connect_db() as conn:

        row = conn.execute(
            """
            SELECT
                COUNT(*) AS calls_count

            FROM calls

            WHERE
                client_key = ?

                AND

                COALESCE(
                    is_internal_contact,
                    0
                ) = 0
            """,
            (
                client_key,
            ),
        ).fetchone()

    return {
        "calls_count":
            row[
                "calls_count"
            ]
            or 0,
    }


# =========================================================
# FORMATTERS
# =========================================================

def format_duration(
    seconds: int,
):

    seconds = int(
        seconds
        or 0
    )

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, secs = divmod(
        remainder,
        60,
    )

    if hours:

        if minutes:

            return (
                f"{hours} ч "
                f"{minutes} мин"
            )

        return (
            f"{hours} ч"
        )

    if minutes:

        if secs:

            return (
                f"{minutes} мин "
                f"{secs} сек"
            )

        return (
            f"{minutes} мин"
        )

    return (
        f"{secs} сек"
    )


def format_call_time(
    timestamp,
):

    if not timestamp:
        return "—"

    return (
        datetime
        .fromtimestamp(
            int(
                timestamp
            ),
            UZ_TZ,
        )
        .strftime(
            "%H:%M"
        )
    )


# =========================================================
# TELEGRAM MESSAGE
# =========================================================

def build_telegram_message(
    event: dict,
    webhook: dict,
    talk_duration: int,
):

    direction = event.get(
        "direction"
    )

    answered = int(
        event.get(
            "answered",
            0,
        )
        or 0
    )

    client_number = (
        event.get(
            "client_number"
        )
        or ""
    )

    client_name = (
        event.get(
            "client_name"
        )
        or ""
    )

    internal_contact_name = (
        get_internal_contact_name(
            client_number
        )
    )

    sim = (
        event.get(
            "src_number"
        )
        or ""
    )

    start_time = event.get(
        "start_time"
    )

    answer_time = event.get(
        "answer_time"
    )

    # -------------------------------------------------
    # TITLE
    # -------------------------------------------------

    if (
        direction == 0
        and answered
    ):

        title = (
            "📥 <b>Входящий звонок</b>"
        )

    elif (
        direction == 0
        and not answered
    ):

        title = (
            "❌ <b>Пропущенный звонок</b>"
        )

    elif (
        direction == 1
        and answered
    ):

        title = (
            "📤 <b>Исходящий звонок</b>"
        )

    else:

        title = (
            "⚠️ <b>Исходящий без ответа</b>"
        )

    lines = [
        title,
        "",
    ]

    # -------------------------------------------------
    # INTERNAL CONTACT
    # -------------------------------------------------

    if internal_contact_name:

        lines.append(
            "👤 Контакт: "
            f"<b>{escape(internal_contact_name)}</b>"
        )

        lines.append(
            "📞 "
            f"{escape(client_number or '—')}"
        )

    # -------------------------------------------------
    # NORMAL CLIENT
    # -------------------------------------------------

    else:

        if client_name:

            lines.append(
                "👤 "
                f"<b>{escape(client_name)}</b>"
            )

        lines.append(
            "📞 "
            f"{escape(client_number or '—')}"
        )

        history = get_client_history(
            client_number
        )

        contacts = history[
            "calls_count"
        ]

        if contacts <= 1:

            lines.append(
                "🆕 Новый клиент"
            )

        else:

            lines.append(
                "🔁 Контактов с номером: "
                f"<b>{contacts}</b>"
            )

    lines.append(
        ""
    )

    # -------------------------------------------------
    # TIME
    # -------------------------------------------------

    if answered:

        lines.append(
            "🕐 Начало разговора: "
            f"<b>{format_call_time(answer_time)}</b>"
        )

        lines.append(
            "⏱ Разговор: "
            f"<b>{format_duration(talk_duration)}</b>"
        )

    else:

        lines.append(
            "🕐 Время звонка: "
            f"<b>{format_call_time(start_time)}</b>"
        )

    lines.append(
        "📲 SIM: "
        f"{escape(sim or '—')}"
    )

    # -------------------------------------------------
    # BUTTON PROMPT
    # -------------------------------------------------

    if (
        answered
        and not internal_contact_name
    ):

        lines.extend(
            [
                "",
                "<b>Кто разговаривал?</b>",
            ]
        )

    return "\n".join(
        lines
    )


# =========================================================
# TELEGRAM KEYBOARDS
# =========================================================

def build_manager_keyboard(
    call_id: int,
):

    return {
        "inline_keyboard": [

            [
                {
                    "text":
                        "👤 Olmas",

                    "callback_data":
                        f"manager:olmas:{call_id}",
                },

                {
                    "text":
                        "👤 Otabek",

                    "callback_data":
                        f"manager:otabek:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "👤 Ali",

                    "callback_data":
                        f"manager:ali:{call_id}",
                },

                {
                    "text":
                        "👤 Abbos",

                    "callback_data":
                        f"manager:abbos:{call_id}",
                },
            ],
        ]
    }


def build_sale_keyboard(
    call_id: int,
    manager_name: str | None = None,
):

    keyboard = []

    if manager_name:

        keyboard.append(
            [
                {
                    "text":
                        f"👤 Менеджер: {manager_name}",

                    "callback_data":
                        f"manager_selected:{call_id}",
                }
            ]
        )

    keyboard.extend(
        [

            [
                {
                    "text":
                        "✅ Купил",

                    "callback_data":
                        f"result:bought:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "📦 Нет товара",

                    "callback_data":
                        f"result:no_stock:{call_id}",
                },

                {
                    "text":
                        "💰 Не устроила цена",

                    "callback_data":
                        f"result:price:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "💰 Цена изменилась",

                    "callback_data":
                        f"result:price_changed:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🤔 Думает / сравнивает",

                    "callback_data":
                        f"result:thinking:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🔎 Ищет другой товар",

                    "callback_data":
                        f"result:other_product:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🔎 Хочет на кредит",

                    "callback_data":
                        f"result:credit:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🏪 Хочет прийти в магазин",

                    "callback_data":
                        f"result:visit_store:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "⏳ Купит позже",

                    "callback_data":
                        f"result:later:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🏪 Купил в другом месте",

                    "callback_data":
                        f"result:bought_elsewhere:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🚚 Не подошли условия",

                    "callback_data":
                        f"result:conditions:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🚫 Не целевой звонок",

                    "callback_data":
                        f"result:not_target:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "📝 Другая причина",

                    "callback_data":
                        f"result:other:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "👤 Изменить менеджера",

                    "callback_data":
                        f"manager_back:{call_id}",
                }
            ],
        ]
    )


    return {
        "inline_keyboard":
            keyboard
    }


def build_selected_keyboard(
    call_id: int,
    manager_name: str,
    selected_text: str,
):

    return {
        "inline_keyboard": [

            [
                {
                    "text":
                        f"👤 {manager_name}",

                    "callback_data":
                        f"manager_selected:{call_id}",
                }
            ],

            [
                {
                    "text":
                        selected_text,

                    "callback_data":
                        f"selected:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "↩️ Изменить результат",

                    "callback_data":
                        f"result_back:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "👤 Изменить менеджера",

                    "callback_data":
                        f"manager_back:{call_id}",
                }
            ],
        ]
    }


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_api(
    method: str,
    *,
    data: dict | None = None,
    files: dict | None = None,
    timeout: int = 30,
):

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не указан"
        )

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    response = HTTP.post(
        url,
        data=data,
        files=files,
        timeout=timeout,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get(
        "ok"
    ):

        raise RuntimeError(
            str(
                result
            )
        )

    return result


def send_text_message(
    text: str,
    reply_markup: dict | None = None,
):

    data = {
        "chat_id":
            TELEGRAM_CHAT_ID,

        "text":
            text,

        "parse_mode":
            "HTML",

        "disable_web_page_preview":
            True,
    }

    if reply_markup:

        data[
            "reply_markup"
        ] = json.dumps(
            reply_markup,
            ensure_ascii=False,
        )

    return telegram_api(
        "sendMessage",
        data=data,
        timeout=30,
    )


def send_voice_bytes(
    voice_bytes: bytes,
    caption: str,
    reply_markup: dict | None = None,
):

    data = {
        "chat_id":
            TELEGRAM_CHAT_ID,

        "caption":
            caption,

        "parse_mode":
            "HTML",
    }

    if reply_markup:

        data[
            "reply_markup"
        ] = json.dumps(
            reply_markup,
            ensure_ascii=False,
        )

    return telegram_api(
        "sendVoice",

        data=data,

        files={
            "voice": (
                "call.ogg",
                voice_bytes,
                "audio/ogg",
            )
        },

        timeout=60,
    )


def answer_callback_query(
    callback_query_id: str,
    text: str = "",
):

    if not callback_query_id:
        return

    try:

        data = {
            "callback_query_id":
                callback_query_id,
        }

        if text:

            data["text"] = text

        telegram_api(
            "answerCallbackQuery",
            data=data,
            timeout=15,
        )

    except Exception as exc:

        print(
            "ANSWER CALLBACK ERROR:",
            exc,
        )


def edit_reply_markup(
    chat_id,
    message_id,
    reply_markup,
):

    return telegram_api(
        "editMessageReplyMarkup",

        data={
            "chat_id":
                chat_id,

            "message_id":
                message_id,

            "reply_markup":
                json.dumps(
                    reply_markup,
                    ensure_ascii=False,
                ),
        },

        timeout=30,
    )


# =========================================================
# CALL DATABASE
# =========================================================

def get_call(
    call_id: int,
):

    with connect_db() as conn:

        return conn.execute(
            """
            SELECT *

            FROM calls

            WHERE id = ?
            """,
            (
                call_id,
            ),
        ).fetchone()


def get_telegram_user_name(
    telegram_user: dict,
):

    return (
        telegram_user.get(
            "username"
        )

        or

        telegram_user.get(
            "first_name"
        )

        or

        telegram_user.get(
            "last_name"
        )

        or

        ""
    )


# =========================================================
# MANAGER DATABASE
# =========================================================

def mark_talk_manager(
    call_id: int,
    manager_code: str,
    telegram_user: dict,
):

    manager_name = MANAGERS.get(
        manager_code
    )

    if not manager_name:

        raise ValueError(
            "Неизвестный менеджер"
        )

    now_ts = int(
        datetime
        .now(
            UZ_TZ
        )
        .timestamp()
    )

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE calls

            SET
                talk_manager_code = ?,
                talk_manager_name = ?,

                manager_marked_at = ?,
                manager_marked_by = ?,
                manager_marked_username = ?

            WHERE id = ?
            """,
            (
                manager_code,
                manager_name,

                now_ts,

                telegram_user.get(
                    "id"
                ),

                get_telegram_user_name(
                    telegram_user
                ),

                call_id,
            ),
        )

        conn.commit()

    return manager_name


# =========================================================
# SALE DATABASE
# =========================================================

def mark_sale_bought(
    call_id: int,
    telegram_user: dict,
):

    now_ts = int(
        datetime
        .now(
            UZ_TZ
        )
        .timestamp()
    )

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE calls

            SET
                sale_status = 'bought',

                no_sale_reason = NULL,
                no_sale_reason_code = NULL,

                sale_marked_at = ?,
                sale_marked_by = ?,
                sale_marked_username = ?

            WHERE id = ?
            """,
            (
                now_ts,

                telegram_user.get(
                    "id"
                ),

                get_telegram_user_name(
                    telegram_user
                ),

                call_id,
            ),
        )

        conn.commit()


def mark_sale_not_bought(
    call_id: int,
    reason_code: str,
    telegram_user: dict,
):

    reason = SALE_REASONS.get(
        reason_code
    )

    if not reason:

        raise ValueError(
            "Неизвестная причина"
        )

    now_ts = int(
        datetime
        .now(
            UZ_TZ
        )
        .timestamp()
    )

    with connect_db() as conn:

        conn.execute(
            """
            UPDATE calls

            SET
                sale_status = 'not_bought',

                no_sale_reason = ?,
                no_sale_reason_code = ?,

                sale_marked_at = ?,
                sale_marked_by = ?,
                sale_marked_username = ?

            WHERE id = ?
            """,
            (
                reason,
                reason_code,

                now_ts,

                telegram_user.get(
                    "id"
                ),

                get_telegram_user_name(
                    telegram_user
                ),

                call_id,
            ),
        )

        conn.commit()


# =========================================================
# PRICE PAGE
# =========================================================

PRICE_HTML_FILE = Path(
    "/app/price/index.html"
)


@app.get(
    "/price",
    response_class=HTMLResponse,
)
async def price_page():

    if not PRICE_HTML_FILE.exists():

        return HTMLResponse(
            content=(
                "<h1>Price page not found</h1>"
            ),
            status_code=404,
        )

    html_content = (
        PRICE_HTML_FILE.read_text(
            encoding="utf-8"
        )
    )

    return HTMLResponse(
        content=html_content,
        status_code=200,
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.post(
    "/telegram/webhook"
)
async def telegram_webhook(
    request: Request,
):

    if TELEGRAM_WEBHOOK_SECRET:

        received_secret = (
            request.headers.get(
                "X-Telegram-Bot-Api-Secret-Token",
                "",
            )
        )

        if (
            received_secret
            != TELEGRAM_WEBHOOK_SECRET
        ):

            raise HTTPException(
                status_code=403,
                detail=(
                    "Invalid Telegram secret"
                ),
            )

    data = await request.json()

    print(
        "TELEGRAM UPDATE:",
        data,
    )

    callback = data.get(
        "callback_query"
    )

    if not callback:

        return {
            "ok": True
        }

    callback_id = callback.get(
        "id"
    )

    callback_data = (
        callback.get(
            "data"
        )
        or ""
    )

    telegram_user = (
        callback.get(
            "from"
        )
        or {}
    )

    telegram_message = (
        callback.get(
            "message"
        )
        or {}
    )

    chat_id = (
        telegram_message
        .get(
            "chat",
            {},
        )
        .get(
            "id"
        )
    )

    message_id = (
        telegram_message.get(
            "message_id"
        )
    )

    print(
        "TELEGRAM CALLBACK:",
        callback_data,
    )

    try:

        # =================================================
        # MANAGER SELECTED
        # =================================================

        if callback_data.startswith(
            "manager:"
        ):

            parts = callback_data.split(
                ":"
            )

            if len(parts) != 3:

                answer_callback_query(
                    callback_id,
                    "Ошибка кнопки",
                )

                return {
                    "ok": True
                }

            manager_code = parts[1]

            try:

                call_id = int(
                    parts[2]
                )

            except ValueError:

                answer_callback_query(
                    callback_id,
                    "Ошибка ID звонка",
                )

                return {
                    "ok": True
                }

            call = get_call(
                call_id
            )

            if not call:

                answer_callback_query(
                    callback_id,
                    "Звонок не найден",
                )

                return {
                    "ok": True
                }

            if call[
                "is_internal_contact"
            ]:

                answer_callback_query(
                    callback_id,
                    "Это внутренний контакт",
                )

                return {
                    "ok": True
                }

            manager_name = mark_talk_manager(
                call_id,
                manager_code,
                telegram_user,
            )

            edit_reply_markup(
                chat_id,
                message_id,

                build_sale_keyboard(
                    call_id,
                    manager_name,
                ),
            )

            answer_callback_query(
                callback_id,
                f"👤 {manager_name}",
            )

            print(
                "TALK MANAGER:",
                call_id,
                manager_name,
            )

            return {
                "ok": True,

                "call_id":
                    call_id,

                "manager":
                    manager_name,
            }

        # =================================================
        # CHANGE MANAGER
        # =================================================

        if callback_data.startswith(
            "manager_back:"
        ):

            call_id = int(
                callback_data.split(
                    ":",
                    1,
                )[1]
            )

            call = get_call(
                call_id
            )

            if not call:

                answer_callback_query(
                    callback_id,
                    "Звонок не найден",
                )

                return {
                    "ok": True
                }

            edit_reply_markup(
                chat_id,
                message_id,

                build_manager_keyboard(
                    call_id
                ),
            )

            answer_callback_query(
                callback_id,
                "Выберите менеджера",
            )

            return {
                "ok": True
            }

        # =================================================
        # MANAGER DISPLAY BUTTON
        # =================================================

        if callback_data.startswith(
            "manager_selected:"
        ):

            answer_callback_query(
                callback_id,
                "Менеджер уже выбран",
            )

            return {
                "ok": True
            }

        # =================================================
        # CHANGE RESULT
        # =================================================

        if callback_data.startswith(
            "result_back:"
        ):

            call_id = int(
                callback_data.split(
                    ":",
                    1,
                )[1]
            )

            call = get_call(
                call_id
            )

            if not call:

                answer_callback_query(
                    callback_id,
                    "Звонок не найден",
                )

                return {
                    "ok": True
                }

            manager_name = (
                call[
                    "talk_manager_name"
                ]
                or "Не указан"
            )

            edit_reply_markup(
                chat_id,
                message_id,

                build_sale_keyboard(
                    call_id,
                    manager_name,
                ),
            )

            answer_callback_query(
                callback_id,
                "Выберите новый результат",
            )

            return {
                "ok": True
            }

        # =================================================
        # SELECTED RESULT
        # =================================================

        if callback_data.startswith(
            "selected:"
        ):

            answer_callback_query(
                callback_id,
                "Этот результат уже выбран",
            )

            return {
                "ok": True
            }

        # =================================================
        # SALE RESULT
        # =================================================

        if callback_data.startswith(
            "result:"
        ):

            parts = callback_data.split(
                ":"
            )

            if len(parts) != 3:

                answer_callback_query(
                    callback_id,
                    "Ошибка кнопки",
                )

                return {
                    "ok": True
                }

            result_code = parts[1]

            try:

                call_id = int(
                    parts[2]
                )

            except ValueError:

                answer_callback_query(
                    callback_id,
                    "Ошибка ID звонка",
                )

                return {
                    "ok": True
                }

            call = get_call(
                call_id
            )

            if not call:

                answer_callback_query(
                    callback_id,
                    "Звонок не найден",
                )

                return {
                    "ok": True
                }

            if call[
                "is_internal_contact"
            ]:

                answer_callback_query(
                    callback_id,
                    "Для контактов результат не нужен",
                )

                return {
                    "ok": True
                }

            manager_name = (
                call[
                    "talk_manager_name"
                ]
            )

            if not manager_name:

                edit_reply_markup(
                    chat_id,
                    message_id,

                    build_manager_keyboard(
                        call_id
                    ),
                )

                answer_callback_query(
                    callback_id,
                    "Сначала выберите менеджера",
                )

                return {
                    "ok": True
                }

            # =============================================
            # BOUGHT
            # =============================================

            if result_code == "bought":

                mark_sale_bought(
                    call_id,
                    telegram_user,
                )

                edit_reply_markup(
                    chat_id,
                    message_id,

                    build_selected_keyboard(
                        call_id,
                        manager_name,
                        "✅ Купил",
                    ),
                )

                answer_callback_query(
                    callback_id,
                    "✅ Сохранено",
                )

                return {
                    "ok": True,

                    "call_id":
                        call_id,

                    "sale_status":
                        "bought",
                }

            # =============================================
            # NOT BOUGHT REASON
            # =============================================

            reason = SALE_REASONS.get(
                result_code
            )

            if not reason:

                answer_callback_query(
                    callback_id,
                    "Неизвестный результат",
                )

                return {
                    "ok": True
                }

            mark_sale_not_bought(
                call_id,
                result_code,
                telegram_user,
            )

            edit_reply_markup(
                chat_id,
                message_id,

                build_selected_keyboard(
                    call_id,
                    manager_name,
                    reason,
                ),
            )

            answer_callback_query(
                callback_id,
                "✅ Сохранено",
            )

            return {
                "ok": True,

                "call_id":
                    call_id,

                "sale_status":
                    "not_bought",

                "reason":
                    reason,
            }

        answer_callback_query(
            callback_id,
            "Неизвестная команда",
        )

        return {
            "ok": True
        }

    except Exception as exc:

        print(
            "TELEGRAM CALLBACK ERROR:",
            repr(
                exc
            ),
        )

        answer_callback_query(
            callback_id,
            "Ошибка сохранения",
        )

        raise


# =========================================================
# PERIODS
# =========================================================

def parse_date(
    value: str,
):

    try:

        return datetime.strptime(
            value,
            "%Y-%m-%d",
        ).date()

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=(
                "Дата должна быть "
                "в формате YYYY-MM-DD"
            ),
        ) from exc


def get_period(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    today = (
        datetime
        .now(
            UZ_TZ
        )
        .date()
    )

    if period == "today":

        start_date = today
        end_date = today
        label = "Сегодня"

    elif period == "yesterday":

        start_date = (
            today
            - timedelta(
                days=1
            )
        )

        end_date = start_date
        label = "Вчера"

    elif period == "7d":

        start_date = (
            today
            - timedelta(
                days=6
            )
        )

        end_date = today
        label = "Последние 7 дней"

    elif period == "30d":

        start_date = (
            today
            - timedelta(
                days=29
            )
        )

        end_date = today
        label = "Последние 30 дней"

    elif period == "custom":

        if (
            not date_from
            or not date_to
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "Для custom нужны "
                    "date_from и date_to"
                ),
            )

        start_date = parse_date(
            date_from
        )

        end_date = parse_date(
            date_to
        )

        if end_date < start_date:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Конечная дата "
                    "меньше начальной"
                ),
            )

        if (
            end_date
            - start_date
        ).days > 366:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Максимальный период "
                    "366 дней"
                ),
            )

        label = (
            f"{start_date.strftime('%d.%m.%Y')}"
            " — "
            f"{end_date.strftime('%d.%m.%Y')}"
        )

    else:

        raise HTTPException(
            status_code=400,
            detail=(
                "Неизвестный период"
            ),
        )

    start_dt = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=UZ_TZ,
    )

    end_dt = (
        datetime(
            end_date.year,
            end_date.month,
            end_date.day,
            tzinfo=UZ_TZ,
        )
        + timedelta(
            days=1
        )
    )

    return {
        "period":
            period,

        "label":
            label,

        "start_date":
            start_date.isoformat(),

        "end_date":
            end_date.isoformat(),

        "start_ts":
            int(
                start_dt.timestamp()
            ),

        "end_ts":
            int(
                end_dt.timestamp()
            ),

        "days":
            (
                end_date
                - start_date
            ).days
            + 1,
    }


# =========================================================
# CUSTOMER RATING PAGE
# =========================================================

def limit_text(
    value,
    max_length: int = 2000,
):

    if value is None:
        return None

    return str(
        value
    )[:max_length]


def get_request_ip(
    request: Request,
):

    forwarded_for = request.headers.get(
        "x-forwarded-for",
        "",
    )

    if forwarded_for:
        return limit_text(
            forwarded_for.split(
                ","
            )[0].strip(),
            100,
        )

    connecting_ip = request.headers.get(
        "cf-connecting-ip",
        "",
    )

    if connecting_ip:
        return limit_text(
            connecting_ip,
            100,
        )

    if request.client:
        return limit_text(
            request.client.host,
            100,
        )

    return None


def get_rating_request_metadata(
    request: Request,
):

    header_names = [
        "user-agent",
        "accept-language",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-ch-ua-model",
        "sec-ch-ua-platform-version",
        "sec-ch-ua-arch",
        "sec-ch-ua-bitness",
        "sec-ch-ua-form-factors",
        "sec-ch-device-memory",
        "sec-ch-dpr",
        "sec-ch-viewport-width",
        "sec-ch-viewport-height",
        "save-data",
        "downlink",
        "ect",
        "rtt",
    ]

    selected_headers = {}

    for name in header_names:

        value = request.headers.get(
            name
        )

        if value:
            selected_headers[name] = (
                limit_text(
                    value,
                    1000,
                )
            )

    return {
        "ip": get_request_ip(
            request
        ),

        "user_agent": limit_text(
            request.headers.get(
                "user-agent"
            ),
            2000,
        ),

        "accept_language": limit_text(
            request.headers.get(
                "accept-language"
            ),
            500,
        ),

        "referer": limit_text(
            request.headers.get(
                "referer"
            ),
            2000,
        ),

        "headers_json": json.dumps(
            selected_headers,
            ensure_ascii=False,
        )[:10000],
    }


def record_rating_request(
    token: str,
    request: Request,
    *,
    is_submission: bool = False,
):

    token_hash = hash_rating_token(
        token
    )

    metadata = get_rating_request_metadata(
        request
    )

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    with connect_db() as conn:

        if is_submission:

            cursor = conn.execute(
                """
                UPDATE call_ratings

                SET
                    rated_ip = ?,
                    user_agent = ?,
                    accept_language = ?,
                    referer = ?,
                    request_headers_json = ?

                WHERE token_hash = ?
                """,
                (
                    metadata["ip"],
                    metadata["user_agent"],
                    metadata["accept_language"],
                    metadata["referer"],
                    metadata["headers_json"],
                    token_hash,
                ),
            )

        else:

            cursor = conn.execute(
                """
                UPDATE call_ratings

                SET
                    first_opened_at = COALESCE(
                        first_opened_at,
                        ?
                    ),
                    last_opened_at = ?,
                    open_count = COALESCE(
                        open_count,
                        0
                    ) + 1,
                    first_ip = COALESCE(
                        first_ip,
                        ?
                    ),
                    last_ip = ?,
                    user_agent = ?,
                    accept_language = ?,
                    referer = ?,
                    request_headers_json = ?

                WHERE token_hash = ?
                """,
                (
                    now_ts,
                    now_ts,
                    metadata["ip"],
                    metadata["ip"],
                    metadata["user_agent"],
                    metadata["accept_language"],
                    metadata["referer"],
                    metadata["headers_json"],
                    token_hash,
                ),
            )

        conn.commit()

        return bool(
            cursor.rowcount
        )


def sanitize_device_value(
    value,
    depth: int = 0,
):

    if depth > 3:
        return None

    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return value

    if isinstance(
        value,
        (
            int,
            float,
        ),
    ):
        return value

    if isinstance(
        value,
        str,
    ):
        return value[:500]

    if isinstance(
        value,
        list,
    ):
        return [
            sanitize_device_value(
                item,
                depth + 1,
            )
            for item in value[:30]
        ]

    if isinstance(
        value,
        dict,
    ):
        return {
            str(key)[:100]:
                sanitize_device_value(
                    item,
                    depth + 1,
                )
            for key, item in list(
                value.items()
            )[:50]
        }

    return limit_text(
        value,
        500,
    )


def sanitize_device_payload(
    payload,
):

    if not isinstance(
        payload,
        dict,
    ):
        return {}

    allowed_keys = {
        "language",
        "languages",
        "timezone",
        "timezone_offset_minutes",
        "screen_width",
        "screen_height",
        "screen_avail_width",
        "screen_avail_height",
        "color_depth",
        "pixel_depth",
        "viewport_width",
        "viewport_height",
        "device_pixel_ratio",
        "max_touch_points",
        "touch_supported",
        "platform",
        "hardware_concurrency",
        "device_memory_gb",
        "cookie_enabled",
        "do_not_track",
        "online",
        "color_scheme",
        "reduced_motion",
        "connection",
        "user_agent_data",
        "high_entropy",
    }

    return {
        key: sanitize_device_value(
            payload.get(
                key
            )
        )
        for key in allowed_keys
        if key in payload
    }


def rating_html_response(
    title: str,
    message_ru: str,
    message_uz: str,
    token: str | None = None,
    selected_score: int | None = None,
    status_code: int = 200,
):

    buttons_html = ""
    privacy_html = ""

    if token:

        safe_token = escape(
            token,
            quote=True,
        )

        buttons = []

        for score in range(
            1,
            6,
        ):
            buttons.append(
                f"""
                <form
                    method="post"
                    action="/rate/{safe_token}/{score}"
                >
                    <button
                        type="submit"
                        aria-label="Оценка {score}"
                    >
                        {score}
                    </button>
                </form>
                """
            )

        buttons_html = (
            """
            <div class="scale">
                <span>1 — плохо / yomon</span>
                <span>5 — отлично / a’lo</span>
            </div>
            <div class="buttons">
            """
            + "".join(
                buttons
            )
            + "</div>"
        )

        privacy_html = """
        <p class="privacy">
            При открытии сохраняются IP-адрес и технические
            данные браузера.<br>
            Sahifa ochilganda IP-manzil va brauzerning texnik
            ma’lumotlari saqlanadi.
        </p>
        """

    score_html = ""

    if selected_score is not None:
        score_html = (
            '<div class="selected">'
            + str(selected_score)
            + " ★</div>"
        )

    device_script = ""

    if token:

        token_json = json.dumps(
            token
        )

        device_script = """
<script>
(async () => {
    try {
        const nav = window.navigator || {};
        const connection =
            nav.connection
            || nav.mozConnection
            || nav.webkitConnection
            || null;

        const data = {
            language: nav.language || null,
            languages: Array.from(
                nav.languages || []
            ),
            timezone: (
                Intl.DateTimeFormat()
                    .resolvedOptions()
                    .timeZone
                || null
            ),
            timezone_offset_minutes:
                new Date().getTimezoneOffset(),
            screen_width: window.screen.width,
            screen_height: window.screen.height,
            screen_avail_width:
                window.screen.availWidth,
            screen_avail_height:
                window.screen.availHeight,
            color_depth: window.screen.colorDepth,
            pixel_depth: window.screen.pixelDepth,
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            device_pixel_ratio:
                window.devicePixelRatio || 1,
            max_touch_points:
                Number(nav.maxTouchPoints || 0),
            touch_supported:
                (
                    "ontouchstart" in window
                    || Number(nav.maxTouchPoints || 0) > 0
                ),
            platform: nav.platform || null,
            hardware_concurrency:
                nav.hardwareConcurrency || null,
            device_memory_gb:
                nav.deviceMemory || null,
            cookie_enabled:
                Boolean(nav.cookieEnabled),
            do_not_track:
                nav.doNotTrack || null,
            online:
                Boolean(nav.onLine),
            color_scheme:
                window.matchMedia(
                    "(prefers-color-scheme: dark)"
                ).matches
                    ? "dark"
                    : "light",
            reduced_motion:
                window.matchMedia(
                    "(prefers-reduced-motion: reduce)"
                ).matches,
            connection: connection
                ? {
                    effective_type:
                        connection.effectiveType || null,
                    downlink_mbps:
                        connection.downlink || null,
                    rtt_ms:
                        connection.rtt || null,
                    save_data:
                        Boolean(connection.saveData),
                }
                : null,
            user_agent_data: nav.userAgentData
                ? {
                    brands:
                        nav.userAgentData.brands || [],
                    mobile:
                        Boolean(nav.userAgentData.mobile),
                    platform:
                        nav.userAgentData.platform || null,
                }
                : null,
            high_entropy: null,
        };

        if (
            nav.userAgentData
            && nav.userAgentData.getHighEntropyValues
        ) {
            try {
                data.high_entropy =
                    await nav.userAgentData
                        .getHighEntropyValues(
                            [
                                "architecture",
                                "bitness",
                                "formFactors",
                                "fullVersionList",
                                "model",
                                "platformVersion",
                            ]
                        );
            } catch (error) {
                data.high_entropy = null;
            }
        }

        const ratingToken = __RATING_TOKEN__;

        await fetch(
            "/rating-device/"
            + encodeURIComponent(ratingToken),
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(data),
                credentials: "omit",
                keepalive: true,
            }
        );
    } catch (error) {
        // Оценка должна работать,
        // даже если браузер не отдал данные.
    }
})();
</script>
        """.replace(
            "__RATING_TOKEN__",
            token_json,
        )

    html = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >
    <meta
        name="robots"
        content="noindex,nofollow"
    >
    <title>{escape(title)}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 20px;
            background: #111;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont,
                "Segoe UI", Arial, sans-serif;
        }}
        .card {{
            width: 100%;
            max-width: 620px;
            padding: 34px 26px;
            border: 1px solid #333;
            border-radius: 22px;
            background: #1b1b1b;
            text-align: center;
        }}
        .brand {{
            color: #d9b565;
            font-weight: 800;
            letter-spacing: .08em;
        }}
        h1 {{
            margin: 18px 0 12px;
            font-size: 28px;
        }}
        p {{
            margin: 8px 0;
            color: #c7c7c7;
            line-height: 1.5;
        }}
        .buttons {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 10px;
            margin-top: 18px;
        }}
        form {{ margin: 0; }}
        button {{
            width: 100%;
            min-height: 58px;
            border: 1px solid #4a4a4a;
            border-radius: 14px;
            background: #242424;
            color: #fff;
            font-size: 24px;
            font-weight: 800;
            cursor: pointer;
        }}
        button:hover,
        button:focus {{
            background: #d9b565;
            color: #111;
            border-color: #d9b565;
        }}
        .scale {{
            display: flex;
            justify-content: space-between;
            gap: 15px;
            margin-top: 24px;
            color: #888;
            font-size: 12px;
        }}
        .selected {{
            margin: 24px auto 4px;
            color: #d9b565;
            font-size: 44px;
            font-weight: 800;
        }}
        .privacy {{
            margin-top: 22px;
            color: #777;
            font-size: 11px;
        }}
    </style>
</head>
<body>
    <main class="card">
        <div class="brand">TEXNIKACH</div>
        <h1>{escape(title)}</h1>
        <p>{escape(message_ru)}</p>
        <p>{escape(message_uz)}</p>
        {score_html}
        {buttons_html}
        {privacy_html}
    </main>
    {device_script}
</body>
</html>
    """

    return HTMLResponse(
        content=html,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.get(
    "/rate/{token}",
    response_class=HTMLResponse,
)
def rating_page(
    token: str,
    request: Request,
):

    token_hash = hash_rating_token(
        token
    )

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    with connect_db() as conn:

        row = conn.execute(
            """
            SELECT
                score,
                expires_at

            FROM call_ratings

            WHERE token_hash = ?

            LIMIT 1
            """,
            (
                token_hash,
            ),
        ).fetchone()

    if not row:
        return rating_html_response(
            "Ссылка не найдена",
            "Проверьте ссылку из SMS.",
            "SMSdagi havolani tekshiring.",
            status_code=404,
        )

    if row["score"] is not None:
        return rating_html_response(
            "Спасибо за оценку!",
            "Ваш ответ уже сохранён.",
            "Javobingiz saqlandi. Rahmat!",
            selected_score=row["score"],
        )

    if row["expires_at"] < now_ts:
        return rating_html_response(
            "Срок ссылки истёк",
            "Эта ссылка была активна 30 дней.",
            "Ushbu havola 30 kun davomida faol edi.",
            status_code=410,
        )

    record_rating_request(
        token,
        request,
        is_submission=False,
    )

    return rating_html_response(
        "Оцените разговор",
        "Выберите оценку от 1 до 5.",
        "Suhbatni 1 dan 5 gacha baholang.",
        token=token,
    )


@app.post(
    "/rate/{token}/{score}",
    response_class=HTMLResponse,
)
def submit_rating(
    token: str,
    score: int,
    request: Request,
):

    if score not in {
        1,
        2,
        3,
        4,
        5,
    }:
        raise HTTPException(
            status_code=400,
            detail="Rating must be from 1 to 5",
        )

    token_hash = hash_rating_token(
        token
    )

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    request_metadata = (
        get_rating_request_metadata(
            request
        )
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        row = conn.execute(
            """
            SELECT
                score,
                expires_at

            FROM call_ratings

            WHERE token_hash = ?

            LIMIT 1
            """,
            (
                token_hash,
            ),
        ).fetchone()

        if not row:
            conn.commit()

            return rating_html_response(
                "Ссылка не найдена",
                "Проверьте ссылку из SMS.",
                "SMSdagi havolani tekshiring.",
                status_code=404,
            )

        if row["score"] is not None:
            conn.commit()

            return rating_html_response(
                "Спасибо за оценку!",
                "Первая оценка уже сохранена.",
                "Birinchi baho saqlangan.",
                selected_score=row["score"],
            )

        if row["expires_at"] < now_ts:
            conn.commit()

            return rating_html_response(
                "Срок ссылки истёк",
                "Эта ссылка была активна 30 дней.",
                "Ushbu havola 30 kun davomida faol edi.",
                status_code=410,
            )

        conn.execute(
            """
            UPDATE call_ratings

            SET
                score = ?,
                rated_at = ?,
                rated_ip = ?,
                user_agent = ?,
                accept_language = ?,
                referer = ?,
                request_headers_json = ?

            WHERE
                token_hash = ?
                AND score IS NULL
            """,
            (
                score,
                now_ts,
                request_metadata["ip"],
                request_metadata["user_agent"],
                request_metadata["accept_language"],
                request_metadata["referer"],
                request_metadata["headers_json"],
                token_hash,
            ),
        )

        conn.commit()

    return rating_html_response(
        "Спасибо за оценку!",
        "Ваш ответ сохранён.",
        "Javobingiz saqlandi. Rahmat!",
        selected_score=score,
    )


@app.post(
    "/rating-device/{token}"
)
async def capture_rating_device(
    token: str,
    request: Request,
):

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Некорректные данные устройства",
        )

    clean_payload = sanitize_device_payload(
        payload
    )

    payload_json = json.dumps(
        clean_payload,
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
    )

    if len(payload_json) > 20000:
        raise HTTPException(
            status_code=413,
            detail="Слишком большой объём данных",
        )

    token_hash = hash_rating_token(
        token
    )

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    with connect_db() as conn:

        cursor = conn.execute(
            """
            UPDATE call_ratings

            SET
                device_data_json = ?,
                device_data_updated_at = ?

            WHERE
                token_hash = ?
                AND expires_at >= ?
            """,
            (
                payload_json,
                now_ts,
                token_hash,
                now_ts,
            ),
        )

        conn.commit()

    if cursor.rowcount != 1:
        raise HTTPException(
            status_code=404,
            detail="Ссылка не найдена или истекла",
        )

    return {
        "ok": True,
    }


# =========================================================
# ROOT
# =========================================================

@app.get("/")
def root():

    return {
        "status": "ok"
    }


# =========================================================
# GENERAL STATS
# =========================================================

@app.get("/stats")
def stats(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    start_ts = p[
        "start_ts"
    ]

    end_ts = p[
        "end_ts"
    ]

    with connect_db() as conn:

        row = conn.execute(
            """
            SELECT

                COUNT(*)
                    AS calls,

                SUM(
                    CASE
                        WHEN COALESCE(
                            is_internal_contact,
                            0
                        ) = 1

                        THEN 1
                        ELSE 0
                    END
                )
                    AS internal_calls,

                COUNT(
                    DISTINCT

                    CASE

                        WHEN
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            client_key IS NOT NULL

                            AND

                            client_key != ''

                        THEN client_key

                    END
                )
                    AS unique_clients,

                SUM(
                    CASE
                        WHEN direction = 0
                        THEN 1
                        ELSE 0
                    END
                )
                    AS incoming,

                SUM(
                    CASE
                        WHEN direction = 1
                        THEN 1
                        ELSE 0
                    END
                )
                    AS outgoing,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN 1
                        ELSE 0
                    END
                )
                    AS answered,

                SUM(
                    CASE
                        WHEN
                            direction = 0
                            AND
                            answered = 0
                            AND
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                        THEN 1
                        ELSE 0
                    END
                )
                    AS missed,

                SUM(
                    CASE
                        WHEN
                            direction = 0
                            AND
                            answered = 1

                        THEN 1
                        ELSE 0
                    END
                )
                    AS incoming_answered,

                AVG(
                    CASE
                        WHEN answered = 1
                        THEN duration
                    END
                )
                    AS avg_duration,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN duration
                        ELSE 0
                    END
                )
                    AS total_duration,

                AVG(
                    CASE
                        WHEN
                            direction = 0
                            AND
                            answered = 1
                            AND
                            answer_time > start_time

                        THEN
                            answer_time
                            - start_time
                    END
                )
                    AS avg_answer_delay

            FROM calls

            WHERE
                start_time >= ?

                AND

                start_time < ?
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        # -------------------------------------------------
        # CLIENTS
        # -------------------------------------------------

        clients_row = conn.execute(
            """
            WITH period_clients AS (

                SELECT DISTINCT
                    client_key

                FROM calls

                WHERE
                    start_time >= ?

                    AND

                    start_time < ?

                    AND

                    COALESCE(
                        is_internal_contact,
                        0
                    ) = 0

                    AND

                    client_key IS NOT NULL

                    AND

                    client_key != ''
            )

            SELECT

                SUM(
                    CASE

                        WHEN NOT EXISTS (

                            SELECT 1

                            FROM calls AS old_call

                            WHERE
                                old_call.client_key =
                                    period_clients.client_key

                                AND

                                COALESCE(
                                    old_call.is_internal_contact,
                                    0
                                ) = 0

                                AND

                                old_call.start_time < ?
                        )

                        THEN 1
                        ELSE 0

                    END
                )
                    AS new_clients,

                SUM(
                    CASE

                        WHEN EXISTS (

                            SELECT 1

                            FROM calls AS old_call

                            WHERE
                                old_call.client_key =
                                    period_clients.client_key

                                AND

                                COALESCE(
                                    old_call.is_internal_contact,
                                    0
                                ) = 0

                                AND

                                old_call.start_time < ?
                        )

                        THEN 1
                        ELSE 0

                    END
                )
                    AS repeat_clients

            FROM period_clients
            """,
            (
                start_ts,
                end_ts,

                start_ts,
                start_ts,
            ),
        ).fetchone()

        # -------------------------------------------------
        # MISSED
        # -------------------------------------------------

        missed_row = conn.execute(
            """
            WITH missed_clients AS (

                SELECT
                    client_key,

                    MIN(start_time)
                        AS first_missed_time

                FROM calls

                WHERE
                    direction = 0

                    AND

                    answered = 0

                    AND

                    COALESCE(
                        is_internal_contact,
                        0
                    ) = 0

                    AND

                    start_time >= ?

                    AND

                    start_time < ?

                    AND

                    client_key IS NOT NULL

                    AND

                    client_key != ''

                GROUP BY
                    client_key
            )

            SELECT

                COUNT(*)
                    AS unique_missed_clients,

                SUM(
                    CASE

                        WHEN EXISTS (

                            SELECT 1

                            FROM calls AS callback

                            WHERE
                                callback.client_key =
                                    missed_clients.client_key

                                AND

                                callback.direction = 1

                                AND

                                callback.start_time >
                                    missed_clients.first_missed_time
                        )

                        THEN 1
                        ELSE 0

                    END
                )
                    AS missed_called_back,

                SUM(
                    CASE

                        WHEN EXISTS (

                            SELECT 1

                            FROM calls AS contact

                            WHERE
                                contact.client_key =
                                    missed_clients.client_key

                                AND

                                contact.answered = 1

                                AND

                                contact.start_time >
                                    missed_clients.first_missed_time
                        )

                        THEN 1
                        ELSE 0

                    END
                )
                    AS missed_contacted,

                SUM(
                    CASE

                        WHEN NOT EXISTS (

                            SELECT 1

                            FROM calls AS contact

                            WHERE
                                contact.client_key =
                                    missed_clients.client_key

                                AND

                                contact.answered = 1

                                AND

                                contact.start_time >
                                    missed_clients.first_missed_time
                        )

                        THEN 1
                        ELSE 0

                    END
                )
                    AS missed_not_processed

            FROM missed_clients
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        # -------------------------------------------------
        # SALES
        # -------------------------------------------------

        sales_row = conn.execute(
            """
            SELECT

                SUM(
                    CASE
                        WHEN
                            answered = 1

                            AND

                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            sale_status =
                                'bought'

                        THEN 1
                        ELSE 0
                    END
                )
                    AS bought,

                SUM(
                    CASE
                        WHEN
                            answered = 1

                            AND

                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            sale_status =
                                'not_bought'

                        THEN 1
                        ELSE 0
                    END
                )
                    AS not_bought,

                SUM(
                    CASE
                        WHEN
                            answered = 1

                            AND

                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            (
                                sale_status IS NULL
                                OR
                                sale_status = ''
                            )

                        THEN 1
                        ELSE 0
                    END
                )
                    AS sale_unmarked,

                SUM(
                    CASE
                        WHEN
                            answered = 1

                            AND

                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            (
                                talk_manager_code IS NULL
                                OR
                                talk_manager_code = ''
                            )

                        THEN 1
                        ELSE 0
                    END
                )
                    AS manager_unmarked

            FROM calls

            WHERE
                start_time >= ?

                AND

                start_time < ?
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        ratings_row = conn.execute(
            """
            SELECT
                COUNT(
                    CASE
                        WHEN
                            rating.sms_sent_at
                                IS NOT NULL
                            OR
                            rating.score
                                IS NOT NULL
                        THEN 1
                    END
                )
                    AS invitations_sent,

                COUNT(rating.score)
                    AS ratings_count,

                AVG(rating.score)
                    AS average_rating,

                SUM(
                    CASE
                        WHEN rating.score = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS score_1,

                SUM(
                    CASE
                        WHEN rating.score = 2
                        THEN 1
                        ELSE 0
                    END
                ) AS score_2,

                SUM(
                    CASE
                        WHEN rating.score = 3
                        THEN 1
                        ELSE 0
                    END
                ) AS score_3,

                SUM(
                    CASE
                        WHEN rating.score = 4
                        THEN 1
                        ELSE 0
                    END
                ) AS score_4,

                SUM(
                    CASE
                        WHEN rating.score = 5
                        THEN 1
                        ELSE 0
                    END
                ) AS score_5

            FROM calls AS call

            LEFT JOIN call_ratings AS rating
                ON rating.call_id = call.id

            WHERE
                call.start_time >= ?

                AND

                call.start_time < ?

                AND

                call.answered = 1

                AND

                COALESCE(
                    call.is_internal_contact,
                    0
                ) = 0
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

    incoming = (
        row["incoming"]
        or 0
    )

    incoming_answered = (
        row["incoming_answered"]
        or 0
    )

    answer_rate = (
        round(
            (
                incoming_answered
                / incoming
            )
            * 100,
            1,
        )

        if incoming
        else 0
    )

    bought = (
        sales_row[
            "bought"
        ]
        or 0
    )

    not_bought = (
        sales_row[
            "not_bought"
        ]
        or 0
    )

    marked_results = (
        bought
        + not_bought
    )

    sale_conversion = (
        round(
            (
                bought
                / marked_results
            )
            * 100,
            1,
        )

        if marked_results
        else 0
    )

    ratings_count = (
        ratings_row[
            "ratings_count"
        ]
        or 0
    )

    rating_invitations_sent = (
        ratings_row[
            "invitations_sent"
        ]
        or 0
    )

    rating_response_rate = (
        round(
            (
                ratings_count
                / rating_invitations_sent
            )
            * 100,
            1,
        )
        if rating_invitations_sent
        else 0
    )

    return {
        "period": {
            "type":
                p["period"],

            "label":
                p["label"],

            "date_from":
                p["start_date"],

            "date_to":
                p["end_date"],

            "days":
                p["days"],
        },

        "stats": {
            "calls":
                row["calls"]
                or 0,

            "internal_calls":
                row[
                    "internal_calls"
                ]
                or 0,

            "unique_clients":
                row[
                    "unique_clients"
                ]
                or 0,

            "incoming":
                incoming,

            "outgoing":
                row["outgoing"]
                or 0,

            "answered":
                row["answered"]
                or 0,

            "missed":
                row["missed"]
                or 0,

            "answer_rate":
                answer_rate,

            "new_clients":
                clients_row[
                    "new_clients"
                ]
                or 0,

            "repeat_clients":
                clients_row[
                    "repeat_clients"
                ]
                or 0,

            "unique_missed_clients":
                missed_row[
                    "unique_missed_clients"
                ]
                or 0,

            "missed_called_back":
                missed_row[
                    "missed_called_back"
                ]
                or 0,

            "missed_contacted":
                missed_row[
                    "missed_contacted"
                ]
                or 0,

            "missed_not_processed":
                missed_row[
                    "missed_not_processed"
                ]
                or 0,

            "average_duration_seconds":
                round(
                    row[
                        "avg_duration"
                    ]
                    or 0
                ),

            "total_duration_seconds":
                row[
                    "total_duration"
                ]
                or 0,

            "average_answer_delay_seconds":
                round(
                    row[
                        "avg_answer_delay"
                    ]
                    or 0
                ),

            "bought":
                bought,

            "not_bought":
                not_bought,

            "sale_unmarked":
                sales_row[
                    "sale_unmarked"
                ]
                or 0,

            "manager_unmarked":
                sales_row[
                    "manager_unmarked"
                ]
                or 0,

            "sale_conversion":
                sale_conversion,

            "average_rating":
                (
                    round(
                        float(
                            ratings_row[
                                "average_rating"
                            ]
                        ),
                        2,
                    )
                    if ratings_row[
                        "average_rating"
                    ] is not None
                    else None
                ),

            "ratings_count":
                ratings_count,

            "rating_invitations_sent":
                rating_invitations_sent,

            "rating_response_rate":
                rating_response_rate,

            "rating_distribution": {
                str(score): (
                    ratings_row[
                        f"score_{score}"
                    ]
                    or 0
                )
                for score in range(
                    1,
                    6,
                )
            },
        },
    }


# =========================================================
# SALES REASONS
# =========================================================

@app.get(
    "/stats/sales/reasons"
)
def stats_sales_reasons(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    with connect_db() as conn:

        rows = conn.execute(
            """
            SELECT

                no_sale_reason_code,

                no_sale_reason,

                COUNT(*)
                    AS count

            FROM calls

            WHERE
                start_time >= ?

                AND

                start_time < ?

                AND

                COALESCE(
                    is_internal_contact,
                    0
                ) = 0

                AND

                sale_status =
                    'not_bought'

            GROUP BY
                no_sale_reason_code,
                no_sale_reason

            ORDER BY
                count DESC
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    return {
        "results": [
            {
                "code":
                    row[
                        "no_sale_reason_code"
                    ],

                "reason":
                    row[
                        "no_sale_reason"
                    ]
                    or "Не указано",

                "count":
                    row["count"]
                    or 0,
            }

            for row in rows
        ]
    }


# =========================================================
# TIMELINE
# =========================================================

@app.get(
    "/stats/timeline"
)
def stats_timeline(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    with connect_db() as conn:

        if p["days"] == 1:

            rows = conn.execute(
                """
                SELECT

                    CAST(
                        strftime(
                            '%H',
                            start_time,
                            'unixepoch',
                            '+5 hours'
                        )
                        AS INTEGER
                    )
                        AS bucket,

                    COUNT(*)
                        AS calls,

                    SUM(
                        CASE
                            WHEN direction = 0
                            THEN 1
                            ELSE 0
                        END
                    )
                        AS incoming,

                    SUM(
                        CASE
                            WHEN direction = 1
                            THEN 1
                            ELSE 0
                        END
                    )
                        AS outgoing

                FROM calls

                WHERE
                    start_time >= ?

                    AND

                    start_time < ?

                GROUP BY bucket

                ORDER BY bucket
                """,
                (
                    p["start_ts"],
                    p["end_ts"],
                ),
            ).fetchall()

            by_hour = {
                row["bucket"]:
                    row

                for row in rows
            }

            points = []

            for hour in range(
                8,
                23,
            ):

                row = by_hour.get(
                    hour
                )

                points.append(
                    {
                        "key":
                            hour,

                        "label":
                            f"{hour:02d}:00",

                        "calls":
                            (
                                row["calls"]
                                if row
                                else 0
                            ),

                        "incoming":
                            (
                                row["incoming"]
                                if row
                                else 0
                            ),

                        "outgoing":
                            (
                                row["outgoing"]
                                if row
                                else 0
                            ),
                    }
                )

            granularity = "hour"

        else:

            rows = conn.execute(
                """
                SELECT

                    date(
                        start_time,
                        'unixepoch',
                        '+5 hours'
                    )
                        AS bucket,

                    COUNT(*)
                        AS calls,

                    SUM(
                        CASE
                            WHEN direction = 0
                            THEN 1
                            ELSE 0
                        END
                    )
                        AS incoming,

                    SUM(
                        CASE
                            WHEN direction = 1
                            THEN 1
                            ELSE 0
                        END
                    )
                        AS outgoing

                FROM calls

                WHERE
                    start_time >= ?

                    AND

                    start_time < ?

                GROUP BY bucket

                ORDER BY bucket
                """,
                (
                    p["start_ts"],
                    p["end_ts"],
                ),
            ).fetchall()

            by_date = {
                row["bucket"]:
                    row

                for row in rows
            }

            points = []

            current = parse_date(
                p["start_date"]
            )

            last = parse_date(
                p["end_date"]
            )

            while current <= last:

                key = (
                    current.isoformat()
                )

                row = by_date.get(
                    key
                )

                points.append(
                    {
                        "key":
                            key,

                        "label":
                            current.strftime(
                                "%d.%m"
                            ),

                        "calls":
                            (
                                row["calls"]
                                if row
                                else 0
                            ),

                        "incoming":
                            (
                                row["incoming"]
                                if row
                                else 0
                            ),

                        "outgoing":
                            (
                                row["outgoing"]
                                if row
                                else 0
                            ),
                    }
                )

                current += timedelta(
                    days=1
                )

            granularity = "day"

    return {
        "granularity":
            granularity,

        "points":
            points,
    }


@app.get(
    "/stats/hourly"
)
def stats_hourly():

    return stats_timeline(
        period="today"
    )


# =========================================================
# MANAGER STATISTICS
# =========================================================

@app.get(
    "/stats/managers"
)
def stats_managers(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    with connect_db() as conn:

        rows = conn.execute(
            """
            SELECT

                COALESCE(
                    NULLIF(
                        talk_manager_name,
                        ''
                    ),
                    'Не указан'
                )
                    AS manager,

                COUNT(*)
                    AS calls,

                COUNT(
                    DISTINCT

                    CASE
                        WHEN
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            client_key IS NOT NULL

                            AND

                            client_key != ''

                        THEN client_key
                    END
                )
                    AS unique_clients,

                SUM(
                    CASE
                        WHEN direction = 0
                        THEN 1
                        ELSE 0
                    END
                )
                    AS incoming,

                SUM(
                    CASE
                        WHEN direction = 1
                        THEN 1
                        ELSE 0
                    END
                )
                    AS outgoing,

                SUM(
                    CASE
                        WHEN
                            direction = 0

                            AND

                            answered = 0

                            AND

                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                        THEN 1
                        ELSE 0
                    END
                )
                    AS missed,

                SUM(
                    CASE
                        WHEN
                            direction = 0

                            AND

                            answered = 1

                        THEN 1
                        ELSE 0
                    END
                )
                    AS incoming_answered,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN duration
                        ELSE 0
                    END
                )
                    AS total_duration,

                SUM(
                    CASE
                        WHEN
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            sale_status =
                                'bought'

                        THEN 1
                        ELSE 0
                    END
                )
                    AS bought,

                SUM(
                    CASE
                        WHEN
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            sale_status =
                                'not_bought'

                        THEN 1
                        ELSE 0
                    END
                )
                    AS not_bought,

                SUM(
                    CASE
                        WHEN
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 1

                        THEN 1
                        ELSE 0
                    END
                )
                    AS internal_calls,

                COUNT(rating.score)
                    AS ratings_count,

                AVG(rating.score)
                    AS average_rating,

                COUNT(
                    CASE
                        WHEN
                            rating.sms_sent_at
                                IS NOT NULL
                            OR
                            rating.score
                                IS NOT NULL
                        THEN 1
                    END
                )
                    AS rating_invitations_sent

            FROM calls

            LEFT JOIN (
                SELECT
                    call_id,
                    score,
                    sms_sent_at

                FROM call_ratings
            ) AS rating
                ON rating.call_id = calls.id

            WHERE
                start_time >= ?

                AND

                start_time < ?

            GROUP BY
                manager

            ORDER BY
                calls DESC
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    results = []

    for row in rows:

        incoming = (
            row["incoming"]
            or 0
        )

        incoming_answered = (
            row[
                "incoming_answered"
            ]
            or 0
        )

        bought = (
            row["bought"]
            or 0
        )

        not_bought = (
            row["not_bought"]
            or 0
        )

        marked = (
            bought
            + not_bought
        )

        ratings_count = (
            row[
                "ratings_count"
            ]
            or 0
        )

        rating_invitations_sent = (
            row[
                "rating_invitations_sent"
            ]
            or 0
        )

        results.append(
            {
                "manager":
                    row["manager"],

                "calls":
                    row["calls"]
                    or 0,

                "unique_clients":
                    row[
                        "unique_clients"
                    ]
                    or 0,

                "incoming":
                    incoming,

                "outgoing":
                    row["outgoing"]
                    or 0,

                "missed":
                    row["missed"]
                    or 0,

                "internal_calls":
                    row[
                        "internal_calls"
                    ]
                    or 0,

                "answer_rate":
                    (
                        round(
                            (
                                incoming_answered
                                / incoming
                            )
                            * 100,
                            1,
                        )

                        if incoming

                        else 0
                    ),

                "total_duration_seconds":
                    row[
                        "total_duration"
                    ]
                    or 0,

                "bought":
                    bought,

                "not_bought":
                    not_bought,

                "sale_conversion":
                    (
                        round(
                            (
                                bought
                                / marked
                            )
                            * 100,
                            1,
                        )

                        if marked

                        else 0
                    ),

                "average_rating":
                    (
                        round(
                            float(
                                row[
                                    "average_rating"
                                ]
                            ),
                            2,
                        )
                        if row[
                            "average_rating"
                        ] is not None
                        else None
                    ),

                "ratings_count":
                    ratings_count,

                "rating_invitations_sent":
                    rating_invitations_sent,

                "rating_response_rate":
                    (
                        round(
                            (
                                ratings_count
                                / rating_invitations_sent
                            )
                            * 100,
                            1,
                        )
                        if rating_invitations_sent
                        else 0
                    ),
            }
        )

    return {
        "results":
            results
    }


# =========================================================
# DAILY CUSTOMER RATINGS BY MANAGER
# =========================================================

@app.get(
    "/stats/ratings/daily"
)
def stats_ratings_daily(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    with connect_db() as conn:

        rows = conn.execute(
            """
            SELECT
                date(
                    datetime(
                        call.start_time,
                        'unixepoch',
                        '+5 hours'
                    )
                )
                    AS call_date,

                COALESCE(
                    NULLIF(
                        call.talk_manager_code,
                        ''
                    ),
                    'unmarked'
                )
                    AS manager_code,

                COALESCE(
                    NULLIF(
                        call.talk_manager_name,
                        ''
                    ),
                    'Не указан'
                )
                    AS manager,

                COUNT(rating.score)
                    AS ratings_count,

                AVG(rating.score)
                    AS average_rating,

                COUNT(
                    CASE
                        WHEN
                            rating.sms_sent_at
                                IS NOT NULL
                            OR
                            rating.score
                                IS NOT NULL
                        THEN 1
                    END
                )
                    AS invitations_sent

            FROM calls AS call

            LEFT JOIN call_ratings AS rating
                ON rating.call_id = call.id

            WHERE
                call.start_time >= ?

                AND

                call.start_time < ?

                AND

                call.answered = 1

                AND

                COALESCE(
                    call.is_internal_contact,
                    0
                ) = 0

            GROUP BY
                call_date,
                manager_code,
                manager

            HAVING
                invitations_sent > 0
                OR ratings_count > 0

            ORDER BY
                call_date DESC,
                manager
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    results = []

    for row in rows:

        invitations_sent = (
            row[
                "invitations_sent"
            ]
            or 0
        )

        ratings_count = (
            row[
                "ratings_count"
            ]
            or 0
        )

        results.append(
            {
                "date":
                    row["call_date"],

                "manager_code":
                    row["manager_code"],

                "manager":
                    row["manager"],

                "average_rating":
                    (
                        round(
                            float(
                                row[
                                    "average_rating"
                                ]
                            ),
                            2,
                        )
                        if row[
                            "average_rating"
                        ] is not None
                        else None
                    ),

                "ratings_count":
                    ratings_count,

                "invitations_sent":
                    invitations_sent,

                "response_rate":
                    (
                        round(
                            (
                                ratings_count
                                / invitations_sent
                            )
                            * 100,
                            1,
                        )
                        if invitations_sent
                        else 0
                    ),
            }
        )

    return {
        "results":
            results
    }


# =========================================================
# SIM
# =========================================================

@app.get(
    "/stats/sims"
)
def stats_sims(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    with connect_db() as conn:

        rows = conn.execute(
            """
            SELECT

                COALESCE(
                    NULLIF(
                        src_number,
                        ''
                    ),
                    'Неизвестно'
                )
                    AS sim,

                src_slot,

                COUNT(*)
                    AS calls,

                COUNT(
                    DISTINCT

                    CASE
                        WHEN
                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                            AND

                            client_key IS NOT NULL

                            AND

                            client_key != ''

                        THEN client_key
                    END
                )
                    AS unique_clients,

                SUM(
                    CASE
                        WHEN direction = 0
                        THEN 1
                        ELSE 0
                    END
                )
                    AS incoming,

                SUM(
                    CASE
                        WHEN direction = 1
                        THEN 1
                        ELSE 0
                    END
                )
                    AS outgoing,

                SUM(
                    CASE
                        WHEN
                            direction = 0

                            AND

                            answered = 0

                            AND

                            COALESCE(
                                is_internal_contact,
                                0
                            ) = 0

                        THEN 1
                        ELSE 0
                    END
                )
                    AS missed,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN duration
                        ELSE 0
                    END
                )
                    AS total_duration

            FROM calls

            WHERE
                start_time >= ?

                AND

                start_time < ?

            GROUP BY
                sim,
                src_slot

            ORDER BY
                calls DESC
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    return {
        "results": [
            {
                "sim":
                    row["sim"],

                "slot":
                    row["src_slot"],

                "calls":
                    row["calls"]
                    or 0,

                "unique_clients":
                    row[
                        "unique_clients"
                    ]
                    or 0,

                "incoming":
                    row["incoming"]
                    or 0,

                "outgoing":
                    row["outgoing"]
                    or 0,

                "missed":
                    row["missed"]
                    or 0,

                "total_duration_seconds":
                    row[
                        "total_duration"
                    ]
                    or 0,
            }

            for row in rows
        ]
    }


# =========================================================
# RECENT CALLS
# =========================================================

@app.get(
    "/stats/recent"
)
def stats_recent(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
):

    p = get_period(
        period,
        date_from,
        date_to,
    )

    limit = max(
        1,
        min(
            limit,
            100,
        ),
    )

    with connect_db() as conn:

        rows = conn.execute(
            """
            SELECT

                calls.id AS id,
                db_call_id,

                client_number,
                client_name,

                is_internal_contact,
                internal_contact_name,

                direction,
                answered,

                talk_manager_name,

                src_number,

                start_time,

                duration,

                sale_status,
                no_sale_reason,

                rating.score
                    AS customer_rating,

                rating.sms_status
                    AS rating_sms_status

            FROM calls

            LEFT JOIN call_ratings AS rating
                ON rating.call_id = calls.id

            WHERE
                start_time >= ?

                AND

                start_time < ?

            ORDER BY
                start_time DESC

            LIMIT ?
            """,
            (
                p["start_ts"],
                p["end_ts"],
                limit,
            ),
        ).fetchall()

    results = []

    for row in rows:

        start_time = (
            row["start_time"]
            or 0
        )

        local_time = (
            datetime
            .fromtimestamp(
                start_time,
                UZ_TZ,
            )
            .strftime(
                "%d.%m.%Y %H:%M"
            )

            if start_time

            else "—"
        )

        results.append(
            {
                "id":
                    row["id"],

                "db_call_id":
                    row["db_call_id"],

                "client_number":
                    row[
                        "client_number"
                    ]
                    or "—",

                "client_name":
                    row[
                        "client_name"
                    ]
                    or "",

                "is_internal_contact":
                    bool(
                        row[
                            "is_internal_contact"
                        ]
                    ),

                "internal_contact_name":
                    row[
                        "internal_contact_name"
                    ]
                    or "",

                "direction":
                    row["direction"],

                "answered":
                    row["answered"]
                    or 0,

                "manager":
                    row[
                        "talk_manager_name"
                    ]
                    or "—",

                "sim":
                    row[
                        "src_number"
                    ]
                    or "—",

                "local_time":
                    local_time,

                "duration":
                    row["duration"]
                    or 0,

                "sale_status":
                    row[
                        "sale_status"
                    ],

                "no_sale_reason":
                    row[
                        "no_sale_reason"
                    ],

                "customer_rating":
                    row[
                        "customer_rating"
                    ],

                "rating_sms_status":
                    row[
                        "rating_sms_status"
                    ],
            }
        )

    return {
        "results":
            results
    }


def format_uz_datetime(
    value,
):

    if value in {
        None,
        "",
        0,
    }:
        return "—"

    try:
        return (
            datetime
            .fromtimestamp(
                int(value),
                UZ_TZ,
            )
            .strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        )
    except (
        TypeError,
        ValueError,
        OSError,
    ):
        return str(value)


def parse_saved_json(
    value,
):

    if not value:
        return {}

    try:
        parsed = json.loads(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return {
            "raw": str(value),
        }

    return parsed


@app.get(
    "/stats/rating/details/{call_id}"
)
def rating_details(
    call_id: int,
):

    with connect_db() as conn:

        row = conn.execute(
            """
            SELECT
                call.id,
                call.db_call_id,
                call.event_pbx_call_id,
                call.client_number,
                call.client_name,
                call.is_internal_contact,
                call.internal_contact_name,
                call.direction,
                call.answered,
                call.user_id,
                call.user_login,
                call.src_number,
                call.src_id,
                call.src_slot,
                call.event_created,
                call.start_time,
                call.answer_time,
                call.end_time,
                call.duration,
                call.api_duration,
                call.duration_source,
                call.recording,
                call.account_id,
                call.account_name,
                call.talk_manager_code,
                call.talk_manager_name,
                call.manager_marked_at,
                call.manager_marked_username,
                call.sale_status,
                call.no_sale_reason,
                call.no_sale_reason_code,
                call.sale_marked_at,
                call.sale_marked_username,
                call.created_at AS call_created_at,

                rating.sender_user_login,
                rating.sms_status,
                rating.sms_reserved_at,
                rating.sms_sent_at,
                rating.sms_error,
                rating.provider_response,
                rating.score,
                rating.rated_at,
                rating.expires_at,
                rating.created_at AS rating_created_at,
                rating.first_opened_at,
                rating.last_opened_at,
                rating.open_count,
                rating.first_ip,
                rating.last_ip,
                rating.rated_ip,
                rating.user_agent,
                rating.accept_language,
                rating.referer,
                rating.request_headers_json,
                rating.device_data_json,
                rating.device_data_updated_at

            FROM calls AS call

            JOIN call_ratings AS rating
                ON rating.call_id = call.id

            WHERE
                call.id = ?
                AND rating.score IS NOT NULL

            LIMIT 1
            """,
            (
                call_id,
            ),
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Оценка для этого звонка не найдена",
        )

    direction = (
        "Входящий"
        if row["direction"] == 0
        else "Исходящий"
    )

    answered = (
        "Отвечен"
        if row["answered"]
        else "Не отвечен"
    )

    sale_labels = {
        "bought": "Купил",
        "not_bought": "Не купил",
    }

    return {
        "call_id": row["id"],
        "score": row["score"],
        "sections": [
            {
                "title": "Звонок",
                "items": {
                    "ID звонка": row["id"],
                    "ID Мои Звонки": row["db_call_id"],
                    "PBX ID": row["event_pbx_call_id"],
                    "Клиент": row["client_number"] or "—",
                    "Имя клиента": row["client_name"] or "—",
                    "Внутренний контакт": (
                        row["internal_contact_name"]
                        if row["is_internal_contact"]
                        else "Нет"
                    ),
                    "Направление": direction,
                    "Статус": answered,
                    "Начало": format_uz_datetime(row["start_time"]),
                    "Ответ": format_uz_datetime(row["answer_time"]),
                    "Окончание": format_uz_datetime(row["end_time"]),
                    "Разговор, сек": row["duration"] or 0,
                    "Длительность API, сек": row["api_duration"],
                    "Источник длительности": row["duration_source"] or "—",
                    "Запись": row["recording"] or "—",
                },
            },
            {
                "title": "Менеджер и результат",
                "items": {
                    "Менеджер": row["talk_manager_name"] or "—",
                    "Код менеджера": row["talk_manager_code"] or "—",
                    "Менеджер отмечен": format_uz_datetime(row["manager_marked_at"]),
                    "Кто отметил менеджера": row["manager_marked_username"] or "—",
                    "Результат": sale_labels.get(row["sale_status"], "Не отмечено"),
                    "Причина": row["no_sale_reason"] or "—",
                    "Код причины": row["no_sale_reason_code"] or "—",
                    "Результат отмечен": format_uz_datetime(row["sale_marked_at"]),
                    "Кто отметил результат": row["sale_marked_username"] or "—",
                },
            },
            {
                "title": "SIM и аккаунт",
                "items": {
                    "SIM / номер телефона": row["src_number"] or "—",
                    "SIM ID": row["src_id"],
                    "SIM слот": row["src_slot"],
                    "Пользователь": row["user_login"] or "—",
                    "ID пользователя": row["user_id"],
                    "Аккаунт": row["account_name"] or "—",
                    "ID аккаунта": row["account_id"],
                    "Время события": row["event_created"] or "—",
                    "Запись создана": row["call_created_at"] or "—",
                },
            },
            {
                "title": "Оценка и SMS",
                "items": {
                    "Оценка": f'{row["score"]} из 5',
                    "Оценено": format_uz_datetime(row["rated_at"]),
                    "SMS статус": row["sms_status"] or "—",
                    "SMS зарезервировано": format_uz_datetime(row["sms_reserved_at"]),
                    "SMS отправлено": format_uz_datetime(row["sms_sent_at"]),
                    "Телефон-отправитель": row["sender_user_login"] or "—",
                    "Ошибка SMS": row["sms_error"] or "—",
                    "Ответ SMS-сервиса": row["provider_response"] or "—",
                    "Ссылка действует до": format_uz_datetime(row["expires_at"]),
                    "Запрос оценки создан": row["rating_created_at"] or "—",
                },
            },
            {
                "title": "Открытие страницы",
                "items": {
                    "Первое открытие": format_uz_datetime(row["first_opened_at"]),
                    "Последнее открытие": format_uz_datetime(row["last_opened_at"]),
                    "Количество открытий": row["open_count"] or 0,
                    "Первый IP": row["first_ip"] or "—",
                    "Последний IP": row["last_ip"] or "—",
                    "IP при оценке": row["rated_ip"] or "—",
                    "User-Agent": row["user_agent"] or "—",
                    "Язык HTTP": row["accept_language"] or "—",
                    "Источник перехода": row["referer"] or "—",
                    "HTTP-заголовки": parse_saved_json(row["request_headers_json"]),
                },
            },
            {
                "title": "Телефон и браузер",
                "items": {
                    "Данные браузера": parse_saved_json(row["device_data_json"]),
                    "Обновлены": format_uz_datetime(row["device_data_updated_at"]),
                },
            },
        ],
    }


# =========================================================
# DASHBOARD
# =========================================================

@app.get(
    "/dashboard",
    response_class=HTMLResponse,
)
def dashboard():

    return """
<!DOCTYPE html>
<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Texnikach Call Dashboard</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #111;
    color: #fff;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}

.container {
    width: 100%;
    max-width: 1800px;

    margin: 0 auto;
    padding: 26px;
}

.header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;

    gap: 20px;
    margin-bottom: 28px;
}

.title {
    margin: 0;
    font-size: 34px;
}

.subtitle {
    margin-top: 7px;
    color: #888;
    font-size: 14px;
}

.periods {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.period-btn,
.chart-btn,
.apply-btn {
    appearance: none;

    border: 1px solid #333;
    border-radius: 10px;

    background: #1b1b1b;
    color: #999;

    padding: 10px 14px;

    cursor: pointer;
    font-weight: 600;
}

.period-btn.active,
.chart-btn.active {
    color: #111;
    background: #d9b565;
    border-color: #d9b565;
}

.custom-panel {
    display: none;

    margin-bottom: 20px;

    padding: 16px;

    background: #1b1b1b;

    border: 1px solid #333;
    border-radius: 14px;

    gap: 10px;

    align-items: center;
    flex-wrap: wrap;
}

.custom-panel.visible {
    display: flex;
}

.custom-panel input {
    border: 1px solid #3a3a3a;

    background: #111;
    color: #fff;

    border-radius: 8px;

    padding: 9px 10px;
}

.apply-btn {
    color: #111;
    background: #d9b565;
    border-color: #d9b565;
}

.section-title {
    margin: 30px 0 14px;
    font-size: 20px;
}

.grid {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                205px,
                1fr
            )
        );

    gap: 15px;
}

.card,
.panel {
    background: #1b1b1b;

    border: 1px solid #333;
    border-radius: 16px;
}

.card {
    padding: 20px;
    min-height: 118px;
}

.card.good {
    border-color:
        rgba(
            120,
            220,
            145,
            .25
        );
}

.card.bad {
    border-color:
        rgba(
            255,
            120,
            120,
            .25
        );
}

.card.warn {
    border-color:
        rgba(
            217,
            181,
            101,
            .35
        );
}

.label {
    color: #a5a5a5;
    font-size: 14px;
    margin-bottom: 12px;
}

.value {
    font-size: 32px;
    line-height: 1.12;
    font-weight: 750;
}

.panel {
    margin-top: 24px;
    padding: 22px;
}

.panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;

    gap: 16px;

    flex-wrap: wrap;

    margin-bottom: 18px;
}

.panel h2 {
    margin: 0;
    font-size: 24px;
}

.chart-buttons {
    display: flex;
    gap: 7px;
}

.chart {
    display: flex;
    align-items: flex-end;

    gap: 9px;

    min-height: 280px;

    overflow-x: auto;

    padding: 30px 4px 0;
}

.bar-wrap {
    flex: 1;
    min-width: 44px;
    text-align: center;
}

.bar-value {
    height: 22px;
    font-size: 12px;
    font-weight: 600;
}

.bar {
    width: 70%;

    max-width: 58px;
    min-height: 2px;

    margin: 0 auto;

    background: #d9b565;

    border-radius:
        8px
        8px
        2px
        2px;
}

.bar.outgoing {
    opacity: .5;
}

.bar-label {
    margin-top: 8px;

    color: #999;

    font-size: 11px;

    white-space: nowrap;
}

.tables {
    display: grid;

    grid-template-columns:
        repeat(
            2,
            minmax(
                0,
                1fr
            )
        );

    gap: 24px;
}

.table-wrap {
    overflow-x: auto;
}

table {
    width: 100%;

    border-collapse: collapse;

    min-width: 650px;
}

th {
    text-align: left;

    color: #888;

    font-size: 12px;
    font-weight: 600;

    padding: 11px 10px;

    border-bottom:
        1px solid #333;
}

td {
    padding: 13px 10px;

    border-bottom:
        1px solid #292929;

    font-size: 13px;
}

tbody tr:last-child td {
    border-bottom: 0;
}

.phone {
    font-family:
        ui-monospace,
        SFMono-Regular,
        Menlo,
        monospace;
}

.sale-bought {
    color: #a9e5b3;
}

.sale-not-bought {
    color: #ffabab;
}

.sale-unmarked {
    color: #777;
}

.contact {
    color: #d9b565;
}

.empty {
    color: #777;
}

.details-button {
    appearance: none;
    border: 1px solid #d9b565;
    border-radius: 9px;
    background: transparent;
    color: #d9b565;
    padding: 7px 10px;
    cursor: pointer;
    white-space: nowrap;
    font-weight: 650;
}

.details-button:hover,
.details-button:focus {
    background: #d9b565;
    color: #111;
}

.modal-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: rgba(0, 0, 0, .78);
}

.modal-overlay.visible {
    display: flex;
}

.modal-card {
    width: min(980px, 100%);
    max-height: calc(100vh - 40px);
    overflow: auto;
    border: 1px solid #3a3a3a;
    border-radius: 18px;
    background: #171717;
    box-shadow: 0 24px 80px rgba(0, 0, 0, .55);
}

.modal-header {
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 18px 20px;
    border-bottom: 1px solid #333;
    background: #171717;
}

.modal-title {
    margin: 0;
    font-size: 21px;
}

.modal-close {
    width: 38px;
    height: 38px;
    border: 1px solid #444;
    border-radius: 10px;
    background: #222;
    color: #fff;
    cursor: pointer;
    font-size: 24px;
    line-height: 1;
}

.modal-content {
    padding: 20px;
}

.details-section {
    margin-bottom: 22px;
}

.details-section:last-child {
    margin-bottom: 0;
}

.details-section h3 {
    margin: 0 0 10px;
    color: #d9b565;
    font-size: 16px;
}

.details-grid {
    display: grid;
    grid-template-columns: minmax(170px, 260px) minmax(260px, 1fr);
    border: 1px solid #303030;
    border-radius: 12px;
    overflow: hidden;
}

.details-label,
.details-value {
    padding: 10px 12px;
    border-bottom: 1px solid #2b2b2b;
}

.details-label {
    color: #999;
    background: #1c1c1c;
}

.details-value {
    min-width: 0;
    overflow-wrap: anywhere;
}

.details-grid > :nth-last-child(-n + 2) {
    border-bottom: 0;
}

.details-value pre {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: #ddd;
    font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
}

.details-value a {
    color: #d9b565;
}

@media (
    max-width: 1000px
) {

    .header {
        flex-direction: column;
    }

    .tables {
        grid-template-columns: 1fr;
    }
}

@media (
    max-width: 600px
) {

    .container {
        padding: 14px;
    }

    .title {
        font-size: 26px;
    }

    .grid {
        grid-template-columns:
            repeat(
                2,
                minmax(
                    0,
                    1fr
                )
            );

        gap: 10px;
    }

    .card {
        min-height: 105px;
        padding: 15px;
    }

    .value {
        font-size: 25px;
    }

    .panel {
        padding: 15px;
    }

    .modal-overlay {
        padding: 8px;
    }

    .modal-card {
        max-height: calc(100vh - 16px);
    }

    .modal-content {
        padding: 14px;
    }

    .details-grid {
        grid-template-columns: 1fr;
    }

    .details-label {
        border-bottom: 0;
        padding-bottom: 3px;
    }

    .details-value {
        padding-top: 3px;
    }

    .details-grid > :nth-last-child(-n + 2) {
        border-bottom: 0;
    }
}

</style>

</head>

<body>

<div class="container">

<div class="header">

    <div>

        <h1 class="title">
            Texnikach — статистика звонков
        </h1>

        <div
            class="subtitle"
            id="period_label"
        >
            Сегодня
        </div>

    </div>

    <div class="periods">

        <button
            class="period-btn active"
            data-period="today"
        >
            Сегодня
        </button>

        <button
            class="period-btn"
            data-period="yesterday"
        >
            Вчера
        </button>

        <button
            class="period-btn"
            data-period="7d"
        >
            7 дней
        </button>

        <button
            class="period-btn"
            data-period="30d"
        >
            30 дней
        </button>

        <button
            class="period-btn"
            data-period="custom"
        >
            Свой период
        </button>

    </div>

</div>


<div
    class="custom-panel"
    id="custom_panel"
>

    <span>С</span>

    <input
        id="date_from"
        type="date"
    >

    <span>по</span>

    <input
        id="date_to"
        type="date"
    >

    <button
        class="apply-btn"
        id="apply_custom"
    >
        Показать
    </button>

</div>


<div class="grid">

    <div class="card">
        <div class="label">
            Всего звонков
        </div>

        <div
            class="value"
            id="calls"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Уникальные клиенты
        </div>

        <div
            class="value"
            id="unique_clients"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Внутренние контакты
        </div>

        <div
            class="value"
            id="internal_calls"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Входящие
        </div>

        <div
            class="value"
            id="incoming"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Исходящие
        </div>

        <div
            class="value"
            id="outgoing"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Отвеченные
        </div>

        <div
            class="value"
            id="answered"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Пропущенные входящие
        </div>

        <div
            class="value"
            id="missed"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Ответили на входящие
        </div>

        <div
            class="value"
            id="answer_rate"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Новые клиенты
        </div>

        <div
            class="value"
            id="new_clients"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Повторные клиенты
        </div>

        <div
            class="value"
            id="repeat_clients"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Уникально пропущено
        </div>

        <div
            class="value"
            id="unique_missed_clients"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Были исходящие после пропущенного
        </div>

        <div
            class="value"
            id="missed_called_back"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Связались после пропущенного
        </div>

        <div
            class="value"
            id="missed_contacted"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Не обработано
        </div>

        <div
            class="value"
            id="missed_not_processed"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Средний разговор
        </div>

        <div
            class="value"
            id="average_duration"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Общее время разговоров
        </div>

        <div
            class="value"
            id="total_duration"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Среднее время ответа
        </div>

        <div
            class="value"
            id="average_answer_delay"
        >—</div>
    </div>

</div>


<h2 class="section-title">
    Продажи
</h2>


<div class="grid">

    <div class="card good">

        <div class="label">
            ✅ Купил
        </div>

        <div
            class="value"
            id="bought"
        >—</div>

    </div>


    <div class="card bad">

        <div class="label">
            ❌ Не купил
        </div>

        <div
            class="value"
            id="not_bought"
        >—</div>

    </div>


    <div class="card warn">

        <div class="label">
            Результат не отмечен
        </div>

        <div
            class="value"
            id="sale_unmarked"
        >—</div>

    </div>


    <div class="card warn">

        <div class="label">
            Менеджер не указан
        </div>

        <div
            class="value"
            id="manager_unmarked"
        >—</div>

    </div>


    <div class="card good">

        <div class="label">
            Конверсия отмеченных
        </div>

        <div
            class="value"
            id="sale_conversion"
        >—</div>

    </div>

</div>


<h2 class="section-title">
    Качество обслуживания
</h2>


<div class="grid">

    <div class="card good">
        <div class="label">
            Средняя оценка клиентов
        </div>

        <div
            class="value"
            id="average_rating"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Получено оценок
        </div>

        <div
            class="value"
            id="ratings_count"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Отправлено приглашений
        </div>

        <div
            class="value"
            id="rating_invitations_sent"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Ответили на оценку
        </div>

        <div
            class="value"
            id="rating_response_rate"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Распределение 1–5
        </div>

        <div
            class="value"
            id="rating_distribution"
            style="font-size: 18px"
        >—</div>
    </div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2 id="chart_title">
            Звонки по часам
        </h2>

        <div class="chart-buttons">

            <button
                class="chart-btn active"
                data-chart="all"
            >
                Все
            </button>

            <button
                class="chart-btn"
                data-chart="incoming"
            >
                Входящие
            </button>

            <button
                class="chart-btn"
                data-chart="outgoing"
            >
                Исходящие
            </button>

        </div>

    </div>

    <div
        class="chart"
        id="timeline_chart"
    ></div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2>
            Почему не купили
        </h2>

    </div>

    <div class="table-wrap">

        <table>

            <thead>

            <tr>
                <th>
                    Причина
                </th>

                <th>
                    Количество
                </th>
            </tr>

            </thead>

            <tbody
                id="reasons_body"
            ></tbody>

        </table>

    </div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2>
            Менеджеры
        </h2>

    </div>

    <div class="table-wrap">

        <table>

            <thead>

            <tr>

                <th>Менеджер</th>
                <th>Звонки</th>
                <th>Клиенты</th>
                <th>Вход.</th>
                <th>Исход.</th>
                <th>Пропущ.</th>
                <th>Ответ %</th>
                <th>Купил</th>
                <th>Не купил</th>
                <th>Конверсия</th>
                <th>Ср. оценка</th>
                <th>Оценок</th>
                <th>Ответили</th>
                <th>Разговор</th>

            </tr>

            </thead>

            <tbody
                id="managers_body"
            ></tbody>

        </table>

    </div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2>
            Оценки продавцов по дням
        </h2>

    </div>

    <div class="table-wrap">

        <table>

            <thead>

            <tr>
                <th>Дата</th>
                <th>Менеджер</th>
                <th>Средняя оценка</th>
                <th>Оценок</th>
                <th>Приглашений</th>
                <th>Ответили</th>
            </tr>

            </thead>

            <tbody
                id="ratings_daily_body"
            ></tbody>

        </table>

    </div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2>
            SIM-карты
        </h2>

    </div>

    <div class="table-wrap">

        <table>

            <thead>

            <tr>

                <th>SIM</th>
                <th>Слот</th>
                <th>Звонки</th>
                <th>Клиенты</th>
                <th>Вход.</th>
                <th>Исход.</th>
                <th>Пропущ.</th>
                <th>Разговор</th>

            </tr>

            </thead>

            <tbody
                id="sims_body"
            ></tbody>

        </table>

    </div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2>
            Последние звонки
        </h2>

    </div>

    <div class="table-wrap">

        <table>

            <thead>

            <tr>

                <th>Время</th>
                <th>Клиент / контакт</th>
                <th>Направление</th>
                <th>Статус</th>
                <th>Менеджер</th>
                <th>SIM</th>
                <th>Разговор</th>
                <th>Результат</th>
                <th>Оценка</th>
                <th>Все данные</th>

            </tr>

            </thead>

            <tbody
                id="recent_body"
            ></tbody>

        </table>

    </div>

</div>


</div>


<div
    class="modal-overlay"
    id="rating_details_modal"
    role="dialog"
    aria-modal="true"
    aria-labelledby="rating_details_title"
>

    <div class="modal-card">

        <div class="modal-header">

            <h2
                class="modal-title"
                id="rating_details_title"
            >
                Все данные оценки
            </h2>

            <button
                class="modal-close"
                id="rating_details_close"
                type="button"
                aria-label="Закрыть"
            >×</button>

        </div>

        <div
            class="modal-content"
            id="rating_details_content"
        ></div>

    </div>

</div>


<script>

let selectedPeriod =
    "today";

let customFrom =
    "";

let customTo =
    "";

let chartMode =
    "all";

let timelineData =
    [];


function formatDuration(
    seconds
) {

    seconds = Number(
        seconds
        || 0
    );

    const hours =
        Math.floor(
            seconds
            / 3600
        );

    const minutes =
        Math.floor(
            (
                seconds
                % 3600
            )
            / 60
        );

    const secs =
        seconds
        % 60;


    if (
        hours > 0
    ) {

        if (
            minutes > 0
        ) {

            return (
                hours
                + " ч "
                + minutes
                + " мин"
            );
        }

        return (
            hours
            + " ч"
        );
    }


    if (
        minutes > 0
    ) {

        if (
            secs > 0
        ) {

            return (
                minutes
                + " мин "
                + secs
                + " сек"
            );
        }

        return (
            minutes
            + " мин"
        );
    }


    return (
        secs
        + " сек"
    );
}


function queryString() {

    const params =
        new URLSearchParams();


    params.set(
        "period",
        selectedPeriod
    );


    if (
        selectedPeriod
        === "custom"
    ) {

        params.set(
            "date_from",
            customFrom
        );

        params.set(
            "date_to",
            customTo
        );
    }


    return (
        "?"
        + params.toString()
    );
}


async function getJson(
    url
) {

    const response =
        await fetch(
            url
            + queryString()
        );


    if (
        !response.ok
    ) {

        throw new Error(
            await response.text()
        );
    }


    return (
        await response.json()
    );
}


async function loadStats() {

    const data =
        await getJson(
            "/stats"
        );

    const s =
        data.stats;


    document
        .getElementById(
            "period_label"
        )
        .textContent =
            data.period.label;


    const fields = {

        calls:
            s.calls,

        internal_calls:
            s.internal_calls,

        unique_clients:
            s.unique_clients,

        incoming:
            s.incoming,

        outgoing:
            s.outgoing,

        answered:
            s.answered,

        missed:
            s.missed,

        answer_rate:
            s.answer_rate
            + "%",

        new_clients:
            s.new_clients,

        repeat_clients:
            s.repeat_clients,

        unique_missed_clients:
            s.unique_missed_clients,

        missed_called_back:
            s.missed_called_back,

        missed_contacted:
            s.missed_contacted,

        missed_not_processed:
            s.missed_not_processed,

        bought:
            s.bought,

        not_bought:
            s.not_bought,

        sale_unmarked:
            s.sale_unmarked,

        manager_unmarked:
            s.manager_unmarked,

        sale_conversion:
            s.sale_conversion
            + "%",

        average_rating:
            s.average_rating === null
                ? "—"
                : Number(
                    s.average_rating
                ).toFixed(2)
                + " ★",

        ratings_count:
            s.ratings_count,

        rating_invitations_sent:
            s.rating_invitations_sent,

        rating_response_rate:
            s.rating_response_rate
            + "%",

        rating_distribution:
            [1, 2, 3, 4, 5]
                .map(
                    score => (
                        score
                        + "★: "
                        + (
                            s.rating_distribution[
                                String(score)
                            ]
                            || 0
                        )
                    )
                )
                .join(" · "),
    };


    for (
        const [
            id,
            value
        ]
        of Object.entries(
            fields
        )
    ) {

        document
            .getElementById(
                id
            )
            .textContent =
                value;
    }


    document
        .getElementById(
            "average_duration"
        )
        .textContent =
            formatDuration(
                s.average_duration_seconds
            );


    document
        .getElementById(
            "total_duration"
        )
        .textContent =
            formatDuration(
                s.total_duration_seconds
            );


    document
        .getElementById(
            "average_answer_delay"
        )
        .textContent =
            formatDuration(
                s.average_answer_delay_seconds
            );
}


async function loadTimeline() {

    const data =
        await getJson(
            "/stats/timeline"
        );


    timelineData =
        data.points;


    document
        .getElementById(
            "chart_title"
        )
        .textContent =

            data.granularity
            === "hour"

                ? "Звонки по часам"

                : "Звонки по дням";


    renderTimeline();
}


function renderTimeline() {

    const chart =
        document.getElementById(
            "timeline_chart"
        );


    chart.innerHTML =
        "";


    const values =
        timelineData.map(
            item => {

                if (
                    chartMode
                    === "incoming"
                ) {

                    return (
                        item.incoming
                    );
                }


                if (
                    chartMode
                    === "outgoing"
                ) {

                    return (
                        item.outgoing
                    );
                }


                return (
                    item.calls
                );
            }
        );


    const maxValue =
        Math.max(
            ...values,
            1
        );


    timelineData.forEach(
        (
            item,
            index
        ) => {

            const value =
                values[index];


            const wrap =
                document.createElement(
                    "div"
                );


            wrap.className =
                "bar-wrap";


            const valueText =
                document.createElement(
                    "div"
                );


            valueText.className =
                "bar-value";


            valueText.textContent =
                value > 0
                    ? value
                    : "";


            const bar =
                document.createElement(
                    "div"
                );


            bar.className =
                "bar";


            if (
                chartMode
                === "outgoing"
            ) {

                bar.classList.add(
                    "outgoing"
                );
            }


            bar.style.height =

                Math.max(
                    2,

                    Math.round(
                        (
                            value
                            / maxValue
                        )
                        * 210
                    )
                )

                + "px";


            const label =
                document.createElement(
                    "div"
                );


            label.className =
                "bar-label";


            label.textContent =
                item.label;


            wrap.appendChild(
                valueText
            );


            wrap.appendChild(
                bar
            );


            wrap.appendChild(
                label
            );


            chart.appendChild(
                wrap
            );
        }
    );
}


async function loadReasons() {

    const data =
        await getJson(
            "/stats/sales/reasons"
        );


    const body =
        document.getElementById(
            "reasons_body"
        );


    body.innerHTML =
        "";


    if (
        data.results.length
        === 0
    ) {

        const tr =
            document.createElement(
                "tr"
            );


        const td =
            document.createElement(
                "td"
            );


        td.colSpan =
            2;


        td.className =
            "empty";


        td.textContent =
            "Пока нет данных";


        tr.appendChild(
            td
        );


        body.appendChild(
            tr
        );


        return;
    }


    data.results.forEach(
        row => {

            const tr =
                document.createElement(
                    "tr"
                );


            const reason =
                document.createElement(
                    "td"
                );


            reason.textContent =
                row.reason;


            const count =
                document.createElement(
                    "td"
                );


            count.textContent =
                row.count;


            tr.appendChild(
                reason
            );


            tr.appendChild(
                count
            );


            body.appendChild(
                tr
            );
        }
    );
}


async function loadManagers() {

    const data =
        await getJson(
            "/stats/managers"
        );


    const body =
        document.getElementById(
            "managers_body"
        );


    body.innerHTML =
        "";


    data.results.forEach(
        row => {

            const tr =
                document.createElement(
                    "tr"
                );


            const values = [

                row.manager,

                row.calls,

                row.unique_clients,

                row.incoming,

                row.outgoing,

                row.missed,

                row.answer_rate
                + "%",

                row.bought,

                row.not_bought,

                row.sale_conversion
                + "%",

                row.average_rating === null
                    ? "—"
                    : Number(
                        row.average_rating
                    ).toFixed(2)
                    + " ★",

                row.ratings_count,

                row.rating_response_rate
                + "%",

                formatDuration(
                    row.total_duration_seconds
                ),
            ];


            values.forEach(
                value => {

                    const td =
                        document.createElement(
                            "td"
                        );


                    td.textContent =
                        value;


                    tr.appendChild(
                        td
                    );
                }
            );


            body.appendChild(
                tr
            );
        }
    );
}


async function loadRatingsDaily() {

    const data =
        await getJson(
            "/stats/ratings/daily"
        );


    const body =
        document.getElementById(
            "ratings_daily_body"
        );


    body.innerHTML =
        "";


    if (
        data.results.length
        === 0
    ) {

        const tr =
            document.createElement(
                "tr"
            );

        const td =
            document.createElement(
                "td"
            );

        td.colSpan = 6;
        td.className = "empty";
        td.textContent =
            "Пока нет отправленных приглашений";

        tr.appendChild(
            td
        );

        body.appendChild(
            tr
        );

        return;
    }


    data.results.forEach(
        row => {

            const tr =
                document.createElement(
                    "tr"
                );

            const values = [
                row.date,
                row.manager,
                row.average_rating === null
                    ? "—"
                    : Number(
                        row.average_rating
                    ).toFixed(2)
                    + " ★",
                row.ratings_count,
                row.invitations_sent,
                row.response_rate + "%",
            ];

            values.forEach(
                value => {

                    const td =
                        document.createElement(
                            "td"
                        );

                    td.textContent =
                        value;

                    tr.appendChild(
                        td
                    );
                }
            );

            body.appendChild(
                tr
            );
        }
    );
}


async function loadSims() {

    const data =
        await getJson(
            "/stats/sims"
        );


    const body =
        document.getElementById(
            "sims_body"
        );


    body.innerHTML =
        "";


    data.results.forEach(
        row => {

            const tr =
                document.createElement(
                    "tr"
                );


            const values = [

                row.sim,

                row.slot === null
                    ? "—"
                    : row.slot + 1,

                row.calls,

                row.unique_clients,

                row.incoming,

                row.outgoing,

                row.missed,

                formatDuration(
                    row.total_duration_seconds
                ),
            ];


            values.forEach(
                value => {

                    const td =
                        document.createElement(
                            "td"
                        );


                    td.textContent =
                        value;


                    tr.appendChild(
                        td
                    );
                }
            );


            body.appendChild(
                tr
            );
        }
    );
}


function closeRatingDetails() {

    document
        .getElementById(
            "rating_details_modal"
        )
        .classList.remove(
            "visible"
        );

    document.body.style.overflow =
        "";
}


function appendDetailsValue(
    container,
    value
) {

    if (
        value !== null
        && typeof value === "object"
    ) {

        const pre =
            document.createElement(
                "pre"
            );

        pre.textContent =
            Object.keys(value).length
                ? JSON.stringify(
                    value,
                    null,
                    2
                )
                : "—";

        container.appendChild(
            pre
        );

        return;
    }

    const text =
        value === null
        || value === undefined
        || value === ""
            ? "—"
            : String(value);

    if (
        text.startsWith("https://")
        || text.startsWith("http://")
    ) {

        const link =
            document.createElement(
                "a"
            );

        link.href = text;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = text;

        container.appendChild(
            link
        );

        return;
    }

    container.textContent = text;
}


async function openRatingDetails(
    callId
) {

    const modal =
        document.getElementById(
            "rating_details_modal"
        );

    const content =
        document.getElementById(
            "rating_details_content"
        );

    content.textContent =
        "Загрузка…";

    modal.classList.add(
        "visible"
    );

    document.body.style.overflow =
        "hidden";

    try {

        const response =
            await fetch(
                "/stats/rating/details/"
                + encodeURIComponent(callId),
                {
                    cache: "no-store",
                }
            );

        if (!response.ok) {
            throw new Error(
                "HTTP "
                + response.status
            );
        }

        const data =
            await response.json();

        content.textContent = "";

        data.sections.forEach(
            section => {

                const sectionElement =
                    document.createElement(
                        "section"
                    );

                sectionElement.className =
                    "details-section";

                const heading =
                    document.createElement(
                        "h3"
                    );

                heading.textContent =
                    section.title;

                const grid =
                    document.createElement(
                        "div"
                    );

                grid.className =
                    "details-grid";

                Object.entries(
                    section.items
                ).forEach(
                    ([label, value]) => {

                        const labelElement =
                            document.createElement(
                                "div"
                            );

                        labelElement.className =
                            "details-label";

                        labelElement.textContent =
                            label;

                        const valueElement =
                            document.createElement(
                                "div"
                            );

                        valueElement.className =
                            "details-value";

                        appendDetailsValue(
                            valueElement,
                            value
                        );

                        grid.appendChild(
                            labelElement
                        );

                        grid.appendChild(
                            valueElement
                        );
                    }
                );

                sectionElement.appendChild(
                    heading
                );

                sectionElement.appendChild(
                    grid
                );

                content.appendChild(
                    sectionElement
                );
            }
        );

    } catch (error) {

        content.textContent =
            "Не удалось загрузить данные оценки.";
    }
}


document
    .getElementById(
        "rating_details_close"
    )
    .addEventListener(
        "click",
        closeRatingDetails
    );


document
    .getElementById(
        "rating_details_modal"
    )
    .addEventListener(
        "click",
        event => {
            if (
                event.target.id
                === "rating_details_modal"
            ) {
                closeRatingDetails();
            }
        }
    );


document.addEventListener(
    "keydown",
    event => {
        if (event.key === "Escape") {
            closeRatingDetails();
        }
    }
);


async function loadRecent() {

    const data =
        await getJson(
            "/stats/recent"
        );


    const body =
        document.getElementById(
            "recent_body"
        );


    body.innerHTML =
        "";


    data.results.forEach(
        row => {

            const tr =
                document.createElement(
                    "tr"
                );


            const time =
                document.createElement(
                    "td"
                );


            time.textContent =
                row.local_time;


            const client =
                document.createElement(
                    "td"
                );


            client.className =
                "phone";


            if (
                row.is_internal_contact
            ) {

                client.classList.add(
                    "contact"
                );


                client.textContent =
                    row.internal_contact_name
                    + " · "
                    + row.client_number;

            } else {

                client.textContent =

                    row.client_name

                        ? (
                            row.client_name
                            + " · "
                            + row.client_number
                        )

                        : row.client_number;
            }


            const direction =
                document.createElement(
                    "td"
                );


            direction.textContent =

                row.direction
                === 0

                    ? "Входящий"

                    : "Исходящий";


            const status =
                document.createElement(
                    "td"
                );


            status.textContent =

                row.answered

                    ? "Отвечен"

                    : "Не отвечен";


            const manager =
                document.createElement(
                    "td"
                );


            manager.textContent =
                row.manager;


            const customerRating =
                document.createElement(
                    "td"
                );


            if (
                row.customer_rating
                !== null
            ) {
                customerRating.textContent =
                    row.customer_rating
                    + " ★";

            } else if (
                row.rating_sms_status
                === "sent"
            ) {
                customerRating.textContent =
                    "Ожидаем";

            } else if (
                row.rating_sms_status
                === "error"
            ) {
                customerRating.textContent =
                    "SMS ошибка";

            } else {
                customerRating.textContent =
                    "—";
            }


            const sim =
                document.createElement(
                    "td"
                );


            sim.className =
                "phone";


            sim.textContent =
                row.sim;


            const duration =
                document.createElement(
                    "td"
                );


            duration.textContent =
                formatDuration(
                    row.duration
                );


            const sale =
                document.createElement(
                    "td"
                );


            if (
                row.is_internal_contact
            ) {

                sale.className =
                    "contact";


                sale.textContent =
                    "Контакт";

            } else if (
                row.sale_status
                === "bought"
            ) {

                sale.className =
                    "sale-bought";


                sale.textContent =
                    "✅ Купил";

            } else if (
                row.sale_status
                === "not_bought"
            ) {

                sale.className =
                    "sale-not-bought";


                sale.textContent =
                    row.no_sale_reason
                    || "Не купил";

            } else if (
                row.answered
            ) {

                sale.className =
                    "sale-unmarked";


                sale.textContent =
                    "Не отмечено";

            } else {

                sale.textContent =
                    "—";
            }


            const details =
                document.createElement(
                    "td"
                );


            if (
                row.customer_rating
                !== null
            ) {

                const detailsButton =
                    document.createElement(
                        "button"
                    );

                detailsButton.type =
                    "button";

                detailsButton.className =
                    "details-button";

                detailsButton.textContent =
                    "Все данные";

                detailsButton.addEventListener(
                    "click",
                    () => openRatingDetails(
                        row.id
                    )
                );

                details.appendChild(
                    detailsButton
                );

            } else {

                details.textContent =
                    "—";
            }


            tr.appendChild(
                time
            );


            tr.appendChild(
                client
            );


            tr.appendChild(
                direction
            );


            tr.appendChild(
                status
            );


            tr.appendChild(
                manager
            );


            tr.appendChild(
                sim
            );


            tr.appendChild(
                duration
            );


            tr.appendChild(
                sale
            );


            tr.appendChild(
                customerRating
            );


            tr.appendChild(
                details
            );


            body.appendChild(
                tr
            );
        }
    );
}


async function loadAll() {

    try {

        await Promise.all(
            [

                loadStats(),

                loadTimeline(),

                loadReasons(),

                loadManagers(),

                loadRatingsDaily(),

                loadSims(),

                loadRecent(),

            ]
        );

    } catch (
        error
    ) {

        console.error(
            error
        );
    }
}


function selectPeriod(
    period
) {

    selectedPeriod =
        period;


    document
        .querySelectorAll(
            ".period-btn"
        )
        .forEach(
            button => {

                button
                    .classList
                    .toggle(

                        "active",

                        button
                            .dataset
                            .period

                        === period
                    );
            }
        );


    document
        .getElementById(
            "custom_panel"
        )
        .classList
        .toggle(

            "visible",

            period
            === "custom"
        );


    if (
        period
        !== "custom"
    ) {

        loadAll();
    }
}


document
    .querySelectorAll(
        ".period-btn"
    )
    .forEach(
        button => {

            button.addEventListener(

                "click",

                () => {

                    selectPeriod(
                        button
                            .dataset
                            .period
                    );
                }
            );
        }
    );


document
    .querySelectorAll(
        ".chart-btn"
    )
    .forEach(
        button => {

            button.addEventListener(

                "click",

                () => {

                    chartMode =
                        button
                            .dataset
                            .chart;


                    document
                        .querySelectorAll(
                            ".chart-btn"
                        )
                        .forEach(
                            item => {

                                item
                                    .classList
                                    .toggle(

                                        "active",

                                        item
                                            .dataset
                                            .chart

                                        === chartMode
                                    );
                            }
                        );


                    renderTimeline();
                }
            );
        }
    );


document
    .getElementById(
        "apply_custom"
    )
    .addEventListener(

        "click",

        () => {

            customFrom =
                document
                    .getElementById(
                        "date_from"
                    )
                    .value;


            customTo =
                document
                    .getElementById(
                        "date_to"
                    )
                    .value;


            if (
                !customFrom
                ||
                !customTo
            ) {

                alert(
                    "Выберите обе даты"
                );


                return;
            }


            loadAll();
        }
    );


loadAll();


setInterval(
    loadAll,
    30000
);

</script>

</body>
</html>
    """


# =========================================================
# MOIZVONKI WEBHOOK
# =========================================================

@app.post(
    "/webhooks/moizvonki"
)
async def moizvonki_webhook(
    request: Request,
):

    data = await request.json()

    print(
        "MOIZVONKI:",
        data,
    )

    webhook = (
        data.get(
            "webhook"
        )
        or {}
    )

    event = (
        data.get(
            "event"
        )
        or {}
    )

    if (
        webhook.get(
            "action"
        )
        != "call.finish"
    ):

        return {
            "ok": True
        }

    answered = int(
        event.get(
            "answered",
            0,
        )
        or 0
    )

    recording = event.get(
        "recording"
    )

    # -----------------------------------------------------
    # RECORDING
    # -----------------------------------------------------

    audio_duration = None
    voice_bytes = None

    if (
        answered
        and recording
    ):

        (
            audio_duration,
            voice_bytes,
        ) = prepare_recording(
            recording
        )

    # -----------------------------------------------------
    # DURATION
    # -----------------------------------------------------

    (
        talk_duration,
        duration_source,
    ) = get_talk_duration(
        event,
        audio_duration,
    )

    # -----------------------------------------------------
    # SAVE
    # -----------------------------------------------------

    save_result = save_call(
        webhook,
        event,
        talk_duration,
        duration_source,
    )

    call_id = (
        save_result[
            "call_id"
        ]
    )

    telegram_already_sent = (
        save_result[
            "already_sent"
        ]
    )

    is_internal_contact = (
        save_result[
            "is_internal_contact"
        ]
    )

    client_number = (
        event.get(
            "client_number"
        )
        or ""
    )

    sender_user_login = (
        webhook.get(
            "user_login"
        )
        or MOIZVONKI_USER_NAME
    )

    # -----------------------------------------------------
    # AUTO SMS
    # -----------------------------------------------------

    sms_status = "not_sent"

    if is_internal_contact:
        sms_status = "internal_contact"

    else:

        reservation = reserve_client_sms(
            call_id,
            client_number,
            sender_user_login,
        )

        if reservation["reserved"]:

            history_id = reservation[
                "history_id"
            ]

            try:

                sms_result = send_client_sms(
                    client_number,
                    sender_user_login,
                )

                mark_sms_sent(
                    call_id,
                    history_id,
                    sms_result,
                )

                sms_status = "sent"

                print(
                    "AUTO SMS SENT:",
                    call_id,
                    client_number,
                    sender_user_login,
                )

            except Exception as exc:

                mark_sms_error(
                    call_id,
                    history_id,
                    repr(exc),
                )

                sms_status = "error"

                print(
                    "AUTO SMS ERROR:",
                    call_id,
                    client_number,
                    repr(exc),
                )

        else:
            sms_status = reservation[
                "reason"
            ]

    # -----------------------------------------------------
    # CUSTOMER RATING SMS
    # -----------------------------------------------------

    rating_sms_status = "not_applicable"

    if (
        answered

        and

        not is_internal_contact

        and

        normalize_phone(
            client_number
        )
    ):

        rating_reservation = (
            reserve_call_rating(
                call_id,
                client_number,
                sender_user_login,
            )
        )

        if rating_reservation[
            "reserved"
        ]:

            rating_id = rating_reservation[
                "rating_id"
            ]

            try:

                rating_url = build_rating_url(
                    rating_reservation[
                        "token"
                    ]
                )

                rating_text = (
                    RATING_SMS_TEXT.format(
                        rating_url=rating_url
                    )
                )

                rating_sms_result = (
                    send_client_sms(
                        client_number,
                        sender_user_login,
                        rating_text,
                    )
                )

                mark_rating_sms_sent(
                    rating_id,
                    rating_sms_result,
                )

                rating_sms_status = "sent"

                print(
                    "RATING SMS SENT:",
                    call_id,
                    client_number,
                    sender_user_login,
                )

            except Exception as exc:

                mark_rating_sms_error(
                    rating_id,
                    repr(exc),
                )

                rating_sms_status = "error"

                print(
                    "RATING SMS ERROR:",
                    call_id,
                    client_number,
                    repr(exc),
                )

        else:
            rating_sms_status = (
                rating_reservation.get(
                    "sms_status"
                )
                or rating_reservation[
                    "reason"
                ]
            )

    # -----------------------------------------------------
    # TELEGRAM
    # -----------------------------------------------------

    telegram_status = (
        "already_sent"
        if telegram_already_sent
        else "not_sent"
    )

    if not telegram_already_sent:

        text = build_telegram_message(
            event,
            webhook,
            talk_duration,
        )

        result_keyboard = None

        if (
            answered

            and

            not is_internal_contact
        ):
            result_keyboard = (
                build_manager_keyboard(
                    call_id
                )
            )

        try:

            if (
                answered
                and voice_bytes
            ):
                telegram_result = (
                    send_voice_bytes(
                        voice_bytes,
                        text,
                        reply_markup=
                            result_keyboard,
                    )
                )

            else:
                telegram_result = (
                    send_text_message(
                        text,
                        reply_markup=
                            result_keyboard,
                    )
                )

            mark_telegram_sent(
                call_id,
                telegram_result,
            )

            telegram_status = "sent"

        except Exception as exc:

            telegram_status = "error"

            print(
                "TELEGRAM ERROR:",
                repr(exc),
            )

    duplicate = (
        telegram_already_sent
        and sms_status in {
            "cooldown",
            "internal_contact",
            "empty_number",
        }
    )

    if duplicate:
        print(
            "DUPLICATE CALL:",
            event.get(
                "db_call_id"
            ),
        )

    return {
        "ok":
            True,

        "call_id":
            call_id,

        "duplicate":
            duplicate,

        "internal_contact":
            is_internal_contact,

        "duration":
            talk_duration,

        "duration_source":
            duration_source,

        "telegram":
            telegram_status,

        "sms":
            sms_status,

        "rating_sms":
            rating_sms_status,
    }
