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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse


# =========================================================
# APP
# =========================================================

app = FastAPI()


# =========================================================
# CONFIG
# =========================================================

UZ_TZ = timezone(
    timedelta(hours=5)
)

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "data" / ".env"

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

# Необязательно.
# Если потом настроим Telegram secret_token,
# можно указать его в ENV.
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
    bool(TELEGRAM_BOT_TOKEN),
)

print(
    "CHAT ID EXISTS:",
    bool(TELEGRAM_CHAT_ID),
)

print(
    "DATABASE:",
    DB_PATH,
)


# =========================================================
# SALE REASONS
# =========================================================

SALE_REASONS = {
    "no_stock":
        "📦 Нет товара",

    "price":
        "💰 Не устроила цена",

    "price_changed":
        "💰 Цена Изменилось",

    "thinking":
        "🤔 Думает / сравнивает",

    "other_product":
        "🔎 Ищет другой товар",

    "credit":
        "🔎 Хочет На Кредит",

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
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                db_call_id INTEGER UNIQUE,
                event_pbx_call_id TEXT,

                client_number TEXT,
                client_key TEXT,
                client_name TEXT,

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

                telegram_sent INTEGER DEFAULT 0,
                telegram_chat_id TEXT,
                telegram_message_id INTEGER,

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
                "PRAGMA table_info(calls)"
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
                ADD COLUMN telegram_sent INTEGER DEFAULT 0
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
        }

        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(
                    sql
                )

        # ---------------------------------------------
        # Нормализация старых номеров
        # ---------------------------------------------

        old_rows = conn.execute(
            """
            SELECT
                id,
                client_number

            FROM calls

            WHERE
                client_key IS NULL
                OR client_key = ''
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

        # ---------------------------------------------
        # Старые записи Telegram
        # ---------------------------------------------

        conn.execute(
            """
            UPDATE calls

            SET telegram_sent = 1

            WHERE telegram_sent IS NULL
            """
        )

        # ---------------------------------------------
        # INDEXES
        # ---------------------------------------------

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
            idx_calls_no_sale_reason_code
            ON calls(no_sale_reason_code)
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
        return None, None

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

        return None, None

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

        # ---------------------------------------------
        # REAL AUDIO DURATION
        # ---------------------------------------------

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

                    str(source_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

            value = (
                result.stdout
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

        # ---------------------------------------------
        # CONVERT TO OGG OPUS
        # ---------------------------------------------

        voice_bytes = None

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",

                    "-i",
                    str(source_path),

                    "-vn",

                    "-c:a",
                    "libopus",

                    "-b:a",
                    "32k",

                    "-vbr",
                    "on",

                    "-application",
                    "voip",

                    str(voice_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

            voice_bytes = (
                voice_path.read_bytes()
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
        return 0, "none"

    # ---------------------------------------------
    # 1. AUDIO
    # ---------------------------------------------

    if (
        audio_duration is not None
        and audio_duration >= 0
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

    # ---------------------------------------------
    # 2. TIMESTAMPS
    # ---------------------------------------------

    if (
        answer_time > 0
        and end_time > answer_time
    ):
        return (
            end_time - answer_time,
            "timestamps",
        )

    # ---------------------------------------------
    # 3. API
    # ---------------------------------------------

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
                    telegram_sent

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
                    telegram_sent

                FROM calls

                WHERE event_pbx_call_id = ?

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
                    telegram_sent

                FROM calls

                ORDER BY id DESC

                LIMIT 1
                """
            ).fetchone()

    return {
        "call_id":
            row["id"]
            if row
            else cursor.lastrowid,

        "already_sent":
            bool(
                row
                and row["telegram_sent"]
            ),
    }


