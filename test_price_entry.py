import copy
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
from price_server.price_entry import (
    EntryError, HEADERS, PriceEntryService, SheetsPriceSource, identity, price, revision,
)


def sample():
    row = dict(zip(HEADERS, [1, "Test phone", "Black", "256 GB", 10, 100, 11, None, 95, "Phones"]))
    row.update(key=identity(row), row=2, locked=[], revision=revision(row))
    return row


class Source:
    def __init__(self):
        self.row = sample()
        self.calls = []
        self.fail = False

    def read(self, sheet_id):
        return {"rows": [copy.deepcopy(self.row)]}

    def write(self, sheet_id, changes):
        self.calls.append((sheet_id, changes))
        if self.fail:
            raise TimeoutError("SECRET must never be exposed")
        for change in changes:
            self.row[change["field"]] = change["after"]
        self.row["revision"] = revision(self.row)


class EntryServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Source()
        self.service = PriceEntryService(Path(self.folder.name) / "price.db", self.source)
        self.op = str(uuid.uuid4())

    def body(self, value=150):
        row = sample()
        return {"changes": [{"key": row["key"], "field": "price_1", "revision": row["revision"], "value": value}]}

    def error(self, code, body=None, operation=None):
        with self.assertRaises(EntryError) as caught:
            self.service.save(123, self.body() if body is None else body, operation or self.op)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_integer_validation(self):
        for v in [None, "", 0, "0", 100000, "0012"]:
            price(v)
        for v in [True, -1, 100001, "1.5", "=1+1", "NaN", "inf", "1e3", "1,000", [], {}, 1.5]:
            with self.assertRaises(EntryError):
                price(v)

    def test_save_verified_and_replay_exactly_once(self):
        result = self.service.save(123, self.body(), self.op)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.source.row["price_1"], 150)
        self.assertEqual(self.service.save(123, self.body(), self.op), result)
        self.assertEqual(len(self.source.calls), 1)
        history = self.service.history()["operations"]
        self.assertEqual(history[0]["changes"][0]["before"], 100)
        self.assertEqual(history[0]["status"], "applied")

    def test_clear_price_is_explicit(self):
        self.service.save(123, self.body(None), self.op)
        self.assertIsNone(self.source.row["price_1"])

    def test_same_key_cannot_save_different_payload(self):
        self.service.save(123, self.body(), self.op)
        self.error("idempotency_conflict", self.body(200))
        self.assertEqual(len(self.source.calls), 1)

    def test_new_sheet_edit_conflicts_before_write(self):
        self.source.row["price_12"] = 200
        self.source.row["revision"] = revision(self.source.row)
        self.error("prices_changed")
        self.assertEqual(self.source.calls, [])

    def test_changed_identity_conflicts(self):
        self.source.row["key"] = "2:20:21"
        self.error("prices_changed")

    def test_model_change_is_conflict(self):
        self.source.row["model_name"] = "Other model"
        self.source.row["revision"] = revision(self.source.row)
        self.error("prices_changed")

    def test_row_movement_resolved_by_ids(self):
        self.source.row["row"] = 456
        self.service.save(123, self.body(), self.op)
        self.assertEqual(self.source.calls[0][1][0]["row"], 456)

    def test_formula_and_protected_prices_never_written(self):
        self.source.row["locked"] = ["price_1"]
        self.error("cell_read_only")
        self.assertEqual(self.source.calls, [])

    def test_bad_requests_do_not_write(self):
        self.error("idempotency_key_required", operation="bad")
        for body in [{}, [], {"changes": []}, {"changes": [self.body()["changes"][0]] * 201}]:
            with self.assertRaises(EntryError):
                self.service.save(123, body, self.op)
        body = self.body(); body["changes"][0]["field"] = "product_id"
        self.error("invalid_or_duplicate_field", body)
        body = self.body(); body["changes"].append(body["changes"][0])
        self.error("invalid_or_duplicate_field", body)
        body = self.body(); body["changes"][0]["row"] = 444
        self.error("invalid_request", body)
        self.assertEqual(self.source.calls, [])

    def test_timeout_journaled_and_never_retried(self):
        self.source.fail = True
        self.error("save_outcome_unknown")
        restarted = PriceEntryService(self.service.db_path, self.source)
        with self.assertRaisesRegex(EntryError, "save_outcome_unknown"):
            restarted.save(123, self.body(), self.op)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(restarted.history()["operations"][0]["status"], "uncertain")

    def test_verified_different_value_is_uncertain(self):
        self.source.write = Mock()
        self.error("save_outcome_unknown")
        self.assertEqual(self.service.history()["operations"][0]["status"], "uncertain")

    def test_db_lock_blocks_parallel_site_writes(self):
        with self.service.write_lock():
            self.error("save_in_progress")
        self.assertEqual(self.source.calls, [])

    def test_minimum_formula_change_not_a_conflict(self):
        row = sample(); old = revision(row); row["min_price"] = 70
        self.assertEqual(old, revision(row))

    def test_unchanged_prices_not_written(self):
        self.assertEqual(self.service.save(123, self.body(100), self.op)["status"], "unchanged")
        self.assertEqual(self.source.calls, [])

    def test_second_cell_conflict_means_no_partial_save(self):
        body = self.body()
        body["changes"].append({**body["changes"][0], "field": "price_12", "revision": "stale"})
        self.error("prices_changed", body)
        self.assertEqual(self.source.calls, [])


