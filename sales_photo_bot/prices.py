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
    header_index = next(
        (
            index
            for index, (_, _, line) in enumerate(lines[:12])
            if _CARD_HEADER_RE.match(line) is not None
        ),
        None,
    )
    if header_index is None:
        return PriceCardNormalization(original, original_entities)

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
