"""Isolated price-entry routes; reuse the existing price administrator gate."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .price_entry import EntryError, PriceEntryService

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def install_entry_routes(router, admin, enabled, settings):
    def service():
        return PriceEntryService(settings.db_path)

    def call(function, *args):
        try:
            return function(*args)
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

    @router.get("/price/assets/price-entry.{extension}", include_in_schema=False)
    def entry_asset(extension: str):
        if extension not in {"js", "css"}:
            raise HTTPException(404)
        media = "text/javascript" if extension == "js" else "text/css"
        return Response(STATIC.joinpath("price-entry." + extension).read_bytes(), media_type=media,
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @router.get("/price/api/v1/entry/suppliers")
    def suppliers(request: Request):
        enabled()
        admin(request, action=False)
        return {"suppliers": call(service().source.suppliers)}

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
        result = await run_in_threadpool(call, service().save, sheet_id, body,
                                        request.headers.get("idempotency-key", ""))
        return JSONResponse(result)
