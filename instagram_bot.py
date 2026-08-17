import hashlib
import hmac
import os
import re
import sqlite3

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

INSTAGRAM_DB_PATH.parent.mkdir(
    parents=True,
    exist_ok=True,
)

HTTP = requests.Session()


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

PRICE_TRIGGERS = {
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
    # WORDS
    # -------------------------------------------------

    words = set(
        normalized.split()
    )

    if (
        words
        &
        PRICE_TRIGGERS
    ):

        return True

    return False


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
        # LANGUAGE
        # -------------------------------------------------

        language = detect_language(
            comment_text
        )

        # -------------------------------------------------
        # PRODUCT
        # -------------------------------------------------

        resolution = resolve_product(
            comment_text,
            media_id,
        )

        action = (
            resolution[
                "action"
            ]
        )

        source = (
            resolution[
                "source"
            ]
        )

        product = (
            resolution[
                "product"
            ]
        )

        print(
            "INSTAGRAM RESOLUTION:",
            {
                "action":
                    action,

                "source":
                    source,

                "product_id":
                    (
                        product[
                            "id"
                        ]
                        if product
                        else None
                    ),

                "product_name":
                    (
                        product[
                            "name"
                        ]
                        if product
                        else None
                    ),
            },
        )

        # -------------------------------------------------
        # IGNORE NORMAL COMMENTS
        # -------------------------------------------------

        if action == "ignore":

            update_comment_status(
                comment_id,
                "ignored",
                detection_source=
                    source,
            )

            return

        # -------------------------------------------------
        # BUILD PRIVATE MESSAGE
        # -------------------------------------------------

        if (
            action == "product"
            and
            product
        ):

            private_message = (
                build_product_message(
                    product,
                    language,
                )
            )

            product_id = (
                product[
                    "id"
                ]
            )

        else:

            private_message = (
                build_fallback_message(
                    language
                )
            )

            product_id = None

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
                    build_public_reply(
                        language
                    ),
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
                product_id,
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

        "products":
            products,

        "aliases":
            aliases,

        "post_mappings":
            mappings,

        "processed_comments":
            processed,
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

    print(
        "INSTAGRAM WEBHOOK EVENT:",
        data,
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

    for entry in (
        data.get(
            "entry"
        )
        or []
    ):

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

    return {
        "ok": True
    }