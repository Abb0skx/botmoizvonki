from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .templates import (
    TEMPLATE_ALIASES,
    TEMPLATES,
    TemplateOverride,
    normalize_template_code,
    render_template,
    validate_template_text,
)

LOG = logging.getLogger("telegram_business.sheets")

SHEETS = {
    "Автоответы": ["code", "enabled", "scope", "priority", "text_ru", "text_uz", "cooldown_minutes", "notes", "updated_at"],
    "Интенты": ["intent_code", "enabled", "scope", "priority", "keywords_ru", "keywords_uz", "negative_keywords", "template_code", "stop_processing", "notes"],
    "Настройки": ["key", "value", "type", "description", "updated_at"],
    "Диалоги": ["session_id", "cycle_id", "business_date", "chat_id", "telegram_user_id", "name", "username", "language", "first_client_at_uz", "last_client_at_uz", "first_bot_at_uz", "bot_response_seconds", "manager_due_at_uz", "first_manager_at_uz", "calendar_response_seconds", "work_response_seconds", "model_query", "matched_model", "memory", "color", "location_received", "location_url", "preferred_time", "price_sent", "order_intent", "credit_intent", "final_sent", "needs_manager_reply", "priority", "handoff_reason", "status", "created_at_utc", "updated_at_utc"],
    "Сообщения": ["event_id", "update_id", "business_connection_id", "chat_id", "message_id", "session_id", "cycle_id", "direction", "sender_type", "telegram_date_uz", "message_type", "text", "language", "intent", "model_query", "template_code", "reply_to_message_id", "processed_status", "created_at_utc"],
    "Статистика": ["period", "metric", "value", "updated_at_utc"],
    "Ошибки": ["error_id", "date_uz", "source", "operation", "chat_id", "session_id", "error_type", "message", "attempts", "resolved", "created_at_utc"],
    "Заявки": [
        "request_id", "session_id", "cycle_id", "business_date", "chat_id",
        "telegram_user_id", "language", "state", "status",
        "model_query", "exact_model", "model_url", "option_kind",
        "option_value", "color", "color_any", "fulfillment_method",
        "contact_method", "phone_masked", "location_received", "location_url",
        "address", "preferred_time", "database_price", "source_updated_at",
        "items",
        "needs_manager_reply", "created_at_utc", "updated_at_utc",
    ],
}

_AUTO_META = {
    "greeting_no_model": ("night", 100, 0, "Первое ночное сообщение без модели"),
    "greeting_model": ("night", 100, 0, "Короткое приветствие, когда модель уже указана"),
    "credit": ("all", 100, 720, "Кредит и рассрочка; круглосуточно"),
    "product_result": ("night", 80, 10, "Результат поиска; название модели может быть ссылкой"),
    "ambiguous": ("night", 80, 0, "До пяти кликабельных моделей"),
    "not_found_1": ("night", 70, 0, "Первая неудачная попытка поиска"),
    "not_found_2": ("night", 70, 0, "Вторая попытка, затем менеджер"),
    "location_before_model": ("night", 85, 0, "Локация получена до модели"),
    "order_request": ("night", 95, 0, "Не подтверждает и не оформляет заказ"),
    "human_handoff": ("night", 100, 0, "Передача вопроса менеджеру"),
    "media_only": ("night", 75, 0, "Медиа без текста"),
    "final": ("night", 90, 0, "Один раз после цены"),
    "product_source_unavailable": ("night", 100, 0, "Без старой или вымышленной цены"),
    "data_added": ("night", 20, 0, "Не чаще одного раза за 30 секунд"),
    "catalog": ("night", 60, 0, "Ссылка на актуальный каталог"),
    "delivery": ("night", 65, 0, "Только утверждённые условия доставки"),
    "pickup": ("night", 65, 0, "Самовывоз только после подтверждения"),
    "availability": ("night", 65, 0, "Наличие подтверждает только менеджер"),
    "request_choose_attribute": ("night", 85, 0, "Выбор памяти или размера из реальных вариантов"),
    "request_choose_color": ("night", 85, 0, "Выбор реального цвета или цвет не важен"),
    "request_choose_fulfillment": ("night", 90, 0, "Доставка или самовывоз"),
    "request_delivery_phone": ("night", 90, 0, "Телефон обязателен только для полной заявки на доставку"),
    "request_pickup_contact": ("night", 90, 0, "Для самовывоза телефон необязателен"),
    "request_delivery_location": ("night", 90, 0, "Адрес обязателен для полной заявки на доставку"),
    "request_review": ("night", 95, 0, "Сводка без подтверждения заказа"),
    "request_saved_delivery": ("night", 100, 0, "Данные доставки переданы менеджеру"),
    "request_saved_pickup": ("night", 100, 0, "Самовывоз только после подтверждения менеджера"),
    "request_partial_saved": ("night", 100, 0, "Неполный черновик передан менеджеру"),
    "request_cancelled": ("night", 100, 0, "Отмена с очисткой телефона и точной локации"),
    "request_stale_button": ("night", 100, 0, "Устаревшая или чужая кнопка"),
    "request_invalid_phone": ("night", 90, 0, "Телефон не распознан"),
    "request_invalid_location": ("night", 90, 0, "Адрес или локация не распознаны"),
}

