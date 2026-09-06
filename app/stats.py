import base64
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from app.analytics_service import build_delivery_analytics
from app.database import OrderRepository
from app.monitor_service import build_delivery_monitor
from app.routing_service import RoutingService, enrich_monitor_routes, enrich_stats_routes
from app.stats_service import build_delivery_stats, parse_report_day
from app.utils.couriers import COURIERS_BY_ID
from app.utils.static_map import DeliverySequenceStop, render_delivery_sequence_map


DATABASE_PATH = Path(os.getenv("DELIVERY_DB_PATH", "/app/data/delivery.db"))
DELIVERY_CACHE_PATH = os.getenv("DELIVERY_CACHE_PATH", "").strip()
STATS_USERNAME = (
    os.getenv("DELIVERY_STATS_USERNAME")
    or os.getenv("REVIEWS_ADMIN_USERNAME")
    or "admin"
).strip() or "admin"
STATS_PASSWORD = (
    os.getenv("DELIVERY_STATS_PASSWORD")
    or os.getenv("REVIEWS_ADMIN_PASSWORD")
    or ""
)
MONITORING_BASE_URL = os.getenv("MONITORING_BASE_URL", "").strip().rstrip("/")
MONITORING_DELIVERY_SERVICE_TOKEN = os.getenv(
    "MONITORING_DELIVERY_SERVICE_TOKEN", ""
).strip()
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATS_TEMPLATE_PATH = TEMPLATE_DIR / "delivery_stats.html"
MONITOR_TEMPLATE_PATH = TEMPLATE_DIR / "delivery_monitor.html"
_ROUTING_SERVICE: RoutingService | None = None
_ROUTING_CACHE_PATH: Path | None = None
logger = logging.getLogger(__name__)

app = FastAPI(
    title="TEXNIKACH Delivery Statistics",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _authentication_error(detail: str = "authentication_required") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": 'Basic realm="TEXNIKACH Delivery Stats"'},
    )


def require_stats_auth(request: Request) -> str:
    if not STATS_PASSWORD:
        raise HTTPException(status_code=503, detail="stats_password_not_configured")
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        raise _authentication_error()
    try:
        decoded = base64.b64decode(
            authorization[6:].strip(),
            validate=True,
        ).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        username = password = ""
    if not (
        secrets.compare_digest(username, STATS_USERNAME)
        and secrets.compare_digest(password, STATS_PASSWORD)
    ):
        raise _authentication_error("invalid_credentials")
    return username


def require_internal_monitoring_auth(request: Request) -> None:
    if not MONITORING_DELIVERY_SERVICE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="monitoring_delivery_service_token_not_configured",
        )
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization.startswith(prefix) else ""
    if not supplied or not secrets.compare_digest(
        supplied, MONITORING_DELIVERY_SERVICE_TOKEN
    ):
        raise HTTPException(status_code=403, detail="invalid_service_token")


def _repository() -> OrderRepository:
    if not DATABASE_PATH.is_file():
        raise HTTPException(status_code=503, detail="delivery_database_not_found")
    return OrderRepository(DATABASE_PATH, read_only=True)


def _cache_directory() -> Path:
    return Path(DELIVERY_CACHE_PATH) if DELIVERY_CACHE_PATH else DATABASE_PATH.parent


def _routing_service() -> RoutingService:
    global _ROUTING_SERVICE, _ROUTING_CACHE_PATH
    cache_path = _cache_directory() / "routing-cache.db"
    if _ROUTING_SERVICE is None or _ROUTING_CACHE_PATH != cache_path:
        _ROUTING_SERVICE = RoutingService(cache_path)
        _ROUTING_CACHE_PATH = cache_path
    return _ROUTING_SERVICE


