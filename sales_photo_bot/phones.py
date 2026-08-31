from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from telegram import MessageEntity


# Uzbekistan's E.164 national significant number is exactly nine digits. These
# two-digit destination codes combine the published national plan with current
# official operator allocations. Keeping an allowlist prevents a price or a
# random nine-digit order number from being treated as a telephone number.
UZBEK_DESTINATION_CODES = frozenset(
    {
        "20",
        "33",
        "50",
        "55",
        "61",
        "62",
        "65",
        "66",
        "67",
        "69",
        "70",
        "71",
        "72",
        "73",
        "74",
        "75",
        "76",
        "77",
        "78",
        "79",
        "87",
        "88",
        "90",
        "91",
        "93",
        "94",
        "95",
        "97",
        "98",
        "99",
    }
)

# A run stops at letters, slash, comma, semicolon, or a line break. This makes
# two explicitly separated numbers independent candidates, while a long/merged
# digit run fails closed instead of being split into a false phone number.
_PHONE_RUN_RE = re.compile(
    r"(?<!\w)(?:\+|00)?[0-9]"
    r"(?:[0-9 \t\u00a0().\-\u2010-\u2015\u2212]*[0-9])?(?!\w)"
)
_CARD_HEADER_RE = re.compile(r"^[ \t\u2063]*🛒💵[ \t]*:")
_EXPENSE_LINE_RE = re.compile(r"^[ \t]*rasxod[ \t]*:", re.IGNORECASE)
_PHONE_LINE_RE = re.compile(
    r"^(?P<prefix>[ \t\u2063]*)📞[ \t]*:"
)
_PHONE_FIELD_SEPARATORS_RE = re.compile(
    r"^[\s/|,;.()\-\u2010-\u2015\u2212]*$"
)


def normalize_uzbek_phone(value: object) -> str | None:
    """Return one Uzbekistan phone in ``+998 XX XXX XX XX`` form."""

    raw = str(value or "").strip()
    digits = "".join(char for char in raw if "0" <= char <= "9")
    if raw.startswith("+") and not (
        len(digits) == 12 and digits.startswith("998")
    ):
        return None
    if len(digits) == 14 and digits.startswith("00998"):
        national = digits[5:]
    elif len(digits) == 12 and digits.startswith("998"):
        national = digits[3:]
    elif len(digits) == 9:
        national = digits
    else:
        return None

    if national[:2] not in UZBEK_DESTINATION_CODES:
        return None
    return (
        f"+998 {national[:2]} {national[2:5]} "
        f"{national[5:7]} {national[7:9]}"
    )


def _phones_from_run(value: str) -> tuple[str, ...]:
    normalized = normalize_uzbek_phone(value)
    if normalized:
        return (normalized,)

    # Two national numbers are often pasted with only a space between them.
    # Try every whitespace boundary and accept only one unambiguous split.
    pairs: list[tuple[str, str]] = []
    for gap in re.finditer(r"[ \t\u00a0]+", value):
        first = normalize_uzbek_phone(value[: gap.start()])
        second = normalize_uzbek_phone(value[gap.end() :])
        if first and second and (first, second) not in pairs:
            pairs.append((first, second))
    return pairs[0] if len(pairs) == 1 else ()


def _scan_uzbek_phones(
    value: object,
    stop_after: int = 3,
) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    result: list[str] = []
    accepted_spans: list[tuple[int, int]] = []
    for match in _PHONE_RUN_RE.finditer(str(value or "")):
        phones = _phones_from_run(match.group(0))
        if not phones:
            continue
        accepted_spans.append(match.span())
        for normalized in phones:
            if normalized not in result:
                result.append(normalized)
                if len(result) >= max(1, int(stop_after)):
                    return tuple(result), tuple(accepted_spans)
    return tuple(result), tuple(accepted_spans)


def _find_uzbek_phones(value: object, stop_after: int = 3) -> tuple[str, ...]:
    return _scan_uzbek_phones(value, stop_after=stop_after)[0]


