"""Standalone worker adapter, deployed as ``Cod/server_sorting_source.py``.

The website is authoritative when PRICE_SORT_SOURCE=server. A failed or invalid
response deliberately stops publication; it never silently falls back to Excel.
No application imports are required in the separate worker image.
"""

import hashlib
import hmac
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ORDER_FIELDS = ("category_order", "model_order", "memory_order", "color_order")
OPTIONAL_ORDER_FIELDS = ("supplier_category_order", "raw_color_order")
_SLUG = re.compile(r"[a-z][a-z0-9-]{0,79}\Z")


def _invalid(reason):
    # Reasons are fixed strings: never interpolate remote payloads or secrets.
    raise RuntimeError("Server sorting source: " + reason) from None


def _text(value, limit=1000, *, empty=False, multiline=False):
    if not isinstance(value, str) or len(value) > limit:
        return False
    if not empty and not value.strip():
        return False
    return not any(ord(char) < 32 and not (multiline and char in "\r\n\t")
                   for char in value)


def _order(value):
    if not isinstance(value, dict) or len(value) > 50000:
        return False
    # Historical laptop model names include line breaks. Matching is exact in
    # the existing worker: preserve those names instead of normalizing them.
    return all(_text(name, multiline=True) and type(rank) is int and 0 <= rank <= 1000000
               for name, rank in value.items())


def _catalog(groups):
    if not isinstance(groups, list) or not 1 <= len(groups) <= 256:
        _invalid("invalid catalog groups")
    group_ids, group_positions, section_ids, dom_ids = set(), set(), set(), {
        "price-content", "price-clipboard-payloads", "scroll-to-top-button",
    }
    for group in groups:
        if not isinstance(group, dict):
            _invalid("invalid catalog group")
        group_id, position = group.get("group_id"), group.get("position")
        entries = group.get("sections")
        if (not isinstance(group_id, str) or not _SLUG.fullmatch(group_id)
                or group_id in group_ids or type(position) is not int
                or position < 0 or position in group_positions
                or not _text(group.get("title"), 500)
                or not isinstance(entries, list) or not 1 <= len(entries) <= 1000):
            _invalid("invalid catalog group")
        group_ids.add(group_id)
        group_positions.add(position)
        group_dom_id = "catalog-group-" + group_id
        if group_dom_id in dom_ids:
            _invalid("duplicate catalog HTML identity")
        dom_ids.add(group_dom_id)
        positions = set()
        for section in entries:
            if not isinstance(section, dict):
                _invalid("invalid catalog section")
            section_id, section_position = section.get("section_key"), section.get("position")
            categories = section.get("category_names")
            if (not isinstance(section_id, str) or not _SLUG.fullmatch(section_id)
                    or section_id in section_ids or type(section_position) is not int
                    or section_position < 0 or section_position in positions
                    or not _text(section.get("catalog_label"), 500)
                    or not _text(section.get("title"), 500)
                    or type(section.get("enabled")) is not bool
                    or not isinstance(categories, list) or not 1 <= len(categories) <= 1000
                    or not all(_text(name, 500) for name in categories)
                    or len(categories) != len(set(categories))
                    or not _text(section.get("notice_html", ""), 10000, empty=True, multiline=True)
                    or not _text(section.get("notice_text", ""), 2000, empty=True, multiline=True)):
                _invalid("invalid catalog section")
            section_ids.add(section_id)
            positions.add(section_position)
            for dom_id in (section_id, section_id + "-content"):
                if dom_id in dom_ids:
                    _invalid("duplicate catalog HTML identity")
                dom_ids.add(dom_id)
            for field in ("include_model_pattern", "exclude_model_pattern"):
                pattern = section.get(field)
                if pattern is not None:
                    if not _text(pattern, 5000):
                        _invalid("invalid catalog model pattern")
                    try:
                        re.compile(pattern)
                    except re.error:
                        _invalid("invalid catalog model pattern")
            if (section.get("include_model_pattern") is not None
                    and section.get("include_model_pattern") == section.get("exclude_model_pattern")):
                _invalid("conflicting catalog model patterns")
    if len(section_ids) > 4096:
        _invalid("too many catalog sections")


