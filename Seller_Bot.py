import os
import re
import json
import time
import html
import asyncio

import requests
import pandas as pd
import gspread

from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# TEXNIKACH — SELLER BOT
# ============================================================


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "SELLER_BOT_TOKEN"
)


# ============================================================
# GOOGLE SERVICE ACCOUNT
# ============================================================

GOOGLE_SA_JSON_PATH = os.getenv(
    "GOOGLE_SA_JSON",
    "Data/sheets-auto-update-484813-fe0f96f83d38.json",
)


# ============================================================
# ОСНОВНАЯ GOOGLE-ТАБЛИЦА
# ============================================================

SPREADSHEET_ID = (
    "1TrS6C4oHe6nzQTPTa_4se_upXBFF6rmbfnE7RqznR8U"
)

PRICES_SHEET_NAME = "bot_prices"
SETTINGS_SHEET_NAME = "bot_settings"


# ============================================================
# ТАБЛИЦА СОРТИРОВКИ
# ============================================================

SORT_SPREADSHEET_ID = (
    "1Hiq-ccGGo3skcp0Imyw0Jum4OPilEs8lBJGApK6vlrs"
)


# ============================================================
# BOT_URLS
# ============================================================

BOT_URLS_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSMu58tsoX9-LunrEtYYZLLw2C-z2N8_BMfA_Hk78UaFxgMR345UMaJnzY2ljQsO-jYI5w99U0Gd5IG/"
    "pub?output=xlsx"
)

