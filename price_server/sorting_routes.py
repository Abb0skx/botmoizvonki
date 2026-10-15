"""Authenticated sorting controls and a signed, read-only worker export."""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .entry_refresh import enqueue_price_refresh
from .price_entry import EntryError
from .sorting import SortingService

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_BODY = 256 * 1024


def install_sorting_routes(router, admin, enabled, settings):
    def call(method, *args):
        try:
            return getattr(SortingService(settings.db_path), method)(*args)
        except EntryError as exc:
            raise HTTPException(exc.status, {"code": exc.code, **exc.details}) from None
        except Exception as exc:
            LOG.warning("price_sorting_unavailable type=%s", type(exc).__name__)
            raise HTTPException(503, {"code": "sorting_unavailable"}) from None

    @router.get("/price/sorting", include_in_schema=False)
    def sorting_page():
        # Shell is public; catalogue data and edits require the existing gate.
        return HTMLResponse(STATIC.joinpath("price-sorting.html").read_text(), headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
        })

    @router.get("/price/assets/price-sorting.{extension}", include_in_schema=False)
    def sorting_asset(extension: str):
        if extension not in {"js", "css"}:
            raise HTTPException(404)
        return Response(STATIC.joinpath("price-sorting." + extension).read_bytes(),
                        media_type="text/javascript" if extension == "js" else "text/css",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/price/api/v1/entry/sorting")
    def sorting_state(request: Request):
        enabled()
        admin(request, action=False)
        return JSONResponse(call("state"), headers={"Cache-Control": "no-store"})

    @router.post("/price/api/v1/entry/sorting")
    async def sorting_save(request: Request):
        enabled()
        admin(request, action=True)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, {"code": "invalid_json"}) from None
        operation_id = request.headers.get("idempotency-key", "")
        result = await run_in_threadpool(call, "save", body, operation_id)
        if result.get("status") == "applied":
            result = dict(result)
            result["refresh"] = await run_in_threadpool(enqueue_price_refresh, operation_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/price/api/v1/entry/sorting/export")
    def sorting_export(request: Request):
        enabled()
        key = getattr(settings, "sync_api_key", "")
        if not key or not secrets.compare_digest(request.headers.get("X-Price-Sync-Key", ""), key):
            raise HTTPException(401)
        return JSONResponse(call("export"), headers={"Cache-Control": "no-store"})