def decode(payload):
    """Validate an export's checksum without changing any ordering.

    The checksum detects corruption; HTTPS and the sync key authenticate the
    transport. The checksum itself is not a cryptographic signature.
    """
    if not isinstance(payload, dict):
        _invalid("invalid schema/checksum")
    data = dict(payload)
    checksum = data.pop("content_hash", None)
    try:
        actual = hashlib.sha256(json.dumps(
            data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        _invalid("invalid schema/checksum")
    if (not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or not hmac.compare_digest(checksum, actual)
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("source") != "sqlite"):
        _invalid("invalid schema/checksum")
    revision = data.get("revision")
    if (not ((type(revision) is int and revision >= 0) or _text(revision, 256))
            or not _text(data.get("updated_at"), 100)):
        _invalid("invalid revision")
    for field in ORDER_FIELDS:
        if not _order(data.get(field)):
            _invalid("invalid ordering")
    if not data["category_order"] or not data["model_order"]:
        _invalid("empty catalog ordering")
    for field in OPTIONAL_ORDER_FIELDS:
        if field in data and not _order(data[field]):
            _invalid("invalid ordering")
    replacements = data.get("color_replace_dict")
    if (not isinstance(replacements, dict) or len(replacements) > 50000
            or not all(_text(raw) and raw == raw.strip().lower() and _text(canonical)
                       for raw, canonical in replacements.items())):
        _invalid("invalid color replacement")
    _catalog(data.get("catalog_groups"))
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid("duplicate JSON key")
        result[key] = value
    return result


def load_server_sorting_payload():
    """Read one bounded authenticated export; errors never reveal credentials."""
    base = os.getenv("PRICE_SERVER_URL", "").strip().rstrip("/")
    key = os.getenv("PRICE_SYNC_API_KEY", "").strip()
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and len(key) >= 32 and "\r" not in key and "\n" not in key)
        # Accessing port explicitly also validates malformed ports.
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        _invalid("invalid URL/key configuration")
    try:
        with requests.get(
            base + "/price/api/v1/entry/sorting/export",
            headers={"X-Price-Sync-Key": key}, timeout=(10, 60),
            allow_redirects=False, stream=True,
        ) as response:
            if response.status_code != 200:
                _invalid("HTTP " + str(int(response.status_code)))
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    _invalid("payload too large")
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_object)
    except requests.RequestException:
        raise RuntimeError("Server sorting source unavailable; Google fallback is disabled") from None
    except (ValueError, UnicodeError):
        _invalid("invalid JSON response")
    return decode(payload)


def load_server_sorting(group_factory, entry_factory):
    """Return the existing Price.py tuple using its own catalog dataclasses."""
    data = load_server_sorting_payload()
    groups = tuple(group_factory(
        group_id=group["group_id"], label=group["title"],
        entries=tuple(entry_factory(
            section_id=section["section_key"], nav_label=section["catalog_label"],
            title=section["title"], category_names=tuple(section["category_names"]),
            include_model_pattern=section.get("include_model_pattern"),
            exclude_model_pattern=section.get("exclude_model_pattern"),
            notice_html=section.get("notice_html", ""), enabled=section["enabled"],
        ) for section in sorted(group["sections"], key=lambda item: item["position"])),
    ) for group in sorted(data["catalog_groups"], key=lambda item: item["position"]))
    return (data["color_replace_dict"], data["category_order"], data["model_order"],
            data["memory_order"], data["color_order"], groups)


def load_server_sorting_rows():
    """Supply Excel new.py's worksheet interface without changing export logic.

    Supplier workbooks use raw color names and their own category list; neither
    is interchangeable with the public price's canonical colors/categories.
    """
    data = load_server_sorting_payload()
    fields = ("supplier_category_order", "model_order", "memory_order", "raw_color_order")
    if any(field not in data for field in fields):
        _invalid("supplier sorting is not initialized")
    headers = ["category_name", "model_name", "memory_sort", "color_sort"]
    columns = [sorted(data[field], key=lambda name: data[field][name]) for field in fields]
    values = [headers[:]] + [
        [column[index] if index < len(column) else "" for column in columns]
        for index in range(max(map(len, columns), default=0))
    ]
    return SimpleNamespace(title="Website sorting"), values, headers


def sort_price_rows(group):
    """Preserve website memory/color order while grouping equal prices.

    Price.py already orders rows by memory and color in prepare_price_data,
    then removes the temporary rank columns. Their first-occurrence order is
    therefore authoritative here. A stable price sort within each memory
    keeps the intended color order for equal prices without changing prices,
    grouping rules, product identities, or the returned DataFrame columns.
    """
    if group.empty:
        return group.copy()
    if "memory" not in group.columns:
        return group.sort_values(by="price", kind="stable")
    memories = group["memory"].fillna("").astype(str)
    ranks = {name: index for index, name in enumerate(memories.drop_duplicates())}
    memory_values, price_values = memories.tolist(), group["price"].tolist()
    # Use a Series position rather than adding a temporary column: no risk of
    # clobbering an existing name or leaking sort metadata into the snapshot.
    positions = sorted(range(len(group)), key=lambda index: (
        ranks[memory_values[index]], price_values[index],
    ))
    return group.iloc[positions].copy()
