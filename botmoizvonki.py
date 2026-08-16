import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI()


# -----------------------------
# ENV
# -----------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "data" / ".env"

load_dotenv(
    dotenv_path=ENV_FILE,
    override=True,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print("ENV FILE:", ENV_FILE)
print("ENV EXISTS:", ENV_FILE.exists())
print("TOKEN EXISTS:", bool(TELEGRAM_BOT_TOKEN))
print("CHAT ID EXISTS:", bool(TELEGRAM_CHAT_ID))


# -----------------------------
# DATABASE
# -----------------------------

DB_PATH = Path("/app/data/calls.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                db_call_id INTEGER UNIQUE,
                event_pbx_call_id TEXT,

                client_number TEXT,
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

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.commit()


def save_call(webhook: dict, event: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO calls (
                db_call_id,
                event_pbx_call_id,

                client_number,
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
                ?, ?,
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
                event.get("db_call_id"),
                event.get("event_pbx_call_id"),

                event.get("client_number"),
                event.get("client_name"),

                event.get("direction"),
                event.get("answered"),

                webhook.get("user_id"),
                webhook.get("user_login"),

                event.get("src_number"),
                event.get("src_id"),
                event.get("src_slot"),

                event.get("event_created"),

                event.get("start_time"),
                event.get("answer_time"),
                event.get("end_time"),

                event.get("duration"),

                event.get("recording"),

                webhook.get("account_id"),
                webhook.get("account_name"),
            ),
        )

        conn.commit()


init_db()

print("DATABASE:", DB_PATH)


# -----------------------------
# TELEGRAM
# -----------------------------

def send_text_message(text: str):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    response.raise_for_status()


def send_as_voice(
    recording_url: str,
    caption: str,
):
    # Скачиваем запись из "Мои Звонки"
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

        # Сохраняем оригинальную запись
        with open(source_path, "wb") as file:
            file.write(audio_response.content)

        # Конвертируем запись в OGG/Opus,
        # чтобы Telegram показывал её как Voice Message
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
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendVoice"
        )

        with open(voice_path, "rb") as voice:
            response = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption,
                    "parse_mode": "HTML",
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


# -----------------------------
# FASTAPI
# -----------------------------

@app.get("/")
def root():
    return {
        "status": "ok"
    }


# -----------------------------
# STATISTICS
# -----------------------------

@app.get("/stats")
def stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT
                COUNT(*) AS calls,

                COUNT(
                    DISTINCT client_number
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
                        ELSE NULL
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
                             AND answer_time > 0
                             AND start_time > 0
                        THEN answer_time - start_time
                        ELSE NULL
                    END
                ) AS avg_answer_delay

            FROM calls

            WHERE
                date(
                    start_time,
                    'unixepoch',
                    '+5 hours'
                )
                =
                date(
                    'now',
                    '+5 hours'
                )
            """
        ).fetchone()

        clients_row = conn.execute(
            """
            WITH today_clients AS (
                SELECT DISTINCT
                    client_number

                FROM calls

                WHERE
                    date(
                        start_time,
                        'unixepoch',
                        '+5 hours'
                    )
                    =
                    date(
                        'now',
                        '+5 hours'
                    )

                    AND client_number IS NOT NULL
                    AND client_number != ''
            )

            SELECT
                SUM(
                    CASE
                        WHEN NOT EXISTS (
                            SELECT 1

                            FROM calls AS old_calls

                            WHERE
                                old_calls.client_number =
                                today_clients.client_number

                                AND date(
                                    old_calls.start_time,
                                    'unixepoch',
                                    '+5 hours'
                                )
                                <
                                date(
                                    'now',
                                    '+5 hours'
                                )
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS new_clients,

                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1

                            FROM calls AS old_calls

                            WHERE
                                old_calls.client_number =
                                today_clients.client_number

                                AND date(
                                    old_calls.start_time,
                                    'unixepoch',
                                    '+5 hours'
                                )
                                <
                                date(
                                    'now',
                                    '+5 hours'
                                )
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS repeat_clients

            FROM today_clients
            """
        ).fetchone()

        missed_row = conn.execute(
            """
            WITH missed_clients AS (
                SELECT
                    client_number,
                    MIN(start_time) AS first_missed_time

                FROM calls

                WHERE
                    direction = 0
                    AND answered = 0

                    AND date(
                        start_time,
                        'unixepoch',
                        '+5 hours'
                    )
                    =
                    date(
                        'now',
                        '+5 hours'
                    )

                    AND client_number IS NOT NULL
                    AND client_number != ''

                GROUP BY client_number
            )

            SELECT
                COUNT(*) AS unique_missed_clients,

                SUM(
                    CASE
                        WHEN EXISTS (
                            SELECT 1

                            FROM calls AS callback

                            WHERE
                                callback.client_number =
                                missed_clients.client_number

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
                                callback.client_number =
                                missed_clients.client_number

                                AND callback.direction = 1

                                AND callback.start_time >
                                missed_clients.first_missed_time
                        )
                        THEN 1
                        ELSE 0
                    END
                ) AS missed_not_called_back

            FROM missed_clients
            """
        ).fetchone()

    return {
        "today": {
            "calls": row["calls"] or 0,
            "unique_clients": row["unique_clients"] or 0,

            "incoming": row["incoming"] or 0,
            "outgoing": row["outgoing"] or 0,

            "answered": row["answered"] or 0,
            "missed": row["missed"] or 0,

            "new_clients":
                clients_row["new_clients"] or 0,

            "repeat_clients":
                clients_row["repeat_clients"] or 0,

            "unique_missed_clients":
                missed_row["unique_missed_clients"] or 0,

            "missed_called_back":
                missed_row["missed_called_back"] or 0,

            "missed_not_called_back":
                missed_row["missed_not_called_back"] or 0,

            "average_duration_seconds": round(
                row["avg_duration"] or 0
            ),

            "total_duration_seconds":
                row["total_duration"] or 0,

            "average_answer_delay_seconds": round(
                row["avg_answer_delay"] or 0
            ),
        }
    }

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Texnikach Call Dashboard</title>

        <style>
            body {
                margin: 0;
                padding: 30px;
                font-family: Arial, sans-serif;
                background: #111;
                color: #fff;
            }

            h1 {
                margin-bottom: 30px;
            }

            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 16px;
            }

            .card {
                background: #1c1c1c;
                border: 1px solid #333;
                border-radius: 14px;
                padding: 20px;
            }

            .label {
                color: #aaa;
                font-size: 14px;
                margin-bottom: 10px;
            }

            .value {
                font-size: 32px;
                font-weight: bold;
            }
        </style>
    </head>

    <body>
        <h1>Texnikach — статистика звонков</h1>

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
                <div class="label">Пропущенные</div>
                <div class="value" id="missed">—</div>
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
                <div class="label">Не перезвонили</div>
                <div class="value" id="missed_not_called_back">—</div>
            </div>

            <div class="card">
                <div class="label">Средняя длительность</div>
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

        <script>
            function formatTime(seconds) {
                seconds = Number(seconds || 0);

                const minutes = Math.floor(seconds / 60);
                const secs = seconds % 60;

                if (minutes === 0) {
                    return secs + " сек";
                }

                return minutes + " мин " + secs + " сек";
            }

            async function loadStats() {
                const response = await fetch("/stats");
                const data = await response.json();
                const s = data.today;

                document.getElementById("calls").textContent = s.calls;
                document.getElementById("unique_clients").textContent = s.unique_clients;
                document.getElementById("incoming").textContent = s.incoming;
                document.getElementById("outgoing").textContent = s.outgoing;
                document.getElementById("answered").textContent = s.answered;
                document.getElementById("missed").textContent = s.missed;
                document.getElementById("new_clients").textContent = s.new_clients;
                document.getElementById("repeat_clients").textContent = s.repeat_clients;
                document.getElementById("missed_not_called_back").textContent =
                    s.missed_not_called_back;

                document.getElementById("average_duration").textContent =
                    formatTime(s.average_duration_seconds);

                document.getElementById("total_duration").textContent =
                    formatTime(s.total_duration_seconds);

                document.getElementById("average_answer_delay").textContent =
                    formatTime(s.average_answer_delay_seconds);
            }

            loadStats();

            setInterval(loadStats, 30000);
        </script>
    </body>
    </html>
    """

# -----------------------------
# MOIZVONKI WEBHOOK
# -----------------------------

@app.post("/webhooks/moizvonki")
async def moizvonki_webhook(
    request: Request,
):
    data = await request.json()

    print("MOIZVONKI:", data)

    webhook = data.get(
        "webhook",
        {},
    )

    event = data.get(
        "event",
        {},
    )

    # Нас интересует только завершение звонка
    if webhook.get("action") != "call.finish":
        return {
            "ok": True
        }

    # -----------------------------
    # Сохраняем звонок
    # -----------------------------

    save_call(
        webhook,
        event,
    )

    # -----------------------------
    # Получаем данные звонка
    # -----------------------------

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

    duration = int(
        event.get(
            "duration",
            0,
        ) or 0
    )

    answered = int(
        event.get(
            "answered",
            0,
        ) or 0
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

    # -----------------------------
    # Тип звонка
    # -----------------------------

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

    # -----------------------------
    # Telegram сообщение
    # -----------------------------

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

    # -----------------------------
    # Отвеченный звонок + запись
    # -----------------------------

    if answered and recording:
        send_as_voice(
            recording_url=recording,
            caption=text,
        )

    # -----------------------------
    # Пропущенный / записи нет
    # -----------------------------

    else:
        send_text_message(
            text
        )

    return {
        "ok": True
    }