def mark_telegram_sent(
    call_id: int,
    telegram_result: dict | None = None,
):
    if not call_id:
        return

    message_id = None
    chat_id = None

    if telegram_result:
        result = telegram_result.get(
            "result",
            {},
        )

        message_id = result.get(
            "message_id"
        )

        chat = result.get(
            "chat",
            {},
        )

        chat_id = chat.get(
            "id"
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
                str(chat_id)
                if chat_id is not None
                else None,

                message_id,

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
            "first_contact": None,
            "last_contact": None,
        }

    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS calls_count,

                MIN(start_time)
                    AS first_contact,

                MAX(start_time)
                    AS last_contact

            FROM calls

            WHERE client_key = ?
            """,
            (
                client_key,
            ),
        ).fetchone()

    return {
        "calls_count":
            row["calls_count"]
            or 0,

        "first_contact":
            row["first_contact"],

        "last_contact":
            row["last_contact"],
    }


# =========================================================
# FORMAT
# =========================================================

def format_duration(
    seconds: int,
):
    seconds = int(
        seconds or 0
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

    return datetime.fromtimestamp(
        int(timestamp),
        UZ_TZ,
    ).strftime(
        "%H:%M"
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

    client_number = event.get(
        "client_number",
        "",
    )

    client_name = event.get(
        "client_name",
        "",
    )

    manager = webhook.get(
        "user_login",
        "",
    )

    sim = event.get(
        "src_number",
        "",
    )

    start_time = event.get(
        "start_time"
    )

    answer_time = event.get(
        "answer_time"
    )

    history = get_client_history(
        client_number
    )

    contacts = history[
        "calls_count"
    ]

    # ---------------------------------------------
    # TITLE
    # ---------------------------------------------

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

    # ---------------------------------------------
    # CLIENT
    # ---------------------------------------------

    if client_name:
        lines.append(
            "👤 "
            f"<b>{escape(str(client_name))}</b>"
        )

    lines.append(
        "📞 "
        f"{escape(str(client_number or '—'))}"
    )

    # ---------------------------------------------
    # HISTORY
    # ---------------------------------------------

    if contacts <= 1:
        lines.append(
            "🆕 Новый клиент"
        )

    else:
        lines.append(
            "🔁 Контактов с номером: "
            f"<b>{contacts}</b>"
        )

    lines.append("")

    # ---------------------------------------------
    # TIME
    # ---------------------------------------------

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

    # ---------------------------------------------
    # MANAGER
    # ---------------------------------------------

    lines.append(
        "👨‍💼 Менеджер: "
        f"{escape(str(manager or '—'))}"
    )

    lines.append(
        "📲 SIM: "
        f"{escape(str(sim or '—'))}"
    )

    if answered:
        lines.extend(
            [
                "",
                "<b>Результат разговора:</b>",
            ]
        )

    return "\n".join(
        lines
    )


# =========================================================
# TELEGRAM KEYBOARDS
# =========================================================

def build_sale_keyboard(
    call_id: int,
):
    return {
        "inline_keyboard": [
            [
                {
                    "text":
                        "✅ Купил",

                    "callback_data":
                        f"sale:bought:{call_id}",
                },
                {
                    "text":
                        "❌ Не купил",

                    "callback_data":
                        f"sale:not_bought:{call_id}",
                },
            ]
        ]
    }


def build_reason_keyboard(
    call_id: int,
):
    return {
        "inline_keyboard": [
            [
                {
                    "text":
                        "📦 Нет товара",

                    "callback_data":
                        f"reason:no_stock:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "💰 Не устроила цена",

                    "callback_data":
                        f"reason:price:{call_id}",
                },

                {
                    "text":
                        "💰 Цена Изменилось",

                    "callback_data":
                        f"reason:price_changed:{call_id}",
                },
            ],

            [
                {
                    "text":
                        "🤔 Думает / сравнивает",

                    "callback_data":
                        f"reason:thinking:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "🔎 Ищет другой товар",

                    "callback_data":
                        f"reason:other_product:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "🔎 Хочет На Кредит",

                    "callback_data":
                        f"reason:credit:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "🏪 Хочет прийти в магазин",

                    "callback_data":
                        f"reason:visit_store:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "⏳ Купит позже",

                    "callback_data":
                        f"reason:later:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "🏪 Купил в другом месте",

                    "callback_data":
                        f"reason:bought_elsewhere:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "🚚 Не подошли условия",

                    "callback_data":
                        f"reason:conditions:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "🚫 Не целевой звонок",

                    "callback_data":
                        f"reason:not_target:{call_id}",
                }
            ],

            [
                {
                    "text":
                        "📝 Другая причина",

                    "callback_data":
                        f"reason:other:{call_id}",
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
            result
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
    text: str | None = None,
):
    data = {
        "callback_query_id":
            callback_query_id,
    }

    if text:
        data["text"] = text

    try:
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
    reply_markup: dict | None,
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
                    reply_markup
                    if reply_markup
                    else {
                        "inline_keyboard": []
                    },
                    ensure_ascii=False,
                ),
        },
        timeout=30,
    )


def edit_message_result(
    telegram_message: dict,
    result_lines: list[str],
):
    chat = telegram_message.get(
        "chat",
        {},
    )

    chat_id = chat.get(
        "id"
    )

    message_id = telegram_message.get(
        "message_id"
    )

    if not chat_id or not message_id:
        return

    original_text = (
        telegram_message.get(
            "caption"
        )
        or telegram_message.get(
            "text"
        )
        or ""
    )

    # Убираем служебную строку перед добавлением результата
    marker = (
        "\n\n<b>Результат разговора:</b>"
    )

    if marker in original_text:
        original_text = (
            original_text
            .split(
                marker,
                1,
            )[0]
        )

    final_text = (
        original_text
        + "\n\n"
        + "\n".join(
            result_lines
        )
    )

    # Voice / audio / photo обычно используют caption
    if telegram_message.get(
        "voice"
    ):
        telegram_api(
            "editMessageCaption",
            data={
                "chat_id":
                    chat_id,

                "message_id":
                    message_id,

                "caption":
                    final_text,

                "parse_mode":
                    "HTML",

                "reply_markup":
                    json.dumps(
                        {
                            "inline_keyboard": []
                        }
                    ),
            },
            timeout=30,
        )

    else:
        telegram_api(
            "editMessageText",
            data={
                "chat_id":
                    chat_id,

                "message_id":
                    message_id,

                "text":
                    final_text,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True,

                "reply_markup":
                    json.dumps(
                        {
                            "inline_keyboard": []
                        }
                    ),
            },
            timeout=30,
        )


# =========================================================
# SALE DATABASE
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


def mark_sale_bought(
    call_id: int,
    telegram_user: dict,
):
    now_ts = int(
        datetime.now(
            UZ_TZ
        ).timestamp()
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

                telegram_user.get(
                    "username"
                )
                or telegram_user.get(
                    "first_name"
                )
                or "",

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
            "Unknown reason"
        )

    now_ts = int(
        datetime.now(
            UZ_TZ
        ).timestamp()
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

                telegram_user.get(
                    "username"
                )
                or telegram_user.get(
                    "first_name"
                )
                or "",

                call_id,
            ),
        )

        conn.commit()


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.post(
    "/telegram/webhook"
)
async def telegram_webhook(
    request: Request,
):
    # ---------------------------------------------
    # Optional Telegram secret_token protection
    # ---------------------------------------------

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
                detail="Invalid Telegram secret",
            )

    data = await request.json()

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

    callback_data = callback.get(
        "data",
        "",
    )

    telegram_user = callback.get(
        "from",
        {},
    )

    telegram_message = callback.get(
        "message",
        {},
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

    message_id = telegram_message.get(
        "message_id"
    )

    print(
        "TELEGRAM CALLBACK:",
        callback_data,
    )

    try:

        # =========================================
        # ✅ КУПИЛ
        # =========================================

        if callback_data.startswith(
            "sale:bought:"
        ):
            call_id = int(
                callback_data
                .split(":")[-1]
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

            mark_sale_bought(
                call_id,
                telegram_user,
            )

            answer_callback_query(
                callback_id,
                "✅ Сохранено: Купил",
            )

            edit_message_result(
                telegram_message,
                [
                    "✅ <b>Купил</b>"
                ],
            )

            return {
                "ok": True,
                "sale_status":
                    "bought",
            }

        # =========================================
        # ❌ НЕ КУПИЛ
        # =========================================

        if callback_data.startswith(
            "sale:not_bought:"
        ):
            call_id = int(
                callback_data
                .split(":")[-1]
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

            answer_callback_query(
                callback_id,
                "Выберите причину",
            )

            edit_reply_markup(
                chat_id,
                message_id,
                build_reason_keyboard(
                    call_id
                ),
            )

            return {
                "ok": True,
                "waiting_reason":
                    True,
            }

        # =========================================
        # ПРИЧИНА НЕ ПОКУПКИ
        # =========================================

        if callback_data.startswith(
            "reason:"
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

            reason_code = parts[1]

            call_id = int(
                parts[2]
            )

            reason = SALE_REASONS.get(
                reason_code
            )

            if not reason:
                answer_callback_query(
                    callback_id,
                    "Причина не найдена",
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

            mark_sale_not_bought(
                call_id,
                reason_code,
                telegram_user,
            )

            answer_callback_query(
                callback_id,
                "❌ Причина сохранена",
            )

            edit_message_result(
                telegram_message,
                [
                    "❌ <b>Не купил</b>",
                    f"Причина: {escape(reason)}",
                ],
            )

            return {
                "ok": True,

                "sale_status":
                    "not_bought",

                "reason":
                    reason,
            }

        answer_callback_query(
            callback_id,
            "Неизвестная команда",
        )

    except Exception as exc:
        print(
            "TELEGRAM CALLBACK ERROR:",
            exc,
        )

        answer_callback_query(
            callback_id,
            "Ошибка сохранения",
        )

        raise

    return {
        "ok": True
    }


# =========================================================
# PERIOD
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
    today = datetime.now(
        UZ_TZ
    ).date()

    if period == "today":
        start_date = today
        end_date = today
        label = "Сегодня"

    elif period == "yesterday":
        start_date = (
            today
            - timedelta(days=1)
        )

        end_date = start_date
        label = "Вчера"

    elif period == "7d":
        start_date = (
            today
            - timedelta(days=6)
        )

        end_date = today
        label = "Последние 7 дней"

    elif period == "30d":
        start_date = (
            today
            - timedelta(days=29)
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
                    "Конечная дата меньше "
                    "начальной"
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
            detail="Неизвестный период",
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
        + timedelta(days=1)
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
            ).days + 1,
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

    start_ts = p["start_ts"]
    end_ts = p["end_ts"]

    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS calls,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN client_key != ''
                        THEN client_key
                    END
                ) AS unique_clients,

                SUM(
                    CASE
                        WHEN direction = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS incoming,

                SUM(
                    CASE
                        WHEN direction = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS outgoing,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS answered,

                SUM(
                    CASE
                        WHEN direction = 0
                             AND answered = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS missed,

                SUM(
                    CASE
                        WHEN direction = 1
                             AND answered = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS unanswered_outgoing,

                SUM(
                    CASE
                        WHEN direction = 0
                             AND answered = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS incoming_answered,

                AVG(
                    CASE
                        WHEN answered = 1
                        THEN duration
                    END
                ) AS avg_duration,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN duration
                        ELSE 0
                    END
                ) AS total_duration,

                AVG(
                    CASE
                        WHEN direction = 0
                             AND answered = 1
                             AND answer_time > start_time
                        THEN
                            answer_time - start_time
                    END
                ) AS avg_answer_delay

            FROM calls

            WHERE
                start_time >= ?
                AND start_time < ?
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        # -----------------------------------------
        # NEW / REPEAT
        # -----------------------------------------

        clients_row = conn.execute(
            """
            WITH period_clients AS (
                SELECT DISTINCT
                    client_key

                FROM calls

                WHERE
                    start_time >= ?
                    AND start_time < ?

                    AND client_key IS NOT NULL
                    AND client_key != ''
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

                                AND old_call.start_time < ?
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS new_clients,

                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1

                            FROM calls AS old_call

                            WHERE
                                old_call.client_key =
                                period_clients.client_key

                                AND old_call.start_time < ?
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS repeat_clients

            FROM period_clients
            """,
            (
                start_ts,
                end_ts,
                start_ts,
                start_ts,
            ),
        ).fetchone()

        # -----------------------------------------
        # MISSED PROCESSING
        # -----------------------------------------

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
                    AND answered = 0

                    AND start_time >= ?
                    AND start_time < ?

                    AND client_key IS NOT NULL
                    AND client_key != ''

                GROUP BY client_key
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

                                AND callback.direction = 1

                                AND callback.start_time >
                                missed_clients.first_missed_time
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS missed_called_back,

                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1

                            FROM calls AS contact

                            WHERE
                                contact.client_key =
                                missed_clients.client_key

                                AND contact.answered = 1

                                AND contact.start_time >
                                missed_clients.first_missed_time
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS missed_contacted,

                SUM(
                    CASE
                        WHEN
                            NOT EXISTS (
                                SELECT 1

                                FROM calls AS callback

                                WHERE
                                    callback.client_key =
                                    missed_clients.client_key

                                    AND callback.direction = 1

                                    AND callback.start_time >
                                    missed_clients.first_missed_time
                            )

                            AND

                            NOT EXISTS (
                                SELECT 1

                                FROM calls AS contact

                                WHERE
                                    contact.client_key =
                                    missed_clients.client_key

                                    AND contact.answered = 1

                                    AND contact.start_time >
                                    missed_clients.first_missed_time
                            )

                        THEN 1
                        ELSE 0
                    END
                ) AS missed_not_processed

            FROM missed_clients
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        # -----------------------------------------
        # SALES
        # -----------------------------------------

        sales_row = conn.execute(
            """
            SELECT
                SUM(
                    CASE
                        WHEN answered = 1
                             AND sale_status = 'bought'
                        THEN 1
                        ELSE 0
                    END
                ) AS bought,

                SUM(
                    CASE
                        WHEN answered = 1
                             AND sale_status = 'not_bought'
                        THEN 1
                        ELSE 0
                    END
                ) AS not_bought,

                SUM(
                    CASE
                        WHEN answered = 1
                             AND (
                                 sale_status IS NULL
                                 OR sale_status = ''
                             )
                        THEN 1
                        ELSE 0
                    END
                ) AS sale_unmarked

            FROM calls

            WHERE
                start_time >= ?
                AND start_time < ?
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
            incoming_answered
            / incoming
            * 100,
            1,
        )
        if incoming
        else 0
    )

    bought = (
        sales_row["bought"]
        or 0
    )

    not_bought = (
        sales_row["not_bought"]
        or 0
    )

    marked_sales = (
        bought
        + not_bought
    )

    sale_conversion = (
        round(
            bought
            / marked_sales
            * 100,
            1,
        )
        if marked_sales
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

            "unique_clients":
                row["unique_clients"]
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

            "unanswered_outgoing":
                row["unanswered_outgoing"]
                or 0,

            "answer_rate":
                answer_rate,

            "new_clients":
                clients_row[
                    "new_clients"
                ] or 0,

            "repeat_clients":
                clients_row[
                    "repeat_clients"
                ] or 0,

            "unique_missed_clients":
                missed_row[
                    "unique_missed_clients"
                ] or 0,

            "missed_called_back":
                missed_row[
                    "missed_called_back"
                ] or 0,

            "missed_contacted":
                missed_row[
                    "missed_contacted"
                ] or 0,

            "missed_not_processed":
                missed_row[
                    "missed_not_processed"
                ] or 0,

            "average_duration_seconds":
                round(
                    row["avg_duration"]
                    or 0
                ),

            "total_duration_seconds":
                row["total_duration"]
                or 0,

            "average_answer_delay_seconds":
                round(
                    row["avg_answer_delay"]
                    or 0
                ),

            # SALES
            "bought":
                bought,

            "not_bought":
                not_bought,

            "sale_unmarked":
                sales_row[
                    "sale_unmarked"
                ] or 0,

            "sale_conversion":
                sale_conversion,
        },
    }


