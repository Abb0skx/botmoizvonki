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

    The original six fields intentionally retain their order so old
    positional construction remains compatible.
    """

    imei: str | None = None
    imei2: str | None = None
    serial_number: str | None = None
    product_info: str | None = None
    product_model: str | None = None
    phone_numbers: tuple[str, ...] = ()
    # Collection fields are appended so every positional constructor used by
    # the original six-field OCR contract keeps exactly the same meaning.
    # They are authoritative when the OCR service sees several product boxes
    # in one photograph.
    product_names: tuple[str, ...] = ()
    imeis: tuple[str, ...] = ()
    serial_numbers: tuple[str, ...] = ()
    warranty_card_detected: bool = False


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


def _unique_values(
    values: object,
    *,
    limit: int,
    product_keys: bool = False,
) -> tuple[str, ...]:
    unique: list[str] = []
    keys: set[str] = set()
    if isinstance(values, (str, bytes)) or values is None:
        source = () if values is None else (values,)
    else:
        try:
            source = tuple(values)  # type: ignore[arg-type]
        except TypeError:
            source = (values,)
    for raw in source:
        value = _compact(raw, limit)
        if value is None:
            continue
        key = _product_key(value) if product_keys else value.casefold()
        if not key or key in keys:
            continue
        keys.add(key)
        unique.append(value)
    return tuple(unique)


def product_display_values(
    identifiers: ProductIdentifiers,
) -> tuple[str, ...]:
    """Return every distinct product label without losing multi-box results."""

    names = _unique_values(
        identifiers.product_names,
        limit=160,
        product_keys=True,
    )
    if names:
        # A single explicit product may still have a separately printed model
        # code. With several boxes that code cannot safely be assigned to one
        # of them, so the catalog names remain unmodified.
        if len(names) == 1:
            model = _compact(identifiers.product_model, 80)
            if model:
                single = ProductIdentifiers(
                    product_info=names[0],
                    product_model=model,
                )
                display = _legacy_product_display(single, limit=160)
                if display:
                    return (display,)
        return names

    legacy = _legacy_product_display(identifiers)
    return (legacy,) if legacy else ()


def identifier_imeis(identifiers: ProductIdentifiers) -> tuple[str, ...]:
    """Return ordered, de-duplicated legacy and collection IMEI values."""

    return _unique_values(
        (
            *identifiers.imeis,
            identifiers.imei,
            identifiers.imei2,
        ),
        limit=15,
    )


def identifier_serial_numbers(
    identifiers: ProductIdentifiers,
) -> tuple[str, ...]:
    """Return ordered, de-duplicated legacy and collection serial values."""

    return _unique_values(
        (*identifiers.serial_numbers, identifiers.serial_number),
        limit=40,
    )


def _legacy_product_display(
    identifiers: ProductIdentifiers,
    *,
    limit: int = MAX_PRODUCT_DISPLAY,
) -> str | None:
    """Build the original product_info/product_model display value."""

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


def product_display_name(
    identifiers: ProductIdentifiers,
    *,
    limit: int = MAX_PRODUCT_DISPLAY,
) -> str | None:
    """Build a bounded product name/model label suitable for later escaping."""

    values = product_display_values(identifiers)
    return _compact("; ".join(values), max(1, int(limit)))


def merge_product_identifiers(
    results: tuple[ProductIdentifiers, ...] | list[ProductIdentifiers],
    *,
    receipt_safe: bool = False,
) -> ProductIdentifiers:
    """Conservatively merge OCR results from all photographs in an album.

    Legacy scalar conflicts are omitted instead of choosing an arbitrary
    value. Explicit collection values represent distinct boxes and are
    therefore unioned in stable order. In receipt-safe album mode, phone-only
    legacy OCR results are ignored; a positively detected warranty card may
    contribute its phone number because the v2 recognizer restricts phone
    extraction to that region.
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
                    "product_names",
                    "imeis",
                    "serial_numbers",
                )
            )
            or item.warranty_card_detected
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

    explicit_product_names = _unique_values(
        (
            name
            for item in items
            for name in item.product_names
        ),
        limit=160,
        product_keys=True,
    )
    if explicit_product_names:
        # product_info mirrors the collection only for a single product. Do
        # not assign a scalar label when several boxes were recognized.
        info = (
            explicit_product_names[0]
            if len(explicit_product_names) == 1
            else None
        )

    has_explicit_imeis = any(item.imeis for item in items)
    if has_explicit_imeis:
        merged_imeis = _unique_values(
            (
                value
                for item in items
                for value in identifier_imeis(item)
            ),
            limit=15,
        )
        imei = merged_imeis[0] if merged_imeis else None
        imei2 = merged_imeis[1] if len(merged_imeis) > 1 else None
    else:
        imei = one_value("imei", 15)
        imei2 = one_value("imei2", 15)
        if imei is not None and imei2 is not None and imei == imei2:
            imei2 = None
        merged_imeis = ()

    has_explicit_serials = any(item.serial_numbers for item in items)
    if has_explicit_serials:
        merged_serials = _unique_values(
            (
                value
                for item in items
                for value in identifier_serial_numbers(item)
            ),
            limit=40,
        )
        serial_number = merged_serials[0] if len(merged_serials) == 1 else None
    else:
        serial_number = one_value("serial_number", 40)
        merged_serials = ()

    return ProductIdentifiers(
        imei=imei,
        imei2=imei2,
        serial_number=serial_number,
        product_info=info,
        product_model=model,
        phone_numbers=tuple(phones),
        product_names=explicit_product_names,
        imeis=merged_imeis,
        serial_numbers=merged_serials,
        warranty_card_detected=any(
            item.warranty_card_detected for item in items
        ),
    )


EMPTY_IDENTIFIERS = ProductIdentifiers()
