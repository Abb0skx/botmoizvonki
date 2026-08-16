import os
import re
import time
import asyncio
import datetime
import html
import json

import requests
import pandas as pd
import gspread

from google.oauth2.service_account import Credentials
from telegram import Bot


# ============================================================
# НАСТРОЙКИ
# ============================================================

# Задержка перед запуском
START_DELAY_SECONDS = 0

# Курс будет загружен из Google Sheets
kurs = None


# ============================================================
# GOOGLE SHEETS — BOT_URLS
# ============================================================

BOT_URLS_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSMu58tsoX9-LunrEtYYZLLw2C-z2N8_BMfA_Hk78UaFxgMR345UMaJnzY2ljQsO-jYI5w99U0Gd5IG/"
    "pub?output=xlsx"
)

BOT_URLS_PATH = "Data/Bot_URLS.xlsx"


# ============================================================
# GOOGLE SHEETS — ЦЕНЫ И НАСТРОЙКИ
# ============================================================

GOOGLE_SA_JSON_PATH = os.getenv(
    "GOOGLE_SA_JSON",
    "Data/sheets-auto-update-484813-fe0f96f83d38.json",
)

# Основная таблица:
# bot_prices
# bot_settings
SPREADSHEET_ID = "1TrS6C4oHe6nzQTPTa_4se_upXBFF6rmbfnE7RqznR8U"

PRICES_SHEET_NAME = "bot_prices"
SETTINGS_SHEET_NAME = "bot_settings"


# ============================================================
# GOOGLE SHEETS — СОРТИРОВКА
# ============================================================

# Новая онлайн-таблица product_sort
SORT_SPREADSHEET_ID = "1Hiq-ccGGo3skcp0Imyw0Jum4OPilEs8lBJGApK6vlrs"


# ============================================================
# TELEGRAM TOKEN
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# ============================================================
# КАНАЛЫ, КОТОРЫЕ НУЖНО ОБНОВЛЯТЬ
# ============================================================

bardak = 1

phone = 1
planshet = 1
notebook = 1

watch = bardak
music = bardak
home = bardak
accsessories = bardak

texnikach = 1
dop = 1


# ============================================================
# ДАТА ОБНОВЛЕНИЯ
# ============================================================

def get_formatted_date():
    current_datetime = datetime.datetime.now()

    formatted_date_string = current_datetime.strftime(
        "%d.%m.%Y (%H:%M)"
    )

    return (
        f'<p>📆 <a href="https://t.me/Texnikach">Обновлено:</a> '
        f'{html.escape(formatted_date_string)}</p>'
    )


# ============================================================
# GOOGLE SHEETS — АВТОРИЗАЦИЯ
# ============================================================

def get_google_client():

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    google_json = os.getenv("GOOGLE_SA_JSON_CONTENT")

    # На сервере — JSON из Environment Variable
    if google_json:
        service_account_info = json.loads(google_json)

        creds = Credentials.from_service_account_info(
            service_account_info,
            scopes=scopes,
        )

    # На Mac — старый JSON-файл
    else:
        if not os.path.exists(GOOGLE_SA_JSON_PATH):
            raise FileNotFoundError(
                "Не найден Google Service Account.\n"
                "Нет GOOGLE_SA_JSON_CONTENT и нет файла:\n"
                f"{GOOGLE_SA_JSON_PATH}"
            )

        creds = Credentials.from_service_account_file(
            GOOGLE_SA_JSON_PATH,
            scopes=scopes,
        )

    return gspread.authorize(creds)


# ============================================================
# СКАЧИВАНИЕ BOT_URLS.xlsx
# ============================================================

def download_bot_urls():
    print(
        "⬇️ Скачиваю Bot_URLS.xlsx из Google Sheets..."
    )

    response = requests.get(
        BOT_URLS_URL,
        timeout=60,
    )

    response.raise_for_status()

    os.makedirs(
        os.path.dirname(BOT_URLS_PATH),
        exist_ok=True,
    )

    with open(BOT_URLS_PATH, "wb") as file:
        file.write(response.content)

    print(
        f"✅ Файл успешно сохранён: {BOT_URLS_PATH}"
    )


# ============================================================
# ЗАГРУЗКА КУРСА ИЗ GOOGLE SHEETS
# ============================================================

