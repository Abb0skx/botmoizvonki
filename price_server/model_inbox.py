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
_NUMBERED = re.compile(r"^\s*([0123])\s*[.)：:．）]\s*(.*?)\s*$")
_SEPARATORS = ",;，；،"
_BULLET = re.compile(r"^(?:[•·▪●◦*]|[-–—])\s+(.+)$")


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


def _text(value: str, maximum: int, **location) -> str:
    value = " ".join(unicodedata.normalize("NFC", str(value)).split())
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        field = location.get("field", "Значение")
        raise EntryError("invalid_model_draft", **location, limit=maximum,
                         message=f"Поле «{field}» должно содержать не более {maximum} символов "
                                 "и не должно содержать управляющих символов.")
    return value


def _values(value: str, maximum: int, **location) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in re.split("[" + _SEPARATORS + "]", value):
        item = _text(raw, maximum, **location)
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
    if len(memories) * len(materialized_colors) > MAX_VARIANTS:
        raise EntryError("invalid_catalog_variants", limit=MAX_VARIANTS,
                         message=f"У одной модели может быть не более {MAX_VARIANTS} конфигураций.")
    result = [
        {"memory": memory, "color": color}
        for memory in memories
        for color in materialized_colors
    ]
    if not 1 <= len(result) <= MAX_VARIANTS:
        raise EntryError("invalid_catalog_variants")
    return result


def _parse_numbered(lines: list[tuple[int, str]]) -> list[dict]:
    models: list[dict] = []
    active_category: int | None = None
    current: dict | None = None
    seen: set[str] = set()
    field_lines: dict[int, int] = {}
    last_field: int | None = None
    separate_lines = False

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        memories = _values(current.pop("memory_text", ""), 100,
                           field="Память / размер", line=field_lines.get(2, field_lines[1]))
        colors = _values(current.pop("color_text", ""), 150,
                         field="Цвета", line=field_lines.get(3, field_lines[1]))
        current["variants"] = _variants(memories, colors)
        models.append(current)
        if len(models) > MAX_MODELS:
            raise EntryError("invalid_catalog_variants")
        current = None

    for line_number, line in lines:
        match = _NUMBERED.fullmatch(line)
        if match is None:
            bullet = _BULLET.fullmatch(line)
            key = "memory_text" if last_field == 2 else "color_text"
            if current is not None and last_field in (2, 3):
                previous = current[key]
                # Continuations must be explicit: a list starting on its own
                # line, a bullet, or a separator at the end of the prior line.
                if separate_lines or bullet or previous.endswith(tuple(_SEPARATORS)):
                    value = bullet.group(1) if bullet else line
                    separator = ", " if previous and not previous.endswith(tuple(_SEPARATORS)) else " "
                    current[key] += separator + value
                    continue
            raise EntryError("model_draft_numbered_format", line=line_number,
                             message="Ожидается строка 0, 1, 2 или 3. Продолжайте список "
                                     "после запятой или начните строку с маркера •.")
        prefix = int(match.group(1))
        # Memory/color lines are lists, not single names. Validate their
        # individual values in finish(), keeping the overall draft bound.
        fields = {0: "Категория", 1: "Модель", 2: "Память / размер", 3: "Цвета"}
        value = _text(match.group(2), 250 if prefix in (0, 1) else MAX_TEXT,
                      field=fields[prefix], line=line_number)
        if prefix == 0:
            finish()
            if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
                raise EntryError("model_draft_category_format", line=line_number)
            active_category = int(value)
        elif prefix == 1:
            finish()
            if not value or value.casefold() in seen:
                raise EntryError("model_draft_model_format", line=line_number,
                                 message="Укажите непустое уникальное название модели после 1.")
            seen.add(value.casefold())
            current = {
                "category_id": active_category,
                "category_name": "",
                "model_name": value,
            }
            field_lines = {1: line_number}
        elif current is None:
            raise EntryError("model_draft_model_format", line=line_number)
        elif prefix == 2:
            if "memory_text" in current or "color_text" in current:
                raise EntryError("model_draft_numbered_format", line=line_number)
            current["memory_text"] = value
        else:
            if "color_text" in current:
                raise EntryError("model_draft_numbered_format", line=line_number)
            current["color_text"] = value
        field_lines[prefix] = line_number
        last_field = prefix
        separate_lines = not value
    finish()
    if not models:
        raise EntryError("model_draft_model_format")
    return models


def _parse_labelled(lines: list[tuple[int, str]]) -> list[dict]:
    if lines and _PREFIX.fullmatch(lines[0][1]):
        lines.pop(0)
    category_name = model_name = None
    remaining = []
    for line_number, line in lines:
        category = _CATEGORY.fullmatch(line)
        model = _MODEL.fullmatch(line)
        if category and category_name is None:
            category_name = _text(category.group(1), 200, field="Категория", line=line_number)
        elif model and model_name is None:
            model_name = _text(model.group(1), 250, field="Модель", line=line_number)
        else:
            remaining.append((line_number, line))
    if category_name is None and model_name is None and len(remaining) >= 3:
        category_line, raw_category = remaining.pop(0)
        model_line, raw_model = remaining.pop(0)
        category_name = _text(raw_category, 200, field="Категория", line=category_line)
        model_name = _text(raw_model, 250, field="Модель", line=model_line)
    if not category_name or not model_name:
        raise EntryError("model_draft_headers_required")
    variants: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in remaining:
        line = re.sub(r"^[•·▪️\-–—]+\s*", "", line).strip()
        if line.casefold() in {"память | цвет", "memory | color"}:
            continue
        if line.count("|") != 1:
            raise EntryError("model_draft_variant_format", line=line_number,
                             message="Укажите память или размер, затем | и список цветов.")
        memory, colors = (part.strip() for part in line.split("|", 1))
        for color in _values(colors, 150, field="Цвета", line=line_number):
            item = {"memory": _text(memory, 100, field="Память / размер", line=line_number), "color": color}
            marker = (item["memory"].casefold(), item["color"].casefold())
            if marker in seen:
                raise EntryError("duplicate_catalog_variant", 409, line=line_number)
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
        raise EntryError("invalid_model_draft", limit=MAX_TEXT,
                         message=f"Вставьте список моделей длиной до {MAX_TEXT} символов.")
    normalized = raw_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = [(number, line.strip()) for number, line in enumerate(normalized.split("\n"), 1) if line.strip()]
    if lines and _NUMBERED.fullmatch(lines[0][1]):
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
