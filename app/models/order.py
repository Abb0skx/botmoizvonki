from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Order:
    id: int
    order_number: int
    manager_id: int
    manager_name: str
    client_phone: str
    product: str
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
    delivery_time: str | None = None
    comment: str | None = None
    status: str = "draft"
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

    @classmethod
    def from_row(cls, row: Any) -> "Order":
        values = dict(row)
        return cls(**{name: values.get(name) for name in cls.__dataclass_fields__})
