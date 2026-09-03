from __future__ import annotations

from typing import Any


def calls_summary(
    *, period: str, date_from: str | None, date_to: str | None,
) -> dict[str, Any]:
    import botmoizvonki as source

    return source.stats(period=period, date_from=date_from, date_to=date_to)


def calls_managers(
    *, period: str, date_from: str | None, date_to: str | None,
) -> dict[str, Any]:
    import botmoizvonki as source

    return source.stats_managers(period=period, date_from=date_from, date_to=date_to)


def calls_recent(
    *, period: str, date_from: str | None, date_to: str | None, limit: int,
) -> dict[str, Any]:
    import botmoizvonki as source

    return source.stats_recent(
        period=period, date_from=date_from, date_to=date_to, limit=limit
    )


def call_detail(call_id: int, *, include_technical: bool) -> dict[str, Any]:
    import botmoizvonki as source

    result = source.rating_details(call_id)
    if include_technical:
        return result
    sections = []
    for section in result.get("sections", []):
        if section.get("title") in {"Открытие страницы", "Телефон и браузер"}:
            continue
        clean = dict(section)
        items = dict(clean.get("items") or {})
        for key in (
            "Первый IP", "Последний IP", "IP при оценке", "User-Agent",
            "HTTP-заголовки", "Данные браузера",
        ):
            items.pop(key, None)
        clean["items"] = items
        sections.append(clean)
    return {**result, "sections": sections}

