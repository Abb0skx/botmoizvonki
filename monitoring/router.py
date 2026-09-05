from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import httpx

from .adapters import calls as calls_adapter
from .adapters import prices as prices_adapter
from .adapters import reviews as reviews_adapter
from .adapters.delivery import DeliveryAdapter
from .adapters.go_site import GoSiteAdapter
from .auth import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    ManagerPrincipal,
    MonitoringAuth,
    _safe_next,
    monitoring_security_headers,
)
from .config import MonitoringSettings
from .database import MonitoringStore
from .manager_registry import public_registry


LOG = logging.getLogger("monitoring")
TASHKENT = ZoneInfo("Asia/Tashkent")
ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"

_PRICE_ADMIN_GET_PATHS = frozenset({"jobs", "sections"})
_PRICE_ADMIN_POST_PATHS = (
    re.compile(
        r"sections/[a-z0-9][a-z0-9-]{0,127}/"
        r"(?:send-now|edit-current|schedule)"
    ),
    re.compile(r"jobs/[1-9][0-9]*/(?:cancel|reconcile)"),
    re.compile(r"quick-link-rotations/publish-now"),
    re.compile(
        r"quick-link-rotations/[1-9][0-9]*/(?:reconcile|retry)"
    ),
    re.compile(r"posts/update-all"),
)

router = APIRouter(tags=["manager-monitoring"])
settings = MonitoringSettings.load()
_store: MonitoringStore | None = None
_auth: MonitoringAuth | None = None


def get_store() -> MonitoringStore:
    global _store
    if _store is None:
        _store = MonitoringStore(settings.session_db_path)
        _store.initialize()
    return _store


def get_auth() -> MonitoringAuth:
    global _auth
    if _auth is None:
        _auth = MonitoringAuth(settings, get_store())
    return _auth


def _principal(request: Request, *, admin: bool = False) -> ManagerPrincipal:
    return get_auth().principal(request, admin=admin)


def _json(payload: Any, status_code: int = 200) -> JSONResponse:
    return monitoring_security_headers(JSONResponse(payload, status_code=status_code))


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return monitoring_security_headers(HTMLResponse(content, status_code=status_code))


def _login_redirect(request: Request) -> Response:
    next_path = request.url.path
    if request.url.query:
        next_path += "?" + request.url.query
    return monitoring_security_headers(RedirectResponse(
        "/monitoring/login?next=" + quote(next_path, safe=""),
        status_code=303,
    ))


def _inject_price_admin_script(content: bytes) -> bytes:
    try:
        document = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=502, detail="price_catalog_invalid_encoding"
        ) from exc
    script = (
        '<script src="/monitoring/assets/price-admin-bridge.js"></script>'
        '<script src="/price/assets/admin.js" defer></script>'
    )
    legacy = '<script src="/price/assets/admin.js" defer></script>'
    if script in document:
        return content
    if legacy in document:
        return document.replace(legacy, script, 1).encode("utf-8")
    body_index = document.casefold().rfind("</body>")
    if body_index >= 0:
        document = document[:body_index] + script + document[body_index:]
    else:
        document += script
    return document.encode("utf-8")


def _catalog_html(content: bytes) -> Response:
    response = Response(content, media_type="text/html")
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline' https:; img-src data: https:; "
        "font-src data: https:; connect-src 'none'; "
        "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
    )
    return response


def _price_manage_html(content: bytes) -> Response:
    response = Response(content, media_type="text/html")
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'unsafe-inline' https:; img-src data: https:; "
        "font-src data: https:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    return response


def _price_admin_target(method: str, path: str) -> str:
    allowed = path in _PRICE_ADMIN_GET_PATHS if method == "GET" else any(
        pattern.fullmatch(path) for pattern in _PRICE_ADMIN_POST_PATHS
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="price_action_not_found")
    return "/price/api/v1/" + path


def _meta(source: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "fetched_at": datetime.now(TASHKENT).isoformat(timespec="seconds"),
        **extra,
    }


