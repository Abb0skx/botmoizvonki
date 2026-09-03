from __future__ import annotations

from typing import Any

from reviews.analytics import get_review_detail, get_reviews_dashboard
from reviews.config import REVIEWS_DB_PATH


_TECHNICAL_FIELDS = {
    "device", "request_headers", "ip_hash_short", "ip_hash",
    "ip_address", "user_agent", "device_data_json", "request_headers_json",
}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in value.items()
            if key not in _TECHNICAL_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def reviews_dashboard(params: dict[str, str], *, include_technical: bool) -> dict:
    result = get_reviews_dashboard(params, REVIEWS_DB_PATH)
    return result if include_technical else _sanitize(result)


def review_detail(review_id: int, *, include_technical: bool) -> dict | None:
    result = get_review_detail(review_id, REVIEWS_DB_PATH)
    if result is None or include_technical:
        return result
    return _sanitize(result)

