"""Telegram-backed drafts for safe catalogue changes.

Messages never change the live catalogue directly. They become one reviewable
draft per model on ``/price/models``. The parser accepts both the established
Model Yegish numbered format and the simpler labelled site format.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from .price_entry import EntryError, now

MAX_TEXT = 12_000
MAX_MODELS = 100
MAX_VARIANTS = 500
_PREFIX = re.compile(r"^(?:добавить|новая)\s+модель\s*:?\s*$", re.I)
_CATEGORY = re.compile(r"^категория\s*:\s*(.+)$", re.I)
_MODEL = re.compile(r"^модель\s*:\s*(.+)$", re.I)
_NUMBERED = re.compile(r"^\s*([0123])\s*[.)]\s*(.*?)\s*$")


def _tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS entry_model_inbox(
        draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_chat_id TEXT NOT NULL,
        source_message_id INTEGER NOT NULL,
        model_index INTEGER NOT NULL,
        source_update_id INTEGER NOT NULL,
        raw_text TEXT NOT NULL,
        parsed_json TEXT,
        parse_error TEXT,
        status TEXT NOT NULL CHECK(status IN ('draft','invalid','applied','dismissed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(source_chat_id,source_message_id,model_index)
    )""")


def _text(value: str, maximum: int) -> str:
    value = " ".join(unicodedata.normalize("NFC", str(value)).split())
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise EntryError("invalid_model_draft")
    return value


def _values(value: str, maximum: int) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in value.replace(";", ",").replace("，", ",").split(","):
        item = _text(raw, maximum)
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            result.append(item)
    return tuple(result) or ("",)


def _variants(memories: tuple[str, ...], colors: tuple[str, ...]) -> list[dict]:
    real_colors = tuple(color for color in colors if color)
    # Keep the established Model Yegish result: multiple colors also create
    # one generic blank-color variant for every memory value.
    materialized_colors = real_colors + (("",) if len(real_colors) > 1 else ())
    if not materialized_colors:
        materialized_colors = ("",)
    result = [
        {"memory": memory, "color": color}
        for memory in memories
        for color in materialized_colors
    ]
    if not 1 <= len(result) <= MAX_VARIANTS:
        raise EntryError("invalid_catalog_variants")
    return result


def _parse_numbered(lines: list[str]) -> list[dict]:
    models: list[dict] = []
    active_category: int | None = None
    current: dict | None = None
    seen: set[str] = set()

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        memories = _values(current.pop("memory_text", ""), 100)
        colors = _values(current.pop("color_text", ""), 150)
        current["variants"] = _variants(memories, colors)
        models.append(current)
        if len(models) > MAX_MODELS:
            raise EntryError("invalid_catalog_variants")
        current = None

    for line in lines:
        match = _NUMBERED.fullmatch(line)
        if match is None:
            raise EntryError("model_draft_numbered_format")
        prefix, value = int(match.group(1)), _text(match.group(2), 250)
        if prefix == 0:
            finish()
            if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
                raise EntryError("model_draft_category_format")
            active_category = int(value)
        elif prefix == 1:
            finish()
            if not value or value.casefold() in seen:
                raise EntryError("model_draft_model_format")
            seen.add(value.casefold())
            current = {
                "category_id": active_category,
                "category_name": "",
                "model_name": value,
            }
        elif current is None:
            raise EntryError("model_draft_model_format")
        elif prefix == 2:
            if "memory_text" in current or "color_text" in current:
                raise EntryError("model_draft_numbered_format")
            current["memory_text"] = value
        else:
            if "color_text" in current:
                raise EntryError("model_draft_numbered_format")
            current["color_text"] = value
    finish()
    if not models:
        raise EntryError("model_draft_model_format")
    return models


def _parse_labelled(lines: list[str]) -> list[dict]:
    if lines and _PREFIX.fullmatch(lines[0]):
        lines.pop(0)
    category_name = model_name = None
    remaining = []
    for line in lines:
        category = _CATEGORY.fullmatch(line)
        model = _MODEL.fullmatch(line)
        if category and category_name is None:
            category_name = _text(category.group(1), 200)
        elif model and model_name is None:
            model_name = _text(model.group(1), 250)
        else:
            remaining.append(line)
    if category_name is None and model_name is None and len(remaining) >= 3:
        category_name, model_name = remaining.pop(0), remaining.pop(0)
    if not category_name or not model_name:
        raise EntryError("model_draft_headers_required")
    variants: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in remaining:
        line = re.sub(r"^[•·▪️\-–—]+\s*", "", line).strip()
        if line.casefold() in {"память | цвет", "memory | color"}:
            continue
        if line.count("|") != 1:
            raise EntryError("model_draft_variant_format")
        memory, colors = (part.strip() for part in line.split("|", 1))
        for color in _values(colors, 150):
            item = {"memory": _text(memory, 100), "color": color}
            marker = (item["memory"].casefold(), item["color"].casefold())
            if marker in seen:
                raise EntryError("duplicate_catalog_variant", 409)
            seen.add(marker)
            variants.append(item)
    if not 1 <= len(variants) <= MAX_VARIANTS:
        raise EntryError("invalid_catalog_variants")
    return [{
        "category_id": None,
        "category_name": category_name,
        "model_name": model_name,
        "variants": variants,
    }]


