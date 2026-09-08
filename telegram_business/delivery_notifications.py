from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

import requests


PUBLIC_DELIVERY_STATUSES = frozenset(
    {"pending", "picked_up", "on_way", "completed", "cancelled"}
)


class DeliveryFeedError(RuntimeError):
    """A log-safe error raised for an invalid or unavailable delivery feed."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class DeliveryFeedPage:
    events: tuple[dict[str, Any], ...]
    next_after_event_id: int
    latest_event_id: int
    has_more: bool
    invalidations: tuple[dict[str, Any], ...] = ()
    feed_instance_id: str = ""
    cursor_reset_required: bool = False


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise DeliveryFeedError(f"delivery feed has invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DeliveryFeedError(f"delivery feed has invalid {field}") from exc
    if parsed < 0:
        raise DeliveryFeedError(f"delivery feed has invalid {field}")
    return parsed


def _optional_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise DeliveryFeedError("delivery feed contains an invalid text field")
    return str(value).strip()[:maximum]


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    parsed = _non_negative_int(value, field)
    if parsed == 0:
        raise DeliveryFeedError(f"delivery feed has invalid {field}")
    return parsed


def _optional_single_line(value: Any, field: str, maximum: int) -> str:
    result = _optional_text(value, maximum)
    if any(character in result for character in "\r\n\x00"):
        raise DeliveryFeedError(f"delivery feed has invalid {field}")
    return result


def _optional_phone(value: Any) -> str:
    result = _optional_single_line(value, "courier_phone", 32)
    if result and not re.fullmatch(r"\+?[0-9][0-9 ()-]{6,24}", result):
        raise DeliveryFeedError("delivery feed has invalid courier_phone")
    return result


def _validated_instance_id(value: Any) -> str:
    if not isinstance(value, str):
        raise DeliveryFeedError("delivery feed has invalid feed_instance_id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise DeliveryFeedError("delivery feed has invalid feed_instance_id") from exc
    if str(parsed) != value.casefold():
        raise DeliveryFeedError("delivery feed has invalid feed_instance_id")
    return str(parsed)


def _validated_event(raw: Any, after_event_id: int, page_end: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeliveryFeedError("delivery feed contains a non-object event")
    event_id = _non_negative_int(raw.get("event_id"), "event_id")
    if event_id <= after_event_id or event_id > page_end:
        raise DeliveryFeedError("delivery feed event is outside its cursor range")
    order_id = _non_negative_int(raw.get("order_id"), "order_id")
    if not order_id:
        raise DeliveryFeedError("delivery feed has invalid order_id")
    status = _optional_text(raw.get("status") or raw.get("to_status"), 40)
    current_status = _optional_text(raw.get("current_status"), 40)
    if status not in PUBLIC_DELIVERY_STATUSES or not current_status:
        raise DeliveryFeedError("delivery feed contains a non-public status")
    return {
        "event_id": event_id,
        "order_id": order_id,
        "order_number": _optional_text(raw.get("order_number"), 80),
        "status": status,
        "current_status": current_status,
        "from_status": _optional_text(raw.get("from_status"), 40),
        "product": _optional_text(raw.get("product"), 500),
        "client_phone": _optional_text(raw.get("client_phone"), 80),
        "client_phone_2": _optional_text(raw.get("client_phone_2"), 80),
        "courier_id": _optional_positive_int(raw.get("courier_id"), "courier_id"),
        "courier_name": _optional_single_line(
            raw.get("courier_name"), "courier_name", 80
        ),
        "courier_phone": _optional_phone(raw.get("courier_phone")),
        "created_at": _optional_text(raw.get("created_at"), 80),
    }


def _validated_invalidation(
    raw: Any,
    after_event_id: int,
    page_end: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeliveryFeedError("delivery feed contains a non-object invalidation")
    event_id = _non_negative_int(raw.get("event_id"), "event_id")
    if event_id <= after_event_id or event_id > page_end:
        raise DeliveryFeedError("delivery feed invalidation is outside its cursor range")
    order_id = _non_negative_int(raw.get("order_id"), "order_id")
    current_status = _optional_text(raw.get("current_status"), 40)
    to_status = _optional_text(raw.get("to_status"), 40)
    if not order_id or not current_status or not to_status:
        raise DeliveryFeedError("delivery feed contains an invalid invalidation")
    return {
        "event_id": event_id,
        "order_id": order_id,
        "current_status": current_status,
        "to_status": to_status,
        "created_at": _optional_text(raw.get("created_at"), 80),
    }


def validate_feed_page(payload: Any, after_event_id: int) -> DeliveryFeedPage:
    if not isinstance(payload, dict):
        raise DeliveryFeedError("delivery feed response is not an object")
    feed_instance_id = _validated_instance_id(payload.get("feed_instance_id"))
    next_id = _non_negative_int(
        payload.get("next_after_event_id"), "next_after_event_id"
    )
    latest_id = _non_negative_int(payload.get("latest_event_id"), "latest_event_id")
    reset_required = payload.get("cursor_reset_required", False)
    if not isinstance(reset_required, bool):
        raise DeliveryFeedError("delivery feed has invalid cursor_reset_required")
    if next_id < after_event_id:
        raise DeliveryFeedError("delivery feed cursor moved backwards")
    raw_events = payload.get("events")
    raw_invalidations = payload.get("invalidations")
    if (
        not isinstance(raw_events, list)
        or not isinstance(raw_invalidations, list)
        or len(raw_events) + len(raw_invalidations) > 500
    ):
        raise DeliveryFeedError("delivery feed has an invalid events list")
    events = tuple(
        _validated_event(raw, after_event_id, next_id) for raw in raw_events
    )
    invalidations = tuple(
        _validated_invalidation(raw, after_event_id, next_id)
        for raw in raw_invalidations
    )
    event_ids = [event["event_id"] for event in events]
    if event_ids != sorted(set(event_ids)):
        raise DeliveryFeedError("delivery feed events are not strictly ordered")
    invalidation_ids = [item["event_id"] for item in invalidations]
    if invalidation_ids != sorted(set(invalidation_ids)):
        raise DeliveryFeedError("delivery feed invalidations are not strictly ordered")
    if set(event_ids).intersection(invalidation_ids):
        raise DeliveryFeedError("delivery feed event is also an invalidation")
    has_more = payload.get("has_more")
    if not isinstance(has_more, bool):
        raise DeliveryFeedError("delivery feed has invalid has_more")
    if reset_required:
        if (
            after_event_id <= latest_id
            or next_id != after_event_id
            or has_more
            or events
            or invalidations
        ):
            raise DeliveryFeedError("delivery feed has invalid reset response")
    elif latest_id < next_id:
        raise DeliveryFeedError("delivery feed cursor moved backwards")
    elif has_more != (next_id < latest_id):
        raise DeliveryFeedError("delivery feed has inconsistent pagination")
    return DeliveryFeedPage(
        events,
        next_id,
        latest_id,
        has_more,
        invalidations,
        feed_instance_id,
        reset_required,
    )


class DeliveryStatusClient:
    """Read the protected delivery event feed without exposing its bearer token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: Any = None,
        timeout: tuple[float, float] = (3.05, 10.0),
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/") + "/"
        self.token = str(token or "")
        self.http = http or requests
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return urljoin(
            self.base_url,
            "internal/monitoring/v1/delivery/status-events",
        )

    def fetch(self, after_event_id: int, limit: int = 100) -> DeliveryFeedPage:
        after = _non_negative_int(after_event_id, "after_event_id")
        bounded_limit = min(500, max(1, int(limit)))
        try:
            response = self.http.get(
                self.endpoint,
                params={"after_event_id": after, "limit": bounded_limit},
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and 300 <= status_code < 400:
                raise DeliveryFeedError(
                    f"delivery feed request failed (HTTP {status_code})"
                )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            error_response = getattr(exc, "response", None)
            status = getattr(error_response, "status_code", None)
            retry_after = None
            if error_response is not None:
                raw_retry_after = getattr(error_response, "headers", {}).get(
                    "Retry-After"
                )
                try:
                    retry_after = (
                        max(0.0, float(raw_retry_after))
                        if raw_retry_after is not None
                        else None
                    )
                except (TypeError, ValueError):
                    retry_after = None
            suffix = f" (HTTP {status})" if status else ""
            raise DeliveryFeedError(
                f"delivery feed request failed{suffix}",
                retry_after=retry_after,
            ) from None
        except (TypeError, ValueError):
            raise DeliveryFeedError("delivery feed returned invalid JSON") from None
        return validate_feed_page(payload, after)


def delivery_phones(event: dict[str, Any]) -> tuple[str, ...]:
    """Return the two delivery phone fields, without logging or transforming them."""

    values: list[str] = []
    for field in ("client_phone", "client_phone_2"):
        value = str(event.get(field) or "").strip()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def newest_order_events(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Keep the newest customer-visible transition per order in one page."""

    selected: dict[int, dict[str, Any]] = {}
    for event in events:
        order_id = int(event["order_id"])
        previous = selected.get(order_id)
        if previous is None or int(event["event_id"]) > int(previous["event_id"]):
            selected[order_id] = event
    return tuple(
        sorted(selected.values(), key=lambda item: int(item["event_id"]))
    )


def newest_current_events(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Keep only the newest currently-active public state for every order."""

    current = (
        event
        for event in newest_order_events(events)
        if event.get("status") == event.get("current_status")
    )
    return tuple(sorted(current, key=lambda item: int(item["event_id"])))