def load_kurs_from_google(
    spreadsheet,
) -> float:
    """
    Загружает курс из листа bot_settings.

    Формат:

    setting | value
    kurs    | 12000
    """

    print(
        f"💱 Загружаю курс из Google Sheets: "
        f"{SETTINGS_SHEET_NAME}"
    )

    worksheet = spreadsheet.worksheet(
        SETTINGS_SHEET_NAME
    )

    data = worksheet.get_all_records()

    settings_df = pd.DataFrame(data)

    required_columns = [
        "setting",
        "value",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in settings_df.columns
    ]

    if missing_columns:
        raise ValueError(
            "В Google Sheets отсутствуют колонки "
            "в bot_settings: "
            + ", ".join(missing_columns)
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
            "В листе bot_settings "
            "не найдена строка setting = kurs"
        )

    kurs_value = pd.to_numeric(
        kurs_rows.iloc[0]["value"],
        errors="coerce",
    )

    if pd.isna(kurs_value):
        raise ValueError(
            "Значение kurs в bot_settings "
            "не является числом"
        )

    kurs_value = float(kurs_value)

    if kurs_value <= 0:
        raise ValueError(
            f"Некорректный курс: {kurs_value}"
        )

    print(
        f"✅ Курс из Google Sheets: "
        f"{kurs_value:,.0f}".replace(",", " ")
    )

    return kurs_value


# ============================================================
# ЗАГРУЗКА ЦЕН ИЗ GOOGLE SHEETS
# ============================================================