_TEMPLATE_ORDER = tuple(_AUTO_META)

INTENT_SEED = (
    ("credit", True, "all", 100, "кредит;рассрочка;в рассрочку;оплата частями;платить частями;ежемесячно;в месяц;без первоначального взноса", "kredit;nasiya;nasiyaga;bo‘lib to‘lash;bo’lib to’lash;bolib tolash;muddatli to‘lov;oyma-oy;boshlang‘ich to‘lovsiz;насия;бўлиб тўлаш;муддатли тўлов;ойма-ой;бошланғич тўловсиз", "кредитная карта;кредит не нужен;без кредита;оплачу полностью;kredit kerak emas;kreditsiz;to‘liq to‘layman;кредит керак эмас;кредитсиз;тўлиқ тўлайман", "credit", False, "Круглосуточно, cooldown"),
    ("product_search", True, "night", 80, "цена;сколько стоит;модель;нархи", "narxi;qancha;model", "", "product_result", False, "Модель также определяется без ключевого слова"),
    ("order_request", True, "night", 95, "беру;оформите;заказываю;отправьте;доставьте", "olaman;buyurtma;yuboring;yetkazib bering;оламан;буюртма;юборинг;етказиб беринг;расмийлаштиринг", "", "order_request", False, "Не оформлять заказ"),
    ("location", True, "night", 90, "локация;адрес;координаты", "lokatsiya;manzil;koordinata", "", "location_before_model", False, "Telegram Location и карты"),
    ("delivery", True, "night", 60, "доставк;доставит;привез", "yetkazib berish;yetkazish;етказиб бериш", "", "delivery", False, "Только утверждённые условия"),
    ("pickup", True, "night", 60, "самовывоз;заберу;приехать", "olib ketish;borib olaman", "", "pickup", False, "Только после подтверждения"),
    ("catalog", True, "night", 50, "каталог;сайт;ссылка", "katalog;sayt;havola", "", "catalog", False, "Каталог TEXNIKACH"),
    ("payment", True, "night", 100, "оплата картой;перевод на карту;реквизит", "karta orqali;to‘lov ma’lumot;карта орқали;тўлов маълумот", "", "human_handoff", True, "Платёжные данные не сохранять"),
    ("availability", True, "night", 65, "наличие;есть в наличии", "mavjud;bormi;мавжуд;мавжудми;борми", "", "availability", False, "Наличие не подтверждать"),
    ("warranty", True, "night", 100, "гарантия;гарантийный", "kafolat;garantiya;кафолат", "", "human_handoff", True, "Всегда менеджеру"),
    ("return", True, "night", 100, "возврат;вернуть;возврат денег", "qaytarish;pulni qaytarish;қайтар;пулни қайтар", "", "human_handoff", True, "Всегда менеджеру"),
    ("complaint", True, "night", 100, "жалоба;брак;сломался;конфликт", "shikoyat;nuqson;buzildi;nizo;шикоят;нуқсон;бузил;низо", "", "human_handoff", True, "Всегда менеджеру"),
    ("human_request", True, "night", 100, "позовите менеджера;живой человек;оператор", "menejer;odam bilan gaplashish;operator;менежер;оператор;одам билан;инсон билан", "", "human_handoff", True, "Всегда менеджеру"),
    ("discount", True, "night", 100, "скидка;торг;последняя цена", "chegirma;oxirgi narx;savdolashish;чегирма;охирги нарх;савдолаш", "", "human_handoff", True, "Всегда менеджеру"),
    ("active_order", True, "night", 100, "мой заказ;где заказ;статус заказа;заказ уже;заказ оформлен;изменить заказ;изменить адрес доставки в заказе;отменить заказ;курьер уже едет", "buyurtmam;buyurtma qayerda;buyurtma holati;buyurtma berilgan;buyurtma manzilini o‘zgartir;buyurtmani bekor qil;kuryer yo‘lda;буюртмам;буюртма қаерда;буюртма ҳолати;буюртма берилган;буюртма манзилини ўзгартир;буюртмани бекор қил;курьер йўлда", "", "human_handoff", True, "Уже оформленный или изменяемый заказ — всегда менеджеру"),
    ("technical", True, "night", 100, "характеристик;камера;экран;герц;поддерживает;совместим;что лучше", "xususiyat;kamera;ekran;gers;qo‘llab-quvvatlaydi;mos keladi;qaysi biri yaxshi;хусусият;камера;экран;герц;қўллаб-қувватлайди;мос келади;қайси бири яхши", "", "human_handoff", True, "Технические вопросы без утверждённого ответа — менеджеру"),
    ("media_only", True, "night", 75, "фото;голосовое;видео;файл", "rasm;ovozli;video;fayl", "", "media_only", True, "Тип сообщения также проверяется"),
)

