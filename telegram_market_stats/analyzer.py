from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from openpyxl import load_workbook

from telegram_business.products import (
    KNOWN_COLORS,
    _family_name,
    extract_product_query,
    normalize_model,
)


DEMAND_PATTERNS = (
    r"\bищ(?:у|ем|ет)\b", r"\bнуж(?:ен|на|но|ны)\b", r"\bкуплю\b",
    r"\bкто\s+(?:даст|продаст)\b", r"\bу\s+кого\s+есть\b",
    r"\bнадо\b", r"\bтребуется\b", r"\bесть\s+у\s+кого\b",
    r"\bkerak\b", r"\bkerek\b", r"\bkere\b", r"\bкерак\b",
    r"\bbor\s*mi\b", r"\bbormi\b", r"\bборми\b",
    r"\bkimda\s+bor\b", r"\bqidir", r"\bқидир",
    r"\bolaman\b", r"\bоламан\b", r"\btopib\s+ber", r"\bтопиб\s+бер",
)
SUPPLY_PATTERNS = (
    r"\bпродам\b", r"\bпродаю\b", r"\bв\s+наличии\b",
    r"\bесть\s+в\s+наличии\b", r"\bпредлагаю\b", r"\bимеется\b",
    r"\bsotaman\b", r"\bsotiladi\b", r"\bсотаман\b", r"\bсотилади\b",
    r"\bmavjud\b", r"\bмавжуд\b", r"\btayyor\b", r"\bтайёр\b",
)


@dataclass(frozen=True, slots=True)
class ModelMention:
    model_key: str
    model_name: str
    memory: str | None
    color: str | None
    confidence: float


@dataclass(frozen=True, slots=True)
class MessageAnalysis:
    intent: str
    mentions: tuple[ModelMention, ...]


def classify_intent(text: str) -> str:
    normalized = normalize_model(text)
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in DEMAND_PATTERNS):
        return "demand"
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in SUPPLY_PATTERNS):
        return "supply"
    return "unknown"


def _aliases(model_name: str) -> set[str]:
    normalized = normalize_model(model_name)
    aliases = {normalized}
    words = normalized.split()
    brands = {
        "apple", "iphone", "samsung", "galaxy", "xiaomi", "redmi", "poco",
        "honor", "huawei", "oppo", "vivo", "realme", "oneplus", "tecno",
        "infinix", "google", "pixel",
    }
    while words and words[0] in brands:
        words = words[1:]
        if len(words) >= 2 and any(any(c.isdigit() for c in word) for word in words):
            aliases.add(" ".join(words))
    return {alias for alias in aliases if alias}


def _canonical_model(value: str) -> str:
    """Collapse catalogue transport/radio variants into the searched family."""
    cleaned = re.sub(r"\s+(?:4g|5g)(?:\s+\d+)?\s*$", "", str(value), flags=re.I)
    family = str(_family_name(cleaned) or cleaned).strip()
    return family or str(value).strip()


def _message_forms(text: str) -> str:
    normalized = normalize_model(text)
    normalized = re.sub(r"\b(\d{1,2})\s*pm\b", r"\1 pro max", normalized)
    normalized = re.sub(r"\b(\d{1,2})\s*p\b", r"\1 pro", normalized)
    normalized = re.sub(r"\bs\s*(\d{1,2})\s*u\b", r"s\1 ultra", normalized)
    return " ".join(normalized.split())


class ProductModelIndex:
    def __init__(self, models: Iterable[str]):
        canonical: dict[str, str] = {}
        alias_map: dict[str, set[str]] = {}
        for raw in models:
            model = _canonical_model(str(raw or "").strip())
            key = normalize_model(model)
            if not key or key in {"model", "model name"}:
                continue
            canonical.setdefault(key, model)
            for alias in _aliases(model):
                alias_map.setdefault(alias, set()).add(key)
        self.models = canonical
        self.aliases = tuple(sorted(
            (
                (alias, next(iter(keys)))
                for alias, keys in alias_map.items()
                if len(keys) == 1
            ),
            key=lambda item: (-len(item[0].split()), -len(item[0]), item[0]),
        ))

    @classmethod
    def from_xlsx(cls, path: Path) -> "ProductModelIndex":
        if not path.is_file():
            raise FileNotFoundError(f"catalog not found: {path}")
        workbook = load_workbook(path, read_only=True, data_only=True)
        models: list[str] = []
        try:
            for sheet in workbook.worksheets:
                rows = sheet.iter_rows(values_only=True)
                header = next(rows, ())
                columns = {
                    str(value or "").strip().casefold(): index
                    for index, value in enumerate(header)
                }
                model_index = next((
                    columns[name] for name in ("model", "model_name", "модель")
                    if name in columns
                ), None)
                if model_index is None:
                    continue
                for row in rows:
                    if model_index < len(row) and row[model_index]:
                        models.append(str(row[model_index]).strip())
        finally:
            workbook.close()
        if not models:
            raise ValueError("catalog contains no model column")
        return cls(models)

    def find(self, text: str, *, maximum: int = 12) -> tuple[ModelMention, ...]:
        haystack = f" {_message_forms(text)} "
        _query, memory, color = extract_product_query(text)
        found: dict[str, tuple[str, int]] = {}
        for alias, key in self.aliases:
            if f" {alias} " not in haystack:
                continue
            previous = found.get(key)
            score = len(alias.split())
            if previous is None or score > previous[1]:
                found[key] = (alias, score)
            if len(found) >= maximum * 3:
                break
        # Suppress a shorter catalogue title contained in a longer exact title,
        # for example iPhone 16 Pro when iPhone 16 Pro Max is present.
        selected: list[tuple[str, str, int]] = []
        ranked = sorted(
            ((key, alias, score) for key, (alias, score) in found.items()),
            key=lambda item: (-item[2], -len(item[1]), item[1]),
        )
        for key, alias, score in ranked:
            if any(f" {alias} " in f" {kept_alias} " for _, kept_alias, _ in selected):
                continue
            selected.append((key, alias, score))
            if len(selected) >= maximum:
                break
        color_value = color
        if color_value is None:
            normalized_words = _message_forms(text).split()
            color_value = next((word for word in normalized_words if word in KNOWN_COLORS), None)
        return tuple(
            ModelMention(
                model_key=key,
                model_name=self.models[key],
                memory=memory,
                color=color_value,
                confidence=1.0 if alias == key else 0.95,
            )
            for key, alias, _score in selected
        )


class MarketMessageAnalyzer:
    def __init__(self, index: ProductModelIndex):
        self.index = index

    def analyze(self, text: str) -> MessageAnalysis:
        return MessageAnalysis(
            intent=classify_intent(text),
            mentions=self.index.find(text),
        )
