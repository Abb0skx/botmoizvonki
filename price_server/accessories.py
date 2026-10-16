"""One durable publication identity for all small accessories.

The original AirTag key and URL remain authoritative. Legacy job payloads are
resolved here too, so an old queued Pencil/Keyboard/Mouse task cannot recreate
an independent partial post after the catalogue has been combined.
"""
from __future__ import annotations

from copy import deepcopy

ACCESSORIES_KEY = "accessories-tags"
ACCESSORIES_TITLE = "AirTag, SmartTag, Pencil, Keyboard, Mouse"
ACCESSORIES_LEGACY_KEYS = (
    ACCESSORIES_KEY, "accessories-pencil", "accessories-keyboard", "accessories-mouse",
)
ACCESSORIES_KEYS = frozenset((*ACCESSORIES_LEGACY_KEYS, "accessories-combined"))
ACCESSORIES_CATEGORIES = (
    "Tag Бренда Apple", "Tag Бренда Samsung",
    "Pencil бренда Apple", "Pencil бренда Xiaomi", "Pencil бренда Samsung",
    "Клавиатура бренда Apple", "Клавиатура бренда Samsung", "Клавиатура бренда Xiaomi",
    "Мышь Бренда Apple", "Мышь Бренда Xiaomi",
)


def canonical_publication_key(key: str) -> str:
    return ACCESSORIES_KEY if key in ACCESSORIES_KEYS else key


def unified_accessories_catalog(groups: list[dict]) -> list[dict]:
    """Return a worker catalogue with one accessories section, without writes.

    Run on each export rather than relying on a one-time data edit; future
    catalogue reloads therefore cannot restore the old four-publication layout.
    Other groups and all product sorting ranks are retained verbatim.
    """
    result = deepcopy(groups)
    for group in result:
        sections = group.get("sections", [])
        matches = [item for item in sections if item.get("section_key") in ACCESSORIES_KEYS]
        if not matches:
            continue
        categories = list(dict.fromkeys(
            name for item in matches for name in item.get("category_names", [])
        ))
        categories.extend(name for name in ACCESSORIES_CATEGORIES if name not in categories)
        combined = deepcopy(matches[0])
        combined.update(section_key=ACCESSORIES_KEY, title=ACCESSORIES_TITLE,
                        catalog_label=ACCESSORIES_TITLE, category_names=categories,
                        position=min(int(item.get("position", 0)) for item in matches),
                        enabled=any(item.get("enabled", True) for item in matches),
                        include_model_pattern=None, exclude_model_pattern=None)
        group["sections"] = [
            combined if item is matches[0] else item
            for item in sections
            if item is matches[0] or item.get("section_key") not in ACCESSORIES_KEYS
        ]
        if group.get("group_id") == "accessories":
            group["title"] = ACCESSORIES_TITLE
    return result
