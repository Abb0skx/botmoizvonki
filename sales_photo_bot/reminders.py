from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Sequence

from .dates import TASHKENT_TZ


FILL_REMINDER_MARKER = "\u2063\u2064"
ADMIN_MENTION = "@Texnikach_Admin"
NOTICE_CLOCKS = tuple(time(hour, 0) for hour in range(11, 22))
CLEANUP_CLOCKS = tuple(
    (datetime(2000, 1, 1, 10, 15) + timedelta(minutes=30 * offset)).time()
    for offset in range(23)
)


@dataclass(frozen=True)
class FillCheck:
    supplier_price: bool
    phone: bool
    manager: str | None

    @property
    def complete(self) -> bool:
        return self.supplier_price and self.phone and self.manager is not None

    @property
    def missing_labels(self) -> tuple[str, ...]:
        missing = []
        if not self.supplier_price:
            missing.append("поставщика и цену 🛒💵")
        if not self.phone:
            missing.append("номер телефона 📞")
        if self.manager is None:
            missing.append("выберите менеджера")
        return tuple(missing)


def _field_has_value(body: object, label: str) -> bool:
    for raw_line in str(body or "").splitlines():
        line = raw_line.lstrip("\u2063\u2064\ufeff ")
        if not line.startswith(label):
            continue
        return bool(line[len(label) :].strip())
    return False


def inspect_fill_fields(body: object, manager: object) -> FillCheck:
    selected = str(manager or "").strip() or None
    return FillCheck(
        supplier_price=_field_has_value(body, "🛒💵:"),
        phone=_field_has_value(body, "📞:"),
        manager=selected,
    )


def build_fill_reminder(check: FillCheck) -> str:
    lines = [FILL_REMINDER_MARKER + ADMIN_MENTION]
    if check.manager is not None:
        lines.append(f"Менеджер: {check.manager}")
    lines.extend(("", "Пожалуйста, заполните:"))
    lines.extend(f"• {label}" for label in check.missing_labels)
    return "\n".join(lines)


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TASHKENT_TZ)
    return value.astimezone(TASHKENT_TZ)


def latest_active_slot(
    now: datetime,
    clocks: Sequence[time],
    *,
    grace: timedelta,
) -> datetime | None:
    local = _local(now)
    candidates = [
        datetime.combine(local.date(), clock, tzinfo=TASHKENT_TZ)
        for clock in clocks
    ]
    due = next((slot for slot in reversed(candidates) if slot <= local), None)
    if due is None or local > candidates[-1] + grace:
        return None
    return due


def next_slot(now: datetime, clocks: Sequence[time]) -> datetime:
    local = _local(now)
    for clock in clocks:
        candidate = datetime.combine(local.date(), clock, tzinfo=TASHKENT_TZ)
        if candidate > local:
            return candidate
    return datetime.combine(
        local.date() + timedelta(days=1),
        clocks[0],
        tzinfo=TASHKENT_TZ,
    )
