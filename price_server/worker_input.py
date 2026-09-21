"""Standalone worker adapter. Deploy as Cod/server_price_source.py.

Only replaces Google Price input; calculation, exclusions, publication and
worker staging/rollback stay in the existing import pipeline.
"""
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

import requests

HEADERS = ["product_id", "model_name", "color", "memory", "version_id_1",
           "price_1", "version_id_12", "price_12", "min_price", "category_name"]


def decode(payload):
    data = dict(payload)
    checksum = data.pop("content_hash", None)
    actual = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    if checksum != actual or data.get("schema_version") != 1 or data.get("source") != "sqlite":
        raise RuntimeError("Server price source: invalid schema/checksum")
    products, suppliers, prices = data["products"], data["suppliers"], data["prices"]
    if not 1 <= len(products) <= 25000 or not 1 <= len(suppliers) <= 100:
        raise RuntimeError("Server price source: invalid catalog size")
    keys, versions = [], set()
    for row in products:
        ids = [str(row[k]) for k in ("product_id", "version_id_1", "version_id_12")]
        if (not all(re.fullmatch(r"[1-9][0-9]*", v) for v in ids)
                or ids[1] == ids[2] or versions.intersection(ids[1:])):
            raise RuntimeError("Server price source: invalid product identity")
        versions.update(ids[1:]); keys.append(":".join(ids))
    if len(set(keys)) != len(keys):
        raise RuntimeError("Server price source: duplicate product")
    supplier_ids, sheets, titles = set(), set(), set()
    for s in suppliers:
        if (type(s["supplier_id"]) is not int or s["supplier_id"] <= 0
                or type(s["sheet_id"]) is not int or s["sheet_id"] in sheets
                or s["supplier_id"] in supplier_ids or s["title"] in titles
                or not s["title"].startswith(str(s["supplier_id"]) + "-")):
            raise RuntimeError("Server price source: invalid supplier identity")
        sheets.add(s["sheet_id"]); supplier_ids.add(s["supplier_id"]); titles.add(s["title"])
    known = set(keys)
    offers = {}
    for sheet, key, p1, p12 in prices:
        if sheet not in sheets or key not in known or (sheet, key) in offers:
            raise RuntimeError("Server price source: invalid offer identity")
        if any(p is not None and (type(p) is not int or not 0 <= p <= 100000) for p in (p1, p12)):
            raise RuntimeError("Server price source: invalid price")
        offers[sheet, key] = (p1, p12)
    result = {}
    for supplier in suppliers:
        rows = [HEADERS[:]]
        for key, product in zip(keys, products):
            p1, p12 = offers.get((supplier["sheet_id"], key), (None, None))
            row = dict(product, price_1=p1, price_12=p12, min_price="")
            rows.append(["" if row[k] is None else row[k] for k in HEADERS])
        result[supplier["title"]] = rows
    return result


def load_server_prices():
    base = os.getenv("PRICE_SERVER_URL", "").strip().rstrip("/")
    parsed = urlsplit(base)
    key = os.getenv("PRICE_SYNC_API_KEY", "").strip()
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or len(key) < 32):
        raise RuntimeError("Server price source: invalid URL/key configuration")
    try:
        with requests.get(base + "/price/api/v1/entry/export", headers={"X-Price-Sync-Key": key},
                          timeout=(10, 60), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError("Server price source: HTTP " + str(response.status_code))
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 24 * 1024 * 1024:
                    raise RuntimeError("Server price source: payload too large")
                chunks.append(chunk)
        return decode(json.loads(b"".join(chunks)))
    except requests.RequestException:
        # Never print request headers, URL credentials or exception repr.
        raise RuntimeError("Server price source unavailable; Google fallback is disabled") from None
