from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from telegram import MessageEntity


_CARD_HEADER_RE = re.compile(
    r"^(?P<label>[ \t\u2063]*🛒💵[ \t]*:[ \t]*)(?P<value>.*)$"
)
_USD_LINE_RE = re.compile(
    r"^(?P<label>[ \t]*💵[ \t]*:[ \t]*)(?P<value>.*)$"
)
_UZS_LINE_RE = re.compile(
    r"^(?P<label>[ \t]*🇺🇿[ \t]*:[ \t]*)(?P<value>.*)$"
)
_EXPENSE_LINE_RE = re.compile(
    r"^[ \t\u2063]*rasxod[ \t]*(?::|$)",
    re.IGNORECASE,
)
_HEADER_AMOUNT_RE = re.compile(
    r"(?<![\w/])(?:\$[ \t]*)?"
    r"(?P<amount>\d(?:[\d \t\u00a0]*\d)?)"
    r"(?:[ \t]*(?:\$|so['’`]?m|сум|uzs))?[ \t]*$",
    re.IGNORECASE,
)
_FIELD_AMOUNT_RE = re.compile(
    r"^[ \t]*(?:\$[ \t]*)?"
    r"(?P<amount>\d(?:[\d \t\u00a0]*\d)?)"
    r"(?:[ \t]*(?P<currency>\$|so['’`]?m|сум|uzs))?"
    r"(?P<suffix>[ \t]+[^\d \t\u00a0].*)?[ \t]*$",
    re.IGNORECASE,
)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _line_spans(value: str) -> tuple[tuple[int, int, str], ...]:
    result: list[tuple[int, int, str]] = []
    offset = 0
    for chunk in value.splitlines(keepends=True):
        line = chunk.rstrip("\r\n")
        result.append((offset, offset + len(line), line))
        offset += len(chunk)
    if value and not result:
        result.append((0, len(value), value))
    return tuple(result)


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _group_thousands(value: str) -> str:
    digits = _digits(value).lstrip("0") or "0"
    groups: list[str] = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    return " ".join(reversed(groups))


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    value: str
    bold_relative_spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class PriceCardNormalization:
    body: str
    entities: tuple[MessageEntity, ...]
    changed: bool = False


@dataclass(frozen=True)
class SupplierProductPrice:
    """Structured supplier row read only from a canonical sales card."""

    state: str
    supplier_name: str | None = None
    amount: int | None = None
    currency: str | None = None

    @property
    def conclusive(self) -> bool:
        return self.state in {"empty", "valid"}


def parse_supplier_product_price(body: object) -> SupplierProductPrice:
    """Split ``🛒💵: supplier price`` without scanning unrelated rows.

    The parser follows the same last-number and 5000 threshold rules as price
    normalization. A malformed card is intentionally inconclusive so a manual
    typo cannot erase previously stored sale analytics.
    """

    lines = _line_spans(str(body or ""))
    headers = [
        (index, match)
        for index, (_, _, line) in enumerate(lines)
        if (match := _CARD_HEADER_RE.match(line)) is not None
    ]
    expenses = [
        index
        for index, (_, _, line) in enumerate(lines)
        if _EXPENSE_LINE_RE.match(line) is not None
    ]
    if (
        len(headers) != 1
        or len(expenses) != 1
        or headers[0][0] >= expenses[0]
    ):
        return SupplierProductPrice("malformed")

    _, header_match = headers[0]
    value = " ".join(header_match.group("value").split())
    if not value:
        return SupplierProductPrice("empty")

    amount_match = _HEADER_AMOUNT_RE.search(value)
    if amount_match is None:
        return SupplierProductPrice("valid", supplier_name=value[:256])

    digits = _digits(amount_match.group("amount"))
    if not digits:
        return SupplierProductPrice("malformed")
    amount = int(digits)
    if amount > 9_223_372_036_854_775_807:
        return SupplierProductPrice("malformed")

    price_token = amount_match.group(0).casefold()
    if "$" in price_token:
        currency = "USD"
    elif any(token in price_token for token in ("so'm", "so’m", "so`m", "сум", "uzs")):
        currency = "UZS"
    else:
        currency = "USD" if amount < 5000 else "UZS"
    supplier = value[: amount_match.start()].strip(" \t/|,;:-–—")[:256] or None
    return SupplierProductPrice(
        "valid",
        supplier_name=supplier,
        amount=amount,
        currency=currency,
    )


def _has_exact_bold(
    entities: Sequence[MessageEntity],
    body: str,
    absolute_start: int,
) -> bool:
    expected_offset = _utf16_length(body[:absolute_start])
    expected_length = _utf16_length("So'm")
    return any(
        entity.type == MessageEntity.BOLD
        and int(entity.offset) == expected_offset
        and int(entity.length) == expected_length
        for entity in entities
    )