def _error_code(error: BaseException) -> str:
    text = str(error).strip()
    if text and len(text) <= 100 and all(
        character.isalnum() or character in "_-" for character in text
    ):
        return text
    return type(error).__name__.casefold()


def _source_error(source: str, error: BaseException) -> dict[str, Any]:
    LOG.warning("monitoring_source_unavailable source=%s type=%s", source, type(error).__name__)
    return {"data": None, "meta": _meta(source, "unavailable", error_code=_error_code(error))}


def _delivery_params(request: Request) -> dict[str, str]:
    params = dict(request.query_params)
    legacy_courier = params.pop("delivery_courier_id", "")
    if legacy_courier and not params.get("courier_id"):
        params["courier_id"] = legacy_courier
    if not params.get("day") and params.get("period") in {"today", "yesterday"}:
        params["day"] = params["period"]
    return params


def _period(
    period: str,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, str | None]:
    if period not in {"today", "yesterday", "7d", "30d", "custom"}:
        raise HTTPException(status_code=422, detail="invalid_period")
    if period == "custom" and (not date_from or not date_to):
        raise HTTPException(status_code=422, detail="custom_period_requires_dates")
    return {"period": period, "date_from": date_from, "date_to": date_to}


@router.get("/monitoring/login", response_class=HTMLResponse)
def login_page(request: Request):
    login_url = "/monitoring/auth/start?next=" + quote(
        _safe_next(request.query_params.get("next")), safe=""
    )
    try:
        _principal(request)
    except HTTPException as exc:
        if exc.status_code == 503:
            return _html(
                TEMPLATES.joinpath("login.html").read_text(encoding="utf-8")
                .replace("__STATE__", "Портал пока не настроен")
                .replace("__LOGIN_HIDDEN__", "hidden")
                .replace("__LOGIN_URL__", login_url),
                status_code=503,
            )
    else:
        return monitoring_security_headers(RedirectResponse("/monitoring", status_code=303))
    content = TEMPLATES.joinpath("login.html").read_text(encoding="utf-8")
    return _html(
        content.replace("__STATE__", "Доступ только для менеджеров TEXNIKACH")
        .replace("__LOGIN_HIDDEN__", "")
        .replace("__LOGIN_URL__", login_url)
    )


@router.get("/monitoring/auth/start")
def auth_start(next: str | None = Query(None, max_length=1000)):
    return monitoring_security_headers(get_auth().begin_login(next))


@router.get("/monitoring/auth/callback")
async def auth_callback(
    request: Request,
    code: str = Query(..., min_length=1, max_length=4096),
    state: str = Query(..., min_length=16, max_length=512),
):
    raw_session, csrf_token, next_path = await get_auth().finish_login(
        request, code=code, state=state
    )
    response = RedirectResponse(next_path, status_code=303)
    get_auth().attach_session_cookies(
        response, raw_session=raw_session, csrf_token=csrf_token
    )
    response.delete_cookie(
        OAUTH_COOKIE, path="/monitoring/auth", secure=True, samesite="lax"
    )
    return monitoring_security_headers(response)


@router.post("/monitoring/auth/logout")
def auth_logout(request: Request):
    principal = _principal(request)
    get_auth().verify_csrf(request, principal)
    get_store().revoke_session(
        request.cookies.get(SESSION_COOKIE, ""), reason="logout"
    )
    get_store().audit(
        "logout", result="success", telegram_user_id=principal.telegram_user_id,
        role=principal.role, route=request.url.path,
    )
    response = RedirectResponse("/monitoring/login", status_code=303)
    get_auth().clear_cookies(response)
    return monitoring_security_headers(response)


@router.get("/monitoring/assets/{filename}")
def monitoring_asset(filename: str):
    if filename not in {
        "monitoring.css", "monitoring.js", "price-admin-bridge.js",
    }:
        raise HTTPException(status_code=404, detail="asset_not_found")
    path = STATIC / filename
    media_type = "text/css" if filename.endswith(".css") else "application/javascript"
    response = Response(path.read_text(encoding="utf-8"), media_type=media_type)
    return monitoring_security_headers(response)


