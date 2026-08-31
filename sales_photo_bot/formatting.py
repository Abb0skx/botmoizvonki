from __future__ import annotations

import html
import re

from .models import Recognition


MAX_CLIENT_TEXT = 300
MAX_PRODUCT_FIELD = 120
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
    recognition: Recognition,
    manager: str | None = None,
) -> str:
    """Build the sales template while omitting unavailable product fields."""

    lines: list[str] = []
    client = _safe(client_caption, MAX_CLIENT_TEXT)
    if client:
        lines.append(f"📞 Клиент: {client}")

    model = _safe(recognition.model_name, MAX_PRODUCT_FIELD)
    memory = _safe(recognition.memory, MAX_PRODUCT_FIELD)
    color = _safe(recognition.color, MAX_PRODUCT_FIELD)
    if model:
        lines.append(f"📦 {model}")
    if memory:
        lines.append(f"💾 {memory}")
    if color:
        lines.append(f"🎨 {color}")

    if lines:
        lines.append("")
    lines.extend(
        [
            "🛒💵:",
            "rasxod:",
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