def parse_model_drafts(raw_text: str) -> list[dict]:
    if not isinstance(raw_text, str) or not raw_text.strip() or len(raw_text) > MAX_TEXT:
        raise EntryError("invalid_model_draft")
    lines = [line.strip() for line in raw_text.replace("\r", "").split("\n") if line.strip()]
    if lines and _NUMBERED.fullmatch(lines[0]):
        return _parse_numbered(lines)
    return _parse_labelled(lines)


def parse_model_draft(raw_text: str) -> dict:
    """Compatibility helper for callers expecting one model."""
    models = parse_model_drafts(raw_text)
    if len(models) != 1:
        raise EntryError("model_draft_multiple_models")
    return models[0]


class ModelInbox:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def record(self, *, update_id: int, chat_id: str, message_id: int,
               text: str) -> dict:
        if update_id < 0 or message_id <= 0 or not chat_id:
            raise EntryError("invalid_model_draft")
        parsed_models: list[dict] = []
        error = None
        try:
            parsed_models = parse_model_drafts(text)
            status = "draft"
        except EntryError as exc:
            status = "invalid"
            error = exc.code
        entries = parsed_models or [None]
        timestamp = now()
        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            _tables(db)
            previous = list(db.execute(
                """SELECT draft_id,status FROM entry_model_inbox
                WHERE source_chat_id=? AND source_message_id=?""",
                (str(chat_id), int(message_id)),
            ))
            locked = next((row for row in previous
                           if row["status"] in {"applied", "dismissed"}), None)
            if locked:
                return {"status": locked["status"], "draft_id": locked["draft_id"]}
            draft_ids = []
            for index, parsed in enumerate(entries):
                db.execute("""INSERT INTO entry_model_inbox(
                    source_chat_id,source_message_id,model_index,source_update_id,raw_text,
                    parsed_json,parse_error,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_chat_id,source_message_id,model_index) DO UPDATE SET
                    source_update_id=excluded.source_update_id,raw_text=excluded.raw_text,
                    parsed_json=excluded.parsed_json,parse_error=excluded.parse_error,
                    status=excluded.status,updated_at=excluded.updated_at""", (
                        str(chat_id), int(message_id), index, int(update_id), text,
                        json.dumps(parsed, ensure_ascii=False) if parsed else None,
                        error, status, timestamp, timestamp,
                    ))
                row = db.execute("""SELECT draft_id FROM entry_model_inbox
                    WHERE source_chat_id=? AND source_message_id=? AND model_index=?""",
                    (str(chat_id), int(message_id), index)).fetchone()
                draft_ids.append(int(row["draft_id"]))
            db.execute("""DELETE FROM entry_model_inbox
                WHERE source_chat_id=? AND source_message_id=? AND model_index>=?
                  AND status IN ('draft','invalid')""",
                (str(chat_id), int(message_id), len(entries)))
            return {"status": status, "draft_id": draft_ids[0],
                    "draft_ids": draft_ids, "parse_error": error}

    def list(self, *, limit: int = 100) -> dict:
        limit = min(100, max(1, int(limit)))
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            _tables(db)
            rows = []
            for row in db.execute("""SELECT * FROM entry_model_inbox
                WHERE status IN ('draft','invalid')
                ORDER BY updated_at DESC,draft_id DESC LIMIT ?""", (limit,)):
                item = dict(row)
                parsed_json = item.pop("parsed_json")
                item["parsed"] = json.loads(parsed_json) if parsed_json else None
                rows.append(item)
            return {"drafts": rows}

    def finish(self, draft_id: int, *, status: str) -> dict:
        if status not in {"applied", "dismissed"}:
            raise EntryError("invalid_model_draft_status")
        if type(draft_id) is not int or draft_id <= 0:
            raise EntryError("invalid_model_draft")
        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            _tables(db)
            cursor = db.execute("""UPDATE entry_model_inbox SET status=?,updated_at=?
                WHERE draft_id=? AND status IN ('draft','invalid')""",
                (status, now(), draft_id))
            if cursor.rowcount != 1:
                raise EntryError("model_draft_not_found", 404)
            return {"status": status, "draft_id": draft_id}