BOT_URLS_PATH = (
    "Data/Bot_URLS.xlsx"
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

MAX_SEARCH_RESULTS = 20

# Данные Google будут перечитываться максимум
# один раз в 60 секунд
CACHE_LIFETIME = 60


# ============================================================
# ГЛОБАЛЬНЫЕ ДАННЫЕ
# ============================================================

google_client = None
main_spreadsheet = None

prices_df = pd.DataFrame()

model_order = {}
memory_order = {}
color_order = {}

# product_id -> Telegram URL
product_urls = {}

kurs = None

last_update_time = 0


# ============================================================
# CALLBACK ДАННЫЕ
# ============================================================

# callback_id -> {
#     "model_name": ...,
#     "search_id": ...
# }

model_callback_map = {}

# search_id -> список найденных моделей

search_results_map = {}


# ============================================================
# GOOGLE — АВТОРИЗАЦИЯ
# ============================================================

def get_google_client():

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    google_json = os.getenv(
        "GOOGLE_SA_JSON_CONTENT"
    )

    # ========================================================
    # SERVER / COOLIFY
    # ========================================================

    if google_json:

        service_account_info = json.loads(
            google_json
        )

        creds = (
            Credentials.from_service_account_info(
                service_account_info,
                scopes=scopes,
            )
        )

    # ========================================================
    # MAC
    # ========================================================

    else:

        if not os.path.exists(
            GOOGLE_SA_JSON_PATH
        ):

            raise FileNotFoundError(
                "\nНе найден Google Service Account.\n\n"
                "Файл должен находиться здесь:\n"
                f"{GOOGLE_SA_JSON_PATH}\n"
            )

        creds = (
            Credentials.from_service_account_file(
                GOOGLE_SA_JSON_PATH,
                scopes=scopes,
            )
        )

    return gspread.authorize(
        creds
    )


# ============================================================
# ЗАГРУЗКА КУРСА
# ============================================================

def load_kurs_from_google(
    spreadsheet,
):

    worksheet = spreadsheet.worksheet(
        SETTINGS_SHEET_NAME
    )

    data = worksheet.get_all_records()

    settings_df = pd.DataFrame(
        data
    )

    if settings_df.empty:

        raise ValueError(
            "bot_settings пустой"
        )

    required_columns = [
        "setting",
        "value",
    ]

    for column in required_columns:

        if column not in settings_df.columns:

            raise ValueError(
                f"Нет колонки {column} "
                f"в {SETTINGS_SHEET_NAME}"
            )

    kurs_rows = settings_df[
        settings_df["setting"]
        .astype(str)
        .str.strip()
        .str.lower()
        == "kurs"
    ]

    if kurs_rows.empty:

        raise ValueError(
            "Не найден setting = kurs"
        )

    kurs_value = pd.to_numeric(
        kurs_rows.iloc[0]["value"],
        errors="coerce",
    )

    if pd.isna(
        kurs_value
    ):

        raise ValueError(
            "Курс не является числом"
        )

    kurs_value = float(
        kurs_value
    )

    if kurs_value <= 0:

        raise ValueError(
            f"Некорректный курс: {kurs_value}"
        )

    return kurs_value


# ============================================================
# ЗАГРУЗКА ЦЕН
# ============================================================

def load_prices_from_google(
    spreadsheet,
):

    worksheet = spreadsheet.worksheet(
        PRICES_SHEET_NAME
    )

    data = worksheet.get_all_records()

    df = pd.DataFrame(
        data
    )

    required_columns = [
        "product_id",
        "model_name",
        "memory",
        "color",
        "price",
        "warranty_period",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:

        raise ValueError(
            "В bot_prices отсутствуют колонки: "
            + ", ".join(
                missing_columns
            )
        )

    # ========================================================
    # ЧИСЛОВЫЕ ПОЛЯ
    # ========================================================

    df["product_id"] = pd.to_numeric(
        df["product_id"],
        errors="coerce",
    )

    df["price"] = pd.to_numeric(
        df["price"],
        errors="coerce",
    )

    df["warranty_period"] = pd.to_numeric(
        df["warranty_period"],
        errors="coerce",
    )

    # ========================================================
    # УДАЛЯЕМ НЕВАЛИДНЫЕ
    # ========================================================

    df = df.dropna(
        subset=[
            "product_id",
            "price",
        ]
    )

    df = df[
        df["price"] > 0
    ].copy()

    df["product_id"] = (
        df["product_id"]
        .astype(int)
    )

    # ========================================================
    # ТЕКСТОВЫЕ ПОЛЯ
    # ========================================================

    for column in [
        "model_name",
        "memory",
        "color",
    ]:

        df[column] = (
            df[column]
            .fillna("")
            .astype(str)
            .str.strip()
        )

    # Удаляем товары без названия

    df = df[
        df["model_name"] != ""
    ].copy()

    return df


# ============================================================
# ЗАГРУЗКА BOT_URLS
# ============================================================

def load_product_urls():

    print(
        "🔗 Загружаю ссылки Bot_URLS..."
    )

    response = requests.get(
        BOT_URLS_URL,
        timeout=60,
    )

    response.raise_for_status()

    os.makedirs(
        os.path.dirname(
            BOT_URLS_PATH
        ),
        exist_ok=True,
    )

    with open(
        BOT_URLS_PATH,
        "wb",
    ) as file:

        file.write(
            response.content
        )

    urls_df = pd.read_excel(
        BOT_URLS_PATH
    )

    required_columns = [
        "product_id",
        "post_id",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in urls_df.columns
    ]

    if missing_columns:

        raise ValueError(
            "В Bot_URLS отсутствуют колонки: "
            + ", ".join(
                missing_columns
            )
        )

    result = {}

    # ========================================================
    # РАЗБИРАЕМ product_id
    #
    # Например:
    # 100, 101, 102
    #
    # Все получат одну ссылку post_id
    # ========================================================

    for _, row in urls_df.iterrows():

        raw_product_ids = row.get(
            "product_id"
        )

        raw_url = row.get(
            "post_id"
        )

        if pd.isna(
            raw_product_ids
        ):

            continue

        if pd.isna(
            raw_url
        ):

            continue

        url = str(
            raw_url
        ).strip()

        if not url:

            continue

        product_ids = str(
            raw_product_ids
        ).split(",")

        for product_id in product_ids:

            product_id = (
                product_id
                .strip()
            )

            # Excel иногда превращает:
            #
            # 123
            #
            # в
            #
            # 123.0

            if product_id.endswith(
                ".0"
            ):

                product_id = (
                    product_id[:-2]
                )

            if not product_id.isdigit():

                continue

            product_id = int(
                product_id
            )

            # Оставляем ПЕРВУЮ найденную ссылку
            # для этого product_id

            if product_id not in result:

                result[
                    product_id
                ] = url

    print(
        f"✅ Ссылок загружено: "
        f"{len(result)}"
    )

    return result


# ============================================================
# ЗАГРУЗКА СОРТИРОВКИ
# ============================================================

def load_sorting_from_google(
    client,
):

    sort_spreadsheet = client.open_by_key(
        SORT_SPREADSHEET_ID
    )

    worksheet = sort_spreadsheet.sheet1

    values = worksheet.get_all_values()

    if not values:

        raise ValueError(
            "product_sort пустой"
        )

    headers = [
        str(value).strip()
        for value in values[0]
    ]

    required_columns = [
        "model_name",
        "memory_sort",
        "color_sort",
    ]

    column_indexes = {}

    for column_name in required_columns:

        if column_name not in headers:

            raise ValueError(
                "В product_sort нет колонки: "
                f"{column_name}"
            )

        column_indexes[
            column_name
        ] = headers.index(
            column_name
        )

    rows = []

    for row in values[1:]:

        def get_value(
            column_name,
        ):

            index = column_indexes[
                column_name
            ]

            if index < len(
                row
            ):

                return str(
                    row[index]
                ).strip()

            return ""

        rows.append(
            {
                "model_name": get_value(
                    "model_name"
                ),

                "memory_sort": get_value(
                    "memory_sort"
                ),

                "color_sort": get_value(
                    "color_sort"
                ),
            }
        )

    sort_df = pd.DataFrame(
        rows
    )

    # ========================================================
    # ПОРЯДОК МОДЕЛЕЙ
    # ========================================================

    models = [
        value
        for value
        in sort_df[
            "model_name"
        ].tolist()
        if value
    ]

    models = list(
        dict.fromkeys(
            models
        )
    )

    model_order_result = {
        value: index
        for index, value
        in enumerate(
            models
        )
    }

    # ========================================================
    # ПОРЯДОК ПАМЯТИ
    # ========================================================

    memories = [
        value
        for value
        in sort_df[
            "memory_sort"
        ].tolist()
        if value
    ]

    memories = list(
        dict.fromkeys(
            memories
        )
    )

    memory_order_result = {
        value: index
        for index, value
        in enumerate(
            memories
        )
    }

    # ========================================================
    # ПОРЯДОК ЦВЕТОВ
    # ========================================================

    colors = [
        value
        for value
        in sort_df[
            "color_sort"
        ].tolist()
        if value
    ]

    colors = list(
        dict.fromkeys(
            colors
        )
    )

    color_order_result = {
        value: index
        for index, value
        in enumerate(
            colors
        )
    }

    return (
        model_order_result,
        memory_order_result,
        color_order_result,
    )


# ============================================================
# ОБНОВЛЕНИЕ БАЗЫ
# ============================================================

def refresh_database(
    force=False,
):

    global google_client
    global main_spreadsheet

    global prices_df

    global model_order
    global memory_order
    global color_order

    global product_urls

    global kurs
    global last_update_time

    current_time = time.time()

    # ========================================================
    # КЭШ ЕЩЁ АКТУАЛЕН
    # ========================================================

    if (
        not force
        and not prices_df.empty
        and current_time - last_update_time
        < CACHE_LIFETIME
    ):

        return

    print()
    print(
        "🔄 Обновляю данные..."
    )

    # ========================================================
    # GOOGLE CLIENT
    # ========================================================

    if google_client is None:

        google_client = (
            get_google_client()
        )

        print(
            "✅ Google авторизация успешна"
        )

    # ========================================================
    # ОСНОВНАЯ GOOGLE-ТАБЛИЦА
    # ========================================================

    if main_spreadsheet is None:

        main_spreadsheet = (
            google_client.open_by_key(
                SPREADSHEET_ID
            )
        )

    # ========================================================
    # КУРС
    # ========================================================

    kurs = load_kurs_from_google(
        main_spreadsheet
    )

    # ========================================================
    # ЦЕНЫ
    # ========================================================

    prices_df = (
        load_prices_from_google(
            main_spreadsheet
        )
    )

    # ========================================================
    # ССЫЛКИ
    # ========================================================

    product_urls = (
        load_product_urls()
    )

    # ========================================================
    # СОРТИРОВКА
    # ========================================================

    (
        model_order,
        memory_order,
        color_order,
    ) = load_sorting_from_google(
        google_client
    )

    last_update_time = current_time

    print(
        f"✅ Цен: "
        f"{len(prices_df)}"
    )

    print(
        f"✅ Ссылок: "
        f"{len(product_urls)}"
    )

    print(
        f"✅ Курс: "
        f"{kurs:,.0f}".replace(
            ",",
            " ",
        )
    )

    print(
        f"✅ Моделей сортировки: "
        f"{len(model_order)}"
    )

    print(
        "✅ Данные обновлены"
    )


# ============================================================
# НОРМАЛИЗАЦИЯ ТЕКСТА
# ============================================================

def normalize_text(
    value,
):

    value = str(
        value
    ).casefold()

    value = value.replace(
        "ё",
        "е",
    )

    value = re.sub(
        r"[^a-zа-я0-9]+",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ============================================================
# ПОИСК МОДЕЛЕЙ
# ============================================================

def search_models(
    search_text,
):

    if prices_df.empty:

        return []

    search_text_normalized = (
        normalize_text(
            search_text
        )
    )

    if not search_text_normalized:

        return []

    search_words = (
        search_text_normalized
        .split()
    )

    models = (
        prices_df[
            "model_name"
        ]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
        .tolist()
    )

    found = []

    for model_name in models:

        normalized_model = (
            normalize_text(
                model_name
            )
        )

        # ====================================================
        # ВСЕ СЛОВА ДОЛЖНЫ БЫТЬ В НАЗВАНИИ
        # ====================================================

        if not all(
            word in normalized_model
            for word in search_words
        ):

            continue

        # ====================================================
        # РАНЖИРОВАНИЕ
        # ====================================================

        if (
            normalized_model
            == search_text_normalized
        ):

            search_score = 0

        elif normalized_model.startswith(
            search_text_normalized
        ):

            search_score = 1

        elif (
            search_text_normalized
            in normalized_model
        ):

            search_score = 2

        else:

            search_score = 3

        sort_position = (
            model_order.get(
                model_name,
                999999,
            )
        )

        found.append(
            (
                search_score,
                sort_position,
                model_name,
            )
        )

    found.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2].casefold(),
        )
    )

    return [
        item[2]
        for item
        in found[
            :MAX_SEARCH_RESULTS
        ]
    ]


