import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse


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
):
    if not phone:
        return ""

    return "".join(
        char
        for char in str(phone)
        if char.isdigit()
    )


def get_talk_duration(
    event: dict,
) -> int:
    answered = int(
        event.get(
            "answered",
            0,
        )
        or 0
    )

    if not answered:
        return 0

    recording = event.get(
        "recording"
    )

    # 1. Самый надёжный источник —
    # фактическая длина записи разговора
    if recording:
        audio_duration = (
            get_audio_duration_seconds(
                recording
            )
        )

        if (
            audio_duration is not None
            and audio_duration >= 0
        ):
            return audio_duration

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

    # 2. Если запись недоступна —
    # считаем по timestamps
    if (
        answer_time > 0
        and end_time > answer_time
    ):
        return (
            end_time
            - answer_time
        )

    # 3. Последний запасной вариант
    return int(
        event.get(
            "duration",
            0,
        )
        or 0
    )

def get_audio_duration_seconds(
    recording_url: str,
) -> int | None:
    if not recording_url:
        return None

    try:
        audio_response = requests.get(
            recording_url,
            timeout=60,
        )

        audio_response.raise_for_status()

        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(
                tmpdir,
                "call_source",
            )

            with open(
                source_path,
                "wb",
            ) as file:
                file.write(
                    audio_response.content
                )

            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    source_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

            value = result.stdout.strip()

            if not value:
                return None

            duration = float(
                value
            )

            if duration <= 0:
                return None

            return round(
                duration
            )

    except Exception as exc:
        print(
            "AUDIO DURATION ERROR:",
            exc,
        )

        return None

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

                recording TEXT,

                account_id TEXT,
                account_name TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # -----------------------------------------
        # Миграция старой базы
        # -----------------------------------------

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(calls)"
            ).fetchall()
        }

        if "client_key" not in columns:
            conn.execute(
                """
                ALTER TABLE calls
                ADD COLUMN client_key TEXT
                """
            )

        # -----------------------------------------
        # Нормализация старых телефонов
        # -----------------------------------------

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

        # -----------------------------------------
        # Исправляем длительность старых звонков
        #
        # Для отвеченных звонков:
        # duration = end_time - answer_time
        # -----------------------------------------

        conn.execute(
            """
            UPDATE calls

            SET duration =
                end_time - answer_time

            WHERE
                answered = 1

                AND answer_time IS NOT NULL
                AND end_time IS NOT NULL

                AND answer_time > 0
                AND end_time > answer_time
            """
        )

        # -----------------------------------------
        # Пропущенные не имеют разговора
        # -----------------------------------------

        conn.execute(
            """
            UPDATE calls

            SET duration = 0

            WHERE answered = 0
            """
        )

        # -----------------------------------------
        # Индексы
        # -----------------------------------------

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