def load_prices_from_google(
    spreadsheet,
) -> pd.DataFrame:
    """
    Загружает весь лист bot_prices.

    Нужные колонки:

    product_id
    model_name
    memory
    color
    price
    warranty_period
    """

    print(
        f"⬇️ Загружаю цены из Google Sheets: "
        f"{PRICES_SHEET_NAME}"
    )

    worksheet = spreadsheet.worksheet(
        PRICES_SHEET_NAME
    )

    data = worksheet.get_all_records()

    price_df = pd.DataFrame(data)

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
        if column not in price_df.columns
    ]

    if missing_columns:
        raise ValueError(
            "В Google Sheets отсутствуют колонки: "
            + ", ".join(missing_columns)
        )

    price_df["product_id"] = pd.to_numeric(
        price_df["product_id"],
        errors="coerce",
    )

    price_df["price"] = pd.to_numeric(
        price_df["price"],
        errors="coerce",
    )

    price_df["warranty_period"] = pd.to_numeric(
        price_df["warranty_period"],
        errors="coerce",
    )

    price_df = price_df.dropna(
        subset=[
            "product_id",
            "price",
        ]
    )

    price_df = price_df[
        price_df["price"] > 0
    ].copy()

    price_df["product_id"] = (
        price_df["product_id"]
        .astype(int)
    )

    price_df["model_name"] = (
        price_df["model_name"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    price_df["memory"] = (
        price_df["memory"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    price_df["color"] = (
        price_df["color"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    print(
        f"✅ Получено цен из Google Sheets: "
        f"{len(price_df)}"
    )

    return price_df


# ============================================================
# ЗАГРУЗКА СОРТИРОВКИ ИЗ GOOGLE SHEETS
# ============================================================

def load_sorting_from_google(
    client,
):
    """
    Загружает product_sort из отдельной Google-таблицы.

    Используются только:
    - model_name
    - memory_sort
    - color_sort

    Лишние и пустые колонки игнорируются.
    """

    print(
        "⬇️ Загружаю product_sort из Google Sheets..."
    )

    sort_spreadsheet = client.open_by_key(
        SORT_SPREADSHEET_ID
    )

    worksheet = sort_spreadsheet.sheet1

    # Берём сырые значения, чтобы не было ошибки
    # из-за пустых/повторяющихся заголовков.
    values = worksheet.get_all_values()

    if not values:
        raise ValueError(
            "Google-таблица product_sort пустая"
        )

    headers = [
        str(value).strip()
        for value in values[0]
    ]

    # --------------------------------------------------------
    # ИЩЕМ НУЖНЫЕ КОЛОНКИ
    # --------------------------------------------------------

    required_columns = [
        "model_name",
        "memory_sort",
        "color_sort",
    ]

    column_indexes = {}

    for column_name in required_columns:

        try:
            column_indexes[column_name] = (
                headers.index(column_name)
            )

        except ValueError:

            raise ValueError(
                f"В product_sort не найдена колонка: "
                f"{column_name}"
            )

    # --------------------------------------------------------
    # СОБИРАЕМ ТОЛЬКО НУЖНЫЕ ДАННЫЕ
    # --------------------------------------------------------

    rows = []

    for row in values[1:]:

        def get_value(column_name):

            index = column_indexes[
                column_name
            ]

            if index < len(row):
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

    # --------------------------------------------------------
    # ПОРЯДОК МОДЕЛЕЙ
    # --------------------------------------------------------

    model_values = [
        value
        for value
        in sort_df["model_name"].tolist()
        if value
    ]

    model_values = list(
        dict.fromkeys(
            model_values
        )
    )

    model_order = {
        name: i
        for i, name
        in enumerate(
            model_values
        )
    }

    # --------------------------------------------------------
    # ПОРЯДОК ПАМЯТИ
    # --------------------------------------------------------

    memory_values = [
        value
        for value
        in sort_df["memory_sort"].tolist()
        if value
    ]

    memory_values = list(
        dict.fromkeys(
            memory_values
        )
    )

    memory_order = {
        name: i
        for i, name
        in enumerate(
            memory_values
        )
    }

    # --------------------------------------------------------
    # ПОРЯДОК ЦВЕТОВ
    # --------------------------------------------------------

    color_values = [
        value
        for value
        in sort_df["color_sort"].tolist()
        if value
    ]

    color_values = list(
        dict.fromkeys(
            color_values
        )
    )

    color_order = {
        name: i
        for i, name
        in enumerate(
            color_values
        )
    }

    print(
        "✅ product_sort загружен из Google Sheets"
    )

    print(
        f"   📱 Моделей: "
        f"{len(model_order)}"
    )

    print(
        f"   💾 Вариантов памяти: "
        f"{len(memory_order)}"
    )

    print(
        f"   🎨 Цветов: "
        f"{len(color_order)}"
    )

    return (
        model_order,
        memory_order,
        color_order,
    )


# ============================================================
# ПОЛУЧЕНИЕ ТОВАРОВ
# ============================================================

def get_product_prices(
    product_ids,
    all_prices_df,
    model_order,
    memory_order,
    color_order,
):

    product_ids = [
        int(product_id)
        for product_id in product_ids
    ]

    price_df = all_prices_df[
        all_prices_df["product_id"].isin(
            product_ids
        )
    ].copy()

    if price_df.empty:
        return price_df

    # Для совместимости со старым кодом
    price_df = price_df.rename(
        columns={
            "product_id": "id",
        }
    )

    # --------------------------------------------------------
    # СОРТИРОВКА
    # --------------------------------------------------------

    price_df["model_sort"] = (
        price_df["model_name"]
        .map(model_order)
        .fillna(9999)
    )

    price_df["memory_sort"] = (
        price_df["memory"]
        .map(memory_order)
        .fillna(9999)
    )

    price_df["color_sort"] = (
        price_df["color"]
        .map(color_order)
        .fillna(9999)
    )

    price_df[
        ["memory", "color"]
    ] = price_df[
        ["memory", "color"]
    ].fillna("")

    price_df = price_df.sort_values(
        by=[
            "model_sort",
            "memory_sort",
            "color_sort",
        ]
    )

    price_df = price_df.drop(
        columns=[
            "model_sort",
            "memory_sort",
            "color_sort",
        ]
    )

    return price_df


# ============================================================
# ОБНОВЛЕНИЕ TELEGRAM СООБЩЕНИЯ
# ============================================================

async def update_message_text_by_link(
    bot,
    link,
    id_excelurl,
    new_text,
):
    pattern = (
        r"https?://t\.me/(?:c/)?"
        r"(?P<chat_identifier>[\w\d_]+)/"
        r"(?:(?P<topic_id>\d+)/)?"
        r"(?P<message_id>\d+)"
    )

    match = re.match(
        pattern,
        str(link),
    )

    if not match:
        print(
            f"❌ Неверный формат ссылки: {link}"
        )
        return

    chat_identifier = match.group(
        "chat_identifier"
    )

    mapping = {
        "Texnikach_Phone": "2183885353",
        "Texnikach_Planshet": "2474667815",
        "Texnikach_Notebook": "2305277813",
        "texnikach_watch": "2326008222",
        "Texnikach_Music": "2469451519",
        "Texnikach_Home": "2368266631",
        "Texnikach_accsessories": "2324584882",
        "texnikach": "1463992448",
        "Texnikach_dop": "2673457333",
    }

    chat_identifier = mapping.get(
        chat_identifier,
        chat_identifier,
    )

    message_id = int(
        match.group("message_id")
    )

    try:

        if chat_identifier.isdigit():

            chat_id = int(
                f"-100{chat_identifier}"
            )

        else:

            chat = await bot.get_chat(
                chat_identifier
            )

            chat_id = chat.id

        api_url = (
            "https://api.telegram.org/"
            f"bot{bot.token}/editMessageText"
        )

        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {
                "html": new_text,
            },
        }

        response = await asyncio.to_thread(
            requests.post,
            api_url,
            json=payload,
            timeout=30,
        )

        try:
            result = response.json()

        except Exception:
            raise RuntimeError(
                response.text
            )

        if (
            not response.ok
            or not result.get("ok")
        ):

            raise RuntimeError(
                result.get(
                    "description",
                    response.text,
                )
            )

        print(
            f"✅ Текст сообщения {link} "
            f"успешно обновлён. "
            f"ID: {id_excelurl}"
        )

    except Exception as error:

        print(
            f"❌ {id_excelurl} "
            f"Ошибка при обновлении "
            f"сообщения {link}: "
            f"{error}"
        )


# ============================================================
# ФОРМАТ ЦЕНЫ
# ============================================================

def format_price_uzs_usd(
    price,
    rate=None,
):

    global kurs

    if rate is None:
        rate = kurs

    if rate is None:
        raise RuntimeError(
            "Курс доллара ещё не загружен"
        )

    price = float(price)
    rate = float(rate)

    if price.is_integer():
        usd = int(price)
    else:
        usd = price

    uzs = price * rate

    if uzs > 10000:

        uzs = int(
            round(
                uzs / 1000
            ) * 1000
        )

    else:

        uzs = int(
            round(uzs)
        )

    uzs_formatted = f"{uzs:,}"

    return (
        f"{uzs_formatted}-So'm({usd})"
    )


# ============================================================
# ФОРМИРОВАНИЕ ТАБЛИЦЫ ЦЕН
# ============================================================

def format_prices(
    model_name,
    group,
):

    rows = []

    has_memory = (
        "memory" in group.columns
        and group[
            "memory"
        ]
        .astype(str)
        .str.strip()
        .ne("")
        .any()
    )

    has_color = (
        "color" in group.columns
        and group[
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

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                [
                    "memory",
                    "price",
                    "warranty_period",
                ],
                sort=False,
                dropna=False,
            )
        )

        for (
            group_keys,
            sub_group_df,
        ) in sub_group:

            (
                memory,
                price,
                warranty,
            ) = group_keys

            colors = (
                "•"
                + "•".join(
                    color
                    .strip()
                    .replace(
                        " ",
                        "_",
                    )
                    for color
                    in sub_group_df[
                        "color"
                    ]
                    .astype(str)
                    .unique()
                    if color.strip()
                )
            )

            formatted_price = (
                format_price_uzs_usd(
                    price
                )
            )

            if (
                pd.notna(warranty)
                and int(warranty) == 12
            ):

                formatted_price = (
                    f"{formatted_price} ✔️"
                )

            rows.append(
                {
                    "model": model_name,
                    "memory": str(
                        memory
                    ).strip(),
                    "color": colors,
                    "price": formatted_price,
                }
            )

    # ========================================================
    # ТОЛЬКО ПАМЯТЬ
    # ========================================================

    elif has_memory:

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                [
                    "memory",
                    "price",
                    "warranty_period",
                ],
                sort=False,
                dropna=False,
            )
        )

        for group_keys, _ in sub_group:

            (
                memory,
                price,
                warranty,
            ) = group_keys

            formatted_price = (
                format_price_uzs_usd(
                    price
                )
            )

            if (
                pd.notna(warranty)
                and int(warranty) == 12
            ):

                formatted_price = (
                    f"{formatted_price} ✔️"
                )

            rows.append(
                {
                    "model": model_name,
                    "memory": str(
                        memory
                    ).strip(),
                    "price": formatted_price,
                }
            )

    # ========================================================
    # ТОЛЬКО ЦВЕТ
    # ========================================================

    elif has_color:

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                [
                    "price",
                    "warranty_period",
                ],
                sort=False,
                dropna=False,
            )
        )

        for (
            group_keys,
            sub_group_df,
        ) in sub_group:

            price, warranty = group_keys

            colors = (
                "•"
                + "•".join(
                    color
                    .strip()
                    .replace(
                        " ",
                        "_",
                    )
                    for color
                    in sub_group_df[
                        "color"
                    ]
                    .astype(str)
                    .unique()
                    if color.strip()
                )
            )

            formatted_price = (
                format_price_uzs_usd(
                    price
                )
            )

            if (
                pd.notna(warranty)
                and int(warranty) == 12
            ):

                formatted_price = (
                    f"{formatted_price} ✔️"
                )

            rows.append(
                {
                    "model": model_name,
                    "color": colors,
                    "price": formatted_price,
                }
            )

    # ========================================================
    # БЕЗ ПАМЯТИ И ЦВЕТА
    # ========================================================

    else:

        sub_group = (
            group
            .sort_values(
                by="price"
            )
            .groupby(
                [
                    "price",
                    "warranty_period",
                ],
                sort=False,
                dropna=False,
            )
        )

        for group_keys, _ in sub_group:

            price, warranty = group_keys

            formatted_price = (
                format_price_uzs_usd(
                    price
                )
            )

            if (
                pd.notna(warranty)
                and int(warranty) == 12
            ):

                formatted_price = (
                    f"{formatted_price} ✔️"
                )

            rows.append(
                {
                    "model": model_name,
                    "price": formatted_price,
                }
            )

    if not rows:
        return ""

    # ========================================================
    # КОЛОНКИ
    # ========================================================

    columns = []

    if has_memory:
        columns.append(
            (
                "memory",
                "Память",
            )
        )

    if has_color:
        columns.append(
            (
                "color",
                "Цвет",
            )
        )

    columns.append(
        (
            "price",
            "Цена",
        )
    )

    header_html = "".join(
        f"<th>{html.escape(title)}</th>"
        for _, title
        in columns
    )

    table_rows = []

    for row in rows:

        cells = []

        for key, _ in columns:

            raw_value = str(
                row.get(
                    key,
                    "",
                )
            )

            escaped_value = html.escape(
                raw_value
            )

            if key == "price":

                cells.append(
                    '<td align="right" '
                    'valign="middle" '
                    'nowrap>'
                    f"{escaped_value}"
                    "</td>"
                )

            else:

                cells.append(
                    '<td valign="middle" '
                    'nowrap>'
                    f"{escaped_value}"
                    "</td>"
                )

        table_rows.append(
            "<tr>"
            + "".join(cells)
            + "</tr>"
        )

    return (
        f"<h1>"
        f"{html.escape(str(model_name))}"
        f"</h1>"
        "<table bordered striped>"
        f"<tr>{header_html}</tr>"
        + "".join(table_rows)
        + "</table>"
    )


# ============================================================
# НИЖНИЙ ТЕКСТ
# ============================================================

end_text = (
    '<p><a href="https://t.me/Texnikach_info/3">'
    "📦 Заказать / Sotib olish"
    "</a></p>"
    "<p><b>Характеристики</b> ⤵️</p>"
)


# ============================================================
# ОБНОВЛЕНИЕ ВСЕХ СООБЩЕНИЙ
# ============================================================

async def update_all_messages(
    token,
    df,
    all_prices_df,
    model_order,
    memory_order,
    color_order,
):

    bot = Bot(
        token=token
    )

    total = len(df)

    formatted_date = get_formatted_date()

    print(
        f"📨 Сообщений в Bot_URLS: {total}"
    )

    for index, row in df.iterrows():

        try:

            link = str(
                row["post_id"]
            ).strip()

            id_excelurl = row[
                "product_id"
            ]

            product_ids = [
                int(
                    product_id.strip()
                )
                for product_id
                in str(
                    row["product_id"]
                ).split(",")
                if product_id.strip()
            ]

            post_id = (
                link.lower()
            )

            # =================================================
            # ПРОВЕРКА КАНАЛА
            # =================================================

            if (
                (
                    phone == 1
                    and (
                        "texnikach_phone"
                        in post_id
                        or "2183885353"
                        in post_id
                    )
                )
                or
                (
                    planshet == 1
                    and (
                        "texnikach_planshet"
                        in post_id
                        or "2474667815"
                        in post_id
                    )
                )
                or
                (
                    notebook == 1
                    and (
                        "texnikach_notebook"
                        in post_id
                        or "2305277813"
                        in post_id
                    )
                )
                or
                (
                    watch == 1
                    and (
                        "texnikach_watch"
                        in post_id
                        or "2326008222"
                        in post_id
                    )
                )
                or
                (
                    music == 1
                    and (
                        "texnikach_music"
                        in post_id
                        or "2469451519"
                        in post_id
                    )
                )
                or
                (
                    home == 1
                    and (
                        "texnikach_home"
                        in post_id
                        or "2368266631"
                        in post_id
                    )
                )
                or
                (
                    accsessories == 1
                    and (
                        "texnikach_accsessories"
                        in post_id
                        or "2324584882"
                        in post_id
                    )
                )
                or
                (
                    dop == 1
                    and (
                        "texnikach_dop"
                        in post_id
                        or "2673457333"
                        in post_id
                    )
                )
                or
                (
                    texnikach == 1
                    and (
                        "texnikach/"
                        in post_id
                        or "1463992448"
                        in post_id
                    )
                )
            ):

                price_df = get_product_prices(
                    product_ids=product_ids,
                    all_prices_df=all_prices_df,
                    model_order=model_order,
                    memory_order=memory_order,
                    color_order=color_order,
                )

                if price_df.empty:

                    new_text = (
                        "\n"
                        "Нет в наличии ❌"
                        "\n\n"
                        "Mavjud emas ❌"
                        "\n\n"
                    )

                else:

                    grouped = price_df.groupby(
                        "model_name",
                        sort=False,
                    )

                    new_text = "".join(
                        [
                            format_prices(
                                model,
                                group,
                            )
                            for model, group
                            in grouped
                        ]
                    )

                await update_message_text_by_link(
                    bot=bot,
                    link=link,
                    id_excelurl=id_excelurl,
                    new_text=(
                        formatted_date
                        + new_text
                        + end_text
                    ),
                )

                await asyncio.sleep(3)

        except Exception as error:

            print(
                f"❌ Ошибка строки "
                f"{index + 1}/{total}: "
                f"{error}"
            )


# ============================================================
# ОТПРАВКА ТЕКСТА В ГРУППУ
# ============================================================

async def send_text_to_group(
    token: str,
    target: str | int,
    text: str,
    parse_mode: str | None = "Markdown",
    disable_web_page_preview: bool = True,
):

    bot = Bot(
        token=token
    )

    chat_id = None
    thread_id = None

    if isinstance(
        target,
        int,
    ):

        chat_id = target

    elif isinstance(
        target,
        str,
    ):

        pattern = (
            r"https?://t\.me/"
            r"(?:(?P<prefix>c)/)?"
            r"(?P<ident>[\w\d_]+)/"
            r"(?:(?P<topic>\d+)/)?"
            r"(?P<msg>\d+)?"
        )

        match = re.match(
            pattern,
            target,
        )

        if match:

            ident = match.group(
                "ident"
            )

            topic = match.group(
                "topic"
            )

            if (
                match.group("prefix")
                == "c"
                and ident.isdigit()
            ):

                chat_id = int(
                    f"-100{ident}"
                )

            else:

                chat_id = (
                    ident
                    if ident.startswith("@")
                    else f"@{ident}"
                )

            if topic:

                thread_id = int(
                    topic
                )

        else:

            s = target.strip()

            if s.startswith("@"):

                chat_id = s

            elif (
                s.startswith("-100")
                and s[4:].isdigit()
            ):

                chat_id = int(s)

            elif s.isdigit():

                chat_id = int(
                    f"-100{s}"
                )

            else:

                chat_id = f"@{s}"

    if chat_id is None:

        raise ValueError(
            "Не смог распознать "
            "chat_id/username из target"
        )

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        disable_web_page_preview=(
            disable_web_page_preview
        ),
        message_thread_id=thread_id,
    )

    return sent.message_id