# =========================================================
# SALES REASONS STATS
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

                COUNT(*) AS count

            FROM calls

            WHERE
                start_time >= ?
                AND start_time < ?

                AND sale_status =
                    'not_bought'

            GROUP BY
                no_sale_reason_code,
                no_sale_reason

            ORDER BY count DESC
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

@app.get("/stats/timeline")
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
                    ) AS bucket,

                    COUNT(*) AS calls,

                    SUM(
                        CASE
                            WHEN direction = 0
                            THEN 1
                            ELSE 0
                        END
                    ) AS incoming,

                    SUM(
                        CASE
                            WHEN direction = 1
                            THEN 1
                            ELSE 0
                        END
                    ) AS outgoing

                FROM calls

                WHERE
                    start_time >= ?
                    AND start_time < ?

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
                            row["calls"]
                            if row
                            else 0,

                        "incoming":
                            row["incoming"]
                            if row
                            else 0,

                        "outgoing":
                            row["outgoing"]
                            if row
                            else 0,
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
                    ) AS bucket,

                    COUNT(*) AS calls,

                    SUM(
                        CASE
                            WHEN direction = 0
                            THEN 1
                            ELSE 0
                        END
                    ) AS incoming,

                    SUM(
                        CASE
                            WHEN direction = 1
                            THEN 1
                            ELSE 0
                        END
                    ) AS outgoing

                FROM calls

                WHERE
                    start_time >= ?
                    AND start_time < ?

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
                key = current.isoformat()

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
                            row["calls"]
                            if row
                            else 0,

                        "incoming":
                            row["incoming"]
                            if row
                            else 0,

                        "outgoing":
                            row["outgoing"]
                            if row
                            else 0,
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


