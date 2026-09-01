from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


TASHKENT_TZ = timezone(timedelta(hours=5))
_SALE_DATE_RE = re.compile(
    r"(?<![\w/])"
    r"(?P<day>\d{1,2})/(?P<month>\d{1,2})"
    r"(?:/(?P<year>\d{4}))?"
    r"(?![\w/])"
)


@dataclass(frozen=True)
class SaleDateMatch:
    value: date
    start: int
    end: int


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