SETTINGS_SEED = (
    ("timezone", "Asia/Tashkent", "string", "Часовой пояс"),
    ("night_start", "20:00", "time", "Начало ночного режима"),
    ("night_end", "09:30", "time", "Окончание ночного режима"),
    ("manager_start", "10:00", "time", "Начало рабочего времени менеджера"),
    ("manager_end", "20:00", "time", "Окончание рабочего времени менеджера"),
    ("workdays", "1,2,3,4,5,6,7", "list", "Рабочие дни ISO: понедельник=1"),
    ("final_idle_seconds", "300", "int", "Тишина до финального сообщения"),
    ("debounce_seconds", "3", "int", "Объединение коротких сообщений"),
    ("manager_lock_minutes", "120", "int", "Пауза после ручного ответа"),
    ("credit_cooldown_minutes", "720", "int", "Cooldown ответа о кредите"),
    ("template_cache_seconds", "300", "int", "Кэш ответов и настроек"),
    ("max_bot_messages_10m", "4", "int", "Антиспам за 10 минут"),
    ("max_bot_messages_session", "8", "int", "Антиспам за сессию"),
    ("delivery_city", "Tashkent", "string", "Город бесплатной доставки"),
    ("delivery_price", "0", "int", "Цена доставки по Ташкенту"),
    ("delivery_eta", "2–3 часа", "string", "После подтверждения"),
    ("catalog_url", "https://texnikach.uz/go", "string", "Ссылка на актуальный каталог"),
)

SHEET_SEEDS = {
    "Автоответы": [
        [
            code,
            True,
            _AUTO_META[code][0],
            _AUTO_META[code][1],
            TEMPLATES[code]["ru"],
            TEMPLATES[code]["uz"],
            _AUTO_META[code][2],
            _AUTO_META[code][3],
            "",
        ]
        for code in _TEMPLATE_ORDER
    ],
    "Интенты": [list(row) for row in INTENT_SEED],
    "Настройки": [[*row, ""] for row in SETTINGS_SEED],
}


@dataclass(frozen=True)
class IntentOverride:
    code: str
    enabled: bool
    scope: str
    priority: int
    keywords_ru: tuple[str, ...]
    keywords_uz: tuple[str, ...]
    negative_keywords: tuple[str, ...]
    template_code: str
    stop_processing: bool
    notes: str = ""


@dataclass(frozen=True)
class SheetContent:
    templates: Mapping[str, TemplateOverride]
    intents: tuple[IntentOverride, ...]
    settings: Mapping[str, Any]
    refreshed_at: datetime | None = None


def google_client():
    import gspread
    from google.oauth2.service_account import Credentials

    # Opening a spreadsheet by its ID and editing its worksheets only needs the
    # Sheets scope.  Do not request broad Google Drive access for customer data.
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    content = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_SA_JSON_CONTENT")
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH") or os.getenv("GOOGLE_SA_JSON_PATH")
    if content:
        try:
            info = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Google service account JSON is invalid") from exc
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif path:
        creds = Credentials.from_service_account_file(path, scopes=scopes)
    else:
        raise RuntimeError("Google service account is not configured")
    return gspread.authorize(creds)


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on", "да", "истина"}:
        return True
    if normalized in {"0", "false", "no", "off", "нет", "ложь"}:
        return False
    raise ValueError("boolean value is invalid")


def _integer(value: Any, *, minimum: int = 0) -> int:
    parsed = int(str(value or "0").strip())
    if parsed < minimum:
        raise ValueError("integer is below minimum")
    return parsed


def _keywords(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip().casefold()
            for part in re.split(r"[;\n,]+", str(value or ""))
            if part.strip()
        )
    )