# =========================================================
# MANAGERS
# =========================================================

@app.get("/stats/managers")
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
                        user_login,
                        ''
                    ),
                    'Неизвестно'
                ) AS manager,

                COUNT(*) AS calls,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN client_key != ''
                        THEN client_key
                    END
                ) AS unique_clients,

                SUM(
                    CASE
                        WHEN direction = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS incoming,

                SUM(
                    CASE
                        WHEN direction = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS outgoing,

                SUM(
                    CASE
                        WHEN direction = 0
                             AND answered = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS missed,

                SUM(
                    CASE
                        WHEN direction = 0
                             AND answered = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS incoming_answered,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN duration
                        ELSE 0
                    END
                ) AS total_duration,

                SUM(
                    CASE
                        WHEN sale_status = 'bought'
                        THEN 1
                        ELSE 0
                    END
                ) AS bought,

                SUM(
                    CASE
                        WHEN sale_status = 'not_bought'
                        THEN 1
                        ELSE 0
                    END
                ) AS not_bought

            FROM calls

            WHERE
                start_time >= ?
                AND start_time < ?

            GROUP BY manager

            ORDER BY calls DESC
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
            row["incoming_answered"]
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
                    row["unique_clients"]
                    or 0,

                "incoming":
                    incoming,

                "outgoing":
                    row["outgoing"]
                    or 0,

                "missed":
                    row["missed"]
                    or 0,

                "answer_rate":
                    round(
                        incoming_answered
                        / incoming
                        * 100,
                        1,
                    )
                    if incoming
                    else 0,

                "total_duration_seconds":
                    row["total_duration"]
                    or 0,

                "bought":
                    bought,

                "not_bought":
                    not_bought,

                "sale_conversion":
                    round(
                        bought
                        / marked
                        * 100,
                        1,
                    )
                    if marked
                    else 0,
            }
        )

    return {
        "results":
            results
    }


# =========================================================
# SIM
# =========================================================

@app.get("/stats/sims")
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
                ) AS sim,

                src_slot,

                COUNT(*) AS calls,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN client_key != ''
                        THEN client_key
                    END
                ) AS unique_clients,

                SUM(
                    CASE
                        WHEN direction = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS incoming,

                SUM(
                    CASE
                        WHEN direction = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS outgoing,

                SUM(
                    CASE
                        WHEN direction = 0
                             AND answered = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS missed,

                SUM(
                    CASE
                        WHEN answered = 1
                        THEN duration
                        ELSE 0
                    END
                ) AS total_duration

            FROM calls

            WHERE
                start_time >= ?
                AND start_time < ?

            GROUP BY
                sim,
                src_slot

            ORDER BY calls DESC
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
                    row["unique_clients"]
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
                    row["total_duration"]
                    or 0,
            }

            for row in rows
        ]
    }


