"""Pure parsing and review data for the site model-import wizard."""
from __future__ import annotations

import hashlib
import json

from .model_inbox import MAX_MODELS, parse_model_drafts
from .price_entry import EntryError

MAX_IMPORT_VARIANTS = 5_000


def prepare_model_import(raw_text: str, category_id: int) -> dict:
    """Parse Model Yegish-style text while making the UI category authoritative."""
    if type(category_id) is not int or category_id <= 0:
        raise EntryError("invalid_catalog_category")
    parsed = parse_model_drafts(raw_text)
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
        raise EntryError("invalid_catalog_variants")

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
