from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductIdentifiers:
    """Optional legacy identifier fields used by caption-formatting helpers.

    Production photo handling no longer extracts these values from images.
    """

    imei: str | None = None
    imei2: str | None = None
    serial_number: str | None = None


EMPTY_IDENTIFIERS = ProductIdentifiers()
