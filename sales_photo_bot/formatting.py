from __future__ import annotations

import html
import re
from datetime import date

from .models import ProductIdentifiers
from .phones import extract_uzbek_phones, phone_line


MAX_SERIAL_NUMBER = 64
MANAGER_LINE_RE = re.compile(
    r"(?:\n\n)?👤 Менеджер: <b>[^<>\r\n]{1,64}</b>\s*\Z"
)
MANAGER_NAME_RE = re.compile(
    r"(?:^|\n)👤 Менеджер: <b>([^<>\r\n]{1,64})</b>\s*\Z"
)


def _compact(value: object, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def _safe(value: object, limit: int) -> str:
    return html.escape(_compact(value, limit), quote=True)


def build_caption(
    client_caption: str | None,
    identifiers: ProductIdentifiers,
    manager: str | None = None,
    product_label: str | None = None,
    sale_date: date | None = None,
) -> str:
    """Build the sales card, retaining only verified phones and identifiers."""

    phones = extract_uzbek_phones(client_caption)
    lines: list[str] = []
    if sale_date:
        lines.extend([f"📆: {sale_date:%d/%m/%Y}", ""])
    if product_label:
        lines.extend([f"📦 {_safe(product_label, 120)}", ""])
    lines.extend([
        "🛒💵:",
        "rasxod:",
        "",
        phone_line(phones),
    ])

    identifier_lines: list[str] = []
    if identifiers.imei:
        identifier_lines.append(
            f"<blockquote>IMEI: {_safe(identifiers.imei, 15)}</blockquote>"
        )
    if identifiers.imei2:
        identifier_lines.append(
            f"<blockquote>IMEI2: {_safe(identifiers.imei2, 15)}</blockquote>"
        )
    if identifiers.serial_number:
        identifier_lines.append(
            f"<blockquote>S/N: {_safe(identifiers.serial_number, MAX_SERIAL_NUMBER)}"
            "</blockquote>"
        )
    if identifier_lines:
        lines.extend(["", *identifier_lines])

    lines.extend(
        [
            "",
            "<b>Наличка</b>",
            "💵:",
            "🇺🇿:",
            "",
            "<b>Card/Terminal/Paynet</b>",
            "💵:",
            "🇺🇿:",
        ]
    )
    if manager:
        lines.extend(["", f"👤 Менеджер: <b>{_safe(manager, 64)}</b>"])
    return "\n".join(lines)


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
