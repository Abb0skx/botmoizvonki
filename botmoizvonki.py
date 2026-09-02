import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import requests

from dotenv import load_dotenv

# Load the project environment before importing routers. Some routers construct
# immutable settings objects at import time.
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "data" / ".env"
# Coolify/container environment variables are authoritative.  The optional
# local file may only fill values that are not already present; it must never
# replace production secrets injected by the platform.
load_dotenv(dotenv_path=ENV_FILE, override=False)

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)

from fastapi.responses import HTMLResponse

from instagram_bot import router as instagram_router
from price_server.router import (
    router as price_server_router,
    start_price_server,
    stop_price_server,
)
from reviews.router import router as reviews_router
from telegram_business.router import router as telegram_business_router
from telegram_business.router import get_service as get_telegram_business_service
from telegram_business.router import settings as telegram_business_settings
from telegram_business.scheduler import DurableScheduler


# =========================================================
# APP
# =========================================================

app = FastAPI()

app.include_router(
    instagram_router
)

app.include_router(
    price_server_router
)

app.include_router(
    reviews_router
)

app.include_router(
    telegram_business_router
)

_telegram_business_scheduler = None
_transcription_worker = None


@app.on_event("startup")
async def start_telegram_business():
    global _telegram_business_scheduler
    global _transcription_worker

    await start_price_server()

    if telegram_business_settings.enabled:
        service = get_telegram_business_service()
        _telegram_business_scheduler = DurableScheduler(service)
        await _telegram_business_scheduler.start()

    if TRANSCRIPTION_ENABLED:
        transcription_config_error = (
            get_transcription_config_error()
        )

        if transcription_config_error:
            print(
                "TRANSCRIPTION CONFIG BLOCKED:",
                transcription_config_error,
            )
        else:
            _transcription_worker = TranscriptionWorker()
            await _transcription_worker.start()


@app.on_event("shutdown")
async def stop_telegram_business():
    await stop_price_server()

    if _telegram_business_scheduler:
        await _telegram_business_scheduler.stop()

    if _transcription_worker:
        await _transcription_worker.stop()


# =========================================================
# CONFIG
# =========================================================

UZ_TZ = timezone(
    timedelta(hours=5)
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

MOIZVONKI_WEBHOOK_SECRET = os.getenv(
    "MOIZVONKI_WEBHOOK_SECRET",
    "",
).strip()

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
    "https://bot.texnikach.uz",
).rstrip("/")

SMS_COOLDOWN_DAYS = 30

RESULT_COOLDOWN_HOURS = 30

# Only this Telegram user may set the internal 1–5 manager score.
MANAGER_RATING_ADMIN_ID = 202134293

AUTO_SMS_ENABLED = (
    os.getenv(
        "AUTO_SMS_ENABLED",
        "true",
    ).strip().casefold()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)

RATING_SMS_ENABLED = (
    os.getenv(
        "RATING_SMS_ENABLED",
        "true",
    ).strip().casefold()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)

MISSED_CALL_WORK_START = os.getenv(
    "MISSED_CALL_WORK_START",
    "10:00",
).strip()

MISSED_CALL_WORK_END = os.getenv(
    "MISSED_CALL_WORK_END",
    "20:00",
).strip()

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

AFTER_HOURS_MISSED_SMS_TEXT = """TEXNIKACH
Мы закрыты. {day_ru} работаем с {work_start}.
Доставка по городу бесплатная. Также можно забрать в магазине.

Hozir yopiqmiz. {day_uz} {work_start} dan ishlaymiz.
Shahar bo‘ylab yetkazib berish bepul. Shuningdek, do‘kondan olib ketish mumkin.

Все цены в Telegram / Telegramdagi barcha narxlar:
https://texnikach.uz/go"""


def parse_work_clock(
    value: str,
    default: str,
) -> tuple[int, str]:

    match = re.fullmatch(
        r"(\d{1,2}):(\d{2})",
        str(
            value
            or ""
        ).strip(),
    )

    if match:
        hours = int(
            match.group(1)
        )
        minutes = int(
            match.group(2)
        )

        if (
            0 <= hours <= 23
            and 0 <= minutes <= 59
        ):
            return (
                hours * 60 + minutes,
                f"{hours:02d}:{minutes:02d}",
            )

    print(
        "WORK CLOCK INVALID; USING DEFAULT:",
        value,
    )

    default_hours, default_minutes = (
        int(part)
        for part in default.split(
            ":",
            1,
        )
    )

    return (
        default_hours * 60
        + default_minutes,
        default,
    )


(
    MISSED_CALL_WORK_START_MINUTES,
    MISSED_CALL_WORK_START_LABEL,
) = parse_work_clock(
    MISSED_CALL_WORK_START,
    "10:00",
)

(
    MISSED_CALL_WORK_END_MINUTES,
    MISSED_CALL_WORK_END_LABEL,
) = parse_work_clock(
    MISSED_CALL_WORK_END,
    "20:00",
)

if (
    MISSED_CALL_WORK_START_MINUTES
    >= MISSED_CALL_WORK_END_MINUTES
):
    print(
        "MISSED CALL WORK WINDOW INVALID; "
        "USING 10:00-20:00"
    )
    MISSED_CALL_WORK_START_MINUTES = 10 * 60
    MISSED_CALL_WORK_START_LABEL = "10:00"
    MISSED_CALL_WORK_END_MINUTES = 20 * 60
    MISSED_CALL_WORK_END_LABEL = "20:00"


def env_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:

    raw_value = os.getenv(
        name
    )

    if raw_value is None:
        return default

    try:
        value = int(
            raw_value
        )
    except (
        TypeError,
        ValueError,
    ):
        print(
            "CONFIG VALUE INVALID; USING DEFAULT:",
            name,
        )
        return default

    if not minimum <= value <= maximum:
        print(
            "CONFIG VALUE OUT OF RANGE; USING DEFAULT:",
            name,
        )
        return default

    return value


def env_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:

    raw_value = os.getenv(
        name
    )

    if raw_value is None:
        return default

    try:
        value = float(
            raw_value
        )
    except (
        TypeError,
        ValueError,
    ):
        print(
            "CONFIG VALUE INVALID; USING DEFAULT:",
            name,
        )
        return default

    if not minimum <= value <= maximum:
        print(
            "CONFIG VALUE OUT OF RANGE; USING DEFAULT:",
            name,
        )
        return default

    return value

TRANSCRIPTION_ENABLED = (
    os.getenv(
        "TRANSCRIPTION_ENABLED",
        "false",
    ).strip().casefold()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)

TRANSCRIPTION_API_URL = os.getenv(
    "TRANSCRIPTION_API_URL",
    "https://api.openai.com/v1/audio/transcriptions",
).strip()

TRANSCRIPTION_API_KEY = (
    os.getenv(
        "TRANSCRIPTION_API_KEY",
        "",
    ).strip()
    or os.getenv(
        "OPENAI_API_KEY",
        "",
    ).strip()
)

TRANSCRIPTION_MODEL = os.getenv(
    "TRANSCRIPTION_MODEL",
    "gpt-4o-mini-transcribe",
).strip()

TRANSCRIPTION_TIMEOUT_SECONDS = env_int(
    "TRANSCRIPTION_TIMEOUT_SECONDS",
    180,
    10,
    3600,
)

TRANSCRIPTION_MAX_BYTES = env_int(
    "TRANSCRIPTION_MAX_BYTES",
    25 * 1024 * 1024,
    64 * 1024,
    100 * 1024 * 1024,
)

TRANSCRIPTION_MAX_DURATION_SECONDS = env_int(
    "TRANSCRIPTION_MAX_DURATION_SECONDS",
    1800,
    1,
    4 * 60 * 60,
)

TRANSCRIPTION_MAX_ATTEMPTS = env_int(
    "TRANSCRIPTION_MAX_ATTEMPTS",
    4,
    1,
    20,
)

TRANSCRIPTION_LEASE_SECONDS = env_int(
    "TRANSCRIPTION_LEASE_SECONDS",
    600,
    60,
    24 * 60 * 60,
)

TRANSCRIPTION_POLL_SECONDS = env_float(
    "TRANSCRIPTION_POLL_SECONDS",
    5.0,
    0.25,
    300.0,
)

TRANSCRIPTION_ALLOWED_HOSTS = tuple(
    host.strip().casefold()
    for host in os.getenv(
        "TRANSCRIPTION_ALLOWED_HOSTS",
        ".moizvonki.ru",
    ).split(",")
    if host.strip()
)


def get_transcription_config_error() -> str | None:

    if not TRANSCRIPTION_API_KEY:
        return "TRANSCRIPTION_API_KEY не указан"

    if not TRANSCRIPTION_MODEL:
        return "TRANSCRIPTION_MODEL не указан"

    parsed = urlparse(
        TRANSCRIPTION_API_URL
    )

    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return "TRANSCRIPTION_API_URL должен быть безопасным HTTPS URL"

    if not TRANSCRIPTION_ALLOWED_HOSTS:
        return "TRANSCRIPTION_ALLOWED_HOSTS пуст"

    return None

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

