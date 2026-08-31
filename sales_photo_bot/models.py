from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Recognition:
    """Allowlisted product fields safe for rendering in a Telegram card.

    ``model_name`` is a commercial name such as ``Samsung Galaxy Tab A11``.
    A raw manufacturer code such as ``SM-X133`` is deliberately kept separate
    and is never rendered as the commercial model name.
    """

    model_name: str | None = None
    model_code: str | None = None
    sku: str | None = None
    memory: str | None = None
    color: str | None = None
    confidence: float = 0.0
    source_count: int = 0


EMPTY_RECOGNITION = Recognition()