class SheetsAdapterTests(unittest.TestCase):
    def grid(self, field_formula=False, protected=False, invalid_ids=False):
        p = {"sheetId": 99, "title": "7-Jovoh", "gridProperties": {"rowCount": 5}}
        row = [1, "Phone", "Black", "256 GB", 10, 100, 11, "", 95, "Phones"]
        if invalid_ids:
            row[0] = ""
        cells = [{"userEnteredValue": {"numberValue" if isinstance(x, int) else "stringValue": x}} for x in row]
        cells[8] = {"userEnteredValue": {"formulaValue": "=MIN(F2;H2)"}, "effectiveValue": {"numberValue": 95}}
        if field_formula:
            cells[5] = {"userEnteredValue": {"formulaValue": "=1+99"}, "effectiveValue": {"numberValue": 100}}
        sheet = {"properties": p, "data": [{"rowData": [
            {"values": [{"userEnteredValue": {"stringValue": x}} for x in HEADERS]}, {"values": cells}]}]}
        if protected:
            sheet["protectedRanges"] = [{"range": {"startColumnIndex": 7, "endColumnIndex": 8}}]
        return sheet

    def source(self, sheet):
        source = SheetsPriceSource("test-book")
        book = Mock()
        source._book = book
        book.fetch_sheet_metadata.side_effect = [{"sheets": [{"properties": sheet["properties"]}]}, {"sheets": [{"properties": {"sheetId": 1}}, sheet]}]
        return source

    def test_reads_matching_sheet_not_first_sheet(self):
        data = self.source(self.grid()).read(99)
        self.assertEqual(data["rows"][0]["price_1"], 100)
        self.assertEqual(data["rows"][0]["min_price"], 95)
        self.assertEqual(data["rows"][0]["locked"], [])

    def test_formula_protection_validation_and_bad_ids_lock(self):
        row = self.source(self.grid(True, True)).read(99)["rows"][0]
        self.assertIn("price_1", row["locked"]); self.assertIn("price_12", row["locked"])
        row = self.source(self.grid(invalid_ids=True)).read(99)["rows"][0]
        self.assertIn("price_1", row["locked"])
        grid = self.grid(); grid["data"][0]["rowData"][1]["values"][5]["dataValidation"] = {"strict": True}
        self.assertIn("price_1", self.source(grid).read(99)["rows"][0]["locked"])

    def test_rejects_duplicate_ids_or_changed_headers(self):
        grid = self.grid(); grid["data"][0]["rowData"].append(grid["data"][0]["rowData"][1])
        with self.assertRaisesRegex(EntryError, "duplicate_product_identity"):
            self.source(grid).read(99)
        grid = self.grid(); grid["data"][0]["rowData"][0]["values"][0] = {}
        with self.assertRaisesRegex(EntryError, "supplier_schema_changed"):
            self.source(grid).read(99)

    def test_batch_writes_only_price_value_fields(self):
        source = SheetsPriceSource("test"); source._book = Mock()
        source.write(99, [{"field": "price_1", "row": 3, "after": 250}, {"field": "price_12", "row": 7, "after": None}])
        requests = source._book.batch_update.call_args.args[0]["requests"]
        self.assertEqual(len(requests), 2)
        for r in requests:
            self.assertEqual(list(r), ["updateCells"])
            self.assertEqual(r["updateCells"]["fields"], "userEnteredValue")
        self.assertEqual(requests[0]["updateCells"]["range"]["startColumnIndex"], 5)
        self.assertEqual(requests[1]["updateCells"]["range"]["startColumnIndex"], 7)
        self.assertEqual(requests[1]["updateCells"]["rows"], [{"values": [{}]}])