def normalize_card_prices(
    body: object,
    entities: Sequence[MessageEntity] | None = None,
    *,
    max_length: int,
) -> PriceCardNormalization:
    """Normalize the generated card's price fields without parsing arbitrary text.

    The supplier row uses the last standalone integer as its price: values below
    5000 are USD, while larger values are UZS. Exact ``💵:`` rows are USD and
    exact ``🇺🇿:`` rows are UZS. Notes after a field amount are retained.
    """

    original = str(body or "")
    original_entities = tuple(entities or ())
    lines = _line_spans(original)
    header_indexes = [
        index
        for index, (_, _, line) in enumerate(lines)
        if _CARD_HEADER_RE.match(line) is not None
    ]
    if len(header_indexes) != 1:
        return PriceCardNormalization(original, original_entities)
    header_index = header_indexes[0]

    replacements: list[_Replacement] = []
    for index, (line_start, line_end, line) in enumerate(lines[header_index:]):
        header_match = _CARD_HEADER_RE.match(line) if index == 0 else None
        usd_match = _USD_LINE_RE.match(line) if index > 0 else None
        uzs_match = _UZS_LINE_RE.match(line) if index > 0 else None

        if header_match is not None:
            value = header_match.group("value")
            amount_match = _HEADER_AMOUNT_RE.search(value)
            if amount_match is None:
                continue
            digits = _digits(amount_match.group("amount"))
            if not digits:
                continue
            amount = int(digits)
            normalized_amount = (
                f"{amount}$" if amount < 5000 else f"{_group_thousands(digits)} So'm"
            )
            start = line_start + header_match.start("value") + amount_match.start()
            end = line_start + header_match.start("value") + amount_match.end()
            replacement = _Replacement(start, end, normalized_amount)
        elif usd_match is not None or uzs_match is not None:
            field_match = usd_match or uzs_match
            assert field_match is not None
            value = field_match.group("value")
            amount_match = _FIELD_AMOUNT_RE.fullmatch(value)
            if amount_match is None:
                continue
            digits = _digits(amount_match.group("amount"))
            if not digits:
                continue
            if usd_match is not None:
                normalized_value = f"{int(digits)}$"
                bold_spans: tuple[tuple[int, int], ...] = ()
            else:
                grouped = _group_thousands(digits)
                normalized_value = f"{grouped} So'm"
                token_start = len(grouped) + 1
                bold_spans = ((token_start, token_start + len("So'm")),)
            start = line_start + field_match.start("value")
            suffix_start = amount_match.start("suffix")
            end = (
                line_start + field_match.start("value") + suffix_start
                if suffix_start >= 0
                else line_start + field_match.end("value")
            )
            replacement = _Replacement(start, end, normalized_value, bold_spans)
        else:
            continue

        text_changed = original[replacement.start : replacement.end] != replacement.value
        missing_bold = any(
            not _has_exact_bold(
                original_entities,
                original,
                replacement.start + relative_start,
            )
            for relative_start, _ in replacement.bold_relative_spans
        )
        if text_changed or missing_bold:
            replacements.append(replacement)

    if not replacements:
        return PriceCardNormalization(original, original_entities)

    normalized = original
    normalized_entities = list(original_entities)
    for replacement in sorted(replacements, key=lambda item: item.start, reverse=True):
        start_utf16 = _utf16_length(normalized[: replacement.start])
        end_utf16 = start_utf16 + _utf16_length(
            normalized[replacement.start : replacement.end]
        )
        delta = _utf16_length(replacement.value) - (end_utf16 - start_utf16)
        shifted: list[MessageEntity] = []
        for entity in normalized_entities:
            entity_start = int(entity.offset)
            entity_end = entity_start + int(entity.length)
            if entity_end <= start_utf16:
                shifted.append(entity)
                continue
            if entity_start >= end_utf16:
                shifted.extend(MessageEntity.shift_entities(delta, (entity,)))
                continue
            if entity_start >= start_utf16 and entity_end <= end_utf16:
                continue
            return PriceCardNormalization(original, original_entities)

        for relative_start, relative_end in replacement.bold_relative_spans:
            shifted.append(
                MessageEntity(
                    type=MessageEntity.BOLD,
                    offset=start_utf16
                    + _utf16_length(replacement.value[:relative_start]),
                    length=_utf16_length(
                        replacement.value[relative_start:relative_end]
                    ),
                )
            )
        normalized = (
            normalized[: replacement.start]
            + replacement.value
            + normalized[replacement.end :]
        )
        normalized_entities = shifted

    if len(normalized) > max(1, int(max_length)):
        return PriceCardNormalization(original, original_entities)
    normalized_entities.sort(key=lambda item: (int(item.offset), int(item.length)))
    return PriceCardNormalization(normalized, tuple(normalized_entities), True)
