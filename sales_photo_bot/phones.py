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
_LINE_PREFIX = r"[ \t\u2063\u2064\ufeff]*"
_CARD_HEADER_RE = re.compile(rf"^{_LINE_PREFIX}🛒💵[ \t]*:")
_EXPENSE_LINE_RE = re.compile(
    rf"^{_LINE_PREFIX}rasxod[ \t]*:",
    re.IGNORECASE,
)
_PHONE_LINE_RE = re.compile(
    rf"^(?P<prefix>{_LINE_PREFIX})📞[ \t]*:"
)
_CASH_HEADER_RE = re.compile(rf"^{_LINE_PREFIX}Наличка[ \t]*$", re.IGNORECASE)
_CARD_PAYMENT_HEADER_RE = re.compile(
    rf"^{_LINE_PREFIX}Card/Terminal/Paynet[ \t]*$",
    re.IGNORECASE,
)
_MONEY_LINE_RE = re.compile(rf"^{_LINE_PREFIX}(?:💵|🇺🇿)[ \t]*:")
_DELIVERY_LINE_RE = re.compile(
    rf"^{_LINE_PREFIX}(?:Доставка|Статус)[ \t]*:",
    re.IGNORECASE,
)
_PHONE_FIELD_SEPARATORS_RE = re.compile(
    r"^[\s/|,;.()\-\u2010-\u2015\u2212]*$"
)
_NO_PHONE_VALUES = frozenset({"-", "–", "—", "без номера"})
_PRODUCT_TRIM_RE = re.compile(
    r"^[\s/|,;:.()\-\u2010-\u2015\u2212]+|"
    r"[\s/|,;:.()\-\u2010-\u2015\u2212]+$"
)
_PHONE_ONLY_LABELS = frozenset(
    {
        "client",
        "phone",
        "tel",
        "клиент",
        "номер",
        "тел",
        "телефон",
    }
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


def _phones_from_run(
    value: str,
) -> tuple[tuple[str, tuple[int, int]], ...]:
    normalized = normalize_uzbek_phone(value)
    if normalized:
        return ((normalized, (0, len(value))),)

    # Two national numbers are often pasted with only a space between them.
    # Try every whitespace boundary and accept only one unambiguous split.
    pairs: list[tuple[tuple[str, tuple[int, int]], ...]] = []
    singles: list[tuple[str, tuple[int, int]]] = []
    for gap in re.finditer(r"[ \t\u00a0]+", value):
        first = normalize_uzbek_phone(value[: gap.start()])
        second = normalize_uzbek_phone(value[gap.end() :])
        if first and second:
            pair = (
                (first, (0, gap.start())),
                (second, (gap.end(), len(value))),
            )
            if pair not in pairs:
                pairs.append(pair)
            continue

        # A short numeric model/memory fragment can immediately precede or
        # follow a phone: ``A16 8/256 901234567``. Keep the short fragment as
        # product text and accept only one unambiguous phone suffix/prefix.
        left_digits = sum(character.isdigit() for character in value[: gap.start()])
        right_digits = sum(character.isdigit() for character in value[gap.end() :])
        if second and left_digits <= 4:
            candidate = (second, (gap.end(), len(value)))
            if candidate not in singles:
                singles.append(candidate)
        if first and right_digits <= 4:
            candidate = (first, (0, gap.start()))
            if candidate not in singles:
                singles.append(candidate)
    if len(pairs) == 1:
        return pairs[0]
    if not pairs and len(singles) == 1:
        return (singles[0],)
    return ()


def _scan_uzbek_phones(
    value: object,
    stop_after: int = 3,
) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    result: list[str] = []
    accepted_spans: list[tuple[int, int]] = []
    for match in _PHONE_RUN_RE.finditer(str(value or "")):
        phone_matches = _phones_from_run(match.group(0))
        if not phone_matches:
            continue
        for normalized, (relative_start, relative_end) in phone_matches:
            accepted_spans.append(
                (
                    match.start() + relative_start,
                    match.start() + relative_end,
                )
            )
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


@dataclass(frozen=True)
class CaptionPhoneField:
    """A fail-closed structural reading of the generated card's phone row."""

    state: str
    phones: tuple[str, ...] = ()
    start: int | None = None
    end: int | None = None
    prefix: str = ""
    value: str = ""

    @property
    def conclusive(self) -> bool:
        return self.state in {"valid", "empty"}


def _is_structural_barrier(line: str) -> bool:
    return any(
        pattern.match(line) is not None
        for pattern in (
            _PHONE_LINE_RE,
            _CASH_HEADER_RE,
            _CARD_PAYMENT_HEADER_RE,
            _MONEY_LINE_RE,
            _DELIVERY_LINE_RE,
        )
    )


def parse_caption_phone_field(value: object) -> CaptionPhoneField:
    """Locate and validate only the canonical ``📞:`` field.

    Supplier/price text may span multiple lines between ``🛒💵:`` and
    ``rasxod:``. Structural ambiguity remains fail-closed so phone-like prices,
    expenses, payment values, IMEIs, and notes are never used for matching.
    """

    caption = str(value or "")
    lines = _line_spans(caption)
    header_indexes = [
        index
        for index, (_, _, line) in enumerate(lines)
        if _CARD_HEADER_RE.match(line) is not None
    ]
    if len(header_indexes) != 1:
        return CaptionPhoneField("malformed")
    header_index = header_indexes[0]

    expense_indexes = [
        index
        for index, (_, _, line) in enumerate(lines)
        if _EXPENSE_LINE_RE.match(line) is not None
    ]
    phone_indexes = [
        index
        for index, (_, _, line) in enumerate(lines)
        if _PHONE_LINE_RE.match(line) is not None
    ]
    if len(expense_indexes) != 1 or len(phone_indexes) != 1:
        return CaptionPhoneField("malformed")
    expense_index = expense_indexes[0]
    phone_index = phone_indexes[0]
    if not header_index < expense_index < phone_index:
        return CaptionPhoneField("malformed")
    if any(
        _is_structural_barrier(lines[index][2])
        for index in range(header_index + 1, expense_index)
    ):
        return CaptionPhoneField("malformed")

    first_nonblank_after_expense = next(
        (
            index
            for index in range(expense_index + 1, len(lines))
            if lines[index][2].strip(" \t\u2063\u2064\ufeff")
        ),
        None,
    )
    if first_nonblank_after_expense != phone_index:
        return CaptionPhoneField("malformed")

    start, end, line = lines[phone_index]
    match = _PHONE_LINE_RE.match(line)
    if match is None:
        return CaptionPhoneField("malformed")
    field_value = caption[start + match.end() : end]
    stripped = field_value.strip()
    base = CaptionPhoneField(
        "empty",
        start=start,
        end=end,
        prefix=match.group("prefix"),
        value=field_value,
    )
    if not stripped or stripped.casefold() in _NO_PHONE_VALUES:
        return base

    phones, accepted_spans = _scan_uzbek_phones(field_value, stop_after=3)
    if not phones or len(phones) > 2:
        return CaptionPhoneField(
            "invalid",
            start=start,
            end=end,
            prefix=match.group("prefix"),
            value=field_value,
        )
    residual = []
    previous_end = 0
    for span_start, span_end in accepted_spans:
        residual.append(field_value[previous_end:span_start])
        previous_end = span_end
    residual.append(field_value[previous_end:])
    if _PHONE_FIELD_SEPARATORS_RE.fullmatch("".join(residual)) is None:
        return CaptionPhoneField(
            "invalid",
            start=start,
            end=end,
            prefix=match.group("prefix"),
            value=field_value,
        )
    return CaptionPhoneField(
        "valid",
        phones=phones,
        start=start,
        end=end,
        prefix=match.group("prefix"),
        value=field_value,
    )


def extract_caption_phones(value: object) -> tuple[str, ...]:
    """Extract phones only from a structurally valid generated card."""

    parsed = parse_caption_phone_field(value)
    return parsed.phones if parsed.state == "valid" else ()


def extract_product_label(value: object, limit: int = 120) -> str | None:
    """Return manually typed product text with phone numbers removed.

    A long number-only value is treated as a telephone/identifier rather than
    a product model. Short numeric model names (for example ``16``) remain
    valid product labels.
    """

    raw = str(value or "").strip()
    if not raw:
        return None

    _, phone_spans = _scan_uzbek_phones(raw, stop_after=20)
    residual: list[str] = []
    previous_end = 0
    for start, end in phone_spans:
        residual.append(raw[previous_end:start])
        previous_end = end
    residual.append(raw[previous_end:])
    compact = " ".join("".join(residual).split())
    compact = _PRODUCT_TRIM_RE.sub("", compact).strip()
    if compact.casefold().rstrip(":") in _PHONE_ONLY_LABELS:
        return None

    digits = "".join(character for character in compact if character.isdigit())
    if len(digits) >= 7 and not any(character.isalpha() for character in compact):
        return None
    if not compact:
        return None

    maximum = max(1, int(limit))
    if len(compact) <= maximum:
        return compact
    return compact[: maximum - 1].rstrip() + "…"


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


@dataclass(frozen=True)
class PhoneCaptionNormalization:
    caption: str
    entities: tuple[MessageEntity, ...]
    changed: bool = False


def normalize_caption_phone_field(
    caption: object,
    entities: Sequence[MessageEntity] | None = None,
    *,
    max_length: int = 1024,
) -> PhoneCaptionNormalization:
    """Normalize the one exact ``📞:`` field and preserve all other entities.

    Entity offsets received from Telegram use UTF-16 units. Entities fully inside
    the replaced phone row are intentionally removed; entities crossing the row
    boundary make the operation fail closed so formatting elsewhere is untouched.
    """

    original = str(caption or "")
    original_entities = tuple(entities or ())
    parsed = parse_caption_phone_field(original)
    if parsed.state in {"malformed", "invalid"}:
        return PhoneCaptionNormalization(original, original_entities)
    if parsed.start is None or parsed.end is None:
        return PhoneCaptionNormalization(original, original_entities)
    start, end = parsed.start, parsed.end
    if parsed.state == "empty" and parsed.value.strip():
        # Keep an intentional no-phone placeholder exactly as the manager typed it.
        return PhoneCaptionNormalization(original, original_entities)
    replacement = parsed.prefix + phone_line(parsed.phones)
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
    if len(normalized_caption) > max(1, int(max_length)):
        return PhoneCaptionNormalization(original, original_entities)
    return PhoneCaptionNormalization(
        normalized_caption,
        tuple(shifted),
        changed=True,
    )