# ============================================================
# ФОРМАТ ЦЕНЫ
# ============================================================

def format_price(
    price,
):

    if kurs is None:

        raise RuntimeError(
            "Курс ещё не загружен"
        )

    price = float(
        price
    )

    if price.is_integer():

        usd = int(
            price
        )

    else:

        usd = price

    uzs = (
        price
        * float(
            kurs
        )
    )

    # ========================================================
    # ОКРУГЛЕНИЕ ДО 1000 СУМ
    # ========================================================

    if uzs > 10000:

        uzs = int(
            round(
                uzs / 1000
            )
            * 1000
        )

    else:

        uzs = int(
            round(
                uzs
            )
        )

    uzs_text = (
        f"{uzs:,}"
        .replace(
            ",",
            " ",
        )
    )

    return (
        f"{uzs_text} So'm (${usd})"
    )


# ============================================================
# ДАННЫЕ ОДНОЙ МОДЕЛИ
# ============================================================

def get_model_dataframe(
    model_name,
):

    df = prices_df[
        prices_df[
            "model_name"
        ] == model_name
    ].copy()

    if df.empty:

        return df

    # ========================================================
    # СОРТИРОВКА
    # ========================================================

    df["_memory_sort"] = (
        df[
            "memory"
        ]
        .map(
            memory_order
        )
        .fillna(
            999999
        )
    )

    df["_color_sort"] = (
        df[
            "color"
        ]
        .map(
            color_order
        )
        .fillna(
            999999
        )
    )

    df = df.sort_values(
        by=[
            "_memory_sort",
            "price",
            "_color_sort",
        ]
    )

    df = df.drop(
        columns=[
            "_memory_sort",
            "_color_sort",
        ]
    )

    return df


