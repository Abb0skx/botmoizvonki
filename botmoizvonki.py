import json
import os
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

        conn.commit()


init_db()


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
                    AS internal_calls

            FROM calls

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

                id,
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
                no_sale_reason

            FROM calls

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
            }
        )

    return {
        "results":
            results
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

            </tr>

            </thead>

            <tbody
                id="recent_body"
            ></tbody>

        </table>

    </div>

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

    already_sent = (
        save_result[
            "already_sent"
        ]
    )

    is_internal_contact = (
        save_result[
            "is_internal_contact"
        ]
    )

    # -----------------------------------------------------
    # DUPLICATE
    # -----------------------------------------------------

    if already_sent:

        print(
            "DUPLICATE CALL:",
            event.get(
                "db_call_id"
            ),
        )

        return {
            "ok": True,
            "duplicate": True,
        }

    # -----------------------------------------------------
    # MESSAGE
    # -----------------------------------------------------

    text = build_telegram_message(
        event,
        webhook,
        talk_duration,
    )

    # -----------------------------------------------------
    # BUTTONS
    #
    # Только:
    # отвеченный звонок
    # НЕ внутренний контакт
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # TELEGRAM
    # -----------------------------------------------------

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

    except Exception as exc:

        print(
            "TELEGRAM ERROR:",
            repr(
                exc
            ),
        )

        raise

    return {
        "ok":
            True,

        "call_id":
            call_id,

        "internal_contact":
            is_internal_contact,

        "duration":
            talk_duration,

        "duration_source":
            duration_source,
    }