def _report(day: str, courier_id: int | None) -> dict:
    try:
        report_day = parse_report_day(day)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if courier_id is not None and courier_id not in COURIERS_BY_ID:
        raise HTTPException(status_code=422, detail="unknown_courier")
    return build_delivery_stats(
        _repository(),
        report_day,
        courier_id=courier_id,
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if (
        request.url.path.startswith("/delivery/")
        or request.url.path.startswith("/internal/monitoring/")
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "img-src 'self' data: blob: https://tile.openstreetmap.org; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'"
        )
    return response


@app.get("/healthz")
@app.get("/delivery/stats/healthz")
@app.get("/delivery/monitor/healthz")
def health():
    if not STATS_PASSWORD and not MONITORING_DELIVERY_SERVICE_TOKEN:
        return JSONResponse({"ok": False, "reason": "authentication"}, status_code=503)
    if not DATABASE_PATH.is_file():
        return JSONResponse({"ok": False, "reason": "database"}, status_code=503)
    try:
        with sqlite3.connect(
            DATABASE_PATH.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as database:
            database.execute("PRAGMA busy_timeout=5000")
            quick_check = database.execute("PRAGMA quick_check").fetchone()
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            cleanup_columns = {
                row[1]
                for row in database.execute(
                    "PRAGMA table_info(telegram_cleanup_queue)"
                ).fetchall()
            }
        if not quick_check or quick_check[0] != "ok":
            return JSONResponse(
                {"ok": False, "reason": "database_integrity"},
                status_code=503,
            )
        if not {"orders", "order_events", "telegram_cleanup_queue"}.issubset(tables):
            return JSONResponse(
                {"ok": False, "reason": "database_schema"},
                status_code=503,
            )
        if not {"terminal", "next_attempt_at"}.issubset(cleanup_columns):
            return JSONResponse(
                {"ok": False, "reason": "database_migration"},
                status_code=503,
            )
    except sqlite3.Error:
        logger.exception("Delivery statistics health check could not read SQLite")
        return JSONResponse({"ok": False, "reason": "database_read"}, status_code=503)
    return {"ok": True, "database": "ok"}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/delivery/stats", response_class=HTMLResponse)
@app.get("/delivery/stats/", response_class=HTMLResponse, include_in_schema=False)
def statistics_page(request: Request):
    if MONITORING_BASE_URL:
        target = MONITORING_BASE_URL + "/delivery/stats"
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=303)
    require_stats_auth(request)
    return HTMLResponse(STATS_TEMPLATE_PATH.read_text(encoding="utf-8"))


@app.get("/delivery/monitor", response_class=HTMLResponse)
@app.get("/delivery/monitor/", response_class=HTMLResponse, include_in_schema=False)
def monitor_page(request: Request):
    if MONITORING_BASE_URL:
        return RedirectResponse(MONITORING_BASE_URL + "/delivery/live", status_code=303)
    require_stats_auth(request)
    return HTMLResponse(MONITOR_TEMPLATE_PATH.read_text(encoding="utf-8"))


@app.get("/internal/monitoring/v1/delivery/live")
async def internal_monitoring_live(request: Request):
    require_internal_monitoring_auth(request)
    repository = _repository()
    return await run_in_threadpool(build_delivery_monitor, repository)


@app.get("/internal/monitoring/v1/delivery/report")
async def internal_monitoring_report(
    request: Request,
    day: str = Query("today", max_length=20),
    courier_id: int | None = Query(None),
):
    require_internal_monitoring_auth(request)
    return await run_in_threadpool(_report, day, courier_id)


@app.get("/internal/monitoring/v1/delivery/analytics")
def internal_monitoring_analytics(
    request: Request,
    month: str | None = Query(None, max_length=7),
    week: str | None = Query(None, max_length=8),
    courier_id: int | None = Query(None),
):
    require_internal_monitoring_auth(request)
    if courier_id is not None and courier_id not in COURIERS_BY_ID:
        raise HTTPException(status_code=422, detail="unknown_courier")
    try:
        return build_delivery_analytics(
            _repository(), month=month, week=week, courier_id=courier_id
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/internal/monitoring/v1/delivery/map.png")
async def internal_monitoring_map(
    request: Request,
    day: str = Query("today", max_length=20),
    courier_id: int | None = Query(None),
):
    require_internal_monitoring_auth(request)
    return await statistics_map(day=day, courier_id=courier_id, _username="internal")


@app.get("/delivery/monitor/api/state")
async def monitor_state(_username: str = Depends(require_stats_auth)):
    repository = _repository()
    state = await run_in_threadpool(build_delivery_monitor, repository)
    return await enrich_monitor_routes(state, _routing_service())


@app.get("/delivery/stats/api/report")
async def statistics_report(
    day: str = Query("today", max_length=20),
    courier_id: int | None = Query(None),
    _username: str = Depends(require_stats_auth),
):
    report = await run_in_threadpool(_report, day, courier_id)
    return await enrich_stats_routes(report, _routing_service())


@app.get("/delivery/stats/api/analytics")
def statistics_analytics(
    month: str | None = Query(None, max_length=7),
    week: str | None = Query(None, max_length=8),
    courier_id: int | None = Query(None),
    _username: str = Depends(require_stats_auth),
):
    if courier_id is not None and courier_id not in COURIERS_BY_ID:
        raise HTTPException(status_code=422, detail="unknown_courier")
    try:
        return build_delivery_analytics(
            _repository(),
            month=month,
            week=week,
            courier_id=courier_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/delivery/stats/map.png")
async def statistics_map(
    day: str = Query("today", max_length=20),
    courier_id: int | None = Query(None),
    _username: str = Depends(require_stats_auth),
):
    report = await run_in_threadpool(_report, day, courier_id)
    report = await enrich_stats_routes(report, _routing_service())
    stops = [
        DeliverySequenceStop(
            sequence=stop["sequence"],
            order_number=stop["order_number"],
            latitude=stop["latitude"],
            longitude=stop["longitude"],
            courier_id=stop["courier_id"],
            courier_name=stop["courier_name"],
            color=stop["color"],
            state=stop["state"],
        )
        for stop in report["stops"]
    ]
    try:
        image = await render_delivery_sequence_map(
            stops,
            report_day_label=report["day_label"],
            cache_dir=_cache_directory() / "map-tiles",
            road_routes=report["routes"],
        )
    except Exception as error:
        logger.exception(
            "Could not render delivery statistics map for day=%s courier_id=%s",
            day,
            courier_id,
        )
        raise HTTPException(status_code=503, detail="map_temporarily_unavailable") from error
    filename = f"texnikach-delivery-{report['day']}.png"
    return StreamingResponse(
        image,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