# ============================================================
# ПОЛУЧЕНИЕ ССЫЛКИ МОДЕЛИ
# ============================================================

def get_model_url(
    df,
):

    if df.empty:

        return None

    # ========================================================
    # У ОДНОЙ МОДЕЛИ МОЖЕТ БЫТЬ НЕСКОЛЬКО PRODUCT_ID
    #
    # ИЩЕМ ПЕРВЫЙ PRODUCT_ID,
    # ДЛЯ КОТОРОГО ЕСТЬ TELEGRAM URL
    # ========================================================

    for product_id in df[
        "product_id"
    ].tolist():

        try:

            product_id = int(
                product_id
            )

        except Exception:

            continue

        url = product_urls.get(
            product_id
        )

        if url:

            return url

    return None


# ============================================================
# ПОСТРОЕНИЕ ТЕКСТА МОДЕЛИ
# ============================================================

def build_model_text(
    model_name,
):

    df = get_model_dataframe(
        model_name
    )

    if df.empty:

        return (
            "❌ Товар не найден "
            "или нет в наличии."
        )

    lines = []

    # ========================================================
    # НАЗВАНИЕ + TELEGRAM ССЫЛКА
    # ========================================================

    model_url = (
        get_model_url(
            df
        )
    )

    if model_url:

        safe_url = html.escape(
            model_url,
            quote=True,
        )

        safe_model_name = html.escape(
            model_name
        )

        lines.append(
            f'<b><a href="{safe_url}">'
            f'{safe_model_name}'
            f'</a></b>'
        )

    else:

        lines.append(
            f"<b>"
            f"{html.escape(model_name)}"
            f"</b>"
        )

    lines.append("")

    # ========================================================
    # ПРОВЕРЯЕМ ПАМЯТЬ
    # ========================================================

    has_memory = (
        df[
            "memory"
        ]
        .astype(str)
        .str.strip()
        .ne("")
        .any()
    )

    # ========================================================
    # ПРОВЕРЯЕМ ЦВЕТ
    # ========================================================

    has_color = (
        df[
            "color"
        ]
        .astype(str)
        .str.strip()
        .ne("")
        .any()
    )

    # ========================================================
    # ПАМЯТЬ + ЦВЕТ
    # ========================================================

    if has_memory and has_color:

        grouped = df.groupby(
            [
                "memory",
                "price",
                "warranty_period",
            ],
            sort=False,
            dropna=False,
        )

        first_group = True

        for (
            group_keys,
            group,
        ) in grouped:

            memory, price, warranty = (
                group_keys
            )

            if not first_group:

                lines.append("")

            first_group = False

            # =================================================
            # ПАМЯТЬ
            # =================================================

            memory_text = str(
                memory
            ).strip()

            if memory_text:

                lines.append(
                    f"<b>"
                    f"{html.escape(memory_text)}"
                    f"</b>"
                )

            # =================================================
            # ЦВЕТА
            # =================================================

            seen_colors = set()

            for color in group[
                "color"
            ].astype(str):

                color = (
                    color.strip()
                )

                if (
                    color
                    and color
                    not in seen_colors
                ):

                    seen_colors.add(
                        color
                    )

                    lines.append(
                        "• "
                        + html.escape(
                            color
                        )
                    )

            # =================================================
            # ЦЕНА
            # =================================================

            price_text = (
                format_price(
                    price
                )
            )

            if (
                pd.notna(
                    warranty
                )
                and int(
                    warranty
                ) == 12
            ):

                price_text += (
                    " ✔️"
                )

            lines.append(
                f"<b>"
                f"{html.escape(price_text)}"
                f"</b>"
            )

    # ========================================================
    # ТОЛЬКО ПАМЯТЬ
    # ========================================================

    elif has_memory:

        grouped = df.groupby(
            [
                "memory",
                "price",
                "warranty_period",
            ],
            sort=False,
            dropna=False,
        )

        first_group = True

        for (
            group_keys,
            _
        ) in grouped:

            memory, price, warranty = (
                group_keys
            )

            if not first_group:

                lines.append("")

            first_group = False

            memory_text = str(
                memory
            ).strip()

            if memory_text:

                lines.append(
                    f"<b>"
                    f"{html.escape(memory_text)}"
                    f"</b>"
                )

            price_text = (
                format_price(
                    price
                )
            )

            if (
                pd.notna(
                    warranty
                )
                and int(
                    warranty
                ) == 12
            ):

                price_text += (
                    " ✔️"
                )

            lines.append(
                f"<b>"
                f"{html.escape(price_text)}"
                f"</b>"
            )

    # ========================================================
    # ТОЛЬКО ЦВЕТ
    # ========================================================

    elif has_color:

        grouped = df.groupby(
            [
                "price",
                "warranty_period",
            ],
            sort=False,
            dropna=False,
        )

        first_group = True

        for (
            group_keys,
            group,
        ) in grouped:

            price, warranty = (
                group_keys
            )

            if not first_group:

                lines.append("")

            first_group = False

            seen_colors = set()

            for color in group[
                "color"
            ].astype(str):

                color = (
                    color.strip()
                )

                if (
                    color
                    and color
                    not in seen_colors
                ):

                    seen_colors.add(
                        color
                    )

                    lines.append(
                        "• "
                        + html.escape(
                            color
                        )
                    )

            price_text = (
                format_price(
                    price
                )
            )

            if (
                pd.notna(
                    warranty
                )
                and int(
                    warranty
                ) == 12
            ):

                price_text += (
                    " ✔️"
                )

            lines.append(
                f"<b>"
                f"{html.escape(price_text)}"
                f"</b>"
            )

    # ========================================================
    # БЕЗ ПАМЯТИ И ЦВЕТА
    # ========================================================

    else:

        grouped = df.groupby(
            [
                "price",
                "warranty_period",
            ],
            sort=False,
            dropna=False,
        )

        first_group = True

        for (
            group_keys,
            _
        ) in grouped:

            price, warranty = (
                group_keys
            )

            if not first_group:

                lines.append("")

            first_group = False

            price_text = (
                format_price(
                    price
                )
            )

            if (
                pd.notna(
                    warranty
                )
                and int(
                    warranty
                ) == 12
            ):

                price_text += (
                    " ✔️"
                )

            lines.append(
                f"<b>"
                f"{html.escape(price_text)}"
                f"</b>"
            )

    # ========================================================
    # КУРС
    # ========================================================

    lines.append("")

    kurs_text = (
        f"{kurs:,.0f}"
        .replace(
            ",",
            " ",
        )
    )

    lines.append(
        f"💱 Курс: "
        f"<b>{kurs_text}</b>"
    )

    return "\n".join(
        lines
    )