class EntryRouteTests(unittest.TestCase):
    def setUp(self):
        self.admin = Mock()
        router = APIRouter()
        install_entry_routes(router, self.admin, lambda: None, SimpleNamespace(db_path=Path("unused.db")))
        app = FastAPI(); app.include_router(router)
        self.client = TestClient(app)

    def test_shell_has_no_data_and_strict_csp(self):
        response = self.client.get("/price/entry")
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("1Obi7ZVFJ", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn('<th class="product-col">МОДЕЛЬ</th>', response.text)
        self.assertIn('<th class="memory-col">ПАМЯТЬ</th>', response.text)
        self.assertIn('<th class="color-col">ЦВЕТ</th>', response.text)
        self.assertNotIn("ПАМЯТЬ / ЦВЕТ", response.text)

    def test_public_assets_whitelisted(self):
        entry_js = self.client.get("/price/assets/price-entry.js")
        self.assertEqual(entry_js.status_code, 200)
        self.assertIn('const PRICE_FIELDS = ["price_1", "price_12"]', entry_js.text)
        self.assertIn('PRICE_FIELDS.forEach(field =>', entry_js.text)
        self.assertIn('"memory-cell"', entry_js.text)
        self.assertIn('"color-cell"', entry_js.text)
        self.assertIn('tr.classList.add("model-start")', entry_js.text)
        self.assertNotIn('Object.keys(fields).forEach(field =>', entry_js.text)
        self.assertEqual(self.client.get("/price/assets/price-entry.env").status_code, 404)
        self.assertEqual(self.client.get("/price/models").status_code, 200)
        self.assertEqual(self.client.get("/price/assets/price-models.js").status_code, 200)
        self.assertEqual(self.client.get("/price/assets/price-models.css").status_code, 200)
        self.assertEqual(self.client.get("/price/assets/price-models.env").status_code, 404)

    def test_every_data_route_requires_auth(self):
        self.admin.side_effect = HTTPException(401)
        for route in ["suppliers", "catalog/99", "history"]:
            self.assertEqual(self.client.get("/price/api/v1/entry/" + route).status_code, 401)
        self.assertEqual(self.client.post("/price/api/v1/entry/save/99", json={}).status_code, 401)
        self.assertTrue(self.admin.call_args.kwargs["action"])

    def test_no_google_error_details_exposed(self):
        with patch("price_server.entry_routes.PriceEntryService") as service:
            service.return_value.source.suppliers.side_effect = RuntimeError("SECRET")
            response = self.client.get("/price/api/v1/entry/suppliers")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.text)

    def test_body_limit_and_bad_json(self):
        self.assertEqual(self.client.post("/price/api/v1/entry/save/99", content=b"x" * 65537).status_code, 413)
        self.assertEqual(self.client.post("/price/api/v1/entry/save/99", content=b"not-json").status_code, 400)


if __name__ == "__main__":
    unittest.main()
