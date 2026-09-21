"""Self-contained product catalogue management for the price-entry site."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
import uuid
from pathlib import Path

from .price_entry import EntryError, identity, now
from .model_import import prepare_model_import

MAX_VARIANTS = 500


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()


def _text(value, *, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise EntryError("invalid_catalog_text")
    value = " ".join(unicodedata.normalize("NFC", value).split())
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise EntryError("invalid_catalog_text")
    return value


def _positive(value) -> int:
    if type(value) is not int or value <= 0:
        raise EntryError("invalid_catalog_id")
    return value


def _tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS entry_categories(
        category_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS entry_catalog_state(
        id INTEGER PRIMARY KEY CHECK(id=1),
        next_category_id INTEGER NOT NULL,
        next_product_id INTEGER NOT NULL,
        next_version_id INTEGER NOT NULL,
        migration_hash TEXT NOT NULL,
        initialized_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS entry_catalog_operations(
        operation_id TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL
    )""")


def _metadata_rows(db: sqlite3.Connection) -> list[tuple[sqlite3.Row, dict]]:
    rows = []
    for row in db.execute(
        "SELECT product_key,position,metadata_json FROM entry_products ORDER BY position"
    ):
        rows.append((row, json.loads(row["metadata_json"])))
    return rows


def _model_revision(products: list[dict]) -> str:
    return _digest([
        {
            "product_id": int(item["product_id"]),
            "model_name": str(item["model_name"]),
            "category_id": int(item["category_id"]),
            "category_name": str(item["category_name"]),
            "memory": str(item.get("memory") or ""),
            "color": str(item.get("color") or ""),
            "version_id_1": int(item["version_id_1"]),
            "version_id_12": int(item["version_id_12"]),
        }
        for item in sorted(products, key=lambda value: int(value["product_id"]))
    ])