# ============================================================
# СОЗДАНИЕ КНОПОК МОДЕЛЕЙ
# ============================================================

def create_model_keyboard(
    models,
    search_id,
):

    global model_callback_map
    global search_results_map

    search_results_map[
        search_id
    ] = models

    buttons = []

    for model_name in models:

        # ====================================================
        # CALLBACK ID
        # ====================================================

        callback_id = str(
            abs(
                hash(
                    model_name
                )
            )
        )

        model_callback_map[
            callback_id
        ] = {
            "model_name": model_name,
            "search_id": search_id,
        }

        buttons.append(
            [
                InlineKeyboardButton(
                    text=model_name,
                    callback_data=(
                        f"model:{callback_id}"
                    ),
                )
            ]
        )

    return InlineKeyboardMarkup(
        buttons
    )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "<b>🔎 Texnikach — поиск товара</b>\n\n"
        "Напишите название товара "
        "или часть названия.\n\n"

        "Например:\n"

        "<code>S25 Ultra</code>\n"
        "<code>iPhone 16</code>\n"
        "<code>Redmi Note 14</code>\n\n"

        "Я покажу актуальные варианты, "
        "цвета и цены."
    )

    await update.message.reply_text(
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
# /REFRESH
# ============================================================

async def refresh_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = (
        await update.message.reply_text(
            "🔄 Обновляю базу..."
        )
    )

    try:

        await asyncio.to_thread(
            refresh_database,
            True,
        )

        kurs_text = (
            f"{kurs:,.0f}"
            .replace(
                ",",
                " ",
            )
        )

        await message.edit_text(
            "✅ База обновлена.\n\n"
            f"📦 Цен: {len(prices_df)}\n"
            f"🔗 Ссылок: {len(product_urls)}\n"
            f"💱 Курс: {kurs_text}"
        )

    except Exception as error:

        print(
            f"❌ Ошибка обновления: "
            f"{error}"
        )

        await message.edit_text(
            "❌ Ошибка обновления базы:\n\n"
            f"{error}"
        )


# ============================================================
# ПОИСК ПО СООБЩЕНИЮ
# ============================================================

async def search_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    search_text = (
        update.message.text
        or ""
    ).strip()

    if len(
        search_text
    ) < 2:

        await update.message.reply_text(
            "Введите хотя бы 2 символа."
        )

        return

    try:

        # ====================================================
        # ОБНОВЛЯЕМ БАЗУ ПРИ НЕОБХОДИМОСТИ
        # ====================================================

        await asyncio.to_thread(
            refresh_database,
            False,
        )

        models = search_models(
            search_text
        )

        # ====================================================
        # НИЧЕГО НЕ НАЙДЕНО
        # ====================================================

        if not models:

            await update.message.reply_text(
                "❌ Ничего не найдено.\n\n"
                "Попробуйте написать "
                "название немного короче."
            )

            return

        # ====================================================
        # НАЙДЕНА ОДНА МОДЕЛЬ
        # ====================================================

        if len(
            models
        ) == 1:

            model_name = (
                models[0]
            )

            text = (
                build_model_text(
                    model_name
                )
            )

            await update.message.reply_text(
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

            return

        # ====================================================
        # НАЙДЕНО НЕСКОЛЬКО МОДЕЛЕЙ
        # ====================================================

        search_id = (
            f"{update.effective_chat.id}_"
            f"{update.message.message_id}"
        )

        keyboard = (
            create_model_keyboard(
                models=models,
                search_id=search_id,
            )
        )

        await update.message.reply_text(
            text=(
                f"🔎 Найдено моделей: "
                f"<b>{len(models)}</b>\n\n"
                "Выберите нужную:"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    except Exception as error:

        print(
            f"❌ Ошибка поиска: "
            f"{error}"
        )

        await update.message.reply_text(
            "❌ Произошла ошибка.\n\n"
            f"{error}"
        )


# ============================================================
# CALLBACK
# ============================================================

async def model_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = (
        update.callback_query
    )

    await query.answer()

    try:

        callback_data = (
            query.data
            or ""
        )

        # ====================================================
        # НАЗАД К СПИСКУ
        # ====================================================

        if callback_data.startswith(
            "back:"
        ):

            search_id = (
                callback_data.split(
                    ":",
                    1,
                )[1]
            )

            models = (
                search_results_map.get(
                    search_id
                )
            )

            if not models:

                await query.edit_message_text(
                    "⚠️ Список устарел.\n\n"
                    "Напишите название товара ещё раз."
                )

                return

            keyboard = (
                create_model_keyboard(
                    models=models,
                    search_id=search_id,
                )
            )

            # =================================================
            # ПОЛНОСТЬЮ УБИРАЕМ ДАННЫЕ СТАРОЙ МОДЕЛИ
            # =================================================

            await query.edit_message_text(
                text=(
                    f"🔎 Найдено моделей: "
                    f"<b>{len(models)}</b>\n\n"
                    "Выберите нужную:"
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            return

        # ====================================================
        # ВЫБОР МОДЕЛИ
        # ====================================================

        if not callback_data.startswith(
            "model:"
        ):

            return

        callback_id = (
            callback_data.split(
                ":",
                1,
            )[1]
        )

        callback_info = (
            model_callback_map.get(
                callback_id
            )
        )

        if not callback_info:

            await query.edit_message_text(
                "⚠️ Кнопка устарела.\n\n"
                "Напишите название товара ещё раз."
            )

            return

        model_name = (
            callback_info[
                "model_name"
            ]
        )

        search_id = (
            callback_info[
                "search_id"
            ]
        )

        # ====================================================
        # ОБНОВЛЯЕМ БАЗУ
        # ====================================================

        await asyncio.to_thread(
            refresh_database,
            False,
        )

        # ====================================================
        # СОЗДАЁМ КАРТОЧКУ
        # ====================================================

        text = (
            build_model_text(
                model_name
            )
        )

        # ====================================================
        # КНОПКА НАЗАД
        # ====================================================

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=(
                            f"back:{search_id}"
                        ),
                    )
                ]
            ]
        )

        # ====================================================
        # ЗАМЕНЯЕМ СПИСОК МОДЕЛЕЙ КАРТОЧКОЙ
        # ====================================================

        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    except Exception as error:

        print(
            f"❌ Callback ошибка: "
            f"{error}"
        )

        try:

            await query.edit_message_text(
                "❌ Ошибка при получении товара."
            )

        except Exception:

            pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "❌ Telegram ошибка:",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 60
    )

    print(
        "🚀 TEXNIKACH SELLER BOT"
    )

    print(
        "=" * 60
    )

    # ========================================================
    # ПРОВЕРКА TOKEN
    # ========================================================

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "\nНе задан TELEGRAM_BOT_TOKEN.\n\n"
            "Добавьте его в PyCharm:\n"
            "Run → Edit Configurations → "
            "Environment variables\n"
        )

    # ========================================================
    # ПЕРВАЯ ЗАГРУЗКА
    # ========================================================

    refresh_database(
        force=True
    )

    # ========================================================
    # TELEGRAM APPLICATION
    # ========================================================

    application = (
        Application
        .builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    # ========================================================
    # /START
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    # ========================================================
    # /REFRESH
    # ========================================================

    application.add_handler(
        CommandHandler(
            "refresh",
            refresh_command,
        )
    )

    # ========================================================
    # CALLBACK
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            model_callback,
            pattern=r"^(model|back):",
        )
    )

    # ========================================================
    # ПОИСК
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            search_message,
        )
    )

    # ========================================================
    # ERROR HANDLER
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    print()
    print(
        "✅ База загружена"
    )

    print(
        "🤖 Seller Bot запущен"
    )

    print(
        "🔎 Можно искать товар"
    )

    print()

    # ========================================================
    # POLLING
    # ========================================================

    application.run_polling(
        allowed_updates=(
            Update.ALL_TYPES
        )
    )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    main()