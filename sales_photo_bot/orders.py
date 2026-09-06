from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from telegram import MessageEntity


_GLOBAL_ID_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)🆔[ \t]*:[ \t]*\d+[ \t]*$"
)
_GLOBAL_ID_FIELD_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)🆔[ \t]*:.*$"
)
_DAILY_QUANTITY_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)Шт[ \t]*:[ \t]*(?P<value>\d+)[ \t]*$",
    re.IGNORECASE,
)
_DAILY_QUANTITY_FIELD_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)Шт[ \t]*:.*$",
    re.IGNORECASE,
)
_CARD_NUMBER_FIELD_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)(?P<label>Шт|🆔)[ \t]*:.*$",
    re.IGNORECASE,
)
_CARD_BODY_START_RE = re.compile(r"^[ \t\u2063]*🛒💵[ \t]*:")
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
    """Return the daily number, including from legacy cards using only ``🆔``."""

    original = str(body or "")
    lines = _line_spans(original)[:8]
    if _top_daily_field(original) is not None:
        return card_daily_quantity(body)
    for _, _, line in lines:
        match = _GLOBAL_ID_LINE_RE.match(line)
        if match is not None:
            digits = "".join(character for character in line if character.isdigit())
            return int(digits) if digits else None
    return None


def card_daily_quantity(body: object) -> int | None:
    """Return the explicit per-day ``Шт`` number from a sales card."""

    line = _top_daily_field(str(body or ""))
    match = _DAILY_QUANTITY_LINE_RE.match(line or "")
    return int(match.group("value")) if match is not None else None


def card_global_id(body: object) -> int | None:
    """Return the permanent ``🆔`` value from a sales card."""

    original = str(body or "")
    insert_at, leading = _header_insert_position(original)
    header_lines = _line_spans(original[insert_at:])[:8] if not leading else ()
    if header_lines and _DAILY_QUANTITY_FIELD_RE.match(header_lines[0][2]):
        if len(header_lines) < 2:
            return None
        match = _GLOBAL_ID_LINE_RE.match(header_lines[1][2])
        if match is None:
            return None
        digits = "".join(
            character for character in header_lines[1][2] if character.isdigit()
        )
        return int(digits) if digits else None
    for _, _, line in _line_spans(original)[:8]:
        if _CARD_BODY_START_RE.match(line) is not None:
            break
        if _GLOBAL_ID_FIELD_RE.match(line) is None:
            continue
        match = _GLOBAL_ID_LINE_RE.match(line)
        if match is None:
            return None
        digits = "".join(character for character in line if character.isdigit())
        return int(digits) if digits else None
    return None


def _header_insert_position(body: str) -> tuple[int, str]:
    """Return the canonical header position and any required leading newline."""

    marker_end = 0
    while marker_end < len(body) and body[marker_end] == "\u2063":
        marker_end += 1
    remainder = body[marker_end:]
    newline = re.search(r"\r\n|\r|\n", remainder)
    first_line = remainder if newline is None else remainder[: newline.start()]
    if _DATE_LINE_RE.match(first_line) is None:
        return marker_end, ""
    if newline is None:
        return len(body), "\n"
    return marker_end + newline.end(), ""


def _top_daily_field(body: str) -> str | None:
    insert_at, leading = _header_insert_position(body)
    if leading:
        return None
    lines = _line_spans(body[insert_at:])
    if not lines or _DAILY_QUANTITY_FIELD_RE.match(lines[0][2]) is None:
        return None
    return lines[0][2]


def _has_two_number_header(body: str) -> bool:
    insert_at, leading = _header_insert_position(body)
    if leading:
        return False
    lines = _line_spans(body[insert_at:])
    return (
        len(lines) >= 2
        and _DAILY_QUANTITY_FIELD_RE.match(lines[0][2]) is not None
        and _GLOBAL_ID_FIELD_RE.match(lines[1][2]) is not None
    )


def _line_with_ending_end(body: str, line_end: int) -> int:
    if body[line_end : line_end + 2] == "\r\n":
        return line_end + 2
    if body[line_end : line_end + 1] in {"\r", "\n"}:
        return line_end + 1
    return line_end


def _apply_replacements(
    original: str,
    original_entities: tuple[MessageEntity, ...],
    replacements: Sequence[tuple[int, int, str]],
    *,
    max_length: int,
) -> CardOrderNormalization:
    replacements = tuple(
        replacement
        for replacement in replacements
        if original[replacement[0] : replacement[1]] != replacement[2]
    )
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