# ============================================================
# TOPIC IDS
# ============================================================

topic_ids = [
    # 3,  # 16 Pro Max
    # 7,  # iPhone 11
    # 6,
]


print(topic_ids)


# ============================================================
# MAIN
# ============================================================

async def main():

    global kurs

    print(
        "=" * 60
    )

    print(
        "🚀 TEXNIKACH TELEGRAM PRICE BOT"
    )

    print(
        "=" * 60
    )

    # --------------------------------------------------------
    # TOKEN
    # --------------------------------------------------------

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "\nНе задан TELEGRAM_BOT_TOKEN.\n"
            "Добавь его в Environment Variables."
        )

    # --------------------------------------------------------
    # ЗАДЕРЖКА
    # --------------------------------------------------------

    if START_DELAY_SECONDS > 0:

        print(
            f"⏳ Задержка перед запуском: "
            f"{START_DELAY_SECONDS} сек."
        )

        await asyncio.sleep(
            START_DELAY_SECONDS
        )

    print(
        "✅ Код запустился"
    )

    # --------------------------------------------------------
    # BOT_URLS
    # --------------------------------------------------------

    download_bot_urls()

    df = pd.read_excel(
        BOT_URLS_PATH
    )

    print(
        f"✅ Bot_URLS загружен: "
        f"{len(df)} строк"
    )

    # --------------------------------------------------------
    # GOOGLE
    # --------------------------------------------------------

    print(
        "🔗 Подключаюсь к Google Sheets..."
    )

    client = get_google_client()

    print(
        "✅ Авторизация Google Sheets успешна"
    )

    # --------------------------------------------------------
    # ОСНОВНАЯ GOOGLE-ТАБЛИЦА
    # --------------------------------------------------------

    spreadsheet = client.open_by_key(
        SPREADSHEET_ID
    )

    print(
        "✅ Основная Google-таблица открыта"
    )

    # --------------------------------------------------------
    # СОРТИРОВКА ИЗ GOOGLE
    # --------------------------------------------------------

    (
        model_order,
        memory_order,
        color_order,
    ) = load_sorting_from_google(
        client
    )

    # --------------------------------------------------------
    # КУРС ИЗ GOOGLE
    # --------------------------------------------------------

    kurs = load_kurs_from_google(
        spreadsheet
    )

    # --------------------------------------------------------
    # ЦЕНЫ ИЗ GOOGLE
    # --------------------------------------------------------

    all_prices_df = (
        load_prices_from_google(
            spreadsheet
        )
    )

    # --------------------------------------------------------
    # ПРОВЕРКА
    # --------------------------------------------------------

    print()
    print(
        "📊 Данные для обновления:"
    )

    print(
        f"💱 Курс: "
        f"{kurs:,.0f}".replace(",", " ")
    )

    print(
        f"📦 Цен: "
        f"{len(all_prices_df)}"
    )

    print(
        f"📱 Моделей сортировки: "
        f"{len(model_order)}"
    )

    print(
        f"💾 Памяти сортировки: "
        f"{len(memory_order)}"
    )

    print(
        f"🎨 Цветов сортировки: "
        f"{len(color_order)}"
    )

    print(
        f"📨 Telegram-сообщений: "
        f"{len(df)}"
    )

    # --------------------------------------------------------
    # ОБНОВЛЕНИЕ TELEGRAM
    # --------------------------------------------------------

    print()
    print(
        "🚀 Начинаю обновление Telegram..."
    )
    print()

    await update_all_messages(
        token=TELEGRAM_BOT_TOKEN,
        df=df,
        all_prices_df=all_prices_df,
        model_order=model_order,
        memory_order=memory_order,
        color_order=color_order,
    )

    print()
    print(
        "=" * 60
    )

    print(
        "✅ ОБНОВЛЕНИЕ ЗАВЕРШЕНО"
    )

    print(
        "=" * 60
    )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    while True:

        try:
            asyncio.run(
                main()
            )

        except KeyboardInterrupt:
            print(
                "🛑 Бот остановлен вручную"
            )
            break

        except Exception as error:
            print(
                f"❌ Критическая ошибка: {error}"
            )

        print()
        print(
            "🔄 Следующий полный запуск через 10 минут..."
        )

        time.sleep(600)