def save_call(
    webhook: dict,
    event: dict,
):
    client_number = event.get(
        "client_number"
    )

    client_key = normalize_phone(
        client_number
    )

    talk_duration = get_talk_duration(
        event
    )

    with connect_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO calls (
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

                recording,

                account_id,
                account_name
            )
            VALUES (
                ?, ?,

                ?, ?, ?,

                ?, ?,

                ?, ?,

                ?, ?, ?,

                ?,

                ?, ?, ?,

                ?,

                ?,

                ?, ?
            )
            """,
            (
                event.get(
                    "db_call_id"
                ),

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


init_db()


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
    now = datetime.now(
        UZ_TZ
    )

    today = now.date()

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
            end_date - start_date
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
            f" — "
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

    start_ts = int(
        start_dt.timestamp()
    )

    end_ts = int(
        end_dt.timestamp()
    )

    days = (
        end_date
        - start_date
    ).days + 1

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
            start_ts,

        "end_ts":
            end_ts,

        "days":
            days,
    }


# =========================================================
# TELEGRAM
# =========================================================

def send_text_message(
    text: str,
):
    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        "sendMessage"
    )

    response = requests.post(
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


def send_as_voice(
    recording_url: str,
    caption: str,
):
    audio_response = requests.get(
        recording_url,
        timeout=60,
    )

    audio_response.raise_for_status()

    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = os.path.join(
            tmpdir,
            "call_source",
        )

        voice_path = os.path.join(
            tmpdir,
            "call.ogg",
        )

        with open(
            source_path,
            "wb",
        ) as file:
            file.write(
                audio_response.content
            )

        subprocess.run(
            [
                "ffmpeg",
                "-y",

                "-i",
                source_path,

                "-vn",

                "-c:a",
                "libopus",

                "-b:a",
                "32k",

                "-vbr",
                "on",

                "-application",
                "voip",

                voice_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        url = (
            "https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/"
            "sendVoice"
        )

        with open(
            voice_path,
            "rb",
        ) as voice:
            response = requests.post(
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
                        voice,
                        "audio/ogg",
                    )
                },
                timeout=60,
            )

        response.raise_for_status()


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
                        WHEN answered = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS missed,

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

    answered = (
        row["answered"]
        or 0
    )

    calls = (
        row["calls"]
        or 0
    )

    answer_rate = (
        round(
            answered
            / calls
            * 100,
            1,
        )
        if calls
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
                calls,

            "unique_clients":
                row["unique_clients"]
                or 0,

            "incoming":
                row["incoming"]
                or 0,

            "outgoing":
                row["outgoing"]
                or 0,

            "answered":
                answered,

            "missed":
                row["missed"]
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
                row["bucket"]: row
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
                row["bucket"]: row
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
                        WHEN answered = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS answered,

                SUM(
                    CASE
                        WHEN answered = 0
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
                ) AS total_duration,

                AVG(
                    CASE
                        WHEN answered = 1
                        THEN duration
                    END
                ) AS avg_duration,

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

            GROUP BY manager

            ORDER BY calls DESC
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    result = []

    for row in rows:
        calls = (
            row["calls"]
            or 0
        )

        answered = (
            row["answered"]
            or 0
        )

        result.append(
            {
                "manager":
                    row["manager"],

                "calls":
                    calls,

                "unique_clients":
                    row["unique_clients"]
                    or 0,

                "incoming":
                    row["incoming"]
                    or 0,

                "outgoing":
                    row["outgoing"]
                    or 0,

                "answered":
                    answered,

                "missed":
                    row["missed"]
                    or 0,

                "answer_rate":
                    round(
                        answered
                        / calls
                        * 100,
                        1,
                    )
                    if calls
                    else 0,

                "total_duration_seconds":
                    row["total_duration"]
                    or 0,

                "average_duration_seconds":
                    round(
                        row["avg_duration"]
                        or 0
                    ),

                "average_answer_delay_seconds":
                    round(
                        row["avg_answer_delay"]
                        or 0
                    ),
            }
        )

    return {
        "results":
            result
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
                        WHEN answered = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS answered,

                SUM(
                    CASE
                        WHEN answered = 0
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

                "answered":
                    row["answered"]
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
# RECENT CALLS
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
                answer_time,
                end_time,

                duration

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

        if start_time:
            local_time = (
                datetime.fromtimestamp(
                    start_time,
                    UZ_TZ,
                )
                .strftime(
                    "%d.%m.%Y %H:%M"
                )
            )

        else:
            local_time = "—"

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

                "start_time":
                    start_time,

                "local_time":
                    local_time,

                "duration":
                    row["duration"]
                    or 0,
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
    border-radius: 9px;
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
    border-radius: 12px;
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
    border-radius: 14px;
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
    font-weight: 600;
}

.bar {
    width: 70%;
    max-width: 58px;
    min-height: 2px;
    margin: 0 auto;
    background: #d9b565;
    border-radius: 7px 7px 0 0;
    transition: height .2s ease;
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
            <div class="label">
                Всего звонков
            </div>
            <div class="value" id="calls">—</div>
        </div>

        <div class="card">
            <div class="label">
                Уникальные клиенты
            </div>
            <div class="value" id="unique_clients">—</div>
        </div>

        <div class="card">
            <div class="label">
                Входящие
            </div>
            <div class="value" id="incoming">—</div>
        </div>

        <div class="card">
            <div class="label">
                Исходящие
            </div>
            <div class="value" id="outgoing">—</div>
        </div>

        <div class="card">
            <div class="label">
                Отвеченные
            </div>
            <div class="value" id="answered">—</div>
        </div>

        <div class="card">
            <div class="label">
                Пропущенные
            </div>
            <div class="value" id="missed">—</div>
        </div>

        <div class="card">
            <div class="label">
                Ответили, %
            </div>
            <div class="value" id="answer_rate">—</div>
        </div>

        <div class="card">
            <div class="label">
                Новые клиенты
            </div>
            <div class="value" id="new_clients">—</div>
        </div>

        <div class="card">
            <div class="label">
                Повторные клиенты
            </div>
            <div class="value" id="repeat_clients">—</div>
        </div>

        <div class="card">
            <div class="label">
                Уникально пропущено
            </div>
            <div class="value" id="unique_missed_clients">—</div>
        </div>

        <div class="card">
            <div class="label">
                Потом перезвонили
            </div>
            <div class="value" id="missed_called_back">—</div>
        </div>

        <div class="card">
            <div class="label">
                Не перезвонили
            </div>
            <div class="value" id="missed_not_called_back">—</div>
        </div>

        <div class="card">
            <div class="label">
                Средний разговор
            </div>
            <div class="value" id="average_duration">—</div>
        </div>

        <div class="card">
            <div class="label">
                Общее время разговоров
            </div>
            <div class="value" id="total_duration">—</div>
        </div>

        <div class="card">
            <div class="label">
                Среднее время ответа
            </div>
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
                    <th>Длительность</th>
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

        if (minutes > 0) {
            return (
                hours +
                " ч " +
                minutes +
                " мин"
            );
        }

        return (
            hours +
            " ч"
        );
    }

    if (minutes > 0) {

        if (secs > 0) {
            return (
                minutes +
                " мин " +
                secs +
                " сек"
            );
        }

        return (
            minutes +
            " мин"
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

        const text =
            await response.text();

        throw new Error(
            text
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

    for (
        let i = 0;
        i < timelineData.length;
        i++
    ) {

        const item =
            timelineData[i];

        const value =
            values[i];

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
                        value /
                        maxValue
                    )
                    * 210
                )
            )
            + "px";

        bar.title =
            item.label +
            " — всего: " +
            item.calls +
            ", входящие: " +
            item.incoming +
            ", исходящие: " +
            item.outgoing;


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

    for (
        const row
        of data.results
    ) {

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

        for (
            const value
            of values
        ) {

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

        body.appendChild(
            tr
        );
    }
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

    for (
        const row
        of data.results
    ) {

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

        for (
            const value
            of values
        ) {

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

        body.appendChild(
            tr
        );
    }
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

    for (
        const row
        of data.results
    ) {

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

        const badge =
            document.createElement(
                "span"
            );

        badge.className =
            "badge " +
            (
                row.answered
                    ? "good"
                    : "bad"
            );

        badge.textContent =
            row.answered
                ? "Отвечен"
                : "Не отвечен";

        status.appendChild(
            badge
        );


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

        body.appendChild(
            tr
        );
    }
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

    const customPanel =
        document.getElementById(
            "custom_panel"
        );

    customPanel.classList.toggle(
        "visible",
        period === "custom"
    );

    if (
        period !== "custom"
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

                                item
                                    .classList
                                    .toggle(
                                        "active",
                                        item
                                            .dataset
                                            .chart
                                            ===
                                            chartMode
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

    # -----------------------------------------
    # Сначала сохраняем звонок
    # -----------------------------------------

    save_call(
        webhook,
        event,
    )

    direction = event.get(
        "direction"
    )

    client_number = event.get(
        "client_number",
        "Неизвестно",
    )

    client_name = event.get(
        "client_name",
        "",
    )

    # -----------------------------------------
    # Реальная длительность разговора
    # -----------------------------------------

    duration = get_talk_duration(
        event
    )

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

    src_number = event.get(
        "src_number",
        "",
    )

    user_login = webhook.get(
        "user_login",
        "",
    )

    minutes, seconds = divmod(
        duration,
        60,
    )

    if (
        not answered
        and direction == 0
    ):
        call_type = (
            "❌ Пропущенный звонок"
        )

    elif (
        not answered
        and direction == 1
    ):
        call_type = (
            "⚠️ Неотвеченный исходящий"
        )

    elif direction == 0:
        call_type = (
            "📥 Входящий звонок"
        )

    else:
        call_type = (
            "📤 Исходящий звонок"
        )

    text = (
        f"<b>{call_type}</b>\n\n"

        f"👤 Клиент: "
        f"{client_name or '—'}\n"

        f"📱 Номер: "
        f"<code>{client_number}</code>\n"

        f"👨‍💼 Менеджер: "
        f"{user_login or '—'}\n"

        f"📲 SIM: "
        f"{src_number or '—'}\n"

        f"⏱ Длительность: "
        f"{minutes}:{seconds:02d}\n"

        f"✅ Ответ: "
        f"{'Да' if answered else 'Нет'}"
    )

    if (
        answered
        and recording
    ):
        send_as_voice(
            recording_url=
                recording,

            caption=
                text,
        )

    else:
        send_text_message(
            text
        )

    return {
        "ok": True
    }