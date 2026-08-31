import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import threading
import time

from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_HALF_UP,
)
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path

import requests

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Request,
)

from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
)


# =========================================================
# ROUTER
# =========================================================

router = APIRouter()


# =========================================================
# CONFIG
# =========================================================

INSTAGRAM_VERIFY_TOKEN = os.getenv(
    "INSTAGRAM_VERIFY_TOKEN",
    "",
)

INSTAGRAM_ACCESS_TOKEN = os.getenv(
    "INSTAGRAM_ACCESS_TOKEN",
    "",
)

INSTAGRAM_APP_SECRET = os.getenv(
    "INSTAGRAM_APP_SECRET",
    "",
)

INSTAGRAM_API_VERSION = os.getenv(
    "INSTAGRAM_API_VERSION",
    "v26.0",
)

INSTAGRAM_USERNAME = os.getenv(
    "INSTAGRAM_USERNAME",
    "texnikach",
).lower()

INSTAGRAM_PUBLIC_REPLY_ENABLED = (
    os.getenv(
        "INSTAGRAM_PUBLIC_REPLY_ENABLED",
        "true",
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)

INSTAGRAM_DB_PATH = Path(
    os.getenv(
        "INSTAGRAM_DB_PATH",
        "/app/data/instagram.db",
    )
)

INSTAGRAM_RULES_SHEET_ID = os.getenv(
    "INSTAGRAM_RULES_SHEET_ID",
    "1ZdSyTJr9jSBdBDUZowXi2CpjTb7GZMQCQNsMRCMjywk",
).strip()

INSTAGRAM_RULES_SHEET_NAME = os.getenv(
    "INSTAGRAM_RULES_SHEET_NAME",
    "Лист1",
).strip()

INSTAGRAM_PRODUCTS_SHEET_ID = os.getenv(
    "INSTAGRAM_PRODUCTS_SHEET_ID",
    "1TrS6C4oHe6nzQTPTa_4se_upXBFF6rmbfnE7RqznR8U",
).strip()

INSTAGRAM_PRODUCTS_SHEET_NAME = os.getenv(
    "INSTAGRAM_PRODUCTS_SHEET_NAME",
    "bot_prices",
).strip()

INSTAGRAM_SETTINGS_SHEET_NAME = os.getenv(
    "INSTAGRAM_SETTINGS_SHEET_NAME",
    "bot_settings",
).strip()

INSTAGRAM_POST_MODELS_SHEET_NAME = os.getenv(
    "INSTAGRAM_POST_MODELS_SHEET_NAME",
    "post_models",
).strip()

INSTAGRAM_DIRECT_RULES_SHEET_NAME = os.getenv(
    "INSTAGRAM_DIRECT_RULES_SHEET_NAME",
    "direct_rules",
).strip()

INSTAGRAM_DIRECT_SETTINGS_SHEET_NAME = os.getenv(
    "INSTAGRAM_DIRECT_SETTINGS_SHEET_NAME",
    "direct_settings",
).strip()

INSTAGRAM_ACCOUNT_ID = os.getenv(
    "INSTAGRAM_ACCOUNT_ID",
    "17841444196466655",
).strip()

INSTAGRAM_POST_SYNC_ENABLED = (
    os.getenv(
        "INSTAGRAM_POST_SYNC_ENABLED",
        "true",
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)

try:
    INSTAGRAM_POST_SYNC_INTERVAL = max(
        60,
        int(
            os.getenv(
                "INSTAGRAM_POST_SYNC_INTERVAL",
                "300",
            )
        ),
    )
except ValueError:
    INSTAGRAM_POST_SYNC_INTERVAL = 300

try:
    INSTAGRAM_POST_SYNC_LIMIT = min(
        100,
        max(
            1,
            int(
                os.getenv(
                    "INSTAGRAM_POST_SYNC_LIMIT",
                    "50",
                )
            ),
        ),
    )
except ValueError:
    INSTAGRAM_POST_SYNC_LIMIT = 50

try:
    INSTAGRAM_RULES_CACHE_TTL = max(
        30,
        int(
            os.getenv(
                "INSTAGRAM_RULES_CACHE_TTL",
                "60",
            )
        ),
    )
except ValueError:
    INSTAGRAM_RULES_CACHE_TTL = 60

try:
    INSTAGRAM_PRODUCTS_CACHE_TTL = max(
        30,
        int(
            os.getenv(
                "INSTAGRAM_PRODUCTS_CACHE_TTL",
                "60",
            )
        ),
    )
except ValueError:
    INSTAGRAM_PRODUCTS_CACHE_TTL = 60

try:
    INSTAGRAM_RULES_HTTP_TIMEOUT = max(
        3,
        int(
            os.getenv(
                "INSTAGRAM_RULES_HTTP_TIMEOUT",
                "15",
            )
        ),
    )
except ValueError:
    INSTAGRAM_RULES_HTTP_TIMEOUT = 15

INSTAGRAM_DB_PATH.parent.mkdir(
    parents=True,
    exist_ok=True,
)

HTTP = requests.Session()
RULES_HTTP = requests.Session()
PRODUCTS_HTTP = requests.Session()
POSTS_HTTP = requests.Session()
DIRECT_HTTP = requests.Session()

RULES_CACHE_LOCK = threading.Lock()
RULES_CACHE = {
    "rules": [],
    "loaded_at": 0.0,
    "source": "not_loaded",
}

PRODUCTS_CACHE_LOCK = threading.Lock()
PRODUCTS_CACHE = {
    "catalog": None,
    "loaded_at": 0.0,
    "source": "not_loaded",
}

POST_MODELS_CACHE_LOCK = threading.Lock()
POST_MODELS_CACHE = {
    "mappings": {},
    "loaded_at": 0.0,
    "source": "not_loaded",
}

DIRECT_CONFIG_CACHE_LOCK = threading.Lock()
DIRECT_CONFIG_CACHE = {
    "rules": [],
    "settings": {},
    "loaded_at": 0.0,
    "source": "not_loaded",
}

GOOGLE_WRITE_CLIENT = None
GOOGLE_WRITE_CLIENT_LOCK = threading.Lock()
POST_SYNC_TASK = None


# =========================================================
# URLS
# =========================================================

GRAPH_BASE_URL = (
    "https://graph.instagram.com/"
    f"{INSTAGRAM_API_VERSION}"
)


# =========================================================
# TRIGGERS
# =========================================================

PRICE_TRIGGER_WORDS = {
    "+",
    "++",
    "+++",

    "narx",
    "narxi",
    "narh",
    "narhi",

    "price",

    "цена",
    "цену",
    "стоимость",

    "qancha",
    "qanca",
    "kancha",

    "bormi",
    "bor",
    "есть",
    "наличие",

    "нечпул",
    "nechpul",
    "necha",
    "nechchi",
    "сколько",
    "почем",
}


PRICE_TRIGGER_PHRASES = {
    "necha pul",
    "narxi qancha",
    "qancha turadi",
    "сколько стоит",
}


# =========================================================
# MODEL SUFFIXES
#
# Используются, чтобы:
#
# S25 != S25 Ultra
# S25 != S25+
# S25 != S25 FE
# =========================================================

MODEL_SUFFIX_WORDS = {
    "ultra",
    "pro",
    "max",
    "plus",
    "fe",
    "lite",
    "mini",
    "edge",
    "air",
}


# =========================================================
# DATABASE
# =========================================================

def connect_instagram_db():

    conn = sqlite3.connect(
        INSTAGRAM_DB_PATH,
        timeout=30,
    )

    conn.row_factory = (
        sqlite3.Row
    )

    conn.execute(
        "PRAGMA busy_timeout = 30000"
    )

    return conn


def init_instagram_db():

    with connect_instagram_db() as conn:

        conn.execute(
            "PRAGMA journal_mode = WAL"
        )

        conn.execute(
            "PRAGMA synchronous = NORMAL"
        )

        # -------------------------------------------------
        # PRODUCTS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            instagram_products (

                id INTEGER
                    PRIMARY KEY,

                name TEXT
                    NOT NULL,

                price_text TEXT,

                availability_text TEXT,

                variants_text TEXT,

                product_url TEXT,

                active INTEGER
                    NOT NULL
                    DEFAULT 1,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # -------------------------------------------------
        # PRODUCT ALIASES
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            instagram_product_aliases (

                id INTEGER
                    PRIMARY KEY
                    AUTOINCREMENT,

                product_id INTEGER
                    NOT NULL,

                alias TEXT
                    NOT NULL,

                alias_normalized TEXT
                    NOT NULL,

                UNIQUE(
                    product_id,
                    alias_normalized
                ),

                FOREIGN KEY(product_id)
                    REFERENCES
                    instagram_products(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------------------------------
        # POST -> PRODUCT
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            instagram_post_products (

                media_id TEXT
                    PRIMARY KEY,

                product_id INTEGER
                    NOT NULL,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(product_id)
                    REFERENCES
                    instagram_products(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------------------------------
        # PROCESSED COMMENTS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            instagram_processed_comments (

                comment_id TEXT
                    PRIMARY KEY,

                media_id TEXT,

                instagram_user_id TEXT,

                username TEXT,

                comment_text TEXT,

                detected_product_id INTEGER,

                detection_source TEXT,

                status TEXT
                    NOT NULL,

                error TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # -------------------------------------------------
        # PROCESSED DIRECT MESSAGES
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            instagram_processed_messages (

                message_id TEXT
                    PRIMARY KEY,

                sender_id TEXT,

                recipient_id TEXT,

                detection_source TEXT,

                status TEXT
                    NOT NULL,

                error TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # -------------------------------------------------
        # INDEXES
        # -------------------------------------------------

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_instagram_alias_normalized

            ON instagram_product_aliases(
                alias_normalized
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_instagram_processed_status

            ON instagram_processed_comments(
                status
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_instagram_messages_status

            ON instagram_processed_messages(
                status
            )
            """
        )

        conn.commit()


init_instagram_db()


# =========================================================
# NORMALIZATION
# =========================================================

def normalize_text(
    value: str | None,
) -> str:

    if not value:
        return ""

    value = str(
        value
    ).lower()

    value = (
        value
        .replace(
            "ё",
            "е",
        )
        .replace(
            "’",
            "'",
        )
        .replace(
            "ʻ",
            "'",
        )
        .replace(
            "‘",
            "'",
        )
    )

    # Сохраняем +
    value = re.sub(
        r"[^\w+\s'-]",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# =========================================================
# LANGUAGE
# =========================================================

def detect_language(
    text: str,
) -> str:

    normalized = normalize_text(
        text
    )

    uz_words = {
        "narx",
        "narxi",
        "narh",
        "narhi",
        "qancha",
        "qanca",
        "kancha",
        "bormi",
        "bor",
        "nechpul",
        "necha",
        "nechchi",
    }

    words = set(
        normalized.split()
    )

    if (
        words
        &
        uz_words
    ):

        return "uz"

    if re.search(
        r"[а-яё]",
        text.lower(),
    ):

        return "ru"

    return "uz"


# =========================================================
# TRIGGER DETECTION
# =========================================================

def is_generic_trigger(
    text: str,
) -> bool:

    normalized = normalize_text(
        text
    )

    if not normalized:
        return False

    # -------------------------------------------------
    # + / ++ / +++
    # -------------------------------------------------

    compact = normalized.replace(
        " ",
        "",
    )

    if (
        compact
        and
        set(compact) == {"+"}
    ):

        return True

    # -------------------------------------------------
    # WORDS AND PHRASES
    # -------------------------------------------------

    words = set(
        normalized.split()
    )

    if (
        words
        &
        PRICE_TRIGGER_WORDS
    ):

        return True

    for phrase in PRICE_TRIGGER_PHRASES:

        phrase_pattern = re.compile(
            r"(?<![\w])"
            + re.escape(phrase).replace(
                r"\ ",
                r"\s+",
            )
            + r"(?![\w])",
            flags=re.IGNORECASE,
        )

        if phrase_pattern.search(normalized):

            return True

    return False


# =========================================================
# GOOGLE SHEETS RESPONSE RULES
# =========================================================

SUPPORTED_RULE_MATCH_TYPES = {
    "contains_any",
    "contains_all",
    "exact",
    "default",
}

LOCAL_DEFAULT_RULE = {
    "priority": 0,
    "keywords": [],
    "match_type": "default",
    "private_reply": (
        "Здравствуйте! Спасибо за обращение в Texnikach.\n\n"
        "Наш Telegram-канал:\n"
        "https://t.me/texnikach\n\n"
        "Сайт:\n"
        "https://texnikach.uz/go\n\n"
        "Для заказа или уточнения напишите менеджеру:\n"
        "https://t.me/texnikach_admin"
    ),
    "public_reply": "Ответили в Direct ✅",
    "row_number": 0,
}

LOCAL_DIRECT_SETTINGS = {
    "telegram_url":
        "https://t.me/texnikach",

    "manager_url":
        "https://t.me/texnikach_admin",

    "model_intro":
        "TEXNIKACH",

    "model_prices_label":
        "Актуальные цены / Aktual narxlar:",

    "model_course_label":
        "💱 Курс:",

    "model_footer": (
        "Для оформления заказа напишите менеджеру:\n"
        "{manager_url}\n\n"
        "Buyurtma berish uchun menejerga yozing:\n"
        "{manager_url}"
    ),

    "model_other_variants":
        "Другие варианты уточните у менеджера.",

    "model_from_prefix":
        "от",

    "model_empty_memory_label":
        "Цена",

    "model_not_found_reply": (
        "Пожалуйста, напишите полное название модели. "
        "Для помощи и заказа свяжитесь с менеджером:\n"
        "{manager_url}\n\n"
        "Iltimos, modelning to‘liq nomini yozing. "
        "Yordam va buyurtma uchun menejerga murojaat qiling:\n"
        "{manager_url}"
    ),
}

LOCAL_DIRECT_RULES = [
    {
        "priority": 100,
        "keywords": [
            "заказ",
            "заказать",
            "купить",
            "buyurtma",
            "sotib olish",
        ],
        "match_type": "contains_any",
        "reply_text": (
            "Для оформления заказа напишите менеджеру:\n"
            "{manager_url}\n\n"
            "Buyurtma berish uchun menejerga yozing:\n"
            "{manager_url}"
        ),
        "row_number": 0,
    },
    {
        "priority": 0,
        "keywords": [],
        "match_type": "default",
        "reply_text": (
            "Здравствуйте! Не совсем понял ваш вопрос.\n\n"
            "Актуальные модели и цены есть в Telegram-канале:\n"
            "{telegram_url}\n\n"
            "Для помощи и заказа напишите менеджеру:\n"
            "{manager_url}\n\n"
            "Assalomu alaykum! Savolingizni to‘liq tushunmadim.\n\n"
            "Aktual modellar va narxlar Telegram-kanalimizda:\n"
            "{telegram_url}\n\n"
            "Yordam va buyurtma uchun menejerga yozing:\n"
            "{manager_url}"
        ),
        "row_number": 0,
    },
]


def parse_enabled(value: str | None) -> bool:

    return str(
        value
        or ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "да",
    }


def split_rule_keywords(
    value: str | None,
) -> list[str]:

    keywords = []

    for item in re.split(
        r"[,;\n]+",
        str(
            value
            or ""
        ),
    ):

        normalized = normalize_text(
            item
        )

        if (
            normalized
            and
            normalized != "default"
            and
            normalized not in keywords
        ):

            keywords.append(
                normalized
            )

    return keywords


def parse_rules_csv(
    csv_text: str,
) -> list[dict]:

    reader = csv.DictReader(
        io.StringIO(
            csv_text.lstrip(
                "\ufeff"
            )
        )
    )

    headers = {
        str(
            header
            or ""
        ).strip().lower()
        for header in (
            reader.fieldnames
            or []
        )
    }

    required_headers = {
        "priority",
        "enabled",
        "keywords",
        "match_type",
        "private_reply",
        "public_reply",
    }

    missing_headers = (
        required_headers
        - headers
    )

    if missing_headers:

        raise ValueError(
            "Google Sheets is missing columns: "
            + ", ".join(
                sorted(
                    missing_headers
                )
            )
        )

    rules = []

    for row_number, raw_row in enumerate(
        reader,
        start=2,
    ):

        row = {
            str(
                key
                or ""
            ).strip().lower(): (
                value
                or ""
            )
            for key, value in raw_row.items()
        }

        if not parse_enabled(
            row.get(
                "enabled"
            )
        ):

            continue

        match_type = str(
            row.get(
                "match_type"
            )
            or ""
        ).strip().lower()

        if (
            match_type
            not in SUPPORTED_RULE_MATCH_TYPES
        ):

            print(
                "INSTAGRAM RULE SKIPPED:",
                {
                    "row": row_number,
                    "reason": "unsupported_match_type",
                    "match_type": match_type,
                },
            )

            continue

        private_reply = str(
            row.get(
                "private_reply"
            )
            or ""
        ).strip()

        if not private_reply:

            print(
                "INSTAGRAM RULE SKIPPED:",
                {
                    "row": row_number,
                    "reason": "empty_private_reply",
                },
            )

            continue

        try:
            priority = int(
                str(
                    row.get(
                        "priority"
                    )
                    or "0"
                ).strip()
            )
        except ValueError:
            priority = 0

        keywords = split_rule_keywords(
            row.get(
                "keywords"
            )
        )

        if (
            match_type != "default"
            and
            not keywords
        ):

            print(
                "INSTAGRAM RULE SKIPPED:",
                {
                    "row": row_number,
                    "reason": "empty_keywords",
                },
            )

            continue

        rules.append(
            {
                "priority": priority,
                "keywords": keywords,
                "match_type": match_type,
                "private_reply": private_reply,
                "public_reply": str(
                    row.get(
                        "public_reply"
                    )
                    or ""
                ).strip(),
                "row_number": row_number,
            }
        )

    if not rules:

        raise ValueError(
            "Google Sheets contains no active valid rules"
        )

    return sorted(
        rules,
        key=lambda rule: (
            -rule[
                "priority"
            ],
            rule[
                "row_number"
            ],
        ),
    )


def fetch_google_sheet_rules() -> list[dict]:

    if not INSTAGRAM_RULES_SHEET_ID:

        raise RuntimeError(
            "INSTAGRAM_RULES_SHEET_ID is not configured"
        )

    url = (
        "https://docs.google.com/spreadsheets/d/"
        f"{INSTAGRAM_RULES_SHEET_ID}/gviz/tq"
    )

    response = RULES_HTTP.get(
        url,
        params={
            "tqx": "out:csv",
            "sheet": INSTAGRAM_RULES_SHEET_NAME,
        },
        timeout=INSTAGRAM_RULES_HTTP_TIMEOUT,
    )

    if not response.ok:

        print(
            "INSTAGRAM RULES DOWNLOAD ERROR:",
            response.status_code,
            response.text[
                :500
            ],
        )

        response.raise_for_status()

    csv_text = response.content.decode(
        "utf-8-sig"
    )

    return parse_rules_csv(
        csv_text
    )


def get_response_rules(
    *,
    force_refresh: bool = False,
) -> list[dict]:

    now = time.monotonic()

    cached_rules = RULES_CACHE[
        "rules"
    ]

    if (
        not force_refresh
        and
        cached_rules
        and
        now
        - RULES_CACHE[
            "loaded_at"
        ]
        < INSTAGRAM_RULES_CACHE_TTL
    ):

        return cached_rules

    with RULES_CACHE_LOCK:

        now = time.monotonic()

        cached_rules = RULES_CACHE[
            "rules"
        ]

        if (
            not force_refresh
            and
            cached_rules
            and
            now
            - RULES_CACHE[
                "loaded_at"
            ]
            < INSTAGRAM_RULES_CACHE_TTL
        ):

            return cached_rules

        try:

            rules = fetch_google_sheet_rules()

            RULES_CACHE.update(
                {
                    "rules": rules,
                    "loaded_at": now,
                    "source": "google_sheets",
                }
            )

            print(
                "INSTAGRAM RULES LOADED:",
                {
                    "count": len(
                        rules
                    ),
                    "sheet": INSTAGRAM_RULES_SHEET_NAME,
                },
            )

            return rules

        except Exception as exc:

            print(
                "INSTAGRAM RULES LOAD FAILED:",
                repr(
                    exc
                )[
                    :1000
                ],
            )

            if cached_rules:

                RULES_CACHE.update(
                    {
                        "loaded_at": now,
                        "source": "stale_cache",
                    }
                )

                print(
                    "INSTAGRAM RULES USING STALE CACHE:",
                    {
                        "count": len(
                            cached_rules
                        ),
                    },
                )

                return cached_rules

            fallback_rules = [
                dict(
                    LOCAL_DEFAULT_RULE
                )
            ]

            RULES_CACHE.update(
                {
                    "rules": fallback_rules,
                    "loaded_at": now,
                    "source": "local_fallback",
                }
            )

            return fallback_rules


def keyword_matches_text(
    normalized_text: str,
    keyword: str,
) -> bool:

    pattern = re.compile(
        r"(?<![\w])"
        + re.escape(
            keyword
        ).replace(
            r"\ ",
            r"\s+",
        )
        + r"(?![\w])",
        flags=re.IGNORECASE,
    )

    return bool(
        pattern.search(
            normalized_text
        )
    )


def resolve_response_rule(
    text: str,
    *,
    rules: list[dict] | None = None,
) -> dict:

    normalized = normalize_text(
        text
    )

    active_rules = (
        rules
        if rules is not None
        else get_response_rules()
    )

    default_rule = None

    for rule in active_rules:

        match_type = rule[
            "match_type"
        ]

        if match_type == "default":

            if default_rule is None:
                default_rule = rule

            continue

        keyword_results = [
            keyword_matches_text(
                normalized,
                keyword,
            )
            for keyword in rule[
                "keywords"
            ]
        ]

        matched = False

        if match_type == "contains_any":
            matched = any(
                keyword_results
            )

        elif match_type == "contains_all":
            matched = all(
                keyword_results
            )

        elif match_type == "exact":
            matched = normalized in rule[
                "keywords"
            ]

        if matched:

            return {
                "rule": rule,
                "source": (
                    "google_sheet_rule_"
                    f"row_{rule['row_number']}"
                ),
            }

    selected_default = (
        default_rule
        or LOCAL_DEFAULT_RULE
    )

    return {
        "rule": selected_default,
        "source": (
            "google_sheet_default"
            if default_rule
            else "local_default"
        ),
    }


# =========================================================
# GOOGLE SHEETS DIRECT CONFIG
# =========================================================

def parse_direct_rules_csv(
    csv_text: str,
) -> list[dict]:

    reader = csv.DictReader(
        io.StringIO(
            csv_text.lstrip(
                "\ufeff"
            )
        )
    )

    headers = {
        str(
            header
            or ""
        ).strip().lower()
        for header in (
            reader.fieldnames
            or []
        )
    }

    required_headers = {
        "priority",
        "enabled",
        "keywords",
        "match_type",
        "reply_text",
    }

    missing_headers = (
        required_headers
        - headers
    )

    if missing_headers:

        raise ValueError(
            "direct_rules is missing columns: "
            + ", ".join(
                sorted(
                    missing_headers
                )
            )
        )

    rules = []

    for row_number, raw_row in enumerate(
        reader,
        start=2,
    ):

        row = {
            str(
                key
                or ""
            ).strip().lower(): (
                value
                or ""
            )
            for key, value in raw_row.items()
        }

        if not parse_enabled(
            row.get(
                "enabled"
            )
        ):
            continue

        match_type = str(
            row.get(
                "match_type"
            )
            or ""
        ).strip().lower()

        if (
            match_type
            not in SUPPORTED_RULE_MATCH_TYPES
        ):

            print(
                "INSTAGRAM DIRECT RULE SKIPPED:",
                {
                    "row": row_number,
                    "reason": "unsupported_match_type",
                    "match_type": match_type,
                },
            )

            continue

        reply_text = str(
            row.get(
                "reply_text"
            )
            or ""
        ).strip()

        if not reply_text:

            print(
                "INSTAGRAM DIRECT RULE SKIPPED:",
                {
                    "row": row_number,
                    "reason": "empty_reply_text",
                },
            )

            continue

        try:
            priority = int(
                str(
                    row.get(
                        "priority"
                    )
                    or "0"
                ).strip()
            )
        except ValueError:
            priority = 0

        keywords = split_rule_keywords(
            row.get(
                "keywords"
            )
        )

        if (
            match_type != "default"
            and
            not keywords
        ):

            print(
                "INSTAGRAM DIRECT RULE SKIPPED:",
                {
                    "row": row_number,
                    "reason": "empty_keywords",
                },
            )

            continue

        rules.append(
            {
                "priority": priority,
                "keywords": keywords,
                "match_type": match_type,
                "reply_text": reply_text,
                "row_number": row_number,
            }
        )

    if not rules:

        raise ValueError(
            "direct_rules contains no active valid rules"
        )

    return sorted(
        rules,
        key=lambda rule: (
            -rule[
                "priority"
            ],
            rule[
                "row_number"
            ],
        ),
    )


def parse_direct_settings_csv(
    csv_text: str,
) -> dict[str, str]:

    reader = csv.DictReader(
        io.StringIO(
            csv_text.lstrip(
                "\ufeff"
            )
        )
    )

    headers = {
        str(
            header
            or ""
        ).strip().lower()
        for header in (
            reader.fieldnames
            or []
        )
    }

    required_headers = {
        "setting",
        "value",
    }

    missing_headers = (
        required_headers
        - headers
    )

    if missing_headers:

        raise ValueError(
            "direct_settings is missing columns: "
            + ", ".join(
                sorted(
                    missing_headers
                )
            )
        )

    settings = {}

    for raw_row in reader:

        row = {
            str(
                key
                or ""
            ).strip().lower(): (
                value
                if value is not None
                else ""
            )
            for key, value in raw_row.items()
        }

        setting = str(
            row.get(
                "setting"
            )
            or ""
        ).strip().lower()

        if not setting:
            continue

        settings[
            setting
        ] = str(
            row.get(
                "value"
            )
            or ""
        ).strip()

    if not settings:

        raise ValueError(
            "direct_settings contains no settings"
        )

    return settings


def fetch_direct_sheet_csv(
    sheet_name: str,
) -> str:

    if not INSTAGRAM_RULES_SHEET_ID:

        raise RuntimeError(
            "INSTAGRAM_RULES_SHEET_ID is not configured"
        )

    url = (
        "https://docs.google.com/spreadsheets/d/"
        f"{INSTAGRAM_RULES_SHEET_ID}/gviz/tq"
    )

    response = DIRECT_HTTP.get(
        url,
        params={
            "tqx": "out:csv",
            "sheet": sheet_name,
        },
        timeout=INSTAGRAM_RULES_HTTP_TIMEOUT,
    )

    if not response.ok:

        print(
            "INSTAGRAM DIRECT SHEET DOWNLOAD ERROR:",
            {
                "sheet": sheet_name,
                "status": response.status_code,
                "body": response.text[
                    :500
                ],
            },
        )

        response.raise_for_status()

    return response.content.decode(
        "utf-8-sig"
    )


def fetch_direct_config() -> dict:

    rules = parse_direct_rules_csv(
        fetch_direct_sheet_csv(
            INSTAGRAM_DIRECT_RULES_SHEET_NAME
        )
    )

    sheet_settings = parse_direct_settings_csv(
        fetch_direct_sheet_csv(
            INSTAGRAM_DIRECT_SETTINGS_SHEET_NAME
        )
    )

    settings = dict(
        LOCAL_DIRECT_SETTINGS
    )

    settings.update(
        sheet_settings
    )

    for required_setting in (
        "telegram_url",
        "manager_url",
    ):

        if str(
            settings.get(
                required_setting
            )
            or ""
        ).strip():
            continue

        print(
            "INSTAGRAM DIRECT SETTING FALLBACK:",
            required_setting,
        )

        settings[
            required_setting
        ] = LOCAL_DIRECT_SETTINGS[
            required_setting
        ]

    return {
        "rules": rules,
        "settings": settings,
    }


def get_direct_config(
    *,
    force_refresh: bool = False,
) -> dict:

    now = time.monotonic()

    cached_rules = DIRECT_CONFIG_CACHE[
        "rules"
    ]

    if (
        not force_refresh
        and
        cached_rules
        and
        now
        - DIRECT_CONFIG_CACHE[
            "loaded_at"
        ]
        < INSTAGRAM_RULES_CACHE_TTL
    ):

        return {
            "rules": cached_rules,
            "settings": DIRECT_CONFIG_CACHE[
                "settings"
            ],
        }

    with DIRECT_CONFIG_CACHE_LOCK:

        now = time.monotonic()

        cached_rules = DIRECT_CONFIG_CACHE[
            "rules"
        ]

        if (
            not force_refresh
            and
            cached_rules
            and
            now
            - DIRECT_CONFIG_CACHE[
                "loaded_at"
            ]
            < INSTAGRAM_RULES_CACHE_TTL
        ):

            return {
                "rules": cached_rules,
                "settings": DIRECT_CONFIG_CACHE[
                    "settings"
                ],
            }

        try:

            config = fetch_direct_config()

            DIRECT_CONFIG_CACHE.update(
                {
                    "rules": config[
                        "rules"
                    ],
                    "settings": config[
                        "settings"
                    ],
                    "loaded_at": now,
                    "source": "google_sheets",
                }
            )

            print(
                "INSTAGRAM DIRECT CONFIG LOADED:",
                {
                    "rules": len(
                        config[
                            "rules"
                        ]
                    ),
                    "settings": len(
                        config[
                            "settings"
                        ]
                    ),
                },
            )

            return config

        except Exception as exc:

            print(
                "INSTAGRAM DIRECT CONFIG LOAD FAILED:",
                repr(
                    exc
                )[
                    :1000
                ],
            )

            if cached_rules:

                DIRECT_CONFIG_CACHE.update(
                    {
                        "loaded_at": now,
                        "source": "stale_cache",
                    }
                )

                return {
                    "rules": cached_rules,
                    "settings": DIRECT_CONFIG_CACHE[
                        "settings"
                    ],
                }

            fallback = {
                "rules": [
                    dict(
                        rule
                    )
                    for rule in LOCAL_DIRECT_RULES
                ],
                "settings": dict(
                    LOCAL_DIRECT_SETTINGS
                ),
            }

            DIRECT_CONFIG_CACHE.update(
                {
                    "rules": fallback[
                        "rules"
                    ],
                    "settings": fallback[
                        "settings"
                    ],
                    "loaded_at": now,
                    "source": "local_fallback",
                }
            )

            return fallback


def resolve_direct_rule(
    text: str,
    *,
    rules: list[dict] | None = None,
) -> dict:

    normalized = normalize_text(
        text
    )

    active_rules = (
        rules
        if rules is not None
        else get_direct_config()[
            "rules"
        ]
    )

    default_rule = None

    for rule in active_rules:

        match_type = rule[
            "match_type"
        ]

        if match_type == "default":

            if default_rule is None:
                default_rule = rule

            continue

        keyword_results = [
            keyword_matches_text(
                normalized,
                keyword,
            )
            for keyword in rule[
                "keywords"
            ]
        ]

        matched = False

        if match_type == "contains_any":
            matched = any(
                keyword_results
            )

        elif match_type == "contains_all":
            matched = all(
                keyword_results
            )

        elif match_type == "exact":
            matched = normalized in rule[
                "keywords"
            ]

        if matched:

            return {
                "rule": rule,
                "source": (
                    "direct_sheet_rule_"
                    f"row_{rule['row_number']}"
                ),
            }

    selected_default = (
        default_rule
        or LOCAL_DIRECT_RULES[
            -1
        ]
    )

    return {
        "rule": selected_default,
        "source": (
            "direct_sheet_default"
            if default_rule
            else "direct_local_default"
        ),
    }


def render_direct_template(
    value: str,
    settings: dict[str, str],
) -> str:

    rendered = str(
        value
        or ""
    )

    for _ in range(
        3
    ):

        previous = rendered

        for key, replacement in settings.items():

            rendered = rendered.replace(
                "{" + key + "}",
                str(
                    replacement
                    or ""
                ),
            )

        if rendered == previous:
            break

    return rendered.strip()


# =========================================================
# LIVE PRODUCT PRICES FROM GOOGLE SHEETS
# =========================================================

PRODUCT_REQUIRED_HEADERS = {
    "product_id",
    "model_name",
    "memory",
    "color",
    "price",
    "warranty_period",
}

SIM_VARIANT_SUFFIX_PATTERN = re.compile(
    r"\s*\(\s*(?:e?sim)"
    r"(?:\s*\+\s*e?sim)?\s*\)\s*$",
    flags=re.IGNORECASE,
)

MARKET_VARIANT_SUFFIX_PATTERN = re.compile(
    r"\s*\(\s*(?:"
    r"global\s+(?:rom|version)|"
    r"china\s+version|"
    r"hong\s+kong|"
    r"cn|eu|usa|uae|hk"
    r")\s*\)\s*$",
    flags=re.IGNORECASE,
)

NETWORK_SUFFIX_PATTERN = re.compile(
    r"\s+(?:[345]g)\s*$",
    flags=re.IGNORECASE,
)

CONNECTOR_SUFFIX_PATTERN = re.compile(
    r"\s+(?:usb[\s-]*c|lightning)\s*$",
    flags=re.IGNORECASE,
)

INSTAGRAM_PRICE_MESSAGE_LIMIT = 950

UNSAFE_SINGLE_MODEL_ALIASES = {
    "active",
    "air",
    "book",
    "edge",
    "lite",
    "max",
    "mini",
    "note",
    "pad",
    "phone",
    "plus",
    "pro",
    "ultra",
    "watch",
}


def normalize_model_text(
    value: str | None,
) -> str:

    if not value:
        return ""

    normalized = str(
        value
    ).casefold()

    normalized = (
        normalized
        .replace(
            "ё",
            "е",
        )
        .replace(
            "+",
            " plus ",
        )
        .replace(
            "’",
            "'",
        )
        .replace(
            "ʻ",
            "'",
        )
        .replace(
            "‘",
            "'",
        )
    )

    normalized = re.sub(
        r"[^a-zа-я0-9']+",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized.strip()


def model_family_name(
    model_name: str,
) -> str:

    family_name = str(
        model_name
    ).strip()

    family_name = SIM_VARIANT_SUFFIX_PATTERN.sub(
        "",
        family_name,
    ).strip()

    family_name = MARKET_VARIANT_SUFFIX_PATTERN.sub(
        "",
        family_name,
    ).strip()

    family_name = NETWORK_SUFFIX_PATTERN.sub(
        "",
        family_name,
    ).strip()

    family_name = CONNECTOR_SUFFIX_PATTERN.sub(
        "",
        family_name,
    ).strip()

    return family_name


def parse_decimal_value(
    value,
) -> Decimal | None:

    raw_value = str(
        value
        or ""
    ).strip().replace(
        " ",
        "",
    )

    if not raw_value:
        return None

    if (
        "," in raw_value
        and
        "." not in raw_value
    ):
        raw_value = raw_value.replace(
            ",",
            ".",
        )

    try:
        return Decimal(
            raw_value
        )
    except InvalidOperation:
        return None


def read_csv_rows(
    csv_text: str,
    *,
    required_headers: set[str],
) -> list[dict]:

    reader = csv.DictReader(
        io.StringIO(
            csv_text.lstrip(
                "\ufeff"
            )
        )
    )

    headers = {
        str(
            header
            or ""
        ).strip().lower()
        for header in (
            reader.fieldnames
            or []
        )
    }

    missing_headers = (
        required_headers
        - headers
    )

    if missing_headers:

        raise ValueError(
            "Google Sheets is missing columns: "
            + ", ".join(
                sorted(
                    missing_headers
                )
            )
        )

    rows = []

    for raw_row in reader:

        rows.append(
            {
                str(
                    key
                    or ""
                ).strip().lower(): (
                    value
                    or ""
                )
                for key, value in raw_row.items()
            }
        )

    return rows


def parse_product_catalog(
    products_csv: str,
    settings_csv: str,
) -> dict:

    raw_products = read_csv_rows(
        products_csv,
        required_headers=
            PRODUCT_REQUIRED_HEADERS,
    )

    raw_settings = read_csv_rows(
        settings_csv,
        required_headers={
            "setting",
            "value",
        },
    )

    kurs = None

    for setting in raw_settings:

        if (
            str(
                setting.get(
                    "setting"
                )
                or ""
            ).strip().casefold()
            == "kurs"
        ):

            kurs = parse_decimal_value(
                setting.get(
                    "value"
                )
            )

            break

    if (
        kurs is None
        or
        kurs <= 0
    ):

        raise ValueError(
            "bot_settings has no valid positive kurs"
        )

    families = {}
    valid_rows = []

    for raw_product in raw_products:

        model_name = str(
            raw_product.get(
                "model_name"
            )
            or ""
        ).strip()

        price = parse_decimal_value(
            raw_product.get(
                "price"
            )
        )

        if (
            not model_name
            or
            price is None
            or
            price <= 0
        ):

            continue

        raw_product_id = str(
            raw_product.get(
                "product_id"
            )
            or ""
        ).strip()

        try:
            product_id = int(
                Decimal(
                    raw_product_id
                )
            )
        except (
            InvalidOperation,
            ValueError,
        ):
            continue

        family_name = model_family_name(
            model_name
        )

        family_key = normalize_model_text(
            family_name
        )

        if not family_key:
            continue

        warranty = parse_decimal_value(
            raw_product.get(
                "warranty_period"
            )
        )

        product = {
            "product_id": product_id,
            "model_name": model_name,
            "family_name": family_name,
            "memory": str(
                raw_product.get(
                    "memory"
                )
                or ""
            ).strip(),
            "color": str(
                raw_product.get(
                    "color"
                )
                or ""
            ).strip(),
            "price": price,
            "warranty_period": warranty,
        }

        valid_rows.append(
            product
        )

        family = families.setdefault(
            family_key,
            {
                "key": family_key,
                "name": family_name,
                "rows": [],
            },
        )

        family[
            "rows"
        ].append(
            product
        )

    if not valid_rows:

        raise ValueError(
            "bot_prices contains no valid products"
        )

    aliases = {}

    for family_key, family in families.items():

        normalized_name = normalize_model_text(
            family[
                "name"
            ]
        )

        tokens = normalized_name.split()

        candidate_aliases = {
            normalized_name,
        }

        for start_index in range(
            1,
            len(
                tokens
            ),
        ):

            alias_tokens = tokens[
                start_index:
            ]

            has_mixed_model_token = any(
                re.search(
                    r"[a-zа-я]",
                    token,
                    flags=re.IGNORECASE,
                )
                and
                re.search(
                    r"\d",
                    token,
                )
                for token in alias_tokens
            )

            safe_text_alias = (
                len(
                    alias_tokens
                ) >= 2
                or
                (
                    len(
                        alias_tokens
                    ) == 1
                    and
                    len(
                        alias_tokens[
                            0
                        ]
                    ) >= 5
                    and
                    alias_tokens[
                        0
                    ]
                    not in UNSAFE_SINGLE_MODEL_ALIASES
                )
            )

            if (
                has_mixed_model_token
                or
                safe_text_alias
            ):

                candidate_aliases.add(
                    " ".join(
                        alias_tokens
                    )
                )

        for alias in candidate_aliases:

            if not alias:
                continue

            aliases.setdefault(
                alias,
                set(),
            ).add(
                family_key
            )

    sorted_aliases = sorted(
        aliases,
        key=lambda alias: (
            len(
                alias.split()
            ),
            len(
                alias
            ),
        ),
        reverse=True,
    )

    return {
        "kurs": kurs,
        "rows": valid_rows,
        "families": families,
        "aliases": aliases,
        "sorted_aliases": sorted_aliases,
    }


def fetch_sheet_csv(
    spreadsheet_id: str,
    sheet_name: str,
) -> str:

    url = (
        "https://docs.google.com/spreadsheets/d/"
        f"{spreadsheet_id}/gviz/tq"
    )

    response = PRODUCTS_HTTP.get(
        url,
        params={
            "tqx": "out:csv",
            "sheet": sheet_name,
        },
        timeout=INSTAGRAM_RULES_HTTP_TIMEOUT,
    )

    if not response.ok:

        print(
            "INSTAGRAM PRODUCTS DOWNLOAD ERROR:",
            {
                "sheet": sheet_name,
                "status_code": response.status_code,
                "response": response.text[
                    :500
                ],
            },
        )

        response.raise_for_status()

    return response.content.decode(
        "utf-8-sig"
    )


def fetch_product_catalog() -> dict:

    if not INSTAGRAM_PRODUCTS_SHEET_ID:

        raise RuntimeError(
            "INSTAGRAM_PRODUCTS_SHEET_ID is not configured"
        )

    products_csv = fetch_sheet_csv(
        INSTAGRAM_PRODUCTS_SHEET_ID,
        INSTAGRAM_PRODUCTS_SHEET_NAME,
    )

    settings_csv = fetch_sheet_csv(
        INSTAGRAM_PRODUCTS_SHEET_ID,
        INSTAGRAM_SETTINGS_SHEET_NAME,
    )

    return parse_product_catalog(
        products_csv,
        settings_csv,
    )


def get_product_catalog(
    *,
    force_refresh: bool = False,
) -> dict | None:

    now = time.monotonic()

    cached_catalog = PRODUCTS_CACHE[
        "catalog"
    ]

    if (
        not force_refresh
        and
        cached_catalog is not None
        and
        now
        - PRODUCTS_CACHE[
            "loaded_at"
        ]
        < INSTAGRAM_PRODUCTS_CACHE_TTL
    ):

        return cached_catalog

    with PRODUCTS_CACHE_LOCK:

        now = time.monotonic()

        cached_catalog = PRODUCTS_CACHE[
            "catalog"
        ]

        if (
            not force_refresh
            and
            cached_catalog is not None
            and
            now
            - PRODUCTS_CACHE[
                "loaded_at"
            ]
            < INSTAGRAM_PRODUCTS_CACHE_TTL
        ):

            return cached_catalog

        try:

            catalog = fetch_product_catalog()

            PRODUCTS_CACHE.update(
                {
                    "catalog": catalog,
                    "loaded_at": now,
                    "source": "google_sheets",
                }
            )

            print(
                "INSTAGRAM PRODUCTS LOADED:",
                {
                    "products": len(
                        catalog[
                            "rows"
                        ]
                    ),
                    "models": len(
                        catalog[
                            "families"
                        ]
                    ),
                    "sheet": INSTAGRAM_PRODUCTS_SHEET_NAME,
                },
            )

            return catalog

        except Exception as exc:

            print(
                "INSTAGRAM PRODUCTS LOAD FAILED:",
                repr(
                    exc
                )[
                    :1000
                ],
            )

            if cached_catalog is not None:

                PRODUCTS_CACHE.update(
                    {
                        "loaded_at": now,
                        "source": "stale_cache",
                    }
                )

                return cached_catalog

            PRODUCTS_CACHE.update(
                {
                    "loaded_at": now,
                    "source": "unavailable",
                }
            )

            return None


def model_alias_pattern(
    alias: str,
):

    return re.compile(
        r"(?<![a-zа-я0-9'])"
        + re.escape(
            alias
        ).replace(
            r"\ ",
            r"\s+",
        )
        + r"(?![a-zа-я0-9'])",
        flags=re.IGNORECASE,
    )


def find_price_model_in_text(
    text: str,
    *,
    catalog: dict | None = None,
) -> dict:

    active_catalog = (
        catalog
        if catalog is not None
        else get_product_catalog()
    )

    if not active_catalog:

        return {
            "status": "unavailable",
            "family": None,
            "alias": None,
        }

    normalized_text = normalize_model_text(
        text
    )

    if not normalized_text:

        return {
            "status": "not_found",
            "family": None,
            "alias": None,
        }

    matches = []

    for alias in active_catalog[
        "sorted_aliases"
    ]:

        if not model_alias_pattern(
            alias
        ).search(
            normalized_text
        ):

            continue

        matches.append(
            {
                "alias": alias,
                "family_keys": active_catalog[
                    "aliases"
                ][
                    alias
                ],
                "score": (
                    len(
                        alias.split()
                    ),
                    len(
                        alias
                    ),
                ),
            }
        )

    if not matches:

        return {
            "status": "not_found",
            "family": None,
            "alias": None,
        }

    best_score = max(
        match[
            "score"
        ]
        for match in matches
    )

    best_matches = [
        match
        for match in matches
        if match[
            "score"
        ] == best_score
    ]

    family_keys = set()

    for match in best_matches:
        family_keys.update(
            match[
                "family_keys"
            ]
        )

    if len(
        family_keys
    ) != 1:

        return {
            "status": "ambiguous",
            "family": None,
            "alias": best_matches[
                0
            ][
                "alias"
            ],
        }

    family_key = next(
        iter(
            family_keys
        )
    )

    return {
        "status": "found",
        "family": active_catalog[
            "families"
        ][
            family_key
        ],
        "alias": best_matches[
            0
        ][
            "alias"
        ],
        "kurs": active_catalog[
            "kurs"
        ],
    }


def format_live_price(
    price: Decimal,
    kurs: Decimal,
) -> str:

    uzs = price * kurs

    if uzs > 10000:

        uzs = (
            uzs
            / Decimal(
                "1000"
            )
        ).quantize(
            Decimal(
                "1"
            ),
            rounding=ROUND_HALF_UP,
        ) * Decimal(
            "1000"
        )

    else:

        uzs = uzs.quantize(
            Decimal(
                "1"
            ),
            rounding=ROUND_HALF_UP,
        )

    uzs_text = f"{int(uzs):,}".replace(
        ",",
        " ",
    )

    return f"{uzs_text} So'm"


def memory_sort_key(
    memory: str,
):

    values = [
        int(
            value
        )
        for value in re.findall(
            r"\d+",
            memory,
        )
    ]

    return (
        values[
            -1
        ]
        if values
        else 999999,
        normalize_model_text(
            memory
        ),
    )


def concrete_model_qualifier(
    model_name: str,
) -> str:

    match = (
        SIM_VARIANT_SUFFIX_PATTERN.search(
            model_name
        )
        or MARKET_VARIANT_SUFFIX_PATTERN.search(
            model_name
        )
        or CONNECTOR_SUFFIX_PATTERN.search(
            model_name
        )
    )

    if not match:
        return ""

    qualifier = model_name[
        match.start():
    ].strip()

    return qualifier.strip(
        "() "
    )


def build_live_product_message(
    family: dict,
    kurs: Decimal,
    *,
    text_settings: dict[str, str] | None = None,
) -> str:

    active_texts = {
        "model_intro": "",
        "model_prices_label": "Актуальные цены:",
        "model_course_label": "💱 Курс:",
        "model_footer": (
            "Цвета и точное наличие уточняйте у менеджера:\n"
            "https://t.me/texnikach_admin"
        ),
        "model_other_variants":
            "Другие варианты уточните у менеджера.",
        "model_from_prefix": "от",
        "model_empty_memory_label": "Цена",
    }

    if text_settings:

        active_texts.update(
            text_settings
        )

    rows_by_model = {}

    for row in family[
        "rows"
    ]:

        rows_by_model.setdefault(
            row[
                "model_name"
            ],
            [],
        ).append(
            row
        )

    detail_lines = []
    multiple_concrete_models = (
        len(
            rows_by_model
        ) > 1
    )

    for model_name, model_rows in rows_by_model.items():

        qualifier = concrete_model_qualifier(
            model_name
        )

        if multiple_concrete_models:

            detail_lines.append(
                (
                    qualifier
                    or model_name
                )
                + ":"
            )

        rows_by_memory = {}

        for row in model_rows:

            memory = row[
                "memory"
            ]

            rows_by_memory.setdefault(
                memory,
                [],
            ).append(
                row
            )

        for memory in sorted(
            rows_by_memory,
            key=memory_sort_key,
        ):

            price_values = sorted(
                {
                    row[
                        "price"
                    ]
                    for row in rows_by_memory[
                        memory
                    ]
                }
            )

            minimum_price = price_values[
                0
            ]

            price_text = format_live_price(
                minimum_price,
                kurs,
            )

            if len(
                price_values
            ) > 1:
                price_text = (
                    str(
                        active_texts.get(
                            "model_from_prefix"
                        )
                        or ""
                    ).strip()
                    + " "
                    + price_text
                ).strip()

            label = (
                memory
                or active_texts[
                    "model_empty_memory_label"
                ]
            )

            detail_lines.append(
                f"• {label} — {price_text}"
            )

        if multiple_concrete_models:
            detail_lines.append(
                ""
            )

    while (
        detail_lines
        and
        detail_lines[
            -1
        ] == ""
    ):
        detail_lines.pop()

    kurs_text = f"{int(kurs):,}".replace(
        ",",
        " ",
    )

    intro_text = str(
        active_texts.get(
            "model_intro"
        )
        or ""
    ).strip()

    heading_lines = []

    if intro_text:

        heading_lines.extend(
            intro_text.splitlines()
        )

        heading_lines.append(
            ""
        )

    heading_lines.extend(
        [
            family[
                "name"
            ],
            "",
            str(
                active_texts[
                    "model_prices_label"
                ]
            ),
        ]
    )

    model_footer = str(
        active_texts.get(
            "model_footer"
        )
        or ""
    ).strip()

    footer_lines = [
        "",
        (
            str(
                active_texts[
                    "model_course_label"
                ]
            ).rstrip()
            + " "
            + kurs_text
        ).strip(),
    ]

    if model_footer:

        footer_lines.extend(
            model_footer.splitlines()
        )

    selected_detail_lines = []
    omitted = False

    for line in detail_lines:

        candidate_lines = [
            *heading_lines,
            *selected_detail_lines,
            line,
            *footer_lines,
        ]

        if len(
            "\n".join(
                candidate_lines
            )
        ) > INSTAGRAM_PRICE_MESSAGE_LIMIT:

            omitted = True
            break

        selected_detail_lines.append(
            line
        )

    if omitted:

        omitted_lines = [
            "",
            str(
                active_texts[
                    "model_other_variants"
                ]
            ),
        ]

        while selected_detail_lines:

            candidate_message = "\n".join(
                [
                    *heading_lines,
                    *selected_detail_lines,
                    *omitted_lines,
                    *footer_lines,
                ]
            )

            if len(
                candidate_message
            ) <= INSTAGRAM_PRICE_MESSAGE_LIMIT:
                break

            selected_detail_lines.pop()

        selected_detail_lines.extend(
            omitted_lines
        )

    return "\n".join(
        [
            *heading_lines,
            *selected_detail_lines,
            *footer_lines,
        ]
    )


def limit_instagram_message(
    message: str,
) -> str:

    text = str(
        message
        or ""
    ).strip()

    if len(
        text
    ) <= INSTAGRAM_PRICE_MESSAGE_LIMIT:
        return text

    return (
        text[
            :INSTAGRAM_PRICE_MESSAGE_LIMIT
            - 1
        ].rstrip()
        + "…"
    )


DIRECT_URL_PATTERN = re.compile(
    r"https?://[^\s<>()]+",
    flags=re.IGNORECASE,
)


def limit_direct_message(
    message: str,
) -> str:

    text = str(
        message
        or ""
    ).strip()

    if len(
        text
    ) <= INSTAGRAM_PRICE_MESSAGE_LIMIT:
        return text

    urls = []

    for matched_url in DIRECT_URL_PATTERN.findall(
        text
    ):

        url = matched_url.rstrip(
            ".,;:!?"
        )

        if (
            url
            and
            url not in urls
        ):
            urls.append(
                url
            )

    if not urls:
        return limit_instagram_message(
            text
        )

    links_footer = "\n".join(
        urls
    )

    separator = "\n\n"

    body_limit = (
        INSTAGRAM_PRICE_MESSAGE_LIMIT
        - len(
            separator
            + links_footer
        )
        - 1
    )

    if body_limit < 1:
        return limit_instagram_message(
            text
        )

    body = text[
        :body_limit
    ].rstrip()

    return (
        body
        + "…"
        + separator
        + links_footer
    )


def build_direct_product_message(
    family: dict,
    kurs: Decimal,
    settings: dict[str, str],
) -> str:

    text_keys = {
        "model_intro",
        "model_prices_label",
        "model_course_label",
        "model_footer",
        "model_other_variants",
        "model_from_prefix",
        "model_empty_memory_label",
    }

    text_settings = {
        key: render_direct_template(
            settings.get(
                key,
                "",
            ),
            settings,
        )
        for key in text_keys
        if key in settings
    }

    return limit_direct_message(
        build_live_product_message(
            family,
            kurs,
            text_settings=text_settings,
        )
    )


# =========================================================
# INSTAGRAM POST -> PRODUCT MODELS
# =========================================================

POST_MODELS_REQUIRED_HEADERS = {
    "enabled",
    "media_id",
    "permalink",
    "models",
    "caption",
    "published_at",
    "updated_at",
}


def split_post_model_names(
    value: str | None,
) -> list[str]:

    result = []

    for item in re.split(
        r"[,;\n]+",
        str(
            value
            or ""
        ),
    ):

        model_name = item.strip()

        if (
            model_name
            and
            model_name not in result
        ):

            result.append(
                model_name
            )

    return result


def parse_post_models_csv(
    csv_text: str,
) -> dict[str, list[str]]:

    rows = read_csv_rows(
        csv_text,
        required_headers=
            POST_MODELS_REQUIRED_HEADERS,
    )

    mappings = {}

    for row in rows:

        if not parse_enabled(
            row.get(
                "enabled"
            )
        ):

            continue

        media_id = str(
            row.get(
                "media_id"
            )
            or ""
        ).strip()

        if not media_id:
            continue

        model_names = split_post_model_names(
            row.get(
                "models"
            )
        )

        if not model_names:
            continue

        target = mappings.setdefault(
            media_id,
            [],
        )

        for model_name in model_names:

            if model_name not in target:
                target.append(
                    model_name
                )

    return mappings


def fetch_post_model_mappings() -> dict[str, list[str]]:

    url = (
        "https://docs.google.com/spreadsheets/d/"
        f"{INSTAGRAM_RULES_SHEET_ID}/gviz/tq"
    )

    response = POSTS_HTTP.get(
        url,
        params={
            "tqx": "out:csv",
            "sheet": INSTAGRAM_POST_MODELS_SHEET_NAME,
        },
        timeout=INSTAGRAM_RULES_HTTP_TIMEOUT,
    )

    if not response.ok:

        print(
            "INSTAGRAM POST MODELS DOWNLOAD ERROR:",
            response.status_code,
            response.text[
                :500
            ],
        )

        response.raise_for_status()

    return parse_post_models_csv(
        response.content.decode(
            "utf-8-sig"
        )
    )


def get_post_model_mappings(
    *,
    force_refresh: bool = False,
) -> dict[str, list[str]]:

    now = time.monotonic()

    cached_mappings = POST_MODELS_CACHE[
        "mappings"
    ]

    if (
        not force_refresh
        and
        POST_MODELS_CACHE[
            "loaded_at"
        ]
        and
        now
        - POST_MODELS_CACHE[
            "loaded_at"
        ]
        < INSTAGRAM_RULES_CACHE_TTL
    ):

        return cached_mappings

    with POST_MODELS_CACHE_LOCK:

        now = time.monotonic()

        if (
            not force_refresh
            and
            POST_MODELS_CACHE[
                "loaded_at"
            ]
            and
            now
            - POST_MODELS_CACHE[
                "loaded_at"
            ]
            < INSTAGRAM_RULES_CACHE_TTL
        ):

            return POST_MODELS_CACHE[
                "mappings"
            ]

        try:

            mappings = fetch_post_model_mappings()

            POST_MODELS_CACHE.update(
                {
                    "mappings": mappings,
                    "loaded_at": now,
                    "source": "google_sheets",
                }
            )

            print(
                "INSTAGRAM POST MODELS LOADED:",
                {
                    "mapped_posts": len(
                        mappings
                    ),
                    "sheet": INSTAGRAM_POST_MODELS_SHEET_NAME,
                },
            )

            return mappings

        except Exception as exc:

            print(
                "INSTAGRAM POST MODELS LOAD FAILED:",
                repr(
                    exc
                )[
                    :1000
                ],
            )

            if cached_mappings:

                POST_MODELS_CACHE.update(
                    {
                        "loaded_at": now,
                        "source": "stale_cache",
                    }
                )

                return cached_mappings

            POST_MODELS_CACHE.update(
                {
                    "mappings": {},
                    "loaded_at": now,
                    "source": "unavailable",
                }
            )

            return {}


def resolve_post_model_families(
    media_id: str,
    *,
    catalog: dict,
    mappings: dict[str, list[str]] | None = None,
) -> dict:

    active_mappings = (
        mappings
        if mappings is not None
        else get_post_model_mappings()
    )

    configured_models = active_mappings.get(
        str(
            media_id
            or ""
        ),
        [],
    )

    families = []
    family_keys = set()
    invalid_models = []

    for configured_model in configured_models:

        resolution = find_price_model_in_text(
            configured_model,
            catalog=catalog,
        )

        if (
            resolution[
                "status"
            ]
            != "found"
        ):

            invalid_models.append(
                configured_model
            )

            continue

        family = resolution[
            "family"
        ]

        family_key = family[
            "key"
        ]

        if family_key in family_keys:
            continue

        family_keys.add(
            family_key
        )

        families.append(
            family
        )

    return {
        "families": families,
        "invalid_models": invalid_models,
        "configured_models": configured_models,
    }


def build_post_models_message(
    families: list[dict],
    kurs: Decimal,
) -> str:

    if len(
        families
    ) == 1:

        return build_live_product_message(
            families[
                0
            ],
            kurs,
        )

    price_lines = []

    for family in families:

        prices = sorted(
            {
                row[
                    "price"
                ]
                for row in family[
                    "rows"
                ]
            }
        )

        if not prices:
            continue

        price_text = format_live_price(
            prices[
                0
            ],
            kurs,
        )

        if len(
            prices
        ) > 1:
            price_text = (
                "от "
                + price_text
            )

        price_lines.append(
            f"• {family['name']} — {price_text}"
        )

    footer_lines = [
        "",
        "Цвета, память и точное наличие уточняйте у менеджера:",
        "https://t.me/texnikach_admin",
    ]

    selected_lines = []
    omitted = False

    for line in price_lines:

        candidate = "\n".join(
            [
                "Актуальные цены моделей:",
                "",
                *selected_lines,
                line,
                *footer_lines,
            ]
        )

        if len(
            candidate
        ) > INSTAGRAM_PRICE_MESSAGE_LIMIT:

            omitted = True
            break

        selected_lines.append(
            line
        )

    if omitted:
        selected_lines.append(
            "• Другие модели — у менеджера"
        )

    return "\n".join(
        [
            "Актуальные цены моделей:",
            "",
            *selected_lines,
            *footer_lines,
        ]
    )


# =========================================================
# AUTOMATIC INSTAGRAM POSTS -> GOOGLE SHEETS SYNC
# =========================================================

def get_google_write_client():

    global GOOGLE_WRITE_CLIENT

    if GOOGLE_WRITE_CLIENT is not None:
        return GOOGLE_WRITE_CLIENT

    with GOOGLE_WRITE_CLIENT_LOCK:

        if GOOGLE_WRITE_CLIENT is not None:
            return GOOGLE_WRITE_CLIENT

        google_json = os.getenv(
            "GOOGLE_SA_JSON_CONTENT",
            "",
        ).strip()

        google_json_path = os.getenv(
            "GOOGLE_SA_JSON",
            "Data/sheets-auto-update-484813-fe0f96f83d38.json",
        ).strip()

        try:

            import gspread

            from google.oauth2.service_account import Credentials

            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]

            if google_json:

                credentials = Credentials.from_service_account_info(
                    json.loads(
                        google_json
                    ),
                    scopes=scopes,
                )

            elif (
                google_json_path
                and
                os.path.exists(
                    google_json_path
                )
            ):

                credentials = Credentials.from_service_account_file(
                    google_json_path,
                    scopes=scopes,
                )

            else:

                return None

            GOOGLE_WRITE_CLIENT = gspread.authorize(
                credentials
            )

            return GOOGLE_WRITE_CLIENT

        except Exception as exc:

            print(
                "INSTAGRAM POST SYNC GOOGLE AUTH FAILED:",
                repr(
                    exc
                )[
                    :1000
                ],
            )

            return None


def fetch_recent_instagram_media() -> list[dict]:

    if not INSTAGRAM_ACCOUNT_ID:

        raise RuntimeError(
            "INSTAGRAM_ACCOUNT_ID is not configured"
        )

    url = (
        f"{GRAPH_BASE_URL}/"
        f"{INSTAGRAM_ACCOUNT_ID}/media"
    )

    response = POSTS_HTTP.get(
        url,
        headers=instagram_headers(),
        params={
            "fields": (
                "id,permalink,caption,"
                "media_type,media_product_type,timestamp"
            ),
            "limit": INSTAGRAM_POST_SYNC_LIMIT,
        },
        timeout=30,
    )

    if not response.ok:

        print(
            "INSTAGRAM POST SYNC META ERROR:",
            response.status_code,
            response.text[
                :1000
            ],
        )

        response.raise_for_status()

    data = response.json()

    return (
        data.get(
            "data"
        )
        or []
    )


def sync_instagram_posts_to_sheet() -> dict:

    google_client = get_google_write_client()

    if google_client is None:

        return {
            "status": "disabled",
            "added": 0,
            "reason": "google_write_credentials_missing",
        }

    spreadsheet = google_client.open_by_key(
        INSTAGRAM_RULES_SHEET_ID
    )

    worksheet = spreadsheet.worksheet(
        INSTAGRAM_POST_MODELS_SHEET_NAME
    )

    values = worksheet.get_all_values()

    if not values:

        raise ValueError(
            "post_models sheet has no header row"
        )

    headers = [
        str(
            value
        ).strip().lower()
        for value in values[
            0
        ]
    ]

    missing_headers = (
        POST_MODELS_REQUIRED_HEADERS
        - set(
            headers
        )
    )

    if missing_headers:

        raise ValueError(
            "post_models is missing columns: "
            + ", ".join(
                sorted(
                    missing_headers
                )
            )
        )

    media_id_index = headers.index(
        "media_id"
    )

    existing_media_ids = {
        row[
            media_id_index
        ].strip()
        for row in values[
            1:
        ]
        if (
            media_id_index
            < len(
                row
            )
            and
            row[
                media_id_index
            ].strip()
        )
    }

    media_items = fetch_recent_instagram_media()

    now_text = datetime.now(
        timezone.utc
    ).isoformat()

    new_rows = []

    for media in reversed(
        media_items
    ):

        media_id = str(
            media.get(
                "id"
            )
            or ""
        ).strip()

        if (
            not media_id
            or
            media_id in existing_media_ids
        ):

            continue

        row_by_header = {
            "enabled": "TRUE",
            "media_id": media_id,
            "permalink": str(
                media.get(
                    "permalink"
                )
                or ""
            ).strip(),
            "models": "",
            "caption": str(
                media.get(
                    "caption"
                )
                or ""
            ).strip()[
                :5000
            ],
            "published_at": str(
                media.get(
                    "timestamp"
                )
                or ""
            ).strip(),
            "updated_at": now_text,
        }

        new_rows.append(
            [
                row_by_header.get(
                    header,
                    "",
                )
                for header in headers
            ]
        )

        existing_media_ids.add(
            media_id
        )

    if new_rows:

        worksheet.append_rows(
            new_rows,
            value_input_option="RAW",
        )

        POST_MODELS_CACHE[
            "loaded_at"
        ] = 0.0

    result = {
        "status": "ok",
        "added": len(
            new_rows
        ),
        "received": len(
            media_items
        ),
    }

    print(
        "INSTAGRAM POST SYNC DONE:",
        result,
    )

    return result


async def instagram_post_sync_loop():

    await asyncio.sleep(
        5
    )

    while True:

        try:

            result = await asyncio.to_thread(
                sync_instagram_posts_to_sheet
            )

            if result.get(
                "status"
            ) == "disabled":

                print(
                    "INSTAGRAM POST SYNC DISABLED:",
                    result.get(
                        "reason"
                    ),
                )

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            print(
                "INSTAGRAM POST SYNC FAILED:",
                repr(
                    exc
                )[
                    :1000
                ],
            )

        await asyncio.sleep(
            INSTAGRAM_POST_SYNC_INTERVAL
        )


@router.on_event(
    "startup"
)
async def start_instagram_post_sync():

    global POST_SYNC_TASK

    if (
        not INSTAGRAM_POST_SYNC_ENABLED
        or
        not INSTAGRAM_ACCESS_TOKEN
        or
        not INSTAGRAM_ACCOUNT_ID
    ):

        return

    if (
        POST_SYNC_TASK is None
        or
        POST_SYNC_TASK.done()
    ):

        POST_SYNC_TASK = asyncio.create_task(
            instagram_post_sync_loop()
        )


@router.on_event(
    "shutdown"
)
async def stop_instagram_post_sync():

    global POST_SYNC_TASK

    if POST_SYNC_TASK is None:
        return

    POST_SYNC_TASK.cancel()

    try:
        await POST_SYNC_TASK
    except asyncio.CancelledError:
        pass

    POST_SYNC_TASK = None


# =========================================================
# PRODUCT DATABASE
# =========================================================

def get_product(
    product_id: int,
):

    with connect_instagram_db() as conn:

        return conn.execute(
            """
            SELECT *

            FROM instagram_products

            WHERE
                id = ?

                AND

                active = 1
            """,
            (
                product_id,
            ),
        ).fetchone()


def get_product_for_media(
    media_id: str,
):

    if not media_id:
        return None

    with connect_instagram_db() as conn:

        return conn.execute(
            """
            SELECT
                p.*

            FROM instagram_post_products
                AS map

            INNER JOIN instagram_products
                AS p

                ON p.id =
                    map.product_id

            WHERE
                map.media_id = ?

                AND

                p.active = 1

            LIMIT 1
            """,
            (
                str(
                    media_id
                ),
            ),
        ).fetchone()


def get_all_aliases():

    with connect_instagram_db() as conn:

        return conn.execute(
            """
            SELECT

                a.product_id,

                a.alias,

                a.alias_normalized,

                p.name

            FROM instagram_product_aliases
                AS a

            INNER JOIN instagram_products
                AS p

                ON p.id =
                    a.product_id

            WHERE
                p.active = 1

            ORDER BY
                LENGTH(
                    a.alias_normalized
                ) DESC
            """
        ).fetchall()


# =========================================================
# ALIAS MATCHING
# =========================================================

def alias_pattern(
    alias: str,
):

    escaped = re.escape(
        alias
    )

    escaped = escaped.replace(
        r"\ ",
        r"\s+",
    )

    return re.compile(
        r"(?<![\w])"
        + escaped
        + r"(?![\w+])",
        flags=re.IGNORECASE,
    )


def has_unmatched_model_suffix(
    text: str,
    match_end: int,
) -> bool:

    tail = (
        text[
            match_end:
        ]
        .strip()
    )

    if not tail:
        return False

    if tail.startswith(
        "+"
    ):

        return True

    next_word_match = re.match(
        r"^([a-z0-9]+)",
        tail,
        flags=re.IGNORECASE,
    )

    if not next_word_match:
        return False

    next_word = (
        next_word_match
        .group(1)
        .lower()
    )

    return (
        next_word
        in MODEL_SUFFIX_WORDS
    )


def find_product_in_comment(
    text: str,
):

    normalized = normalize_text(
        text
    )

    if not normalized:
        return {
            "status":
                "not_found",

            "product":
                None,
        }

    aliases = get_all_aliases()

    matches = []

    for row in aliases:

        alias = (
            row[
                "alias_normalized"
            ]
            or ""
        )

        if not alias:
            continue

        pattern = alias_pattern(
            alias
        )

        match = pattern.search(
            normalized
        )

        if not match:
            continue

        # -------------------------------------------------
        # НЕ ДАЕМ:
        #
        # alias = s25
        # comment = s25 ultra
        #
        # если Ultra является отдельной моделью.
        # -------------------------------------------------

        if has_unmatched_model_suffix(
            normalized,
            match.end(),
        ):

            continue

        matches.append(
            {
                "product_id":
                    row[
                        "product_id"
                    ],

                "alias":
                    alias,

                "length":
                    len(
                        alias
                    ),
            }
        )

    if not matches:

        return {
            "status":
                "not_found",

            "product":
                None,
        }

    longest = max(
        item[
            "length"
        ]
        for item
        in matches
    )

    best = [
        item
        for item
        in matches
        if item[
            "length"
        ] == longest
    ]

    product_ids = {
        item[
            "product_id"
        ]
        for item
        in best
    }

    # -------------------------------------------------
    # НЕОДНОЗНАЧНО
    # -------------------------------------------------

    if len(
        product_ids
    ) != 1:

        return {
            "status":
                "ambiguous",

            "product":
                None,
        }

    product_id = next(
        iter(
            product_ids
        )
    )

    product = get_product(
        product_id
    )

    if not product:

        return {
            "status":
                "not_found",

            "product":
                None,
        }

    return {
        "status":
            "found",

        "product":
            product,
    }


# =========================================================
# LOOKS LIKE MODEL
# =========================================================

def looks_like_model_request(
    text: str,
) -> bool:

    normalized = normalize_text(
        text
    )

    # Например:
    #
    # s25
    # a56
    # note 15
    # iphone 17
    # 17 pro
    # x9d
    #
    patterns = [

        r"\b[a-z]{1,10}\s?\d{1,4}[a-z]?\b",

        r"\b\d{1,3}\s+"
        r"(pro|max|ultra|plus|fe|lite|mini)\b",

        r"\biphone\s+\d{1,3}\b",

        r"\bgalaxy\s+[a-z]?\d{1,4}\b",

        r"\bredmi\s+"
        r"(note\s+)?\d{1,3}\b",

        r"\bxiaomi\s+\d{1,3}\b",

        r"\bhonor\s+[a-z]?\d{1,4}\b",

        r"\bpoco\s+[a-z]?\d{1,4}\b",
    ]

    for pattern in patterns:

        if re.search(
            pattern,
            normalized,
            flags=re.IGNORECASE,
        ):

            return True

    return False


# =========================================================
# PRODUCT RESOLUTION
# =========================================================

def resolve_product(
    comment_text: str,
    media_id: str,
):

    # =====================================================
    # PRIORITY 1:
    # MODEL WRITTEN IN COMMENT
    # =====================================================

    comment_result = (
        find_product_in_comment(
            comment_text
        )
    )

    if (
        comment_result[
            "status"
        ]
        == "found"
    ):

        return {
            "action":
                "product",

            "product":
                comment_result[
                    "product"
                ],

            "source":
                "comment",
        }

    if (
        comment_result[
            "status"
        ]
        == "ambiguous"
    ):

        return {
            "action":
                "fallback",

            "product":
                None,

            "source":
                "ambiguous_comment",
        }

    # =====================================================
    # USER SEEMS TO HAVE WRITTEN A MODEL,
    # BUT WE DON'T HAVE IT
    #
    # NEVER FALL BACK TO POST MODEL.
    #
    # Example:
    #
    # POST = S25 Ultra
    # COMMENT = A57 narx
    #
    # If A57 isn't in DB:
    # do NOT send S25 Ultra.
    # =====================================================

    if looks_like_model_request(
        comment_text
    ):

        return {
            "action":
                "fallback",

            "product":
                None,

            "source":
                "unknown_model",
        }

    # =====================================================
    # NO MODEL.
    # ONLY RESPOND TO GENERIC TRIGGERS.
    # =====================================================

    if not is_generic_trigger(
        comment_text
    ):

        return {
            "action":
                "ignore",

            "product":
                None,

            "source":
                "not_trigger",
        }

    # =====================================================
    # PRIORITY 2:
    # PRODUCT CONNECTED TO POST / REEL
    # =====================================================

    post_product = get_product_for_media(
        media_id
    )

    if post_product:

        return {
            "action":
                "product",

            "product":
                post_product,

            "source":
                "post",
        }

    # =====================================================
    # NO POST MAPPING
    # =====================================================

    return {
        "action":
            "fallback",

        "product":
            None,

        "source":
            "post_not_mapped",
    }


# =========================================================
# MESSAGE BUILDERS
# =========================================================

def build_fallback_message(
    language: str,
) -> str:

    if language == "uz":

        return (
            "Assalomu alaykum! "
            "Texnikach mahsulotlarining "
            "aktual narxlari va mavjudligini "
            "Telegram kanalimizda ko‘rishingiz mumkin.\n\n"

            "Buyurtma yoki aniq model bo‘yicha "
            "ma'lumot olish uchun menejerimizga yozing:\n"
            "https://t.me/texnikach_admin"
        )

    return (
        "Здравствуйте! "
        "Актуальные цены и наличие товаров "
        "Texnikach можно посмотреть "
        "в нашем Telegram-канале.\n\n"

        "Для заказа или уточнения конкретной модели "
        "напишите нашему менеджеру:\n"
        "https://t.me/texnikach_admin"
    )


def build_product_message(
    product,
    language: str,
) -> str:

    name = (
        product[
            "name"
        ]
        or ""
    ).strip()

    price = (
        product[
            "price_text"
        ]
        or ""
    ).strip()

    availability = (
        product[
            "availability_text"
        ]
        or ""
    ).strip()

    variants = (
        product[
            "variants_text"
        ]
        or ""
    ).strip()

    # -------------------------------------------------
    # CRITICAL:
    #
    # Не придумываем.
    # Если нет цены или наличия в БД,
    # отправляем fallback вместо неполных данных.
    # -------------------------------------------------

    if (
        not name
        or
        not price
        or
        not availability
    ):

        return build_fallback_message(
            language
        )

    if language == "uz":

        lines = [
            "Assalomu alaykum! "
            "Men Texnikach avtomatik yordamchisiman.",
            "",
            name,
            "",
            "Aktual narx:",
            price,
            "",
            "Mavjudligi:",
            availability,
        ]

        if variants:

            lines.extend(
                [
                    "",
                    "Variantlar:",
                    variants,
                ]
            )

        lines.extend(
            [
                "",
                "Buyurtma uchun menejerimizga yozing:",
                "https://t.me/texnikach_admin",
            ]
        )

        return "\n".join(
            lines
        )

    lines = [
        "Здравствуйте! "
        "Я автоматический помощник Texnikach.",
        "",
        name,
        "",
        "Актуальная цена:",
        price,
        "",
        "Наличие:",
        availability,
    ]

    if variants:

        lines.extend(
            [
                "",
                "Доступные варианты:",
                variants,
            ]
        )

    lines.extend(
        [
            "",
            "Для оформления заказа напишите менеджеру:",
            "https://t.me/texnikach_admin",
        ]
    )

    return "\n".join(
        lines
    )


def build_public_reply(
    language: str,
) -> str:

    if language == "uz":

        return (
            "Ma'lumotni Directga yubordik ✅"
        )

    return (
        "Ответили в Direct ✅"
    )


# =========================================================
# META API
# =========================================================

def instagram_headers():

    if not INSTAGRAM_ACCESS_TOKEN:

        raise RuntimeError(
            "INSTAGRAM_ACCESS_TOKEN is not configured"
        )

    return {
        "Authorization":
            (
                "Bearer "
                + INSTAGRAM_ACCESS_TOKEN
            ),

        "Content-Type":
            "application/json",
    }


# =========================================================
# PRIVATE REPLY
# =========================================================

def send_private_reply(
    instagram_account_id: str,
    comment_id: str,
    message: str,
):

    if not instagram_account_id:

        raise RuntimeError(
            "Instagram account ID is empty"
        )

    if not comment_id:

        raise RuntimeError(
            "Comment ID is empty"
        )

    url = (
        f"{GRAPH_BASE_URL}/"
        f"{instagram_account_id}/messages"
    )

    payload = {

        "recipient": {
            "comment_id":
                comment_id,
        },

        "message": {
            "text":
                message,
        },
    }

    response = HTTP.post(
        url,
        headers=instagram_headers(),
        json=payload,
        timeout=30,
    )

    if not response.ok:

        print(
            "INSTAGRAM PRIVATE REPLY ERROR:",
            response.status_code,
            response.text[
                :1000
            ],
        )

        response.raise_for_status()

    result = response.json()

    print(
        "INSTAGRAM PRIVATE REPLY SENT:",
        {
            "comment_id":
                comment_id,

            "result":
                result,
        },
    )

    return result


# =========================================================
# DIRECT MESSAGE
# =========================================================

def send_direct_message(
    instagram_account_id: str,
    recipient_id: str,
    message: str,
):

    if not instagram_account_id:

        raise RuntimeError(
            "Instagram account ID is empty"
        )

    if not recipient_id:

        raise RuntimeError(
            "Direct recipient ID is empty"
        )

    message_text = limit_direct_message(
        message
    )

    if not message_text:

        raise RuntimeError(
            "Direct message text is empty"
        )

    url = (
        f"{GRAPH_BASE_URL}/"
        f"{instagram_account_id}/messages"
    )

    payload = {
        "recipient": {
            "id": recipient_id,
        },
        "message": {
            "text": message_text,
        },
    }

    response = HTTP.post(
        url,
        headers=instagram_headers(),
        json=payload,
        timeout=30,
    )

    if not response.ok:

        print(
            "INSTAGRAM DIRECT SEND ERROR:",
            response.status_code,
            response.text[
                :1000
            ],
        )

        response.raise_for_status()

    result = response.json()

    print(
        "INSTAGRAM DIRECT SENT:",
        {
            "recipient_id": recipient_id,
            "message_id": result.get(
                "message_id"
            ),
        },
    )

    return result


# =========================================================
# PUBLIC COMMENT REPLY
# =========================================================

def send_public_comment_reply(
    comment_id: str,
    message: str,
):

    if not comment_id:

        raise RuntimeError(
            "Comment ID is empty"
        )

    url = (
        f"{GRAPH_BASE_URL}/"
        f"{comment_id}/replies"
    )

    payload = {
        "message":
            message,
    }

    response = HTTP.post(
        url,
        headers=instagram_headers(),
        json=payload,
        timeout=30,
    )

    if not response.ok:

        print(
            "INSTAGRAM PUBLIC REPLY ERROR:",
            response.status_code,
            response.text[
                :1000
            ],
        )

        response.raise_for_status()

    result = response.json()

    print(
        "INSTAGRAM PUBLIC REPLY SENT:",
        {
            "comment_id":
                comment_id,

            "result":
                result,
        },
    )

    return result


# =========================================================
# COMMENT DEDUPLICATION
# =========================================================

def claim_comment(
    *,
    comment_id: str,
    media_id: str,
    instagram_user_id: str,
    username: str,
    comment_text: str,
) -> bool:

    if not comment_id:
        return False

    with connect_instagram_db() as conn:

        row = conn.execute(
            """
            SELECT
                status

            FROM instagram_processed_comments

            WHERE comment_id = ?
            """,
            (
                comment_id,
            ),
        ).fetchone()

        if row:

            status = (
                row[
                    "status"
                ]
                or ""
            )

            if status in {
                "processing",
                "done",
                "ignored",
            }:

                return False

        conn.execute(
            """
            INSERT INTO
            instagram_processed_comments (

                comment_id,
                media_id,
                instagram_user_id,
                username,
                comment_text,
                status,
                updated_at
            )

            VALUES (
                ?, ?, ?, ?, ?,
                'processing',
                CURRENT_TIMESTAMP
            )

            ON CONFLICT(comment_id)

            DO UPDATE SET

                media_id =
                    excluded.media_id,

                instagram_user_id =
                    excluded.instagram_user_id,

                username =
                    excluded.username,

                comment_text =
                    excluded.comment_text,

                status =
                    'processing',

                error =
                    NULL,

                updated_at =
                    CURRENT_TIMESTAMP
            """,
            (
                comment_id,
                media_id,
                instagram_user_id,
                username,
                comment_text,
            ),
        )

        conn.commit()

    return True


def update_comment_status(
    comment_id: str,
    status: str,
    *,
    product_id: int | None = None,
    detection_source: str | None = None,
    error: str | None = None,
):

    with connect_instagram_db() as conn:

        conn.execute(
            """
            UPDATE
                instagram_processed_comments

            SET
                status = ?,

                detected_product_id = ?,

                detection_source = ?,

                error = ?,

                updated_at =
                    CURRENT_TIMESTAMP

            WHERE
                comment_id = ?
            """,
            (
                status,
                product_id,
                detection_source,
                error,
                comment_id,
            ),
        )

        conn.commit()


# =========================================================
# DIRECT MESSAGE DEDUPLICATION
# =========================================================

def claim_direct_message(
    *,
    message_id: str,
    sender_id: str,
    recipient_id: str,
) -> bool:

    if (
        not message_id
        or
        not sender_id
    ):
        return False

    with connect_instagram_db() as conn:

        cursor = conn.execute(
            """
            INSERT INTO
            instagram_processed_messages (

                message_id,
                sender_id,
                recipient_id,
                status,
                updated_at
            )

            VALUES (
                ?, ?, ?,
                'processing',
                CURRENT_TIMESTAMP
            )

            ON CONFLICT(message_id)

            DO UPDATE SET

                sender_id =
                    excluded.sender_id,

                recipient_id =
                    excluded.recipient_id,

                status =
                    'processing',

                detection_source =
                    NULL,

                error =
                    NULL,

                updated_at =
                    CURRENT_TIMESTAMP

            WHERE
                instagram_processed_messages.status =
                    'failed'

                OR (
                    instagram_processed_messages.status =
                        'processing'

                    AND

                    instagram_processed_messages.updated_at <
                        datetime(
                            'now',
                            '-10 minutes'
                        )
                )
            """,
            (
                message_id,
                sender_id,
                recipient_id,
            ),
        )

        conn.commit()

        return cursor.rowcount == 1


def update_direct_message_status(
    message_id: str,
    status: str,
    *,
    detection_source: str | None = None,
    error: str | None = None,
):

    with connect_instagram_db() as conn:

        conn.execute(
            """
            UPDATE
                instagram_processed_messages

            SET
                status = ?,

                detection_source = ?,

                error = ?,

                updated_at =
                    CURRENT_TIMESTAMP

            WHERE
                message_id = ?
            """,
            (
                status,
                detection_source,
                error,
                message_id,
            ),
        )

        conn.commit()


# =========================================================
# PROCESS DIRECT MESSAGE
# =========================================================

def process_instagram_direct_message(
    *,
    instagram_account_id: str,
    sender_id: str,
    message_id: str,
    message_text: str,
):

    try:

        print(
            "INSTAGRAM DIRECT RECEIVED:",
            {
                "sender_id": sender_id,
                "message_id": message_id,
                "has_text": bool(
                    str(
                        message_text
                        or ""
                    ).strip()
                ),
            },
        )

        config = get_direct_config()

        settings = config[
            "settings"
        ]

        catalog = get_product_catalog()

        if catalog is None:

            model_resolution = {
                "status": "unavailable",
                "family": None,
                "alias": None,
            }

        else:

            model_resolution = find_price_model_in_text(
                message_text,
                catalog=catalog,
            )

        if (
            model_resolution[
                "status"
            ]
            == "found"
        ):

            family = model_resolution[
                "family"
            ]

            reply_text = build_direct_product_message(
                family,
                model_resolution[
                    "kurs"
                ],
                settings,
            )

            source = "direct_product_model"

            print(
                "INSTAGRAM DIRECT PRODUCT MATCH:",
                {
                    "message_id": message_id,
                    "model": family[
                        "name"
                    ],
                    "variants": len(
                        family[
                            "rows"
                        ]
                    ),
                },
            )

        elif (
            model_resolution[
                "status"
            ]
            == "ambiguous"
            or
            looks_like_model_request(
                message_text
            )
        ):

            reply_text = render_direct_template(
                settings.get(
                    "model_not_found_reply",
                    LOCAL_DIRECT_SETTINGS[
                        "model_not_found_reply"
                    ],
                ),
                settings,
            )

            source = "direct_model_not_found"

        else:

            resolution = resolve_direct_rule(
                message_text,
                rules=config[
                    "rules"
                ],
            )

            reply_text = render_direct_template(
                resolution[
                    "rule"
                ][
                    "reply_text"
                ],
                settings,
            )

            source = resolution[
                "source"
            ]

            print(
                "INSTAGRAM DIRECT RULE MATCH:",
                {
                    "message_id": message_id,
                    "source": source,
                    "row": resolution[
                        "rule"
                    ].get(
                        "row_number"
                    ),
                },
            )

        send_direct_message(
            instagram_account_id,
            sender_id,
            reply_text,
        )

        update_direct_message_status(
            message_id,
            "done",
            detection_source=source,
        )

        print(
            "INSTAGRAM DIRECT DONE:",
            message_id,
        )

    except Exception as exc:

        print(
            "INSTAGRAM DIRECT ERROR:",
            message_id,
            repr(
                exc
            ),
        )

        update_direct_message_status(
            message_id,
            "failed",
            error=str(
                exc
            )[
                :2000
            ],
        )


# =========================================================
# PROCESS COMMENT
# =========================================================

def process_instagram_comment(
    *,
    instagram_account_id: str,
    commenter_id: str,
    username: str,
    media_id: str,
    comment_id: str,
    comment_text: str,
):

    try:

        print(
            "INSTAGRAM COMMENT:",
            {
                "username":
                    username,

                "media_id":
                    media_id,

                "comment_id":
                    comment_id,

                "text":
                    comment_text,
            },
        )

        # -------------------------------------------------
        # IGNORE OUR OWN COMMENTS
        # -------------------------------------------------

        if (
            username.lower()
            == INSTAGRAM_USERNAME
        ):

            update_comment_status(
                comment_id,
                "ignored",
                detection_source=
                    "own_comment",
            )

            print(
                "INSTAGRAM COMMENT IGNORED:"
                " own account"
            )

            return

        if (
            commenter_id
            and
            instagram_account_id
            and
            commenter_id
            == instagram_account_id
        ):

            update_comment_status(
                comment_id,
                "ignored",
                detection_source=
                    "own_comment_id",
            )

            return

        # -------------------------------------------------
        # PRODUCT MODEL HAS FIRST PRIORITY
        # -------------------------------------------------

        catalog = get_product_catalog()

        if catalog is None:

            model_resolution = {
                "status": "unavailable",
                "family": None,
                "alias": None,
            }

        else:

            model_resolution = find_price_model_in_text(
                comment_text,
                catalog=catalog,
            )

        if (
            model_resolution[
                "status"
            ]
            == "found"
        ):

            family = model_resolution[
                "family"
            ]

            private_message = build_live_product_message(
                family,
                model_resolution[
                    "kurs"
                ],
            )

            public_message = LOCAL_DEFAULT_RULE[
                "public_reply"
            ]

            source = "product_sheet_model"

            print(
                "INSTAGRAM PRODUCT MATCH:",
                {
                    "source": source,
                    "alias": model_resolution[
                        "alias"
                    ],
                    "model": family[
                        "name"
                    ],
                    "variants": len(
                        family[
                            "rows"
                        ]
                    ),
                },
            )

        else:

            response_selected = False

            if (
                model_resolution[
                    "status"
                ]
                == "ambiguous"
            ):

                print(
                    "INSTAGRAM PRODUCT MATCH AMBIGUOUS:",
                    {
                        "alias": model_resolution[
                            "alias"
                        ],
                    },
                )

            comment_has_model_request = (
                model_resolution[
                    "status"
                ]
                == "ambiguous"
                or looks_like_model_request(
                    comment_text
                )
            )

            # ---------------------------------------------
            # NO MODEL IN COMMENT: USE POST MAPPING
            # ---------------------------------------------

            if (
                not comment_has_model_request
                and
                catalog is not None
            ):

                post_resolution = resolve_post_model_families(
                    media_id,
                    catalog=catalog,
                )

                post_families = post_resolution[
                    "families"
                ]

                if post_resolution[
                    "invalid_models"
                ]:

                    print(
                        "INSTAGRAM POST MODELS INVALID:",
                        {
                            "media_id": media_id,
                            "models": post_resolution[
                                "invalid_models"
                            ],
                        },
                    )

                if post_families:

                    private_message = build_post_models_message(
                        post_families,
                        catalog[
                            "kurs"
                        ],
                    )

                    public_message = LOCAL_DEFAULT_RULE[
                        "public_reply"
                    ]

                    source = "post_sheet_models"
                    response_selected = True

                    print(
                        "INSTAGRAM POST MODELS MATCH:",
                        {
                            "source": source,
                            "media_id": media_id,
                            "models": [
                                family[
                                    "name"
                                ]
                                for family in post_families
                            ],
                        },
                    )

            # ---------------------------------------------
            # NO POST MODEL: USE GOOGLE SHEETS RULES
            # ---------------------------------------------

            if not response_selected:

                resolution = resolve_response_rule(
                    comment_text
                )

                rule = resolution[
                    "rule"
                ]

                source = resolution[
                    "source"
                ]

                private_message = rule[
                    "private_reply"
                ]

                public_message = (
                    rule.get(
                        "public_reply"
                    )
                    or LOCAL_DEFAULT_RULE[
                        "public_reply"
                    ]
                )

                print(
                    "INSTAGRAM RULE MATCH:",
                    {
                        "source": source,
                        "row": rule.get(
                            "row_number"
                        ),
                        "priority": rule.get(
                            "priority"
                        ),
                        "match_type": rule.get(
                            "match_type"
                        ),
                    },
                )

        # -------------------------------------------------
        # PRIVATE REPLY FIRST
        # -------------------------------------------------

        send_private_reply(
            instagram_account_id,
            comment_id,
            private_message,
        )

        # -------------------------------------------------
        # PUBLIC REPLY ONLY AFTER SUCCESSFUL DIRECT
        # -------------------------------------------------

        if INSTAGRAM_PUBLIC_REPLY_ENABLED:

            try:

                send_public_comment_reply(
                    comment_id,
                    public_message,
                )

            except Exception as exc:

                # Direct уже отправлен.
                # Не считаем всю обработку неуспешной.
                print(
                    "INSTAGRAM PUBLIC REPLY "
                    "FAILED:",
                    repr(
                        exc
                    ),
                )

        # -------------------------------------------------
        # DONE
        # -------------------------------------------------

        update_comment_status(
            comment_id,
            "done",
            product_id=
                None,
            detection_source=
                source,
        )

        print(
            "INSTAGRAM COMMENT DONE:",
            comment_id,
        )

    except Exception as exc:

        print(
            "INSTAGRAM COMMENT ERROR:",
            comment_id,
            repr(
                exc
            ),
        )

        update_comment_status(
            comment_id,
            "failed",
            error=str(
                exc
            )[
                :2000
            ],
        )


# =========================================================
# WEBHOOK SIGNATURE
# =========================================================

def verify_meta_signature(
    body: bytes,
    signature_header: str | None,
) -> bool:

    # Пока App Secret не добавлен,
    # не блокируем webhook.
    #
    # Позже обязательно добавим
    # INSTAGRAM_APP_SECRET в Coolify.
    # -------------------------------------------------

    if not INSTAGRAM_APP_SECRET:

        return True

    if not signature_header:

        return False

    prefix = (
        "sha256="
    )

    if not signature_header.startswith(
        prefix
    ):

        return False

    received = signature_header[
        len(
            prefix
        ):
    ]

    expected = hmac.new(
        INSTAGRAM_APP_SECRET.encode(
            "utf-8"
        ),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        received,
    )


# =========================================================
# PRIVACY POLICY
# =========================================================

@router.get(
    "/privacy",
    response_class=HTMLResponse,
)
async def privacy_policy():

    return """
<!DOCTYPE html>
<html lang="ru">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
    Политика конфиденциальности — Texnikach
</title>

<style>

body {
    margin: 0;
    background: #111;
    color: #eee;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;

    line-height: 1.65;
}

.container {
    max-width: 850px;
    margin: 0 auto;

    padding:
        40px
        22px
        70px;
}

h1 {
    font-size: 34px;
    margin-bottom: 10px;
}

h2 {
    margin-top: 34px;
    font-size: 22px;
}

p,
li {
    color: #ccc;
}

a {
    color: #d9b565;
}

.updated {
    color: #888;
    margin-bottom: 35px;
}

</style>

</head>

<body>

<div class="container">

<h1>
    Политика конфиденциальности Texnikach
</h1>

<div class="updated">
    Последнее обновление:
    17 августа 2026 г.
</div>

<p>
    Настоящая Политика конфиденциальности
    описывает, как Texnikach обрабатывает
    данные при использовании
    автоматизированных функций,
    связанных с Instagram.
</p>

<h2>
    1. Какие данные могут обрабатываться
</h2>

<p>
    При взаимодействии пользователя
    с Instagram-аккаунтом Texnikach
    приложение может получать через
    официальные инструменты Meta
    и Instagram API данные,
    необходимые для работы автоматизации.
</p>

<ul>

<li>
    идентификатор пользователя Instagram;
</li>

<li>
    имя пользователя Instagram;
</li>

<li>
    текст комментариев и сообщений;
</li>

<li>
    идентификаторы публикаций,
    Reels и комментариев;
</li>

<li>
    технические данные,
    предоставляемые Instagram API.
</li>

</ul>

<h2>
    2. Для чего используются данные
</h2>

<p>
    Данные используются исключительно
    для работы сервиса Texnikach.
</p>

<ul>

<li>
    обработка запросов клиентов;
</li>

<li>
    ответы на комментарии;
</li>

<li>
    отправка информации о товарах
    в Instagram Direct;
</li>

<li>
    передача запроса живому менеджеру;
</li>

<li>
    обеспечение стабильной
    и безопасной работы сервиса.
</li>

</ul>

<h2>
    3. Данные о товарах
</h2>

<p>
    Информация о ценах, наличии
    и характеристиках товаров
    формируется на основе внутренних
    данных Texnikach.
</p>

<h2>
    4. Передача данных третьим лицам
</h2>

<p>
    Texnikach не продаёт персональные
    данные пользователей.
</p>

<h2>
    5. Хранение данных
</h2>

<p>
    Мы стараемся хранить только данные,
    необходимые для работы сервиса,
    диагностики и обслуживания клиентов.
</p>

<h2>
    6. Безопасность
</h2>

<p>
    Ключи доступа и секретные данные
    приложения не размещаются
    в открытом доступе.
</p>

<h2>
    7. Удаление данных
</h2>

<p>
    Для запроса удаления данных:
</p>

<p>

<a href="mailto:texnikach@gmail.com">
    texnikach@gmail.com
</a>

</p>

<h2>
    8. Контакты
</h2>

<p>
    Texnikach<br>
    Ташкент, Узбекистан
</p>

<p>

Email:

<a href="mailto:texnikach@gmail.com">
    texnikach@gmail.com
</a>

</p>

<p>

Сайт:

<a
    href="https://texnikach.uz"
    target="_blank"
    rel="noopener noreferrer"
>
    texnikach.uz
</a>

</p>

</div>

</body>

</html>
    """


# =========================================================
# STATUS
# =========================================================

@router.get(
    "/instagram/status"
)
async def instagram_status():

    rules_loaded_at = RULES_CACHE[
        "loaded_at"
    ]

    rules_cache_age_seconds = (
        int(
            max(
                0,
                time.monotonic()
                - rules_loaded_at,
            )
        )
        if rules_loaded_at
        else None
    )

    products_loaded_at = PRODUCTS_CACHE[
        "loaded_at"
    ]

    products_cache_age_seconds = (
        int(
            max(
                0,
                time.monotonic()
                - products_loaded_at,
            )
        )
        if products_loaded_at
        else None
    )

    post_models_loaded_at = POST_MODELS_CACHE[
        "loaded_at"
    ]

    post_models_cache_age_seconds = (
        int(
            max(
                0,
                time.monotonic()
                - post_models_loaded_at,
            )
        )
        if post_models_loaded_at
        else None
    )

    direct_loaded_at = DIRECT_CONFIG_CACHE[
        "loaded_at"
    ]

    direct_cache_age_seconds = (
        int(
            max(
                0,
                time.monotonic()
                - direct_loaded_at,
            )
        )
        if direct_loaded_at
        else None
    )

    live_catalog = PRODUCTS_CACHE[
        "catalog"
    ]

    google_json_path = os.getenv(
        "GOOGLE_SA_JSON",
        "Data/sheets-auto-update-484813-fe0f96f83d38.json",
    ).strip()

    google_write_credentials_configured = bool(
        os.getenv(
            "GOOGLE_SA_JSON_CONTENT",
            "",
        ).strip()
        or (
            google_json_path
            and os.path.exists(
                google_json_path
            )
        )
    )

    with connect_instagram_db() as conn:

        products = conn.execute(
            """
            SELECT COUNT(*)
            FROM instagram_products
            """
        ).fetchone()[0]

        aliases = conn.execute(
            """
            SELECT COUNT(*)
            FROM instagram_product_aliases
            """
        ).fetchone()[0]

        mappings = conn.execute(
            """
            SELECT COUNT(*)
            FROM instagram_post_products
            """
        ).fetchone()[0]

        processed = conn.execute(
            """
            SELECT COUNT(*)
            FROM instagram_processed_comments
            """
        ).fetchone()[0]

        processed_direct = conn.execute(
            """
            SELECT COUNT(*)
            FROM instagram_processed_messages
            """
        ).fetchone()[0]

    return {

        "status":
            "ok",

        "instagram_token":
            bool(
                INSTAGRAM_ACCESS_TOKEN
            ),

        "verify_token":
            bool(
                INSTAGRAM_VERIFY_TOKEN
            ),

        "app_secret":
            bool(
                INSTAGRAM_APP_SECRET
            ),

        "products":
            products,

        "aliases":
            aliases,

        "post_mappings":
            mappings,

        "processed_comments":
            processed,

        "processed_direct_messages":
            processed_direct,

        "direct_messages_ready":
            bool(
                INSTAGRAM_ACCESS_TOKEN
                and
                INSTAGRAM_APP_SECRET
                and
                INSTAGRAM_ACCOUNT_ID
                and
                INSTAGRAM_RULES_SHEET_ID
                and
                INSTAGRAM_PRODUCTS_SHEET_ID
            ),

        "rules_sheet_configured":
            bool(
                INSTAGRAM_RULES_SHEET_ID
            ),

        "rules_sheet_name":
            INSTAGRAM_RULES_SHEET_NAME,

        "rules_cache_source":
            RULES_CACHE[
                "source"
            ],

        "rules_cache_count":
            len(
                RULES_CACHE[
                    "rules"
                ]
            ),

        "rules_cache_age_seconds":
            rules_cache_age_seconds,

        "direct_rules_sheet_name":
            INSTAGRAM_DIRECT_RULES_SHEET_NAME,

        "direct_settings_sheet_name":
            INSTAGRAM_DIRECT_SETTINGS_SHEET_NAME,

        "direct_cache_source":
            DIRECT_CONFIG_CACHE[
                "source"
            ],

        "direct_rules_count":
            len(
                DIRECT_CONFIG_CACHE[
                    "rules"
                ]
            ),

        "direct_settings_count":
            len(
                DIRECT_CONFIG_CACHE[
                    "settings"
                ]
            ),

        "direct_cache_age_seconds":
            direct_cache_age_seconds,

        "products_sheet_configured":
            bool(
                INSTAGRAM_PRODUCTS_SHEET_ID
            ),

        "products_sheet_name":
            INSTAGRAM_PRODUCTS_SHEET_NAME,

        "products_cache_source":
            PRODUCTS_CACHE[
                "source"
            ],

        "products_cache_age_seconds":
            products_cache_age_seconds,

        "post_models_sheet_name":
            INSTAGRAM_POST_MODELS_SHEET_NAME,

        "post_models_cache_source":
            POST_MODELS_CACHE[
                "source"
            ],

        "post_models_cache_count":
            len(
                POST_MODELS_CACHE[
                    "mappings"
                ]
            ),

        "post_models_cache_age_seconds":
            post_models_cache_age_seconds,

        "post_sync_enabled":
            INSTAGRAM_POST_SYNC_ENABLED,

        "post_sync_interval_seconds":
            INSTAGRAM_POST_SYNC_INTERVAL,

        "post_sync_limit":
            INSTAGRAM_POST_SYNC_LIMIT,

        "post_sync_write_credentials":
            google_write_credentials_configured,

        "live_price_rows":
            (
                len(
                    live_catalog[
                        "rows"
                    ]
                )
                if live_catalog
                else 0
            ),

        "live_price_models":
            (
                len(
                    live_catalog[
                        "families"
                    ]
                )
                if live_catalog
                else 0
            ),
    }


# =========================================================
# INSTAGRAM WEBHOOK VERIFY
# =========================================================

@router.get(
    "/webhooks/instagram",
    response_class=PlainTextResponse,
)
async def instagram_webhook_verify(
    request: Request,
):

    mode = request.query_params.get(
        "hub.mode"
    )

    verify_token = request.query_params.get(
        "hub.verify_token"
    )

    challenge = request.query_params.get(
        "hub.challenge"
    )

    print(
        "INSTAGRAM WEBHOOK VERIFY:",
        {
            "mode":
                mode,

            "verify_token_received":
                bool(
                    verify_token
                ),

            "challenge_received":
                bool(
                    challenge
                ),
        },
    )

    if not INSTAGRAM_VERIFY_TOKEN:

        raise HTTPException(
            status_code=500,
            detail=(
                "Instagram verify token "
                "is not configured"
            ),
        )

    if (
        mode == "subscribe"
        and
        verify_token == INSTAGRAM_VERIFY_TOKEN
        and
        challenge is not None
    ):

        print(
            "INSTAGRAM WEBHOOK VERIFIED"
        )

        return challenge

    raise HTTPException(
        status_code=403,
        detail="Invalid verify token",
    )


# =========================================================
# INSTAGRAM WEBHOOK EVENTS
# =========================================================

def extract_direct_message_events(
    entry: dict,
) -> list[dict]:

    instagram_account_id = str(
        entry.get(
            "id"
        )
        or ""
    )

    if not instagram_account_id:
        return []

    if (
        INSTAGRAM_ACCOUNT_ID
        and
        instagram_account_id
        != INSTAGRAM_ACCOUNT_ID
    ):

        print(
            "INSTAGRAM DIRECT ACCOUNT IGNORED:",
            instagram_account_id,
        )

        return []

    result = []

    for event in (
        entry.get(
            "messaging"
        )
        or []
    ):

        if not isinstance(
            event,
            dict,
        ):
            continue

        message = event.get(
            "message"
        )

        if not isinstance(
            message,
            dict,
        ):
            continue

        if (
            message.get(
                "is_echo"
            )
            or
            message.get(
                "is_deleted"
            )
            or
            message.get(
                "reaction"
            )
        ):
            continue

        sender_id = str(
            (
                event.get(
                    "sender"
                )
                or {}
            ).get(
                "id"
            )
            or ""
        )

        recipient_id = str(
            (
                event.get(
                    "recipient"
                )
                or {}
            ).get(
                "id"
            )
            or ""
        )

        message_id = str(
            message.get(
                "mid"
            )
            or ""
        )

        message_text = str(
            message.get(
                "text"
            )
            or ""
        )

        attachments = (
            message.get(
                "attachments"
            )
            or []
        )

        if (
            not sender_id
            or
            sender_id
            == instagram_account_id
            or
            not recipient_id
            or
            recipient_id
            != instagram_account_id
            or
            not message_id
            or (
                not message_text.strip()
                and
                not attachments
            )
        ):
            continue

        result.append(
            {
                "instagram_account_id":
                    instagram_account_id,
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message_id": message_id,
                "message_text": message_text,
            }
        )

    return result

@router.post(
    "/webhooks/instagram"
)
async def instagram_webhook_event(
    request: Request,
    background_tasks: BackgroundTasks,
):

    raw_body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256"
    )

    if not verify_meta_signature(
        raw_body,
        signature,
    ):

        print(
            "INSTAGRAM INVALID SIGNATURE"
        )

        raise HTTPException(
            status_code=403,
            detail="Invalid webhook signature",
        )

    try:

        data = await request.json()

    except Exception as exc:

        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        ) from exc

    entries = (
        data.get(
            "entry"
        )
        or []
    )

    print(
        "INSTAGRAM WEBHOOK EVENT:",
        {
            "object": data.get(
                "object"
            ),
            "entries": len(
                entries
            ),
            "changes": sum(
                len(
                    entry.get(
                        "changes"
                    )
                    or []
                )
                for entry in entries
            ),
            "messaging": sum(
                len(
                    entry.get(
                        "messaging"
                    )
                    or []
                )
                for entry in entries
            ),
        },
    )

    if (
        data.get(
            "object"
        )
        != "instagram"
    ):

        return {
            "ok": True,
            "ignored": True,
        }

    for entry in entries:

        instagram_account_id = str(
            entry.get(
                "id"
            )
            or ""
        )

        for change in (
            entry.get(
                "changes"
            )
            or []
        ):

            if (
                change.get(
                    "field"
                )
                != "comments"
            ):

                continue

            value = (
                change.get(
                    "value"
                )
                or {}
            )

            commenter = (
                value.get(
                    "from"
                )
                or {}
            )

            media = (
                value.get(
                    "media"
                )
                or {}
            )

            commenter_id = str(
                commenter.get(
                    "id"
                )
                or ""
            )

            username = str(
                commenter.get(
                    "username"
                )
                or ""
            )

            media_id = str(
                media.get(
                    "id"
                )
                or ""
            )

            comment_id = str(
                value.get(
                    "id"
                )
                or ""
            )

            comment_text = str(
                value.get(
                    "text"
                )
                or ""
            )

            if not comment_id:

                continue

            # -------------------------------------------------
            # CLAIM / DEDUPLICATE
            # -------------------------------------------------

            claimed = claim_comment(

                comment_id=
                    comment_id,

                media_id=
                    media_id,

                instagram_user_id=
                    commenter_id,

                username=
                    username,

                comment_text=
                    comment_text,
            )

            if not claimed:

                print(
                    "INSTAGRAM DUPLICATE COMMENT:",
                    comment_id,
                )

                continue

            # -------------------------------------------------
            # RETURN WEBHOOK QUICKLY.
            # PROCESS IN BACKGROUND.
            # -------------------------------------------------

            background_tasks.add_task(

                process_instagram_comment,

                instagram_account_id=
                    instagram_account_id,

                commenter_id=
                    commenter_id,

                username=
                    username,

                media_id=
                    media_id,

                comment_id=
                    comment_id,

                comment_text=
                    comment_text,
            )

        # -------------------------------------------------
        # INCOMING DIRECT MESSAGES
        # -------------------------------------------------

        if (
            entry.get(
                "messaging"
            )
            and
            not INSTAGRAM_APP_SECRET
        ):

            print(
                "INSTAGRAM DIRECT DISABLED:",
                "instagram_app_secret_missing",
            )

            continue

        for direct_event in extract_direct_message_events(
            entry
        ):

            claimed = claim_direct_message(
                message_id=direct_event[
                    "message_id"
                ],
                sender_id=direct_event[
                    "sender_id"
                ],
                recipient_id=direct_event[
                    "recipient_id"
                ],
            )

            if not claimed:

                print(
                    "INSTAGRAM DIRECT DUPLICATE:",
                    direct_event[
                        "message_id"
                    ],
                )

                continue

            background_tasks.add_task(
                process_instagram_direct_message,
                instagram_account_id=direct_event[
                    "instagram_account_id"
                ],
                sender_id=direct_event[
                    "sender_id"
                ],
                message_id=direct_event[
                    "message_id"
                ],
                message_text=direct_event[
                    "message_text"
                ],
            )

    return {
        "ok": True
    }
