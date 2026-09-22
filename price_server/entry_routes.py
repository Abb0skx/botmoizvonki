"""Isolated price-entry routes; reuse the existing price administrator gate."""
from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .price_entry import EntryError, PriceEntryService
from .entry_catalog import EntryCatalogService
from .entry_refresh import enqueue_price_refresh
from .entry_schedule import publication_preview
from .model_inbox import ModelInbox

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def install_entry_routes(router, admin, enabled, settings):
    def service():
        return PriceEntryService(settings.db_path)

    def catalog_service():
        return EntryCatalogService(settings.db_path)

    def inbox_service():
        return ModelInbox(settings.db_path)

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except EntryError as exc:
            raise HTTPException(exc.status, {"code": exc.code, **exc.details}) from None
        except Exception as exc:
            # Google exception text can include credentials/URLs. Never log it.
            LOG.warning("price_entry_unavailable type=%s", type(exc).__name__)
            raise HTTPException(503, {"code": "price_entry_unavailable"}) from None

    @router.get("/price/entry", include_in_schema=False)
    def entry_page():
        # Public shell only: no prices, supplier names, tokens or credentials.
        # All data and writes pass through the session/CSRF-protected portal.
        response = HTMLResponse(STATIC.joinpath("price-entry.html").read_text())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @router.get("/price/models", include_in_schema=False)
    def models_page():
        response = HTMLResponse(STATIC.joinpath("price-models.html").read_text())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @router.get("/price/categories", include_in_schema=False)
    def categories_page():
        response = HTMLResponse(STATIC.joinpath("price-categories.html").read_text())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @router.get("/price/assets/price-entry.{extension}", include_in_schema=False)
    def entry_asset(extension: str):
        if extension not in {"js", "css"}:
            raise HTTPException(404)
        media = "text/javascript" if extension == "js" else "text/css"
        return Response(STATIC.joinpath("price-entry." + extension).read_bytes(), media_type=media,
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/price/assets/price-models.{extension}", include_in_schema=False)
    def models_asset(extension: str):
        if extension not in {"js", "css"}:
            raise HTTPException(404)
        media = "text/javascript" if extension == "js" else "text/css"
        return Response(STATIC.joinpath("price-models." + extension).read_bytes(), media_type=media,
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/price/assets/price-categories.{extension}", include_in_schema=False)
    def categories_asset(extension: str):
        if extension not in {"js", "css"}:
            raise HTTPException(404)
        media = "text/javascript" if extension == "js" else "text/css"
        return Response(STATIC.joinpath("price-categories." + extension).read_bytes(), media_type=media,
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/price/api/v1/entry/suppliers")
    def suppliers(request: Request):
        enabled()
        admin(request, action=False)
        result = {"suppliers": call(service().source.suppliers),
                  "source": os.getenv("PRICE_ENTRY_SOURCE", "sqlite")}
        try:
            result["publication_schedule"] = publication_preview(
                settings.db_path, getattr(settings, "timezone", "Asia/Tashkent"),
            )
        except Exception as exc:
            # An unavailable calendar must not block price editing or show stale hints.
            LOG.warning("price_entry_schedule_unavailable type=%s", type(exc).__name__)
            result["publication_schedule"] = None
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/price/api/v1/entry/categories")
    def categories(request: Request):
        enabled()
        admin(request, action=False)
        return JSONResponse(call(catalog_service().categories),
                            headers={"Cache-Control": "no-store"})

    @router.post("/price/api/v1/entry/categories")
    async def create_category(request: Request):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 16 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        result = await run_in_threadpool(
            call, catalog_service().create_category, body,
            request.headers.get("idempotency-key", ""),
        )
        return JSONResponse(result)

    @router.post("/price/api/v1/entry/categories/{category_id}")
    async def update_category(request: Request, category_id: int):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 16 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        result = await run_in_threadpool(
            call, catalog_service().update_category, category_id, body,
            request.headers.get("idempotency-key", ""),
        )
        return JSONResponse(result)

    @router.get("/price/api/v1/entry/models")
    def models(request: Request):
        enabled()
        admin(request, action=False)
        return JSONResponse(call(catalog_service().models),
                            headers={"Cache-Control": "no-store"})

    @router.get("/price/api/v1/entry/models/{product_id}")
    def model(request: Request, product_id: int):
        enabled()
        admin(request, action=False)
        return JSONResponse(call(catalog_service().model, product_id),
                            headers={"Cache-Control": "no-store"})

    @router.get("/price/api/v1/entry/model-inbox")
    def model_inbox(request: Request):
        enabled()
        admin(request, action=False)
        result = call(inbox_service().list)
        result["configured"] = bool(getattr(settings, "model_inbox_chat_id", ""))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/price/api/v1/entry/export")
    def export(request: Request):
        enabled()
        key = getattr(settings, "sync_api_key", "")
        if not key or not secrets.compare_digest(request.headers.get("X-Price-Sync-Key", ""), key):
            raise HTTPException(401)
        from .entry_store import SQLitePriceSource
        if os.getenv("PRICE_ENTRY_SOURCE", "sqlite") != "sqlite":
            raise HTTPException(409, {"code": "local_price_source_disabled"})
        return JSONResponse(call(SQLitePriceSource(settings.db_path).export),
                            headers={"Cache-Control": "no-store"})

    @router.get("/price/api/v1/entry/catalog/{sheet_id}")
    def catalog(request: Request, sheet_id: int):
        enabled()
        admin(request, action=False)
        return call(service().source.read, sheet_id)

    @router.get("/price/api/v1/entry/history")
    def history(request: Request):
        enabled()
        admin(request, action=False)
        return call(service().history)

    @router.get("/price/api/v1/entry/cell-history/{sheet_id}/{product_key}/{field}/{before}")
    def cell_history(request: Request, sheet_id: int, product_key: str, field: str, before: int):
        enabled()
        admin(request, action=False)
        result = call(service().cell_history, sheet_id, product_key, field, before)
        result["timezone"] = getattr(settings, "timezone", "Asia/Tashkent")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/price/api/v1/entry/save/{sheet_id}")
    async def save(request: Request, sheet_id: int):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        operation_id = request.headers.get("idempotency-key", "")
        result = await run_in_threadpool(
            call, service().save, sheet_id, body, operation_id
        )
        if result.get("status") == "applied":
            result = dict(result)
            result["refresh"] = await run_in_threadpool(
                enqueue_price_refresh, operation_id
            )
        return JSONResponse(result)

    @router.post("/price/api/v1/entry/products")
    async def create_product(request: Request):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        result = await run_in_threadpool(
            call, catalog_service().create, body,
            request.headers.get("idempotency-key", ""),
        )
        return JSONResponse(result)

    @router.post("/price/api/v1/entry/model-import/preview")
    async def preview_model_import(request: Request):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        if not isinstance(body, dict) or set(body) != {"category_id", "text"}:
            raise HTTPException(400, {"code": "invalid_catalog_request"})
        result = await run_in_threadpool(
            call, catalog_service().preview_import, body["text"], body["category_id"]
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/price/api/v1/entry/model-import/apply")
    async def apply_model_import(request: Request):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        if not isinstance(body, dict) or set(body) != {
                "category_id", "text", "preview_hash"}:
            raise HTTPException(400, {"code": "invalid_catalog_request"})
        result = await run_in_threadpool(
            call, catalog_service().import_models, body["text"], body["category_id"],
            body["preview_hash"], request.headers.get("idempotency-key", "")
        )
        return JSONResponse(result)

    @router.post("/price/api/v1/entry/models/{product_id}")
    async def update_model(request: Request, product_id: int):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        result = await run_in_threadpool(
            call, catalog_service().update, product_id, body,
            request.headers.get("idempotency-key", ""),
        )
        return JSONResponse(result)

    @router.post("/price/api/v1/entry/model-inbox/{draft_id}/{action}")
    async def finish_model_draft(request: Request, draft_id: int, action: str):
        enabled()
        admin(request, action=True)
        if action not in {"applied", "dismissed"}:
            raise HTTPException(404)
        if len(await request.body()) > 1024:
            raise HTTPException(413)
        return JSONResponse(call(inbox_service().finish, draft_id, status=action))
