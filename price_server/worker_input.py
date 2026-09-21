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


class ServerPriceData(dict):
    """Worksheet-compatible mapping plus the canonical server catalogue."""

    def __init__(self, values, *, categories, products):
        super().__init__(values)
        self.categories = categories
        self.products = products


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
    categories = data.get("categories", [])
    category_by_id, category_names = {}, set()
    for category in categories:
        if (not isinstance(category, dict) or set(category) != {"category_id", "name"}
                or type(category["category_id"]) is not int or category["category_id"] <= 0
                or not isinstance(category["name"], str) or not category["name"].strip()
                or category["category_id"] in category_by_id
                or category["name"].casefold() in category_names):
            raise RuntimeError("Server price source: invalid category")
        category_by_id[category["category_id"]] = category["name"]
        category_names.add(category["name"].casefold())
    keys, versions, product_ids = [], set(), set()
    for row in products:
        ids = [str(row[k]) for k in ("product_id", "version_id_1", "version_id_12")]
        if (not all(re.fullmatch(r"[1-9][0-9]*", v) for v in ids)
                or ids[0] in product_ids or ids[1] == ids[2]
                or versions.intersection(ids[1:])):
            raise RuntimeError("Server price source: invalid product identity")
        if categories and (type(row.get("category_id")) is not int
                           or category_by_id.get(row["category_id"]) != row.get("category_name")):
            raise RuntimeError("Server price source: invalid product category")
        product_ids.add(ids[0]); versions.update(ids[1:]); keys.append(":".join(ids))
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
    return ServerPriceData(result, categories=categories, products=products)


def reconcile_catalog(connection, server_data):
    """Add server-created catalogue rows inside the worker import transaction.

    Existing rows are updated by their immutable server ID after collision
    checks. Rows absent from the server are never deleted, so rollback and
    legacy history remain intact.
    """
    if not isinstance(server_data, ServerPriceData) or not server_data.categories:
        raise RuntimeError("Server price source: catalogue management is not initialized")
    cursor = connection.cursor()
    for category in server_data.categories:
        category_id, name = category["category_id"], category["name"]
        by_id = cursor.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
        by_name = cursor.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if by_name and int(by_name[0]) != category_id:
            raise RuntimeError("Server catalogue: category name conflict")
        if by_id and str(by_id[0]) != name:
            cursor.execute("UPDATE categories SET name=? WHERE id=?", (name, category_id))
        elif not by_id:
            cursor.execute("INSERT INTO categories(id,name) VALUES (?,?)", (category_id, name))

    desired_by_id = {
        int(product["product_id"]): (
            str(product["model_name"]), int(product["category_id"]),
            str(product.get("memory") or ""), str(product.get("color") or ""),
        )
        for product in server_data.products
    }
    created = 0
    for product in server_data.products:
        product_id = int(product["product_id"])
        expected = (str(product["model_name"]), int(product["category_id"]),
                    str(product.get("memory") or ""), str(product.get("color") or ""))
        row = cursor.execute("""SELECT model_name,category_id,
            COALESCE(memory,''),COALESCE(color,'') FROM products WHERE id=?""",
                             (product_id,)).fetchone()
        if row:
            actual = (str(row[0]), int(row[1]), str(row[2]), str(row[3]))
            if actual != expected:
                duplicate = cursor.execute("""SELECT id FROM products
                    WHERE model_name=? AND category_id=? AND COALESCE(memory,'')=?
                      AND COALESCE(color,'')=? AND id<>?""",
                    (*expected, product_id)).fetchone()
                duplicate_id = int(duplicate[0]) if duplicate else None
                if duplicate_id is not None and (
                    duplicate_id not in desired_by_id
                    or desired_by_id[duplicate_id] == expected
                ):
                    raise RuntimeError("Server catalogue: duplicate product identity")
                cursor.execute("""UPDATE products SET model_name=?,category_id=?,
                    memory=?,color=? WHERE id=?""", (*expected, product_id))
        else:
            duplicate = cursor.execute("""SELECT id FROM products
                WHERE model_name=? AND category_id=? AND COALESCE(memory,'')=?
                  AND COALESCE(color,'')=?""", expected).fetchone()
            if duplicate:
                raise RuntimeError("Server catalogue: duplicate product identity")
            cursor.execute("""INSERT INTO products(id,model_name,category_id,memory,color)
                VALUES (?,?,?,?,?)""", (product_id, *expected))
            created += 1
        for field, warranty in (("version_id_1", "1"), ("version_id_12", "12")):
            version_id = int(product[field])
            version = cursor.execute("""SELECT product_id,warranty_period
                FROM product_versions WHERE version_id=?""", (version_id,)).fetchone()
            if version and (int(version[0]), str(version[1])) != (product_id, warranty):
                raise RuntimeError("Server catalogue: version ID conflict")
            if not version:
                cursor.execute("""INSERT INTO product_versions(version_id,product_id,warranty_period)
                    VALUES (?,?,?)""", (version_id, product_id, warranty))
    return created


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
