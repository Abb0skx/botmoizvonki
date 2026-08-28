from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from urllib.parse import quote_plus

from .intents import extract_text_location, is_outside_tashkent
from .products import safe_product_url


_PHONE_ALLOWED = re.compile(r"^[+()\d\s.\-/]{7,32}$")
_ADDRESS_TEXT = re.compile(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", re.UNICODE)
_ADDRESS_HINT = re.compile(
    r"\b(?:улица|ул|дом|квартал|массив|район|махалля|подъезд|"
    r"ko['’]?cha|uy|mavze|daha|mahalla|tuman|"
    r"чиланзар|chilonzor|юнусабад|yunusobod|мирзо\s+улугбек|"
    r"сергели|sergeli|яккасарай|yakkasaroy|шайхантахур|shayxontohur|"
    r"алмазар|olmazor|учтепа|uchtepa|бек-темир|bektemir|мирабад|mirobod|"
    r"самарканд|samarqand|чирчик|chirchiq|коканд|qo['’]?qon|"
    r"ургенч|urganch|гулистан|guliston|амира\s+темура)\b",
    re.IGNORECASE,
)
_EXPLICIT_UZ_PHONE = re.compile(
    r"(?<!\d)(\+998[\s().\-/]*\d{2}[\s().\-/]*\d{3}"
    r"[\s().\-/]*\d{2}[\s().\-/]*\d{2})(?!\d)"
)
_PHONE_MARKER = re.compile(
    r"(?:телефон|тел\.?|номер|phone|contact|aloqa|telefon|tel\.?|raqam|рақам)"
    r"\s*[:\-]?\s*([+()\d\s.\-/]{7,32})",
    re.IGNORECASE,
)


def _value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(row, key, default)
    return default if value is None else value


def normalize_phone(value: Any) -> str | None:
    """Normalize a deliberately typed/shared phone without guessing cards.

    Uzbek local and +998 formats are accepted. Other international numbers are
    accepted only with an explicit leading ``+``. A plain 13–19 digit value is
    never treated as a phone because it can be a payment credential.
    """

    raw = " ".join(str(value or "").strip().split())
    if not raw or "PAYMENT_DATA_REDACTED" in raw or not _PHONE_ALLOWED.fullmatch(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:
        return "+998" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return "+998" + digits[1:]
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def masked_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    country = "+998 " if digits.startswith("998") and len(digits) == 12 else "+"
    return f"{country}** *** ** {digits[-2:]}"


def phone_from_message(message: Mapping[str, Any] | None, text: str = "") -> tuple[str | None, str | None]:
    message = message or {}
    contact = message.get("contact") if isinstance(message, Mapping) else None
    if isinstance(contact, Mapping):
        phone = normalize_phone(contact.get("phone_number"))
        if phone:
            return phone, "telegram_contact"
    phone = normalize_phone(text)
    if phone:
        return phone, "typed"
    # Accept a phone alongside a model/address only when it is either an
    # explicit +998 number or follows an unambiguous phone label. Never scan
    # arbitrary digit runs, which could be card or account data.
    compact = " ".join(str(text or "").split())[:500]
    explicit = _EXPLICIT_UZ_PHONE.search(compact)
    if explicit and (phone := normalize_phone(explicit.group(1))):
        return phone, "typed"
    marked = _PHONE_MARKER.search(compact)
    if marked and (phone := normalize_phone(marked.group(1).strip())):
        return phone, "typed"
    return None, None


@dataclass(frozen=True)
class RequestLocation:
    url: str
    address: str | None = None
    outside_tashkent: bool = False
    source: str = "text"


def location_from_message(
    message: Mapping[str, Any] | None,
    text: str = "",
    *,
    expected: bool = False,
) -> RequestLocation | None:
    """Parse a location without opening a customer-supplied URL."""

    message = message or {}
    location = message.get("location") if isinstance(message, Mapping) else None
    if isinstance(location, Mapping):
        try:
            latitude = float(location.get("latitude"))
            longitude = float(location.get("longitude"))
        except (TypeError, ValueError):
            latitude = longitude = float("nan")
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return RequestLocation(
                url=f"https://www.google.com/maps?q={latitude:.6f},{longitude:.6f}",
                outside_tashkent=is_outside_tashkent(
                    latitude=latitude, longitude=longitude,
                ),
                source="telegram_location",
            )

    compact = " ".join(str(text or "").strip().split())[:500]
    if safe_url := extract_text_location(compact):
        address = compact if "maps/search/?api=1&query=" in safe_url else None
        return RequestLocation(
            url=safe_url,
            address=address,
            outside_tashkent=is_outside_tashkent(compact),
            source="map_or_address",
        )
    if not expected:
        return None
    # During the explicit address step a short answer such as "Чиланзар 10"
    # is unambiguous. Unknown links and number-only strings remain rejected.
    if (
        3 <= len(compact) <= 500
        and _ADDRESS_TEXT.search(compact)
        and (re.search(r"\d", compact) or _ADDRESS_HINT.search(compact))
        and not re.search(r"https?://", compact, re.IGNORECASE)
        and normalize_phone(compact) is None
        and "PAYMENT_DATA_REDACTED" not in compact
    ):
        return RequestLocation(
            url="https://www.google.com/maps/search/?api=1&query=" + quote_plus(compact),
            address=compact,
            outside_tashkent=is_outside_tashkent(compact),
            source="address",
        )
    return None


def selection_fields(request: Mapping[str, Any] | Any) -> dict[str, Any]:
    raw = _value(request, "selection_fields", "{}")
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def format_price(value: Any, language: str) -> str:
    try:
        amount = Decimal(str(value or ""))
    except (InvalidOperation, ValueError):
        return ""
    if not amount.is_finite() or amount <= 0:
        return ""
    quantum = Decimal("1000") if amount > Decimal("10000") else Decimal("1")
    rounded = int((amount / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum)
    rendered = f"{rounded:,}".replace(",", " ")
    return f"{rendered} so'm"


def missing_request_fields(request: Mapping[str, Any] | Any) -> tuple[str, ...]:
    fields = selection_fields(request)
    missing: list[str] = []
    items = [item for item in fields.get("items", []) if isinstance(item, dict)]
    if not items and not str(_value(request, "exact_model", "")).strip():
        missing.append("model")
    if not items and fields.get("attribute_required") and not str(_value(request, "option_value", "")).strip():
        missing.append(str(_value(request, "option_kind", "attribute") or "attribute"))
    if not items and fields.get("color_required") and not (
        str(_value(request, "color", "")).strip()
        or bool(_value(request, "color_any", 0))
    ):
        missing.append("color")
    fulfillment = str(_value(request, "fulfillment_method", "") or "")
    if fulfillment not in {"delivery", "pickup"}:
        missing.append("fulfillment")
    elif fulfillment == "delivery":
        if not str(_value(request, "phone", "")).strip():
            missing.append("phone")
        if not (
            str(_value(request, "location_url", "")).strip()
            or str(_value(request, "address", "")).strip()
        ):
            missing.append("location")
    return tuple(missing)


def localized_missing(fields: tuple[str, ...], language: str) -> str:
    labels = {
        "model": ("модель", "model"),
        "memory": ("память", "xotira"),
        "size": ("размер", "o‘lcham"),
        "attribute": ("вариант", "variant"),
        "color": ("цвет", "rang"),
        "fulfillment": ("доставка или самовывоз", "yetkazib berish yoki olib ketish"),
        "phone": ("телефон", "telefon"),
        "location": ("адрес доставки", "yetkazib berish manzili"),
    }
    index = 1 if language == "uz" else 0
    return ", ".join(labels.get(field, (field, field))[index] for field in fields)


def request_summary(request: Mapping[str, Any] | Any, language: str) -> str:
    language = "uz" if language == "uz" else "ru"
    model = html.escape(str(_value(request, "exact_model", "") or "")[:300])
    model_url = safe_product_url(_value(request, "model_url", ""))
    if model_url and model:
        model = f'<a href="{html.escape(model_url, quote=True)}">{model}</a>'
    option_kind = str(_value(request, "option_kind", "") or "")
    option_value = html.escape(str(_value(request, "option_value", "") or "")[:160])
    color = str(_value(request, "color", "") or "")
    if bool(_value(request, "color_any", 0)):
        color = "Не важен" if language == "ru" else "Muhim emas"
    fulfillment = str(_value(request, "fulfillment_method", "") or "")
    price = format_price(_value(request, "database_price", ""), language)
    phone = masked_phone(_value(request, "phone", ""))
    location = str(_value(request, "address", "") or _value(request, "location_url", "") or "")[:500]
    preferred_time = str(_value(request, "preferred_time", "") or "")[:100]

    if language == "uz":
        lines = [f"Model: {model or 'Ko‘rsatilmagan'}"]
        if option_kind:
            label = "O‘lcham" if option_kind == "size" else "Xotira"
            lines.append(f"{label}: {option_value or 'Ko‘rsatilmagan'}")
        if selection_fields(request).get("color_required"):
            lines.append(f"Rang: {html.escape(color) if color else 'Ko‘rsatilmagan'}")
        lines.append(
            "Olish usuli: "
            + ({"delivery": "Yetkazib berish", "pickup": "Do‘kondan olib ketish"}.get(fulfillment, "Ko‘rsatilmagan"))
        )
        lines.append(f"Aloqa: {phone or 'Telegram'}")
        if fulfillment == "delivery":
            lines.append(f"Manzil: {html.escape(location) if location else 'Ko‘rsatilmagan'}")
        if preferred_time:
            lines.append(f"Qulay vaqt: {html.escape(preferred_time)}")
        lines.append(f"Bazadagi narx: {price or 'Menejer tekshiradi'}")
        return "\n".join(lines)

    lines = [f"Модель: {model or 'Не указана'}"]
    if option_kind:
        label = "Размер" if option_kind == "size" else "Память"
        lines.append(f"{label}: {option_value or 'Не указан'}")
    if selection_fields(request).get("color_required"):
        lines.append(f"Цвет: {html.escape(color) if color else 'Не указан'}")
    lines.append(
        "Получение: "
        + ({"delivery": "Доставка", "pickup": "Самовывоз"}.get(fulfillment, "Не указано"))
    )
    lines.append(f"Связь: {phone or 'Telegram'}")
    if fulfillment == "delivery":
        lines.append(f"Адрес: {html.escape(location) if location else 'Не указан'}")
    if preferred_time:
        lines.append(f"Удобное время: {html.escape(preferred_time)}")
    lines.append(f"Цена в базе: {price or 'Проверит менеджер'}")
    return "\n".join(lines)