# =========================================================
# RECENT
# =========================================================

@app.get("/stats/recent")
def stats_recent(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 30,
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

                direction,
                answered,

                user_login,
                src_number,

                start_time,
                duration,

                sale_status,
                no_sale_reason

            FROM calls

            WHERE
                start_time >= ?
                AND start_time < ?

            ORDER BY start_time DESC

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
            datetime.fromtimestamp(
                start_time,
                UZ_TZ,
            ).strftime(
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
                    row["client_number"]
                    or "—",

                "client_name":
                    row["client_name"]
                    or "",

                "direction":
                    row["direction"],

                "answered":
                    row["answered"]
                    or 0,

                "manager":
                    row["user_login"]
                    or "—",

                "sim":
                    row["src_number"]
                    or "—",

                "local_time":
                    local_time,

                "duration":
                    row["duration"]
                    or 0,

                "sale_status":
                    row["sale_status"],

                "no_sale_reason":
                    row["no_sale_reason"],
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
    max-width: 1800px;
    margin: auto;
    padding: 26px;
}

.header {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 28px;
}

.title {
    margin: 0;
    font-size: 34px;
}

.subtitle {
    color: #888;
    margin-top: 7px;
}

.periods {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.period-btn,
.chart-btn,
.apply-btn {
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
}

.custom-panel.visible {
    display: flex;
}

.custom-panel input {
    background: #111;
    color: #fff;

    border: 1px solid #333;
    border-radius: 8px;

    padding: 9px;
}

.apply-btn {
    background: #d9b565;
    color: #111;
}

.section-title {
    margin:
        30px
        0
        14px;

    font-size: 20px;
}

.grid {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(205px, 1fr)
        );

    gap: 15px;
}

.card,
.panel {
    background: #1b1b1b;

    border:
        1px
        solid
        #333;

    border-radius: 16px;
}

.card {
    padding: 20px;
    min-height: 118px;
}

.card.sale-good {
    border-color:
        rgba(
            112,
            210,
            130,
            .3
        );
}

.card.sale-bad {
    border-color:
        rgba(
            255,
            120,
            120,
            .25
        );
}

.label {
    color: #a5a5a5;

    font-size: 14px;

    margin-bottom: 12px;
}

.value {
    font-size: 32px;
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

    margin-bottom: 18px;
}

.panel h2 {
    margin: 0;
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

    padding:
        30px
        4px
        0;
}

.bar-wrap {
    flex: 1;

    min-width: 44px;

    text-align: center;
}

.bar-value {
    height: 22px;

    font-size: 12px;
}

.bar {
    width: 70%;

    max-width: 58px;

    min-height: 2px;

    margin: auto;

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
            minmax(0, 1fr)
        );

    gap: 24px;
}