def card_numbers_match(
    body: object,
    daily_quantity: int,
    global_order_id: int,
) -> bool:
    """Return true only when the canonical two-number header is already intact."""

    original = str(body or "")
    expected = (
        f"Шт: {max(1, int(daily_quantity))}\n"
        f"🆔: {max(1, int(global_order_id))}"
    )
    insert_at, leading = _header_insert_position(original)
    if leading or not original[insert_at:].startswith(expected):
        return False
    remainder = original[insert_at + len(expected) :]
    if remainder:
        if len(remainder) <= 2 or not remainder.startswith("\n\n"):
            return False
        body_first_line = re.split(r"\r\n|\r|\n", remainder[2:], maxsplit=1)[0]
        if not body_first_line.strip(" \t"):
            return False
    fields: list[str] = []
    for _, _, line in _line_spans(original)[:8]:
        if _CARD_BODY_START_RE.match(line) is not None:
            break
        match = _CARD_NUMBER_FIELD_RE.match(line)
        if match is not None:
            fields.append(match.group("label").casefold())
    return fields.count("шт") == 1 and fields.count("🆔") == 1


def ensure_card_numbers(
    body: object,
    entities: Sequence[MessageEntity] | None,
    daily_quantity: int,
    global_order_id: int,
    *,
    max_length: int,
) -> CardOrderNormalization:
    """Normalize the mutable daily number and immutable ID as one header block.

    The block is placed immediately after the optional sale date. Duplicate or
    malformed ``Шт``/``🆔`` fields within the first eight lines are removed.
    If an entity crosses a changed boundary, or the result would be too long,
    the original text and entities are returned unchanged.
    """

    original = str(body or "")
    original_entities = tuple(entities or ())
    canonical = (
        f"Шт: {max(1, int(daily_quantity))}\n"
        f"🆔: {max(1, int(global_order_id))}"
    )
    insert_at, leading = _header_insert_position(original)

    # Replace the initial header/blank area wholesale so its placement and
    # spacing are deterministic, regardless of which legacy number came first.
    prefix_end = insert_at
    while prefix_end < len(original):
        newline = re.search(r"\r\n|\r|\n", original[prefix_end:])
        if newline is None:
            line_end = len(original)
            full_end = line_end
        else:
            line_end = prefix_end + newline.start()
            full_end = prefix_end + newline.end()
        line = original[prefix_end:line_end]
        if line.strip(" \t") and _CARD_NUMBER_FIELD_RE.match(line) is None:
            break
        prefix_end = full_end
        if newline is None:
            break

    prefix_replacement = leading + canonical
    if prefix_end < len(original):
        prefix_replacement += "\n\n"
    replacements: list[tuple[int, int, str]] = [
        (insert_at, prefix_end, prefix_replacement)
    ]

    for line_start, line_end, line in _line_spans(original)[:8]:
        if _CARD_BODY_START_RE.match(line) is not None:
            break
        if _CARD_NUMBER_FIELD_RE.match(line) is None:
            continue
        full_end = _line_with_ending_end(original, line_end)
        if full_end > insert_at and line_start < prefix_end:
            continue
        replacements.append((line_start, full_end, ""))

    return _apply_replacements(
        original,
        original_entities,
        replacements,
        max_length=max_length,
    )


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
    # New cards carry the per-day value in ``Шт``. Legacy cards carry it
    # in ``🆔``; retain support for both while callers migrate.
    has_daily_field = _has_two_number_header(original)
    field_re = _DAILY_QUANTITY_FIELD_RE if has_daily_field else _GLOBAL_ID_FIELD_RE
    label = "Шт" if has_daily_field else "🆔"
    canonical = f"{label}: {max(1, int(order_id))}"
    replacements: list[tuple[int, int, str]] = []
    collected_order_lines: list[tuple[int, int, re.Match[str]]] = []
    for line_start, line_end, line in _line_spans(original)[:8]:
        if _CARD_BODY_START_RE.match(line) is not None:
            break
        match = field_re.match(line)
        if match is not None:
            collected_order_lines.append((line_start, line_end, match))
    order_lines = tuple(collected_order_lines)
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

    return _apply_replacements(
        original,
        original_entities,
        replacements,
        max_length=max_length,
    )
