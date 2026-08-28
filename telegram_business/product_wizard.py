from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from .products import ProductMatch, ProductVariant, normalize_model, safe_product_url


Language = Literal["ru", "uz", "bi"]
StepCode = Literal[
    "start",
    "model",
    "price_result",
    "memory",
    "size",
    "color",
    "item_ready",
    "cart",
    "fulfillment",
    "delivery_phone",
    "pickup_contact",
    "delivery_location",
    "review",
    "edit",
]
AttributeKind = Literal["memory", "size"]
FulfillmentMethod = Literal["delivery", "pickup"]

_SIZE_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?\s*mm\b", re.IGNORECASE)
_MEMORY_RE = re.compile(
    r"(?<!\w)(?:\d+\s*/\s*)?\d+(?:[.,]\d+)?\s*(?:gb|tb)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WizardChoice:
    """One catalogue-backed value addressable by a short, stable callback id."""

    choice_id: str
    value: str
    label: str
    ordinal: int
    url: str | None = None


@dataclass(frozen=True)
class ButtonSpec:
    """Telegram-independent inline button description.

    URL buttons have ``url`` and no action. Callback buttons have ``action``
    and may refer to a catalogue choice through ``choice_id``. The integration
    layer remains responsible for adding chat/draft/version information to
    callback_data.
    """

    text: str
    action: str | None = None
    choice_id: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class WizardStep:
    code: StepCode
    text: str
    choices: tuple[WizardChoice, ...]
    keyboard: tuple[tuple[ButtonSpec, ...], ...]
    parse_mode: str = "HTML"

    def choice(self, choice_id: str) -> WizardChoice:
        for item in self.choices:
            if item.choice_id == choice_id:
                return item
        raise KeyError(choice_id)

    def inline_keyboard(
        self,
        encode_callback: Callable[[str, str | None], str],
    ) -> dict:
        """Materialize an InlineKeyboardMarkup dict with bounded callbacks."""

        rows: list[list[dict[str, str]]] = []
        for row in self.keyboard:
            rendered: list[dict[str, str]] = []
            for button in row:
                if button.url:
                    rendered.append({"text": button.text, "url": button.url})
                    continue
                if not button.action:
                    raise ValueError("callback button has no action")
                callback_data = encode_callback(button.action, button.choice_id)
                if not callback_data or len(callback_data.encode("utf-8")) > 64:
                    raise ValueError("callback_data must contain 1-64 UTF-8 bytes")
                rendered.append(
                    {"text": button.text, "callback_data": callback_data}
                )
            if rendered:
                rows.append(rendered)
        return {"inline_keyboard": rows}


@dataclass(frozen=True)
class WizardReviewData:
    """Catalogue-backed customer preferences ready for a safe review.

    ``phone`` and ``location`` are mandatory only for delivery. Pickup uses
    the current Telegram chat when no optional phone is supplied. A missing
    color means the catalogue had no color choice; ``any_color`` records an
    explicit customer preference and is never treated as a stock variant.
    """

    model: str
    fulfillment: FulfillmentMethod
    model_url: str | None = None
    attribute_kind: AttributeKind | None = None
    attribute_value: str | None = None
    color: str | None = None
    any_color: bool = False
    phone: str | None = None
    location: str | None = None


def _language(value: str) -> Language:
    return value if value in {"ru", "uz", "bi"} else "bi"  # type: ignore[return-value]


def _localized(language: str, ru: str, uz: str) -> str:
    selected = _language(language)
    if selected == "ru":
        return ru
    if selected == "uz":
        return uz
    return f"{ru}\n{uz}"


def _button_text(language: str, ru: str, uz: str) -> str:
    selected = _language(language)
    value = ru if selected == "ru" else uz if selected == "uz" else f"{ru} / {uz}"
    return _bounded_label(value, 64)


def _bounded_label(value: str, limit: int = 64) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def _bounded_model_label(value: str, limit: int = 52) -> str:
    """Keep both the beginning and distinguishing suffix of a model name."""

    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    tail = min(16, max(8, limit // 3))
    head = max(1, limit - tail - 1)
    return compact[:head].rstrip() + "…" + compact[-tail:].lstrip()


def _model_button_labels(choices: Sequence[WizardChoice]) -> tuple[str, ...]:
    """Prefer the distinctive suffix when all choices share a long family."""

    tokenized = [choice.label.split() for choice in choices]
    prefix = 0
    if tokenized:
        shortest = min(len(tokens) for tokens in tokenized)
        while prefix < shortest and len(
            {tokens[prefix].casefold() for tokens in tokenized}
        ) == 1:
            prefix += 1
    strip_prefix = prefix >= 2 and all(len(tokens) > prefix for tokens in tokenized)
    labels = [
        " ".join(tokens[prefix:] if strip_prefix else tokens)
        for tokens in tokenized
    ]
    # Never collapse two different catalogue choices into the same label.
    if len({label.casefold() for label in labels}) != len(labels):
        labels = [choice.label for choice in choices]
    return tuple(_bounded_model_label(label) for label in labels)


def _escaped_value(value: object, limit: int = 500) -> str:
    """Escape a single summary value without cutting an HTML entity."""

    compact = " ".join(str(value or "").split())
    rendered: list[str] = []
    length = 0
    truncated = False
    for char in compact:
        escaped = html.escape(char, quote=True)
        if length + len(escaped) > max(1, limit - 1):
            truncated = True
            break
        rendered.append(escaped)
        length += len(escaped)
    if truncated:
        rendered.append("…")
    return "".join(rendered)


def _text_present(value: object) -> bool:
    return bool(str(value or "").strip())


def _navigation_row(language: str) -> tuple[ButtonSpec, ...]:
    return (
        ButtonSpec(
            text=_button_text(language, "Назад", "Orqaga"),
            action="back",
        ),
        ButtonSpec(
            text=_button_text(language, "Отменить", "Bekor qilish"),
            action="cancel",
        ),
    )


def build_start_step(language: str = "bi") -> WizardStep:
    ru = (
        "Я авто-помощник TEXNIKACH. Ночью могу показать цены или собрать "
        "заявку для менеджера. Цена и наличие подтверждаются менеджером."
    )
    uz = (
        "Men TEXNIKACH avto-yordamchisiman. Kechasi narxlarni ko‘rsataman "
        "yoki menejer uchun so‘rov yig‘aman. Narx va mavjudlikni menejer tasdiqlaydi."
    )
    return WizardStep(
        "start",
        _localized(language, ru, uz),
        (),
        (
            (
                ButtonSpec(
                    _button_text(language, "🔎 Найти модель", "🔎 Model topish"),
                    action="browse_prices",
                ),
            ),
            (
                ButtonSpec(
                    _button_text(language, "🛒 Оставить заявку", "🛒 So‘rov qoldirish"),
                    action="start_order",
                ),
            ),
        ),
    )


def build_price_actions_step(text: str, language: str = "bi") -> WizardStep:
    return WizardStep(
        "price_result",
        text,
        (),
        (
            (
                ButtonSpec(
                    _button_text(language, "🛒 Заказать", "🛒 Buyurtma berish"),
                    action="start_item_order",
                ),
            ),
            (
                ButtonSpec(
                    _button_text(language, "🔎 Найти модель", "🔎 Model topish"),
                    action="find_model",
                ),
            ),
        ),
    )


def build_item_ready_step(
    model: str,
    language: str = "bi",
    *,
    attribute_kind: str | None = None,
    attribute_value: str | None = None,
    color: str | None = None,
    any_color: bool = False,
) -> WizardStep:
    safe_model = _escaped_value(model, 240)
    ru_lines = [f"<b>{safe_model}</b>"]
    uz_lines = [f"<b>{safe_model}</b>"]
    if attribute_value:
        ru_label = "Размер" if attribute_kind == "size" else "Память"
        uz_label = "O‘lcham" if attribute_kind == "size" else "Xotira"
        safe_value = _escaped_value(attribute_value, 100)
        ru_lines.append(f"{ru_label}: {safe_value}")
        uz_lines.append(f"{uz_label}: {safe_value}")
    if color or any_color:
        ru_lines.append(f"Цвет: {_escaped_value(color, 100) if color else 'не важен'}")
        uz_lines.append(f"Rang: {_escaped_value(color, 100) if color else 'farqi yo‘q'}")
    return WizardStep(
        "item_ready",
        _localized(language, "\n".join(ru_lines), "\n".join(uz_lines)),
        (),
        (
            (
                ButtonSpec(
                    _button_text(language, "➕ Добавить в заявку", "➕ So‘rovga qo‘shish"),
                    action="add_item",
                ),
            ),
            _navigation_row(language),
        ),
    )


def build_cart_step(items: Sequence[dict], language: str = "bi") -> WizardStep:
    def lines(selected: str) -> str:
        result = []
        for index, item in enumerate(items, 1):
            parts = [str(item.get("model") or "")]
            if item.get("option_value"):
                parts.append(str(item["option_value"]))
            if item.get("color_any"):
                parts.append("цвет не важен" if selected == "ru" else "rang farqi yo‘q")
            elif item.get("color"):
                parts.append(str(item["color"]))
            result.append(f"{index}. {_escaped_value(', '.join(parts), 350)}")
        title = "В заявке:" if selected == "ru" else "So‘rovda:"
        return title + "\n\n" + "\n".join(result)

    rows: list[tuple[ButtonSpec, ...]] = [
        (
            ButtonSpec(
                _button_text(language, "➕ Добавить модель", "➕ Model qo‘shish"),
                action="add_more",
            ),
        ),
        (
            ButtonSpec(
                _button_text(language, "Продолжить", "Davom etish"),
                action="continue_order",
            ),
        ),
    ]
    if items:
        rows.append(
            (
                ButtonSpec(
                    _button_text(language, "Удалить последнюю", "Oxirgisini o‘chirish"),
                    action="remove_last_item",
                ),
            )
        )
    rows.append(_navigation_row(language))
    return WizardStep(
        "cart",
        _localized(language, lines("ru"), lines("uz")),
        (),
        tuple(rows),
    )


def _choice_id(kind: StepCode, value: str) -> str:
    normalized = normalize_model(value) or " ".join(value.casefold().split())
    digest = hashlib.sha256(f"{kind}\0{normalized}".encode("utf-8")).hexdigest()
    return digest[:16]


def _value_key(value: str) -> str:
    return normalize_model(value) or " ".join(str(value).casefold().split())


def _distinct_values(
    variants: Sequence[ProductVariant],
    field: Literal["memory", "color"],
) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for variant in variants:
        value = " ".join(str(getattr(variant, field, "") or "").split())
        if not value:
            continue
        key = _value_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def detect_attribute_kind(
    variants: Sequence[ProductVariant],
) -> AttributeKind | None:
    """Classify only values whose meaning is explicit in the source.

    In the real ``bot_prices`` sheet the ``memory`` column carries watch and
    glasses dimensions such as ``41mm`` and ``L (53mm)``. Bare or unrelated
    values are not guessed: the attribute step is skipped instead.
    """

    values = _distinct_values(variants, "memory")
    if not values:
        return None
    if all(_SIZE_RE.search(value) for value in values):
        return "size"
    if all(_MEMORY_RE.search(value) for value in values):
        return "memory"
    return None


def _choices(kind: StepCode, values: Sequence[str]) -> tuple[WizardChoice, ...]:
    return tuple(
        WizardChoice(
            choice_id=_choice_id(kind, value),
            value=value,
            label=value,
            ordinal=index,
        )
        for index, value in enumerate(values, 1)
    )


def _packed_choice_rows(
    choices: Sequence[WizardChoice],
    action: str,
) -> list[tuple[ButtonSpec, ...]]:
    buttons = [
        ButtonSpec(
            text=_bounded_label(choice.label),
            action=action,
            choice_id=choice.choice_id,
        )
        for choice in choices
    ]
    # Short storage/size/color labels are comfortable two per row. Long
    # catalogue labels stay one per row and are never dropped.
    columns = 2 if buttons and all(len(button.text) <= 24 for button in buttons) else 1
    return [tuple(buttons[index : index + columns]) for index in range(0, len(buttons), columns)]


def build_model_step(match: ProductMatch, language: str = "bi") -> WizardStep | None:
    """Build a maximum-five model disambiguation step from a ProductMatch."""

    seen: set[str] = set()
    model_names: list[str] = []
    for raw_model in match.models:
        model = " ".join(str(raw_model or "").split())
        key = _value_key(model)
        if not model or not key or key in seen:
            continue
        seen.add(key)
        model_names.append(model)
        if len(model_names) == 5:
            break
    if not model_names:
        return None

    trusted_urls = {
        name: trusted
        for name, raw_url in match.model_urls
        if (trusted := safe_product_url(raw_url))
    }
    choices = tuple(
        WizardChoice(
            choice_id=_choice_id("model", model),
            value=model,
            label=model,
            ordinal=index,
            url=trusted_urls.get(model),
        )
        for index, model in enumerate(model_names, 1)
    )

    lines: list[str] = []
    for choice in choices:
        safe_name = html.escape(choice.label[:300])
        if choice.url:
            safe_name = (
                f'<a href="{html.escape(choice.url, quote=True)}">{safe_name}</a>'
            )
        lines.append(f"{choice.ordinal}. {safe_name}")
    ru = "Выберите модель:\n\n" + "\n".join(lines)
    uz = "Modelni tanlang:\n\n" + "\n".join(lines)

    rows: list[tuple[ButtonSpec, ...]] = []
    for choice, label in zip(choices, _model_button_labels(choices)):
        rows.append(
            (
                ButtonSpec(
                    text=_bounded_label(f"{choice.ordinal}. {label}", 56),
                    action="select_model",
                    choice_id=choice.choice_id,
                ),
            )
        )
    rows.append(
        (
            ButtonSpec(
                text=_button_text(language, "Другая модель", "Boshqa model"),
                action="enter_model",
            ),
        )
    )
    return WizardStep(
        code="model",
        text=_localized(language, ru, uz),
        choices=choices,
        keyboard=tuple(rows),
    )


def build_grouped_model_step(
    match: ProductMatch,
    language: str = "bi",
) -> WizardStep | None:
    """Expose real technical model names hidden under one search family.

    The approved catalogue groups suffixes such as SIM/eSIM and
    Lightning/USB-C for search. They are still distinct customer choices and
    must be narrowed before memory/color rather than silently combined.
    """

    variants = tuple(match.all_variants or match.variants)
    seen: set[str] = set()
    names: list[str] = []
    urls: list[tuple[str, str]] = []
    for variant in variants:
        name = " ".join(str(variant.model or "").split())
        key = _value_key(name)
        if not name or not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if trusted := safe_product_url(variant.url):
            urls.append((name, trusted))
    if len(names) <= 1:
        return None
    return build_model_step(
        ProductMatch(
            status="ambiguous",
            models=tuple(names),
            model_urls=tuple(urls),
        ),
        language,
    )


def _single_model(variants: Sequence[ProductVariant]) -> str:
    models = {
        _value_key(variant.model): " ".join(str(variant.model or "").split())
        for variant in variants
        if str(variant.model or "").strip()
    }
    if len(models) != 1:
        raise ValueError("variant step requires exactly one model")
    return next(iter(models.values()))


def build_attribute_step(
    variants: Sequence[ProductVariant],
    language: str = "bi",
    *,
    model_name: str | None = None,
) -> WizardStep | None:
    """Build the real memory or mm-size step, or return None to skip it."""

    kind = detect_attribute_kind(variants)
    if kind is None:
        return None
    if not str(model_name or "").strip():
        _single_model(variants)
    values = _distinct_values(variants, "memory")
    choices = _choices(kind, values)
    if kind == "size":
        ru = "Выберите размер:"
        uz = "O‘lchamni tanlang:"
        action = "select_size"
    else:
        ru = "Выберите память:"
        uz = "Xotirani tanlang:"
        action = "select_memory"
    rows = _packed_choice_rows(choices, action)
    rows.append(
        (
            ButtonSpec(
                text=_button_text(language, "Назад", "Orqaga"),
                action="back",
            ),
        )
    )
    return WizardStep(
        code=kind,
        text=_localized(language, ru, uz),
        choices=choices,
        keyboard=tuple(rows),
    )


def build_color_step(
    variants: Sequence[ProductVariant],
    language: str = "bi",
    *,
    model_name: str | None = None,
) -> WizardStep | None:
    """Build choices only from non-empty catalogue colors.

    ``any_color`` is a customer preference, not a fabricated catalogue row;
    choosing it must keep all variants in the integration layer.
    """

    values = _distinct_values(variants, "color")
    if not values:
        return None
    if not str(model_name or "").strip():
        _single_model(variants)
    choices = _choices("color", values)
    ru = "Выберите цвет:"
    uz = "Rangni tanlang:"
    rows = _packed_choice_rows(choices, "select_color")
    rows.append(
        (
            ButtonSpec(
                text=_button_text(language, "Цвет не важен", "Rang farqi yo‘q"),
                action="any_color",
            ),
            ButtonSpec(
                text=_button_text(language, "Назад", "Orqaga"),
                action="back",
            ),
        )
    )
    return WizardStep(
        code="color",
        text=_localized(language, ru, uz),
        choices=choices,
        keyboard=tuple(rows),
    )


def build_first_product_step(
    match: ProductMatch,
    language: str = "bi",
) -> WizardStep | None:
    """Choose model disambiguation, attribute, color, or no product step."""

    if match.status == "ambiguous":
        return build_model_step(match, language)
    if match.status != "found" or not match.variants:
        return None
    grouped_model_step = build_grouped_model_step(match, language)
    if grouped_model_step is not None:
        return grouped_model_step
    model_name = match.models[0] if match.models else None
    return (
        build_attribute_step(match.variants, language, model_name=model_name)
        or build_color_step(match.variants, language, model_name=model_name)
    )


def build_fulfillment_step(language: str = "bi") -> WizardStep:
    """Ask for a delivery preference without promising fulfillment."""

    ru = "Доставка или самовывоз?"
    uz = "Yetkazib berish yoki olib ketish?"
    keyboard = (
        (
            ButtonSpec(
                text=_button_text(language, "🚚 Доставка", "🚚 Yetkazib berish"),
                action="select_delivery",
            ),
        ),
        (
            ButtonSpec(
                text=_button_text(language, "🏪 Самовывоз", "🏪 Do‘kondan olib ketish"),
                action="select_pickup",
            ),
        ),
        _navigation_row(language),
    )
    return WizardStep(
        code="fulfillment",
        text=_localized(language, ru, uz),
        choices=(),
        keyboard=keyboard,
    )


def build_delivery_phone_step(language: str = "bi") -> WizardStep:
    """Request a delivery phone through text or a manually sent Contact."""

    ru = "Отправьте номер или контакт для доставки."
    uz = "Yetkazib berish uchun raqam yoki kontaktni yuboring."
    return WizardStep(
        code="delivery_phone",
        text=_localized(language, ru, uz),
        choices=(),
        keyboard=(_navigation_row(language),),
    )


def build_pickup_contact_step(
    language: str = "bi",
    *,
    has_saved_phone: bool = False,
) -> WizardStep:
    """Let pickup customers keep Telegram contact or add an optional phone."""

    ru = "Для самовывоза телефон необязателен."
    uz = "Olib ketish uchun telefon raqami shart emas."
    rows: list[tuple[ButtonSpec, ...]] = [
        (
            ButtonSpec(
                text=_button_text(
                    language,
                    "Оставить Telegram",
                    "Telegram orqali bog‘lanish",
                ),
                action="use_telegram_contact",
            ),
        ),
    ]
    if has_saved_phone:
        rows.append(
            (
                ButtonSpec(
                    text=_button_text(
                        language,
                        "Использовать сохранённый номер",
                        "Saqlangan raqamdan foydalanish",
                    ),
                    action="use_saved_phone",
                ),
            )
        )
    rows.extend(
        [
            (
            ButtonSpec(
                text=_button_text(language, "Добавить телефон", "Telefon qo‘shish"),
                action="add_phone",
            ),
        ),
        _navigation_row(language),
        ]
    )
    return WizardStep(
        code="pickup_contact",
        text=_localized(language, ru, uz),
        choices=(),
        keyboard=tuple(rows),
    )


def build_delivery_location_step(language: str = "bi") -> WizardStep:
    """Request a delivery location without using unsupported reply keyboards."""

    ru = "Отправьте геолокацию, ссылку на карту или адрес."
    uz = "Geolokatsiya, xarita havolasi yoki manzilni yuboring."
    return WizardStep(
        code="delivery_location",
        text=_localized(language, ru, uz),
        choices=(),
        keyboard=(_navigation_row(language),),
    )


def _validate_review_data(
    data: WizardReviewData,
    *,
    require_complete: bool,
) -> None:
    if not str(data.model or "").strip():
        raise ValueError("review requires a model")
    if data.fulfillment not in {"delivery", "pickup"}:
        raise ValueError("unsupported fulfillment method")
    if data.attribute_kind not in {None, "memory", "size"}:
        raise ValueError("unsupported attribute kind")
    if bool(data.attribute_kind) != _text_present(data.attribute_value):
        raise ValueError("attribute kind and value must be supplied together")
    if _text_present(data.color) and data.any_color:
        raise ValueError("specific color and any_color are mutually exclusive")
    if require_complete and data.fulfillment == "delivery":
        if not _text_present(data.phone):
            raise ValueError("delivery review requires a phone")
        if not _text_present(data.location):
            raise ValueError("delivery review requires a location")


def _review_lines(
    data: WizardReviewData,
    language: Literal["ru", "uz"],
) -> list[str]:
    model = _escaped_value(data.model, 240)
    trusted_url = safe_product_url(data.model_url)
    if trusted_url:
        model = f'<a href="{html.escape(trusted_url, quote=True)}">{model}</a>'
    rows = [
        f"<b>{'Модель' if language == 'ru' else 'Model'}:</b> {model}"
    ]
    if data.attribute_kind and data.attribute_value:
        if data.attribute_kind == "size":
            label = "Размер" if language == "ru" else "O‘lcham"
        else:
            label = "Память" if language == "ru" else "Xotira"
        rows.append(
            f"<b>{label}:</b> {_escaped_value(data.attribute_value, 120)}"
        )
    if _text_present(data.color) or data.any_color:
        color = (
            _escaped_value(data.color, 160)
            if _text_present(data.color)
            else "не важен" if language == "ru" else "farqi yo‘q"
        )
        rows.append(
            f"<b>{'Цвет' if language == 'ru' else 'Rang'}:</b> {color}"
        )
    method = (
        "Доставка"
        if data.fulfillment == "delivery" and language == "ru"
        else "Yetkazib berish"
        if data.fulfillment == "delivery"
        else "Самовывоз"
        if language == "ru"
        else "Do‘kondan olib ketish"
    )
    rows.append(
        f"<b>{'Получение' if language == 'ru' else 'Olish usuli'}:</b> {method}"
    )
    if _text_present(data.phone):
        rows.append(
            f"<b>{'Телефон' if language == 'ru' else 'Telefon'}:</b> "
            f"{_escaped_value(data.phone, 80)}"
        )
    elif data.fulfillment == "pickup":
        rows.append(
            "<b>Связь:</b> этот Telegram-чат"
            if language == "ru"
            else "<b>Aloqa:</b> shu Telegram chati"
        )
    if data.fulfillment == "delivery" and _text_present(data.location):
        rows.append(
            f"<b>{'Локация' if language == 'ru' else 'Lokatsiya'}:</b> "
            f"{_escaped_value(data.location, 400)}"
        )
    return rows


def build_review_step(
    data: WizardReviewData,
    language: str = "bi",
) -> WizardStep:
    """Render a complete request while explicitly avoiding order confirmation."""

    _validate_review_data(data, require_complete=True)
    ru = (
        "Проверьте:\n\n"
        + "\n".join(_review_lines(data, "ru"))
        + "\n\nЗаказ не оформлен, товар не зарезервирован. Подтвердит менеджер."
    )
    uz = (
        "Tekshiring:\n\n"
        + "\n".join(_review_lines(data, "uz"))
        + "\n\nBuyurtma rasmiylashtirilmagan, mahsulot band qilinmagan. Menejer tasdiqlaydi."
    )
    keyboard = (
        (
            ButtonSpec(
                text=_button_text(
                    language,
                    "Передать менеджеру",
                    "Menejerga yuborish",
                ),
                action="submit",
            ),
        ),
        (
            ButtonSpec(
                text=_button_text(language, "Изменить данные", "Ma’lumotlarni o‘zgartirish"),
                action="edit",
            ),
        ),
        _navigation_row(language),
    )
    return WizardStep(
        code="review",
        text=_localized(language, ru, uz),
        choices=(),
        keyboard=keyboard,
    )


def build_edit_menu(
    data: WizardReviewData,
    language: str = "bi",
) -> WizardStep:
    """Expose only fields meaningful for the selected fulfillment path."""

    _validate_review_data(data, require_complete=False)
    ru = "Что хотите изменить?"
    uz = "Qaysi ma’lumotni o‘zgartirmoqchisiz?"
    rows: list[tuple[ButtonSpec, ...]] = [
        (
            ButtonSpec(
                text=_button_text(language, "Модель", "Model"),
                action="edit_model",
            ),
        )
    ]
    if data.attribute_kind:
        rows.append(
            (
                ButtonSpec(
                    text=_button_text(
                        language,
                        "Размер" if data.attribute_kind == "size" else "Память",
                        "O‘lcham" if data.attribute_kind == "size" else "Xotira",
                    ),
                    action="edit_attribute",
                ),
            )
        )
    if _text_present(data.color) or data.any_color:
        rows.append(
            (
                ButtonSpec(
                    text=_button_text(language, "Цвет", "Rang"),
                    action="edit_color",
                ),
            )
        )
    rows.append(
        (
            ButtonSpec(
                text=_button_text(language, "Способ получения", "Olish usuli"),
                action="edit_fulfillment",
            ),
        )
    )
    if data.fulfillment == "delivery":
        rows.extend(
            [
                (
                    ButtonSpec(
                        text=_button_text(language, "Телефон", "Telefon"),
                        action="edit_phone",
                    ),
                ),
                (
                    ButtonSpec(
                        text=_button_text(language, "Локация", "Lokatsiya"),
                        action="edit_location",
                    ),
                ),
            ]
        )
    else:
        rows.append(
            (
                ButtonSpec(
                    text=_button_text(language, "Контакт", "Aloqa"),
                    action="edit_pickup_contact",
                ),
            )
        )
    rows.append(_navigation_row(language))
    return WizardStep(
        code="edit",
        text=_localized(language, ru, uz),
        choices=(),
        keyboard=tuple(rows),
    )


def variants_for_choice(
    variants: Sequence[ProductVariant],
    step: WizardStep,
    choice_id: str,
) -> tuple[ProductVariant, ...]:
    """Filter the same approved variants by a selected catalogue value."""

    choice = step.choice(choice_id)
    selected = _value_key(choice.value)
    if step.code == "model":
        return tuple(
            variant
            for variant in variants
            if _value_key(variant.model) == selected
        )
    if step.code in {"memory", "size"}:
        return tuple(
            variant
            for variant in variants
            if _value_key(variant.memory) == selected
        )
    if step.code == "color":
        return tuple(
            variant
            for variant in variants
            if _value_key(variant.color) == selected
        )
    raise ValueError(f"unsupported wizard step: {step.code}")