.table-wrap {
    overflow-x: auto;
}

table {
    width: 100%;

    border-collapse:
        collapse;

    min-width: 650px;
}

th {
    text-align: left;

    color: #888;

    font-size: 12px;

    padding: 11px 10px;

    border-bottom:
        1px
        solid
        #333;
}

td {
    padding: 13px 10px;

    border-bottom:
        1px
        solid
        #292929;

    font-size: 13px;
}

.phone {
    font-family:
        ui-monospace,
        monospace;
}

.sale-result {
    white-space: nowrap;
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

@media (
    max-width: 1000px
) {
    .header {
        flex-direction:
            column;
    }

    .tables {
        grid-template-columns:
            1fr;
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
                minmax(0, 1fr)
            );

        gap: 10px;
    }

    .card {
        padding: 15px;
    }

    .value {
        font-size: 25px;
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
                id="period_label"
                class="subtitle"
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
        id="custom_panel"
        class="custom-panel"
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
            id="apply_custom"
            class="apply-btn"
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
                Перезвонили
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

        <div class="card sale-good">

            <div class="label">
                ✅ Купил
            </div>

            <div
                class="value"
                id="bought"
            >
                —
            </div>

        </div>


        <div class="card sale-bad">

            <div class="label">
                ❌ Не купил
            </div>

            <div
                class="value"
                id="not_bought"
            >
                —
            </div>

        </div>


        <div class="card">

            <div class="label">
                Не отмечено
            </div>

            <div
                class="value"
                id="sale_unmarked"
            >
                —
            </div>

        </div>


        <div class="card">

            <div class="label">
                Конверсия
            </div>

            <div
                class="value"
                id="sale_conversion"
            >
                —
            </div>

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
            id="timeline_chart"
            class="chart"
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
                    <th>Причина</th>
                    <th>Количество</th>
                </tr>

                </thead>

                <tbody
                    id="reasons_body"
                ></tbody>

            </table>

        </div>

    </div>


    <div class="tables">

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
                    <th>Клиент</th>
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

    seconds =
        Number(
            seconds || 0
        );

    const hours =
        Math.floor(
            seconds / 3600
        );

    const minutes =
        Math.floor(
            (
                seconds % 3600
            )
            / 60
        );

    const secs =
        seconds % 60;

    if (hours > 0) {

        return (
            hours
            + " ч "
            + minutes
            + " мин"
        );
    }

    if (minutes > 0) {

        return (
            minutes
            + " мин "
            + secs
            + " сек"
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

    if (!response.ok) {

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


    document.getElementById(
        "period_label"
    ).textContent =
        data.period.label;


    const simpleValues = {
        calls:
            s.calls,

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
            simpleValues
        )
    ) {

        document
            .getElementById(
                id
            )
            .textContent =
                value;
    }


    document.getElementById(
        "average_duration"
    ).textContent =
        formatDuration(
            s.average_duration_seconds
        );


    document.getElementById(
        "total_duration"
    ).textContent =
        formatDuration(
            s.total_duration_seconds
        );


    document.getElementById(
        "average_answer_delay"
    ).textContent =
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

    document.getElementById(
        "chart_title"
    ).textContent =
        data.granularity ===
        "hour"
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

        td.textContent =
            "Пока нет данных";

        td.style.color =
            "#777";

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

            client.textContent =
                row.client_name
                    ? (
                        row.client_name
                        + " · "
                        + row.client_number
                    )
                    : row.client_number;


            const direction =
                document.createElement(
                    "td"
                );

            direction.textContent =
                row.direction === 0
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

            sale.className =
                "sale-result";


            if (
                row.sale_status
                === "bought"
            ) {

                sale.classList.add(
                    "sale-bought"
                );

                sale.textContent =
                    "✅ Купил";

            } else if (
                row.sale_status
                === "not_bought"
            ) {

                sale.classList.add(
                    "sale-not-bought"
                );

                sale.textContent =
                    row.no_sale_reason
                    || "❌ Не купил";

            } else if (
                row.answered
            ) {

                sale.classList.add(
                    "sale-unmarked"
                );

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

    } catch (error) {

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
                || !customTo
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

    webhook = data.get(
        "webhook",
        {},
    )

    event = data.get(
        "event",
        {},
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

    # ---------------------------------------------
    # RECORDING
    # ---------------------------------------------

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

    # ---------------------------------------------
    # TALK DURATION
    # ---------------------------------------------

    (
        talk_duration,
        duration_source,
    ) = get_talk_duration(
        event,
        audio_duration,
    )

    # ---------------------------------------------
    # SAVE DATABASE
    # ---------------------------------------------

    save_result = save_call(
        webhook,
        event,
        talk_duration,
        duration_source,
    )

    call_id = save_result[
        "call_id"
    ]

    already_sent = save_result[
        "already_sent"
    ]

    # ---------------------------------------------
    # DUPLICATE WEBHOOK
    # ---------------------------------------------

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

    # ---------------------------------------------
    # TELEGRAM TEXT
    # ---------------------------------------------

    text = build_telegram_message(
        event,
        webhook,
        talk_duration,
    )

    # Кнопки только если разговор состоялся
    sale_keyboard = (
        build_sale_keyboard(
            call_id
        )
        if answered
        else None
    )

    # ---------------------------------------------
    # SEND TELEGRAM
    # ---------------------------------------------

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
                        sale_keyboard,
                )
            )

        else:
            telegram_result = (
                send_text_message(
                    text,
                    reply_markup=
                        sale_keyboard,
                )
            )

        mark_telegram_sent(
            call_id,
            telegram_result,
        )

    except Exception as exc:
        print(
            "TELEGRAM ERROR:",
            exc,
        )

        raise

    return {
        "ok": True,

        "call_id":
            call_id,

        "duration":
            talk_duration,

        "duration_source":
            duration_source,
    }