@router.get("/monitoring", response_class=HTMLResponse)
@router.get("/monitoring/calls", response_class=HTMLResponse)
@router.get("/monitoring/site", response_class=HTMLResponse)
@router.get("/monitoring/reviews", response_class=HTMLResponse)
@router.get("/monitoring/delivery/live", response_class=HTMLResponse)
@router.get("/monitoring/delivery/stats", response_class=HTMLResponse)
@router.get("/monitoring/prices", response_class=HTMLResponse)
def monitoring_page(request: Request):
    try:
        principal = _principal(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return _login_redirect(request)
        raise
    if (
        request.url.path == "/monitoring/prices"
        and settings.can_manage_prices(principal.telegram_user_id)
    ):
        return monitoring_security_headers(RedirectResponse(
            "/monitoring/prices/manage", status_code=303
        ))
    section = request.url.path.removeprefix("/monitoring/").strip("/")
    if request.url.path == "/monitoring":
        section = ""
    template = TEMPLATES.joinpath("monitoring.html").read_text(encoding="utf-8")
    bootstrap = json.dumps(
        {
            "section": section or "overview",
            "user": {
                "id": principal.telegram_user_id,
                "name": principal.display_name,
                "role": principal.role,
                "can_manage_prices": settings.can_manage_prices(
                    principal.telegram_user_id
                ),
            },
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    return _html(template.replace("__BOOTSTRAP__", bootstrap))


@router.get("/monitoring/api/me")
def api_me(request: Request):
    principal = _principal(request)
    return _json({
        "telegram_user_id": principal.telegram_user_id,
        "display_name": principal.display_name,
        "role": principal.role,
        "expires_at": principal.session.absolute_expires_at.isoformat(),
    })


@router.get("/monitoring/api/calls")
async def api_calls(
    request: Request,
    period: str = Query("today"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    _principal(request)
    filters = _period(period, date_from, date_to)
    data = await run_in_threadpool(calls_adapter.calls_summary, **filters)
    return _json({"data": data, "meta": _meta("calls")})


@router.get("/monitoring/api/calls/managers")
async def api_call_managers(
    request: Request,
    period: str = Query("today"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    _principal(request)
    filters = _period(period, date_from, date_to)
    data = await run_in_threadpool(calls_adapter.calls_managers, **filters)
    return _json({"data": data, "meta": _meta("calls")})


@router.get("/monitoring/api/calls/recent")
async def api_calls_recent(
    request: Request,
    period: str = Query("today"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
):
    _principal(request)
    filters = _period(period, date_from, date_to)
    data = await run_in_threadpool(calls_adapter.calls_recent, limit=limit, **filters)
    return _json({"data": data, "meta": _meta("calls")})


@router.get("/monitoring/api/calls/{call_id}")
async def api_call_detail(request: Request, call_id: int):
    principal = _principal(request)
    data = await run_in_threadpool(
        calls_adapter.call_detail,
        call_id,
        include_technical=principal.role == "admin",
    )
    return _json({"data": data, "meta": _meta("calls")})


@router.get("/monitoring/api/reviews")
async def api_reviews(request: Request):
    principal = _principal(request)
    params = dict(request.query_params)
    data = await run_in_threadpool(
        reviews_adapter.reviews_dashboard,
        params,
        include_technical=principal.role == "admin",
    )
    return _json({"data": data, "meta": _meta("reviews")})


@router.get("/monitoring/api/reviews/{review_id}")
async def api_review_detail(request: Request, review_id: int):
    principal = _principal(request)
    data = await run_in_threadpool(
        reviews_adapter.review_detail,
        review_id,
        include_technical=principal.role == "admin",
    )
    if data is None:
        raise HTTPException(status_code=404, detail="review_not_found")
    return _json({"data": data, "meta": _meta("reviews")})


@router.get("/monitoring/api/prices")
async def api_prices(request: Request):
    _principal(request)
    try:
        data = await prices_adapter.PriceAdapter(settings).summary()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return _json(_source_error("prices", exc), status_code=503)
    return _json({"data": data, "meta": _meta("prices")})


@router.get("/monitoring/prices/catalog")
async def monitoring_price_catalog(request: Request):
    _principal(request)
    try:
        content, media_type = await prices_adapter.PriceAdapter(settings).catalog()
    except (httpx.HTTPError, RuntimeError, ValueError):
        raise HTTPException(
            status_code=503, detail="price_catalog_unavailable"
        ) from None
    if media_type.split(";", 1)[0].strip().casefold() != "text/html":
        raise HTTPException(
            status_code=502, detail="price_catalog_invalid_media_type"
        )
    return _catalog_html(content)


@router.get("/monitoring/prices/manage")
async def monitoring_price_manage(request: Request):
    try:
        principal = _principal(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return _login_redirect(request)
        raise
    if not settings.can_manage_prices(principal.telegram_user_id):
        raise HTTPException(status_code=403, detail="price_editor_required")
    try:
        content, media_type = await prices_adapter.PriceAdapter(
            settings
        ).catalog()
    except (httpx.HTTPError, RuntimeError, ValueError):
        raise HTTPException(
            status_code=503, detail="price_catalog_unavailable"
        ) from None
    if media_type.split(";", 1)[0].strip().casefold() != "text/html":
        raise HTTPException(
            status_code=502, detail="price_catalog_invalid_media_type"
        )
    return _price_manage_html(_inject_price_admin_script(content))


@router.api_route(
    "/monitoring/api/prices/admin/{path:path}",
    methods=["GET", "POST"],
)
async def api_price_admin(request: Request, path: str):
    principal = _principal(request)
    if not settings.can_manage_prices(principal.telegram_user_id):
        raise HTTPException(status_code=403, detail="price_editor_required")
    method = request.method.upper()
    target = _price_admin_target(method, path)
    body = b""
    idempotency_key = request.headers.get("idempotency-key", "")[:128]
    if method == "POST":
        get_auth().verify_csrf(request, principal)
        content_length = request.headers.get("content-length", "")
        try:
            if content_length and int(content_length) > 64 * 1024:
                raise HTTPException(status_code=413, detail="request_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_content_length")
        body = await request.body()
        if len(body) > 64 * 1024:
            raise HTTPException(status_code=413, detail="request_too_large")
    try:
        status_code, content, media_type = await prices_adapter.PriceAdapter(
            settings
        ).admin_request(
            method,
            target,
            body=body,
            idempotency_key=idempotency_key,
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        if method == "POST":
            get_store().audit(
                "price_admin_proxy",
                result="upstream_unavailable",
                telegram_user_id=principal.telegram_user_id,
                role=principal.role,
                route=target,
                correlation_id=idempotency_key,
            )
        return _json(_source_error("prices", exc), status_code=503)
    if method == "POST":
        get_store().audit(
            "price_admin_proxy",
            result=f"upstream_{status_code}",
            telegram_user_id=principal.telegram_user_id,
            role=principal.role,
            route=target,
            correlation_id=idempotency_key,
        )
    if media_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise HTTPException(
            status_code=502, detail="price_admin_invalid_media_type"
        )
    return monitoring_security_headers(Response(
        content,
        status_code=status_code,
        media_type="application/json",
    ))


@router.get("/monitoring/api/managers")
def api_managers(request: Request):
    _principal(request)
    try:
        registry = public_registry()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return _json({
        "data": {
            "managers": registry,
            "mapping_status": "configured" if registry else "not_configured",
            "note": (
                None if registry else
                "Источники не объединяются по отображаемому имени"
            ),
        },
        "meta": _meta("manager_registry"),
    })


@router.post("/monitoring/api/admin/sessions/{telegram_user_id}/revoke")
def api_revoke_user_sessions(request: Request, telegram_user_id: int):
    principal = _principal(request, admin=True)
    get_auth().verify_csrf(request, principal)
    count = get_store().revoke_user_sessions(
        telegram_user_id, reason=f"revoked_by:{principal.telegram_user_id}"
    )
    get_store().audit(
        "revoke_user_sessions",
        result=f"revoked:{count}",
        telegram_user_id=telegram_user_id,
        role="admin",
        route=request.url.path,
    )
    return _json({"revoked": count, "telegram_user_id": telegram_user_id})


@router.get("/monitoring/api/delivery/live")
async def api_delivery_live(request: Request):
    _principal(request)
    try:
        data = await DeliveryAdapter(settings).get(
            "/internal/monitoring/v1/delivery/live"
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return _json(_source_error("delivery", exc), status_code=503)
    return _json({"data": data, "meta": _meta("delivery")})


@router.get("/monitoring/api/delivery/report")
async def api_delivery_report(request: Request):
    _principal(request)
    params = _delivery_params(request)
    try:
        data = await DeliveryAdapter(settings).get(
            "/internal/monitoring/v1/delivery/report", params=params
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return _json(_source_error("delivery", exc), status_code=503)
    return _json({"data": data, "meta": _meta("delivery")})


@router.get("/monitoring/api/delivery/analytics")
async def api_delivery_analytics(request: Request):
    _principal(request)
    params = _delivery_params(request)
    try:
        data = await DeliveryAdapter(settings).get(
            "/internal/monitoring/v1/delivery/analytics", params=params
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return _json(_source_error("delivery", exc), status_code=503)
    return _json({"data": data, "meta": _meta("delivery")})


@router.get("/monitoring/api/delivery/map.png")
async def api_delivery_map(request: Request):
    _principal(request)
    params = _delivery_params(request)
    try:
        content, media_type = await DeliveryAdapter(settings).get_bytes(
            "/internal/monitoring/v1/delivery/map.png",
            params=params,
            accept="image/png",
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        LOG.warning(
            "monitoring_source_unavailable source=delivery_map type=%s",
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="delivery_map_unavailable") from None
    if media_type.split(";", 1)[0].strip().casefold() != "image/png":
        raise HTTPException(status_code=502, detail="delivery_map_invalid_media_type")
    return monitoring_security_headers(Response(content, media_type="image/png"))


@router.get("/monitoring/api/site")
async def api_go_site(
    request: Request,
    period: str = Query("today"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    _principal(request)
    params = _period(period, date_from, date_to)
    try:
        data = await GoSiteAdapter(settings).stats(
            {key: value for key, value in params.items() if value is not None}
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return _json(_source_error("go_site", exc), status_code=503)
    return _json({"data": data, "meta": _meta("go_site")})


@router.get("/monitoring/api/overview")
async def api_overview(
    request: Request,
    period: str = Query("today"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    principal = _principal(request)
    filters = _period(period, date_from, date_to)

    async def calls_source():
        return await run_in_threadpool(calls_adapter.calls_summary, **filters)

    async def reviews_source():
        return await run_in_threadpool(
            reviews_adapter.reviews_dashboard,
            {key: value for key, value in filters.items() if value is not None},
            include_technical=principal.role == "admin",
        )

    async def prices_source():
        return await prices_adapter.PriceAdapter(settings).summary()

    async def delivery_source():
        return await DeliveryAdapter(settings).get(
            "/internal/monitoring/v1/delivery/live"
        )

    async def go_source():
        return await GoSiteAdapter(settings).stats(
            {key: value for key, value in filters.items() if value is not None}
        )

    names = ("calls", "reviews", "delivery", "prices", "go_site")
    results = await asyncio.gather(
        calls_source(), reviews_source(), delivery_source(),
        prices_source(), go_source(), return_exceptions=True,
    )
    sources: dict[str, Any] = {}
    for name, result in zip(names, results):
        sources[name] = (
            _source_error(name, result)
            if isinstance(result, BaseException)
            else {"data": result, "meta": _meta(name)}
        )
    return _json({
        "period": filters,
        "sources": sources,
        "generated_at": datetime.now(TASHKENT).isoformat(timespec="seconds"),
    })


__all__ = ["router", "settings"]
