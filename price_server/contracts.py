from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo


SECTION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
MAX_SECTIONS = 500
MAX_PRODUCTS = 100_000


class ContractError(ValueError):
    pass


def canonical_content_hash(payload: dict[str, Any]) -> str:
    content = {
        "html_document": payload.get("html_document", ""),
        "products": payload.get("products", []),
        "sections": payload.get("sections", []),
    }
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def section_content_hash(section: dict[str, Any]) -> str:
    content = {
        "title": section.get("title", ""),
        "plain_text": section.get("plain_text", ""),
        "clipboard_html": section.get("clipboard_html", ""),
        "telegram_blocks": section.get("telegram_blocks", []),
    }
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field} must include a timezone offset")
    return parsed


def _fallback_telegram_html(text: str) -> str:
    lines = text.splitlines()
    rendered: list[str] = []
    for line in lines:
        stripped = line.strip()
        if (
            len(stripped) >= 4
            and stripped.startswith("**")
            and stripped.endswith("**")
        ):
            rendered.append(f"<b>{escape(stripped[2:-2])}</b>")
        else:
            rendered.append(escape(line))
    return "\n".join(rendered)


def validate_sync_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("request body must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ContractError("unsupported schema_version")

    generated_at = _aware_datetime(payload.get("generated_at"), "generated_at")
    timezone_name = str(payload.get("timezone") or "").strip()
    if not timezone_name:
        raise ContractError("timezone is required")
    try:
        ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise ContractError("timezone must be a valid IANA timezone") from exc

    html_document = payload.get("html_document")
    if not isinstance(html_document, str) or not html_document.strip():
        raise ContractError("html_document is required")
    if "<html" not in html_document[:1000].casefold():
        raise ContractError("html_document is not a complete HTML page")

    products = payload.get("products")
    if not isinstance(products, list) or len(products) > MAX_PRODUCTS:
        raise ContractError("products must be a bounded JSON array")
    if any(not isinstance(product, dict) for product in products):
        raise ContractError("every product must be a JSON object")

    sections = payload.get("sections")
    if (
        not isinstance(sections, list)
        or not sections
        or len(sections) > MAX_SECTIONS
    ):
        raise ContractError("sections must be a non-empty bounded JSON array")

    normalized_sections: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, source in enumerate(sections):
        if not isinstance(source, dict):
            raise ContractError(f"sections[{index}] must be an object")
        section = dict(source)
        key = str(
            section.get("section_key") or section.get("section_id") or ""
        ).strip()
        if not SECTION_KEY_RE.fullmatch(key):
            raise ContractError(f"sections[{index}].section_key is invalid")
        if key in seen_keys:
            raise ContractError(f"duplicate section_key: {key}")
        seen_keys.add(key)

        title = str(section.get("title") or "").strip()
        plain_text = section.get("plain_text")
        if not title:
            raise ContractError(f"sections[{index}].title is required")
        if not isinstance(plain_text, str) or not plain_text.strip():
            raise ContractError(f"sections[{index}].plain_text is required")

        clipboard_html = section.get("clipboard_html", "")
        if not isinstance(clipboard_html, str):
            raise ContractError(
                f"sections[{index}].clipboard_html must be text"
            )
        blocks = section.get("telegram_blocks")
        if blocks is None:
            blocks = [_fallback_telegram_html(plain_text)]
        if (
            not isinstance(blocks, list)
            or not blocks
            or any(not isinstance(block, str) or not block.strip() for block in blocks)
        ):
            raise ContractError(
                f"sections[{index}].telegram_blocks must contain text"
            )

        section.update(
            {
                "section_key": key,
                "title": title,
                "plain_text": plain_text,
                "clipboard_html": clipboard_html,
                "telegram_blocks": blocks,
                "changed_recently": bool(section.get("changed_recently", False)),
            }
        )
        expected_section_hash = section_content_hash(section)
        supplied_section_hash = str(section.get("content_hash") or "")
        if supplied_section_hash and supplied_section_hash != expected_section_hash:
            raise ContractError(
                f"sections[{index}].content_hash does not match its content"
            )
        section["content_hash"] = expected_section_hash
        normalized_sections.append(section)

    normalized = dict(payload)
    normalized.update(
        {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "timezone": timezone_name,
            "html_document": html_document,
            "products": products,
            "sections": normalized_sections,
        }
    )
    expected_hash = canonical_content_hash(normalized)
    supplied_hash = str(payload.get("content_sha256") or "")
    if supplied_hash and supplied_hash != expected_hash:
        raise ContractError("content_sha256 does not match payload content")
    normalized["content_sha256"] = expected_hash
    normalized["snapshot_id"] = expected_hash
    return normalized
