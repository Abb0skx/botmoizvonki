import copy
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from price_server.entry_migrate import check
from price_server.entry_routes import install_entry_routes
from price_server.entry_store import SQLitePriceSource, digest, initialize
from price_server.price_entry import EntryError, PriceEntryService, revision
from price_server.worker_input import decode, load_server_prices
from test_price_entry import sample


class SQLiteEntryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "db.sqlite"
        sqlite3.connect(self.path).close()
        self.catalogs = [{"sheet_id": 1, "title": "1-First", "rows": [sample()]},
                         {"sheet_id": 7, "title": "7-Reference", "rows": [sample()]}]
        self.catalogs[1]["rows"][0]["price_1"] = 95
        self.catalogs[1]["rows"][0]["revision"] = revision(self.catalogs[1]["rows"][0])
        initialize(self.path, self.catalogs)
        self.source = SQLitePriceSource(self.path)
        self.service = PriceEntryService(self.path, self.source)

    def edit(self, value=150):
        row = self.source.read(1)["rows"][0]
        return {"changes": [{"key": row["key"], "field": "price_1", "revision": row["revision"], "value": value}]}

    def test_migration_exact_and_normalized(self):
        self.assertEqual(check(self.path, self.catalogs)["supplier_product_rows"], 2)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entry_products").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())

    def test_idempotent_migration_never_overwrites_edits(self):
        self.service.save(1, self.edit(), str(uuid.uuid4()))
        self.assertEqual(initialize(self.path, self.catalogs)["status"], "already_initialized")
        self.assertEqual(self.source.read(1)["rows"][0]["price_1"], 150)
        changed = copy.deepcopy(self.catalogs)
        changed[0]["rows"][0]["price_1"] = 500
        with self.assertRaises(EntryError): initialize(self.path, changed)

    def test_migration_rejects_different_catalog_or_locks(self):
        for field, value in [("model_name", "Different"), ("locked", ["price_1"]), ("version_id_1", 11)]:
            catalogs = copy.deepcopy(self.catalogs)
            catalogs[1]["rows"][0][field] = value
            with self.assertRaises(EntryError): initialize(self.path, catalogs)
        self.assertEqual(check(self.path, self.catalogs)["status"], "equal")

    def test_save_atomic_replay_after_restart_and_conflict(self):
        body, operation = self.edit(), str(uuid.uuid4())
        result = self.service.save(1, body, operation)
        service = PriceEntryService(self.path, SQLitePriceSource(self.path))
        self.assertEqual(service.save(1, body, operation), result)
        with self.assertRaises(EntryError) as caught:
            service.save(1, body, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "prices_changed")
        self.assertEqual(len(service.history()["operations"]), 1)

    def test_minimum_recalculates_and_empty_price_is_sparse(self):
        self.service.save(1, self.edit(80), str(uuid.uuid4()))
        minimum = self.source.read(7)["rows"][0]
        self.assertEqual(minimum["min_price"], 80)
        self.assertEqual(minimum["min_supplier_name"], "First")
        self.assertEqual(minimum["min_supplier_id"], 1)
        self.assertEqual(minimum["min_supplier_sheet_id"], 1)
        self.assertEqual(minimum["min_price_field"], "price_1")
        self.service.save(1, self.edit(None), str(uuid.uuid4()))
        minimum = self.source.read(1)["rows"][0]
        self.assertEqual(minimum["min_price"], 95)
        self.assertEqual(minimum["min_supplier_name"], "Reference")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entry_prices WHERE sheet_id=1").fetchone()[0], 0)

    def test_minimum_history_tracks_price_supplier_and_warranty(self):
        operations = []

        def save(sheet_id, field, value):
            row = self.source.read(sheet_id)["rows"][0]
            operation = str(uuid.uuid4()); operations.append(operation)
            self.service.save(sheet_id, {"changes": [{
                "key": row["key"], "field": field,
                "revision": row["revision"], "value": value,
            }]}, operation)

        save(1, "price_1", 80)
        save(1, "price_1", 120)
        save(7, "price_12", 70)
        history = self.service.cell_history(1, sample()["key"], "min_price")
        self.assertEqual([entry["operation_id"] for entry in history["entries"]],
                         list(reversed(operations)))
        newest = history["entries"][0]
        self.assertEqual((newest["before"], newest["before_supplier_name"], newest["before_field"]),
                         (95, "Reference", "price_1"))
        self.assertEqual((newest["after"], newest["after_supplier_name"], newest["after_field"]),
                         (70, "Reference", "price_12"))
        switched = history["entries"][1]
        self.assertEqual((switched["before"], switched["before_supplier_name"]), (80, "First"))
        self.assertEqual((switched["after"], switched["after_supplier_name"]), (95, "Reference"))
        self.assertIsNone(history["next_before"])

    def test_empty_minimum_history_before_first_save(self):
        fresh = Path(self.folder.name) / "fresh.sqlite"
        sqlite3.connect(fresh).close()
        initialize(fresh, self.catalogs)
        service = PriceEntryService(fresh, SQLitePriceSource(fresh))
        history = service.cell_history(1, sample()["key"], "min_price")
        self.assertEqual(history["entries"], [])
        self.assertIsNone(history["next_before"])

    def test_failure_rolls_back_prices_and_journal_together(self):
        original = self.source.write
        def fail(*args):
            original(*args)
            raise RuntimeError("simulated disk failure")
        with patch.object(self.source, "write", side_effect=fail):
            with self.assertRaises(EntryError): self.service.save(1, self.edit(), str(uuid.uuid4()))
        self.assertEqual(self.source.read(1)["rows"][0]["price_1"], 100)
        self.assertEqual(self.service.history()["operations"], [])
        self.assertEqual(self.source.export()["revision"], 1)

    def test_uninitialized_local_mode_never_calls_google(self):
        path = Path(self.folder.name) / "empty.db"
        with patch.dict(os.environ, {"PRICE_ENTRY_SOURCE": "sqlite"}), patch("price_server.price_entry._google_client") as google:
            with self.assertRaises(EntryError): PriceEntryService(path).source.suppliers()
            google.assert_not_called()

    def test_maintenance_rejects_save(self):
        with patch.dict(os.environ, {"PRICE_ENTRY_WRITES_PAUSED": "true"}):
            with self.assertRaises(EntryError): self.service.save(1, self.edit(), str(uuid.uuid4()))
        self.assertEqual(self.source.export()["revision"], 1)

    def test_sqlite_backup_restores_all_input_and_audit(self):
        self.service.save(1, self.edit(), str(uuid.uuid4()))
        target = Path(self.folder.name) / "restored.db"
        with sqlite3.connect(self.path) as src, sqlite3.connect(target) as dst: src.backup(dst)
        self.assertEqual(SQLitePriceSource(target).export(), self.source.export())
        self.assertEqual(PriceEntryService(target, SQLitePriceSource(target)).history(), self.service.history())

    def test_export_reconstructs_worker_prices_exactly(self):
        values = decode(self.source.export())
        for catalog in self.catalogs:
            row = values[catalog["title"]][1]
            self.assertEqual(row[0], 1)
            self.assertEqual(row[5], catalog["rows"][0]["price_1"])
            self.assertEqual(row[7], "")

    def test_worker_rejects_tampering_duplicate_and_invalid_prices(self):
        original = self.source.export()
        changed = copy.deepcopy(original); changed["prices"][0][2] = 999
        with self.assertRaises(RuntimeError): decode(changed)
        for mutation in [lambda d: d["prices"].append(d["prices"][0]),
                         lambda d: d["prices"][0].__setitem__(2, True),
                         lambda d: d["suppliers"].append(d["suppliers"][0]),
                         lambda d: d["products"].append(d["products"][0])]:
            changed = copy.deepcopy(original); changed.pop("content_hash"); mutation(changed)
            changed["content_hash"] = digest(changed)
            with self.assertRaises(RuntimeError): decode(changed)

    def test_worker_no_insecure_url_and_no_error_secrets(self):
        with patch.dict(os.environ, {"PRICE_SERVER_URL": "http://example.com", "PRICE_SYNC_API_KEY": "x" * 32}):
            with self.assertRaises(RuntimeError): load_server_prices()
        import requests
        with patch.dict(os.environ, {"PRICE_SERVER_URL": "https://example.com", "PRICE_SYNC_API_KEY": "x" * 32}), patch("requests.get", side_effect=requests.RequestException("SECRET")):
            with self.assertRaises(RuntimeError) as caught: load_server_prices()
            self.assertNotIn("SECRET", str(caught.exception))

    def test_export_auth_and_source_gate(self):
        router = APIRouter(); app = FastAPI()
        install_entry_routes(router, lambda *a, **kw: None, lambda: None,
                             SimpleNamespace(db_path=self.path, sync_api_key="test-key"))
        app.include_router(router)
        client = TestClient(app)
        url = "/price/api/v1/entry/export"
        self.assertEqual(client.get(url).status_code, 401)
        self.assertEqual(client.get(url, headers={"X-Price-Sync-Key": "wrong"}).status_code, 401)
        headers = {"X-Price-Sync-Key": "test-key"}
        with patch.dict(os.environ, {"PRICE_ENTRY_SOURCE": "google_sheets"}):
            self.assertEqual(client.get(url, headers=headers).status_code, 409)
        with patch.dict(os.environ, {"PRICE_ENTRY_SOURCE": "sqlite"}):
            response = client.get(url, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(decode(response.json())["1-First"][1][5], 100)


if __name__ == "__main__": unittest.main()
