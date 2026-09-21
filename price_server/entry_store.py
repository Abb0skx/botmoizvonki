"""Authoritative supplier prices, separate from generated/public snapshots.

Initialization is explicit and one-shot. Never silently fall back to Google or
re-import its stale prices after cutover. All tables are included in DB backups.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .price_entry import EntryError, HEADERS, PRICE_COLUMNS, identity, now, price, revision

META = tuple(k for k in HEADERS if k not in (*PRICE_COLUMNS, "min_price"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class SQLitePriceSource:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.connection = None

    @contextmanager
    def database(self):
        if self.connection is not None:
            yield self.connection
            return
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ready(self, db):
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_input_state'").fetchone()
        if not table or not db.execute("SELECT 1 FROM entry_input_state WHERE id=1").fetchone():
            raise EntryError("local_prices_not_initialized", 503)

    def suppliers(self):
        with self.database() as db:
            self.ready(db)
            return [dict(r) for r in db.execute("SELECT * FROM entry_suppliers ORDER BY supplier_id")]

    def read(self, sheet_id):
        with self.database() as db:
            self.ready(db)
            supplier = db.execute("SELECT * FROM entry_suppliers WHERE sheet_id=?", (sheet_id,)).fetchone()
            if not supplier:
                raise EntryError("supplier_not_found", 404)
            minima = dict(db.execute("""SELECT product_key, MIN(value) FROM (
                SELECT product_key, price_1 AS value FROM entry_prices
                UNION ALL SELECT product_key, price_12 FROM entry_prices
            ) WHERE value IS NOT NULL GROUP BY product_key"""))
            rows = []
            for stored in db.execute("""SELECT p.*, v.price_1, v.price_12 FROM entry_products p
                LEFT JOIN entry_prices v ON p.product_key=v.product_key AND v.sheet_id=?
                ORDER BY p.position""", (sheet_id,)):
                row = json.loads(stored["metadata_json"])
                row.update(price_1=stored["price_1"], price_12=stored["price_12"],
                           min_price=minima.get(stored["product_key"], 0),
                           key=stored["product_key"], row=stored["position"] + 2, locked=[])
                row["revision"] = revision(row)
                rows.append(row)
            return {"sheet_id": sheet_id, "title": supplier["title"], "rows": rows,
                    "source": "sqlite", "fetched_at": now(), "spreadsheet_url": None}

    def write(self, sheet_id, changes):
        # Caller owns the transaction containing validation AND the audit log.
        if self.connection is None:
            raise RuntimeError("price write requires an atomic service transaction")
        for change in changes:
            field = change["field"]
            if field not in PRICE_COLUMNS:
                raise EntryError("invalid_or_duplicate_field")
            self.connection.execute(f"""INSERT INTO entry_prices(sheet_id,product_key,{field})
                VALUES (?,?,?) ON CONFLICT(sheet_id,product_key)
                DO UPDATE SET {field}=excluded.{field}""", (sheet_id, change["key"], change["after"]))
        self.connection.execute("DELETE FROM entry_prices WHERE price_1 IS NULL AND price_12 IS NULL")
        self.connection.execute("UPDATE entry_input_state SET revision=revision+1, updated_at=? WHERE id=1", (now(),))

    def export(self):
        with self.database() as db:
            self.ready(db)
            state = dict(db.execute("SELECT revision,updated_at FROM entry_input_state WHERE id=1").fetchone())
            has_catalog = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entry_catalog_state'"
            ).fetchone() and db.execute("SELECT 1 FROM entry_catalog_state WHERE id=1").fetchone()
            result = {"schema_version": 1, "source": "sqlite", **state,
                      "suppliers": [dict(r) for r in db.execute("SELECT * FROM entry_suppliers ORDER BY supplier_id")],
                      "products": [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM entry_products ORDER BY position")],
                      "prices": [list(r) for r in db.execute("SELECT sheet_id,product_key,price_1,price_12 FROM entry_prices ORDER BY sheet_id,product_key")]}
            if has_catalog:
                result["categories"] = [dict(r) for r in db.execute(
                    "SELECT category_id,name FROM entry_categories ORDER BY category_id")]
            result["content_hash"] = digest(result)
            return result


def initialize(db_path, catalogs):
    """Validate all sources before atomically installing. Idempotent, no overwrite."""
    if not catalogs or len(catalogs) > 100:
        raise EntryError("invalid_migration")
    products, suppliers, prices, supplier_ids, sheet_ids = [], [], [], set(), set()
    canonical = None
    for catalog in catalogs:
        match = re.fullmatch(r"([1-9][0-9]*)-(.+)", catalog["title"])
        sheet_id = catalog["sheet_id"]
        if not match or type(sheet_id) is not int or sheet_id in sheet_ids or int(match[1]) in supplier_ids:
            raise EntryError("invalid_supplier_identity")
        supplier_ids.add(int(match[1])); sheet_ids.add(sheet_id)
        suppliers.append((sheet_id, int(match[1]), catalog["title"], match[2]))
        metadata, seen, versions = [], set(), set()
        for row in catalog["rows"]:
            key = identity(row)
            ids = [str(row[k]) for k in ("product_id", "version_id_1", "version_id_12")]
            if (row.get("locked") or key in seen or not all(re.fullmatch(r"[1-9][0-9]*", x) for x in ids)
                    or ids[1] == ids[2] or versions.intersection(ids[1:])):
                raise EntryError("invalid_migration_product")
            seen.add(key); versions.update(ids[1:])
            meta = {k: row[k] for k in META}
            metadata.append(meta)
            p1, p12 = price(row["price_1"]), price(row["price_12"])
            if p1 is not None or p12 is not None:
                prices.append((sheet_id, key, p1, p12))
        if not metadata or len(metadata) > 25000:
            raise EntryError("invalid_migration_product_count")
        if canonical is not None and metadata != canonical:
            raise EntryError("supplier_catalogs_differ", 409)
        canonical = metadata
    fingerprint = digest([canonical, suppliers, prices])
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for statement in (
            "CREATE TABLE IF NOT EXISTS entry_input_state(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, updated_at TEXT NOT NULL, migration_hash TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS entry_suppliers(sheet_id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL UNIQUE, title TEXT NOT NULL UNIQUE, name TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS entry_products(product_key TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE, metadata_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS entry_prices(sheet_id INTEGER NOT NULL REFERENCES entry_suppliers(sheet_id), product_key TEXT NOT NULL REFERENCES entry_products(product_key), price_1 INTEGER CHECK(price_1 BETWEEN 0 AND 100000), price_12 INTEGER CHECK(price_12 BETWEEN 0 AND 100000), PRIMARY KEY(sheet_id,product_key))",
        ):
            db.execute(statement)
        state = db.execute("SELECT migration_hash FROM entry_input_state WHERE id=1").fetchone()
        if state:
            if state[0] != fingerprint:
                raise EntryError("local_prices_already_initialized", 409)
            return {"status": "already_initialized"}
        db.executemany("INSERT INTO entry_suppliers VALUES (?,?,?,?)", suppliers)
        db.executemany("INSERT INTO entry_products VALUES (?,?,?)", [(identity(r), i, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(canonical)])
        db.executemany("INSERT INTO entry_prices VALUES (?,?,?,?)", prices)
        db.execute("INSERT INTO entry_input_state VALUES (1,1,?,?)", (now(), fingerprint))
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise EntryError("migration_integrity_failed")
    return {"status": "initialized", "suppliers": len(suppliers), "products": len(canonical), "priced_rows": len(prices), "migration_hash": fingerprint}
