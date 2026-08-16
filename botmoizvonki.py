import os
import tempfile
import subprocess
from pathlib import Path
import sqlite3

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request


app = FastAPI()

a=""""

cd ~/PycharmProjects/botmoizvonki
git add .
git commit -m "обновил бота"
git push

"""
# -----------------------------
# ENV
# -----------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "data" / ".env"

load_dotenv(dotenv_path=ENV_FILE, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print("ENV FILE:", ENV_FILE)
print("ENV EXISTS:", ENV_FILE.exists())
print("TOKEN EXISTS:", bool(TELEGRAM_BOT_TOKEN))
print("CHAT_ID:", TELEGRAM_CHAT_ID)

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


def send_as_voice(recording_url: str, caption: str):
    # Скачиваем запись из "Мои Звонки"
    audio_response = requests.get(
        recording_url,
        timeout=60,
    )
    audio_response.raise_for_status()

    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = os.path.join(
            tmpdir,
            "call_source"
        )

        voice_path = os.path.join(
            tmpdir,
            "call.ogg"
        )

        # Сохраняем оригинальную запись
        with open(source_path, "wb") as f:
            f.write(audio_response.content)

        # Конвертируем в OGG/Opus,
        # чтобы Telegram показал как Voice Message
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
                        "audio/ogg"
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


@app.post("/webhooks/moizvonki")
async def moizvonki_webhook(request: Request):
    data = await request.json()

    print("MOIZVONKI:", data)

    webhook = data.get("webhook", {})
    event = data.get("event", {})

    # Нас интересует только завершение звонка
    if webhook.get("action") != "call.finish":
        return {
            "ok": True
        }

    # Сохраняем завершённый звонок в базу
    save_call(webhook, event)

    direction = event.get("direction")

    client_number = event.get(
        "client_number",
        "Неизвестно"
    )

    client_name = event.get(
        "client_name",
        ""
    )

    duration = int(
        event.get("duration", 0) or 0
    )

    answered = int(
        event.get("answered", 0) or 0
    )

    recording = event.get(
        "recording"
    )

    src_number = event.get(
        "src_number",
        ""
    )

    user_login = webhook.get(
        "user_login",
        ""
    )

    minutes, seconds = divmod(
        duration,
        60
    )

    # -----------------------------
    # Тип звонка
    # -----------------------------

    if not answered and direction == 0:
        call_type = "❌ Пропущенный звонок"

    elif not answered and direction == 1:
        call_type = "⚠️ Неотвеченный исходящий"

    elif direction == 0:
        call_type = "📥 Входящий звонок"

    else:
        call_type = "📤 Исходящий звонок"

    # -----------------------------
    # Текст сообщения
    # -----------------------------

    text = (
        f"<b>{call_type}</b>\n\n"
        f"👤 Клиент: {client_name or '—'}\n"
        f"📱 Номер: <code>{client_number}</code>\n"
        f"👨‍💼 Менеджер: {user_login or '—'}\n"
        f"📲 SIM: {src_number or '—'}\n"
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