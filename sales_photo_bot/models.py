from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_PRODUCT_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)
_PRODUCT_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
MAX_PRODUCT_DISPLAY = 120


@dataclass(frozen=True)
class ProductIdentifiers:
    """Optional values read from one product photograph.

    The first three fields intentionally retain their original order so old
    positional construction remains compatible.
    """

    imei: str | None = None
    imei2: str | None = None
    serial_number: str | None = None
    product_info: str | None = None
    product_model: str | None = None
    phone_numbers: tuple[str, ...] = ()


def _compact(value: object, limit: int) -> str | None:
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(value or "")
    )
    text = " ".join(cleaned.split())
    if not text:
        return None
    return text[: max(1, int(limit))].strip() or None


def _product_key(value: object) -> str:
    return _PRODUCT_TOKEN_RE.sub("", str(value or "").casefold())


def _product_words(value: object) -> tuple[str, ...]:
    return tuple(_PRODUCT_WORD_RE.findall(str(value or "").casefold()))


def _product_values_compatible(left: str, right: str) -> bool:
    """Require exact normalized agreement; prefixes may be different variants."""

    return bool(_product_key(left)) and _product_key(left) == _product_key(right)


def _compatible_product_values(
    values: list[str],
) -> str | None:
    """Return the most informative value only when all variants agree."""

    unique: list[str] = []
    keys: list[str] = []
    for value in values:
        compact = _compact(value, 240)
        key = _product_key(compact)
        if not compact or not key or key in keys:
            continue
        unique.append(compact)
        keys.append(key)
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    for left in unique:
        for right in unique:
            if not _product_values_compatible(left, right):
                return None
    return max(unique, key=lambda item: len(_product_key(item)))


def product_display_name(
    identifiers: ProductIdentifiers,
    *,
    limit: int = MAX_PRODUCT_DISPLAY,
) -> str | None:
    """Build a bounded product name/model label suitable for later escaping."""

    info = _compact(identifiers.product_info, 160)
    model = _compact(identifiers.product_model, 80)
    if info and model:
        if _product_key(info) == _product_key(model):
            return _compact(info, max(1, int(limit)))
        info_words = _product_words(info)
        model_words = _product_words(model)

        def contains_words(
            container: tuple[str, ...],
            candidate: tuple[str, ...],
        ) -> bool:
            if not candidate or len(candidate) > len(container):
                return False
            return any(
                container[index : index + len(candidate)] == candidate
                for index in range(len(container) - len(candidate) + 1)
            )

        if contains_words(info_words, model_words):
            display = info
        elif contains_words(model_words, info_words):
            display = model
        else:
            display = f"{info} — {model}"
    else:
        display = info or model
    return _compact(display, max(1, int(limit)))


def merge_product_identifiers(
    results: tuple[ProductIdentifiers, ...] | list[ProductIdentifiers],
    *,
    receipt_safe: bool = False,
) -> ProductIdentifiers:
    """Conservatively merge OCR results from all photographs in an album.

    Conflicting identifiers are omitted instead of choosing an arbitrary
    value. In receipt-safe album mode, phone-only OCR results are ignored.
    Product variants must agree exactly after case/punctuation normalization.
    """

    items = tuple(results)
    if receipt_safe:
        items = tuple(
            item
            for item in items
            if any(
                getattr(item, field)
                for field in (
                    "product_info",
                    "product_model",
                    "imei",
                    "imei2",
                    "serial_number",
                )
            )
        )
        if not items:
            return ProductIdentifiers()

    def one_value(field: str, limit: int) -> str | None:
        unique: list[str] = []
        keys: set[str] = set()
        for item in items:
            value = _compact(getattr(item, field), limit)
            if value is None:
                continue
            key = value.casefold()
            if key not in keys:
                unique.append(value)
                keys.add(key)
        return unique[0] if len(unique) == 1 else None

    phones: list[str] = []
    for item in items:
        for phone in item.phone_numbers:
            value = str(phone or "").strip()
            if value and value not in phones:
                phones.append(value)
            if len(phones) == 3:
                break
        if len(phones) == 3:
            break
    if len(phones) > 2:
        phones = []

    product_infos = [
        value
        for item in items
        if (value := _compact(item.product_info, 160)) is not None
    ]
    product_models = [
        value
        for item in items
        if (value := _compact(item.product_model, 80)) is not None
    ]
    info = _compatible_product_values(product_infos)
    model = _compatible_product_values(product_models)
    # If either independently observed product field conflicts, do not combine
    # it with the other field into a potentially fictitious product variant.
    if product_infos and info is None:
        model = None
    if product_models and model is None:
        info = None

    imei = one_value("imei", 15)
    imei2 = one_value("imei2", 15)
    if imei is not None and imei2 is not None and imei == imei2:
        imei2 = None

    return ProductIdentifiers(
        imei=imei,
        imei2=imei2,
        serial_number=one_value("serial_number", 40),
        product_info=info,
        product_model=model,
        phone_numbers=tuple(phones),
    )


EMPTY_IDENTIFIERS = ProductIdentifiers()
