"""Pure parsing and review data for the site model-import wizard."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .model_inbox import MAX_MODELS, MAX_TEXT, MAX_VARIANTS, parse_model_drafts
from .price_entry import EntryError

MAX_IMPORT_VARIANTS = 5_000
_LEGACY_HEADER = re.compile(
    r"^(?:[0123]\s*[.)：:．）]|(?:категория|модель)\s*:|(?:добавить|новая)\s+модель\s*:?\s*$)",
    re.I,
)
_TABLE_HEADERS = {
    ("модель", "память", "цвета"), ("модель", "память", "цвет"),
    ("model", "memory", "colors"), ("model", "memory", "color"),
}


def _site_text(value: str, maximum: int, field: str, location: dict) -> str:
    value = " ".join(unicodedata.normalize("NFC", value).split())
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise EntryError(
            "invalid_model_import_value", field=field, limit=maximum, **location,
            message=f"Поле «{field}» должно содержать не более {maximum} символов.",
        )
    return value


def _site_values(value: str, maximum: int, field: str, location: dict) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;，；،]", value):
        item = _site_text(raw, maximum, field, location)
        if not item:
            continue
        if item == "-":
            item = ""
        if item.casefold() in seen:
            raise EntryError(
                "duplicate_catalog_variant", 409, field=field, value=item, **location,
                message=f"В поле «{field}» повторяется значение «{item or '-'}».",
            )
        seen.add(item.casefold())
        values.append(item)
    return values or [""]


def _site_model(columns: list[str], location: dict) -> dict:
    model_name = _site_text(columns[0], 250, "Модель", location)
    if not model_name or model_name == "-":
        raise EntryError(
            "invalid_model_import_value", field="Модель", **location,
            message="Укажите название модели в первой строке или колонке.",
        )
    memories = _site_values(columns[1], 100, "Память", location)
    colors = _site_values(columns[2], 150, "Цвета", location)
    if len(memories) * len(colors) > MAX_VARIANTS:
        raise EntryError(
            "invalid_catalog_variants", limit=MAX_VARIANTS, **location,
            message=f"У одной модели может быть не более {MAX_VARIANTS} вариантов.",
        )
    return {
        "category_id": None,
        "model_name": model_name,
        # New site formats contain exactly the combinations the user listed.
        # The legacy numbered format's extra blank color remains legacy-only.
        "variants": [{"memory": memory, "color": color}
                     for memory in memories for color in colors],
    }


def _parse_site_models(raw_text: str) -> list[dict]:
    if not isinstance(raw_text, str) or not raw_text.strip() or len(raw_text) > MAX_TEXT:
        raise EntryError("invalid_model_draft", limit=MAX_TEXT)
    normalized = raw_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = list(enumerate(normalized.split("\n"), 1))
    populated = [(number, line) for number, line in lines if line.strip()]
    if not populated:
        raise EntryError("invalid_model_draft", limit=MAX_TEXT,
                         message="Вставьте хотя бы одну модель в список.")
    first = populated[0][1].strip()
    legacy_plain = (len(populated) >= 3
                    and all("|" not in line and "\t" not in line for _, line in populated[:2])
                    and all(line.count("|") == 1 for _, line in populated[2:]))
    if _LEGACY_HEADER.match(first) or legacy_plain:
        return parse_model_drafts(raw_text)

    models: list[dict] = []
    seen: set[str] = set()

    def append(columns: list[str], location: dict) -> None:
        model = _site_model(columns, location)
        folded = model["model_name"].casefold()
        if folded in seen:
            raise EntryError(
                "duplicate_catalog_model", 409, model_name=model["model_name"], **location,
                message=f"Модель «{model['model_name']}» встречается в списке несколько раз.",
            )
        if len(models) >= MAX_MODELS:
            raise EntryError(
                "invalid_catalog_variants", limit=MAX_MODELS, **location,
                message=f"За один раз можно добавить не более {MAX_MODELS} моделей.",
            )
        seen.add(folded)
        models.append(model)

    separator = "\t" if "\t" in populated[0][1] else "|" if "|" in first else None
    if separator:
        for index, (line_number, line) in enumerate(populated):
            columns = line.split(separator)
            if len(columns) != 3:
                raise EntryError(
                    "model_import_columns_required", line=line_number,
                    message="В каждой строке нужны три колонки: модель, память и цвета.",
                )
            if index == 0 and tuple(value.strip().casefold() for value in columns) in _TABLE_HEADERS:
                continue
            append(columns, {"line": line_number})
    else:
        blocks: list[list[tuple[int, str]]] = []
        block: list[tuple[int, str]] = []
        for line_number, line in lines:
            if line.strip():
                block.append((line_number, line))
            elif block:
                blocks.append(block)
                block = []
        if block:
            blocks.append(block)
        for index, block in enumerate(blocks, 1):
            location = {"line": block[0][0], "block": index}
            if len(block) != 3:
                raise EntryError(
                    "model_import_block_format", **location,
                    message="Для каждой модели нужны три строки: название, память и цвета. "
                            "Укажите «-» для пустого поля, а модели разделите пустой строкой.",
                )
            append([line for _, line in block], location)
    if not models:
        raise EntryError(
            "invalid_model_draft", line=populated[0][0],
            message="Добавьте хотя бы одну модель под заголовком таблицы.",
        )
    return models


def prepare_model_import(raw_text: str, category_id: int) -> dict:
    """Parse site blocks/tables and legacy text into a category-scoped preview."""
    if type(category_id) is not int or category_id <= 0:
        raise EntryError("invalid_catalog_category")
    parsed = _parse_site_models(raw_text)
    if not 1 <= len(parsed) <= MAX_MODELS:
        raise EntryError("invalid_catalog_variants")

    models: list[dict] = []
    model_names: list[str] = []
    memories: list[str] = []
    colors: list[str] = []
    seen_models: set[str] = set()
    seen_memories: set[str] = set()
    seen_colors: set[str] = set()
    source_category_ids: set[int] = set()
    variant_count = 0

    for item in parsed:
        source_id = item.get("category_id")
        if isinstance(source_id, int):
            source_category_ids.add(source_id)
        model_name = str(item["model_name"])
        folded = model_name.casefold()
        if folded in seen_models:
            raise EntryError("duplicate_catalog_model", 409, model_name=model_name)
        seen_models.add(folded)
        model_names.append(model_name)
        variants = []
        for variant in item["variants"]:
            memory = str(variant.get("memory") or "")
            color = str(variant.get("color") or "")
            variants.append({"memory": memory, "color": color})
            variant_count += 1
            if memory and memory.casefold() not in seen_memories:
                seen_memories.add(memory.casefold())
                memories.append(memory)
            if color and color.casefold() not in seen_colors:
                seen_colors.add(color.casefold())
                colors.append(color)
        models.append({
            "category_id": category_id,
            "model_name": model_name,
            "variants": variants,
        })
    if variant_count > MAX_IMPORT_VARIANTS:
        raise EntryError(
            "invalid_catalog_variants", limit=MAX_IMPORT_VARIANTS,
            message=f"За один раз можно добавить не более {MAX_IMPORT_VARIANTS} вариантов.",
        )

    canonical = {"category_id": category_id, "models": models}
    preview_hash = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    warnings = []
    if source_category_ids and source_category_ids != {category_id}:
        warnings.append("source_category_ignored")
    return {
        **canonical,
        "preview_hash": preview_hash,
        "model_count": len(models),
        "variant_count": variant_count,
        "sorting": {
            "models": model_names,
            "memories": memories,
            "colors": colors,
        },
        "source_category_ids": sorted(source_category_ids),
        "warnings": warnings,
    }