def _operation(db: sqlite3.Connection, operation_id: str, request_hash: str):
    try:
        uuid.UUID(operation_id)
    except (ValueError, TypeError, AttributeError):
        raise EntryError("idempotency_key_required") from None
    previous = db.execute(
        "SELECT request_hash,result_json FROM entry_catalog_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if previous:
        if previous["request_hash"] != request_hash:
            raise EntryError("idempotency_conflict", 409)
        return json.loads(previous["result_json"])
    return None


def _store_operation(db: sqlite3.Connection, operation_id: str,
                     request_hash: str, request: dict, result: dict) -> None:
    db.execute(
        "INSERT INTO entry_catalog_operations VALUES (?,?,?,?,?)",
        (
            operation_id,
            request_hash,
            now(),
            json.dumps(request, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
        ),
    )


def install_catalog(db_path: Path | str, categories: list[dict], *,
                    max_product_id: int, max_version_id: int) -> dict:
    """Attach canonical category IDs and safe ID high-water marks.

    The operation is additive and idempotent. Existing product identities and
    prices are never rewritten; only ``category_id`` is added to metadata.
    High-water marks must come from the worker database so historical IDs that
    are absent from the active site catalogue can never be reused.
    """
    if not isinstance(categories, list) or not categories or len(categories) > 1000:
        raise EntryError("invalid_catalog_migration")
    max_product_id, max_version_id = _positive(max_product_id), _positive(max_version_id)
    parsed, ids, names = [], set(), set()
    for item in categories:
        if not isinstance(item, dict) or set(item) != {"category_id", "name"}:
            raise EntryError("invalid_catalog_migration")
        category_id = _positive(item["category_id"])
        name = _text(item["name"], maximum=200, required=True)
        folded = name.casefold()
        if category_id in ids or folded in names:
            raise EntryError("invalid_catalog_migration")
        ids.add(category_id); names.add(folded); parsed.append((category_id, name))
    parsed.sort()
    migration_hash = _digest({"categories": parsed,
                              "max_product_id": max_product_id,
                              "max_version_id": max_version_id})
    with sqlite3.connect(db_path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_products'").fetchone():
            raise EntryError("local_prices_not_initialized", 503)
        _tables(db)
        state = db.execute("SELECT * FROM entry_catalog_state WHERE id=1").fetchone()
        if state:
            existing = {r[0]: r[1] for r in db.execute(
                "SELECT category_id,name FROM entry_categories")}
            if any(existing.get(category_id) != name for category_id, name in parsed):
                raise EntryError("catalog_migration_conflict", 409)
            if (state["next_product_id"] <= max_product_id
                    or state["next_version_id"] <= max_version_id):
                raise EntryError("catalog_id_high_water_conflict", 409)
            return {"status": "already_initialized", "categories": len(existing),
                    "next_product_id": state["next_product_id"],
                    "next_version_id": state["next_version_id"]}

        by_name = {name.casefold(): (category_id, name) for category_id, name in parsed}
        updates, product_ids, version_ids = [], set(), set()
        for row in db.execute("SELECT product_key,metadata_json FROM entry_products"):
            metadata = json.loads(row["metadata_json"])
            try:
                product_id = _positive(int(metadata["product_id"]))
                version_1 = _positive(int(metadata["version_id_1"]))
                version_12 = _positive(int(metadata["version_id_12"]))
                category_name = _text(metadata["category_name"], maximum=200, required=True)
            except (KeyError, TypeError, ValueError):
                raise EntryError("invalid_catalog_product") from None
            category = by_name.get(category_name.casefold())
            if not category or product_id in product_ids or version_1 == version_12 \
                    or version_ids.intersection({version_1, version_12}):
                raise EntryError("invalid_catalog_product")
            product_ids.add(product_id); version_ids.update({version_1, version_12})
            if "category_id" in metadata and int(metadata["category_id"]) != category[0]:
                raise EntryError("catalog_migration_conflict", 409)
            metadata["category_id"] = category[0]
            updates.append((json.dumps(metadata, ensure_ascii=False), row["product_key"]))
        if not updates:
            raise EntryError("invalid_catalog_product")
        db.executemany("INSERT INTO entry_categories(category_id,name) VALUES (?,?)", parsed)
        db.executemany("UPDATE entry_products SET metadata_json=? WHERE product_key=?", updates)
        next_category = max(ids) + 1
        next_product = max(max_product_id, *product_ids) + 1
        next_version = max(max_version_id, *version_ids) + 1
        db.execute("INSERT INTO entry_catalog_state VALUES (1,?,?,?,?,?)",
                   (next_category, next_product, next_version, migration_hash, now()))
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise EntryError("catalog_migration_integrity_failed")
    return {"status": "initialized", "categories": len(parsed),
            "products": len(updates), "next_product_id": next_product,
            "next_version_id": next_version, "migration_hash": migration_hash}


class EntryCatalogService:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def categories(self) -> dict:
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            _tables(db)
            state = db.execute("SELECT * FROM entry_catalog_state WHERE id=1").fetchone()
            if not state:
                raise EntryError("catalog_management_not_initialized", 503)
            categories = [dict(r) for r in db.execute(
                "SELECT category_id,name FROM entry_categories ORDER BY name COLLATE NOCASE,category_id")]
            return {"categories": categories, "source": "sqlite",
                    "next_product_id": state["next_product_id"]}

    def models(self) -> dict:
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            _tables(db)
            state = db.execute("SELECT * FROM entry_catalog_state WHERE id=1").fetchone()
            if not state:
                raise EntryError("catalog_management_not_initialized", 503)
            groups: dict[tuple[int, str], list[dict]] = {}
            for _, metadata in _metadata_rows(db):
                marker = (
                    int(metadata["category_id"]),
                    str(metadata["model_name"]).casefold(),
                )
                groups.setdefault(marker, []).append(metadata)
            result = []
            for products in groups.values():
                products.sort(key=lambda item: int(item["product_id"]))
                first = products[0]
                result.append({
                    "anchor_product_id": int(first["product_id"]),
                    "model_name": str(first["model_name"]),
                    "category_id": int(first["category_id"]),
                    "category_name": str(first["category_name"]),
                    "variant_count": len(products),
                    "product_ids": [int(item["product_id"]) for item in products],
                    "revision": _model_revision(products),
                })
            result.sort(key=lambda item: (
                item["category_name"].casefold(), item["model_name"].casefold(),
                item["anchor_product_id"],
            ))
            return {
                "models": result,
                "model_count": len(result),
                "variant_count": sum(item["variant_count"] for item in result),
                "source": "sqlite",
            }

    def model(self, anchor_product_id: int) -> dict:
        anchor_product_id = _positive(anchor_product_id)
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            _tables(db)
            rows = _metadata_rows(db)
            anchor = next((metadata for _, metadata in rows
                           if int(metadata["product_id"]) == anchor_product_id), None)
            if anchor is None:
                raise EntryError("catalog_model_not_found", 404)
            marker = (int(anchor["category_id"]), str(anchor["model_name"]).casefold())
            products = [metadata for _, metadata in rows if (
                int(metadata["category_id"]), str(metadata["model_name"]).casefold()
            ) == marker]
            products.sort(key=lambda item: int(item["product_id"]))
            return {
                "anchor_product_id": anchor_product_id,
                "category_id": int(anchor["category_id"]),
                "category_name": str(anchor["category_name"]),
                "model_name": str(anchor["model_name"]),
                "revision": _model_revision(products),
                "variants": [{
                    "product_id": int(item["product_id"]),
                    "memory": str(item.get("memory") or ""),
                    "color": str(item.get("color") or ""),
                    "version_id_1": int(item["version_id_1"]),
                    "version_id_12": int(item["version_id_12"]),
                } for item in products],
            }

    def preview_import(self, raw_text: str, category_id: int) -> dict:
        preview = prepare_model_import(raw_text, category_id)
        with sqlite3.connect(self.db_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            _tables(db)
            category = db.execute(
                "SELECT name FROM entry_categories WHERE category_id=?", (category_id,)
            ).fetchone()
            if not category:
                raise EntryError("catalog_category_not_found", 404)
            existing_names = {
                (int(metadata.get("category_id", -1)),
                 str(metadata.get("model_name", "")).casefold())
                for _, metadata in _metadata_rows(db)
            }
            collisions = [item["model_name"] for item in preview["models"]
                          if (category_id, item["model_name"].casefold()) in existing_names]
            if collisions:
                raise EntryError("catalog_model_exists", 409, model_names=collisions)
        preview["category_name"] = str(category["name"])
        return preview

    def import_models(self, raw_text: str, category_id: int,
                      preview_hash: str, operation_id: str) -> dict:
        prepared = prepare_model_import(raw_text, category_id)
        if not isinstance(preview_hash, str) or not secrets.compare_digest(
                prepared["preview_hash"], preview_hash):
            raise EntryError("catalog_preview_changed", 409)
        canonical = {
            "action": "bulk_import",
            "category_id": category_id,
            "models": prepared["models"],
            "preview_hash": preview_hash,
        }
        request_hash = _digest(canonical)
        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            _tables(db)
            previous = _operation(db, operation_id, request_hash)
            if previous is not None:
                return previous
            state = db.execute("SELECT * FROM entry_catalog_state WHERE id=1").fetchone()
            if not state:
                raise EntryError("catalog_management_not_initialized", 503)
            category = db.execute(
                "SELECT name FROM entry_categories WHERE category_id=?", (category_id,)
            ).fetchone()
            if not category:
                raise EntryError("catalog_category_not_found", 404)

            rows = _metadata_rows(db)
            existing_names = {
                (int(metadata.get("category_id", -1)),
                 str(metadata.get("model_name", "")).casefold())
                for _, metadata in rows
            }
            collisions = [item["model_name"] for item in prepared["models"]
                          if (category_id, item["model_name"].casefold()) in existing_names]
            if collisions:
                raise EntryError("catalog_model_exists", 409, model_names=collisions)

            product_id = int(state["next_product_id"])
            version_id = int(state["next_version_id"])
            max_position = max((int(row["position"]) for row, _ in rows), default=-1)
            created = []
            for model in prepared["models"]:
                for variant in model["variants"]:
                    metadata = {
                        "product_id": product_id,
                        "model_name": model["model_name"],
                        "color": variant["color"],
                        "memory": variant["memory"],
                        "version_id_1": version_id,
                        "version_id_12": version_id + 1,
                        "category_name": str(category["name"]),
                        "category_id": category_id,
                    }
                    product_key = identity(metadata)
                    db.execute(
                        "INSERT INTO entry_products(product_key,position,metadata_json) VALUES (?,?,?)",
                        (product_key, max_position + 1 + len(created),
                         json.dumps(metadata, ensure_ascii=False)),
                    )
                    created.append({**metadata, "key": product_key})
                    product_id += 1
                    version_id += 2
            db.execute(
                "UPDATE entry_catalog_state SET next_product_id=?,next_version_id=? WHERE id=1",
                (product_id, version_id),
            )
            db.execute(
                "UPDATE entry_input_state SET revision=revision+1,updated_at=? WHERE id=1",
                (now(),),
            )
            result = {
                "status": "created",
                "created": created,
                "created_count": len(created),
                "model_count": len(prepared["models"]),
                "category_id": category_id,
                "category_name": str(category["name"]),
                "import": "next_scheduled_import",
            }
            _store_operation(db, operation_id, request_hash, canonical, result)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise EntryError("catalog_create_integrity_failed")
            return result

    def create(self, body: dict, operation_id: str) -> dict:
        if not isinstance(body, dict) or set(body) != {
                "category_id", "new_category_name", "model_name", "variants"}:
            raise EntryError("invalid_catalog_request")
        category_id = body["category_id"]
        new_category_name = body["new_category_name"]
        if category_id is not None and new_category_name is not None:
            raise EntryError("invalid_catalog_category")
        if category_id is None and new_category_name is None:
            raise EntryError("invalid_catalog_category")
        if category_id is not None:
            category_id = _positive(category_id)
        if new_category_name is not None:
            new_category_name = _text(new_category_name, maximum=200, required=True)
        model_name = _text(body["model_name"], maximum=250, required=True)
        variants = body["variants"]
        if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
            raise EntryError("invalid_catalog_variants")
        normalized, seen = [], set()
        for variant in variants:
            if not isinstance(variant, dict) or set(variant) != {"memory", "color"}:
                raise EntryError("invalid_catalog_variants")
            memory = _text(variant["memory"], maximum=100)
            color = _text(variant["color"], maximum=150)
            marker = (memory.casefold(), color.casefold())
            if marker in seen:
                raise EntryError("duplicate_catalog_variant", 409)
            seen.add(marker); normalized.append({"memory": memory, "color": color})
        canonical = {"category_id": category_id, "new_category_name": new_category_name,
                     "model_name": model_name, "variants": normalized}
        request_hash = _digest(canonical)

        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            _tables(db)
            previous = _operation(db, operation_id, request_hash)
            if previous is not None:
                return previous
            state = db.execute("SELECT * FROM entry_catalog_state WHERE id=1").fetchone()
            if not state:
                raise EntryError("catalog_management_not_initialized", 503)
            if new_category_name is not None:
                duplicate = db.execute(
                    "SELECT category_id FROM entry_categories WHERE name=? COLLATE NOCASE",
                    (new_category_name,)).fetchone()
                if duplicate:
                    raise EntryError("catalog_category_exists", 409,
                                     category_id=duplicate["category_id"])
                category_id = state["next_category_id"]
                category_name = new_category_name
                db.execute("INSERT INTO entry_categories VALUES (?,?)", (category_id, category_name))
                next_category = category_id + 1
            else:
                category = db.execute(
                    "SELECT name FROM entry_categories WHERE category_id=?", (category_id,)).fetchone()
                if not category:
                    raise EntryError("catalog_category_not_found", 404)
                category_name = category["name"]
                next_category = state["next_category_id"]

            existing = set()
            max_position = -1
            for row in db.execute("SELECT position,metadata_json FROM entry_products"):
                metadata = json.loads(row["metadata_json"])
                max_position = max(max_position, row["position"])
                existing.add((int(metadata.get("category_id", -1)),
                              str(metadata.get("model_name", "")).casefold(),
                              str(metadata.get("memory", "")).casefold(),
                              str(metadata.get("color", "")).casefold()))
            for variant in normalized:
                marker = (category_id, model_name.casefold(),
                          variant["memory"].casefold(), variant["color"].casefold())
                if marker in existing:
                    raise EntryError("catalog_product_exists", 409,
                                     memory=variant["memory"], color=variant["color"])

            product_id = state["next_product_id"]
            version_id = state["next_version_id"]
            created = []
            for offset, variant in enumerate(normalized):
                metadata = {"product_id": product_id, "model_name": model_name,
                            "color": variant["color"], "memory": variant["memory"],
                            "version_id_1": version_id, "version_id_12": version_id + 1,
                            "category_name": category_name, "category_id": category_id}
                product_key = identity(metadata)
                db.execute("INSERT INTO entry_products(product_key,position,metadata_json) VALUES (?,?,?)",
                           (product_key, max_position + 1 + offset,
                            json.dumps(metadata, ensure_ascii=False)))
                created.append({**metadata, "key": product_key})
                product_id += 1; version_id += 2
            db.execute("""UPDATE entry_catalog_state SET next_category_id=?,
                next_product_id=?,next_version_id=? WHERE id=1""",
                       (next_category, product_id, version_id))
            db.execute("UPDATE entry_input_state SET revision=revision+1,updated_at=? WHERE id=1", (now(),))
            result = {"status": "created", "created": created,
                      "created_count": len(created), "category_id": category_id,
                      "category_name": category_name, "import": "next_scheduled_import"}
            _store_operation(db, operation_id, request_hash, canonical, result)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise EntryError("catalog_create_integrity_failed")
            return result

    def update(self, anchor_product_id: int, body: dict, operation_id: str) -> dict:
        anchor_product_id = _positive(anchor_product_id)
        if not isinstance(body, dict) or set(body) != {
                "category_id", "model_name", "variants", "add_variants",
                "expected_revision"}:
            raise EntryError("invalid_catalog_request")
        category_id = _positive(body["category_id"])
        model_name = _text(body["model_name"], maximum=250, required=True)
        expected_revision = body["expected_revision"]
        if not isinstance(expected_revision, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_revision):
            raise EntryError("invalid_catalog_revision")
        variants = body["variants"]
        additions = body["add_variants"]
        if not isinstance(variants, list) or not variants or len(variants) > MAX_VARIANTS:
            raise EntryError("invalid_catalog_variants")
        if not isinstance(additions, list) or len(variants) + len(additions) > MAX_VARIANTS:
            raise EntryError("invalid_catalog_variants")

        normalized_existing, supplied_ids = [], set()
        for variant in variants:
            if not isinstance(variant, dict) or set(variant) != {
                    "product_id", "memory", "color"}:
                raise EntryError("invalid_catalog_variants")
            product_id = _positive(variant["product_id"])
            if product_id in supplied_ids:
                raise EntryError("duplicate_catalog_variant", 409)
            supplied_ids.add(product_id)
            normalized_existing.append({
                "product_id": product_id,
                "memory": _text(variant["memory"], maximum=100),
                "color": _text(variant["color"], maximum=150),
            })
        normalized_additions = []
        for variant in additions:
            if not isinstance(variant, dict) or set(variant) != {"memory", "color"}:
                raise EntryError("invalid_catalog_variants")
            normalized_additions.append({
                "memory": _text(variant["memory"], maximum=100),
                "color": _text(variant["color"], maximum=150),
            })
        markers = [
            (item["memory"].casefold(), item["color"].casefold())
            for item in normalized_existing + normalized_additions
        ]
        if len(markers) != len(set(markers)):
            raise EntryError("duplicate_catalog_variant", 409)
        canonical = {
            "action": "update",
            "anchor_product_id": anchor_product_id,
            "category_id": category_id,
            "model_name": model_name,
            "variants": normalized_existing,
            "add_variants": normalized_additions,
            "expected_revision": expected_revision,
        }
        request_hash = _digest(canonical)

        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            _tables(db)
            previous = _operation(db, operation_id, request_hash)
            if previous is not None:
                return previous
            state = db.execute("SELECT * FROM entry_catalog_state WHERE id=1").fetchone()
            if not state:
                raise EntryError("catalog_management_not_initialized", 503)
            category = db.execute(
                "SELECT name FROM entry_categories WHERE category_id=?", (category_id,)
            ).fetchone()
            if not category:
                raise EntryError("catalog_category_not_found", 404)
            rows = _metadata_rows(db)
            anchor = next((metadata for _, metadata in rows
                           if int(metadata["product_id"]) == anchor_product_id), None)
            if anchor is None:
                raise EntryError("catalog_model_not_found", 404)
            old_marker = (
                int(anchor["category_id"]), str(anchor["model_name"]).casefold()
            )
            group = [(row, metadata) for row, metadata in rows if (
                int(metadata["category_id"]), str(metadata["model_name"]).casefold()
            ) == old_marker]
            group_metadata = [metadata for _, metadata in group]
            if not secrets.compare_digest(
                    _model_revision(group_metadata), expected_revision):
                raise EntryError("catalog_model_changed", 409)
            current_ids = {int(metadata["product_id"]) for _, metadata in group}
            if supplied_ids != current_ids:
                raise EntryError("catalog_variants_changed", 409)
            own_ids = current_ids
            occupied = set()
            for _, metadata in rows:
                if int(metadata["product_id"]) in own_ids:
                    continue
                occupied.add((
                    int(metadata["category_id"]),
                    str(metadata["model_name"]).casefold(),
                    str(metadata.get("memory") or "").casefold(),
                    str(metadata.get("color") or "").casefold(),
                ))
            for item in normalized_existing + normalized_additions:
                marker = (
                    category_id, model_name.casefold(),
                    item["memory"].casefold(), item["color"].casefold(),
                )
                if marker in occupied:
                    raise EntryError("catalog_product_exists", 409,
                                     memory=item["memory"], color=item["color"])

            by_id = {int(metadata["product_id"]): (row, metadata)
                     for row, metadata in group}
            updated_count = 0
            for item in normalized_existing:
                row, metadata = by_id[item["product_id"]]
                updated = dict(metadata)
                updated.update(
                    category_id=category_id,
                    category_name=str(category["name"]),
                    model_name=model_name,
                    memory=item["memory"],
                    color=item["color"],
                )
                if updated != metadata:
                    db.execute(
                        "UPDATE entry_products SET metadata_json=? WHERE product_key=?",
                        (json.dumps(updated, ensure_ascii=False), row["product_key"]),
                    )
                    updated_count += 1

            product_id = int(state["next_product_id"])
            version_id = int(state["next_version_id"])
            max_position = max(int(row["position"]) for row, _ in rows)
            created = []
            for offset, item in enumerate(normalized_additions):
                metadata = {
                    "product_id": product_id,
                    "model_name": model_name,
                    "color": item["color"],
                    "memory": item["memory"],
                    "version_id_1": version_id,
                    "version_id_12": version_id + 1,
                    "category_name": str(category["name"]),
                    "category_id": category_id,
                }
                product_key = identity(metadata)
                db.execute(
                    "INSERT INTO entry_products(product_key,position,metadata_json) VALUES (?,?,?)",
                    (product_key, max_position + 1 + offset,
                     json.dumps(metadata, ensure_ascii=False)),
                )
                created.append({**metadata, "key": product_key})
                product_id += 1
                version_id += 2
            changed = bool(updated_count or created)
            if changed:
                db.execute(
                    "UPDATE entry_catalog_state SET next_product_id=?,next_version_id=? WHERE id=1",
                    (product_id, version_id),
                )
                db.execute(
                    "UPDATE entry_input_state SET revision=revision+1,updated_at=? WHERE id=1",
                    (now(),),
                )
            result = {
                "status": "updated",
                "updated_count": updated_count,
                "created_count": len(created),
                "created": created,
                "category_id": category_id,
                "category_name": str(category["name"]),
                "model_name": model_name,
                "import": "next_scheduled_import",
            }
            _store_operation(db, operation_id, request_hash, canonical, result)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise EntryError("catalog_update_integrity_failed")
            return result
