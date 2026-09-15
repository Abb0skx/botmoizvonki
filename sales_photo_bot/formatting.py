from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date

from .models import (
    ProductIdentifiers,
    identifier_imeis,
    identifier_serial_numbers,
    product_display_values,
)
from .phones import extract_uzbek_phones, phone_line


MAX_SERIAL_NUMBER = 64
MAX_PRODUCT_CARD_LABEL = 2048
# Leave room for the manager and delivery blocks that are appended later.
MAX_CAPTION_TEXT_UNITS = 800
MAX_MESSAGE_TEXT_UNITS = 3600
MIN_CARD_TEXT_UNITS = 256
MAX_TELEGRAM_TEXT_UNITS = 4096
_HTML_TAG_RE = re.compile(r"<[^>]*>")
MANAGER_LINE_RE = re.compile(
    r"(?:\n\n)?👤 Менеджер: <b>[^<>\r\n]{1,64}</b>\s*\Z"
)
MANAGER_NAME_RE = re.compile(
    r"(?:^|\n)👤 Менеджер: <b>([^<>\r\n]{1,64})</b>\s*\Z"
)
PRODUCT_LINE_RE = re.compile(
    r"(?:^|\n)📦[ \t]+(?:О[ \t]+товаре[ \t]*:[ \t]*)?"
    r"(?P<product>[^\r\n]{1,8192})(?:\n|$)",
    re.IGNORECASE,
)
_CARD_HEADER_LINE_RE = re.compile(r"^[ \t\u2063\u2064\ufeff]*🛒💵[ \t]*:")
_BLOCKQUOTE_TAG_RE = re.compile(r"</?blockquote>", re.IGNORECASE)
_IDENTIFIER_LINE_RE = re.compile(
    r"^(?P<label>S/N(?:[ \t]+\d+)?|IMEI(?:[ \t]*\d+)?)"
    r"[ \t]*:[ \t]*(?P<value>.*)$",
    re.IGNORECASE,
)
_IDENTIFIER_PREFIX_RE = re.compile(r"^(?:S/N|IMEI)", re.IGNORECASE)
_SERIAL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{2,39}$")


@dataclass(frozen=True)
class CardIdentifiers:
    """Identifier fields parsed from the generated part of a sales card."""

    state: str
    serial_numbers: tuple[str, ...] = ()
    imeis: tuple[str, ...] = ()

    @property
    def conclusive(self) -> bool:
        return self.state in {"empty", "valid"}


def _valid_card_imei(value: str) -> bool:
    if re.fullmatch(r"[0-9]{15}", value) is None:
        return False
    total = 0
    for index, character in enumerate(reversed(value)):
        digit = int(character)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def identifiers_from_card(body: object) -> CardIdentifiers:
    """Read all S/N and IMEI rows before the unique sales-card header."""

    raw_lines = str(body or "").splitlines()
    header_indexes = [
        index
        for index, raw_line in enumerate(raw_lines)
        if _CARD_HEADER_LINE_RE.match(raw_line) is not None
    ]
    if len(header_indexes) != 1:
        return CardIdentifiers("malformed")

    serial_numbers: list[str] = []
    imeis: list[str] = []
    for raw_line in raw_lines[: header_indexes[0]]:
        line = html.unescape(_BLOCKQUOTE_TAG_RE.sub("", raw_line)).strip(
            " \t\u2063\u2064\ufeff"
        )
        match = _IDENTIFIER_LINE_RE.fullmatch(line)
        if match is None:
            if _IDENTIFIER_PREFIX_RE.match(line) is not None:
                return CardIdentifiers("malformed")
            continue
        value = " ".join(match.group("value").split())
        if not value or "… ещё" in value.casefold():
            return CardIdentifiers("malformed")
        label = match.group("label").upper().replace(" ", "")
        if label.startswith("S/N"):
            if _SERIAL_VALUE_RE.fullmatch(value) is None:
                return CardIdentifiers("malformed")
            if value.casefold() not in {item.casefold() for item in serial_numbers}:
                serial_numbers.append(value)
        else:
            if not _valid_card_imei(value):
                return CardIdentifiers("malformed")
            if value not in imeis:
                imeis.append(value)
        if len(serial_numbers) + len(imeis) > 160:
            return CardIdentifiers("malformed")

    if not serial_numbers and not imeis:
        return CardIdentifiers("empty")
    return CardIdentifiers(
        "valid",
        serial_numbers=tuple(serial_numbers),
        imeis=tuple(imeis),
    )


