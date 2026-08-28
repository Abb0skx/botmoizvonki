"""Server-side canonical formatting for Telegram price posts.

The Mac snapshot remains the source of products and prices.  The server
normalizes only the Telegram presentation immediately before send/edit so an
older snapshot renderer cannot re-introduce obsolete contact links or markers.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from html.parser import HTMLParser

from .telegram_api import (
    TELEGRAM_CHUNK_TARGET,
    TELEGRAM_MESSAGE_LIMIT,
    split_telegram_blocks,
    telegram_text_units,
)


PRICE_INFO_URL = "https://texnikach.uz/go"
PRICE_INFO_UZ_HTML = (
    f'<a href="{PRICE_INFO_URL}">ⓘ Do‘kon · Aloqa · Yetkazish</a>'
)
PRICE_INFO_RU_HTML = (
    f'<a href="{PRICE_INFO_URL}">ⓘ Магазин · Связь · Доставка</a>'
)

_MEMORY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<first>\d+)\s*/\s*(?P<second>\d+)\s*"
    r"(?P<unit>[GgTt][Bb])\b"
)
_PRICE_RE = re.compile(r"^\d[\d\s.,]*(?:\s*[↑↓])?$")
_VARIANT_RE = re.compile(
    r"^(?P<bullet>\s*•\s*)(?P<memory>\d+/\d+\s+(?:GB|TB))"
    r"(?:\s+(?P<details>.*?))?\s*$"
)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTIPLE_BLANKS_RE = re.compile(r"\n{3,}")
_INFO_UZ_RE = re.compile(
    r"^(?:📌|ⓘ)\s*do[‘’']kon\s*(?:\||│|·)\s*(?:📞\s*)?aloqa"
    r"\s*(?:\||│|·)\s*(?:📦\s*)?yetkazish$",
    re.IGNORECASE,
)
_INFO_RU_RE = re.compile(
    r"^(?:📌|ⓘ)\s*магазин\s*(?:\||│|·)\s*(?:📞\s*)?связь"
    r"\s*(?:\||│|·)\s*(?:📦\s*)?доставка$",
    re.IGNORECASE,
)

_TITLE_PREFIXES = (
    "Автомобильные зарядные устройства",
    "Моментальные фотоаппараты",
    "Спортивные наушники",
    "Фитнес-браслеты",
    "Наушники и колонки",
    "Детские часы",
    "Умные кольца",
    "Умные очки",
    "Кнопочные телефоны",
    "VR-очки",
    "Телефоны",
    "Планшеты",
    "Наушники",
    "Колонки",
    "Часы",
    "Микрофоны",
    "Диктофоны",
    "Техника",
    "Камеры",
)


def _visible_line(value: str) -> str:
    return html.unescape(_TAG_RE.sub("", str(value))).strip()


def _is_information_line(value: str) -> bool:
    visible = re.sub(r"\s+", " ", _visible_line(value)).strip()
    return bool(_INFO_UZ_RE.fullmatch(visible) or _INFO_RU_RE.fullmatch(visible))


def _is_source_title(value: str, section_key: str, title: str) -> bool:
    visible = _visible_line(value).strip("* ")
    if not visible:
        return False
    expected = str(title).strip()
    expected_heading = _heading_text(section_key, title)
    bracketed = re.fullmatch(r"【\s*(.*?)\s*】", visible)
    if bracketed is not None:
        return bracketed.group(1).strip().casefold() in {
            expected.casefold(),
            expected_heading.casefold(),
        }
    if visible.startswith("━━") and visible.endswith("━━"):
        normalized = visible.strip("━ ").strip()
        return normalized.casefold() == expected_heading.casefold()
    if visible.startswith("▰"):
        normalized = visible.lstrip("▰ ").strip()
        return normalized.casefold() in {
            expected.casefold(),
            expected_heading.casefold(),
        }
    normalized = visible.strip("━▰ ").strip()
    return normalized.casefold() == expected.casefold()


def _without_title_prefix(source: str, prefix: str) -> str:
    remainder = source[len(prefix):].lstrip()
    if remainder.startswith(("·", "—", "-")):
        remainder = remainder[1:].lstrip()
    return remainder


def _heading_text(section_key: str, title: str) -> str:
    source = str(title).strip()
    key = str(section_key).strip()
    if key.startswith("smartphones-"):
        if key == "smartphones-keypad":
            return "ТЕЛЕФОНЫ · КНОПОЧНЫЕ"
        if source.casefold().startswith("телефоны "):
            source = _without_title_prefix(source, "Телефоны")
        return f"ТЕЛЕФОНЫ · {source.upper()}"
    if key.startswith("tablets-"):
        if source.casefold().startswith("планшеты "):
            source = _without_title_prefix(source, "Планшеты")
        return f"ПЛАНШЕТЫ · {source.upper()}"

    source_folded = source.casefold()
    for prefix in _TITLE_PREFIXES:
        marker = prefix + " "
        if source_folded.startswith(marker.casefold()):
            detail = _without_title_prefix(source, prefix)
            if detail:
                return f"{prefix.upper()} · {detail.upper()}"
    return source.upper()


def _format_content_line(value: str) -> str:
    def normalize_memory(match: re.Match[str]) -> str:
        unit = match.group("unit").upper()
        return f"{match.group('first')}/{match.group('second')} {unit}"

    line = _transform_fragment_data(
        str(value),
        lambda part: _MEMORY_RE.sub(
            normalize_memory,
            part.replace("🔺", "↑").replace("🔻", "↓"),
        ),
    )
    if ":" not in line:
        return line.rstrip()
    label, candidate = line.rsplit(":", 1)
    price = candidate.strip()
    if not _PRICE_RE.fullmatch(price):
        return line.rstrip()
    variant = _VARIANT_RE.fullmatch(label)
    if variant is not None:
        details = str(variant.group("details") or "").strip()
        suffix = f" · {details}" if details else ""
        return (
            f"{variant.group('bullet')}{variant.group('memory')}"
            f"{suffix} — {price}"
        ).strip()
    return f"{label.rstrip()} — {price}"


def _clean_source_block(value: str, section_key: str, title: str) -> str:
    lines = str(value).strip().splitlines()
    cleaned: list[str] = []
    title_removed = False
    for line in lines:
        if _is_information_line(line):
            continue
        if not title_removed and _is_source_title(line, section_key, title):
            title_removed = True
            continue
        cleaned.append(_format_content_line(line))
    result = "\n".join(cleaned).strip()
    return _MULTIPLE_BLANKS_RE.sub("\n\n", result)


class _FragmentTokenizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tokens: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tokens.append(("start", self.get_starttag_text(), tag.casefold()))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.tokens.append(("raw", self.get_starttag_text(), ""))

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(("end", f"</{tag}>", tag.casefold()))

    def handle_data(self, data: str) -> None:
        if data:
            self.tokens.append(("data", data, ""))

    def handle_entityref(self, name: str) -> None:
        self.tokens.append(("entity", f"&{name};", ""))

    def handle_charref(self, name: str) -> None:
        self.tokens.append(("entity", f"&#{name};", ""))

    def handle_comment(self, data: str) -> None:
        self.tokens.append(("raw", f"<!--{data}-->", ""))


def _transform_fragment_data(
    value: str,
    transform: Callable[[str], str],
) -> str:
    parser = _FragmentTokenizer()
    parser.feed(str(value))
    parser.close()
    return "".join(
        transform(raw) if kind == "data" else raw
        for kind, raw, _tag in parser.tokens
    )


def _text_prefix_for_units(value: str, limit: int) -> tuple[str, str]:
    used = 0
    end = 0
    for index, character in enumerate(value):
        units = len(character.encode("utf-16-le")) // 2
        if used + units > limit:
            break
        used += units
        end = index + 1
    return value[:end], value[end:]


def _split_html_fragment(value: str, *, max_units: int) -> list[str]:
    """Split an HTML fragment while closing/reopening active entities."""

    if max_units < 1:
        raise ValueError("max_units must be positive")
    parser = _FragmentTokenizer()
    parser.feed(str(value))
    parser.close()
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    stack: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal current, current_units
        if current_units <= 0:
            return
        closing = "".join(f"</{tag}>" for tag, _opening in reversed(stack))
        chunks.append("".join(current) + closing)
        current = [opening for _tag, opening in stack]
        current_units = 0

    for kind, raw, tag in parser.tokens:
        if kind == "start":
            if current_units >= max_units:
                flush()
            current.append(raw)
            stack.append((tag, raw))
            continue
        if kind == "end":
            current.append(raw)
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag:
                    del stack[index:]
                    break
            continue
        if kind == "raw":
            current.append(raw)
            continue
        if kind == "entity":
            units = telegram_text_units(raw)
            if current_units and current_units + units > max_units:
                flush()
            current.append(raw)
            current_units += units
            continue

        remaining = raw
        while remaining:
            capacity = max_units - current_units
            if capacity <= 0:
                flush()
                capacity = max_units
            prefix, remaining = _text_prefix_for_units(remaining, capacity)
            if not prefix:
                raise ValueError("one character exceeds the Telegram limit")
            current.append(prefix)
            current_units += telegram_text_units(prefix)
            if remaining:
                flush()

    flush()
    if not chunks and str(value).strip():
        chunks.append(str(value).strip())
    if any(telegram_text_units(chunk) > max_units for chunk in chunks):
        raise AssertionError("HTML fragment splitter produced an oversized chunk")
    return chunks


def _section_body_blocks(
    section_key: str,
    title: str,
    raw_blocks: Sequence[str],
    *,
    max_body_units: int,
) -> list[str]:
    heading = (
        "<b>━━ "
        + html.escape(_heading_text(section_key, title), quote=False)
        + " ━━</b>"
    )
    cleaned = [
        block
        for raw in raw_blocks
        if (block := _clean_source_block(str(raw), section_key, title))
    ]
    content = "\n\n".join(cleaned)
    body = heading + (f"\n\n{content}" if content else "")
    if telegram_text_units(body) <= max_body_units:
        return [body]

    semantic = [
        part.strip()
        for part in re.split(r"\n{2,}", content)
        if part.strip()
    ]
    if not semantic:
        return [heading]
    heading_units = telegram_text_units(heading + "\n\n")
    content_limit = max(1, max_body_units - heading_units)
    content_target = min(
        max(1, TELEGRAM_CHUNK_TARGET - heading_units),
        content_limit,
    )
    safe_semantic: list[str] = []
    for block in semantic:
        if telegram_text_units(block) <= content_limit:
            safe_semantic.append(block)
        else:
            safe_semantic.extend(
                _split_html_fragment(block, max_units=content_limit)
            )
    chunks = split_telegram_blocks(
        safe_semantic,
        target_units=content_target,
        max_units=content_limit,
    )
    return [f"{heading}\n\n{chunk}" for chunk in chunks]


def format_price_sections(
    sections: Sequence[tuple[str, str, Sequence[str]]],
) -> list[str]:
    """Return canonical Telegram HTML for one or more bound sections."""

    if not sections:
        raise ValueError("at least one price section is required")
    wrapper_units = telegram_text_units(
        PRICE_INFO_UZ_HTML + "\n\n\n\n" + PRICE_INFO_RU_HTML
    )
    max_body_units = TELEGRAM_MESSAGE_LIMIT - wrapper_units
    target_body_units = min(
        TELEGRAM_CHUNK_TARGET - wrapper_units,
        max_body_units,
    )
    bodies: list[str] = []
    for section_key, title, raw_blocks in sections:
        bodies.extend(
            _section_body_blocks(
                str(section_key),
                str(title),
                raw_blocks,
                max_body_units=max_body_units,
            )
        )
    chunks = split_telegram_blocks(
        bodies,
        target_units=max(1, target_body_units),
        max_units=max_body_units,
    )
    result = [
        f"{PRICE_INFO_UZ_HTML}\n\n{chunk}\n\n{PRICE_INFO_RU_HTML}"
        for chunk in chunks
    ]
    if any(telegram_text_units(item) > TELEGRAM_MESSAGE_LIMIT for item in result):
        raise AssertionError("formatted price post exceeds Telegram limit")
    return result
