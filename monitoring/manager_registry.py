from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ManagerRecord:
    canonical_id: str
    name: str
    telegram_id: int | None
    call_codes: tuple[str, ...]
    review_codes: tuple[str, ...]
    delivery_names: tuple[str, ...]
    go_ids: tuple[str, ...]


def load_manager_registry() -> tuple[ManagerRecord, ...]:
    raw = os.getenv("MONITORING_MANAGER_REGISTRY_JSON", "").strip()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MONITORING_MANAGER_REGISTRY_JSON is invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("MONITORING_MANAGER_REGISTRY_JSON must be a JSON array")
    records = []
    used_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("manager registry entries must be objects")
        canonical_id = str(item.get("canonical_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not canonical_id or not name or canonical_id in used_ids:
            raise RuntimeError("manager registry IDs and names must be non-empty and unique")
        used_ids.add(canonical_id)
        telegram_id = item.get("telegram_id")
        try:
            telegram_id = int(telegram_id) if telegram_id is not None else None
        except (TypeError, ValueError) as exc:
            raise RuntimeError("manager registry telegram_id must be an integer") from exc

        def strings(key: str) -> tuple[str, ...]:
            value = item.get(key, [])
            if not isinstance(value, list):
                raise RuntimeError(f"manager registry {key} must be an array")
            return tuple(str(entry).strip() for entry in value if str(entry).strip())

        records.append(ManagerRecord(
            canonical_id=canonical_id,
            name=name,
            telegram_id=telegram_id,
            call_codes=strings("call_codes"),
            review_codes=strings("review_codes"),
            delivery_names=strings("delivery_names"),
            go_ids=strings("go_ids"),
        ))
    return tuple(records)


def public_registry() -> list[dict[str, Any]]:
    return [
        {
            "canonical_id": item.canonical_id,
            "name": item.name,
            "telegram_id": item.telegram_id,
            "call_codes": list(item.call_codes),
            "review_codes": list(item.review_codes),
            "delivery_names": list(item.delivery_names),
            "go_ids": list(item.go_ids),
        }
        for item in load_manager_registry()
    ]