def _compact(value: object, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def _safe(value: object, limit: int) -> str:
    return html.escape(_compact(value, limit), quote=True)


def _telegram_text_units(value: str) -> int:
    """Count UTF-16 units after Telegram parses the small HTML subset."""

    plain = html.unescape(_HTML_TAG_RE.sub("", value))
    return len(plain.encode("utf-16-le")) // 2


def build_caption(
    client_caption: str | None,
    identifiers: ProductIdentifiers,
    manager: str | None = None,
    product_label: str | None = None,
    sale_date: date | None = None,
    order_id: int | None = None,
    daily_quantity: int | None = None,
    global_order_id: int | None = None,
    *,
    max_text_units: int = MAX_CAPTION_TEXT_UNITS,
) -> str:
    """Build the sales card, retaining only verified phones and identifiers."""

    if (
        type(max_text_units) is not int
        or not MIN_CARD_TEXT_UNITS
        <= max_text_units
        <= MAX_TELEGRAM_TEXT_UNITS
    ):
        raise ValueError(
            "max_text_units must be an integer between "
            f"{MIN_CARD_TEXT_UNITS} and {MAX_TELEGRAM_TEXT_UNITS}"
        )

    phone_sources = [str(client_caption or "")]
    phone_sources.extend(str(value or "") for value in identifiers.phone_numbers)
    # Scan all sources together. A third distinct number makes the field
    # ambiguous instead of silently selecting whichever two appeared first.
    phones = extract_uzbek_phones("\n".join(phone_sources), limit=2)
    lines: list[str] = []
    if sale_date:
        lines.append(f"📆: {sale_date:%d/%m/%Y}")
    number_lines: list[str] = []
    effective_daily_quantity = daily_quantity
    if effective_daily_quantity is None and global_order_id is not None:
        effective_daily_quantity = order_id
    if effective_daily_quantity is not None:
        number_lines.append(f"Шт: {max(1, int(effective_daily_quantity))}")
    if global_order_id is not None:
        number_lines.append(f"🆔: {max(1, int(global_order_id))}")
    # ``order_id`` alone keeps the legacy one-line output. When a permanent ID
    # is supplied it can also serve as the old name for the daily quantity.
    if not number_lines and order_id is not None:
        number_lines.append(f"🆔: {max(1, int(order_id))}")
    if number_lines:
        lines.extend([*number_lines, ""])
    elif sale_date:
        lines.append("")
    prefix_lines = list(lines)
    manual_product = _compact(product_label, 120)
    product_values = (
        (manual_product,)
        if manual_product
        else product_display_values(identifiers)
    )
    product_values = tuple(value for value in product_values if value)
    imeis = identifier_imeis(identifiers)
    legacy_imei2_only = bool(
        not identifiers.imeis
        and not str(identifiers.imei or "").strip()
        and str(identifiers.imei2 or "").strip()
    )
    serial_numbers = identifier_serial_numbers(identifiers)

    tail_lines = [
        "🛒💵:",
        "rasxod:",
        "",
        phone_line(phones),
        "",
        "<b>Наличка</b>",
        "💵:",
        "🇺🇿:",
        "",
        "<b>Card/Terminal/Paynet</b>",
        "💵:",
        "🇺🇿:",
    ]
    if manager:
        tail_lines.extend(["", f"👤 Менеджер: <b>{_safe(manager, 64)}</b>"])

    visible_products = list(product_values)
    visible_imeis = list(imeis)
    visible_serials = list(serial_numbers)
    hidden_products = 0
    hidden_imeis = 0
    hidden_serials = 0
    product_value_limit = 160

    def render() -> str:
        rendered = list(prefix_lines)
        if visible_products or hidden_products:
            values = [
                _safe(value, product_value_limit)
                for value in visible_products
            ]
            if hidden_products:
                values.append(f"… ещё {hidden_products} товар(а)")
            rendered.append("📦 О товаре: " + "; ".join(values))
        for index, value in enumerate(visible_serials):
            label = "S/N" if index == 0 else f"S/N {index + 1}"
            rendered.append(
                f"<blockquote>{label}: "
                f"{_safe(value, MAX_SERIAL_NUMBER)}</blockquote>"
            )
        if hidden_serials:
            rendered.append(
                f"<blockquote>S/N: … ещё {hidden_serials}</blockquote>"
            )
        # Keep every recognized IMEI directly after the serial-number block.
        # The legacy singular fields and the v2 collection use the same stable
        # labels, so existing cards remain readable while multi-box photos can
        # show every value on its own line.
        for index, value in enumerate(visible_imeis):
            if legacy_imei2_only and index == 0:
                label = "IMEI2"
            else:
                label = "IMEI" if index == 0 else f"IMEI{index + 1}"
            rendered.append(
                f"<blockquote>{label}: {_safe(value, 15)}</blockquote>"
            )
        if hidden_imeis:
            rendered.append(
                f"<blockquote>IMEI: … ещё {hidden_imeis}</blockquote>"
            )
        if (
            visible_products
            or hidden_products
            or visible_imeis
            or hidden_imeis
            or visible_serials
            or hidden_serials
        ):
            rendered.append("")
        rendered.extend(tail_lines)
        return "\n".join(rendered)

    caption = render()
    # Valid service responses normally fit in full (including four boxes with
    # dual IMEIs). Keep a deterministic fail-safe for a pathological card:
    # retain the sales template and visibly report how many tail values could
    # not fit instead of letting Telegram reject the entire card. Prefer all
    # validated IMEIs over additional product/serial values: an IMEI must not
    # disappear merely because one photo contains unusually verbose OCR text.
    while _telegram_text_units(caption) > max_text_units:
        if visible_products and product_value_limit > 80:
            product_value_limit = max(80, product_value_limit - 20)
        elif len(visible_products) > 1:
            visible_products.pop()
            hidden_products += 1
        elif visible_serials:
            visible_serials.pop()
            hidden_serials += 1
        elif visible_imeis:
            visible_imeis.pop()
            hidden_imeis += 1
        else:
            break
        caption = render()
    return caption


def remove_manager_selection(caption_html: str) -> str:
    return MANAGER_LINE_RE.sub("", str(caption_html or "")).rstrip()


def add_manager_selection(caption_html: str, manager: str) -> str:
    base = remove_manager_selection(caption_html)
    return f"{base}\n\n👤 Менеджер: <b>{_safe(manager, 64)}</b>"


def selected_manager_from_caption(caption_html: str) -> str | None:
    match = MANAGER_NAME_RE.search(str(caption_html or ""))
    if not match:
        return None
    return html.unescape(match.group(1))


def product_label_from_card(caption_html: str) -> str | None:
    """Return the optional product line from a generated sales card."""

    match = PRODUCT_LINE_RE.search(str(caption_html or ""))
    if not match:
        return None
    product = html.unescape(match.group("product")).strip()
    return product[:MAX_PRODUCT_CARD_LABEL] or None
