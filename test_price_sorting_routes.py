import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from price_server.price_entry import EntryError
from price_server.sorting_routes import install_sorting_routes


class SortingRouteTests(unittest.TestCase):
    def setUp(self):
        self.admin = Mock()
        router = APIRouter()
        self.settings = SimpleNamespace(db_path=Path("unused.db"), sync_api_key="test-sync-key")
        install_sorting_routes(router, self.admin, lambda: None, self.settings)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_shell_is_public_but_has_no_catalogue_or_inline_scripts(self):
        result = self.client.get("/price/sorting")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertIn("script-src 'self'", result.headers["content-security-policy"])
        self.assertNotIn("<script>", result.text)
        self.assertNotIn(self.settings.sync_api_key, result.text)
        for extension in ("js", "css"):
            self.assertEqual(self.client.get("/price/assets/price-sorting." + extension).status_code, 200)
        self.assertEqual(self.client.get("/price/assets/price-sorting.env").status_code, 404)

    def test_data_and_save_require_existing_admin_gate(self):
        self.admin.side_effect = HTTPException(401)
        with patch("price_server.sorting_routes.SortingService") as service:
            self.assertEqual(self.client.get("/price/api/v1/entry/sorting").status_code, 401)
            self.assertEqual(self.client.post("/price/api/v1/entry/sorting", json={}).status_code, 401)
            service.assert_not_called()
        self.assertTrue(self.admin.call_args.kwargs["action"])

    def test_worker_export_requires_sync_key_not_public_admin_proxy(self):
        url = "/price/api/v1/entry/sorting/export"
        with patch("price_server.sorting_routes.SortingService") as service:
            for headers in ({}, {"X-Price-Sync-Key": "wrong"}):
                self.assertEqual(self.client.get(url, headers=headers).status_code, 401)
            service.assert_not_called()
            service.return_value.export.return_value = {"schema_version": 1, "source": "sqlite"}
            response = self.client.get(url, headers={"X-Price-Sync-Key": "test-sync-key"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.admin.assert_not_called()

    def test_applied_save_queues_refresh_without_telegram(self):
        operation = str(uuid.uuid4())
        body = {"revision": 1, "groups": [{"group_id": "models:1", "item_ids": [2, 1]}]}
        with patch("price_server.sorting_routes.SortingService") as service, patch(
            "price_server.sorting_routes.enqueue_price_refresh", return_value={"status": "queued"}
        ) as refresh:
            service.return_value.save.return_value = {"status": "applied", "revision": 2}
            response = self.client.post("/price/api/v1/entry/sorting", json=body,
                                        headers={"Idempotency-Key": operation})
            self.assertEqual(response.status_code, 200)
            service.return_value.save.assert_called_once_with(body, operation)
            refresh.assert_called_once_with(operation)
            self.assertEqual(response.json()["refresh"]["status"], "queued")

    def test_validation_and_conflicts_never_queue_refresh(self):
        with patch("price_server.sorting_routes.SortingService") as service, patch(
            "price_server.sorting_routes.enqueue_price_refresh"
        ) as refresh:
            service.return_value.save.side_effect = EntryError("sorting_revision_conflict", 409)
            response = self.client.post("/price/api/v1/entry/sorting", json={})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "sorting_revision_conflict")
            refresh.assert_not_called()
            service.reset_mock()
            self.assertEqual(self.client.post("/price/api/v1/entry/sorting", content=b"{" ).status_code, 400)
            self.assertEqual(self.client.post("/price/api/v1/entry/sorting", content=b" " * (256*1024+1)).status_code, 413)
            service.assert_not_called()

    def test_internal_error_is_sanitized(self):
        with patch("price_server.sorting_routes.SortingService") as service:
            service.return_value.state.side_effect = RuntimeError("SECRET")
            response = self.client.get("/price/api/v1/entry/sorting")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.text)


if __name__ == "__main__":
    unittest.main()
