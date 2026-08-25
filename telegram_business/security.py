from __future__ import annotations

import copy
import re
from typing import Any


_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PAYMENT_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){11,23}\d(?!\d)")
_PAYMENT_CONTEXT = re.compile(
    r"(?i)\b(?:card|bank|account|iban|payment|pay|visa|mastercard|карта|карты|"
    r"оплат|банк|сч[её]т|реквизит|karta|hisob|to.?lov|tolov)\w*\b"
)
_EXPIRY = re.compile(r"(?<!\d)(?:0[1-9]|1[0-2])\s*[/.-]\s*(?:\d{2}|\d{4})(?!\d)")
_EXPIRY_WITH_CVV = re.compile(
    r"(?<!\d)(?:0[1-9]|1[0-2])\s*[/.-]\s*(?:\d{2}|\d{4})"
    r"\s*(?:[,;:-]\s*)?(?:cvv|cvc|csc)?\s*[:=\-]?\s*\d{3,4}(?!\d)",
    re.IGNORECASE,
)
_CVV = re.compile(
    r"(?i)(\b(?:cvv|cvc|csc|security\s*code|код\s+безопасности|карта\s+коди|karta\s+kodi)\s*[:=\-]?\s*)\d{3,4}\b"
)
_CONTEXTUAL_CVV = re.compile(
    r"(?i)(\b(?:cvv|cvc|csc|security\s*code|code|код(?:\s+безопасности)?|"
    r"kod(?:i)?|карта\s+коди|karta\s+kodi)\s*[:=\-]?\s*)\d{3,4}\b"
)
_REDACTED = "[PAYMENT_DATA_REDACTED]"
_IBAN = re.compile(r"(?i)\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_TELEGRAM_BOT_URL = re.compile(r"(?i)(https://api\.telegram\.org/bot)[^/\s]+")
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:telegram[^\s=]*token|webhook[^\s=]*secret|private_key)\s*[:=]\s*)[^\s,;]+"
)
_PAYMENT_PAYLOAD_FIELDS = {
    "successful_payment",
    "refunded_payment",
    "telegram_payment_charge_id",
    "provider_payment_charge_id",
    "invoice_payload",
    "shipping_option_id",
    "order_info",
}


def redact_payment_data(value: str | None) -> str | None:
    """Redact payment credentials from user-authored text.

    Telegram identifiers live in structured numeric fields and are preserved by
    ``sanitize_telegram_payload``.  Long digit sequences inside text are redacted
    conservatively even when they fail Luhn: local payment systems and account
    numbers are not guaranteed to use that checksum.
    """
    if not value:
        return value

    payment_context = bool(_PAYMENT_CONTEXT.search(value))
    had_card = False

    def replace_card(match: re.Match[str]) -> str:
        nonlocal had_card
        had_card = True
        return _REDACTED

    candidate_pattern = _PAYMENT_NUMBER if payment_context else _CARD_CANDIDATE
    # Detect the number before mutating the string.  The permissive grouped-card
    # matcher can otherwise consume the month from ``... 4242 12/29 123`` and
    # leave a recognizable expiry/CVV fragment behind.
    payment_context = payment_context or candidate_pattern.search(value) is not None
    redacted = value
    redacted = _CVV.sub(lambda match: f"{match.group(1)}{_REDACTED}", redacted)
    if payment_context:
        redacted = _CONTEXTUAL_CVV.sub(
            lambda match: f"{match.group(1)}{_REDACTED}", redacted
        )
        redacted = _EXPIRY_WITH_CVV.sub(_REDACTED, redacted)
        redacted = _EXPIRY.sub(_REDACTED, redacted)
    # A syntactically valid IBAN is sensitive even when the customer did not add
    # a label such as "IBAN" or "bank account".
    redacted = _IBAN.sub(_REDACTED, redacted)
    redacted = candidate_pattern.sub(replace_card, redacted)
    return redacted


def sanitize_telegram_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy an update while removing user-authored payment credentials.

    Numeric fields such as update_id, chat.id, message_id and dates are deliberately
    preserved; treating every long digit sequence as text would corrupt idempotency.
    Telegram payment objects are not needed for Business routing and are replaced as
    a whole so provider charge IDs and payer/order details never reach raw storage.
    """
    result = copy.deepcopy(payload)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in tuple(value.items()):
                if key in _PAYMENT_PAYLOAD_FIELDS:
                    value[key] = _REDACTED
                elif key in {"text", "caption"} and isinstance(item, str):
                    value[key] = redact_payment_data(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    return result


def redact_sensitive_data(value: str | None) -> str | None:
    """Redact known credentials plus payment data from diagnostics."""
    value = redact_payment_data(value)
    if not value:
        return value
    value = _TELEGRAM_BOT_URL.sub(r"\1[REDACTED]", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    return _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
