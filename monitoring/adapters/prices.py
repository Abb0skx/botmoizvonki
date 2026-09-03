from __future__ import annotations

from typing import Any


def price_summary() -> dict[str, Any]:
    import importlib

    source = importlib.import_module("price_server.router")

    settings = source.settings
    if not settings.enabled:
        return {
            "status": "disabled",
            "snapshot": None,
            "sections": [],
            "scheduler_running": False,
        }
    if source._startup_error:
        return {
            "status": "blocked",
            "snapshot": None,
            "sections": [],
            "scheduler_running": False,
        }
    repository = source.get_repository()
    snapshot = repository.get_current_snapshot(include_payload=False)
    sections = []
    for section in repository.list_sections():
        sections.append({
            key: section.get(key)
            for key in (
                "snapshot_id", "section_key", "position", "title",
                "product_count", "changed_recent",
            )
        })
    return {
        "status": "enabled",
        "snapshot": snapshot,
        "sections": sections,
        "scheduler_running": bool(source._scheduler and source._scheduler.running),
        "telegram_configured": settings.telegram_configured,
    }