def extract_uzbek_phones(value: object, limit: int = 2) -> tuple[str, ...]:
    """Extract distinct Uzbekistan numbers, rejecting input with too many."""

    maximum = max(0, int(limit))
    if maximum == 0:
        return ()
    result = _find_uzbek_phones(value, stop_after=maximum + 1)
    return result if len(result) <= maximum else ()


def phone_line(phones: Sequence[str]) -> str:
    values = tuple(str(value) for value in phones[:2] if value)
    return f"📞: {' / '.join(values)}" if values else "📞:"


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _line_spans(value: str) -> tuple[tuple[int, int, str], ...]:
    result: list[tuple[int, int, str]] = []
    offset = 0
    for chunk in value.splitlines(keepends=True):
        line = chunk.rstrip("\r\n")
        result.append((offset, offset + len(line), line))
        offset += len(chunk)
    return tuple(result)


def _canonical_phone_span(
    caption: str,
) -> tuple[int, int, re.Match[str]] | None:
    """Locate the generated card's phone row, not arbitrary phone-like text."""

    lines = _line_spans(caption)
    if len(lines) < 3 or _CARD_HEADER_RE.match(lines[0][2]) is None:
        return None
    if _EXPENSE_LINE_RE.match(lines[1][2]) is None:
        return None

    # In the generated layout the phone row is the first non-empty row after
    # ``rasxod``. Allow extra blank rows because managers edit captions by hand.
    for start, end, line in lines[2:]:
        if not line.strip(" \t\u2063"):
            continue
        match = _PHONE_LINE_RE.match(line)
        if match is None:
            return None
        return start, end, match
    return None


@dataclass(frozen=True)
class PhoneCaptionNormalization:
    caption: str
    entities: tuple[MessageEntity, ...]
    changed: bool = False


def normalize_caption_phone_field(
    caption: object,
    entities: Sequence[MessageEntity] | None = None,
) -> PhoneCaptionNormalization:
    """Normalize the one exact ``📞:`` field and preserve all other entities.

    Entity offsets received from Telegram use UTF-16 units. Entities fully inside
    the replaced phone row are intentionally removed; entities crossing the row
    boundary make the operation fail closed so formatting elsewhere is untouched.
    """

    original = str(caption or "")
    original_entities = tuple(entities or ())
    phone_span = _canonical_phone_span(original)
    if phone_span is None:
        return PhoneCaptionNormalization(original, original_entities)
    start, end, match = phone_span
    field_value = original[start + match.end() : end]
    candidates, accepted_spans = _scan_uzbek_phones(field_value, stop_after=3)
    if len(candidates) > 2:
        return PhoneCaptionNormalization(original, original_entities)
    # Do not erase incomplete notes while a manager is typing. Empty rows are
    # canonicalized, and rows with one or two valid numbers are normalized.
    if field_value.strip() and not candidates:
        return PhoneCaptionNormalization(original, original_entities)
    if candidates:
        residual_parts: list[str] = []
        previous_end = 0
        for span_start, span_end in accepted_spans:
            residual_parts.append(field_value[previous_end:span_start])
            previous_end = span_end
        residual_parts.append(field_value[previous_end:])
        if _PHONE_FIELD_SEPARATORS_RE.fullmatch("".join(residual_parts)) is None:
            return PhoneCaptionNormalization(original, original_entities)
    replacement = match.group("prefix") + phone_line(candidates)
    if original[start:end] == replacement:
        return PhoneCaptionNormalization(original, original_entities)

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
        return PhoneCaptionNormalization(original, original_entities)

    normalized_caption = original[:start] + replacement + original[end:]
    if len(normalized_caption) > 1024:
        return PhoneCaptionNormalization(original, original_entities)
    return PhoneCaptionNormalization(
        normalized_caption,
        tuple(shifted),
        changed=True,
    )
