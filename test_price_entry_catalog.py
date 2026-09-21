import copy
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from price_server.entry_catalog import EntryCatalogService, install_catalog
from price_server.entry_routes import install_entry_routes
from price_server.entry_store import SQLitePriceSource, initialize
from price_server.model_inbox import ModelInbox, parse_model_draft, parse_model_drafts
from price_server.model_import import prepare_model_import
from price_server.price_entry import EntryError, revision
from price_server.worker_input import decode, reconcile_catalog
from test_price_entry import sample


class EntryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "price.db"
        first = sample()
        second = copy.deepcopy(first)
        second.update(product_id=2, version_id_1=12, version_id_12=13,
                      model_name="Other", category_name="Tablets")
        second.update(key="2:12:13", revision=revision(second))
        catalogs = [
            {"sheet_id": 1, "title": "1-First", "rows": [first, second]},
            {"sheet_id": 2, "title": "2-Second", "rows": [first, second]},
        ]
        initialize(self.path, catalogs)
        self.categories = [{"category_id": 10, "name": "Phones"},
                           {"category_id": 20, "name": "Tablets"}]
        install_catalog(self.path, self.categories,
                        max_product_id=1000, max_version_id=2000)
        self.service = EntryCatalogService(self.path)

    def request(self, **changes):
        body = {"category_id": 10, "new_category_name": None,
                "model_name": "New Phone", "variants": [
                    {"memory": "256 GB", "color": "Black"},
                    {"memory": "512 GB", "color": "Silver"},
                ]}
        body.update(changes)
        return body

    def test_install_adds_only_category_metadata_and_safe_high_water(self):
        source = SQLitePriceSource(self.path)
        exported = source.export()
        self.assertEqual(exported["categories"], self.categories)
        self.assertEqual([p["category_id"] for p in exported["products"]], [10, 20])
        self.assertEqual(install_catalog(
            self.path, self.categories, max_product_id=1000,
            max_version_id=2000)["status"], "already_initialized")
        state = self.service.categories()
        self.assertEqual(state["next_product_id"], 1001)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entry_prices").fetchone()[0], 4)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_create_variants_uses_explicit_ids_and_is_visible_to_all_suppliers(self):
        operation = str(uuid.uuid4())
        result = self.service.create(self.request(), operation)
        self.assertEqual(result["created_count"], 2)
        self.assertEqual([r["product_id"] for r in result["created"]], [1001, 1002])
        self.assertEqual([r["version_id_1"] for r in result["created"]], [2001, 2003])
        self.assertEqual([r["version_id_12"] for r in result["created"]], [2002, 2004])
        self.assertEqual(self.service.create(self.request(), operation), result)
        source = SQLitePriceSource(self.path)
        for sheet_id in (1, 2):
            rows = [r for r in source.read(sheet_id)["rows"] if r["model_name"] == "New Phone"]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["price_1"] is None and r["price_12"] is None for r in rows))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entry_catalog_operations").fetchone()[0], 1)

    def test_create_new_category_and_reject_duplicates_without_partial_rows(self):
        result = self.service.create(self.request(
            category_id=None, new_category_name="Wearables",
            variants=[{"memory": "", "color": "Green"}]), str(uuid.uuid4()))
        self.assertEqual(result["category_id"], 21)
        self.assertIn("Wearables", [c["name"] for c in self.service.categories()["categories"]])
        before = len(SQLitePriceSource(self.path).read(1)["rows"])
        with self.assertRaises(EntryError) as caught:
            self.service.create(self.request(
                variants=[{"memory": "256 GB", "color": "Black"},
                          {"memory": "256 GB", "color": "Black"}]), str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "duplicate_catalog_variant")
        self.assertEqual(len(SQLitePriceSource(self.path).read(1)["rows"]), before)

    def test_existing_variant_and_idempotency_conflict_are_safe(self):
        operation = str(uuid.uuid4())
        self.service.create(self.request(variants=[{"memory": "", "color": ""}]), operation)
        with self.assertRaises(EntryError) as caught:
            self.service.create(self.request(model_name="Different",
                                             variants=[{"memory": "", "color": ""}]), operation)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        with self.assertRaises(EntryError) as caught:
            self.service.create(self.request(variants=[{"memory": "", "color": ""}]), str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "catalog_product_exists")

    def test_model_listing_and_update_preserve_ids_prices_and_add_variants(self):
        before = SQLitePriceSource(self.path).read(1)["rows"][0]
        detail = self.service.model(before["product_id"])
        body = {
            "category_id": 20,
            "model_name": "Renamed Phone",
            "variants": [{
                "product_id": before["product_id"],
                "memory": "1 TB",
                "color": "Blue",
            }],
            "add_variants": [{"memory": "2 TB", "color": "Silver"}],
            "expected_revision": detail["revision"],
        }
        operation = str(uuid.uuid4())
        result = self.service.update(before["product_id"], body, operation)
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(self.service.update(before["product_id"], body, operation), result)
        rows = [row for row in SQLitePriceSource(self.path).read(1)["rows"]
                if row["model_name"] == "Renamed Phone"]
        self.assertEqual(len(rows), 2)
        existing = next(row for row in rows if row["product_id"] == before["product_id"])
        self.assertEqual(existing["category_id"], 20)
        self.assertEqual(existing["category_name"], "Tablets")
        self.assertEqual(existing["price_1"], before["price_1"])
        self.assertEqual(existing["price_12"], before["price_12"])
        added = next(row for row in rows if row["product_id"] != before["product_id"])
        self.assertIsNone(added["price_1"])
        self.assertIsNone(added["price_12"])
        listing = self.service.models()
        self.assertIn("Renamed Phone", [item["model_name"] for item in listing["models"]])

    def test_model_update_rejects_stale_or_missing_variants_atomically(self):
        detail = self.service.model(1)
        request = {
            "category_id": detail["category_id"],
            "model_name": detail["model_name"],
            "variants": [],
            "add_variants": [],
            "expected_revision": detail["revision"],
        }
        with self.assertRaises(EntryError) as caught:
            self.service.update(1, request, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "invalid_catalog_variants")
        request["variants"] = [{"product_id": 1, "memory": "", "color": "Black"}]
        request["expected_revision"] = "0" * 64
        with self.assertRaises(EntryError) as caught:
            self.service.update(1, request, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "catalog_model_changed")

    def test_large_existing_model_remains_editable(self):
        variants = [{"memory": f"{index} GB", "color": "Black"}
                    for index in range(101)]
        created = self.service.create(
            self.request(model_name="Large Model", variants=variants), str(uuid.uuid4()))
        anchor = created["created"][0]["product_id"]
        detail = self.service.model(anchor)
        result = self.service.update(anchor, {
            "category_id": detail["category_id"],
            "model_name": "Large Model Updated",
            "variants": [{
                "product_id": item["product_id"],
                "memory": item["memory"],
                "color": item["color"],
            } for item in detail["variants"]],
            "add_variants": [],
            "expected_revision": detail["revision"],
        }, str(uuid.uuid4()))
        self.assertEqual(result["updated_count"], 101)

    def test_site_import_previews_selected_category_and_sorting_lists(self):
        text = """0. 26
1. Apple Watch Series 12 Aluminum
2. 42mm, 46mm
3. Black, Dark Bronze, Light Gold, Space Gray

0. 26
1. Apple Watch Series 12 Ceramic
2. 42mm, 46mm
3. Night Blue, Pearl White
"""
        preview = self.service.preview_import(text, 10)
        self.assertEqual(preview["category_name"], "Phones")
        self.assertEqual(preview["model_count"], 2)
        self.assertEqual(preview["variant_count"], 16)
        self.assertEqual(preview["source_category_ids"], [26])
        self.assertEqual(preview["warnings"], ["source_category_ignored"])
        self.assertEqual(preview["sorting"]["memories"], ["42mm", "46mm"])
        self.assertIn("Space Gray", preview["sorting"]["colors"])

    def test_site_import_is_atomic_and_idempotent(self):
        text = """0. 26
1. Watch One
2. 42mm, 46mm
3. Black, Silver
1. Watch Two
2. 49mm
3. Natural
"""
        preview = self.service.preview_import(text, 10)
        operation = str(uuid.uuid4())
        result = self.service.import_models(
            text, 10, preview["preview_hash"], operation)
        self.assertEqual(result["model_count"], 2)
        self.assertEqual(result["created_count"], 7)
        self.assertEqual(self.service.import_models(
            text, 10, preview["preview_hash"], operation), result)
        self.assertIn("Watch One", [item["model_name"] for item in self.service.models()["models"]])

    def test_site_import_rejects_changed_preview_and_existing_model_without_partial_write(self):
        text = "0. 26\n1. New One\n2. 42mm\n3. Black"
        preview = self.service.preview_import(text, 10)
        with self.assertRaises(EntryError) as caught:
            self.service.import_models(text + "\n", 10, "0" * 64, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "catalog_preview_changed")
        self.service.create(self.request(model_name="Already Here",
                                         variants=[{"memory": "", "color": ""}]),
                            str(uuid.uuid4()))
        before = self.service.models()["model_count"]
        collision = "0. 10\n1. Fresh\n2. 1\n3. A\n1. Already Here\n2. 2\n3. B"
        collision_preview = prepare_model_import(collision, 10)
        with self.assertRaises(EntryError) as caught:
            self.service.import_models(collision, 10, collision_preview["preview_hash"],
                                       str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "catalog_model_exists")
        self.assertEqual(self.service.models()["model_count"], before)

    def test_install_rejects_unknown_category_and_high_water_regression(self):
        other = Path(self.folder.name) / "other.db"
        initialize(other, [{"sheet_id": 1, "title": "1-First", "rows": [sample()]}])
        with self.assertRaises(EntryError):
            install_catalog(other, [{"category_id": 1, "name": "Wrong"}],
                            max_product_id=1, max_version_id=11)
        with self.assertRaises(EntryError) as caught:
            install_catalog(self.path, self.categories,
                            max_product_id=5000, max_version_id=6000)
        self.assertEqual(caught.exception.code, "catalog_id_high_water_conflict")

    def worker_db(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript("""
            CREATE TABLE categories(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE products(id INTEGER PRIMARY KEY,model_name TEXT NOT NULL,
                category_id INTEGER,memory TEXT,color TEXT);
            CREATE TABLE product_versions(version_id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL,warranty_period TEXT NOT NULL);
        """)
        connection.execute("INSERT INTO categories VALUES (10,'Phones')")
        connection.execute("INSERT INTO categories VALUES (20,'Tablets')")
        for product in SQLitePriceSource(self.path).export()["products"]:
            connection.execute("INSERT INTO products VALUES (?,?,?,?,?)", (
                product["product_id"], product["model_name"], product["category_id"],
                product["memory"], product["color"]))
            connection.execute("INSERT INTO product_versions VALUES (?,?,?)",
                               (product["version_id_1"], product["product_id"], "1"))
            connection.execute("INSERT INTO product_versions VALUES (?,?,?)",
                               (product["version_id_12"], product["product_id"], "12"))
        connection.commit()
        return connection

    def test_worker_reconcile_adds_new_products_and_never_deletes_legacy(self):
        worker = self.worker_db()
        worker.execute("INSERT INTO products VALUES (999,'Legacy',10,'','')")
        worker.execute("INSERT INTO product_versions VALUES (9999,999,'1')")
        self.service.create(self.request(), str(uuid.uuid4()))
        data = decode(SQLitePriceSource(self.path).export())
        self.assertEqual(reconcile_catalog(worker, data), 2)
        self.assertEqual(reconcile_catalog(worker, data), 0)
        self.assertEqual(worker.execute("SELECT count(*) FROM products WHERE id=999").fetchone()[0], 1)
        self.assertEqual(worker.execute("SELECT count(*) FROM products WHERE id IN (1001,1002)").fetchone()[0], 2)
        self.assertEqual(worker.execute("SELECT count(*) FROM product_versions WHERE version_id BETWEEN 2001 AND 2004").fetchone()[0], 4)

    def test_worker_conflict_can_be_rolled_back_by_import_transaction(self):
        worker = self.worker_db()
        self.service.create(self.request(), str(uuid.uuid4()))
        data = decode(SQLitePriceSource(self.path).export())
        worker.execute("INSERT INTO product_versions VALUES (2001,1,'12')")
        worker.commit()
        worker.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            reconcile_catalog(worker, data)
        worker.rollback()
        self.assertEqual(worker.execute("SELECT count(*) FROM products WHERE id=1001").fetchone()[0], 0)

    def test_worker_reconcile_applies_server_authoritative_model_edits(self):
        worker = self.worker_db()
        detail = self.service.model(1)
        self.service.update(1, {
            "category_id": 20,
            "model_name": "Server Name",
            "variants": [{"product_id": 1, "memory": "512 GB", "color": "Green"}],
            "add_variants": [],
            "expected_revision": detail["revision"],
        }, str(uuid.uuid4()))
        data = decode(SQLitePriceSource(self.path).export())
        self.assertEqual(reconcile_catalog(worker, data), 0)
        self.assertEqual(worker.execute(
            "SELECT model_name,category_id,memory,color FROM products WHERE id=1"
        ).fetchone(), ("Server Name", 20, "512 GB", "Green"))

    def test_worker_reconcile_allows_atomic_variant_swap(self):
        self.service.create(self.request(model_name="Swap Phone"), str(uuid.uuid4()))
        worker = self.worker_db()
        detail = self.service.model(1001)
        self.service.update(1001, {
            "category_id": 10,
            "model_name": "Swap Phone",
            "variants": [
                {"product_id": 1001, "memory": "512 GB", "color": "Silver"},
                {"product_id": 1002, "memory": "256 GB", "color": "Black"},
            ],
            "add_variants": [],
            "expected_revision": detail["revision"],
        }, str(uuid.uuid4()))
        data = decode(SQLitePriceSource(self.path).export())
        self.assertEqual(reconcile_catalog(worker, data), 0)
        self.assertEqual(worker.execute(
            "SELECT memory,color FROM products WHERE id=1001"
        ).fetchone(), ("512 GB", "Silver"))
        self.assertEqual(worker.execute(
            "SELECT memory,color FROM products WHERE id=1002"
        ).fetchone(), ("256 GB", "Black"))


class EntryCatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.admin = Mock()
        router = APIRouter()
        install_entry_routes(router, self.admin, lambda: None,
                             SimpleNamespace(db_path=Path("unused.db"), sync_api_key="key"))
        app = FastAPI(); app.include_router(router)
        self.client = TestClient(app)

    def test_catalog_routes_are_protected_and_bounded(self):
        self.admin.side_effect = HTTPException(401)
        self.assertEqual(self.client.get("/price/api/v1/entry/categories").status_code, 401)
        self.assertEqual(self.client.post("/price/api/v1/entry/products", json={}).status_code, 401)
        self.admin.side_effect = None
        with patch("price_server.entry_routes.EntryCatalogService") as service:
            service.return_value.categories.return_value = {"categories": []}
            self.assertEqual(self.client.get("/price/api/v1/entry/categories").status_code, 200)
            service.return_value.create.return_value = {"status": "created"}
            response = self.client.post("/price/api/v1/entry/products", json={}, headers={
                "Idempotency-Key": "123e4567-e89b-42d3-a456-426614174000"})
            self.assertEqual(response.status_code, 200)
            service.return_value.models.return_value = {"models": []}
            service.return_value.model.return_value = {"variants": []}
            self.assertEqual(self.client.get("/price/api/v1/entry/models").status_code, 200)
            self.assertEqual(self.client.get("/price/api/v1/entry/models/1").status_code, 200)
            service.return_value.update.return_value = {"status": "updated"}
            self.assertEqual(self.client.post(
                "/price/api/v1/entry/models/1", json={}, headers={
                    "Idempotency-Key": "123e4567-e89b-42d3-a456-426614174000"
                }).status_code, 200)
            service.return_value.preview_import.return_value = {"models": []}
            self.assertEqual(self.client.post(
                "/price/api/v1/entry/model-import/preview",
                json={"category_id": 10, "text": "0. 10"}).status_code, 200)
            service.return_value.import_models.return_value = {"status": "created"}
            self.assertEqual(self.client.post(
                "/price/api/v1/entry/model-import/apply",
                json={"category_id": 10, "text": "0. 10", "preview_hash": "a" * 64},
                headers={"Idempotency-Key": "123e4567-e89b-42d3-a456-426614174000"}
            ).status_code, 200)
        self.assertEqual(self.client.post(
            "/price/api/v1/entry/products", content=b"x" * 65537).status_code, 413)


class ModelInboxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "price.db"
        self.inbox = ModelInbox(self.path)

    def test_parser_supports_labels_and_comma_separated_colors(self):
        parsed = parse_model_draft("""Добавить модель
Категория: Смартфоны бренда Apple
Модель: Apple iPhone 18 Pro
256 GB | Black, Silver
512 GB | Blue
""")
        self.assertEqual(parsed["model_name"], "Apple iPhone 18 Pro")
        self.assertEqual(parsed["variants"], [
            {"memory": "256 GB", "color": "Black"},
            {"memory": "256 GB", "color": "Silver"},
            {"memory": "512 GB", "color": "Blue"},
        ])

    def test_parser_preserves_existing_model_yegish_multi_model_format(self):
        parsed = parse_model_drafts("""0. 3
1. Samsung Galaxy A57 5G
2. 8/128Gb, 8/256Gb
3. Black, Gray, Blue

1. Samsung Galaxy A37 5G
2. 8/128Gb
3. Black
""")
        self.assertEqual([item["model_name"] for item in parsed], [
            "Samsung Galaxy A57 5G", "Samsung Galaxy A37 5G",
        ])
        self.assertTrue(all(item["category_id"] == 3 for item in parsed))
        self.assertEqual(len(parsed[0]["variants"]), 8)
        self.assertIn({"memory": "8/128Gb", "color": ""}, parsed[0]["variants"])
        self.assertEqual(parsed[1]["variants"], [
            {"memory": "8/128Gb", "color": "Black"},
        ])

    def test_channel_post_is_idempotent_editable_draft(self):
        first = self.inbox.record(update_id=10, chat_id="-1001", message_id=22,
                                  text="Phones\nModel One\n256 GB | Black")
        second = self.inbox.record(update_id=11, chat_id="-1001", message_id=22,
                                   text="Phones\nModel Two\n512 GB | Silver")
        self.assertEqual(first["draft_id"], second["draft_id"])
        drafts = self.inbox.list()["drafts"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["parsed"]["model_name"], "Model Two")
        self.inbox.finish(first["draft_id"], status="applied")
        locked = self.inbox.record(update_id=12, chat_id="-1001", message_id=22,
                                   text="Phones\nChanged\n1 TB | Gold")
        self.assertEqual(locked["status"], "applied")
        self.assertEqual(self.inbox.list()["drafts"], [])

    def test_one_group_message_creates_one_reviewable_draft_per_model(self):
        result = self.inbox.record(
            update_id=20, chat_id="-5581249831", message_id=30,
            text="0. 3\n1. Model A\n2. 128Gb\n3. Black\n1. Model B\n3. Blue",
        )
        self.assertEqual(len(result["draft_ids"]), 2)
        drafts = self.inbox.list()["drafts"]
        self.assertEqual({item["parsed"]["model_name"] for item in drafts},
                         {"Model A", "Model B"})

    def test_invalid_post_is_visible_and_dismissible(self):
        result = self.inbox.record(update_id=1, chat_id="-1001", message_id=1,
                                   text="not enough")
        self.assertEqual(result["status"], "invalid")
        draft = self.inbox.list()["drafts"][0]
        self.assertEqual(draft["parse_error"], "model_draft_headers_required")
        self.assertEqual(self.inbox.finish(draft["draft_id"], status="dismissed")["status"],
                         "dismissed")


if __name__ == "__main__":
    unittest.main()
