from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import re
from typing import Sequence

from telegram import MessageEntity

from .dates import TASHKENT_TZ


FILL_REMINDER_MARKER = "\u2063\u2064"
ADMIN_MENTION = "@Texnikach_Admin"
NOTICE_CLOCKS = (
    *(time(hour, 0) for hour in range(9, 17)),
    time(16, 30),
    *(time(hour, minute) for hour in range(17, 20) for minute in (0, 30)),
    time(20, 0),
    time(21, 0),
)
REMINDER_TOKEN_ANCHOR = "\u2063"
REMINDER_TOKEN_RE = re.compile(
    r"^https://t\.me/Texnikach_Admin\?start=sr_([0-9a-f]{24})_([0-9a-f]{16})$"
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


@dataclass(frozen=True)
class SignedReminder:
    text: str
    entities: tuple[MessageEntity, ...]


def build_signed_fill_reminder(check: FillCheck, token_url: str) -> SignedReminder:
    """Attach a machine-readable token without changing visible reminder text."""

    plain = build_fill_reminder(check)
    text = plain[: len(FILL_REMINDER_MARKER)] + REMINDER_TOKEN_ANCHOR + plain[
        len(FILL_REMINDER_MARKER) :
    ]
    return SignedReminder(
        text=text,
        entities=(
            MessageEntity(
                type=MessageEntity.TEXT_LINK,
                offset=len(FILL_REMINDER_MARKER),
                length=len(REMINDER_TOKEN_ANCHOR),
                url=token_url,
            ),
        ),
    )


def extract_reminder_token(
    body: object,
    entities: Sequence[object],
) -> tuple[str, str] | None:
    if not str(body or "").startswith(FILL_REMINDER_MARKER):
        return None
    for entity in entities:
        entity_type = str(getattr(entity, "type", ""))
        if entity_type not in {"text_link", str(MessageEntity.TEXT_LINK)}:
            continue
        match = REMINDER_TOKEN_RE.fullmatch(str(getattr(entity, "url", "") or ""))
        if match is not None:
            return match.group(1), match.group(2)
    return None


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
