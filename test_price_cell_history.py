import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from price_server.entry_routes import install_entry_routes
from price_server.price_entry import EntryError, PriceEntryService
from test_price_entry import Source, sample


class CellHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "price.db"
        self.source = Source()
        self.service = PriceEntryService(self.path, self.source)
        self.key = sample()["key"]

    def insert(self, number, *, sheet=1, key=None, field="price_1", status="applied", extra=None):
        change = {"key": key or self.key, "field": field, "before": number, "after": number+1}
        with self.service.database() as db:
            db.execute("""INSERT INTO price_entry_operations
                (operation_id,request_hash,sheet_id,created_at,updated_at,status,changes_json)
                VALUES (?,?,?,?,?,?,?)""", (f"op-{number}", "hash", sheet,
                "2026-09-21T13:00:00+00:00", "2026-09-21T13:00:01+00:00", status,
                json.dumps([change] + (extra or []))))

    def test_cell_filters_supplier_product_and_warranty(self):
        self.insert(1)
        self.insert(2, sheet=2)
        self.insert(3, key="2:20:21")
        self.insert(4, field="price_12")
        data = self.service.cell_history(1, self.key, "price_1")
        self.assertEqual([e["operation_id"] for e in data["entries"]], ["op-1"])
        self.assertIsNone(data["next_before"])
        self.assertEqual(data["entries"][0]["before"], 1)
        self.assertEqual(data["entries"][0]["after"], 2)

    def test_journal_beyond_global_last_100_is_available(self):
        self.insert(1)
        for n in range(2, 110): self.insert(n, key="2:20:21")
        self.assertNotIn("op-1", [o["operation_id"] for o in self.service.history()["operations"]])
        self.assertEqual(self.service.cell_history(1, self.key, "price_1")["entries"][0]["operation_id"], "op-1")

    def test_cursor_pagination_survives_new_writes_without_duplicates(self):
        for n in range(1, 122): self.insert(n)
        first = self.service.cell_history(1, self.key, "price_1")
        self.assertEqual(len(first["entries"]), 50)
        self.insert(122)
        second = self.service.cell_history(1, self.key, "price_1", first["next_before"])
        third = self.service.cell_history(1, self.key, "price_1", second["next_before"])
        ids = [e["operation_id"] for p in [first, second, third] for e in p["entries"]]
        self.assertEqual(ids, [f"op-{n}" for n in range(121, 0, -1)])
        self.assertEqual(len(third["entries"]), 21)
        self.assertIsNone(third["next_before"])

    def test_exact_page_has_no_extra_page(self):
        for n in range(50): self.insert(n)
        data = self.service.cell_history(1, self.key, "price_1")
        self.assertEqual(len(data["entries"]), 50)
        self.assertIsNone(data["next_before"])

    def test_bulk_operation_returns_only_selected_cell(self):
        self.insert(1, extra=[{"key": self.key, "field": "price_12", "before": None, "after": 200},
                              {"key": "2:20:21", "field": "price_1", "before": 2, "after": 3}])
        data = self.service.cell_history(1, self.key, "price_12")
        self.assertEqual(len(data["entries"]), 1)
        self.assertIsNone(data["entries"][0]["before"])
        self.assertEqual(data["entries"][0]["after"], 200)

    def test_actual_save_clear_and_idempotent_retry(self):
        row = self.source.row
        body = {"changes": [{"key": row["key"], "field": "price_1", "revision": row["revision"], "value": None}]}
        operation = str(uuid.uuid4())
        self.service.save(1, body, operation)
        self.service.save(1, body, operation)
        restarted = PriceEntryService(self.path, Source())
        entries = restarted.cell_history(1, self.key, "price_1")["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["before"], 100)
        self.assertIsNone(entries[0]["after"])

    def test_empty_cell_never_contacts_google(self):
        with patch("price_server.price_entry._google_client") as google:
            service = PriceEntryService(self.path)
            data = service.cell_history(1, self.key, "price_12")
        self.assertEqual(data["entries"], [])
        self.assertIsNone(data["next_before"])
        google.assert_not_called()

    def test_uncertain_intent_remains_labelled(self):
        self.insert(1, status="uncertain")
        self.insert(2, status="sending")
        entries = self.service.cell_history(1, self.key, "price_1")["entries"]
        self.assertEqual([e["status"] for e in entries], ["sending", "uncertain"])

    def test_invalid_cell_and_cursor_rejected(self):
        invalid = [(-1,self.key,"price_1",0), (1,"bad","price_1",0),
                   (1,"0:10:11","price_1",0), (1,self.key,"min_price",0),
                   (1,self.key,"price_1",-1), (1,self.key,"price_1",2**63),
                   (1,"1:10:11' OR 1=1--","price_1",0)]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(EntryError): self.service.cell_history(*args)


class CellHistoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.admin = Mock(); router = APIRouter(); app = FastAPI()
        install_entry_routes(router, self.admin, lambda: None,
                             SimpleNamespace(db_path=Path("unused.db"), timezone="Asia/Tashkent"))
        app.include_router(router); self.client = TestClient(app)
        self.url = "/price/api/v1/entry/cell-history/1/1:10:11/price_1/0"

    def test_auth_before_history_lookup(self):
        self.admin.side_effect = HTTPException(401)
        with patch("price_server.entry_routes.PriceEntryService") as service:
            self.assertEqual(self.client.get(self.url).status_code, 401)
            service.assert_not_called()

    def test_route_preserves_exact_cell_and_is_not_cached(self):
        with patch("price_server.entry_routes.PriceEntryService") as service:
            service.return_value.cell_history.return_value = {"entries": [], "next_before": None}
            response = self.client.get(self.url)
            service.return_value.cell_history.assert_called_once_with(1, "1:10:11", "price_1", 0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["timezone"], "Asia/Tashkent")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(self.admin.call_args.kwargs["action"])

    def test_error_is_safe_and_invalid_selector_rejected(self):
        with patch("price_server.entry_routes.PriceEntryService") as service:
            service.return_value.cell_history.side_effect = EntryError("invalid_history_cell")
            self.assertEqual(self.client.get(self.url).status_code, 400)
            service.return_value.cell_history.side_effect = RuntimeError("SECRET")
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.text)


if __name__ == "__main__": unittest.main()
