from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductIdentifiers:
    """Identifiers read from a product label by local OCR.

    Missing values stay ``None`` and are omitted from the Telegram card.  IMEI
    values are accepted by the OCR layer only after their check digit has been
    validated.
    """

    imei: str | None = None
    imei2: str | None = None
    serial_number: str | None = None


EMPTY_IDENTIFIERS = ProductIdentifiers()