def _parse_setting(value: Any, kind: Any) -> Any:
    raw = str(value or "").strip()
    normalized = str(kind or "string").strip().lower()
    if normalized in {"string", "str", "text"}:
        return raw
    if normalized in {"int", "integer"}:
        return int(raw)
    if normalized in {"float", "number"}:
        return float(raw)
    if normalized in {"bool", "boolean"}:
        return _truth(raw)
    if normalized == "time":
        hour, minute = (int(part) for part in raw.split(":", 1))
        time(hour, minute)
        return raw
    if normalized in {"list", "csv"}:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if normalized == "json":
        return json.loads(raw)
    raise ValueError("unsupported setting type")


def _validate_setting(key: str, value: Any) -> None:
    if key == "timezone":
        ZoneInfo(str(value))
    if key.endswith("_seconds") or key.endswith("_minutes"):
        if int(value) < 0:
            raise ValueError("duration cannot be negative")
    if key in {"max_bot_messages_10m", "max_bot_messages_session"} and int(value) < 1:
        raise ValueError("message limit must be positive")
    if key == "catalog_url" and not str(value).startswith("https://"):
        raise ValueError("catalog_url must use https")


def _records(values: list[list[Any]], required: Iterable[str]) -> list[dict[str, Any]]:
    if not values:
        raise ValueError("sheet has no header")
    headers = [str(value).strip() for value in values[0]]
    missing = set(required).difference(headers)
    if missing:
        raise ValueError("sheet is missing required columns")
    records = []
    for raw in values[1:]:
        padded = list(raw) + [""] * max(0, len(headers) - len(raw))
        if not any(str(value).strip() for value in padded):
            continue
        records.append(dict(zip(headers, padded)))
    return records


def _ws_update(ws, range_name: str, values: list[list[Any]]) -> None:
    try:
        ws.update(range_name, values, value_input_option="RAW")
    except TypeError:
        ws.update(values, range_name=range_name, value_input_option="RAW")


def _ws_batch_update(
    ws, updates: list[tuple[str, list[list[Any]]]]
) -> None:
    """Apply several ranges with one provider request when supported."""
    if not updates:
        return
    batch = getattr(ws, "batch_update", None)
    if batch is not None:
        batch(
            [
                {"range": range_name, "values": values}
                for range_name, values in updates
            ],
            raw=True,
        )
        return
    # Small fakes/legacy worksheet adapters may not expose batch_update.  The
    # production gspread adapter does; this fallback keeps compatibility only.
    for range_name, values in updates:
        _ws_update(ws, range_name, values)


def _safe_sync_error(exc: Exception) -> str:
    # Never persist a provider exception that may contain credentials or URLs.
    return f"Google Sheets {type(exc).__name__}"[:200]