SALES_PHOTO_DB_PATH = Path(
    os.getenv(
        "SALES_PHOTO_DB_PATH",
        "/app/sales-data/sales_photo.db",
    )
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
# DEVICES, SIMS AND LEAD SOURCES
# =========================================================

LEAD_SOURCES = {
    "olx": "OLX",
    "instagram": "Instagram",
    "telegram_channel": "Telegram Kanal",
    "old_client": "Старый клиент",
}

CALL_SOURCE_PROFILES = {
    "texnikach@gmail.com": {
        "device_name": "Poco",
        "default_manager_code": "abbos",
        # Android reports slots from zero in the webhooks received
        # for this account: 0 = SIM 1, 1 = SIM 2.
        "slot_numbers": {
            0: "+998998446162",
            1: "+998901313999",
        },
        "sim_labels": {
            "998998446162": "SIM 1",
            "998901313999": "SIM 2",
        },
        "source_by_number": {
            "998998446162": "olx",
        },
    },
    "texnikacholx@gmail.com": {
        "device_name": "Tecno",
        "default_manager_code": "otabek",
        "slot_numbers": {
            0: "+998908456162",
        },
        "sim_labels": {
            "998908456162": "SIM 1",
        },
        "source_by_number": {
            "998908456162": "olx",
        },
        "source_by_slot": {
            0: "olx",
        },
    },
    "aashshdjdjdjsj@gmail.com": {
        "device_name": "Redmi",
        "default_manager_code": "olmas",
        "slot_numbers": {
            0: "+998908534466",
        },
        "sim_labels": {
            "998908534466": "SIM 1",
        },
        "source_by_number": {
            "998908534466": "olx",
        },
        "source_by_slot": {
            0: "olx",
        },
    },
}


def normalize_user_login(
    value: str | None,
) -> str:

    return str(
        value
        or ""
    ).strip().casefold()


def get_call_source_profile(
    user_login: str | None,
):

    return CALL_SOURCE_PROFILES.get(
        normalize_user_login(
            user_login
        )
    )


def normalize_display_phone(
    value: str | None,
) -> str:

    phone = normalize_phone(
        value
    )

    return (
        "+" + phone
        if phone
        else ""
    )


def resolve_call_device(
    user_login: str | None,
    provider_src_number: str | None,
    src_slot,
):

    profile = get_call_source_profile(
        user_login
    )

    provider_number = normalize_display_phone(
        provider_src_number
    )

    slot = None

    try:
        if src_slot is not None:
            slot = int(
                src_slot
            )
    except (
        TypeError,
        ValueError,
    ):
        slot = None

    resolved_number = provider_number
    mapping_conflict = False
    device_name = (
        profile.get(
            "device_name"
        )
        if profile
        else (
            normalize_user_login(
                user_login
            )
            or "Неизвестно"
        )
    )

    if profile and slot is not None:
        configured_number = (
            profile.get(
                "slot_numbers",
                {},
            ).get(
                slot
            )
        )

        if configured_number:
            provider_key = normalize_phone(
                provider_number
            )

            configured_key = normalize_phone(
                configured_number
            )

            known_numbers = set(
                profile.get(
                    "sim_labels",
                    {},
                )
            )

            mapping_conflict = bool(
                provider_key
                and configured_key
                and provider_key in known_numbers
                and configured_key in known_numbers
                and provider_key != configured_key
            )

            resolved_number = (
                normalize_display_phone(
                    configured_number
                )
            )

    number_key = normalize_phone(
        resolved_number
    )

    sim_label = None

    if profile:
        sim_label = (
            profile.get(
                "sim_labels",
                {},
            ).get(
                number_key
            )
        )

    if not sim_label and slot is not None:
        sim_label = (
            f"SIM {slot + 1}"
        )

    if not sim_label:
        sim_label = "SIM"

    source_code = None
    source_method = None
    source_confidence = None

    if profile:
        source_code = profile.get(
            "source_by_slot",
            {},
        ).get(
            slot
        )

        if not source_code:
            source_code = (
                profile.get(
                    "source_by_number",
                    {},
                ).get(
                    number_key
                )
            )

        if source_code:
            source_method = "sim"
            source_confidence = 0.90
        else:
            source_code = profile.get(
                "default_source_code"
            )

            if source_code:
                source_method = "account"
                source_confidence = 0.85

    return {
        "device_name": device_name,
        "provider_src_number": provider_number,
        "src_number": resolved_number,
        "src_slot": slot,
        "sim_label": sim_label,
        "mapping_conflict": mapping_conflict,
        "default_manager_code": (
            profile.get(
                "default_manager_code"
            )
            if profile
            else None
        ),
        "lead_source_code": source_code,
        "lead_source_method": source_method,
        "lead_source_confidence": source_confidence,
    }


def get_lead_source_name(
    source_code: str | None,
) -> str | None:

    return LEAD_SOURCES.get(
        source_code
    )


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
    "pending":
        "🕓 В работе / ожидает",

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

RESULT_CATEGORIES = {
    "pending": "pending",
    "thinking": "pending",
    "other_product": "pending",
    "credit": "pending",
    "visit_store": "pending",
    "later": "pending",

    "not_target": "non_target",

    "no_stock": "lost",
    "price": "lost",
    "price_changed": "lost",
    "bought_elsewhere": "lost",
    "conditions": "lost",
    "other": "lost",
}


def get_result_category(
    sale_status: str | None,
    reason_code: str | None,
):

    if sale_status == "bought":
        return "bought"

    if sale_status == "not_bought":
        return RESULT_CATEGORIES.get(
            reason_code,
            "lost",
        )

    return None


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


def normalize_sales_manager_code(
    manager_name: str | None,
) -> str:

    candidate = str(
        manager_name
        or ""
    ).strip().casefold()

    for code, name in MANAGERS.items():
        if candidate == name.casefold():
            return code

    return "unmarked"


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


def recompute_client_window_sources(
    conn,
    client_key: str | None = None,
):

    parameters = []
    client_filter = ""

    if client_key:
        client_filter = (
            "WHERE window.client_key = ?"
        )
        parameters.append(
            client_key
        )

    windows = conn.execute(
        f"""
        SELECT window.id
        FROM client_windows AS window
        {client_filter}
        """,
        parameters,
    ).fetchall()

    for window in windows:

        source = conn.execute(
            """
            SELECT
                call.id,
                CASE
                    WHEN call.lead_source_manual_code
                        IS NOT NULL
                        AND call.lead_source_manual_code != ''
                    THEN call.lead_source_manual_code
                    ELSE call.lead_source_auto_code
                END AS source_code,
                CASE
                    WHEN call.lead_source_manual_code
                        IS NOT NULL
                        AND call.lead_source_manual_code != ''
                    THEN 'manual'
                    ELSE call.lead_source_auto_method
                END AS source_origin,
                CASE
                    WHEN call.lead_source_manual_code
                        IS NOT NULL
                        AND call.lead_source_manual_code != ''
                    THEN 1.0
                    ELSE call.lead_source_auto_confidence
                END AS source_confidence,
                CASE
                    WHEN call.lead_source_manual_code
                        IS NOT NULL
                        AND call.lead_source_manual_code != ''
                    THEN
                        'Ручной выбор'
                        || CASE
                            WHEN NULLIF(
                                call.lead_source_marked_username,
                                ''
                            ) IS NOT NULL
                            THEN
                                ': '
                                || call.lead_source_marked_username
                            ELSE ''
                        END
                    ELSE call.lead_source_auto_evidence
                END AS source_evidence,
                COALESCE(
                    call.lead_source_revision,
                    0
                ) AS source_revision,
                call.start_time

            FROM reporting_calls AS call

            WHERE
                call.client_window_id = ?
                AND (
                    NULLIF(
                        call.lead_source_manual_code,
                        ''
                    ) IS NOT NULL
                    OR NULLIF(
                        call.lead_source_auto_code,
                        ''
                    ) IS NOT NULL
                )

            ORDER BY
                CASE
                    WHEN NULLIF(
                        call.lead_source_manual_code,
                        ''
                    ) IS NOT NULL
                    THEN 3
                    WHEN call.lead_source_auto_method =
                        'transcript'
                    THEN 2
                    ELSE 1
                END DESC,
                CASE
                    WHEN NULLIF(
                        call.lead_source_manual_code,
                        ''
                    ) IS NOT NULL
                    THEN COALESCE(
                        call.lead_source_revision,
                        0
                    )
                    ELSE COALESCE(
                        call.lead_source_auto_confidence,
                        0
                    )
                END DESC,
                CASE
                    WHEN NULLIF(
                        call.lead_source_manual_code,
                        ''
                    ) IS NULL
                    THEN COALESCE(
                        call.start_time,
                        0
                    )
                END ASC,
                call.id ASC

            LIMIT 1
            """,
            (
                window["id"],
            ),
        ).fetchone()

        conn.execute(
            """
            UPDATE client_windows

            SET
                lead_source_code = ?,
                lead_source_origin = ?,
                lead_source_call_id = ?,
                lead_source_confidence = ?,
                lead_source_evidence = ?,
                lead_source_revision = ?,
                updated_at = CURRENT_TIMESTAMP

            WHERE id = ?
            """,
            (
                (
                    source["source_code"]
                    if source
                    else None
                ),
                (
                    source["source_origin"]
                    if source
                    else None
                ),
                (
                    source["id"]
                    if source
                    else None
                ),
                (
                    source["source_confidence"]
                    if source
                    else None
                ),
                (
                    source["source_evidence"]
                    if source
                    else None
                ),
                (
                    source["source_revision"]
                    if source
                    else None
                ),
                window["id"],
            ),
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

                client_window_id INTEGER,
                webhook_dedup_key TEXT,
                duplicate_of_call_id INTEGER,

                is_internal_contact INTEGER
                    DEFAULT 0,

                internal_contact_name TEXT,

                direction INTEGER,
                answered INTEGER,

                user_id INTEGER,
                user_login TEXT,

                device_name TEXT,
                provider_src_number TEXT,
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

                telegram_reserved_at INTEGER,

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

                lead_source_auto_code TEXT,
                lead_source_auto_method TEXT,
                lead_source_auto_confidence REAL,
                lead_source_auto_evidence TEXT,

                lead_source_manual_code TEXT,
                lead_source_revision INTEGER,
                lead_source_marked_at INTEGER,
                lead_source_marked_by INTEGER,
                lead_source_marked_username TEXT,

                sale_status TEXT,

                no_sale_reason TEXT,
                no_sale_reason_code TEXT,

                sale_marked_at INTEGER,
                result_revision INTEGER,
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

        result_revision_was_missing = (
            "result_revision"
            not in columns
        )

        migrations = {

            "client_key":
                """
                ALTER TABLE calls
                ADD COLUMN client_key TEXT
                """,

            "client_window_id":
                """
                ALTER TABLE calls
                ADD COLUMN client_window_id INTEGER
                """,

            "webhook_dedup_key":
                """
                ALTER TABLE calls
                ADD COLUMN webhook_dedup_key TEXT
                """,

            "duplicate_of_call_id":
                """
                ALTER TABLE calls
                ADD COLUMN duplicate_of_call_id INTEGER
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

            "device_name":
                """
                ALTER TABLE calls
                ADD COLUMN device_name TEXT
                """,

            "provider_src_number":
                """
                ALTER TABLE calls
                ADD COLUMN provider_src_number TEXT
                """,

            "lead_source_auto_code":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_auto_code TEXT
                """,

            "lead_source_auto_method":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_auto_method TEXT
                """,

            "lead_source_auto_confidence":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_auto_confidence REAL
                """,

            "lead_source_auto_evidence":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_auto_evidence TEXT
                """,

            "lead_source_manual_code":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_manual_code TEXT
                """,

            "lead_source_revision":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_revision INTEGER
                """,

            "lead_source_marked_at":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_marked_at INTEGER
                """,

            "lead_source_marked_by":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_marked_by INTEGER
                """,

            "lead_source_marked_username":
                """
                ALTER TABLE calls
                ADD COLUMN lead_source_marked_username TEXT
                """,

            "telegram_sent":
                """
                ALTER TABLE calls
                ADD COLUMN telegram_sent INTEGER
                DEFAULT 0
                """,

            "telegram_reserved_at":
                """
                ALTER TABLE calls
                ADD COLUMN telegram_reserved_at INTEGER
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

            "result_revision":
                """
                ALTER TABLE calls
                ADD COLUMN result_revision INTEGER
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
            CREATE TABLE IF NOT EXISTS
                device_manager_assignments (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                user_login TEXT
                    NOT NULL,

                device_name TEXT
                    NOT NULL,

                manager_code TEXT
                    NOT NULL,

                manager_name TEXT
                    NOT NULL,

                effective_from INTEGER
                    NOT NULL,

                effective_until INTEGER
                    NOT NULL,

                changed_at INTEGER
                    NOT NULL,

                changed_by TEXT,

                ended_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                CHECK (
                    effective_until
                    >= effective_from
                )
            )
            """
        )

        device_assignment_columns = {
            row["name"]
            for row in conn.execute(
                """
                PRAGMA table_info(
                    device_manager_assignments
                )
                """
            ).fetchall()
        }

        if (
            "ended_by"
            not in device_assignment_columns
        ):
            conn.execute(
                """
                ALTER TABLE
                    device_manager_assignments
                ADD COLUMN ended_by TEXT
                """
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
                device_manager_defaults (

                user_login TEXT
                    PRIMARY KEY,

                device_name TEXT
                    NOT NULL,

                manager_code TEXT
                    NOT NULL,

                manager_name TEXT
                    NOT NULL,

                updated_at INTEGER
                    NOT NULL,

                updated_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
                device_manager_default_history (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                user_login TEXT
                    NOT NULL,

                device_name TEXT
                    NOT NULL,

                old_manager_code TEXT,
                old_manager_name TEXT,

                new_manager_code TEXT,
                new_manager_name TEXT,

                action TEXT
                    NOT NULL
                    CHECK (
                        action IN (
                            'set',
                            'reset'
                        )
                    ),

                changed_at INTEGER
                    NOT NULL,

                changed_by TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
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

                message_kind TEXT
                    NOT NULL
                    DEFAULT 'promo',

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

        if (
            "message_kind"
            not in sms_history_columns
        ):
            conn.execute(
                """
                ALTER TABLE sms_history
                ADD COLUMN message_kind TEXT
                NOT NULL DEFAULT 'promo'
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

                client_window_id INTEGER,

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

            "client_window_id":
                """
                ALTER TABLE call_ratings
                ADD COLUMN client_window_id INTEGER
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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_windows (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                client_key TEXT
                    NOT NULL,

                started_at INTEGER
                    NOT NULL,

                ends_at INTEGER
                    NOT NULL,

                first_call_id INTEGER
                    NOT NULL,

                latest_call_id INTEGER
                    NOT NULL,

                lead_source_code TEXT,
                lead_source_origin TEXT,
                lead_source_call_id INTEGER,
                lead_source_confidence REAL,
                lead_source_evidence TEXT,
                lead_source_revision INTEGER,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(
                    client_key,
                    started_at
                ),

                FOREIGN KEY(first_call_id)
                    REFERENCES calls(id),

                FOREIGN KEY(latest_call_id)
                    REFERENCES calls(id)
            )
            """
        )

        client_window_columns = {
            row["name"]
            for row in conn.execute(
                """
                PRAGMA table_info(client_windows)
                """
            ).fetchall()
        }

        client_window_migrations = {
            "lead_source_code":
                """
                ALTER TABLE client_windows
                ADD COLUMN lead_source_code TEXT
                """,
            "lead_source_origin":
                """
                ALTER TABLE client_windows
                ADD COLUMN lead_source_origin TEXT
                """,
            "lead_source_call_id":
                """
                ALTER TABLE client_windows
                ADD COLUMN lead_source_call_id INTEGER
                """,
            "lead_source_confidence":
                """
                ALTER TABLE client_windows
                ADD COLUMN lead_source_confidence REAL
                """,
            "lead_source_evidence":
                """
                ALTER TABLE client_windows
                ADD COLUMN lead_source_evidence TEXT
                """,
            "lead_source_revision":
                """
                ALTER TABLE client_windows
                ADD COLUMN lead_source_revision INTEGER
                """,
        }

        for column, sql in (
            client_window_migrations.items()
        ):
            if column not in client_window_columns:
                conn.execute(
                    sql
                )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_results (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                client_key TEXT
                    NOT NULL,

                client_window_id INTEGER,

                window_started_at INTEGER
                    NOT NULL,

                window_ends_at INTEGER
                    NOT NULL,

                source_call_id INTEGER
                    NOT NULL,

                attribution_time INTEGER
                    NOT NULL,

                sale_status TEXT
                    NOT NULL
                    CHECK (
                        sale_status IN (
                            'bought',
                            'not_bought'
                        )
                    ),

                result_category TEXT,

                no_sale_reason TEXT,
                no_sale_reason_code TEXT,

                talk_manager_code TEXT,
                talk_manager_name TEXT,

                marked_at INTEGER
                    NOT NULL,

                marked_by INTEGER,
                marked_username TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(
                    client_key,
                    window_started_at
                ),

                FOREIGN KEY(source_call_id)
                    REFERENCES calls(id),

                FOREIGN KEY(client_window_id)
                    REFERENCES client_windows(id)
            )
            """
        )

        client_result_columns = {
            row["name"]
            for row in conn.execute(
                """
                PRAGMA table_info(client_results)
                """
            ).fetchall()
        }

        if (
            "client_window_id"
            not in client_result_columns
        ):
            conn.execute(
                """
                ALTER TABLE client_results
                ADD COLUMN client_window_id INTEGER
                """
            )

        if (
            "result_category"
            not in client_result_columns
        ):
            conn.execute(
                """
                ALTER TABLE client_results
                ADD COLUMN result_category TEXT
                """
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_ratings (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                client_key TEXT
                    NOT NULL,

                client_window_id INTEGER
                    NOT NULL UNIQUE,

                source_call_id INTEGER
                    NOT NULL,

                attribution_time INTEGER
                    NOT NULL,

                talk_manager_code TEXT
                    NOT NULL,

                talk_manager_name TEXT
                    NOT NULL,

                score INTEGER
                    NOT NULL
                    CHECK (score BETWEEN 1 AND 5),

                marked_at INTEGER
                    NOT NULL,

                marked_by INTEGER
                    NOT NULL,

                marked_username TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(source_call_id)
                    REFERENCES calls(id),

                FOREIGN KEY(client_window_id)
                    REFERENCES client_windows(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS call_transcriptions (

                call_id INTEGER
                    PRIMARY KEY,

                status TEXT
                    NOT NULL,

                attempts INTEGER
                    NOT NULL
                    DEFAULT 0,

                next_attempt_at INTEGER,
                lease_token TEXT,
                lease_until INTEGER,

                provider TEXT,
                model TEXT,
                language TEXT,
                transcript_text TEXT,
                audio_sha256 TEXT,

                error TEXT,

                lead_source_code TEXT,
                lead_source_confidence REAL,
                lead_source_evidence TEXT,
                lead_source_candidates_json TEXT,
                classifier_version TEXT,

                queued_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                updated_at INTEGER NOT NULL,

                FOREIGN KEY(call_id)
                    REFERENCES calls(id)
            )
            """
        )

        # `sale_marked_at` has only one-second precision.  Give
        # legacy result clicks a stable monotonic order so a rebuild
        # cannot change the last selected result when two clicks
        # happened during the same second.  The current canonical
        # client_result wins a timestamp tie.
        if result_revision_was_missing:

            revision_rows = conn.execute(
                """
                SELECT
                    call.id,
                    COALESCE(
                        call.sale_marked_at,
                        call.start_time,
                        call.id
                    ) AS ordering_time,
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM client_results AS result
                            WHERE result.source_call_id = call.id
                        )
                        THEN 1
                        ELSE 0
                    END AS is_current_result

                FROM calls AS call

                WHERE call.sale_status IN (
                    'bought',
                    'not_bought'
                )

                ORDER BY
                    ordering_time,
                    is_current_result,
                    call.id
                """
            ).fetchall()

            for revision, revision_row in enumerate(
                revision_rows,
                start=1,
            ):

                conn.execute(
                    """
                    UPDATE calls
                    SET result_revision = ?
                    WHERE id = ?
                    """,
                    (
                        revision,
                        revision_row["id"],
                    ),
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
        # NORMALIZE KNOWN DEVICES / SIMS AND SAFE DEFAULTS
        # -------------------------------------------------

        profile_rows = conn.execute(
            """
            SELECT
                id,
                user_login,
                provider_src_number,
                src_number,
                src_slot,
                is_internal_contact,
                talk_manager_code,
                lead_source_auto_code,
                lead_source_auto_method

            FROM calls
            """
        ).fetchall()

        for row in profile_rows:

            provider_src_number = (
                row["provider_src_number"]
                or row["src_number"]
            )

            device = resolve_call_device(
                row["user_login"],
                provider_src_number,
                row["src_slot"],
            )

            conn.execute(
                """
                UPDATE calls

                SET
                    provider_src_number = ?,
                    src_number = ?,
                    device_name = ?

                WHERE id = ?
                """,
                (
                    provider_src_number,
                    (
                        device["src_number"]
                        or row["src_number"]
                    ),
                    device["device_name"],
                    row["id"],
                ),
            )

            default_manager_code = device[
                "default_manager_code"
            ]

            if (
                default_manager_code
                and not row["is_internal_contact"]
                and not row["talk_manager_code"]
            ):

                manager_name = MANAGERS.get(
                    default_manager_code
                )

                if manager_name:
                    conn.execute(
                        """
                        UPDATE calls

                        SET
                            talk_manager_code = ?,
                            talk_manager_name = ?,
                            manager_marked_at = COALESCE(
                                manager_marked_at,
                                start_time
                            ),
                            manager_marked_username =
                                COALESCE(
                                    manager_marked_username,
                                    ?
                                )

                        WHERE
                            id = ?
                            AND COALESCE(
                                talk_manager_code,
                                ''
                            ) = ''
                        """,
                        (
                            default_manager_code,
                            manager_name,
                            (
                                "Авто: "
                                + normalize_user_login(
                                    row["user_login"]
                                )
                            ),
                            row["id"],
                        ),
                    )

            default_source_code = device[
                "lead_source_code"
            ]

            static_source_method = (
                row["lead_source_auto_method"]
                in {
                    "sim",
                    "account",
                }
            )

            if (
                not row["is_internal_contact"]
                and (
                    default_source_code
                    or static_source_method
                )
                and (
                    static_source_method
                    or (
                        not row["lead_source_auto_code"]
                        and not row[
                            "lead_source_auto_method"
                        ]
                    )
                )
            ):
                conn.execute(
                    """
                    UPDATE calls

                    SET
                        lead_source_auto_code = ?,
                        lead_source_auto_method = ?,
                        lead_source_auto_confidence = ?,
                        lead_source_auto_evidence = ?

                    WHERE id = ?
                    """,
                    (
                        default_source_code,
                        device[
                            "lead_source_method"
                        ],
                        device[
                            "lead_source_confidence"
                        ],
                        (
                            "Авто по SIM/аккаунту: "
                            + normalize_user_login(
                                row["user_login"]
                            )
                            if default_source_code
                            else None
                        ),
                        row["id"],
                    ),
                )

        conn.execute(
            """
            UPDATE client_results

            SET
                talk_manager_code = (
                    SELECT call.talk_manager_code
                    FROM calls AS call
                    WHERE call.id =
                        client_results.source_call_id
                ),
                talk_manager_name = (
                    SELECT call.talk_manager_name
                    FROM calls AS call
                    WHERE call.id =
                        client_results.source_call_id
                ),
                updated_at = CURRENT_TIMESTAMP

            WHERE
                COALESCE(
                    talk_manager_code,
                    ''
                ) = ''
                AND EXISTS (
                    SELECT 1
                    FROM calls AS call
                    WHERE
                        call.id =
                            client_results.source_call_id
                        AND COALESCE(
                            call.talk_manager_code,
                            ''
                        ) != ''
                )
            """
        )

        # -------------------------------------------------
        # ROLLING 30-HOUR CLIENT WINDOWS FROM CALL STARTS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )

        # Preserve old raw rows, but canonicalize webhook duplicates
        # created by older versions (for example PBX-only -> DB+PBX).
        # Only the canonical row participates in windows and reports.
        duplicate_migration_done = bool(
            conn.execute(
                """
                SELECT 1
                FROM schema_migrations
                WHERE name =
                    'canonical_webhook_duplicates_v1'
                LIMIT 1
                """
            ).fetchone()
        )

        if not duplicate_migration_done:

            duplicate_rows = conn.execute(
                """
                SELECT
                    call.*,

                    EXISTS (
                        SELECT 1
                        FROM call_ratings AS rating
                        WHERE
                            rating.call_id = call.id
                            AND rating.score IS NOT NULL
                    ) AS has_score,

                    EXISTS (
                        SELECT 1
                        FROM call_ratings AS rating
                        WHERE rating.call_id = call.id
                    ) AS has_rating,

                    EXISTS (
                        SELECT 1
                        FROM client_results AS result
                        WHERE result.source_call_id = call.id
                    ) AS has_result

                FROM calls AS call

                ORDER BY call.id
                """
            ).fetchall()

            parents = {
                row["id"]: row["id"]
                for row in duplicate_rows
            }

            root_db_ids = {
                row["id"]: (
                    {
                        str(row["db_call_id"])
                    }
                    if row["db_call_id"]
                        is not None
                    else set()
                )
                for row in duplicate_rows
            }

            def find_parent(call_id):
                while parents[call_id] != call_id:
                    parents[call_id] = parents[
                        parents[call_id]
                    ]
                    call_id = parents[call_id]
                return call_id

            def union_calls(left_id, right_id):
                left_root = find_parent(left_id)
                right_root = find_parent(right_id)

                if (
                    root_db_ids[left_root]
                    and root_db_ids[right_root]
                    and root_db_ids[left_root]
                        != root_db_ids[right_root]
                ):
                    return

                if left_root != right_root:
                    parents[right_root] = left_root
                    root_db_ids[left_root].update(
                        root_db_ids[right_root]
                    )

            alias_owner = {}

            for duplicate_row in duplicate_rows:

                identity_aliases = []

                if duplicate_row[
                    "event_pbx_call_id"
                ]:
                    identity_aliases.append(
                        "pbx:"
                        + str(
                            duplicate_row[
                                "account_id"
                            ]
                            or ""
                        )
                        + ":"
                        + str(
                            duplicate_row[
                                "event_pbx_call_id"
                            ]
                        )
                    )

                duplicate_src_key = (
                    normalize_phone(
                        duplicate_row[
                            "src_number"
                        ]
                    )
                )

                duplicate_start_time = int(
                    duplicate_row["start_time"]
                    or 0
                )

                if (
                    duplicate_start_time > 0
                    and bool(
                        duplicate_row["client_key"]
                        or duplicate_src_key
                    )
                    and duplicate_row["direction"]
                        is not None
                ):
                    identity_payload = json.dumps(
                        {
                            "account_id": str(
                                duplicate_row[
                                    "account_id"
                                ]
                                or ""
                            ),
                            "client_key": (
                                duplicate_row[
                                    "client_key"
                                ]
                                or ""
                            ),
                            "src_key": duplicate_src_key,
                            "direction": duplicate_row[
                                "direction"
                            ],
                            "start_time": (
                                duplicate_start_time
                            ),
                            "answer_time": duplicate_row[
                                "answer_time"
                            ],
                            "end_time": duplicate_row[
                                "end_time"
                            ],
                            "src_id": duplicate_row[
                                "src_id"
                            ],
                            "src_slot": duplicate_row[
                                "src_slot"
                            ],
                            "event_created": duplicate_row[
                                "event_created"
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )

                    identity_aliases.append(
                        "fp:"
                        + hashlib.sha256(
                            identity_payload.encode(
                                "utf-8"
                            )
                        ).hexdigest()
                    )

                for identity_alias in identity_aliases:

                    owner_id = alias_owner.get(
                        identity_alias
                    )

                    if owner_id is None:
                        alias_owner[
                            identity_alias
                        ] = duplicate_row["id"]
                    else:
                        union_calls(
                            owner_id,
                            duplicate_row["id"],
                        )

            duplicate_groups = {}

            for duplicate_row in duplicate_rows:
                duplicate_groups.setdefault(
                    find_parent(
                        duplicate_row["id"]
                    ),
                    [],
                ).append(duplicate_row)

            def canonical_rank(row):
                completeness = sum(
                    1
                    for column in (
                        "client_number",
                        "start_time",
                        "end_time",
                        "answered",
                        "src_number",
                        "recording",
                    )
                    if row[column] not in (
                        None,
                        "",
                    )
                )

                return (
                    int(row["has_score"] or 0),
                    int(row["has_rating"] or 0),
                    int(row["has_result"] or 0),
                    int(
                        row["sale_status"]
                        is not None
                    ),
                    int(row["telegram_sent"] or 0),
                    int(row["sms_sent"] or 0),
                    completeness,
                    int(
                        row["db_call_id"]
                        is not None
                    ),
                    -int(row["id"]),
                )

            conn.execute(
                """
                UPDATE calls
                SET duplicate_of_call_id = NULL
                """
            )

            for group_rows in duplicate_groups.values():

                if len(group_rows) < 2:
                    continue

                canonical = max(
                    group_rows,
                    key=canonical_rank,
                )

                for duplicate_row in group_rows:

                    if duplicate_row["id"] == canonical["id"]:
                        continue

                    conn.execute(
                        """
                        UPDATE calls
                        SET duplicate_of_call_id = ?
                        WHERE id = ?
                        """,
                        (
                            canonical["id"],
                            duplicate_row["id"],
                        ),
                    )

            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (
                    name,
                    applied_at
                )
                VALUES (?, ?)
                """,
                (
                    "canonical_webhook_duplicates_v1",
                    int(
                        datetime.now(
                            timezone.utc
                        ).timestamp()
                    ),
                ),
            )

        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS reporting_calls AS
            SELECT *
            FROM calls
            WHERE duplicate_of_call_id IS NULL
            """
        )

        windows_migration_done = bool(
            conn.execute(
                """
                SELECT 1

                FROM schema_migrations

                WHERE name =
                    'client_windows_rolling_30h_v4'

                LIMIT 1
                """
            ).fetchone()
        )

        if not windows_migration_done:

            conn.execute(
                "DELETE FROM client_results"
            )

            conn.execute(
                "UPDATE call_ratings SET client_window_id = NULL"
            )

            conn.execute(
                "UPDATE calls SET client_window_id = NULL"
            )

            conn.execute(
                "DELETE FROM client_windows"
            )

        cooldown_seconds = (
            RESULT_COOLDOWN_HOURS
            * 60
            * 60
        )

        window_calls = (
            conn.execute(
            """
            SELECT
                id,
                client_key,
                start_time

            FROM reporting_calls

            WHERE
                COALESCE(
                    is_internal_contact,
                    0
                ) = 0

                AND

                client_key IS NOT NULL

                AND

                client_key != ''

                AND

                start_time IS NOT NULL

                AND

                start_time > 0

            ORDER BY
                client_key,
                start_time,
                id
            """
            ).fetchall()
            if not windows_migration_done
            else []
        )

        active_windows = {}

        for call in window_calls:

            client_key = call[
                "client_key"
            ]

            start_time = int(
                call["start_time"]
            )

            active = active_windows.get(
                client_key
            )

            if (
                not active
                or start_time
                >= active["last_start_time"]
                + cooldown_seconds
            ):

                cursor = conn.execute(
                    """
                    INSERT INTO client_windows (
                        client_key,
                        started_at,
                        ends_at,
                        first_call_id,
                        latest_call_id
                    )

                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        client_key,
                        start_time,
                        start_time
                        + cooldown_seconds,
                        call["id"],
                        call["id"],
                    ),
                )

                active = {
                    "id": cursor.lastrowid,
                    "last_start_time": start_time,
                }

                active_windows[client_key] = (
                    active
                )

            else:

                conn.execute(
                    """
                    UPDATE client_windows

                    SET
                        latest_call_id = ?,
                        ends_at = ?,
                        updated_at = CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        call["id"],
                        start_time
                        + cooldown_seconds,
                        active["id"],
                    ),
                )

                active[
                    "last_start_time"
                ] = start_time

            conn.execute(
                """
                UPDATE calls

                SET client_window_id = ?

                WHERE id = ?
                """,
                (
                    active["id"],
                    call["id"],
                ),
            )

        conn.execute(
            """
            UPDATE call_ratings

            SET client_window_id = (
                SELECT calls.client_window_id

                FROM calls

                WHERE calls.id =
                    call_ratings.call_id
            )
            """
        )

        legacy_results = (
            conn.execute(
            """
            SELECT
                call.id,
                call.client_key,
                call.client_window_id,
                window.started_at,
                window.ends_at,
                call.start_time,
                call.sale_status,
                call.no_sale_reason,
                call.no_sale_reason_code,
                call.talk_manager_code,
                call.talk_manager_name,
                call.sale_marked_at,
                call.result_revision,
                call.sale_marked_by,
                call.sale_marked_username

            FROM reporting_calls AS call

            JOIN client_windows AS window
                ON window.id =
                    call.client_window_id

            WHERE
                call.sale_status IN (
                    'bought',
                    'not_bought'
                )

            ORDER BY
                call.client_window_id,
                COALESCE(
                    call.result_revision,
                    0
                ),
                COALESCE(
                    call.sale_marked_at,
                    call.start_time,
                    call.id
                ),
                call.id
            """
            ).fetchall()
            if not windows_migration_done
            else []
        )

        latest_results = {}

        for result in legacy_results:
            latest_results[
                result["client_window_id"]
            ] = result

        for result in latest_results.values():

            marked_at = int(
                result["sale_marked_at"]
                or result["started_at"]
            )

            conn.execute(
                """
                INSERT INTO client_results (
                    client_key,
                    client_window_id,
                    window_started_at,
                    window_ends_at,
                    source_call_id,
                    attribution_time,
                    sale_status,
                    result_category,
                    no_sale_reason,
                    no_sale_reason_code,
                    talk_manager_code,
                    talk_manager_name,
                    marked_at,
                    marked_by,
                    marked_username
                )

                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    result["client_key"],
                    result["client_window_id"],
                    result["started_at"],
                    result["ends_at"],
                    result["id"],
                    result["start_time"]
                    or result["started_at"],
                    result["sale_status"],
                    get_result_category(
                        result["sale_status"],
                        result[
                            "no_sale_reason_code"
                        ],
                    ),
                    result["no_sale_reason"],
                    result["no_sale_reason_code"],
                    result["talk_manager_code"],
                    result["talk_manager_name"],
                    marked_at,
                    result["sale_marked_by"],
                    result[
                        "sale_marked_username"
                    ],
                ),
            )

        if not windows_migration_done:
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (
                    name,
                    applied_at
                )

                VALUES (?, ?)
                """,
                (
                    "client_windows_rolling_30h_v4",
                    int(
                        datetime.now(
                            timezone.utc
                        ).timestamp()
                    ),
                ),
            )

        conn.execute(
            """
            UPDATE client_results

            SET result_category = CASE
                WHEN sale_status = 'bought'
                THEN 'bought'

                WHEN no_sale_reason_code IN (
                    'pending',
                    'thinking',
                    'other_product',
                    'credit',
                    'visit_store',
                    'later'
                )
                THEN 'pending'

                WHEN no_sale_reason_code =
                    'not_target'
                THEN 'non_target'

                ELSE 'lost'
            END

            WHERE result_category IS NULL
            """
        )

        recompute_client_window_sources(
            conn
        )

        # -------------------------------------------------
        # WEBHOOK DEDUP KEYS FOR OLD CALLS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS call_webhook_keys (
                dedup_key TEXT PRIMARY KEY,
                call_id INTEGER NOT NULL,
                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(call_id)
                    REFERENCES calls(id)
            )
            """
        )

        dedup_rows = conn.execute(
            """
            SELECT
                id,
                webhook_dedup_key,
                duplicate_of_call_id,
                db_call_id,
                event_pbx_call_id,
                account_id,
                client_key,
                src_number,
                start_time,
                answer_time,
                end_time,
                direction,
                src_id,
                src_slot,
                event_created

            FROM calls

            ORDER BY id
            """
        ).fetchall()

        seen_dedup_keys = set()

        for dedup_row in dedup_rows:

            dedup_keys = []

            canonical_call_id = (
                dedup_row[
                    "duplicate_of_call_id"
                ]
                or dedup_row["id"]
            )

            legacy_src_key = normalize_phone(
                dedup_row["src_number"]
            )

            legacy_start_time = int(
                dedup_row["start_time"]
                or 0
            )

            legacy_fingerprint_is_strong = (
                legacy_start_time > 0
                and bool(
                    dedup_row["client_key"]
                    or legacy_src_key
                )
                and dedup_row["direction"]
                    is not None
            )

            if (
                dedup_row["webhook_dedup_key"]
                and (
                    not str(
                        dedup_row[
                            "webhook_dedup_key"
                        ]
                    ).startswith("fp:")
                    or legacy_fingerprint_is_strong
                )
            ):
                dedup_keys.append(
                    dedup_row[
                        "webhook_dedup_key"
                    ]
                )

            if dedup_row["db_call_id"] is not None:
                dedup_keys.append(
                    "db:"
                    + str(
                        dedup_row["db_call_id"]
                    )
                )

            if dedup_row["event_pbx_call_id"]:
                dedup_keys.append(
                    "pbx:"
                    + str(
                        dedup_row["account_id"]
                        or ""
                    )
                    + ":"
                    + str(
                        dedup_row[
                            "event_pbx_call_id"
                        ]
                    )
                )

            if legacy_fingerprint_is_strong:

                fingerprint = json.dumps(
                    {
                        "account_id": str(
                            dedup_row["account_id"]
                            or ""
                        ),
                        "client_key": (
                            dedup_row["client_key"]
                            or ""
                        ),
                        "src_key": legacy_src_key,
                        "direction": dedup_row[
                            "direction"
                        ],
                        "start_time": legacy_start_time,
                        "answer_time": dedup_row[
                            "answer_time"
                        ],
                        "end_time": dedup_row[
                            "end_time"
                        ],
                        "src_id": dedup_row[
                            "src_id"
                        ],
                        "src_slot": dedup_row[
                            "src_slot"
                        ],
                        "event_created": dedup_row[
                            "event_created"
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )

                dedup_keys.append(
                    "fp:"
                    + hashlib.sha256(
                        fingerprint.encode(
                            "utf-8"
                        )
                    ).hexdigest()
                )

            if not dedup_keys:
                dedup_keys.append(
                    "legacy:"
                    + str(dedup_row["id"])
                )

            dedup_keys = list(
                dict.fromkeys(
                    dedup_keys
                )
            )

            for dedup_key in dedup_keys:

                conn.execute(
                    """
                    INSERT OR IGNORE INTO
                        call_webhook_keys (
                            dedup_key,
                            call_id
                        )
                    VALUES (?, ?)
                    """,
                    (
                        dedup_key,
                        canonical_call_id,
                    ),
                )

                if dedup_row[
                    "duplicate_of_call_id"
                ] is not None:

                    conn.execute(
                        """
                        UPDATE call_webhook_keys
                        SET call_id = ?
                        WHERE dedup_key = ?
                        """,
                        (
                            canonical_call_id,
                            dedup_key,
                        ),
                    )

            primary_key = (
                next(
                    (
                        key
                        for key in dedup_keys
                        if key.startswith("db:")
                    ),
                    None,
                )
                or next(
                    (
                        key
                        for key in dedup_keys
                        if key.startswith("pbx:")
                    ),
                    None,
                )
                or dedup_keys[-1]
            )

            if (
                dedup_row[
                    "duplicate_of_call_id"
                ] is None
                and primary_key
                    not in seen_dedup_keys
            ):

                seen_dedup_keys.add(
                    primary_key
                )

                conn.execute(
                    """
                    UPDATE calls
                    SET webhook_dedup_key = ?
                    WHERE id = ?
                    """,
                    (
                        primary_key,
                        dedup_row["id"],
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
            idx_device_manager_default_history

            ON device_manager_default_history(
                user_login,
                changed_at
            )
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
            idx_calls_client_window

            ON calls(client_window_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_duplicate_of

            ON calls(duplicate_of_call_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_call_webhook_keys_call_id

            ON call_webhook_keys(call_id)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_calls_webhook_dedup_key

            ON calls(webhook_dedup_key)

            WHERE
                webhook_dedup_key IS NOT NULL
                AND webhook_dedup_key != ''
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
            idx_device_manager_assignments_lookup

            ON device_manager_assignments(
                user_login,
                effective_from,
                effective_until
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_manager_ratings_time_manager

            ON manager_ratings(
                attribution_time,
                talk_manager_code
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_lead_source_manual

            ON calls(lead_source_manual_code)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_calls_lead_source_auto

            ON calls(lead_source_auto_code)
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

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_call_ratings_client_window

            ON call_ratings(client_window_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_client_windows_period

            ON client_windows(started_at)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_client_windows_client_time

            ON client_windows(
                client_key,
                started_at,
                ends_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_client_windows_lead_source

            ON client_windows(
                lead_source_code,
                started_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_call_transcriptions_queue

            ON call_transcriptions(
                status,
                next_attempt_at,
                lease_until
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_call_transcriptions_source

            ON call_transcriptions(
                lead_source_code,
                completed_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_client_results_client_window

            ON client_results(
                client_key,
                window_ends_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_client_results_attribution

            ON client_results(
                attribution_time,
                sale_status
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_client_results_source_call

            ON client_results(source_call_id)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_client_results_window_unique

            ON client_results(client_window_id)

            WHERE client_window_id IS NOT NULL
            """
        )

        conn.commit()


init_db()


# =========================================================
# DEVICE MANAGER ASSIGNMENTS
# =========================================================

def next_uz_midnight_timestamp(
    now_ts: int,
) -> int:

    local_now = datetime.fromtimestamp(
        int(now_ts),
        UZ_TZ,
    )

    next_midnight = (
        local_now
        .replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        + timedelta(days=1)
    )

    return int(
        next_midnight.timestamp()
    )


def get_effective_device_manager(
    user_login: str | None,
    at_ts: int | None = None,
    conn=None,
):

    normalized_login = normalize_user_login(
        user_login
    )

    profile = get_call_source_profile(
        normalized_login
    )

    if not profile:
        return {
            "user_login": normalized_login,
            "device_name": (
                normalized_login
                or "Неизвестно"
            ),
            "manager_code": None,
            "manager_name": None,
            "default_manager_code": None,
            "default_manager_name": None,
            "permanent_manager_code": None,
            "permanent_manager_name": None,
            "permanent_custom": False,
            "configured_manager_code": None,
            "configured_manager_name": None,
            "temporary": False,
            "effective_from": None,
            "effective_until": None,
            "assignment_id": None,
        }

    if at_ts is None:
        at_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )
    else:
        try:
            at_ts = int(at_ts)
        except (
            TypeError,
            ValueError,
        ):
            at_ts = 0

    owns_connection = conn is None

    if owns_connection:
        conn = connect_db()

    try:
        permanent = conn.execute(
            """
            SELECT
                manager_code,
                manager_name,
                updated_at

            FROM device_manager_defaults

            WHERE user_login = ?

            LIMIT 1
            """,
            (
                normalized_login,
            ),
        ).fetchone()

        permanent_at_time = conn.execute(
            """
            SELECT
                new_manager_code,
                new_manager_name,
                action,
                changed_at

            FROM device_manager_default_history

            WHERE
                user_login = ?
                AND changed_at <= ?

            ORDER BY
                changed_at DESC,
                id DESC

            LIMIT 1
            """,
            (
                normalized_login,
                at_ts,
            ),
        ).fetchone()

        assignment = conn.execute(
            """
            SELECT
                id,
                manager_code,
                manager_name,
                effective_from,
                effective_until

            FROM device_manager_assignments

            WHERE
                user_login = ?
                AND effective_from <= ?
                AND effective_until > ?

            ORDER BY
                effective_from DESC,
                id DESC

            LIMIT 1
            """,
            (
                normalized_login,
                at_ts,
                at_ts,
            ),
        ).fetchone()
    finally:
        if owns_connection:
            conn.close()

    configured_manager_code = profile.get(
        "default_manager_code"
    )
    configured_manager_name = MANAGERS.get(
        configured_manager_code
    )

    if permanent_at_time:
        permanent_manager_code = (
            permanent_at_time[
                "new_manager_code"
            ]
            if permanent_at_time[
                "new_manager_code"
            ] in MANAGERS
            else configured_manager_code
        )
        permanent_custom = (
            permanent_at_time["action"]
            == "set"
        )
    elif (
        permanent
        and permanent["updated_at"] <= at_ts
        and permanent["manager_code"] in MANAGERS
    ):
        permanent_manager_code = permanent[
            "manager_code"
        ]
        permanent_custom = True
    else:
        permanent_manager_code = (
            configured_manager_code
        )
        permanent_custom = False

    permanent_manager_name = MANAGERS.get(
        permanent_manager_code
    )

    if (
        assignment
        and assignment["manager_code"]
            in MANAGERS
    ):
        return {
            "user_login": normalized_login,
            "device_name": profile["device_name"],
            "manager_code": assignment[
                "manager_code"
            ],
            "manager_name": MANAGERS[
                assignment["manager_code"]
            ],
            "default_manager_code": (
                permanent_manager_code
            ),
            "default_manager_name": (
                permanent_manager_name
            ),
            "permanent_manager_code": (
                permanent_manager_code
            ),
            "permanent_manager_name": (
                permanent_manager_name
            ),
            "permanent_custom": permanent_custom,
            "configured_manager_code": (
                configured_manager_code
            ),
            "configured_manager_name": (
                configured_manager_name
            ),
            "temporary": True,
            "effective_from": assignment[
                "effective_from"
            ],
            "effective_until": assignment[
                "effective_until"
            ],
            "assignment_id": assignment["id"],
        }

    return {
        "user_login": normalized_login,
        "device_name": profile["device_name"],
        "manager_code": permanent_manager_code,
        "manager_name": permanent_manager_name,
        "default_manager_code": permanent_manager_code,
        "default_manager_name": permanent_manager_name,
        "permanent_manager_code": permanent_manager_code,
        "permanent_manager_name": permanent_manager_name,
        "permanent_custom": permanent_custom,
        "configured_manager_code": configured_manager_code,
        "configured_manager_name": configured_manager_name,
        "temporary": False,
        "effective_from": None,
        "effective_until": None,
        "assignment_id": None,
    }


def list_device_manager_assignments(
    now_ts: int | None = None,
):

    if now_ts is None:
        now_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )

    with connect_db() as conn:
        devices = [
            get_effective_device_manager(
                user_login,
                now_ts,
                conn,
            )
            for user_login in CALL_SOURCE_PROFILES
        ]

    for device in devices:
        until = device[
            "effective_until"
        ]
        device["effective_until_label"] = (
            datetime.fromtimestamp(
                until,
                UZ_TZ,
            ).strftime(
                "%d.%m.%Y %H:%M"
            )
            if until
            else None
        )

    return devices


def set_device_manager_assignment(
    user_login: str,
    manager_code: str,
    changed_by: str = "dashboard",
    now_ts: int | None = None,
):

    normalized_login = normalize_user_login(
        user_login
    )
    profile = get_call_source_profile(
        normalized_login
    )

    if not profile:
        raise ValueError(
            "Неизвестный телефон"
        )

    manager_code = str(
        manager_code
        or ""
    ).strip().casefold()
    manager_name = MANAGERS.get(
        manager_code
    )

    if not manager_name:
        raise ValueError(
            "Неизвестный менеджер"
        )

    if now_ts is None:
        now_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )
    else:
        now_ts = int(now_ts)

    effective_until = (
        next_uz_midnight_timestamp(
            now_ts
        )
    )

    with connect_db() as conn:
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        conn.execute(
            """
            UPDATE device_manager_assignments

            SET
                effective_until = ?,
                ended_by = ?

            WHERE
                user_login = ?
                AND effective_from <= ?
                AND effective_until > ?
            """,
            (
                now_ts,
                str(changed_by or "dashboard")[:200],
                normalized_login,
                now_ts,
                now_ts,
            ),
        )

        cursor = conn.execute(
            """
            INSERT INTO device_manager_assignments (
                user_login,
                device_name,
                manager_code,
                manager_name,
                effective_from,
                effective_until,
                changed_at,
                changed_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_login,
                profile["device_name"],
                manager_code,
                manager_name,
                now_ts,
                effective_until,
                now_ts,
                str(changed_by or "dashboard")[:200],
            ),
        )

        conn.commit()

    result = get_effective_device_manager(
        normalized_login,
        now_ts,
    )
    result["assignment_id"] = cursor.lastrowid

    return result


def reset_device_manager_assignment(
    user_login: str,
    changed_by: str = "dashboard",
    now_ts: int | None = None,
):

    normalized_login = normalize_user_login(
        user_login
    )

    if not get_call_source_profile(
        normalized_login
    ):
        raise ValueError(
            "Неизвестный телефон"
        )

    if now_ts is None:
        now_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )
    else:
        now_ts = int(now_ts)

    with connect_db() as conn:
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        conn.execute(
            """
            UPDATE device_manager_assignments

            SET
                effective_until = ?,
                ended_by = ?

            WHERE
                user_login = ?
                AND effective_from <= ?
                AND effective_until > ?
            """,
            (
                now_ts,
                str(changed_by or "dashboard")[:200],
                normalized_login,
                now_ts,
                now_ts,
            ),
        )

        conn.commit()

    return get_effective_device_manager(
        normalized_login,
        now_ts,
    )


def set_permanent_device_manager(
    user_login: str,
    manager_code: str,
    changed_by: str = "dashboard",
    now_ts: int | None = None,
):

    normalized_login = normalize_user_login(
        user_login
    )
    profile = get_call_source_profile(
        normalized_login
    )

    if not profile:
        raise ValueError(
            "Неизвестный телефон"
        )

    manager_code = str(
        manager_code
        or ""
    ).strip().casefold()
    manager_name = MANAGERS.get(
        manager_code
    )

    if not manager_name:
        raise ValueError(
            "Неизвестный менеджер"
        )

    if now_ts is None:
        now_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )
    else:
        now_ts = int(now_ts)

    actor = str(
        changed_by
        or "dashboard"
    )[:200]

    with connect_db() as conn:
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        previous = conn.execute(
            """
            SELECT
                manager_code,
                manager_name

            FROM device_manager_defaults

            WHERE user_login = ?

            LIMIT 1
            """,
            (
                normalized_login,
            ),
        ).fetchone()

        old_manager_code = (
            previous["manager_code"]
            if previous
            else profile[
                "default_manager_code"
            ]
        )
        old_manager_name = MANAGERS.get(
            old_manager_code
        )

        conn.execute(
            """
            UPDATE device_manager_assignments

            SET
                effective_until = ?,
                ended_by = ?

            WHERE
                user_login = ?
                AND effective_from <= ?
                AND effective_until > ?
            """,
            (
                now_ts,
                actor,
                normalized_login,
                now_ts,
                now_ts,
            ),
        )

        conn.execute(
            """
            INSERT INTO device_manager_defaults (
                user_login,
                device_name,
                manager_code,
                manager_name,
                updated_at,
                updated_by
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(user_login)
            DO UPDATE SET
                device_name = excluded.device_name,
                manager_code = excluded.manager_code,
                manager_name = excluded.manager_name,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (
                normalized_login,
                profile["device_name"],
                manager_code,
                manager_name,
                now_ts,
                actor,
            ),
        )

        conn.execute(
            """
            INSERT INTO
                device_manager_default_history (
                    user_login,
                    device_name,
                    old_manager_code,
                    old_manager_name,
                    new_manager_code,
                    new_manager_name,
                    action,
                    changed_at,
                    changed_by
                )
            VALUES (?, ?, ?, ?, ?, ?, 'set', ?, ?)
            """,
            (
                normalized_login,
                profile["device_name"],
                old_manager_code,
                old_manager_name,
                manager_code,
                manager_name,
                now_ts,
                actor,
            ),
        )

        conn.commit()

    return get_effective_device_manager(
        normalized_login,
        now_ts,
    )


def reset_permanent_device_manager(
    user_login: str,
    changed_by: str = "dashboard",
    now_ts: int | None = None,
):

    normalized_login = normalize_user_login(
        user_login
    )
    profile = get_call_source_profile(
        normalized_login
    )

    if not profile:
        raise ValueError(
            "Неизвестный телефон"
        )

    if now_ts is None:
        now_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )
    else:
        now_ts = int(now_ts)

    actor = str(
        changed_by
        or "dashboard"
    )[:200]

    with connect_db() as conn:
        conn.execute(
            "BEGIN IMMEDIATE"
        )

        previous = conn.execute(
            """
            SELECT
                manager_code,
                manager_name

            FROM device_manager_defaults

            WHERE user_login = ?

            LIMIT 1
            """,
            (
                normalized_login,
            ),
        ).fetchone()

        conn.execute(
            """
            UPDATE device_manager_assignments

            SET
                effective_until = ?,
                ended_by = ?

            WHERE
                user_login = ?
                AND effective_from <= ?
                AND effective_until > ?
            """,
            (
                now_ts,
                actor,
                normalized_login,
                now_ts,
                now_ts,
            ),
        )

        if previous:
            configured_manager_code = profile[
                "default_manager_code"
            ]
            configured_manager_name = MANAGERS.get(
                configured_manager_code
            )

            conn.execute(
                """
                DELETE FROM device_manager_defaults
                WHERE user_login = ?
                """,
                (
                    normalized_login,
                ),
            )

            conn.execute(
                """
                INSERT INTO
                    device_manager_default_history (
                        user_login,
                        device_name,
                        old_manager_code,
                        old_manager_name,
                        new_manager_code,
                        new_manager_name,
                        action,
                        changed_at,
                        changed_by
                    )
                VALUES (?, ?, ?, ?, ?, ?, 'reset', ?, ?)
                """,
                (
                    normalized_login,
                    profile["device_name"],
                    previous["manager_code"],
                    previous["manager_name"],
                    configured_manager_code,
                    configured_manager_name,
                    now_ts,
                    actor,
                ),
            )

        conn.commit()

    return get_effective_device_manager(
        normalized_login,
        now_ts,
    )


# =========================================================
# SMS
# =========================================================

def build_after_hours_missed_sms(
    start_time,
) -> str | None:

    try:
        timestamp = int(
            start_time
            or 0
        )

        if timestamp <= 0:
            return None

        call_time = datetime.fromtimestamp(
            timestamp,
            UZ_TZ,
        )

    except (
        OSError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        return None

    minute_of_day = (
        call_time.hour * 60
        + call_time.minute
    )

    if (
        MISSED_CALL_WORK_START_MINUTES
        <= minute_of_day
        < MISSED_CALL_WORK_END_MINUTES
    ):
        return None

    before_opening = (
        minute_of_day
        < MISSED_CALL_WORK_START_MINUTES
    )

    return AFTER_HOURS_MISSED_SMS_TEXT.format(
        day_ru=(
            "Сегодня"
            if before_opening
            else "Завтра"
        ),
        day_uz=(
            "Bugun"
            if before_opening
            else "Ertaga"
        ),
        work_start=(
            MISSED_CALL_WORK_START_LABEL
        ),
    )


def reserve_client_sms(
    call_id: int,
    client_number: str,
    sender_user_login: str | None,
    message_kind: str = "promo",
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
                        status IN (
                            'reserved',
                            'error'
                        )
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
                message_kind,
                status,
                reserved_at
            )

            VALUES (?, ?, ?, ?, ?, 'reserved', ?)
            """,
            (
                call_id,
                client_number,
                client_key,
                sender_user_login,
                (
                    str(
                        message_kind
                        or "promo"
                    )[:50]
                ),
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

        call = conn.execute(
            """
            SELECT
                client_window_id,
                is_internal_contact,
                answered

            FROM calls

            WHERE id = ?

            LIMIT 1
            """,
            (
                call_id,
            ),
        ).fetchone()

        if (
            not call
            or call["is_internal_contact"]
            or not call["answered"]
            or not call["client_window_id"]
        ):
            conn.commit()

            return {
                "reserved": False,
                "reason": "invalid_client_window",
            }

        client_window_id = call[
            "client_window_id"
        ]

        previous = conn.execute(
            """
            SELECT
                id,
                sms_status,
                score

            FROM call_ratings

            WHERE client_window_id = ?

            ORDER BY
                CASE
                    WHEN score IS NOT NULL
                    THEN 0
                    ELSE 1
                END,
                COALESCE(
                    rated_at,
                    sms_sent_at,
                    sms_reserved_at
                ),
                id

            LIMIT 1
            """,
            (
                client_window_id,
            ),
        ).fetchone()

        if previous:

            conn.commit()

            return {
                "reserved": False,
                "reason": "client_window_cooldown",
                "rating_id": previous["id"],
                "sms_status": previous["sms_status"],
                "score": previous["score"],
            }

        cursor = conn.execute(
            """
            INSERT INTO call_ratings (
                call_id,
                client_window_id,
                token_hash,
                client_key,
                sender_user_login,
                sms_status,
                sms_reserved_at,
                expires_at
            )

            VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
            """,
            (
                call_id,
                client_window_id,
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

    with tempfile.TemporaryDirectory() as tmpdir:

        source_path = (
            Path(tmpdir)
            / "call_source"
        )

        voice_path = (
            Path(tmpdir)
            / "call.ogg"
        )

        try:
            download_recording_limited(
                recording_url,
                source_path,
            )
        except Exception as exc:
            print(
                "RECORDING DOWNLOAD ERROR:",
                type(exc).__name__,
            )
            return (
                None,
                None,
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


# =========================================================
# DURABLE CALL TRANSCRIPTION
# =========================================================

TRANSCRIPTION_CLASSIFIER_VERSION = "lead-source-v2"


def recording_host_is_allowed(
    recording_url: str,
) -> bool:

    parsed = urlparse(
        recording_url
    )

    if parsed.scheme.casefold() != "https":
        return False

    if parsed.username or parsed.password:
        return False

    try:
        parsed_port = parsed.port
    except ValueError:
        return False

    if parsed_port not in {
        None,
        443,
    }:
        return False

    host = (
        parsed.hostname
        or ""
    ).casefold()

    if not host:
        return False

    host_allowed = any(
        (
            host.endswith(
                allowed
            )
            if allowed.startswith(".")
            else host == allowed
        )
        for allowed in TRANSCRIPTION_ALLOWED_HOSTS
    )

    if not host_allowed:
        return False

    try:
        addresses = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False

    for address in addresses:
        ip_text = address[4][0]

        try:
            ip = ipaddress.ip_address(
                ip_text
            )
        except ValueError:
            return False

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return bool(
        addresses
    )


def enqueue_transcription_in_transaction(
    conn,
    call_id: int,
):
    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    call = conn.execute(
        """
        SELECT
            canonical.id,
            canonical.recording,
            canonical.answered,
            canonical.is_internal_contact

        FROM calls AS original

        JOIN calls AS canonical
            ON canonical.id = COALESCE(
                original.duplicate_of_call_id,
                original.id
            )

        WHERE original.id = ?

        LIMIT 1
        """,
        (
            call_id,
        ),
    ).fetchone()

    if not call:
        return {
            "queued": False,
            "reason": "call_not_found",
        }

    if (
        not call["answered"]
        or call["is_internal_contact"]
        or not call["recording"]
    ):
        return {
            "queued": False,
            "reason": "not_applicable",
        }

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO call_transcriptions (
            call_id,
            status,
            attempts,
            next_attempt_at,
            provider,
            model,
            queued_at,
            updated_at
        )

        VALUES (?, 'queued', 0, ?, 'openai', ?, ?, ?)
        """,
        (
            call["id"],
            now_ts,
            TRANSCRIPTION_MODEL,
            now_ts,
            now_ts,
        ),
    )

    return {
        "queued": cursor.rowcount == 1,
        "reason": (
            "queued"
            if cursor.rowcount == 1
            else "already_queued"
        ),
    }


def enqueue_transcription(
    call_id: int,
):

    if not TRANSCRIPTION_ENABLED:
        return {
            "queued": False,
            "reason": "disabled",
        }

    with connect_db() as conn:
        result = enqueue_transcription_in_transaction(
            conn,
            call_id,
        )
        conn.commit()

    if _transcription_worker:
        _transcription_worker.wake()

    return result


def claim_transcription_job():

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    lease_token = uuid.uuid4().hex

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        conn.execute(
            """
            UPDATE call_transcriptions

            SET
                status = 'error',
                lease_token = NULL,
                lease_until = NULL,
                error = COALESCE(
                    error,
                    'Истёк lease последней попытки'
                ),
                updated_at = ?

            WHERE
                status = 'processing'
                AND attempts >= ?
                AND COALESCE(
                    lease_until,
                    0
                ) < ?
            """,
            (
                now_ts,
                TRANSCRIPTION_MAX_ATTEMPTS,
                now_ts,
            ),
        )

        job = conn.execute(
            """
            SELECT
                transcription.call_id,
                transcription.attempts,
                call.recording

            FROM call_transcriptions
                AS transcription

            JOIN calls AS call
                ON call.id = transcription.call_id

            WHERE
                transcription.attempts < ?
                AND (
                    (
                        transcription.status = 'queued'
                        AND COALESCE(
                            transcription.next_attempt_at,
                            0
                        ) <= ?
                    )
                    OR (
                        transcription.status = 'processing'
                        AND COALESCE(
                            transcription.lease_until,
                            0
                        ) < ?
                    )
                )

            ORDER BY
                COALESCE(
                    transcription.next_attempt_at,
                    transcription.queued_at
                ),
                transcription.call_id

            LIMIT 1
            """,
            (
                TRANSCRIPTION_MAX_ATTEMPTS,
                now_ts,
                now_ts,
            ),
        ).fetchone()

        if not job:
            conn.commit()
            return None

        claimed = conn.execute(
            """
            UPDATE call_transcriptions

            SET
                status = 'processing',
                attempts = attempts + 1,
                lease_token = ?,
                lease_until = ?,
                started_at = ?,
                updated_at = ?,
                error = NULL

            WHERE
                call_id = ?
                AND (
                    status = 'queued'
                    OR COALESCE(
                        lease_until,
                        0
                    ) < ?
                )
            """,
            (
                lease_token,
                now_ts
                    + TRANSCRIPTION_LEASE_SECONDS,
                now_ts,
                now_ts,
                job["call_id"],
                now_ts,
            ),
        )

        conn.commit()

    if claimed.rowcount != 1:
        return None

    return {
        "call_id": job["call_id"],
        "attempts": int(
            job["attempts"]
            or 0
        ) + 1,
        "recording": job["recording"],
        "lease_token": lease_token,
    }


def download_recording_limited(
    recording_url: str,
    target_path: Path,
):

    if not recording_host_is_allowed(
        recording_url
    ):
        raise ValueError(
            "Недопустимый адрес записи"
        )

    started_at = time.monotonic()

    with requests.Session() as session, session.get(
        recording_url,
        stream=True,
        timeout=(15, 60),
        allow_redirects=False,
    ) as response:

        if 300 <= response.status_code < 400:
            raise ValueError(
                "Редиректы для записи запрещены"
            )

        response.raise_for_status()

        content_length = response.headers.get(
            "Content-Length"
        )

        if (
            content_length
            and int(content_length)
                > TRANSCRIPTION_MAX_BYTES
        ):
            raise ValueError(
                "Запись слишком большая"
            )

        total = 0
        digest = hashlib.sha256()

        with target_path.open(
            "wb"
        ) as target:

            for chunk in response.iter_content(
                chunk_size=64 * 1024
            ):
                if (
                    time.monotonic()
                    - started_at
                    > TRANSCRIPTION_TIMEOUT_SECONDS
                ):
                    raise TimeoutError(
                        "Превышено общее время загрузки записи"
                    )

                if not chunk:
                    continue

                total += len(
                    chunk
                )

                if total > TRANSCRIPTION_MAX_BYTES:
                    raise ValueError(
                        "Запись слишком большая"
                    )

                digest.update(
                    chunk
                )
                target.write(
                    chunk
                )

    return digest.hexdigest()


def normalize_transcription_audio(
    source_path: Path,
    target_path: Path,
):

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    duration = float(
        probe.stdout.strip()
        or 0
    )

    if duration <= 0:
        raise ValueError(
            "Пустая запись"
        )

    if duration > TRANSCRIPTION_MAX_DURATION_SECONDS:
        raise ValueError(
            "Запись длиннее разрешённого лимита"
        )

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            str(target_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )


def transcribe_audio_file(
    audio_path: Path,
):

    if not TRANSCRIPTION_API_KEY:
        raise RuntimeError(
            "TRANSCRIPTION_API_KEY не указан"
        )

    parsed = urlparse(
        TRANSCRIPTION_API_URL
    )

    if parsed.scheme.casefold() != "https":
        raise RuntimeError(
            "TRANSCRIPTION_API_URL должен использовать HTTPS"
        )

    session = requests.Session()

    with audio_path.open(
        "rb"
    ) as audio_file:

        response = session.post(
            TRANSCRIPTION_API_URL,
            headers={
                "Authorization": (
                    "Bearer "
                    + TRANSCRIPTION_API_KEY
                ),
            },
            data={
                "model": TRANSCRIPTION_MODEL,
                "response_format": "json",
                "prompt": (
                    "TEXNIKACH, OLX, Instagram, Telegram, "
                    "объявление, e'lon, eski mijoz. "
                    "Разговор может быть на русском или узбекском."
                ),
            },
            files={
                "file": (
                    "call.ogg",
                    audio_file,
                    "audio/ogg",
                ),
            },
            timeout=(15, TRANSCRIPTION_TIMEOUT_SECONDS),
        )

    response.raise_for_status()
    result = response.json()

    transcript = str(
        result.get(
            "text"
        )
        or ""
    ).strip()

    if not transcript:
        raise RuntimeError(
            "Сервис вернул пустую расшифровку"
        )

    language = result.get(
        "language"
    )

    languages = result.get(
        "languages"
    )

    if not language and languages:
        first_language = languages[0]
        language = (
            first_language.get("code")
            if isinstance(
                first_language,
                dict,
            )
            else str(
                first_language
            )
        )

    return {
        "text": transcript,
        "language": language,
    }


def detect_lead_source_from_transcript(
    transcript: str,
):

    normalized = str(
        transcript
        or ""
    ).casefold()

    normalized = normalized.replace(
        "’",
        "'",
    ).replace(
        "ʻ",
        "'",
    ).replace(
        "‘",
        "'",
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    ).strip()

    # A platform name alone is not proof of acquisition. For example,
    # "I will send the catalogue in Telegram" describes the next action,
    # not where the customer came from. We only accept a platform when the
    # nearby phrase also contains acquisition intent. The numerical value is
    # a rule score used to rank competing candidates; it is not a measured
    # statistical probability.
    acquisition_intent = re.compile(
        (
            r"(?:наш(?:[её]л|л[аио])\w*|увидел\w*|узнал\w*|"
            r"увидела\w*|узнала\w*|звоню\s+по|обращаюсь\s+по|"
            r"переш[её]л\w*|номер\w*\s+(?:наш[её]л\w*|увидел\w*)|"
            r"topdim|ko[' ]?rdim|kordim|bildim|"
            r"qayerdan|raqam\w*\s+(?:topdim|oldim)|"
            r"qo[' ]?ng[' ]?iroq\s+qil\w*)"
        ),
        flags=re.IGNORECASE,
    )

    negative_context = re.compile(
        (
            r"(?:не\s+(?:наш[её]л\w*|видел\w*|увидел\w*)|"
            r"нас\s+(?:там\s+)?нет|не\s+из|не\s+по|"
            r"yo[' ]?q|emas)"
        ),
        flags=re.IGNORECASE,
    )

    seller_action = re.compile(
        (
            r"(?:отправлю|пришлю|скину|покажу|напишите|пишите|"
            r"подпишитесь|наш\s+канал|наш\s+каталог|"
            r"каталог\w*\s+(?:в|через)|ссылк\w*\s+(?:в|на)|"
            r"yubor\w*|yoz\w*|kanalimiz)"
        ),
        flags=re.IGNORECASE,
    )

    platform_rules = [
        (
            "olx",
            re.compile(
                r"\b(?:olx|олх|оликс|олекс)(?:dan)?\b",
                flags=re.IGNORECASE,
            ),
        ),
        (
            "instagram",
            re.compile(
                r"\b(?:instagram|инстаграм|insta|инста)\w*\b",
                flags=re.IGNORECASE,
            ),
        ),
        (
            "telegram_channel",
            re.compile(
                r"\b(?:telegram|телеграм)\w*\b",
                flags=re.IGNORECASE,
            ),
        ),
    ]

    candidates = {}

    def remember_candidate(
        code,
        rule_score,
        match,
    ):
        start = max(
            0,
            match.start() - 70,
        )
        end = min(
            len(normalized),
            match.end() + 70,
        )
        evidence = normalized[
            start:end
        ].strip()
        previous = candidates.get(
            code
        )

        if (
            not previous
            or rule_score
                > previous["confidence"]
        ):
            candidates[code] = {
                "code": code,
                "confidence": rule_score,
                "rule_score": rule_score,
                "evidence": evidence,
            }

    for code, platform_pattern in platform_rules:
        for match in platform_pattern.finditer(
            normalized
        ):
            context_start = max(
                0,
                match.start() - 90,
            )
            context_end = min(
                len(normalized),
                match.end() + 90,
            )
            context = normalized[
                context_start:context_end
            ]

            if negative_context.search(
                context
            ):
                continue

            if seller_action.search(
                context
            ):
                continue

            explicit_from_platform = bool(
                re.search(
                    (
                        r"(?:из|с|через)\s+"
                        + platform_pattern.pattern
                        + r"|"
                        + platform_pattern.pattern
                        + r"(?:dan|дан)\b"
                    ),
                    context,
                    flags=re.IGNORECASE,
                )
            )

            if not (
                acquisition_intent.search(
                    context
                )
                or explicit_from_platform
            ):
                continue

            remember_candidate(
                code,
                0.90,
                match,
            )

    # A direct "calling from/about your advertisement" phrase is treated as
    # OLX for this business because the user explicitly requested that rule.
    advertisement_pattern = re.compile(
        (
            r"\b(?:звоню|обращаюсь)\s+по\s+(?:ваш\w*\s+)?"
            r"объ?явлен\w*\b|"
            r"\b(?:из|с)\s+объ?явлен\w*\b|"
            r"\b(?:e[' ]?lon|эълон)\w*(?:dan|дан)\b"
        ),
        flags=re.IGNORECASE,
    )

    advertisement_match = advertisement_pattern.search(
        normalized
    )

    if advertisement_match:
        remember_candidate(
            "olx",
            0.82,
            advertisement_match,
        )

    old_client_pattern = re.compile(
        (
            r"\b(?:стар\w+ клиент\w*|раньше покупал\w*|"
            r"уже покупал\w*|покупал\w* у вас|"
            r"eski mijoz\w*|oldin olgan\w*|"
            r"avval olgan\w*)\b"
        ),
        flags=re.IGNORECASE,
    )
    old_client_match = old_client_pattern.search(
        normalized
    )

    if old_client_match:
        remember_candidate(
            "old_client",
            0.94,
            old_client_match,
        )

    ordered = sorted(
        candidates.values(),
        key=lambda item: item[
            "confidence"
        ],
        reverse=True,
    )

    selected = None

    if ordered:
        if (
            len(ordered) == 1
            or ordered[0]["confidence"]
                - ordered[1]["confidence"]
                >= 0.15
        ):
            selected = ordered[0]

    return {
        "code": (
            selected["code"]
            if selected
            else None
        ),
        "confidence": (
            selected["confidence"]
            if selected
            else None
        ),
        "evidence": (
            selected["evidence"]
            if selected
            else None
        ),
        "candidates": ordered,
    }


def complete_transcription_job(
    job: dict,
    transcript_result: dict,
    audio_sha256: str,
):

    detected = detect_lead_source_from_transcript(
        transcript_result["text"]
    )

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        updated = conn.execute(
            """
            UPDATE call_transcriptions

            SET
                status = 'completed',
                lease_token = NULL,
                lease_until = NULL,
                provider = 'openai',
                model = ?,
                language = ?,
                transcript_text = ?,
                audio_sha256 = ?,
                error = NULL,
                lead_source_code = ?,
                lead_source_confidence = ?,
                lead_source_evidence = ?,
                lead_source_candidates_json = ?,
                classifier_version = ?,
                completed_at = ?,
                updated_at = ?

            WHERE
                call_id = ?
                AND status = 'processing'
                AND lease_token = ?
            """,
            (
                TRANSCRIPTION_MODEL,
                transcript_result.get(
                    "language"
                ),
                transcript_result["text"],
                audio_sha256,
                detected["code"],
                detected["confidence"],
                detected["evidence"],
                json.dumps(
                    detected["candidates"],
                    ensure_ascii=False,
                ),
                TRANSCRIPTION_CLASSIFIER_VERSION,
                now_ts,
                now_ts,
                job["call_id"],
                job["lease_token"],
            ),
        )

        if updated.rowcount == 1:

            call = conn.execute(
                """
                SELECT client_key
                FROM calls
                WHERE id = ?
                LIMIT 1
                """,
                (
                    job["call_id"],
                ),
            ).fetchone()

            if detected["code"]:
                conn.execute(
                    """
                    UPDATE calls

                    SET
                        lead_source_auto_code = ?,
                        lead_source_auto_method =
                            'transcript',
                        lead_source_auto_confidence = ?,
                        lead_source_auto_evidence = ?

                    WHERE id = ?
                    """,
                    (
                        detected["code"],
                        detected["confidence"],
                        detected["evidence"],
                        job["call_id"],
                    ),
                )

            if call and call["client_key"]:
                recompute_client_window_sources(
                    conn,
                    call["client_key"],
                )

        conn.commit()

    if updated.rowcount == 1:
        try:
            call = get_call(
                job["call_id"]
            )

            if (
                call
                and call["telegram_chat_id"]
                and call["telegram_message_id"]
                and not call[
                    "is_internal_contact"
                ]
            ):
                edit_reply_markup(
                    call["telegram_chat_id"],
                    call["telegram_message_id"],
                    build_call_state_keyboard(
                        call
                    ),
                )

        except Exception as exc:
            print(
                "TRANSCRIPTION TELEGRAM REFRESH ERROR:",
                job["call_id"],
                type(exc).__name__,
            )

    return updated.rowcount == 1


def fail_transcription_job(
    job: dict,
    error,
    *,
    retry_allowed: bool = True,
    delay_override: int | None = None,
):

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    retry = bool(
        retry_allowed
        and
        job["attempts"]
        < TRANSCRIPTION_MAX_ATTEMPTS
    )

    delay_seconds = min(
        3600,
        30
        * (
            2
            ** max(
                0,
                job["attempts"] - 1,
            )
        ),
    )

    if delay_override is not None:
        delay_seconds = max(
            1,
            min(
                24 * 60 * 60,
                int(delay_override),
            ),
        )

    with connect_db() as conn:

        updated = conn.execute(
            """
            UPDATE call_transcriptions

            SET
                status = ?,
                next_attempt_at = ?,
                lease_token = NULL,
                lease_until = NULL,
                error = ?,
                updated_at = ?

            WHERE
                call_id = ?
                AND status = 'processing'
                AND lease_token = ?
            """,
            (
                (
                    "queued"
                    if retry
                    else "error"
                ),
                (
                    now_ts + delay_seconds
                    if retry
                    else None
                ),
                str(error)[:2000],
                now_ts,
                job["call_id"],
                job["lease_token"],
            ),
        )

        conn.commit()

    return updated.rowcount == 1


def renew_transcription_lease(
    job: dict,
) -> bool:

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    with connect_db() as conn:
        updated = conn.execute(
            """
            UPDATE call_transcriptions

            SET
                lease_until = ?,
                updated_at = ?

            WHERE
                call_id = ?
                AND status = 'processing'
                AND lease_token = ?
            """,
            (
                now_ts
                    + TRANSCRIPTION_LEASE_SECONDS,
                now_ts,
                job["call_id"],
                job["lease_token"],
            ),
        )
        conn.commit()

    return updated.rowcount == 1


def run_transcription_heartbeat(
    job: dict,
    stop_event: threading.Event,
):

    interval = max(
        10.0,
        TRANSCRIPTION_LEASE_SECONDS
        / 3,
    )

    while not stop_event.wait(
        interval
    ):
        try:
            if not renew_transcription_lease(
                job
            ):
                return
        except Exception as exc:
            print(
                "TRANSCRIPTION LEASE HEARTBEAT ERROR:",
                job["call_id"],
                type(exc).__name__,
            )


def process_one_transcription_job():

    job = claim_transcription_job()

    if not job:
        return False

    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=run_transcription_heartbeat,
        args=(
            job,
            heartbeat_stop,
        ),
        name=(
            "transcription-lease-"
            + str(job["call_id"])
        ),
        daemon=True,
    )
    heartbeat.start()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:

            source_path = Path(
                tmpdir
            ) / "source_audio"

            audio_path = Path(
                tmpdir
            ) / "call.ogg"

            audio_sha256 = download_recording_limited(
                job["recording"],
                source_path,
            )

            normalize_transcription_audio(
                source_path,
                audio_path,
            )

            transcript_result = transcribe_audio_file(
                audio_path
            )

        completed = complete_transcription_job(
            job,
            transcript_result,
            audio_sha256,
        )

        if completed:
            print(
                "TRANSCRIPTION COMPLETED:",
                job["call_id"],
            )

    except Exception as exc:

        retry_allowed = True
        retry_after = None

        if isinstance(
            exc,
            requests.HTTPError,
        ) and exc.response is not None:
            status_code = exc.response.status_code
            retry_allowed = (
                status_code in {
                    408,
                    409,
                    429,
                }
                or status_code >= 500
            )
            retry_header = exc.response.headers.get(
                "Retry-After"
            )

            if retry_header:
                try:
                    retry_after = int(
                        retry_header
                    )
                except ValueError:
                    retry_after = None

        failed = fail_transcription_job(
            job,
            repr(exc),
            retry_allowed=retry_allowed,
            delay_override=retry_after,
        )

        if failed:
            print(
                "TRANSCRIPTION ERROR:",
                job["call_id"],
                type(exc).__name__,
            )

    finally:
        heartbeat_stop.set()
        heartbeat.join(
            timeout=1,
        )

    return True


class TranscriptionWorker:

    def __init__(self):
        self._task = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()

    async def start(self):
        if self._task:
            return

        self._task = asyncio.create_task(
            self._run(),
            name="call-transcription-worker",
        )

    async def stop(self):
        self._stop_event.set()
        self._wake_event.set()

        if self._task:
            await self._task
            self._task = None

    def wake(self):
        self._wake_event.set()

    async def _run(self):

        while not self._stop_event.is_set():

            try:
                processed = await asyncio.to_thread(
                    process_one_transcription_job
                )
            except Exception as exc:
                print(
                    "TRANSCRIPTION WORKER ERROR:",
                    type(exc).__name__,
                )
                processed = False

            if processed:
                continue

            self._wake_event.clear()

            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=max(
                        1.0,
                        TRANSCRIPTION_POLL_SECONDS,
                    ),
                )
            except asyncio.TimeoutError:
                pass


def update_call_audio_duration(
    call_id: int,
    audio_duration: int,
):

    duration = max(
        0,
        int(audio_duration),
    )

    with connect_db() as conn:
        conn.execute(
            """
            UPDATE calls

            SET
                duration = ?,
                duration_source = 'audio'

            WHERE id = ?
            """,
            (
                duration,
                call_id,
            ),
        )
        conn.commit()


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

def build_webhook_dedup_keys(
    webhook: dict,
    event: dict,
    client_key: str,
):

    keys = []

    db_call_id = event.get(
        "db_call_id"
    )

    if db_call_id is not None:
        keys.append(
            "db:" + str(
                db_call_id
            )
        )

    event_pbx_call_id = event.get(
        "event_pbx_call_id"
    )

    account_id = webhook.get(
        "account_id"
    )

    if event_pbx_call_id:
        keys.append(
            (
                "pbx:"
                + str(account_id or "")
                + ":"
                + str(event_pbx_call_id)
            )
        )

    src_key = normalize_phone(
        event.get("src_number")
    )

    start_time = int(
        event.get("start_time")
        or 0
    )

    # A fingerprint is only authoritative when it describes an
    # actual call.  Hashing an almost empty payload would make
    # unrelated DB/PBX ids look like the same webhook.
    fingerprint_is_strong = (
        start_time > 0
        and bool(client_key or src_key)
        and event.get("direction")
            is not None
    )

    if fingerprint_is_strong:

        fingerprint = json.dumps(
            {
                "account_id": str(
                    account_id or ""
                ),
                "client_key": client_key,
                "src_key": src_key,
                "direction": event.get(
                    "direction"
                ),
                "start_time": start_time,
                "answer_time": event.get(
                    "answer_time"
                ),
                "end_time": event.get(
                    "end_time"
                ),
                "src_id": event.get(
                    "src_id"
                ),
                "src_slot": event.get(
                    "src_slot"
                ),
                "event_created": (
                    event.get("event_created")
                    or event.get("upload_time")
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        keys.append(
            (
                "fp:"
                + hashlib.sha256(
                    fingerprint.encode(
                        "utf-8"
                    )
                ).hexdigest()
            )
        )

    if not keys:
        keys.append(
            "weak:"
            + secrets.token_hex(16)
        )

    return list(
        dict.fromkeys(
            keys
        )
    )


def build_webhook_dedup_key(
    webhook: dict,
    event: dict,
    client_key: str,
):

    return build_webhook_dedup_keys(
        webhook,
        event,
        client_key,
    )[0]


def rebuild_client_windows_for_client(
    conn,
    client_key: str,
):

    if not client_key:
        return

    calls = conn.execute(
        """
        SELECT
            id,
            start_time

        FROM reporting_calls

        WHERE
            client_key = ?
            AND COALESCE(
                is_internal_contact,
                0
            ) = 0
            AND start_time IS NOT NULL
            AND start_time > 0

        ORDER BY start_time, id
        """,
        (
            client_key,
        ),
    ).fetchall()

    # These tables are derived from calls. Rebuilding one
    # client atomically also handles late/out-of-order webhooks.
    conn.execute(
        """
        DELETE FROM client_results
        WHERE client_key = ?
        """,
        (
            client_key,
        ),
    )

    conn.execute(
        """
        UPDATE call_ratings

        SET client_window_id = NULL

        WHERE call_id IN (
            SELECT id
            FROM calls
            WHERE client_key = ?
        )
        """,
        (
            client_key,
        ),
    )

    conn.execute(
        """
        UPDATE calls
        SET client_window_id = NULL
        WHERE client_key = ?
        """,
        (
            client_key,
        ),
    )

    conn.execute(
        """
        DELETE FROM client_windows
        WHERE client_key = ?
        """,
        (
            client_key,
        ),
    )

    if not calls:
        return

    cooldown_seconds = (
        RESULT_COOLDOWN_HOURS
        * 60
        * 60
    )

    active_window_id = None
    previous_call_start = None

    for call in calls:

        call_start = int(
            call["start_time"]
        )

        if (
            active_window_id is None
            or call_start >= (
                previous_call_start
                + cooldown_seconds
            )
        ):

            cursor = conn.execute(
                """
                INSERT INTO client_windows (
                    client_key,
                    started_at,
                    ends_at,
                    first_call_id,
                    latest_call_id
                )

                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    client_key,
                    call_start,
                    call_start
                    + cooldown_seconds,
                    call["id"],
                    call["id"],
                ),
            )

            active_window_id = (
                cursor.lastrowid
            )

        else:

            conn.execute(
                """
                UPDATE client_windows

                SET
                    latest_call_id = ?,
                    ends_at = ?,
                    updated_at = CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    call["id"],
                    call_start
                    + cooldown_seconds,
                    active_window_id,
                ),
            )

        previous_call_start = call_start

        conn.execute(
            """
            UPDATE calls

            SET client_window_id = ?

            WHERE id = ?
            """,
            (
                active_window_id,
                call["id"],
            ),
        )

    conn.execute(
        """
        UPDATE call_ratings

        SET client_window_id = (
            SELECT call.client_window_id
            FROM calls AS call
            WHERE call.id = call_ratings.call_id
        )

        WHERE call_id IN (
            SELECT id
            FROM calls
            WHERE client_key = ?
        )
        """,
        (
            client_key,
        ),
    )

    recompute_client_window_sources(
        conn,
        client_key,
    )

    raw_results = conn.execute(
        """
        SELECT
            call.id,
            call.client_key,
            call.client_window_id,
            call.start_time,
            call.sale_status,
            call.no_sale_reason,
            call.no_sale_reason_code,
            call.talk_manager_code,
            call.talk_manager_name,
            call.sale_marked_at,
            call.result_revision,
            call.sale_marked_by,
            call.sale_marked_username,
            window.started_at,
            window.ends_at

        FROM reporting_calls AS call

        JOIN client_windows AS window
            ON window.id =
                call.client_window_id

        WHERE
            call.client_key = ?
            AND call.sale_status IN (
                'bought',
                'not_bought'
            )

        ORDER BY
            COALESCE(
                call.result_revision,
                0
            ),
            COALESCE(
                call.sale_marked_at,
                call.start_time,
                call.id
            ),
            call.id
        """,
        (
            client_key,
        ),
    ).fetchall()

    latest_results = {}

    for result in raw_results:
        latest_results[
            result["client_window_id"]
        ] = result

    for result in latest_results.values():

        conn.execute(
            """
            INSERT INTO client_results (
                client_key,
                client_window_id,
                window_started_at,
                window_ends_at,
                source_call_id,
                attribution_time,
                sale_status,
                result_category,
                no_sale_reason,
                no_sale_reason_code,
                talk_manager_code,
                talk_manager_name,
                marked_at,
                marked_by,
                marked_username
            )

            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                result["client_key"],
                result["client_window_id"],
                result["started_at"],
                result["ends_at"],
                result["id"],
                result["start_time"],
                result["sale_status"],
                get_result_category(
                    result["sale_status"],
                    result[
                        "no_sale_reason_code"
                    ],
                ),
                result["no_sale_reason"],
                result["no_sale_reason_code"],
                result["talk_manager_code"],
                result["talk_manager_name"],
                int(
                    result["sale_marked_at"]
                    or result["start_time"]
                ),
                result["sale_marked_by"],
                result[
                    "sale_marked_username"
                ],
            ),
        )


def assign_call_client_window(
    conn,
    call_id: int,
    client_key: str,
    start_time,
    is_internal_contact: int,
):

    start_time = int(
        start_time
        or 0
    )

    if (
        not call_id
        or not client_key
        or start_time <= 0
        or is_internal_contact
    ):
        return None

    rebuild_client_windows_for_client(
        conn,
        client_key,
    )

    row = conn.execute(
        """
        SELECT client_window_id
        FROM calls
        WHERE id = ?
        LIMIT 1
        """,
        (
            call_id,
        ),
    ).fetchone()

    return (
        row["client_window_id"]
        if row
        else None
    )

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

    webhook_dedup_keys = (
        build_webhook_dedup_keys(
            webhook,
            event,
            client_key,
        )
    )

    webhook_dedup_key = (
        webhook_dedup_keys[0]
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        existing = None

        def identity_conflicts(
            candidate,
        ):
            if not candidate:
                return False

            candidate_db_id = candidate[
                "db_call_id"
            ]

            if (
                db_call_id is not None
                and candidate_db_id is not None
                and str(db_call_id)
                    != str(candidate_db_id)
            ):
                return True

            candidate_pbx_id = candidate[
                "event_pbx_call_id"
            ]

            if (
                event_pbx_call_id
                and candidate_pbx_id
                and str(event_pbx_call_id)
                    != str(candidate_pbx_id)
            ):
                return True

            return False

        if db_call_id is not None:
            existing = conn.execute(
                """
                SELECT *
                FROM calls
                WHERE db_call_id = ?
                LIMIT 1
                """,
                (
                    db_call_id,
                ),
            ).fetchone()

        if not existing:

            for dedup_key in webhook_dedup_keys:

                existing = conn.execute(
                    """
                    SELECT call.*

                    FROM call_webhook_keys AS alias

                    JOIN calls AS call
                        ON call.id = alias.call_id

                    WHERE alias.dedup_key = ?

                    LIMIT 1
                    """,
                    (
                        dedup_key,
                    ),
                ).fetchone()

                if identity_conflicts(
                    existing
                ):
                    existing = None

                if existing:
                    break

        if (
            existing
            and existing[
                "duplicate_of_call_id"
            ]
        ):

            duplicate_row_id = existing["id"]
            canonical_row_id = existing[
                "duplicate_of_call_id"
            ]

            if db_call_id is not None:
                conn.execute(
                    """
                    UPDATE calls
                    SET db_call_id = NULL
                    WHERE
                        id = ?
                        AND duplicate_of_call_id = ?
                        AND db_call_id = ?
                    """,
                    (
                        duplicate_row_id,
                        canonical_row_id,
                        db_call_id,
                    ),
                )

            existing = conn.execute(
                """
                SELECT *
                FROM calls
                WHERE id = ?
                LIMIT 1
                """,
                (
                    canonical_row_id,
                ),
            ).fetchone()

        # An account id can be absent in one delivery and appear in
        # another.  PBX id is still a useful alias when it identifies
        # exactly one stored call.
        if (
            not existing
            and event_pbx_call_id
        ):

            account_matches = conn.execute(
                """
                SELECT *

                FROM calls

                WHERE
                    event_pbx_call_id = ?
                    AND COALESCE(
                        CAST(account_id AS TEXT),
                        ''
                    ) = ?

                ORDER BY id

                LIMIT 1
                """,
                (
                    event_pbx_call_id,
                    str(
                        webhook.get(
                            "account_id"
                        )
                        or ""
                    ),
                ),
            ).fetchone()

            if (
                account_matches
                and not identity_conflicts(
                    account_matches
                )
            ):
                existing = account_matches

            else:
                pbx_matches = conn.execute(
                    """
                    SELECT *
                    FROM calls
                    WHERE event_pbx_call_id = ?
                    ORDER BY id
                    LIMIT 2
                    """,
                    (
                        event_pbx_call_id,
                    ),
                ).fetchall()

                if (
                    len(pbx_matches) == 1
                    and not identity_conflicts(
                        pbx_matches[0]
                    )
                ):
                    existing = pbx_matches[0]

        if not existing:

            for dedup_key in webhook_dedup_keys:

                existing = conn.execute(
                    """
                    SELECT *
                    FROM calls
                    WHERE webhook_dedup_key = ?
                    LIMIT 1
                    """,
                    (
                        dedup_key,
                    ),
                ).fetchone()

                if identity_conflicts(
                    existing
                ):
                    existing = None

                if existing:
                    break

        def supplied(value):
            return (
                value is not None
                and not (
                    isinstance(value, str)
                    and not value.strip()
                )
            )

        def old_value(
            column: str,
            default=None,
        ):
            return (
                existing[column]
                if existing
                else default
            )

        def event_value(
            key: str,
            column: str | None = None,
            default=None,
        ):
            value = event.get(key)

            if key in event and supplied(value):
                return value

            return old_value(
                column or key,
                default,
            )

        def webhook_value(
            key: str,
            column: str | None = None,
            default=None,
        ):
            value = webhook.get(key)

            if key in webhook and supplied(value):
                return value

            return old_value(
                column or key,
                default,
            )

        client_number = event_value(
            "client_number"
        )

        client_key = (
            normalize_phone(
                client_number
            )
            or old_value(
                "client_key",
                "",
            )
        )

        internal_contact_name = (
            get_internal_contact_name(
                client_number
            )
            if client_number
            else old_value(
                "internal_contact_name"
            )
        )

        is_internal_contact = (
            1
            if internal_contact_name
            else 0
        )

        db_call_id = (
            old_value("db_call_id")
            if old_value("db_call_id")
                is not None
            else event_value(
                "db_call_id"
            )
        )

        event_pbx_call_id = event_value(
            "event_pbx_call_id"
        )

        event_created = (
            event.get("event_created")
            or event.get("upload_time")
            or old_value("event_created")
        )

        duration_payload_present = any(
            key in event
            and event.get(key) is not None
            for key in (
                "duration",
                "answer_time",
                "end_time",
                "recording",
            )
        )

        old_duration = int(
            old_value(
                "duration",
                0,
            )
            or 0
        )

        duration_update_reliable = (
            not existing
            or int(talk_duration or 0) > 0
            or (
                old_duration <= 0
                and duration_payload_present
            )
        )

        if (
            "duration" in event
            and event.get("duration")
                is not None
        ):
            api_duration = int(
                event.get("duration")
                or 0
            )
        else:
            api_duration = int(
                old_value(
                    "api_duration",
                    0,
                )
                or 0
            )

        merged_duration = (
            talk_duration
            if duration_update_reliable
            else old_duration
        )

        merged_duration_source = (
            duration_source
            if duration_update_reliable
            else old_value(
                "duration_source"
            )
        )

        webhook_dedup_key = (
            old_value(
                "webhook_dedup_key"
            )
            or webhook_dedup_key
        )

        merged_user_login = webhook_value(
            "user_login"
        )

        merged_src_slot = event_value(
            "src_slot"
        )

        if (
            "src_number" in event
            and supplied(
                event.get(
                    "src_number"
                )
            )
        ):
            provider_src_number = event.get(
                "src_number"
            )
        else:
            provider_src_number = (
                old_value(
                    "provider_src_number"
                )
                or old_value(
                    "src_number"
                )
            )

        device = resolve_call_device(
            merged_user_login,
            provider_src_number,
            merged_src_slot,
        )

        call_start_time = event_value(
            "start_time"
        )

        try:
            manager_attribution_time = int(
                call_start_time
                or event_created
                or 0
            )
        except (
            TypeError,
            ValueError,
        ):
            manager_attribution_time = 0

        if manager_attribution_time <= 0:
            manager_attribution_time = int(
                datetime.now(
                    timezone.utc
                ).timestamp()
            )

        device_manager = (
            get_effective_device_manager(
                merged_user_login,
                manager_attribution_time,
                conn,
            )
        )

        values = (
            db_call_id,
            event_pbx_call_id,
            webhook_dedup_key,
            client_number,
            client_key,
            event_value("client_name"),
            is_internal_contact,
            internal_contact_name,
            event_value("direction"),
            event_value("answered"),
            webhook_value("user_id"),
            merged_user_login,
            device["device_name"],
            provider_src_number,
            (
                device["src_number"]
                or event_value(
                    "src_number"
                )
            ),
            event_value("src_id"),
            merged_src_slot,
            event_created,
            call_start_time,
            event_value("answer_time"),
            event_value("end_time"),
            merged_duration,
            api_duration,
            merged_duration_source,
            event_value("recording"),
            webhook_value("account_id"),
            webhook_value("account_name"),
        )

        if existing:

            call_id = existing["id"]

            conn.execute(
                """
                UPDATE calls

                SET
                    db_call_id = ?,
                    event_pbx_call_id = ?,
                    webhook_dedup_key = ?,
                    client_number = ?,
                    client_key = ?,
                    client_name = ?,
                    is_internal_contact = ?,
                    internal_contact_name = ?,
                    direction = ?,
                    answered = ?,
                    user_id = ?,
                    user_login = ?,
                    device_name = ?,
                    provider_src_number = ?,
                    src_number = ?,
                    src_id = ?,
                    src_slot = ?,
                    event_created = ?,
                    start_time = ?,
                    answer_time = ?,
                    end_time = ?,
                    duration = ?,
                    api_duration = ?,
                    duration_source = ?,
                    recording = ?,
                    account_id = ?,
                    account_name = ?

                WHERE id = ?
                """,
                values
                + (
                    call_id,
                ),
            )

        else:

            cursor = conn.execute(
                """
                INSERT INTO calls (

                db_call_id,
                event_pbx_call_id,
                webhook_dedup_key,

                client_number,
                client_key,
                client_name,

                is_internal_contact,
                internal_contact_name,

                direction,
                answered,

                user_id,
                user_login,

                device_name,
                provider_src_number,
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

                ?, ?, ?,

                ?, ?, ?,

                ?, ?,

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

                """,
                values,
            )

            call_id = cursor.lastrowid

        if not is_internal_contact:

            default_manager_code = device_manager[
                "manager_code"
            ]

            default_manager_name = device_manager[
                "manager_name"
            ]

            if default_manager_name:
                conn.execute(
                    """
                    UPDATE calls

                    SET
                        talk_manager_code = ?,
                        talk_manager_name = ?,
                        manager_marked_at = COALESCE(
                            manager_marked_at,
                            start_time
                        ),
                        manager_marked_username =
                            COALESCE(
                                manager_marked_username,
                                ?
                            )

                    WHERE
                        id = ?
                        AND COALESCE(
                            talk_manager_code,
                            ''
                        ) = ''
                    """,
                    (
                        default_manager_code,
                        default_manager_name,
                        (
                            "Авто по телефону: "
                            + device_manager[
                                "device_name"
                            ]
                            + (
                                " (дежурный до конца дня)"
                                if device_manager[
                                    "temporary"
                                ]
                                else " (по умолчанию)"
                            )
                        ),
                        call_id,
                    ),
                )

            default_source_code = device[
                "lead_source_code"
            ]

            conn.execute(
                """
                UPDATE calls

                SET
                    lead_source_auto_code = ?,
                    lead_source_auto_method = ?,
                    lead_source_auto_confidence = ?,
                    lead_source_auto_evidence = ?

                WHERE
                    id = ?
                    AND (
                        lead_source_auto_method IN (
                            'sim',
                            'account'
                        )
                        OR (
                            COALESCE(
                                lead_source_auto_code,
                                ''
                            ) = ''
                            AND COALESCE(
                                lead_source_auto_method,
                                ''
                            ) = ''
                        )
                    )
                """,
                (
                    default_source_code,
                    device[
                        "lead_source_method"
                    ],
                    device[
                        "lead_source_confidence"
                    ],
                    (
                        "Авто по SIM/аккаунту: "
                        + normalize_user_login(
                            merged_user_login
                        )
                        if default_source_code
                        else None
                    ),
                    call_id,
                ),
            )

        merged_identity_event = {
            "db_call_id": db_call_id,
            "event_pbx_call_id": (
                event_pbx_call_id
            ),
            "client_number": client_number,
            "src_number": (
                device["src_number"]
                or event_value(
                    "src_number"
                )
            ),
            "direction": event_value(
                "direction"
            ),
            "start_time": event_value(
                "start_time"
            ),
            "answer_time": event_value(
                "answer_time"
            ),
            "end_time": event_value(
                "end_time"
            ),
            "src_id": event_value(
                "src_id"
            ),
            "src_slot": event_value(
                "src_slot"
            ),
            "event_created": event_created,
        }

        merged_identity_webhook = {
            "account_id": webhook_value(
                "account_id"
            ),
        }

        all_dedup_keys = list(
            dict.fromkeys(
                webhook_dedup_keys
                + build_webhook_dedup_keys(
                    merged_identity_webhook,
                    merged_identity_event,
                    client_key,
                )
            )
        )

        for dedup_key in all_dedup_keys:

            conn.execute(
                """
                INSERT OR IGNORE INTO
                    call_webhook_keys (
                        dedup_key,
                        call_id
                    )
                VALUES (?, ?)
                """,
                (
                    dedup_key,
                    call_id,
                ),
            )

        old_client_key = (
            existing["client_key"]
            if existing
            else None
        )

        if (
            old_client_key
            and old_client_key != client_key
        ):
            rebuild_client_windows_for_client(
                conn,
                old_client_key,
            )

        if client_key:
            rebuild_client_windows_for_client(
                conn,
                client_key,
            )

        # The transcription row is an outbox record: it is committed in the
        # same transaction as the call, so a process crash cannot leave an
        # eligible recording permanently unqueued.
        if TRANSCRIPTION_ENABLED:
            conn.execute(
                "SAVEPOINT transcription_outbox"
            )

            try:
                enqueue_transcription_in_transaction(
                    conn,
                    call_id,
                )
                conn.execute(
                    "RELEASE transcription_outbox"
                )
            except Exception as exc:
                conn.execute(
                    "ROLLBACK TO transcription_outbox"
                )
                conn.execute(
                    "RELEASE transcription_outbox"
                )
                print(
                    "TRANSCRIPTION OUTBOX ERROR:",
                    call_id,
                    type(exc).__name__,
                )

        now_ts = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )

        claim = conn.execute(
            """
            UPDATE calls

            SET telegram_reserved_at = ?

            WHERE
                id = ?
                AND COALESCE(
                    telegram_sent,
                    0
                ) = 0
                AND (
                    telegram_reserved_at IS NULL
                    OR telegram_reserved_at < ?
                )
            """,
            (
                now_ts,
                call_id,
                now_ts - 300,
            ),
        )

        row = conn.execute(
            """
            SELECT
                id,
                telegram_sent,
                telegram_reserved_at,
                is_internal_contact,
                internal_contact_name

            FROM calls

            WHERE id = ?

            LIMIT 1
            """,
            (
                call_id,
            ),
        ).fetchone()

        conn.commit()

    return {
        "call_id":
            (
                row["id"]
                if row
                else call_id
            ),

        "already_sent":
            bool(
                row
                and row[
                    "telegram_sent"
                ]
            ),

        "telegram_claimed":
            claim.rowcount == 1,

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
                telegram_reserved_at = NULL,
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


def release_telegram_claim(
    call_id: int,
):

    if not call_id:
        return

    with connect_db() as conn:
        conn.execute(
            """
            UPDATE calls

            SET telegram_reserved_at = NULL

            WHERE
                id = ?
                AND COALESCE(
                    telegram_sent,
                    0
                ) = 0
            """,
            (
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
    call=None,
):

    if call:
        direction = call[
            "direction"
        ]

        answered = int(
            call[
                "answered"
            ]
            or 0
        )

        client_number = (
            call[
                "client_number"
            ]
            or ""
        )

        client_name = (
            call[
                "client_name"
            ]
            or ""
        )

        internal_contact_name = (
            call[
                "internal_contact_name"
            ]
            if call[
                "is_internal_contact"
            ]
            else None
        )

        start_time = call[
            "start_time"
        ]

        answer_time = call[
            "answer_time"
        ]

        talk_duration = int(
            call[
                "duration"
            ]
            or 0
        )

    else:
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

        start_time = event.get(
            "start_time"
        )

        answer_time = event.get(
            "answer_time"
        )

    sim = (
        call["src_number"]
        if call
        else event.get(
            "src_number"
        )
    ) or ""

    device = resolve_call_device(
        (
            call["user_login"]
            if call
            else webhook.get(
                "user_login"
            )
        ),
        (
            call["provider_src_number"]
            if call
            else event.get(
                "src_number"
            )
        ),
        (
            call["src_slot"]
            if call
            else event.get(
                "src_slot"
            )
        ),
    )

    manager_name = (
        call["talk_manager_name"]
        if call
        else None
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
            "🕐 Начало звонка: "
            f"<b>{format_call_time(start_time)}</b>"
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

    if device["device_name"]:
        lines.append(
            "📱 Устройство: "
            f"<b>{escape(device['device_name'])}</b>"
        )

    lines.append(
        "📲 "
        f"{escape(device['sim_label'])}: "
        f"<b>{escape(sim or '—')}</b>"
    )

    if manager_name:
        lines.append(
            "👤 Менеджер: "
            f"<b>{escape(manager_name)}</b>"
        )

    # -------------------------------------------------
    # BUTTON PROMPT
    # -------------------------------------------------

    if (
        answered
        and not manager_name
        and not internal_contact_name
    ):

        lines.extend(
            [
                "",
                "<b>Кто разговаривал?</b>",
            ]
        )

    elif (
        direction == 1
        and not answered
        and not manager_name
        and not internal_contact_name
    ):

        lines.extend(
            [
                "",
                "<b>Кто совершал исходящий звонок?</b>",
            ]
        )

    elif (
        direction == 0
        and not answered
        and not manager_name
        and not internal_contact_name
    ):

        lines.extend(
            [
                "",
                "<b>Кто отвечает за пропущенный звонок?</b>",
            ]
        )

    return "\n".join(
        lines
    )


# =========================================================
# TELEGRAM KEYBOARDS
# =========================================================

def get_call_lead_source_name(
    call_id: int,
) -> str | None:

    call = get_call(
        call_id
    )

    if not call:
        return None

    return get_lead_source_name(
        call[
            "effective_lead_source_code"
        ]
    )


def build_lead_source_control_row(
    call_id: int,
):

    source_name = get_call_lead_source_name(
        call_id
    )

    return [
        {
            "text": (
                "📣 Источник: "
                + source_name
                if source_name
                else "📣 Выбрать источник"
            ),
            "callback_data": (
                f"source_menu:{call_id}"
            ),
        }
    ]


def build_lead_source_keyboard(
    call_id: int,
):

    return {
        "inline_keyboard": [
            [
                {
                    "text": "OLX",
                    "callback_data": (
                        f"source:olx:{call_id}"
                    ),
                },
                {
                    "text": "Instagram",
                    "callback_data": (
                        f"source:instagram:{call_id}"
                    ),
                },
            ],
            [
                {
                    "text": "Telegram Kanal",
                    "callback_data": (
                        "source:telegram_channel:"
                        f"{call_id}"
                    ),
                },
                {
                    "text": "Старый клиент",
                    "callback_data": (
                        f"source:old_client:{call_id}"
                    ),
                },
            ],
            [
                {
                    "text": "↩️ Назад",
                    "callback_data": (
                        f"source_back:{call_id}"
                    ),
                }
            ],
        ]
    }

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

            build_lead_source_control_row(
                call_id
            ),
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

    keyboard.append(
        build_lead_source_control_row(
            call_id
        )
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
                        "🕓 В работе / ожидает",

                    "callback_data":
                        f"result:pending:{call_id}",
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


def build_manager_rating_keyboard(
    call_id: int,
    manager_name: str,
    selected_score: int | None = None,
):

    def rating_button(
        score: int,
    ):

        selected = (
            selected_score == score
        )

        return {
            "text": (
                f"✅ {score} ★"
                if selected
                else f"{score} ★"
            ),
            "callback_data": (
                f"manager_rating:{score}:{call_id}"
            ),
        }

    return {
        "inline_keyboard": [
            [
                {
                    "text": (
                        f"👤 Менеджер: {manager_name}"
                    ),
                    "callback_data": (
                        f"manager_selected:{call_id}"
                    ),
                }
            ],
            build_lead_source_control_row(
                call_id
            ),
            [
                rating_button(1),
                rating_button(2),
                rating_button(3),
            ],
            [
                rating_button(4),
                rating_button(5),
            ],
            [
                {
                    "text": "👤 Изменить менеджера",
                    "callback_data": (
                        f"manager_back:{call_id}"
                    ),
                }
            ],
        ]
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

            build_lead_source_control_row(
                call_id
            ),

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


def build_missed_manager_keyboard(
    call_id: int,
    manager_name: str,
):

    return {
        "inline_keyboard": [
            [
                {
                    "text":
                        f"👤 Ответственный: {manager_name}",

                    "callback_data":
                        f"manager_selected:{call_id}",
                }
            ],
            build_lead_source_control_row(
                call_id
            ),
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
            SELECT
                calls.*,
                window.lead_source_code
                    AS effective_lead_source_code,
                window.lead_source_origin
                    AS effective_lead_source_origin,
                window.lead_source_confidence
                    AS effective_lead_source_confidence,
                window.lead_source_evidence
                    AS effective_lead_source_evidence,
                result.source_call_id
                    AS effective_result_call_id,
                result.sale_status
                    AS effective_sale_status,
                result.no_sale_reason
                    AS effective_no_sale_reason,
                result.no_sale_reason_code
                    AS effective_no_sale_reason_code,
                result.result_category
                    AS effective_result_category,
                manager_rating.source_call_id
                    AS effective_manager_rating_call_id,
                manager_rating.score
                    AS effective_manager_rating,
                manager_rating.marked_at
                    AS manager_rating_marked_at,
                manager_rating.marked_by
                    AS manager_rating_marked_by,
                manager_rating.marked_username
                    AS manager_rating_marked_username

            FROM calls

            LEFT JOIN client_windows AS window
                ON window.id = calls.client_window_id

            LEFT JOIN client_results AS result
                ON result.client_window_id =
                    calls.client_window_id

            LEFT JOIN manager_ratings AS manager_rating
                ON manager_rating.client_window_id =
                    calls.client_window_id

            WHERE calls.id = ?
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


def get_selected_result_text(
    call,
) -> str | None:

    if call["effective_sale_status"] == "bought":
        return "✅ Купил"

    if call[
        "effective_sale_status"
    ] == "not_bought":
        return (
            call["effective_no_sale_reason"]
            or "Не купил"
        )

    return None


def build_call_state_keyboard(
    call,
):

    manager_name = call[
        "talk_manager_name"
    ]

    if not manager_name:
        return build_manager_keyboard(
            call["id"]
        )

    if call["answered"]:
        return build_manager_rating_keyboard(
            call["id"],
            manager_name,
            call["effective_manager_rating"],
        )

    return build_missed_manager_keyboard(
        call["id"],
        manager_name,
    )


def mark_lead_source(
    call_id: int,
    source_code: str,
    telegram_user: dict,
):

    source_name = get_lead_source_name(
        source_code
    )

    if not source_name:
        raise ValueError(
            "Неизвестный источник"
        )

    now_ts = int(
        datetime.now(
            UZ_TZ
        ).timestamp()
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        call = conn.execute(
            """
            SELECT
                id,
                client_key,
                is_internal_contact

            FROM calls

            WHERE id = ?

            LIMIT 1
            """,
            (
                call_id,
            ),
        ).fetchone()

        if not call:
            conn.rollback()
            raise ValueError(
                "Звонок не найден"
            )

        if call["is_internal_contact"]:
            conn.rollback()
            raise ValueError(
                "Для внутреннего контакта источник не нужен"
            )

        revision = conn.execute(
            """
            SELECT
                COALESCE(
                    MAX(lead_source_revision),
                    0
                ) + 1

            FROM calls
            """
        ).fetchone()[0]

        conn.execute(
            """
            UPDATE calls

            SET
                lead_source_manual_code = ?,
                lead_source_revision = ?,
                lead_source_marked_at = ?,
                lead_source_marked_by = ?,
                lead_source_marked_username = ?

            WHERE id = ?
            """,
            (
                source_code,
                revision,
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

        if call["client_key"]:
            recompute_client_window_sources(
                conn,
                call["client_key"],
            )

        conn.commit()

    return source_name


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

        conn.execute(
            """
            UPDATE client_results

            SET
                talk_manager_code = ?,
                talk_manager_name = ?,
                updated_at = CURRENT_TIMESTAMP

            WHERE source_call_id = ?
            """,
            (
                manager_code,
                manager_name,
                call_id,
            ),
        )

        conn.execute(
            """
            UPDATE manager_ratings

            SET
                talk_manager_code = ?,
                talk_manager_name = ?,
                updated_at = CURRENT_TIMESTAMP

            WHERE source_call_id = ?
            """,
            (
                manager_code,
                manager_name,
                call_id,
            ),
        )

        conn.commit()

    return manager_name


# =========================================================
# INTERNAL MANAGER RATING DATABASE
# =========================================================

def can_rate_manager(
    telegram_user: dict,
) -> bool:

    try:
        user_id = int(
            telegram_user.get("id")
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        return False

    return (
        user_id
        == MANAGER_RATING_ADMIN_ID
    )


def mark_manager_rating(
    call_id: int,
    score: int,
    telegram_user: dict,
):

    if not can_rate_manager(
        telegram_user
    ):
        raise PermissionError(
            "Нет права ставить оценку менеджеру"
        )

    try:
        score = int(score)
    except (
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError(
            "Оценка должна быть от 1 до 5"
        ) from exc

    if score not in range(1, 6):
        raise ValueError(
            "Оценка должна быть от 1 до 5"
        )

    now_ts = int(
        datetime.now(
            UZ_TZ
        ).timestamp()
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        call = conn.execute(
            """
            SELECT
                id,
                client_key,
                client_number,
                client_window_id,
                is_internal_contact,
                answered,
                start_time,
                talk_manager_code,
                talk_manager_name

            FROM calls

            WHERE id = ?

            LIMIT 1
            """,
            (call_id,),
        ).fetchone()

        if not call:
            conn.rollback()
            raise ValueError(
                "Звонок не найден"
            )

        client_key = (
            call["client_key"]
            or normalize_phone(
                call["client_number"]
            )
        )

        if (
            not client_key
            or call["is_internal_contact"]
        ):
            conn.rollback()
            raise ValueError(
                "Оценка для этого номера недоступна"
            )

        if not call["answered"]:
            conn.rollback()
            raise ValueError(
                "Оценить можно только отвеченный звонок"
            )

        if not call["talk_manager_code"]:
            conn.rollback()
            raise ValueError(
                "Сначала выберите менеджера"
            )

        client_window_id = call[
            "client_window_id"
        ]

        if not client_window_id:
            client_window_id = (
                assign_call_client_window(
                    conn,
                    call_id,
                    client_key,
                    call["start_time"],
                    call["is_internal_contact"],
                )
            )

        existing = conn.execute(
            """
            SELECT id
            FROM manager_ratings
            WHERE client_window_id = ?
            LIMIT 1
            """,
            (client_window_id,),
        ).fetchone()

        values = (
            call_id,
            int(call["start_time"] or now_ts),
            call["talk_manager_code"],
            call["talk_manager_name"],
            score,
            now_ts,
            int(telegram_user["id"]),
            get_telegram_user_name(
                telegram_user
            ),
        )

        if existing:
            conn.execute(
                """
                UPDATE manager_ratings

                SET
                    source_call_id = ?,
                    attribution_time = ?,
                    talk_manager_code = ?,
                    talk_manager_name = ?,
                    score = ?,
                    marked_at = ?,
                    marked_by = ?,
                    marked_username = ?,
                    updated_at = CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                values + (existing["id"],),
            )
            rating_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO manager_ratings (
                    client_key,
                    client_window_id,
                    source_call_id,
                    attribution_time,
                    talk_manager_code,
                    talk_manager_name,
                    score,
                    marked_at,
                    marked_by,
                    marked_username
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_key,
                    client_window_id,
                ) + values,
            )
            rating_id = cursor.lastrowid

        conn.commit()

    return {
        "rating_id": rating_id,
        "score": score,
        "replaced": bool(existing),
        "client_window_id": client_window_id,
    }


# =========================================================
# SALE DATABASE
# =========================================================

def mark_client_result(
    call_id: int,
    sale_status: str,
    telegram_user: dict,
    *,
    reason_code: str | None = None,
):

    if sale_status not in {
        "bought",
        "not_bought",
    }:
        raise ValueError(
            "Неизвестный результат"
        )

    reason = None

    if sale_status == "not_bought":
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

    marked_by = telegram_user.get(
        "id"
    )

    marked_username = get_telegram_user_name(
        telegram_user
    )

    with connect_db() as conn:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        call = conn.execute(
            """
            SELECT
                id,
                client_key,
                client_number,
                client_window_id,
                is_internal_contact,
                answered,
                start_time,
                talk_manager_code,
                talk_manager_name

            FROM calls

            WHERE id = ?

            LIMIT 1
            """,
            (
                call_id,
            ),
        ).fetchone()

        if not call:
            conn.rollback()
            raise ValueError(
                "Звонок не найден"
            )

        client_key = (
            call["client_key"]
            or normalize_phone(
                call["client_number"]
            )
        )

        if (
            not client_key
            or call["is_internal_contact"]
        ):
            conn.rollback()
            raise ValueError(
                "Результат для этого номера недоступен"
            )

        if not call["answered"]:
            conn.rollback()
            raise ValueError(
                "Результат доступен только для отвеченного звонка"
            )

        client_window_id = call[
            "client_window_id"
        ]

        if not client_window_id:
            client_window_id = (
                assign_call_client_window(
                    conn,
                    call_id,
                    client_key,
                    call["start_time"],
                    call["is_internal_contact"],
                )
            )

        window = conn.execute(
            """
            SELECT *

            FROM client_windows

            WHERE id = ?

            LIMIT 1
            """,
            (
                client_window_id,
            ),
        ).fetchone()

        if not window:
            conn.rollback()
            raise ValueError(
                "Не удалось определить 30-часовую группу клиента"
            )

        existing = conn.execute(
            """
            SELECT *

            FROM client_results

            WHERE client_window_id = ?

            LIMIT 1
            """,
            (
                client_window_id,
            ),
        ).fetchone()

        attribution_time = int(
            call["start_time"]
            or now_ts
        )

        result_category = get_result_category(
            sale_status,
            reason_code,
        )

        result_revision = conn.execute(
            """
            SELECT
                COALESCE(
                    MAX(result_revision),
                    0
                ) + 1
            FROM calls
            """
        ).fetchone()[0]

        if existing:

            result_id = existing["id"]

            conn.execute(
                """
                UPDATE client_results

                SET
                    source_call_id = ?,
                    attribution_time = ?,
                    sale_status = ?,
                    result_category = ?,
                    no_sale_reason = ?,
                    no_sale_reason_code = ?,
                    talk_manager_code = ?,
                    talk_manager_name = ?,
                    marked_at = ?,
                    marked_by = ?,
                    marked_username = ?,
                    updated_at = CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    call_id,
                    attribution_time,
                    sale_status,
                    result_category,
                    reason,
                    reason_code,
                    call["talk_manager_code"],
                    call["talk_manager_name"],
                    now_ts,
                    marked_by,
                    marked_username,
                    result_id,
                ),
            )

        else:

            cursor = conn.execute(
                """
                INSERT INTO client_results (
                    client_key,
                    client_window_id,
                    window_started_at,
                    window_ends_at,
                    source_call_id,
                    attribution_time,
                    sale_status,
                    result_category,
                    no_sale_reason,
                    no_sale_reason_code,
                    talk_manager_code,
                    talk_manager_name,
                    marked_at,
                    marked_by,
                    marked_username
                )

                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    client_key,
                    client_window_id,
                    window["started_at"],
                    window["ends_at"],
                    call_id,
                    attribution_time,
                    sale_status,
                    result_category,
                    reason,
                    reason_code,
                    call["talk_manager_code"],
                    call["talk_manager_name"],
                    now_ts,
                    marked_by,
                    marked_username,
                ),
            )

            result_id = cursor.lastrowid

        conn.execute(
            """
            UPDATE calls

            SET
                sale_status = ?,
                no_sale_reason = ?,
                no_sale_reason_code = ?,
                sale_marked_at = ?,
                result_revision = ?,
                sale_marked_by = ?,
                sale_marked_username = ?

            WHERE id = ?
            """,
            (
                sale_status,
                reason,
                reason_code,
                now_ts,
                result_revision,
                marked_by,
                marked_username,
                call_id,
            ),
        )

        conn.commit()

    return {
        "result_id": result_id,
        "replaced": bool(existing),
        "previous_status": (
            existing["sale_status"]
            if existing
            else None
        ),
        "previous_call_id": (
            existing["source_call_id"]
            if existing
            else None
        ),
        "window_started_at": (
            existing["window_started_at"]
            if existing
            else window["started_at"]
        ),
    }


def mark_sale_bought(
    call_id: int,
    telegram_user: dict,
):

    return mark_client_result(
        call_id,
        "bought",
        telegram_user,
    )


def mark_sale_not_bought(
    call_id: int,
    reason_code: str,
    telegram_user: dict,
):

    return mark_client_result(
        call_id,
        "not_bought",
        telegram_user,
        reason_code=reason_code,
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
        # LEAD SOURCE MENU
        # =================================================

        if callback_data.startswith(
            "source_menu:"
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
                return {"ok": True}

            if call["is_internal_contact"]:
                answer_callback_query(
                    callback_id,
                    "Это внутренний контакт",
                )
                return {"ok": True}

            edit_reply_markup(
                chat_id,
                message_id,
                build_lead_source_keyboard(
                    call_id
                ),
            )

            answer_callback_query(
                callback_id,
                "Выберите источник клиента",
            )

            return {"ok": True}

        # =================================================
        # LEAD SOURCE BACK
        # =================================================

        if callback_data.startswith(
            "source_back:"
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
                return {"ok": True}

            edit_reply_markup(
                chat_id,
                message_id,
                build_call_state_keyboard(
                    call
                ),
            )

            answer_callback_query(
                callback_id,
                "Назад",
            )

            return {"ok": True}

        # =================================================
        # LEAD SOURCE SELECTED
        # =================================================

        if callback_data.startswith(
            "source:"
        ):

            parts = callback_data.split(
                ":"
            )

            if len(parts) != 3:
                answer_callback_query(
                    callback_id,
                    "Ошибка кнопки",
                )
                return {"ok": True}

            source_code = parts[1]
            call_id = int(
                parts[2]
            )

            source_name = mark_lead_source(
                call_id,
                source_code,
                telegram_user,
            )

            call = get_call(
                call_id
            )

            edit_reply_markup(
                chat_id,
                message_id,
                build_call_state_keyboard(
                    call
                ),
            )

            answer_callback_query(
                callback_id,
                f"📣 Источник: {source_name}",
            )

            print(
                "LEAD SOURCE:",
                call_id,
                source_code,
            )

            return {
                "ok": True,
                "call_id": call_id,
                "lead_source": source_code,
            }

        # =================================================
        # INTERNAL MANAGER RATING
        # =================================================

        if callback_data.startswith(
            "manager_rating:"
        ):

            parts = callback_data.split(
                ":"
            )

            if len(parts) != 3:
                answer_callback_query(
                    callback_id,
                    "Ошибка кнопки",
                )
                return {"ok": True}

            if not can_rate_manager(
                telegram_user
            ):
                answer_callback_query(
                    callback_id,
                    "Только Abbos может ставить оценку",
                )
                return {
                    "ok": True,
                    "forbidden": True,
                }

            score = int(parts[1])
            call_id = int(parts[2])

            saved_rating = mark_manager_rating(
                call_id,
                score,
                telegram_user,
            )

            call = get_call(
                call_id
            )

            edit_reply_markup(
                chat_id,
                message_id,
                build_call_state_keyboard(
                    call
                ),
            )

            answer_callback_query(
                callback_id,
                f"Оценка менеджеру: {score} из 5",
            )

            return {
                "ok": True,
                "call_id": call_id,
                "manager_rating": score,
                "replaced": saved_rating[
                    "replaced"
                ],
            }

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

            call = get_call(
                call_id
            )

            edit_reply_markup(
                chat_id,
                message_id,
                build_call_state_keyboard(
                    call
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
            (
                "result:",
                "result_back:",
                "selected:",
            )
        ):

            answer_callback_query(
                callback_id,
                "Выбор результата отключён",
            )

            return {
                "ok": True,
                "disabled": True,
            }

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

            if not call["answered"]:
                answer_callback_query(
                    callback_id,
                    "Результат доступен только для отвеченного звонка",
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

            if not call["answered"]:

                answer_callback_query(
                    callback_id,
                    "Результат доступен только для отвеченного звонка",
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

                saved_result = mark_sale_bought(
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
                    (
                        "✅ Последний результат клиента обновлён"
                        if saved_result["replaced"]
                        else "✅ Сохранено"
                    ),
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

            saved_result = mark_sale_not_bought(
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
                (
                    "✅ Последний результат клиента обновлён"
                    if saved_result["replaced"]
                    else "✅ Сохранено"
                ),
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
# REAL SALES FROM SALES PHOTO BOT
# =========================================================

def get_sale_timestamp(
    sale_date_value: str,
    created_at_value: str | None,
) -> int:

    sale_day = date.fromisoformat(
        sale_date_value
    )

    try:
        created_at = datetime.fromisoformat(
            str(
                created_at_value
                or ""
            ).replace(
                "Z",
                "+00:00",
            )
        )

        if created_at.tzinfo is None:
            created_at = created_at.replace(
                tzinfo=timezone.utc
            )

        if created_at.astimezone(
            UZ_TZ
        ).date() == sale_day:
            return int(
                created_at.timestamp()
            )

    except (
        TypeError,
        ValueError,
    ):
        pass

    return int(
        datetime(
            sale_day.year,
            sale_day.month,
            sale_day.day,
            23,
            59,
            59,
            tzinfo=UZ_TZ,
        ).timestamp()
    )


def load_external_sales(
    start_date: str,
    end_date: str,
) -> tuple[bool, list[dict]]:

    if not SALES_PHOTO_DB_PATH.is_file():
        return False, []

    try:
        sales_conn = sqlite3.connect(
            SALES_PHOTO_DB_PATH.resolve().as_uri()
            + "?mode=ro",
            uri=True,
            timeout=5,
        )
        sales_conn.row_factory = sqlite3.Row
        sales_conn.execute(
            "PRAGMA query_only = ON"
        )

        columns = {
            row["name"]
            for row in sales_conn.execute(
                "PRAGMA table_info(sales_photo_jobs)"
            ).fetchall()
        }

        required_columns = {
            "chat_id",
            "source_message_id",
            "replacement_message_id",
            "manager",
            "client_phone",
            "client_phone_2",
            "product_label",
            "sale_date",
            "created_at",
            "status",
            "order_removed",
        }

        if not required_columns.issubset(
            columns
        ):
            sales_conn.close()
            return False, []

        rows = sales_conn.execute(
            """
            SELECT
                chat_id,
                source_message_id,
                replacement_message_id,
                manager,
                client_phone,
                client_phone_2,
                product_label,
                sale_date,
                created_at

            FROM sales_photo_jobs

            WHERE
                sale_date >= ?
                AND sale_date <= ?
                AND replacement_message_id
                    IS NOT NULL
                AND order_removed = 0
                AND status IN (
                    'reposted',
                    'delete_pending',
                    'complete'
                )

            ORDER BY
                sale_date,
                created_at,
                source_message_id
            """,
            (
                start_date,
                end_date,
            ),
        ).fetchall()

        sales_conn.close()

    except (
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(
            "SALES ANALYTICS UNAVAILABLE:",
            type(exc).__name__,
        )
        return False, []

    results = []

    for row in rows:
        phones = tuple(
            dict.fromkeys(
                phone_key
                for phone_key in (
                    normalize_phone(
                        row["client_phone"]
                    ),
                    normalize_phone(
                        row["client_phone_2"]
                    ),
                )
                if phone_key
            )
        )

        results.append(
            {
                "sale_id": (
                    f"{row['chat_id']}:"
                    f"{row['source_message_id']}"
                ),
                "source_message_id": row[
                    "source_message_id"
                ],
                "replacement_message_id": row[
                    "replacement_message_id"
                ],
                "sale_date": row[
                    "sale_date"
                ],
                "sold_at": get_sale_timestamp(
                    row["sale_date"],
                    row["created_at"],
                ),
                "phones": phones,
                "phone": (
                    " / ".join(
                        phone
                        for phone in (
                            row["client_phone"],
                            row["client_phone_2"],
                        )
                        if phone
                    )
                ),
                "manager": (
                    row["manager"]
                    or "Не указан"
                ),
                "manager_code": (
                    normalize_sales_manager_code(
                        row["manager"]
                    )
                ),
                "product_label": (
                    row["product_label"]
                    or None
                ),
            }
        )

    return True, results


def build_real_sales_analytics(
    calls_conn: sqlite3.Connection,
    period_data: dict,
) -> dict:

    configured, sales = load_external_sales(
        period_data["start_date"],
        period_data["end_date"],
    )

    valid_sales = [
        sale
        for sale in sales
        if sale["phones"]
    ]

    all_phones = sorted(
        {
            phone
            for sale in valid_sales
            for phone in sale["phones"]
        }
    )

    first_call_by_phone = {}

    if all_phones:
        placeholders = ",".join(
            "?"
            for _ in all_phones
        )

        call_rows = calls_conn.execute(
            f"""
            SELECT
                client_key,
                MIN(start_time) AS first_call_time

            FROM reporting_calls

            WHERE
                client_key IN ({placeholders})
                AND COALESCE(
                    is_internal_contact,
                    0
                ) = 0

            GROUP BY client_key
            """,
            all_phones,
        ).fetchall()

        first_call_by_phone = {
            row["client_key"]: row[
                "first_call_time"
            ]
            for row in call_rows
        }

    parent = {
        phone: phone
        for phone in all_phones
    }

    def find(phone: str) -> str:
        while parent[phone] != phone:
            parent[phone] = parent[
                parent[phone]
            ]
            phone = parent[phone]
        return phone

    def union(first: str, second: str):
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    for sale in valid_sales:
        for phone in sale["phones"][1:]:
            union(
                sale["phones"][0],
                phone,
            )

    buyers = {}

    for sale in valid_sales:
        buyer_key = find(
            sale["phones"][0]
        )
        buyer = buyers.setdefault(
            buyer_key,
            {
                "phones": set(),
                "sales": [],
            },
        )
        buyer["phones"].update(
            sale["phones"]
        )
        buyer["sales"].append(sale)

    buyer_rows = []

    for buyer in buyers.values():
        first_sale = min(
            buyer["sales"],
            key=lambda sale: (
                sale["sold_at"],
                sale["source_message_id"],
            ),
        )
        call_times = [
            first_call_by_phone[phone]
            for phone in buyer["phones"]
            if first_call_by_phone.get(
                phone
            ) is not None
        ]
        first_call_time = (
            min(call_times)
            if call_times
            else None
        )
        called_before = (
            first_call_time is not None
            and first_call_time
                <= first_sale["sold_at"]
        )
        buyer_rows.append(
            {
                "buyer_key": min(
                    buyer["phones"]
                ),
                "manager": first_sale[
                    "manager"
                ],
                "manager_code": first_sale[
                    "manager_code"
                ],
                "called_before_purchase": (
                    called_before
                ),
            }
        )

    manager_buyers = {}

    for buyer in buyer_rows:
        metrics = manager_buyers.setdefault(
            buyer["manager_code"],
            {
                "manager": buyer[
                    "manager"
                ],
                "buyers": 0,
                "called_before_purchase": 0,
                "without_prior_call": 0,
            },
        )
        metrics["buyers"] += 1
        if buyer["called_before_purchase"]:
            metrics[
                "called_before_purchase"
            ] += 1
        else:
            metrics[
                "without_prior_call"
            ] += 1

    customer_rows = []

    for sale in reversed(sales):
        call_times = [
            first_call_by_phone[phone]
            for phone in sale["phones"]
            if first_call_by_phone.get(
                phone
            ) is not None
        ]
        first_call_time = (
            min(call_times)
            if call_times
            else None
        )
        customer_rows.append(
            {
                "sale_id": sale["sale_id"],
                "sale_date": sale[
                    "sale_date"
                ],
                "phone": sale["phone"] or "—",
                "manager": sale["manager"],
                "product_label": (
                    sale["product_label"]
                    or "—"
                ),
                "called_before_purchase": (
                    first_call_time is not None
                    and first_call_time
                        <= sale["sold_at"]
                ),
                "has_phone": bool(
                    sale["phones"]
                ),
            }
        )

    buyers_total = len(
        buyer_rows
    )
    buyers_called = sum(
        buyer[
            "called_before_purchase"
        ]
        for buyer in buyer_rows
    )
    buyers_without_call = (
        buyers_total
        - buyers_called
    )

    return {
        "configured": configured,
        "sales_total": len(sales),
        "sales_without_phone": (
            len(sales)
            - len(valid_sales)
        ),
        "buyers_total": buyers_total,
        "buyers_called_before_purchase": (
            buyers_called
        ),
        "buyers_without_prior_call": (
            buyers_without_call
        ),
        "buyers_without_prior_call_rate": (
            round(
                buyers_without_call
                / buyers_total
                * 100,
                1,
            )
            if buyers_total
            else 0
        ),
        "manager_buyers": manager_buyers,
        "results": customer_rows,
    }


@app.get(
    "/stats/sales/customers"
)
def stats_sales_customers(
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
        analytics = build_real_sales_analytics(
            conn,
            p,
        )

    return {
        "configured": analytics[
            "configured"
        ],
        "stats": {
            key: analytics[key]
            for key in (
                "sales_total",
                "sales_without_phone",
                "buyers_total",
                "buyers_called_before_purchase",
                "buyers_without_prior_call",
                "buyers_without_prior_call_rate",
            )
        },
        "results": analytics["results"],
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
                expires_at,
                client_window_id

            FROM call_ratings

            WHERE token_hash = ?

            LIMIT 1
            """,
            (
                token_hash,
            ),
        ).fetchone()

        window_score = None

        if (
            row
            and row["client_window_id"]
        ):
            scored = conn.execute(
                """
                SELECT score

                FROM call_ratings

                WHERE
                    client_window_id = ?
                    AND score IS NOT NULL

                ORDER BY
                    rated_at,
                    id

                LIMIT 1
                """,
                (
                    row["client_window_id"],
                ),
            ).fetchone()

            if scored:
                window_score = scored[
                    "score"
                ]

    if not row:
        return rating_html_response(
            "Ссылка не найдена",
            "Проверьте ссылку из SMS.",
            "SMSdagi havolani tekshiring.",
            status_code=404,
        )

    saved_score = (
        window_score
        if window_score is not None
        else row["score"]
    )

    if saved_score is not None:
        return rating_html_response(
            "Спасибо за оценку!",
            "Ваш ответ уже сохранён.",
            "Javobingiz saqlandi. Rahmat!",
            selected_score=saved_score,
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
                id,
                score,
                expires_at,
                client_window_id

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

        window_score = None

        if row["client_window_id"]:
            scored = conn.execute(
                """
                SELECT score

                FROM call_ratings

                WHERE
                    client_window_id = ?
                    AND score IS NOT NULL

                ORDER BY
                    rated_at,
                    id

                LIMIT 1
                """,
                (
                    row["client_window_id"],
                ),
            ).fetchone()

            if scored:
                window_score = scored[
                    "score"
                ]

        saved_score = (
            window_score
            if window_score is not None
            else row["score"]
        )

        if saved_score is not None:
            conn.commit()

            return rating_html_response(
                "Спасибо за оценку!",
                "Первая оценка уже сохранена.",
                "Birinchi baho saqlangan.",
                selected_score=saved_score,
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

            FROM reporting_calls

            WHERE
                start_time >= ?

                AND

                start_time < ?

                AND

                COALESCE(
                    is_internal_contact,
                    0
                ) = 0
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        internal_row = conn.execute(
            """
            SELECT COUNT(*) AS internal_calls

            FROM reporting_calls

            WHERE
                start_time >= ?
                AND start_time < ?
                AND COALESCE(
                    is_internal_contact,
                    0
                ) = 1
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

                FROM reporting_calls

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

                            FROM reporting_calls AS old_call

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

                            FROM reporting_calls AS old_call

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
            WITH RECURSIVE
            parameters(cooldown_seconds) AS (
                VALUES (?)
            ),

            missed_ordered AS (
                SELECT
                    call.id,
                    call.client_key,
                    call.start_time,

                    ROW_NUMBER() OVER (
                        PARTITION BY call.client_key
                        ORDER BY call.start_time, call.id
                    ) AS row_number

                FROM reporting_calls AS call

                WHERE
                    call.direction = 0
                    AND call.answered = 0
                    AND COALESCE(
                        call.is_internal_contact,
                        0
                    ) = 0
                    AND call.client_key IS NOT NULL
                    AND call.client_key != ''
            ),

            missed_grouped (
                id,
                client_key,
                start_time,
                row_number,
                episode_number,
                episode_started_at
            ) AS (
                SELECT
                    id,
                    client_key,
                    start_time,
                    row_number,
                    1,
                    start_time

                FROM missed_ordered

                WHERE row_number = 1

                UNION ALL

                SELECT
                    current.id,
                    current.client_key,
                    current.start_time,
                    current.row_number,

                    CASE
                        WHEN current.start_time >=
                            previous.start_time
                            + parameters.cooldown_seconds
                        THEN previous.episode_number + 1
                        ELSE previous.episode_number
                    END,

                    CASE
                        WHEN current.start_time >=
                            previous.start_time
                            + parameters.cooldown_seconds
                        THEN current.start_time
                        ELSE previous.episode_started_at
                    END

                FROM missed_grouped AS previous

                JOIN missed_ordered AS current
                    ON current.client_key =
                        previous.client_key
                    AND current.row_number =
                        previous.row_number + 1

                CROSS JOIN parameters
            ),

            missed_episodes AS (
                SELECT
                    client_key,
                    episode_number,
                    MIN(start_time)
                        AS first_missed_time,
                    MAX(start_time)
                        AS last_missed_time,
                    MAX(start_time)
                        + parameters.cooldown_seconds
                        AS deadline

                FROM missed_grouped

                CROSS JOIN parameters

                GROUP BY
                    client_key,
                    episode_number
            ),

            period_episodes AS (
                SELECT *

                FROM missed_episodes

                WHERE
                    first_missed_time >= ?
                    AND first_missed_time < ?
            ),

            episode_flags AS (
                SELECT
                    episode.*,

                    EXISTS (
                        SELECT 1
                        FROM reporting_calls AS followup
                        WHERE
                            followup.client_key =
                                episode.client_key
                            AND COALESCE(
                                followup.is_internal_contact,
                                0
                            ) = 0
                            AND followup.start_time >
                                episode.last_missed_time
                            AND followup.start_time <
                                episode.deadline
                            AND followup.direction = 1
                    ) AS outgoing_attempted,

                    EXISTS (
                        SELECT 1
                        FROM reporting_calls AS followup
                        WHERE
                            followup.client_key =
                                episode.client_key
                            AND COALESCE(
                                followup.is_internal_contact,
                                0
                            ) = 0
                            AND followup.start_time >
                                episode.last_missed_time
                            AND followup.start_time <
                                episode.deadline
                            AND followup.direction = 1
                            AND followup.answered = 1
                    ) AS outgoing_success,

                    EXISTS (
                        SELECT 1
                        FROM reporting_calls AS followup
                        WHERE
                            followup.client_key =
                                episode.client_key
                            AND COALESCE(
                                followup.is_internal_contact,
                                0
                            ) = 0
                            AND followup.start_time >
                                episode.last_missed_time
                            AND followup.start_time <
                                episode.deadline
                            AND followup.direction = 0
                            AND followup.answered = 1
                    ) AS customer_called_back

                FROM period_episodes AS episode
            )

            SELECT
                COUNT(*) AS unique_missed_clients,

                COALESCE(
                    SUM(outgoing_attempted),
                    0
                ) AS missed_outgoing_attempted,

                COALESCE(
                    SUM(outgoing_success),
                    0
                ) AS missed_outgoing_success,

                COALESCE(
                    SUM(customer_called_back),
                    0
                ) AS missed_customer_called_back,

                COALESCE(
                    SUM(
                        CASE
                            WHEN
                                outgoing_success = 1
                                OR customer_called_back = 1
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS missed_contacted,

                COALESCE(
                    SUM(
                        CASE
                            WHEN
                                outgoing_attempted = 0
                                AND customer_called_back = 0
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS missed_not_processed

            FROM episode_flags
            """,
            (
                RESULT_COOLDOWN_HOURS
                * 60
                * 60,

                start_ts,
                end_ts,
            ),
        ).fetchone()

        # -------------------------------------------------
        # SALES
        # -------------------------------------------------

        sales_row = conn.execute(
            """
            WITH call_ranked AS (
                SELECT
                    call.client_window_id,
                    call.start_time,
                    call.talk_manager_code,
                    call.talk_manager_name,

                    ROW_NUMBER() OVER (
                        PARTITION BY
                            call.client_window_id
                        ORDER BY
                            call.start_time DESC,
                            call.id DESC
                    ) AS row_number

                FROM reporting_calls AS call

                WHERE
                    COALESCE(
                        call.is_internal_contact,
                        0
                    ) = 0
                    AND call.client_window_id
                        IS NOT NULL
            ),

            outcomes AS (
                SELECT
                    window.id,
                    result.id AS result_id,
                    result.result_category,

                    COALESCE(
                        result.attribution_time,
                        latest_call.start_time
                    ) AS attribution_time,

                    COALESCE(
                        NULLIF(
                            result.talk_manager_code,
                            ''
                        ),
                        NULLIF(
                            latest_call.talk_manager_code,
                            ''
                        )
                    ) AS manager_code

                FROM client_windows AS window

                JOIN call_ranked AS latest_call
                    ON latest_call.client_window_id =
                        window.id
                    AND latest_call.row_number = 1

                LEFT JOIN client_results AS result
                    ON result.client_window_id =
                        window.id
            )

            SELECT
                COUNT(*) AS eligible_windows,

                SUM(
                    CASE
                        WHEN result_category = 'bought'
                        THEN 1 ELSE 0
                    END
                ) AS bought,

                SUM(
                    CASE
                        WHEN result_category = 'lost'
                        THEN 1 ELSE 0
                    END
                ) AS not_bought,

                SUM(
                    CASE
                        WHEN result_category = 'pending'
                        THEN 1 ELSE 0
                    END
                ) AS pending,

                SUM(
                    CASE
                        WHEN result_category = 'non_target'
                        THEN 1 ELSE 0
                    END
                ) AS non_target,

                SUM(
                    CASE
                        WHEN result_id IS NULL
                        THEN 1 ELSE 0
                    END
                ) AS sale_unmarked,

                SUM(
                    CASE
                        WHEN manager_code IS NULL
                        THEN 1 ELSE 0
                    END
                ) AS manager_unmarked

            FROM outcomes

            WHERE
                attribution_time >= ?
                AND attribution_time < ?
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        manager_ratings_row = conn.execute(
            """
            SELECT
                COUNT(*) AS ratings_count,
                AVG(score) AS average_rating,
                SUM(CASE WHEN score = 1 THEN 1 ELSE 0 END) AS score_1,
                SUM(CASE WHEN score = 2 THEN 1 ELSE 0 END) AS score_2,
                SUM(CASE WHEN score = 3 THEN 1 ELSE 0 END) AS score_3,
                SUM(CASE WHEN score = 4 THEN 1 ELSE 0 END) AS score_4,
                SUM(CASE WHEN score = 5 THEN 1 ELSE 0 END) AS score_5

            FROM manager_ratings

            WHERE
                attribution_time >= ?
                AND attribution_time < ?
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchone()

        ratings_row = conn.execute(
            """
            WITH rating_ranked AS (
                SELECT
                    rating.*,

                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(
                                rating.id AS TEXT
                            )
                        )

                        ORDER BY
                            CASE
                                WHEN rating.score
                                    IS NOT NULL
                                THEN 0
                                ELSE 1
                            END,
                            COALESCE(
                                rating.rated_at,
                                rating.sms_sent_at,
                                rating.sms_reserved_at
                            ),
                            rating.id
                    ) AS row_number,

                    MAX(
                        CASE
                            WHEN
                                rating.sms_sent_at
                                    IS NOT NULL
                                OR rating.score
                                    IS NOT NULL
                            THEN 1
                            ELSE 0
                        END
                    ) OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(
                                rating.id AS TEXT
                            )
                        )
                    ) AS invited

                FROM call_ratings AS rating
            ),

            canonical_ratings AS (
                SELECT *
                FROM rating_ranked
                WHERE row_number = 1
            )

            SELECT
                COALESCE(
                    SUM(rating.invited),
                    0
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

            FROM canonical_ratings AS rating

            JOIN reporting_calls AS call
                ON call.id = rating.call_id

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

        real_sales = build_real_sales_analytics(
            conn,
            p,
        )

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

    pending = (
        sales_row["pending"]
        or 0
    )

    non_target = (
        sales_row["non_target"]
        or 0
    )

    sale_unmarked = (
        sales_row["sale_unmarked"]
        or 0
    )

    eligible_windows = (
        sales_row["eligible_windows"]
        or 0
    )

    marked_results = (
        bought
        + not_bought
    )

    processed_sale_conversion = (
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

    overall_denominator = max(
        eligible_windows
        - non_target,
        0,
    )

    sale_conversion = (
        round(
            (
                bought
                / overall_denominator
            )
            * 100,
            1,
        )
        if overall_denominator
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
                internal_row[
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

            "client_windows_30h":
                eligible_windows,

            "unique_missed_clients":
                missed_row[
                    "unique_missed_clients"
                ]
                or 0,

            "missed_called_back":
                missed_row[
                    "missed_outgoing_attempted"
                ]
                or 0,

            "missed_outgoing_attempted":
                missed_row[
                    "missed_outgoing_attempted"
                ]
                or 0,

            "missed_outgoing_success":
                missed_row[
                    "missed_outgoing_success"
                ]
                or 0,

            "missed_customer_called_back":
                missed_row[
                    "missed_customer_called_back"
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

            "pending":
                pending,

            "non_target":
                non_target,

            "sale_unmarked":
                sale_unmarked,

            "manager_unmarked":
                sales_row[
                    "manager_unmarked"
                ]
                or 0,

            "sale_conversion":
                sale_conversion,

            "processed_sale_conversion":
                processed_sale_conversion,

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

            "manager_average_rating": (
                round(
                    float(
                        manager_ratings_row[
                            "average_rating"
                        ]
                    ),
                    2,
                )
                if manager_ratings_row[
                    "average_rating"
                ] is not None
                else None
            ),

            "manager_ratings_count": (
                manager_ratings_row[
                    "ratings_count"
                ]
                or 0
            ),

            "manager_rating_distribution": {
                str(score): (
                    manager_ratings_row[
                        f"score_{score}"
                    ]
                    or 0
                )
                for score in range(1, 6)
            },

            "real_sales_configured": (
                real_sales["configured"]
            ),

            "real_sales_total": (
                real_sales["sales_total"]
            ),

            "real_sales_without_phone": (
                real_sales[
                    "sales_without_phone"
                ]
            ),

            "real_buyers_total": (
                real_sales["buyers_total"]
            ),

            "real_buyers_called_before_purchase": (
                real_sales[
                    "buyers_called_before_purchase"
                ]
            ),

            "real_buyers_without_prior_call": (
                real_sales[
                    "buyers_without_prior_call"
                ]
            ),

            "real_buyers_without_prior_call_rate": (
                real_sales[
                    "buyers_without_prior_call_rate"
                ]
            ),
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

            FROM client_results

            WHERE
                attribution_time >= ?

                AND

                attribution_time < ?

                AND

                result_category =
                    'lost'

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

                FROM reporting_calls

                WHERE
                    start_time >= ?

                    AND

                    start_time < ?

                    AND

                    COALESCE(
                        is_internal_contact,
                        0
                    ) = 0

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
                0,
                24,
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

                FROM reporting_calls

                WHERE
                    start_time >= ?

                    AND

                    start_time < ?

                    AND

                    COALESCE(
                        is_internal_contact,
                        0
                    ) = 0

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

    start_ts = p["start_ts"]
    end_ts = p["end_ts"]

    with connect_db() as conn:

        rows = conn.execute(
            """
            WITH rating_ranked AS (
                SELECT
                    rating.*,

                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(rating.id AS TEXT)
                        )
                        ORDER BY
                            CASE
                                WHEN rating.score IS NOT NULL
                                THEN 0 ELSE 1
                            END,
                            COALESCE(
                                rating.rated_at,
                                rating.sms_sent_at,
                                rating.sms_reserved_at
                            ),
                            rating.id
                    ) AS row_number,

                    MAX(
                        CASE
                            WHEN
                                rating.sms_sent_at IS NOT NULL
                                OR rating.score IS NOT NULL
                            THEN 1 ELSE 0
                        END
                    ) OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(rating.id AS TEXT)
                        )
                    ) AS invited

                FROM call_ratings AS rating
            ),

            canonical_ratings AS (
                SELECT *
                FROM rating_ranked
                WHERE row_number = 1
            )

            SELECT
                COALESCE(
                    NULLIF(
                        call.talk_manager_code,
                        ''
                    ),
                    'unmarked'
                ) AS manager_code,

                COALESCE(
                    NULLIF(
                        call.talk_manager_name,
                        ''
                    ),
                    'Не указан'
                ) AS manager,

                COUNT(*) AS calls,
                COUNT(
                    DISTINCT NULLIF(
                        call.client_key,
                        ''
                    )
                ) AS unique_clients,

                SUM(
                    CASE WHEN call.direction = 0
                    THEN 1 ELSE 0 END
                ) AS incoming,

                SUM(
                    CASE WHEN call.direction = 1
                    THEN 1 ELSE 0 END
                ) AS outgoing,

                SUM(
                    CASE
                        WHEN call.direction = 0
                            AND call.answered = 0
                        THEN 1 ELSE 0
                    END
                ) AS missed,

                SUM(
                    CASE
                        WHEN call.direction = 0
                            AND call.answered = 1
                        THEN 1 ELSE 0
                    END
                ) AS incoming_answered,

                SUM(
                    CASE WHEN call.answered = 1
                    THEN call.duration ELSE 0 END
                ) AS total_duration,

                COUNT(rating.score)
                    AS ratings_count,
                AVG(rating.score)
                    AS average_rating,
                COALESCE(
                    SUM(rating.invited),
                    0
                ) AS rating_invitations_sent

            FROM reporting_calls AS call

            LEFT JOIN canonical_ratings AS rating
                ON rating.call_id = call.id

            WHERE
                call.start_time >= ?
                AND call.start_time < ?
                AND COALESCE(
                    call.is_internal_contact,
                    0
                ) = 0

            GROUP BY
                manager_code,
                manager

            ORDER BY calls DESC
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchall()

        outcome_rows = conn.execute(
            """
            WITH call_ranked AS (
                SELECT
                    call.client_window_id,
                    call.start_time,
                    call.talk_manager_code,
                    call.talk_manager_name,

                    ROW_NUMBER() OVER (
                        PARTITION BY
                            call.client_window_id
                        ORDER BY
                            call.start_time DESC,
                            call.id DESC
                    ) AS row_number

                FROM reporting_calls AS call

                WHERE
                    COALESCE(
                        call.is_internal_contact,
                        0
                    ) = 0
                    AND call.client_window_id
                        IS NOT NULL
            ),

            outcomes AS (
                SELECT
                    window.id,
                    result.id AS result_id,
                    result.result_category,

                    COALESCE(
                        result.attribution_time,
                        latest_call.start_time
                    ) AS attribution_time,

                    COALESCE(
                        NULLIF(
                            result.talk_manager_code,
                            ''
                        ),
                        NULLIF(
                            latest_call.talk_manager_code,
                            ''
                        ),
                        'unmarked'
                    ) AS manager_code,

                    COALESCE(
                        NULLIF(
                            result.talk_manager_name,
                            ''
                        ),
                        NULLIF(
                            latest_call.talk_manager_name,
                            ''
                        ),
                        'Не указан'
                    ) AS manager

                FROM client_windows AS window

                JOIN call_ranked AS latest_call
                    ON latest_call.client_window_id =
                        window.id
                    AND latest_call.row_number = 1

                LEFT JOIN client_results AS result
                    ON result.client_window_id =
                        window.id
            )

            SELECT
                manager_code,
                manager,
                COUNT(*) AS eligible_windows,

                SUM(
                    CASE WHEN result_category = 'bought'
                    THEN 1 ELSE 0 END
                ) AS bought,

                SUM(
                    CASE WHEN result_category = 'lost'
                    THEN 1 ELSE 0 END
                ) AS not_bought,

                SUM(
                    CASE WHEN result_category = 'pending'
                    THEN 1 ELSE 0 END
                ) AS pending,

                SUM(
                    CASE WHEN result_category = 'non_target'
                    THEN 1 ELSE 0 END
                ) AS non_target,

                SUM(
                    CASE WHEN result_id IS NULL
                    THEN 1 ELSE 0 END
                ) AS sale_unmarked

            FROM outcomes

            WHERE
                attribution_time >= ?
                AND attribution_time < ?

            GROUP BY manager_code, manager
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchall()

        missed_rows = conn.execute(
            """
            WITH RECURSIVE
            parameters(cooldown_seconds) AS (
                VALUES (?)
            ),

            missed_ordered AS (
                SELECT
                    call.id,
                    call.client_key,
                    call.start_time,
                    call.talk_manager_code,
                    call.talk_manager_name,

                    ROW_NUMBER() OVER (
                        PARTITION BY call.client_key
                        ORDER BY call.start_time, call.id
                    ) AS row_number

                FROM reporting_calls AS call

                WHERE
                    call.direction = 0
                    AND call.answered = 0
                    AND COALESCE(
                        call.is_internal_contact,
                        0
                    ) = 0
                    AND call.client_key IS NOT NULL
                    AND call.client_key != ''
            ),

            missed_grouped (
                id,
                client_key,
                start_time,
                talk_manager_code,
                talk_manager_name,
                row_number,
                episode_number,
                episode_started_at
            ) AS (
                SELECT
                    id,
                    client_key,
                    start_time,
                    talk_manager_code,
                    talk_manager_name,
                    row_number,
                    1,
                    start_time

                FROM missed_ordered
                WHERE row_number = 1

                UNION ALL

                SELECT
                    current.id,
                    current.client_key,
                    current.start_time,
                    current.talk_manager_code,
                    current.talk_manager_name,
                    current.row_number,

                    CASE
                        WHEN current.start_time >=
                            previous.start_time
                            + parameters.cooldown_seconds
                        THEN previous.episode_number + 1
                        ELSE previous.episode_number
                    END,

                    CASE
                        WHEN current.start_time >=
                            previous.start_time
                            + parameters.cooldown_seconds
                        THEN current.start_time
                        ELSE previous.episode_started_at
                    END

                FROM missed_grouped AS previous

                JOIN missed_ordered AS current
                    ON current.client_key =
                        previous.client_key
                    AND current.row_number =
                        previous.row_number + 1

                CROSS JOIN parameters
            ),

            missed_ranked AS (
                SELECT
                    *,

                    ROW_NUMBER() OVER (
                        PARTITION BY
                            client_key,
                            episode_number
                        ORDER BY
                            CASE
                                WHEN talk_manager_code
                                    IS NOT NULL
                                    AND talk_manager_code != ''
                                THEN 0 ELSE 1
                            END,
                            start_time DESC,
                            id DESC
                    ) AS manager_rank

                FROM missed_grouped
            ),

            missed_episodes AS (
                SELECT
                    ranked.client_key,
                    ranked.episode_number,
                    MIN(ranked.start_time)
                        AS first_missed_time,
                    MAX(ranked.start_time)
                        AS last_missed_time,
                    MAX(ranked.start_time)
                        + parameters.cooldown_seconds
                        AS deadline,
                    COALESCE(
                        MAX(
                            CASE
                                WHEN manager_rank = 1
                                THEN talk_manager_code
                            END
                        ),
                        'unmarked'
                    ) AS manager_code,
                    COALESCE(
                        MAX(
                            CASE
                                WHEN manager_rank = 1
                                THEN talk_manager_name
                            END
                        ),
                        'Не указан'
                    ) AS manager

                FROM missed_ranked AS ranked
                CROSS JOIN parameters

                GROUP BY
                    ranked.client_key,
                    ranked.episode_number
            ),

            episode_flags AS (
                SELECT
                    episode.*,

                    EXISTS (
                        SELECT 1
                        FROM reporting_calls AS followup
                        WHERE
                            followup.client_key =
                                episode.client_key
                            AND COALESCE(
                                followup.is_internal_contact,
                                0
                            ) = 0
                            AND followup.start_time >
                                episode.last_missed_time
                            AND followup.start_time <
                                episode.deadline
                            AND followup.direction = 1
                    ) AS outgoing_attempted,

                    EXISTS (
                        SELECT 1
                        FROM reporting_calls AS followup
                        WHERE
                            followup.client_key =
                                episode.client_key
                            AND COALESCE(
                                followup.is_internal_contact,
                                0
                            ) = 0
                            AND followup.start_time >
                                episode.last_missed_time
                            AND followup.start_time <
                                episode.deadline
                            AND followup.direction = 1
                            AND followup.answered = 1
                    ) AS outgoing_success,

                    EXISTS (
                        SELECT 1
                        FROM reporting_calls AS followup
                        WHERE
                            followup.client_key =
                                episode.client_key
                            AND COALESCE(
                                followup.is_internal_contact,
                                0
                            ) = 0
                            AND followup.start_time >
                                episode.last_missed_time
                            AND followup.start_time <
                                episode.deadline
                            AND followup.direction = 0
                            AND followup.answered = 1
                    ) AS customer_called_back

                FROM missed_episodes AS episode

                WHERE
                    episode.first_missed_time >= ?
                    AND episode.first_missed_time < ?
            )

            SELECT
                manager_code,
                manager,
                COUNT(*) AS missed_episodes,
                SUM(outgoing_attempted)
                    AS missed_outgoing_attempted,
                SUM(outgoing_success)
                    AS missed_outgoing_success,
                SUM(customer_called_back)
                    AS missed_customer_called_back,
                SUM(
                    CASE
                        WHEN outgoing_attempted = 0
                            AND customer_called_back = 0
                        THEN 1 ELSE 0
                    END
                ) AS missed_not_processed

            FROM episode_flags

            GROUP BY manager_code, manager
            """,
            (
                RESULT_COOLDOWN_HOURS
                * 60
                * 60,
                start_ts,
                end_ts,
            ),
        ).fetchall()

        internal_rating_rows = conn.execute(
            """
            SELECT
                talk_manager_code AS manager_code,
                talk_manager_name AS manager,
                COUNT(*) AS ratings_count,
                AVG(score) AS average_rating

            FROM manager_ratings

            WHERE
                attribution_time >= ?
                AND attribution_time < ?

            GROUP BY
                talk_manager_code,
                talk_manager_name
            """,
            (
                start_ts,
                end_ts,
            ),
        ).fetchall()

        real_sales = build_real_sales_analytics(
            conn,
            p,
        )

    manager_outcomes = {
        row["manager_code"]: row
        for row in outcome_rows
    }

    manager_missed = {
        row["manager_code"]: row
        for row in missed_rows
    }

    manager_internal_ratings = {
        row["manager_code"]: row
        for row in internal_rating_rows
    }

    manager_real_buyers = real_sales[
        "manager_buyers"
    ]

    results = []

    for row in rows:

        manager_code = row["manager_code"]
        outcome = manager_outcomes.get(
            manager_code
        )
        missed_metrics = manager_missed.get(
            manager_code
        )
        internal_rating = (
            manager_internal_ratings.get(
                manager_code
            )
        )
        real_buyer_metrics = (
            manager_real_buyers.get(
                manager_code
            )
            or {}
        )

        incoming = row["incoming"] or 0
        incoming_answered = (
            row["incoming_answered"] or 0
        )

        eligible_windows = (
            outcome["eligible_windows"]
            if outcome else 0
        ) or 0
        bought = (
            outcome["bought"]
            if outcome else 0
        ) or 0
        not_bought = (
            outcome["not_bought"]
            if outcome else 0
        ) or 0
        pending = (
            outcome["pending"]
            if outcome else 0
        ) or 0
        non_target = (
            outcome["non_target"]
            if outcome else 0
        ) or 0
        sale_unmarked = (
            outcome["sale_unmarked"]
            if outcome else 0
        ) or 0

        ratings_count = (
            row["ratings_count"] or 0
        )
        invitations = (
            row["rating_invitations_sent"]
            or 0
        )
        processed = bought + not_bought
        overall_denominator = max(
            eligible_windows - non_target,
            0,
        )

        results.append(
            {
                "manager_code": manager_code,
                "manager": row["manager"],
                "calls": row["calls"] or 0,
                "unique_clients": (
                    row["unique_clients"] or 0
                ),
                "incoming": incoming,
                "outgoing": row["outgoing"] or 0,
                "missed": row["missed"] or 0,
                "internal_calls": 0,
                "answer_rate": (
                    round(
                        incoming_answered
                        / incoming
                        * 100,
                        1,
                    )
                    if incoming else 0
                ),
                "total_duration_seconds": (
                    row["total_duration"] or 0
                ),
                "eligible_windows": eligible_windows,
                "bought": bought,
                "not_bought": not_bought,
                "pending": pending,
                "non_target": non_target,
                "sale_unmarked": sale_unmarked,
                "sale_conversion": (
                    round(
                        bought
                        / overall_denominator
                        * 100,
                        1,
                    )
                    if overall_denominator else 0
                ),
                "processed_sale_conversion": (
                    round(
                        bought
                        / processed
                        * 100,
                        1,
                    )
                    if processed else 0
                ),
                "average_rating": (
                    round(
                        float(
                            row["average_rating"]
                        ),
                        2,
                    )
                    if row["average_rating"]
                        is not None
                    else None
                ),
                "ratings_count": ratings_count,
                "manager_average_rating": (
                    round(
                        float(
                            internal_rating[
                                "average_rating"
                            ]
                        ),
                        2,
                    )
                    if internal_rating
                    and internal_rating[
                        "average_rating"
                    ] is not None
                    else None
                ),
                "manager_ratings_count": (
                    internal_rating[
                        "ratings_count"
                    ]
                    if internal_rating
                    else 0
                ) or 0,
                "real_buyers": (
                    real_buyer_metrics.get(
                        "buyers",
                        0,
                    )
                ),
                "real_buyers_called_before_purchase": (
                    real_buyer_metrics.get(
                        "called_before_purchase",
                        0,
                    )
                ),
                "real_buyers_without_prior_call": (
                    real_buyer_metrics.get(
                        "without_prior_call",
                        0,
                    )
                ),
                "rating_invitations_sent": (
                    invitations
                ),
                "rating_response_rate": (
                    round(
                        ratings_count
                        / invitations
                        * 100,
                        1,
                    )
                    if invitations else 0
                ),
                "missed_episodes": (
                    missed_metrics[
                        "missed_episodes"
                    ]
                    if missed_metrics else 0
                ) or 0,
                "missed_outgoing_attempted": (
                    missed_metrics[
                        "missed_outgoing_attempted"
                    ]
                    if missed_metrics else 0
                ) or 0,
                "missed_outgoing_success": (
                    missed_metrics[
                        "missed_outgoing_success"
                    ]
                    if missed_metrics else 0
                ) or 0,
                "missed_customer_called_back": (
                    missed_metrics[
                        "missed_customer_called_back"
                    ]
                    if missed_metrics else 0
                ) or 0,
                "missed_not_processed": (
                    missed_metrics[
                        "missed_not_processed"
                    ]
                    if missed_metrics else 0
                ) or 0,
            }
        )

    present_manager_codes = {
        row["manager_code"]
        for row in results
    }

    for manager_code, metrics in (
        manager_real_buyers.items()
    ):
        if manager_code in present_manager_codes:
            continue

        results.append(
            {
                "manager_code": manager_code,
                "manager": metrics["manager"],
                "calls": 0,
                "unique_clients": 0,
                "incoming": 0,
                "outgoing": 0,
                "missed": 0,
                "internal_calls": 0,
                "answer_rate": 0,
                "total_duration_seconds": 0,
                "eligible_windows": 0,
                "bought": 0,
                "not_bought": 0,
                "pending": 0,
                "non_target": 0,
                "sale_unmarked": 0,
                "sale_conversion": 0,
                "processed_sale_conversion": 0,
                "average_rating": None,
                "ratings_count": 0,
                "manager_average_rating": None,
                "manager_ratings_count": 0,
                "real_buyers": metrics[
                    "buyers"
                ],
                "real_buyers_called_before_purchase": (
                    metrics[
                        "called_before_purchase"
                    ]
                ),
                "real_buyers_without_prior_call": (
                    metrics[
                        "without_prior_call"
                    ]
                ),
                "rating_invitations_sent": 0,
                "rating_response_rate": 0,
                "missed_episodes": 0,
                "missed_outgoing_attempted": 0,
                "missed_outgoing_success": 0,
                "missed_customer_called_back": 0,
                "missed_not_processed": 0,
            }
        )

    return {
        "results": results
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
            WITH rating_ranked AS (
                SELECT
                    rating.*,

                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(rating.id AS TEXT)
                        )
                        ORDER BY
                            CASE
                                WHEN rating.score IS NOT NULL
                                THEN 0 ELSE 1
                            END,
                            COALESCE(
                                rating.rated_at,
                                rating.sms_sent_at,
                                rating.sms_reserved_at
                            ),
                            rating.id
                    ) AS row_number,

                    MAX(
                        CASE
                            WHEN
                                rating.sms_sent_at IS NOT NULL
                                OR rating.score IS NOT NULL
                            THEN 1 ELSE 0
                        END
                    ) OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(rating.id AS TEXT)
                        )
                    ) AS invited

                FROM call_ratings AS rating
            ),

            canonical_ratings AS (
                SELECT *
                FROM rating_ranked
                WHERE row_number = 1
            )

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

                COALESCE(
                    SUM(rating.invited),
                    0
                )
                    AS invitations_sent

            FROM canonical_ratings AS rating

            JOIN reporting_calls AS call
                ON call.id = rating.call_id

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
# DAILY INTERNAL MANAGER RATINGS
# =========================================================

@app.get(
    "/stats/manager-ratings/daily"
)
def stats_manager_ratings_daily(
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
                    attribution_time,
                    'unixepoch',
                    '+5 hours'
                ) AS rating_date,
                talk_manager_code AS manager_code,
                talk_manager_name AS manager,
                COUNT(*) AS ratings_count,
                AVG(score) AS average_rating

            FROM manager_ratings

            WHERE
                attribution_time >= ?
                AND attribution_time < ?

            GROUP BY
                rating_date,
                talk_manager_code,
                talk_manager_name

            ORDER BY
                rating_date DESC,
                manager
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    return {
        "results": [
            {
                "date": row["rating_date"],
                "manager_code": row[
                    "manager_code"
                ],
                "manager": row["manager"],
                "average_rating": round(
                    float(
                        row["average_rating"]
                    ),
                    2,
                ),
                "ratings_count": (
                    row["ratings_count"]
                    or 0
                ),
            }
            for row in rows
        ]
    }


# =========================================================
# LEAD SOURCE STATISTICS
# =========================================================

@app.get(
    "/stats/sources"
)
def stats_sources(
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
            WITH outcomes AS (
                SELECT
                    window.id,
                    window.client_key,
                    COALESCE(
                        NULLIF(
                            window.lead_source_code,
                            ''
                        ),
                        'unmarked'
                    ) AS source_code,
                    result.id AS result_id,
                    result.result_category,
                    COALESCE(
                        result.attribution_time,
                        latest_call.start_time
                    ) AS attribution_time

                FROM client_windows AS window

                JOIN reporting_calls AS latest_call
                    ON latest_call.id =
                        window.latest_call_id

                LEFT JOIN client_results AS result
                    ON result.client_window_id =
                        window.id
            )

            SELECT
                source_code,
                COUNT(*) AS client_windows,
                COUNT(
                    DISTINCT client_key
                ) AS unique_clients,
                SUM(
                    CASE
                        WHEN result_category = 'bought'
                        THEN 1 ELSE 0
                    END
                ) AS bought,
                SUM(
                    CASE
                        WHEN result_category = 'lost'
                        THEN 1 ELSE 0
                    END
                ) AS lost,
                SUM(
                    CASE
                        WHEN result_category = 'pending'
                        THEN 1 ELSE 0
                    END
                ) AS pending,
                SUM(
                    CASE
                        WHEN result_category = 'non_target'
                        THEN 1 ELSE 0
                    END
                ) AS non_target,
                SUM(
                    CASE
                        WHEN result_id IS NULL
                        THEN 1 ELSE 0
                    END
                ) AS unmarked

            FROM outcomes

            WHERE
                attribution_time >= ?
                AND attribution_time < ?

            GROUP BY source_code

            ORDER BY client_windows DESC
            """,
            (
                p["start_ts"],
                p["end_ts"],
            ),
        ).fetchall()

    results = []

    for row in rows:

        client_windows = int(
            row["client_windows"]
            or 0
        )
        bought = int(
            row["bought"]
            or 0
        )
        lost = int(
            row["lost"]
            or 0
        )
        non_target = int(
            row["non_target"]
            or 0
        )
        eligible = max(
            0,
            client_windows - non_target,
        )
        completed = bought + lost

        results.append(
            {
                "source_code": row[
                    "source_code"
                ],
                "source": (
                    get_lead_source_name(
                        row["source_code"]
                    )
                    or "Не указан"
                ),
                "client_windows": client_windows,
                "unique_clients": int(
                    row["unique_clients"]
                    or 0
                ),
                "bought": bought,
                "lost": lost,
                "pending": int(
                    row["pending"]
                    or 0
                ),
                "non_target": non_target,
                "unmarked": int(
                    row["unmarked"]
                    or 0
                ),
                "overall_conversion": (
                    round(
                        bought / eligible * 100,
                        1,
                    )
                    if eligible
                    else 0
                ),
                "completed_conversion": (
                    round(
                        bought / completed * 100,
                        1,
                    )
                    if completed
                    else 0
                ),
            }
        )

    return {
        "results": results
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
                        device_name,
                        ''
                    ),
                    NULLIF(
                        user_login,
                        ''
                    ),
                    'Неизвестно'
                )
                    AS device,

                user_login,

                MAX(
                    account_name
                )
                    AS account_name,

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

            FROM reporting_calls

            WHERE
                start_time >= ?

                AND

                start_time < ?

                AND

                COALESCE(
                    is_internal_contact,
                    0
                ) = 0

            GROUP BY
                device,
                user_login,
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
                "device":
                    row["device"],

                "user_login":
                    row["user_login"]
                    or "",

                "account_name":
                    row["account_name"]
                    or "",

                "sim":
                    row["sim"],

                "sim_label":
                    resolve_call_device(
                        row["user_login"],
                        row["sim"],
                        row["src_slot"],
                    )["sim_label"],

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
            WITH rating_ranked AS (
                SELECT
                    rating.*,

                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            CAST(
                                rating.client_window_id
                                AS TEXT
                            ),
                            'rating:'
                            || CAST(rating.id AS TEXT)
                        )
                        ORDER BY
                            CASE
                                WHEN rating.score IS NOT NULL
                                THEN 0 ELSE 1
                            END,
                            COALESCE(
                                rating.rated_at,
                                rating.sms_sent_at,
                                rating.sms_reserved_at
                            ),
                            rating.id
                    ) AS row_number

                FROM call_ratings AS rating
            ),

            canonical_ratings AS (
                SELECT *
                FROM rating_ranked
                WHERE row_number = 1
            )

            SELECT

                calls.id AS id,
                db_call_id,

                client_number,
                client_name,

                is_internal_contact,
                internal_contact_name,

                direction,
                answered,

                calls.talk_manager_name
                    AS talk_manager_name,

                calls.device_name,
                calls.user_login,
                src_number,
                calls.src_slot,

                client_window.lead_source_code,
                client_window.lead_source_origin,

                start_time,

                duration,

                client_result.sale_status
                    AS sale_status,

                client_result.no_sale_reason
                    AS no_sale_reason,

                client_result.result_category
                    AS result_category,

                rating.score
                    AS customer_rating,

                manager_rating.score
                    AS manager_rating,

                rating.sms_status
                    AS rating_sms_status

            FROM reporting_calls AS calls

            LEFT JOIN canonical_ratings AS rating
                ON rating.call_id = calls.id

            LEFT JOIN client_results
                AS client_result

                ON client_result.client_window_id =
                    calls.client_window_id

            LEFT JOIN manager_ratings
                AS manager_rating

                ON manager_rating.client_window_id =
                    calls.client_window_id

            LEFT JOIN client_windows
                AS client_window

                ON client_window.id =
                    calls.client_window_id

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

                "device":
                    row["device_name"]
                    or row["user_login"]
                    or "—",

                "sim_label":
                    resolve_call_device(
                        row["user_login"],
                        row["src_number"],
                        row["src_slot"],
                    )["sim_label"],

                "lead_source_code":
                    row["lead_source_code"],

                "lead_source":
                    get_lead_source_name(
                        row["lead_source_code"]
                    )
                    or "—",

                "lead_source_origin":
                    row["lead_source_origin"]
                    or "",

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

                "result_category":
                    row[
                        "result_category"
                    ],

                "customer_rating":
                    row[
                        "customer_rating"
                    ],

                "manager_rating":
                    row[
                        "manager_rating"
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
                call.device_name,
                call.provider_src_number,
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
                source_call.lead_source_auto_code,
                source_call.lead_source_auto_method,
                source_call.lead_source_auto_confidence,
                source_call.lead_source_auto_evidence,
                source_call.lead_source_manual_code,
                client_window.lead_source_revision,
                source_call.lead_source_marked_at,
                source_call.lead_source_marked_username,
                client_window.lead_source_code,
                client_window.lead_source_origin,
                client_window.lead_source_call_id,
                client_window.lead_source_confidence,
                client_window.lead_source_evidence,
                client_result.sale_status,
                client_result.no_sale_reason,
                client_result.no_sale_reason_code,
                client_result.marked_at
                    AS sale_marked_at,
                client_result.marked_username
                    AS sale_marked_username,
                manager_rating.score
                    AS manager_rating,
                manager_rating.marked_at
                    AS manager_rating_marked_at,
                manager_rating.marked_username
                    AS manager_rating_marked_username,
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

                ,transcription.status
                    AS transcription_status
                ,transcription.model
                    AS transcription_model
                ,transcription.language
                    AS transcription_language
                ,transcription.transcript_text
                    AS transcript_text
                ,transcription.error
                    AS transcription_error
                ,transcription.completed_at
                    AS transcription_completed_at
                ,transcription.lead_source_candidates_json
                    AS lead_source_candidates_json

            FROM calls AS call

            JOIN call_ratings AS rating
                ON rating.call_id = call.id

            LEFT JOIN client_results
                AS client_result

                ON client_result.client_window_id =
                    call.client_window_id

            LEFT JOIN client_windows
                AS client_window

                ON client_window.id =
                    call.client_window_id

            LEFT JOIN manager_ratings
                AS manager_rating

                ON manager_rating.client_window_id =
                    call.client_window_id

            LEFT JOIN calls AS source_call

                ON source_call.id =
                    client_window.lead_source_call_id

            LEFT JOIN call_transcriptions
                AS transcription

                ON transcription.call_id =
                    call.id

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
                "title": "Менеджер и внутренняя оценка",
                "items": {
                    "Менеджер": row["talk_manager_name"] or "—",
                    "Код менеджера": row["talk_manager_code"] or "—",
                    "Менеджер отмечен": format_uz_datetime(row["manager_marked_at"]),
                    "Кто отметил менеджера": row["manager_marked_username"] or "—",
                    "Внутренняя оценка": (
                        f'{row["manager_rating"]} из 5'
                        if row["manager_rating"] is not None
                        else "—"
                    ),
                    "Оценка поставлена": format_uz_datetime(
                        row["manager_rating_marked_at"]
                    ),
                    "Кто поставил оценку": (
                        row["manager_rating_marked_username"]
                        or "—"
                    ),
                },
            },
            {
                "title": "Источник клиента",
                "items": {
                    "Основной источник": (
                        get_lead_source_name(
                            row["lead_source_code"]
                        )
                        or "Не указан"
                    ),
                    "Способ определения": row["lead_source_origin"] or "—",
                    "Звонок источника": row["lead_source_call_id"] or "—",
                    "Уверенность": row["lead_source_confidence"],
                    "Основание": row["lead_source_evidence"] or "—",
                    "Автоисточник": (
                        get_lead_source_name(
                            row["lead_source_auto_code"]
                        )
                        or "—"
                    ),
                    "Автоправило": row["lead_source_auto_method"] or "—",
                    "Уверенность авто": row["lead_source_auto_confidence"],
                    "Основание авто": row["lead_source_auto_evidence"] or "—",
                    "Ручной источник": (
                        get_lead_source_name(
                            row["lead_source_manual_code"]
                        )
                        or "—"
                    ),
                    "Источник изменён": format_uz_datetime(row["lead_source_marked_at"]),
                    "Кто изменил": row["lead_source_marked_username"] or "—",
                    "Ревизия": row["lead_source_revision"],
                },
            },
            {
                "title": "SIM и аккаунт",
                "items": {
                    "Устройство": row["device_name"] or "—",
                    "SIM / номер телефона": row["src_number"] or "—",
                    "Номер от провайдера": row["provider_src_number"] or "—",
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
                "title": "Расшифровка звонка",
                "items": {
                    "Статус": row["transcription_status"] or "Не запускалась",
                    "Модель": row["transcription_model"] or "—",
                    "Язык": row["transcription_language"] or "—",
                    "Готово": format_uz_datetime(row["transcription_completed_at"]),
                    "Текст": row["transcript_text"] or "—",
                    "Кандидаты источника": parse_saved_json(
                        row["lead_source_candidates_json"]
                    ),
                    "Ошибка": row["transcription_error"] or "—",
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
# DASHBOARD DEVICE MANAGERS
# =========================================================

def require_dashboard_same_origin(
    request: Request,
):

    origin = request.headers.get(
        "Origin",
        "",
    ).strip()

    if origin:
        parsed_origin = urlparse(origin)
        public_host = urlparse(
            PUBLIC_BASE_URL
        ).netloc.casefold()
        request_host = request.headers.get(
            "Host",
            "",
        ).casefold()
        origin_host = parsed_origin.netloc.casefold()

        if (
            parsed_origin.scheme not in {
                "http",
                "https",
            }
            or not origin_host
            or origin_host not in {
                public_host,
                request_host,
            }
        ):
            raise HTTPException(
                status_code=403,
                detail="Недопустимый источник запроса",
            )


@app.get(
    "/admin/device-managers"
)
def admin_device_managers(
):

    return {
        "devices": (
            list_device_manager_assignments()
        ),
        "managers": [
            {
                "code": code,
                "name": name,
            }
            for code, name in MANAGERS.items()
        ],
    }


@app.post(
    "/admin/device-managers"
)
async def update_admin_device_manager(
    request: Request,
):

    require_dashboard_same_origin(
        request
    )

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Некорректный JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Некорректные данные",
        )

    user_login = normalize_user_login(
        payload.get("user_login")
    )
    action = str(
        payload.get("action")
        or "assign"
    ).strip().casefold()

    try:
        if action in {
            "reset",
            "reset_temporary",
        }:
            device = (
                reset_device_manager_assignment(
                    user_login,
                    changed_by="dashboard",
                )
            )
        elif action in {
            "assign",
            "temporary",
        }:
            device = (
                set_device_manager_assignment(
                    user_login,
                    payload.get("manager_code"),
                    changed_by="dashboard",
                )
            )
        elif action == "permanent":
            device = (
                set_permanent_device_manager(
                    user_login,
                    payload.get("manager_code"),
                    changed_by="dashboard",
                )
            )
        elif action == "reset_permanent":
            device = (
                reset_permanent_device_manager(
                    user_login,
                    changed_by="dashboard",
                )
            )
        else:
            raise ValueError(
                "Неизвестное действие"
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    until = device[
        "effective_until"
    ]
    device["effective_until_label"] = (
        datetime.fromtimestamp(
            until,
            UZ_TZ,
        ).strftime(
            "%d.%m.%Y %H:%M"
        )
        if until
        else None
    )

    return {
        "ok": True,
        "device": device,
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

.sale-pending {
    color: #d9b565;
}

.sale-non-target {
    color: #aaa;
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

.device-manager-select {
    min-height: 42px;
    border: 1px solid #3a3a3a;
    border-radius: 9px;
    background: #111;
    color: #fff;
    padding: 9px 11px;
}

.admin-action,
.admin-reset {
    min-height: 42px;
    border: 1px solid #d9b565;
    border-radius: 9px;
    padding: 9px 12px;
    cursor: pointer;
    font-weight: 700;
}

.admin-action {
    background: #d9b565;
    color: #111;
}

.admin-reset {
    background: transparent;
    color: #d9b565;
}

.admin-action:disabled,
.admin-reset:disabled {
    cursor: wait;
    opacity: .55;
}

.device-manager-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
}

.device-manager-card {
    padding: 16px;
    border: 1px solid #303030;
    border-radius: 13px;
    background: #151515;
}

.device-manager-name {
    margin-bottom: 5px;
    font-size: 19px;
    font-weight: 750;
}

.device-manager-status {
    min-height: 38px;
    margin-bottom: 12px;
    color: #aaa;
    font-size: 13px;
    line-height: 1.45;
}

.device-manager-status.temporary {
    color: #d9b565;
}

.device-manager-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.device-manager-select {
    flex: 1;
    min-width: 120px;
}

.admin-message {
    margin-top: 12px;
    min-height: 20px;
    color: #aaa;
    font-size: 13px;
}

.admin-message.error {
    color: #ffabab;
}

.hidden {
    display: none !important;
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


<div class="panel">

    <div class="panel-header">

        <div>
            <h2>Кто работает с телефонами</h2>
            <div class="subtitle">
                «На сегодня» действует до 00:00.
                «Постоянно» меняет менеджера для всех новых звонков.
                История звонков не изменяется.
            </div>
        </div>

    </div>

    <div
        class="device-manager-grid"
        id="device_manager_grid"
    ></div>

    <div
        class="admin-message"
        id="device_manager_message"
        aria-live="polite"
    ></div>

</div>


<div class="grid">

    <div class="card">
        <div class="label">
            Клиентские звонки
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
            Клиентские обращения (30 ч)
        </div>

        <div
            class="value"
            id="client_windows_30h"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Пропущенные обращения (30 ч)
        </div>

        <div
            class="value"
            id="unique_missed_clients"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Попытались перезвонить за 30 ч
        </div>

        <div
            class="value"
            id="missed_called_back"
        >—</div>
    </div>


    <div class="card good">
        <div class="label">
            Успешно дозвонились за 30 ч
        </div>

        <div
            class="value"
            id="missed_outgoing_success"
        >—</div>
    </div>


    <div class="card good">
        <div class="label">
            Клиент сам перезвонил за 30 ч
        </div>

        <div
            class="value"
            id="missed_customer_called_back"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Связались любым способом за 30 ч
        </div>

        <div
            class="value"
            id="missed_contacted"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Не обработано за 30 ч
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
    Реальные продажи
</h2>


<div class="grid">

    <div class="card good">
        <div class="label">Продаж</div>
        <div class="value" id="real_sales_total">—</div>
    </div>

    <div class="card good">
        <div class="label">Уникальных покупателей</div>
        <div class="value" id="real_buyers_total">—</div>
    </div>

    <div class="card">
        <div class="label">Звонили до покупки</div>
        <div class="value" id="real_buyers_called_before_purchase">—</div>
    </div>

    <div class="card">
        <div class="label">Купили без звонка</div>
        <div class="value" id="real_buyers_without_prior_call">—</div>
    </div>

    <div class="card">
        <div class="label">Доля без звонка</div>
        <div class="value" id="real_buyers_without_prior_call_rate">—</div>
    </div>

    <div class="card">
        <div class="label">Продаж без номера</div>
        <div class="value" id="real_sales_without_phone">—</div>
    </div>

</div>


<div class="panel">

    <div class="panel-header">
        <h2>Покупатели и продажи</h2>
    </div>

    <div class="table-wrap">

        <table>

            <thead>
            <tr>
                <th>Дата</th>
                <th>Телефон</th>
                <th>Менеджер</th>
                <th>Модель</th>
                <th>Звонил до покупки</th>
            </tr>
            </thead>

            <tbody id="real_sales_body"></tbody>

        </table>

    </div>

</div>


<h2 class="section-title">
    Оценки менеджеров
</h2>


<div class="grid">

    <div class="card good">
        <div class="label">
            Моя средняя оценка менеджеров
        </div>

        <div
            class="value"
            id="manager_average_rating"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Поставлено моих оценок
        </div>

        <div
            class="value"
            id="manager_ratings_count"
        >—</div>
    </div>


    <div class="card">
        <div class="label">
            Мои оценки 1–5
        </div>

        <div
            class="value"
            id="manager_rating_distribution"
            style="font-size: 18px"
        >—</div>
    </div>

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
                <th>Покупатели</th>
                <th>Купили без звонка</th>
                <th>Вход.</th>
                <th>Исход.</th>
                <th>Пропущ.</th>
                <th>Ответ %</th>
                <th>Попытка за 30ч</th>
                <th>Дозвонился за 30ч</th>
                <th>Клиент перезвонил</th>
                <th>Не обработано</th>
                <th>Моя оценка</th>
                <th>Моих оценок</th>
                <th>Оценка клиентов</th>
                <th>Оценок клиентов</th>
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
            Мои оценки менеджеров по дням
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
            </tr>

            </thead>

            <tbody
                id="manager_ratings_daily_body"
            ></tbody>

        </table>

    </div>

</div>


<div class="panel">

    <div class="panel-header">

        <h2>
            Оценки клиентов по дням
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
            Источники клиентов
        </h2>

    </div>

    <div class="table-wrap">

        <table>

            <thead>

            <tr>
                <th>Источник</th>
                <th>Группы 30 ч</th>
                <th>Клиенты</th>
            </tr>

            </thead>

            <tbody
                id="sources_body"
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

                <th>Устройство</th>
                <th>Название SIM</th>
                <th>Номер</th>
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
                <th>Источник</th>
                <th>SIM</th>
                <th>Разговор</th>
                <th>Моя оценка</th>
                <th>Оценка клиента</th>
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


function setDeviceManagerMessage(
    message,
    isError = false
) {

    const element =
        document.getElementById(
            "device_manager_message"
        );

    element.textContent =
        message || "";

    element.classList.toggle(
        "error",
        Boolean(isError)
    );
}


async function deviceManagerRequest(
    method = "GET",
    payload = null
) {

    const options = {
        method,
        cache: "no-store",
        headers: {},
    };

    if (payload !== null) {
        options.headers["Content-Type"] =
            "application/json";
        options.body = JSON.stringify(
            payload
        );
    }

    const response = await fetch(
        "/admin/device-managers",
        options
    );

    let data = {};

    try {
        data = await response.json();
    } catch (error) {
        data = {};
    }

    if (!response.ok) {
        const requestError = new Error(
            data.detail
            || (
                "Ошибка HTTP "
                + response.status
            )
        );

        requestError.status =
            response.status;

        throw requestError;
    }

    return data;
}


function renderDeviceManagers(
    data
) {

    const grid =
        document.getElementById(
            "device_manager_grid"
        );

    grid.textContent = "";

    data.devices.forEach(
        device => {
            const card =
                document.createElement(
                    "div"
                );

            card.className =
                "device-manager-card";

            const name =
                document.createElement(
                    "div"
                );

            name.className =
                "device-manager-name";
            name.textContent =
                device.device_name;

            const status =
                document.createElement(
                    "div"
                );

            status.className =
                "device-manager-status";

            if (device.temporary) {
                status.classList.add(
                    "temporary"
                );
                status.textContent =
                    "Сейчас: "
                    + device.manager_name
                    + ". Временно до "
                    + device.effective_until_label
                    + ". Постоянный: "
                    + device.permanent_manager_name;
            } else {
                status.textContent =
                    "Сейчас: "
                    + device.manager_name
                    + (
                        device.permanent_custom
                        ? " (постоянный)"
                        : " (исходный)"
                    );
            }

            const actions =
                document.createElement(
                    "div"
                );

            actions.className =
                "device-manager-actions";

            const select =
                document.createElement(
                    "select"
                );

            select.className =
                "device-manager-select";
            select.setAttribute(
                "aria-label",
                "Менеджер для "
                + device.device_name
            );

            data.managers.forEach(
                manager => {
                    const option =
                        document.createElement(
                            "option"
                        );

                    option.value =
                        manager.code;
                    option.textContent =
                        manager.name;
                    option.selected =
                        manager.code
                        === device.manager_code;

                    select.appendChild(
                        option
                    );
                }
            );

            const temporaryButton =
                document.createElement(
                    "button"
                );

            temporaryButton.type = "button";
            temporaryButton.className =
                "admin-action";
            temporaryButton.textContent =
                "На сегодня";

            const permanentButton =
                document.createElement(
                    "button"
                );

            permanentButton.type = "button";
            permanentButton.className =
                "admin-action";
            permanentButton.textContent =
                "Постоянно";

            const resetTemporaryButton =
                document.createElement(
                    "button"
                );

            resetTemporaryButton.type = "button";
            resetTemporaryButton.className =
                "admin-reset";
            resetTemporaryButton.textContent =
                "Убрать временного";
            resetTemporaryButton.disabled =
                !device.temporary;

            const resetPermanentButton =
                document.createElement(
                    "button"
                );

            resetPermanentButton.type = "button";
            resetPermanentButton.className =
                "admin-reset";
            resetPermanentButton.textContent =
                "Вернуть исходного";
            resetPermanentButton.disabled =
                !device.permanent_custom;

            const setBusy = busy => {
                select.disabled = busy;
                temporaryButton.disabled = busy;
                permanentButton.disabled = busy;
                resetTemporaryButton.disabled =
                    busy || !device.temporary;
                resetPermanentButton.disabled =
                    busy || !device.permanent_custom;
            };

            const updateAssignment = async (
                action,
                loadingMessage,
                successMessage,
                includeManager = false
            ) => {
                setBusy(true);
                setDeviceManagerMessage(
                    loadingMessage
                );

                const payload = {
                    action,
                    user_login:
                        device.user_login,
                };

                if (includeManager) {
                    payload.manager_code =
                        select.value;
                }

                try {
                    await deviceManagerRequest(
                        "POST",
                        payload
                    );

                    setDeviceManagerMessage(
                        device.device_name
                        + ": "
                        + successMessage
                    );
                    await loadDeviceManagers(
                        false
                    );
                } catch (error) {
                    setDeviceManagerMessage(
                        error.message,
                        true
                    );
                    setBusy(false);
                }
            };

            temporaryButton.addEventListener(
                "click",
                () => updateAssignment(
                    "temporary",
                    "Сохраняю менеджера на сегодня…",
                    "менеджер назначен до 00:00",
                    true
                )
            );

            permanentButton.addEventListener(
                "click",
                () => updateAssignment(
                    "permanent",
                    "Сохраняю постоянного менеджера…",
                    "постоянный менеджер сохранён",
                    true
                )
            );

            resetTemporaryButton.addEventListener(
                "click",
                () => updateAssignment(
                    "reset_temporary",
                    "Убираю временное назначение…",
                    "временное назначение убрано"
                )
            );

            resetPermanentButton.addEventListener(
                "click",
                () => updateAssignment(
                    "reset_permanent",
                    "Возвращаю исходного менеджера…",
                    "исходный менеджер возвращён"
                )
            );

            actions.appendChild(
                select
            );
            actions.appendChild(
                temporaryButton
            );
            actions.appendChild(
                permanentButton
            );
            actions.appendChild(
                resetTemporaryButton
            );
            actions.appendChild(
                resetPermanentButton
            );

            card.appendChild(name);
            card.appendChild(status);
            card.appendChild(actions);
            grid.appendChild(card);
        }
    );

}


async function loadDeviceManagers(
    showLoading = true
) {

    if (showLoading) {
        setDeviceManagerMessage(
            "Загружаю назначения…"
        );
    }

    try {
        const data =
            await deviceManagerRequest();

        renderDeviceManagers(data);

        if (showLoading) {
            setDeviceManagerMessage("");
        }
    } catch (error) {
        setDeviceManagerMessage(
            error.message,
            true
        );
    }
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

        client_windows_30h:
            s.client_windows_30h,

        unique_missed_clients:
            s.unique_missed_clients,

        missed_called_back:
            s.missed_called_back,

        missed_outgoing_success:
            s.missed_outgoing_success,

        missed_customer_called_back:
            s.missed_customer_called_back,

        missed_contacted:
            s.missed_contacted,

        missed_not_processed:
            s.missed_not_processed,

        real_sales_total:
            s.real_sales_total,

        real_buyers_total:
            s.real_buyers_total,

        real_buyers_called_before_purchase:
            s.real_buyers_called_before_purchase,

        real_buyers_without_prior_call:
            s.real_buyers_without_prior_call,

        real_buyers_without_prior_call_rate:
            s.real_buyers_without_prior_call_rate
            + "%",

        real_sales_without_phone:
            s.real_sales_without_phone,


        manager_average_rating:
            s.manager_average_rating === null
                ? "—"
                : Number(
                    s.manager_average_rating
                ).toFixed(2)
                + " ★",

        manager_ratings_count:
            s.manager_ratings_count,

        manager_rating_distribution:
            [1, 2, 3, 4, 5]
                .map(
                    score => (
                        score
                        + "★: "
                        + (
                            s.manager_rating_distribution[
                                String(score)
                            ]
                            || 0
                        )
                    )
                )
                .join(" · "),

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

                row.real_buyers,

                row.real_buyers_without_prior_call,

                row.incoming,

                row.outgoing,

                row.missed,

                row.answer_rate
                + "%",

                row.missed_outgoing_attempted,

                row.missed_outgoing_success,

                row.missed_customer_called_back,

                row.missed_not_processed,

                row.manager_average_rating === null
                    ? "—"
                    : Number(
                        row.manager_average_rating
                    ).toFixed(2)
                    + " ★",

                row.manager_ratings_count,

                row.average_rating === null
                    ? "—"
                    : Number(
                        row.average_rating
                    ).toFixed(2)
                    + " ★",

                row.ratings_count,

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


async function loadRealSales() {

    const data =
        await getJson(
            "/stats/sales/customers"
        );

    const body =
        document.getElementById(
            "real_sales_body"
        );

    body.innerHTML = "";

    if (
        !data.configured
        || data.results.length === 0
    ) {
        const tr =
            document.createElement("tr");
        const td =
            document.createElement("td");

        td.colSpan = 5;
        td.className = "empty";
        td.textContent = data.configured
            ? "Пока нет продаж за выбранный период"
            : "База бота продаж пока не подключена";

        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }

    data.results.forEach(
        row => {
            const tr =
                document.createElement("tr");

            const callStatus =
                !row.has_phone
                    ? "Нет номера"
                    : row.called_before_purchase
                        ? "Да"
                        : "Нет";

            [
                row.sale_date,
                row.phone,
                row.manager,
                row.product_label,
                callStatus,
            ].forEach(
                value => {
                    const td =
                        document.createElement("td");
                    td.textContent = value;
                    tr.appendChild(td);
                }
            );

            body.appendChild(tr);
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


async function loadManagerRatingsDaily() {

    const data =
        await getJson(
            "/stats/manager-ratings/daily"
        );

    const body =
        document.getElementById(
            "manager_ratings_daily_body"
        );

    body.innerHTML = "";

    if (data.results.length === 0) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 4;
        td.className = "empty";
        td.textContent = "Пока нет моих оценок";
        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }

    data.results.forEach(
        row => {
            const tr = document.createElement("tr");

            [
                row.date,
                row.manager,
                Number(
                    row.average_rating
                ).toFixed(2) + " ★",
                row.ratings_count,
            ].forEach(
                value => {
                    const td = document.createElement("td");
                    td.textContent = value;
                    tr.appendChild(td);
                }
            );

            body.appendChild(tr);
        }
    );
}


async function loadSources() {

    const data =
        await getJson(
            "/stats/sources"
        );

    const body =
        document.getElementById(
            "sources_body"
        );

    body.innerHTML = "";

    if (data.results.length === 0) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 3;
        td.className = "empty";
        td.textContent = "Пока нет данных";
        tr.appendChild(td);
        body.appendChild(tr);
        return;
    }

    data.results.forEach(
        row => {
            const tr = document.createElement("tr");

            const values = [
                row.source,
                row.client_windows,
                row.unique_clients,
            ];

            values.forEach(
                value => {
                    const td = document.createElement("td");
                    td.textContent = value;
                    tr.appendChild(td);
                }
            );

            body.appendChild(tr);
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

                row.device,

                row.sim_label,

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


            const leadSource =
                document.createElement(
                    "td"
                );


            leadSource.textContent =
                row.lead_source;


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

            } else {
                customerRating.textContent =
                    "—";
            }


            const managerRating =
                document.createElement(
                    "td"
                );


            managerRating.textContent =
                row.manager_rating === null
                    ? "—"
                    : row.manager_rating
                        + " ★";


            const sim =
                document.createElement(
                    "td"
                );


            sim.className =
                "phone";


            sim.textContent =
                row.device
                + " · "
                + row.sim_label
                + " · "
                + row.sim;


            const duration =
                document.createElement(
                    "td"
                );


            duration.textContent =
                formatDuration(
                    row.duration
                );


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
                leadSource
            );


            tr.appendChild(
                sim
            );


            tr.appendChild(
                duration
            );


            tr.appendChild(
                managerRating
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

                loadRealSales(),

                loadManagers(),

                loadManagerRatingsDaily(),

                loadRatingsDaily(),

                loadSources(),

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


loadDeviceManagers();


loadAll();


setInterval(
    loadAll,
    30000
);


setInterval(
    () => loadDeviceManagers(false),
    60000
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

    webhook_started_monotonic = (
        time.monotonic()
    )
    webhook_received_at = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    if MOIZVONKI_WEBHOOK_SECRET:
        received_secret = (
            request.headers.get(
                "X-Moizvonki-Webhook-Secret"
            )
            or request.headers.get(
                "X-Webhook-Secret"
            )
            or request.query_params.get(
                "secret"
            )
            or ""
        )

        if not secrets.compare_digest(
            received_secret,
            MOIZVONKI_WEBHOOK_SECRET,
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid Moizvonki secret",
            )

    data = await request.json()

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

    def event_timestamp(
        key: str,
    ) -> int | None:
        try:
            value = int(
                event.get(
                    key
                )
                or 0
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

        return value or None

    event_ready_at = (
        event_timestamp(
            "upload_time"
        )
        or event_timestamp(
            "event_created"
        )
    )
    call_ended_at = event_timestamp(
        "end_time"
    )

    phone_to_provider_seconds = (
        event_ready_at
        - call_ended_at
        if event_ready_at
        and call_ended_at
        else None
    )
    provider_to_webhook_seconds = (
        webhook_received_at
        - event_ready_at
        if event_ready_at
        else None
    )
    total_delivery_seconds = (
        webhook_received_at
        - call_ended_at
        if call_ended_at
        else None
    )

    print(
        "MOIZVONKI EVENT:",
        webhook.get(
            "action"
        ),
        event.get(
            "db_call_id"
        ),
        webhook.get(
            "user_login"
        ),
    )

    print(
        "MOIZVONKI DELIVERY LATENCY:",
        json.dumps(
            {
                "db_call_id": event.get(
                    "db_call_id"
                ),
                "phone_to_provider_seconds": (
                    phone_to_provider_seconds
                ),
                "provider_to_webhook_seconds": (
                    provider_to_webhook_seconds
                ),
                "total_seconds": (
                    total_delivery_seconds
                ),
            },
            ensure_ascii=False,
        ),
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

    direction = int(
        event.get(
            "direction",
            0,
        )
        or 0
    )

    recording = event.get(
        "recording"
    )

    # -----------------------------------------------------
    # DURATION
    # -----------------------------------------------------

    (
        talk_duration,
        duration_source,
    ) = get_talk_duration(
        event,
        None,
    )

    # -----------------------------------------------------
    # SAVE
    # -----------------------------------------------------

    save_started = time.monotonic()
    save_result = save_call(
        webhook,
        event,
        talk_duration,
        duration_source,
    )
    save_seconds = (
        time.monotonic()
        - save_started
    )

    call_id = (
        save_result[
            "call_id"
        ]
    )

    saved_call = get_call(
        call_id
    )

    if saved_call:
        answered = int(
            saved_call["answered"]
            or 0
        )
        direction = int(
            saved_call["direction"]
            or 0
        )
        recording = (
            saved_call["recording"]
            or recording
        )

    telegram_already_sent = (
        save_result[
            "already_sent"
        ]
    )

    telegram_claimed = (
        save_result[
            "telegram_claimed"
        ]
    )

    is_internal_contact = (
        save_result[
            "is_internal_contact"
        ]
    )

    client_number = (
        (
            saved_call["client_number"]
            if saved_call
            else event.get(
                "client_number"
            )
        )
        or ""
    )

    sender_user_login = (
        (
            saved_call["user_login"]
            if saved_call
            else None
        )
        or webhook.get(
            "user_login"
        )
        or MOIZVONKI_USER_NAME
    )

    call_start_time = (
        (
            saved_call["start_time"]
            if saved_call
            else None
        )
        or event.get(
            "start_time"
        )
    )

    voice_bytes = None

    try:
        transcription = enqueue_transcription(
            call_id
        )
        transcription_status = transcription[
            "reason"
        ]
    except Exception as exc:
        transcription_status = "queue_error"
        print(
            "TRANSCRIPTION QUEUE ERROR:",
            call_id,
            type(exc).__name__,
        )

    # -----------------------------------------------------
    # TELEGRAM
    # -----------------------------------------------------

    # Telegram must not wait for the two client SMS API calls.  Apart from
    # making call notifications noticeably faster, this also keeps a slow
    # SMS gateway from holding the FastAPI event loop.
    telegram_status = (
        "already_sent"
        if telegram_already_sent
        else (
            "not_sent"
            if telegram_claimed
            else "in_progress"
        )
    )
    recording_seconds = 0.0
    telegram_seconds = 0.0

    if telegram_claimed:

        # Only the request that atomically claimed Telegram delivery may
        # download the recording. Duplicate webhooks never repeat this work.
        if answered and recording:
            recording_started = (
                time.monotonic()
            )
            (
                audio_duration,
                voice_bytes,
            ) = await asyncio.to_thread(
                prepare_recording,
                recording,
            )

            if audio_duration is not None:
                talk_duration = int(
                    audio_duration
                )
                duration_source = "audio"
                update_call_audio_duration(
                    call_id,
                    talk_duration,
                )
                saved_call = get_call(
                    call_id
                )

            recording_seconds = (
                time.monotonic()
                - recording_started
            )

        text = build_telegram_message(
            event,
            webhook,
            talk_duration,
            call=saved_call,
        )

        result_keyboard = None

        if not is_internal_contact:
            result_keyboard = (
                build_call_state_keyboard(
                    saved_call
                )
            )

        telegram_started = time.monotonic()

        try:

            if (
                answered
                and voice_bytes
            ):
                telegram_result = (
                    await asyncio.to_thread(
                        send_voice_bytes,
                        voice_bytes,
                        text,
                        reply_markup=
                            result_keyboard,
                    )
                )

            else:
                telegram_result = (
                    await asyncio.to_thread(
                        send_text_message,
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

            release_telegram_claim(
                call_id
            )

            print(
                "TELEGRAM ERROR:",
                repr(exc),
            )

        telegram_seconds = (
            time.monotonic()
            - telegram_started
        )

    # -----------------------------------------------------
    # AUTO SMS
    # -----------------------------------------------------

    sms_started = time.monotonic()
    sms_status = "not_sent"
    sms_kind = "promo"
    sms_text = SMS_TEXT

    if (
        direction == 0
        and not answered
    ):
        after_hours_text = (
            build_after_hours_missed_sms(
                call_start_time
            )
        )

        if after_hours_text:
            sms_kind = (
                "after_hours_missed"
            )
            sms_text = after_hours_text

    if not AUTO_SMS_ENABLED:
        sms_status = "disabled"

    elif is_internal_contact:
        sms_status = "internal_contact"

    else:

        reservation = reserve_client_sms(
            call_id,
            client_number,
            sender_user_login,
            sms_kind,
        )

        if reservation["reserved"]:

            history_id = reservation[
                "history_id"
            ]

            try:

                sms_result = await asyncio.to_thread(
                    send_client_sms,
                    client_number,
                    sender_user_login,
                    sms_text,
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
                    sms_kind,
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
                    sms_kind,
                    client_number,
                    repr(exc),
                )

        else:
            sms_status = reservation[
                "reason"
            ]

    sms_seconds = (
        time.monotonic()
        - sms_started
    )

    # -----------------------------------------------------
    # CUSTOMER RATING SMS
    # -----------------------------------------------------

    rating_sms_started = time.monotonic()
    rating_sms_status = "not_applicable"

    if not RATING_SMS_ENABLED:
        rating_sms_status = "disabled"

    elif (
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
                    await asyncio.to_thread(
                        send_client_sms,
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

    rating_sms_seconds = (
        time.monotonic()
        - rating_sms_started
    )

    duplicate = (
        not telegram_claimed
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

    total_processing_seconds = (
        time.monotonic()
        - webhook_started_monotonic
    )

    print(
        "MOIZVONKI PIPELINE TIMING:",
        json.dumps(
            {
                "call_id": call_id,
                "auto_sms_enabled": (
                    AUTO_SMS_ENABLED
                ),
                "rating_sms_enabled": (
                    RATING_SMS_ENABLED
                ),
                "sms_kind": sms_kind,
                "save_seconds": round(
                    save_seconds,
                    3,
                ),
                "promo_sms_seconds": round(
                    sms_seconds,
                    3,
                ),
                "rating_sms_seconds": round(
                    rating_sms_seconds,
                    3,
                ),
                "recording_seconds": round(
                    recording_seconds,
                    3,
                ),
                "telegram_seconds": round(
                    telegram_seconds,
                    3,
                ),
                "total_seconds": round(
                    total_processing_seconds,
                    3,
                ),
            },
            ensure_ascii=False,
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

        "sms_kind":
            sms_kind,

        "rating_sms":
            rating_sms_status,

        "transcription":
            transcription_status,
    }
