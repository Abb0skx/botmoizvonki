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

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # -------------------------------------------------
        # Миграции старой базы
        # -------------------------------------------------

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(calls)"
            ).fetchall()
        }

        migrations = {
            "client_key":
                "ALTER TABLE calls "
                "ADD COLUMN client_key TEXT",

            "api_duration":
                "ALTER TABLE calls "
                "ADD COLUMN api_duration INTEGER",

            "duration_source":
                "ALTER TABLE calls "
                "ADD COLUMN duration_source TEXT",

            "telegram_sent":
                "ALTER TABLE calls "
                "ADD COLUMN telegram_sent INTEGER DEFAULT 0",
        }

        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

        # -------------------------------------------------
        # Нормализуем старые номера
        # -------------------------------------------------

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

        # -------------------------------------------------
        # Старые записи не считаем
        # "неотправленными" в Telegram
        # -------------------------------------------------

        conn.execute(
            """
            UPDATE calls
            SET telegram_sent = 1
            WHERE telegram_sent IS NULL
            """
        )

        # -------------------------------------------------
        # Индексы
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

        conn.commit()


init_db()


# =========================================================
# RECORDING
# =========================================================

def prepare_recording(
    recording_url: str,
):
    """
    Запись скачивается ОДИН раз.

    Возвращает:

    (
        audio_duration_seconds | None,
        voice_ogg_bytes | None
    )
    """

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
        source_path = Path(
            tmpdir
        ) / "call_source"

        voice_path = Path(
            tmpdir
        ) / "call.ogg"

        source_path.write_bytes(
            response.content
        )

        # -------------------------------------------------
        # FFPROBE
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

        # -------------------------------------------------
        # FFMPEG -> Telegram Voice
        # -------------------------------------------------

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
    """
    Приоритет:

    1. Реальная длительность аудиозаписи.
    2. end_time - answer_time.
    3. duration от API.
    """

    answered = int(
        event.get(
            "answered",
            0,
        )
        or 0
    )

    if not answered:
        return 0, "none"

    # -------------------------------------------------
    # 1. AUDIO
    # -------------------------------------------------

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

    # -------------------------------------------------
    # 2. TIMESTAMPS
    # -------------------------------------------------

    if (
        answer_time > 0
        and end_time > answer_time
    ):
        return (
            end_time - answer_time,
            "timestamps",
        )

    # -------------------------------------------------
    # 3. API
    # -------------------------------------------------

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

    with connect_db() as conn:

        conn.execute(
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
                    excluded.recording
            """,
            (
                db_call_id,

                event.get(
                    "event_pbx_call_id"
                ),

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

        row = conn.execute(
            """
            SELECT
                telegram_sent

            FROM calls

            WHERE db_call_id = ?
            """,
            (
                db_call_id,
            ),
        ).fetchone()

    return bool(
        row
        and row["telegram_sent"]
    )


def mark_telegram_sent(
    db_call_id,
):
    if db_call_id is None:
        return

    with connect_db() as conn:
        conn.execute(
            """
            UPDATE calls

            SET telegram_sent = 1

            WHERE db_call_id = ?
            """,
            (
                db_call_id,
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
# FORMATTERS
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

    history = get_client_history(
        client_number
    )

    contacts = history[
        "calls_count"
    ]

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

    # -------------------------------------------------
    # CLIENT
    # -------------------------------------------------

    lines = [
        title,
        "",
    ]

    if client_name:
        lines.append(
            "👤 "
            f"<b>{escape(str(client_name))}</b>"
        )

    lines.append(
        "📞 "
        f"<code>{escape(str(client_number or '—'))}</code>"
    )

    # -------------------------------------------------
    # NEW / REPEAT CLIENT
    # -------------------------------------------------

    if contacts <= 1:
        lines.append(
            "🆕 Новый клиент"
        )

    else:
        lines.append(
            "🔁 "
            f"Контактов с номером: <b>{contacts}</b>"
        )

    lines.append("")

    # -------------------------------------------------
    # CALL DETAILS
    # -------------------------------------------------

    lines.append(
        "👨‍💼 "
        f"{escape(str(manager or '—'))}"
    )

    lines.append(
        "📲 "
        f"{escape(str(sim or '—'))}"
    )

    lines.append(
        "🕐 "
        f"{format_call_time(start_time)}"
    )

    if answered:
        lines.append(
            "⏱ Разговор: "
            f"<b>{format_duration(talk_duration)}</b>"
        )

    return "\n".join(
        lines
    )


# =========================================================
# TELEGRAM SEND
# =========================================================

def send_text_message(
    text: str,
):
    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        "sendMessage"
    )

    response = HTTP.post(
        url,
        data={
            "chat_id":
                TELEGRAM_CHAT_ID,

            "text":
                text,

            "parse_mode":
                "HTML",

            "disable_web_page_preview":
                True,
        },
        timeout=30,
    )

    response.raise_for_status()


def send_voice_bytes(
    voice_bytes: bytes,
    caption: str,
):
    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        "sendVoice"
    )

    response = HTTP.post(
        url,
        data={
            "chat_id":
                TELEGRAM_CHAT_ID,

            "caption":
                caption,

            "parse_mode":
                "HTML",
        },
        files={
            "voice": (
                "call.ogg",
                voice_bytes,
                "audio/ogg",
            )
        },
        timeout=60,
    )

    response.raise_for_status()


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
                            answer_time
                            - start_time
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
                        WHEN NOT EXISTS (
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
                ) AS missed_not_called_back

            FROM missed_clients
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

            "missed_not_called_back":
                missed_row[
                    "missed_not_called_back"
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
        },
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

    start_ts = p["start_ts"]
    end_ts = p["end_ts"]

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

                    COUNT(*)
                        AS calls,

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
                    start_ts,
                    end_ts,
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

                    COUNT(*)
                        AS calls,

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
                    start_ts,
                    end_ts,
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


@app.get("/stats/hourly")
def stats_hourly():
    return stats_timeline(
        period="today"
    )


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

                COUNT(*)
                    AS calls,

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
                ) AS total_duration

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
                    (
                        round(
                            incoming_answered
                            / incoming
                            * 100,
                            1,
                        )
                        if incoming
                        else 0
                    ),

                "total_duration_seconds":
                    row["total_duration"]
                    or 0,
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

                COUNT(*)
                    AS calls,

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
                duration_source

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

                "duration_source":
                    row["duration_source"]
                    or "legacy",
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
    border: 1px solid #333;
    border-radius: 16px;
}

.card {
    padding: 20px;
    min-height: 118px;
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
    border-radius: 8px 8px 2px 2px;
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
    border-collapse: collapse;
    min-width: 650px;
}

th {
    text-align: left;
    color: #888;
    font-size: 12px;
    font-weight: 600;
    padding: 11px 10px;
    border-bottom: 1px solid #333;
}

td {
    padding: 13px 10px;
    border-bottom: 1px solid #292929;
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

.badge {
    display: inline-block;
    border-radius: 20px;
    padding: 5px 9px;
    font-size: 11px;
    background: #282828;
}

.badge.good {
    color: #a9e5b3;
}

.badge.bad {
    color: #ff9d9d;
}

@media (max-width: 1000px) {

    .header {
        flex-direction: column;
    }

    .tables {
        grid-template-columns: 1fr;
    }
}

@media (max-width: 600px) {

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
            <div class="label">Всего звонков</div>
            <div class="value" id="calls">—</div>
        </div>

        <div class="card">
            <div class="label">Уникальные клиенты</div>
            <div class="value" id="unique_clients">—</div>
        </div>

        <div class="card">
            <div class="label">Входящие</div>
            <div class="value" id="incoming">—</div>
        </div>

        <div class="card">
            <div class="label">Исходящие</div>
            <div class="value" id="outgoing">—</div>
        </div>

        <div class="card">
            <div class="label">Отвеченные</div>
            <div class="value" id="answered">—</div>
        </div>

        <div class="card">
            <div class="label">Пропущенные входящие</div>
            <div class="value" id="missed">—</div>
        </div>

        <div class="card">
            <div class="label">Ответили на входящие</div>
            <div class="value" id="answer_rate">—</div>
        </div>

        <div class="card">
            <div class="label">Новые клиенты</div>
            <div class="value" id="new_clients">—</div>
        </div>

        <div class="card">
            <div class="label">Повторные клиенты</div>
            <div class="value" id="repeat_clients">—</div>
        </div>

        <div class="card">
            <div class="label">Уникально пропущено</div>
            <div class="value" id="unique_missed_clients">—</div>
        </div>

        <div class="card">
            <div class="label">Потом перезвонили</div>
            <div class="value" id="missed_called_back">—</div>
        </div>

        <div class="card">
            <div class="label">Не перезвонили</div>
            <div class="value" id="missed_not_called_back">—</div>
        </div>

        <div class="card">
            <div class="label">Средний разговор</div>
            <div class="value" id="average_duration">—</div>
        </div>

        <div class="card">
            <div class="label">Общее время разговоров</div>
            <div class="value" id="total_duration">—</div>
        </div>

        <div class="card">
            <div class="label">Среднее время ответа</div>
            <div class="value" id="average_answer_delay">—</div>
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


    <div class="tables">

        <div class="panel">

            <div class="panel-header">
                <h2>Менеджеры</h2>
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
                <h2>SIM-карты</h2>
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
            <h2>Последние звонки</h2>
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

let selectedPeriod = "today";

let customFrom = "";
let customTo = "";

let chartMode = "all";

let timelineData = [];
let timelineGranularity = "hour";


function formatDuration(seconds) {

    seconds = Number(
        seconds || 0
    );

    const hours =
        Math.floor(
            seconds / 3600
        );

    const minutes =
        Math.floor(
            (seconds % 3600)
            / 60
        );

    const secs =
        seconds % 60;

    if (hours > 0) {

        return (
            hours +
            " ч " +
            minutes +
            " мин"
        );
    }

    if (minutes > 0) {

        return (
            minutes +
            " мин " +
            secs +
            " сек"
        );
    }

    return (
        secs +
        " сек"
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
        selectedPeriod === "custom"
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


async function getJson(url) {

    const response =
        await fetch(
            url + queryString()
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

    document.getElementById(
        "calls"
    ).textContent =
        s.calls;

    document.getElementById(
        "unique_clients"
    ).textContent =
        s.unique_clients;

    document.getElementById(
        "incoming"
    ).textContent =
        s.incoming;

    document.getElementById(
        "outgoing"
    ).textContent =
        s.outgoing;

    document.getElementById(
        "answered"
    ).textContent =
        s.answered;

    document.getElementById(
        "missed"
    ).textContent =
        s.missed;

    document.getElementById(
        "answer_rate"
    ).textContent =
        s.answer_rate + "%";

    document.getElementById(
        "new_clients"
    ).textContent =
        s.new_clients;

    document.getElementById(
        "repeat_clients"
    ).textContent =
        s.repeat_clients;

    document.getElementById(
        "unique_missed_clients"
    ).textContent =
        s.unique_missed_clients;

    document.getElementById(
        "missed_called_back"
    ).textContent =
        s.missed_called_back;

    document.getElementById(
        "missed_not_called_back"
    ).textContent =
        s.missed_not_called_back;

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

    timelineGranularity =
        data.granularity;

    document.getElementById(
        "chart_title"
    ).textContent =
        timelineGranularity === "hour"
            ? "Звонки по часам"
            : "Звонки по дням";

    renderTimeline();
}


function renderTimeline() {

    const chart =
        document.getElementById(
            "timeline_chart"
        );

    chart.innerHTML = "";

    const values =
        timelineData.map(
            item => {

                if (
                    chartMode ===
                    "incoming"
                ) {
                    return (
                        item.incoming
                    );
                }

                if (
                    chartMode ===
                    "outgoing"
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
                chartMode ===
                "outgoing"
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


async function loadManagers() {

    const data =
        await getJson(
            "/stats/managers"
        );

    const body =
        document.getElementById(
            "managers_body"
        );

    body.innerHTML = "";

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
                row.answer_rate + "%",
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

    body.innerHTML = "";

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

    body.innerHTML = "";

    data.results.forEach(
        row => {

            const tr =
                document.createElement(
                    "tr"
                );

            const values = [
                row.local_time,

                row.client_name
                    ? (
                        row.client_name
                        + " · "
                        + row.client_number
                    )
                    : row.client_number,

                row.direction === 0
                    ? "Входящий"
                    : "Исходящий",

                row.answered
                    ? "Отвечен"
                    : "Не отвечен",

                row.manager,

                row.sim,

                formatDuration(
                    row.duration
                ),
            ];

            values.forEach(
                (
                    value,
                    index
                ) => {

                    const td =
                        document.createElement(
                            "td"
                        );

                    td.textContent =
                        value;

                    if (
                        index === 1
                        || index === 5
                    ) {
                        td.className =
                            "phone";
                    }

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


async function loadAll() {

    try {

        await Promise.all([
            loadStats(),
            loadTimeline(),
            loadManagers(),
            loadSims(),
            loadRecent(),
        ]);

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

                button.classList.toggle(
                    "active",

                    button.dataset.period
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

            period ===
            "custom"
        );

    if (
        period !==
        "custom"
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
                        button.dataset.period
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
                        button.dataset.chart;

                    document
                        .querySelectorAll(
                            ".chart-btn"
                        )
                        .forEach(
                            item => {

                                item.classList.toggle(
                                    "active",

                                    item.dataset.chart
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
        webhook.get("action")
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

    # -------------------------------------------------
    # Один раз скачиваем запись
    # -------------------------------------------------

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

    # -------------------------------------------------
    # Определяем реальную длительность
    # -------------------------------------------------

    (
        talk_duration,
        duration_source,
    ) = get_talk_duration(
        event,
        audio_duration,
    )

    # -------------------------------------------------
    # Сохраняем
    # -------------------------------------------------

    already_sent = save_call(
        webhook,
        event,
        talk_duration,
        duration_source,
    )

    # -------------------------------------------------
    # Защита от повторного webhook
    # -------------------------------------------------

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

    # -------------------------------------------------
    # Telegram text
    # -------------------------------------------------

    text = build_telegram_message(
        event,
        webhook,
        talk_duration,
    )

    # -------------------------------------------------
    # Telegram
    # -------------------------------------------------

    try:
        if (
            answered
            and voice_bytes
        ):
            send_voice_bytes(
                voice_bytes,
                text,
            )

        else:
            send_text_message(
                text
            )

        mark_telegram_sent(
            event.get(
                "db_call_id"
            )
        )

    except Exception as exc:
        print(
            "TELEGRAM ERROR:",
            exc,
        )

        raise

    return {
        "ok": True,

        "duration":
            talk_duration,

        "duration_source":
            duration_source,
    }