class BusinessSheets:
    def __init__(self, sheet_id: str, repo, cache_seconds: int | None = None):
        self.sheet_id = sheet_id
        self.repo = repo
        self.book = None
        # Opening a workbook and completing its idempotent bootstrap are separate
        # states.  Keeping this flag separate from ``book`` lets a later cycle
        # resume a partially failed bootstrap instead of assuming it succeeded.
        self._initialized = False
        self.cache_seconds = max(
            1,
            int(cache_seconds if cache_seconds is not None else os.getenv("GOOGLE_TEMPLATE_CACHE_SECONDS", "300")),
        )
        self._content = SheetContent(MappingProxyType({}), (), MappingProxyType({}))
        self._next_refresh_at: datetime | None = None
        # Remote Google calls can take seconds.  Cached Telegram reads use
        # ``_lock`` only for the tiny immutable-snapshot read/swap, while this
        # separate lock serializes refreshes without blocking those readers.
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()

    def initialize(self):
        if self.book is None:
            self.book = google_client().open_by_key(self.sheet_id)
        self.book.batch_update(
            {
                "requests": [
                    {
                        "updateSpreadsheetProperties": {
                            "properties": {"timeZone": "Asia/Tashkent"},
                            "fields": "timeZone",
                        }
                    }
                ]
            }
        )
        existing = {ws.title: ws for ws in self.book.worksheets()}
        first = existing.get("Лист1")
        if first and "Автоответы" not in existing and not first.get_all_values():
            first.update_title("Автоответы")
            existing = {ws.title: ws for ws in self.book.worksheets()}

        validation_requests: list[dict[str, Any]] = []
        for title, required_headers in SHEETS.items():
            ws = existing.get(title)
            if ws is None:
                ws = self.book.add_worksheet(
                    title=title,
                    rows=1000,
                    cols=max(20, len(required_headers)),
                )
                existing[title] = ws
            values = ws.get_all_values()
            headers = [str(item).strip() for item in values[0]] if values else []
            if not headers:
                _ws_update(ws, "A1", [required_headers])
                headers = list(required_headers)
                values = [headers]
            else:
                missing = [header for header in required_headers if header not in headers]
                if missing:
                    start = len(headers) + 1
                    _ws_update(
                        ws,
                        f"{_column_letter(start)}1",
                        [missing],
                    )
                    headers.extend(missing)

            seed = SHEET_SEEDS.get(title)
            if seed:
                # Add only missing defaults.  Existing operator-edited rows are
                # never rewritten, and seed values are aligned even when a
                # pre-existing sheet had partial or reordered headers.
                key_index = headers.index(required_headers[0])
                existing_keys = {
                    str(row[key_index]).strip()
                    for row in values[1:]
                    if key_index < len(row) and str(row[key_index]).strip()
                }
                aligned_seed: list[list[Any]] = []
                for raw_seed in seed:
                    record = dict(zip(required_headers, raw_seed))
                    key = str(record.get(required_headers[0], "")).strip()
                    if not key or key in existing_keys:
                        continue
                    aligned_seed.append([record.get(header, "") for header in headers])
                    existing_keys.add(key)
                if aligned_seed:
                    first_row = max(2, len(values) + 1)
                    last_row = first_row + len(aligned_seed) - 1
                    _ws_update(
                        ws,
                        f"A{first_row}:{_column_letter(len(headers))}{last_row}",
                        aligned_seed,
                    )

            # These operations change layout only, never operator-edited cell data.
            try:
                ws.freeze(rows=1)
            except (AttributeError, TypeError):
                pass
            try:
                ws.set_basic_filter(f"A1:{_column_letter(len(headers))}1000")
            except (AttributeError, TypeError):
                pass
            try:
                ws.format(
                    f"A1:{_column_letter(len(headers))}1",
                    {
                        "backgroundColor": {"red": 0.92, "green": 0.92, "blue": 0.92},
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "CENTER",
                        "wrapStrategy": "WRAP",
                    },
                )
            except (AttributeError, TypeError):
                pass
            sheet_numeric_id = getattr(ws, "id", None)
            if title == "Диалоги" and sheet_numeric_id is not None and "status" in headers:
                status_column = headers.index("status")
                validation_requests.append(
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": int(sheet_numeric_id),
                                "startRowIndex": 1,
                                "endRowIndex": 1000,
                                "startColumnIndex": status_column,
                                "endColumnIndex": status_column + 1,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": value}
                                        for value in (
                                            "new", "bot_answered", "waiting_manager",
                                            "manager_answered", "human_handoff", "closed", "error",
                                        )
                                    ],
                                },
                                "strict": True,
                                "showCustomUi": True,
                            },
                        }
                    }
                )
        if validation_requests:
            self.book.batch_update({"requests": validation_requests})
        self._initialized = True
        return self.book

    def _load_templates(self, old: Mapping[str, TemplateOverride]) -> Mapping[str, TemplateOverride]:
        rows = _records(
            self.book.worksheet("Автоответы").get_all_values(),
            ("code", "enabled", "scope", "priority", "text_ru", "text_uz"),
        )
        loaded: dict[str, TemplateOverride] = {}
        for row in rows:
            code = normalize_template_code(row.get("code", ""))
            if not code:
                continue
            try:
                scope = str(row.get("scope") or "all").strip().lower()
                if scope not in {"all", "night", "day"}:
                    raise ValueError("invalid template scope")
                text_ru = str(row.get("text_ru") or "")
                text_uz = str(row.get("text_uz") or "")
                validate_template_text(text_ru)
                validate_template_text(text_uz)
                loaded[code] = TemplateOverride(
                    code=code,
                    enabled=_truth(row.get("enabled")),
                    scope=scope,
                    priority=_integer(row.get("priority")),
                    text_ru=text_ru,
                    text_uz=text_uz,
                    cooldown_minutes=_integer(row.get("cooldown_minutes")),
                    notes=str(row.get("notes") or ""),
                )
            except (TypeError, ValueError):
                if code in old:
                    loaded[code] = old[code]
                LOG.warning("invalid_sheet_template code=%s using_last_known_or_builtin", code)
        return MappingProxyType(loaded)

    def _load_intents(self, old: tuple[IntentOverride, ...]) -> tuple[IntentOverride, ...]:
        rows = _records(
            self.book.worksheet("Интенты").get_all_values(),
            ("intent_code", "enabled", "scope", "priority", "keywords_ru", "keywords_uz", "negative_keywords", "template_code", "stop_processing"),
        )
        old_by_code = {intent.code: intent for intent in old}
        loaded: list[IntentOverride] = []
        for row in rows:
            code = str(row.get("intent_code") or "").strip()
            if not code:
                continue
            try:
                scope = str(row.get("scope") or "all").strip().lower()
                if scope not in {"all", "night", "day"}:
                    raise ValueError("invalid intent scope")
                loaded.append(
                    IntentOverride(
                        code=code,
                        enabled=_truth(row.get("enabled")),
                        scope=scope,
                        priority=_integer(row.get("priority")),
                        keywords_ru=_keywords(row.get("keywords_ru")),
                        keywords_uz=_keywords(row.get("keywords_uz")),
                        negative_keywords=_keywords(row.get("negative_keywords")),
                        template_code=normalize_template_code(row.get("template_code", "")),
                        stop_processing=_truth(row.get("stop_processing")),
                        notes=str(row.get("notes") or ""),
                    )
                )
            except (TypeError, ValueError):
                if code in old_by_code:
                    loaded.append(old_by_code[code])
                LOG.warning("invalid_sheet_intent code=%s using_last_known", code)
        loaded.sort(key=lambda item: (-item.priority, item.code))
        return tuple(loaded)

    def _load_settings(self, old: Mapping[str, Any]) -> Mapping[str, Any]:
        rows = _records(
            self.book.worksheet("Настройки").get_all_values(),
            ("key", "value", "type"),
        )
        loaded: dict[str, Any] = {}
        for row in rows:
            key = str(row.get("key") or "").strip()
            if not key:
                continue
            try:
                value = _parse_setting(row.get("value"), row.get("type"))
                _validate_setting(key, value)
                loaded[key] = value
            except (TypeError, ValueError, KeyError):
                if key in old:
                    loaded[key] = old[key]
                LOG.warning("invalid_sheet_setting key=%s using_last_known", key)
        return MappingProxyType(loaded)

    def refresh_content(self, now: datetime | None = None, *, force: bool = False) -> SheetContent:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            if not force and self._next_refresh_at is not None and now < self._next_refresh_at:
                return self._content

        # Only one thread performs Google I/O.  Recheck the deadline after
        # waiting because another refresher may already have published a newer
        # snapshot while this caller was queued.
        with self._refresh_lock:
            with self._lock:
                if not force and self._next_refresh_at is not None and now < self._next_refresh_at:
                    return self._content
                previous = self._content

            initialization_token: str | None = None
            initialization_release = None
            try:
                if not self._initialized:
                    acquire = getattr(
                        self.repo, "acquire_sheets_sync_lease", None,
                    )
                    initialization_release = getattr(
                        self.repo, "release_sheets_sync_lease", None,
                    )
                    if acquire is not None:
                        initialization_token = acquire(
                            now, lease_seconds=600,
                        )
                        if not initialization_token:
                            # A different process is initializing or syncing the
                            # same spreadsheet.  Keep serving the current cache
                            # and retry soon without racing its remote writes.
                            with self._lock:
                                self._next_refresh_at = now + timedelta(seconds=5)
                                return self._content
                    self.initialize()
                templates = self._load_templates(previous.templates)
                intents = self._load_intents(previous.intents)
                settings = self._load_settings(previous.settings)
                refreshed = SheetContent(templates, intents, settings, now)
            except Exception as exc:
                # Google is optional at runtime: keep the last complete snapshot.
                LOG.warning(
                    "sheet_content_refresh_failed error_type=%s using_last_known_or_builtin",
                    type(exc).__name__,
                )
                refreshed = previous
            finally:
                if initialization_token and initialization_release is not None:
                    initialization_release(initialization_token)
            effective_cache = refreshed.settings.get("template_cache_seconds", self.cache_seconds)
            try:
                effective_cache = max(1, int(effective_cache))
            except (TypeError, ValueError):
                effective_cache = self.cache_seconds
            with self._lock:
                self._content = refreshed
                self._next_refresh_at = now + timedelta(seconds=effective_cache)
                return self._content

    def render_cached(
        self,
        code: str,
        language: str,
        values: Mapping[str, Any] | None = None,
        **extra_values: Any,
    ) -> str | None:
        """Render from the last immutable snapshot without any Google I/O."""
        with self._lock:
            content = self._content
        substitutions = dict(values or {})
        substitutions.update(extra_values)
        return render_template(
            code,
            language,
            overrides=content.templates,
            settings=content.settings,
            values=substitutions,
        )

    def intents_cached(self) -> tuple[IntentOverride, ...]:
        """Return the last intent snapshot without refreshing it remotely."""
        with self._lock:
            return self._content.intents

    def settings_cached(self) -> Mapping[str, Any]:
        """Return the last settings snapshot without refreshing it remotely."""
        with self._lock:
            return self._content.settings

    def setting_cached(self, key: str, default: Any = None) -> Any:
        return self.settings_cached().get(key, default)

    def render(
        self,
        code: str,
        language: str,
        values: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
        **extra_values: Any,
    ) -> str | None:
        content = self.refresh_content(now)
        substitutions = dict(values or {})
        substitutions.update(extra_values)
        return render_template(
            code,
            language,
            overrides=content.templates,
            settings=content.settings,
            values=substitutions,
        )

    def intents(self, now: datetime | None = None) -> tuple[IntentOverride, ...]:
        return self.refresh_content(now).intents

    def settings(self, now: datetime | None = None) -> Mapping[str, Any]:
        return self.refresh_content(now).settings

    def setting(self, key: str, default: Any = None, now: datetime | None = None) -> Any:
        return self.settings(now).get(key, default)

    def _entity_route(self, entity_type: str) -> tuple[str, tuple[str, ...]]:
        normalized = str(entity_type or "").strip().lower()
        routes = {
            "message": ("Сообщения", ("event_id",)),
            "messages": ("Сообщения", ("event_id",)),
            "business_message": ("Сообщения", ("event_id",)),
            "dialog": ("Диалоги", ("session_id", "cycle_id")),
            "dialogs": ("Диалоги", ("session_id", "cycle_id")),
            "session": ("Диалоги", ("session_id", "cycle_id")),
            "cycle": ("Диалоги", ("session_id", "cycle_id")),
            "error": ("Ошибки", ("error_id",)),
            "business_error": ("Ошибки", ("error_id",)),
            "statistic": ("Статистика", ("period", "metric")),
            "statistics": ("Статистика", ("period", "metric")),
            "stats": ("Статистика", ("period", "metric")),
            "request": ("Заявки", ("request_id",)),
            "requests": ("Заявки", ("request_id",)),
            "business_request": ("Заявки", ("request_id",)),
        }
        if normalized not in routes:
            raise ValueError("unsupported Sheets outbox entity type")
        return routes[normalized]

    @staticmethod
    def _payload_value(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    def _sync_payload_batch(
        self,
        items: list[tuple[str, Mapping[str, Any], str]],
    ) -> None:
        """Upsert one worksheet batch with one read and one write request."""
        if not items:
            return
        if not self._initialized:
            self.initialize()
        routed: list[tuple[Mapping[str, Any], str, tuple[str, ...]]] = []
        title: str | None = None
        for entity_type, payload, operation in items:
            if operation not in {"upsert", "append"}:
                raise ValueError("unsupported non-destructive Sheets operation")
            item_title, key_fields = self._entity_route(entity_type)
            if title is None:
                title = item_title
            elif item_title != title:
                raise ValueError("Sheets batch must target one worksheet")
            routed.append((payload, operation, key_fields))
        if title is None:
            return

        ws = self.book.worksheet(title)
        all_values = ws.get_all_values()
        headers = [str(value).strip() for value in all_values[0]] if all_values else []
        header_changed = not headers
        if not headers:
            headers = list(SHEETS[title])
        missing = [header for header in SHEETS[title] if header not in headers]
        if missing:
            headers.extend(missing)
            header_changed = True

        normalized_rows = [
            [str(value) for value in raw]
            + [""] * max(0, len(headers) - len(raw))
            for raw in all_values[1:]
        ]
        key_indexes: dict[tuple[str, ...], dict[tuple[str, ...], int]] = {}
        updates_by_row: dict[int, list[Any]] = {}
        next_row = max(2, len(all_values) + 1)

        for payload, operation, key_fields in routed:
            row_values = [
                self._payload_value(payload.get(header, ""))
                for header in headers
            ]
            target_row: int | None = None
            expected_key: tuple[str, ...] | None = None
            if operation == "upsert":
                expected_key = tuple(
                    str(payload.get(field, "")) for field in key_fields
                )
                if not any(expected_key) or (
                    title == "Статистика" and not all(expected_key)
                ):
                    raise ValueError("Sheets upsert payload has no stable key")
                index = key_indexes.get(key_fields)
                if index is None:
                    positions = tuple(headers.index(field) for field in key_fields)
                    index = {}
                    for row_number, raw in enumerate(normalized_rows, 2):
                        actual = tuple(
                            raw[position] if position < len(raw) else ""
                            for position in positions
                        )
                        if actual not in index:
                            index[actual] = row_number
                    key_indexes[key_fields] = index
                target_row = index.get(expected_key)

            is_new_row = target_row is None
            if is_new_row:
                target_row = next_row
                next_row += 1
                normalized_rows.append([str(value) for value in row_values])
                if expected_key is not None:
                    key_indexes[key_fields][expected_key] = target_row
            current_index = target_row - 2
            current = (
                normalized_rows[current_index]
                if 0 <= current_index < len(normalized_rows)
                else None
            )
            desired = [str(value) for value in row_values]
            if is_new_row or current != desired:
                updates_by_row[target_row] = row_values
                if current is not None:
                    normalized_rows[current_index] = desired

        updates: list[tuple[str, list[list[Any]]]] = []
        if header_changed:
            updates.append(
                (f"A1:{_column_letter(len(headers))}1", [headers])
            )

        # Consecutive rows share one range. Statistics normally become one
        # 76-row update rather than 76 reads plus 76 writes.
        ordered = sorted(updates_by_row.items())
        position = 0
        while position < len(ordered):
            first_row, first_values = ordered[position]
            block = [first_values]
            last_row = first_row
            position += 1
            while position < len(ordered) and ordered[position][0] == last_row + 1:
                last_row, values = ordered[position]
                block.append(values)
                position += 1
            updates.append(
                (
                    f"A{first_row}:{_column_letter(len(headers))}{last_row}",
                    block,
                )
            )
        _ws_batch_update(ws, updates)

    def sync_payload(
        self,
        entity_type: str,
        payload: Mapping[str, Any],
        operation: str = "upsert",
    ) -> None:
        self._sync_payload_batch([(entity_type, payload, operation)])

    def sync_once(self, now: datetime, *, clock=None, max_rows: int = 100) -> None:
        # Refresh/bootstrap before leasing an outbox row.  A slow bootstrap must
        # never consume the lease intended to protect the actual entity write.
        self.refresh_content(now)
        if not self._initialized:
            return

        claim_now = clock() if clock is not None else now
        acquire = getattr(self.repo, "acquire_sheets_sync_lease", None)
        release = getattr(self.repo, "release_sheets_sync_lease", None)
        sync_token: str | None = None
        if acquire is not None:
            sync_token = acquire(claim_now, lease_seconds=600)
            if not sync_token:
                # Another process owns the Google read/modify/write section.
                # Its row leases remain authoritative; this process will retry
                # on the next normal scheduler interval.
                return
        try:
            self._sync_outbox_locked(
                claim_now, clock=clock, max_rows=max_rows,
            )
        finally:
            if sync_token and release is not None:
                release(sync_token)

    def _sync_outbox_locked(
        self, claim_now: datetime, *, clock=None, max_rows: int = 100,
    ) -> None:
        try:
            rows = self.repo.outbox_due(
                claim_now,
                limit=max(1, int(max_rows)),
                lease_seconds=300,
            )
        except TypeError:
            # Compatibility for an initial-rollout repository/fake.
            rows = self.repo.outbox_due(claim_now)
        if not rows:
            return

        groups: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict):
                    raise ValueError("Sheets outbox payload must be an object")
                title, _ = self._entity_route(row["entity_type"])
                groups.setdefault(title, []).append((row, payload))
            except Exception as exc:
                self._retry_outbox_row(row, claim_now, exc)

        for title, batch_rows in groups.items():
            try:
                self._sync_payload_batch(
                    [
                        (row["entity_type"], payload, row["operation"])
                        for row, payload in batch_rows
                    ]
                )
            except Exception as exc:
                for row, _ in batch_rows:
                    self._retry_outbox_row(row, claim_now, exc)
                continue

            for row, _ in batch_rows:
                keys = set(row.keys()) if hasattr(row, "keys") else set()
                lease_token = row["lease_token"] if "lease_token" in keys else None
                generation = row["generation"] if "generation" in keys else None
                finished_at = clock() if clock is not None else claim_now
                if lease_token is None and generation is None:
                    self.repo.outbox_done(row["id"], finished_at)
                else:
                    self.repo.outbox_done(
                        row["id"], finished_at,
                        lease_token=lease_token,
                        generation=generation,
                    )
                LOG.info(
                    "google_sheet_sync_ok entity_type=%s operation=%s",
                    row["entity_type"],
                    row["operation"],
                )

    def _retry_outbox_row(self, row, now: datetime, exc: Exception) -> None:
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        lease_token = row["lease_token"] if "lease_token" in keys else None
        generation = row["generation"] if "generation" in keys else None
        # New repositories increment attempts atomically while claiming; retain
        # the legacy +1 behavior only for an unfenced fake/old repository.
        attempts = int(row["attempts"]) if lease_token is not None else int(row["attempts"]) + 1
        if lease_token is None and generation is None:
            self.repo.outbox_retry(
                row["id"], now, attempts, _safe_sync_error(exc),
            )
        else:
            self.repo.outbox_retry(
                row["id"], now, attempts, _safe_sync_error(exc),
                lease_token=lease_token,
                generation=generation,
            )
        LOG.warning(
            "google_sheet_sync_retry entity_type=%s operation=%s attempt=%s error_type=%s",
            row["entity_type"],
            row["operation"],
            attempts,
            type(exc).__name__,
        )
