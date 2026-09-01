from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from telegram import MessageEntity


TASHKENT_TZ = timezone(timedelta(hours=5))
_SALE_DATE_RE = re.compile(
    r"(?<![\w/])"
    r"(?P<day>\d{1,2})/(?P<month>\d{1,2})"
    r"(?:/(?P<year>\d{4}))?"
    r"(?![\w/])"
)
_CARD_DATE_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)📆[ \t]*:.*$"
)


@dataclass(frozen=True)
class SaleDateMatch:
    value: date
    start: int
    end: int


@dataclass(frozen=True)
class CardDateNormalization:
    body: str
    entities: tuple[MessageEntity, ...]
    changed: bool = False


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


def tashkent_today() -> date:
    return datetime.now(TASHKENT_TZ).date()


def _most_recent_date(day: int, month: int, today: date) -> date | None:
    """Return the latest valid occurrence that is not later than today."""

    # Eight years cover the widest possible gap between leap days. A larger
    # bound keeps the helper safe if the Gregorian rules ever matter in tests.
    for year in range(today.year, max(0, today.year - 16), -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= today:
            return candidate
    return None


def extract_sale_date(
    value: object,
    *,
    today: date | None = None,
) -> SaleDateMatch | None:
    """Find one standalone ``DD/MM`` sale date and infer its Tashkent year."""

    raw = str(value or "")
    reference = today or tashkent_today()
    for match in _SALE_DATE_RE.finditer(raw):
        day = int(match.group("day"))
        month = int(match.group("month"))
        year_text = match.group("year")
        if year_text is not None:
            try:
                candidate = date(int(year_text), month, day)
            except ValueError:
                continue
        else:
            candidate = _most_recent_date(day, month, reference)
            if candidate is None:
                continue
        return SaleDateMatch(candidate, match.start(), match.end())
    return None


def remove_sale_date(
    value: object,
    match: SaleDateMatch | None = None,
) -> str:
    """Remove the recognized date while retaining the rest of the caption."""

    raw = str(value or "")
    selected = match or extract_sale_date(raw)
    if selected is None:
        return raw.strip()
    residual = raw[: selected.start] + " " + raw[selected.end :]
    return " ".join(residual.split())


def normalize_card_sale_date(
    body: object,
    entities: Sequence[MessageEntity] | None,
    sale_date: date,
    *,
    max_length: int,
) -> CardDateNormalization:
    """Normalize an existing card date row from the authoritative ledger date.

    Cards created without an explicitly supplied date intentionally have no
    ``📆:`` row, so this helper never inserts a missing row. It only repairs a
    row that is already present near the top of a generated card.
    """

    original = str(body or "")
    original_entities = tuple(entities or ())
    date_line = next(
        (
            (start, end, match)
            for start, end, line in _line_spans(original)[:8]
            if (match := _CARD_DATE_LINE_RE.match(line)) is not None
        ),
        None,
    )
    if date_line is None:
        return CardDateNormalization(original, original_entities)
    start, end, match = date_line
    replacement = match.group("prefix") + f"📆: {sale_date:%d/%m/%Y}"
    if original[start:end] == replacement:
        return CardDateNormalization(original, original_entities)
    normalized = original[:start] + replacement + original[end:]
    if len(normalized) > max(1, int(max_length)):
        return CardDateNormalization(original, original_entities)

    start_utf16 = _utf16_length(original[:start])
    end_utf16 = start_utf16 + _utf16_length(original[start:end])
    delta = _utf16_length(replacement) - (end_utf16 - start_utf16)
    shifted: list[MessageEntity] = []
    for entity in original_entities:
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
        return CardDateNormalization(original, original_entities)

    return CardDateNormalization(normalized, tuple(shifted), True)
