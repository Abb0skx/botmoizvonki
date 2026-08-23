import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Order:
    id: int
    order_number: int
    manager_id: int
    manager_name: str
    client_phone: str
    product: str
    client_phone_2: str | None = None
    seller_name: str | None = None
    payment_status: str = "collect_on_delivery"
    amount_usd: int | None = None
    amount_uzs: int | None = None
    location_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address_text: str | None = None
    district: str | None = None
    mahalla: str | None = None
    second_location_url: str | None = None
    second_latitude: float | None = None
    second_longitude: float | None = None
    second_address_text: str | None = None
    second_district: str | None = None
    second_mahalla: str | None = None
    delivery_time: str | None = None
    comment: str | None = None
    status: str = "draft"
    assigned_courier_id: int | None = None
    assigned_courier_name: str | None = None
    courier_id: int | None = None
    courier_name: str | None = None
    delivery_photo: str | None = None
    received_usd: int | None = None
    received_uzs: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    delivered_at: str | None = None
    time_started: str | None = None
    delivery_chat_id: int | None = None
    delivery_message_id: int | None = None
    location_chat_id: int | None = None
    location_message_id: int | None = None
    location_details_message_id: int | None = None
    location_footer_message_id: int | None = None
    second_location_chat_id: int | None = None
    second_location_message_id: int | None = None
    second_location_details_message_id: int | None = None
    second_location_footer_message_id: int | None = None
    manager_chat_id: int | None = None
    manager_message_id: int | None = None
    creation_token: str | None = None
    sync_needed: int = 0
    sync_attempted_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Order":
        values = dict(row)
        return cls(**{name: values.get(name) for name in cls.__dataclass_fields__})


@dataclass(slots=True)
class OrderEvent:
    id: int
    order_id: int
    order_number: int
    event_type: str
    actor_id: int | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    changed_fields: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "OrderEvent":
        values = dict(row)
        raw_fields = values.get("changed_fields") or "[]"
        try:
            parsed_fields = json.loads(raw_fields)
        except (TypeError, json.JSONDecodeError):
            parsed_fields = []
        if not isinstance(parsed_fields, list):
            parsed_fields = []
        values["changed_fields"] = tuple(str(value) for value in parsed_fields)
        return cls(**{name: values.get(name) for name in cls.__dataclass_fields__})
