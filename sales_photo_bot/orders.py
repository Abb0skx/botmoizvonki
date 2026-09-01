from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Sequence

from telegram import MessageEntity


_ORDER_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)🆔[ \t]*:[ \t]*\d+[ \t]*$"
)
_ORDER_FIELD_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)🆔[ \t]*:.*$"
)
_DATE_LINE_RE = re.compile(
    r"^📆[ \t]*:[ \t]*\d{2}/\d{2}/\d{4}[ \t]*$"
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


@dataclass(frozen=True)
class CardOrderNormalization:
    body: str
    entities: tuple[MessageEntity, ...]
    changed: bool = False


def card_order_id(body: object) -> int | None:
    for _, _, line in _line_spans(str(body or ""))[:8]:
        match = _ORDER_LINE_RE.match(line)
        if match is not None:
            digits = "".join(character for character in line if character.isdigit())
            return int(digits) if digits else None
    return None


def ensure_card_order_id(
    body: object,
    entities: Sequence[MessageEntity] | None,
    order_id: int,
    *,
    max_length: int,
) -> CardOrderNormalization:
    """Insert or restore the daily order ID without damaging Telegram entities."""

    original = str(body or "")
    original_entities = tuple(entities or ())
    canonical = f"🆔: {max(1, int(order_id))}"
    replacements: list[tuple[int, int, str]] = []
    order_lines = tuple(
        (line_start, line_end, match)
        for line_start, line_end, line in _line_spans(original)[:8]
        if (match := _ORDER_FIELD_RE.match(line)) is not None
    )
    if order_lines:
        first_start, first_end, first_match = order_lines[0]
        replacements.append(
            (
                first_start,
                first_end,
                first_match.group("prefix") + canonical,
            )
        )
        for duplicate_start, duplicate_end, _ in order_lines[1:]:
            if original[duplicate_end : duplicate_end + 2] == "\r\n":
                duplicate_end += 2
            elif original[duplicate_end : duplicate_end + 1] in {"\r", "\n"}:
                duplicate_end += 1
            replacements.append((duplicate_start, duplicate_end, ""))
    else:
        marker_end = 0
        while marker_end < len(original) and original[marker_end] == "\u2063":
            marker_end += 1
        remainder = original[marker_end:]
        first_newline = remainder.find("\n")
        first_line = remainder if first_newline < 0 else remainder[:first_newline]
        if _DATE_LINE_RE.match(first_line) is not None and first_newline >= 0:
            start = end = marker_end + first_newline + 1
            replacement = canonical + "\n"
        elif _DATE_LINE_RE.match(first_line) is not None:
            start = end = len(original)
            replacement = "\n" + canonical
        else:
            start = end = marker_end
            replacement = canonical + "\n\n"
        replacements.append((start, end, replacement))

    replacements = [
        replacement
        for replacement in replacements
        if original[replacement[0] : replacement[1]] != replacement[2]
    ]
    if not replacements:
        return CardOrderNormalization(original, original_entities)

    normalized = original
    normalized_entities = list(original_entities)
    for start, end, replacement in sorted(replacements, reverse=True):
        start_utf16 = _utf16_length(normalized[:start])
        end_utf16 = start_utf16 + _utf16_length(normalized[start:end])
        delta = _utf16_length(replacement) - (end_utf16 - start_utf16)
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
            if start != end and entity_start >= start_utf16 and entity_end <= end_utf16:
                continue
            return CardOrderNormalization(original, original_entities)
        normalized = normalized[:start] + replacement + normalized[end:]
        normalized_entities = shifted

    if len(normalized) > max(1, int(max_length)):
        return CardOrderNormalization(original, original_entities)
    return CardOrderNormalization(normalized, tuple(normalized_entities), True)
