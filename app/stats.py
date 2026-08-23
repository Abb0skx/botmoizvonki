import base64
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from app.analytics_service import build_delivery_analytics
from app.database import OrderRepository
from app.monitor_service import build_delivery_monitor
from app.routing_service import RoutingService, enrich_monitor_routes, enrich_stats_routes
from app.stats_service import build_delivery_stats, parse_report_day
from app.utils.couriers import COURIERS_BY_ID
from app.utils.static_map import DeliverySequenceStop, render_delivery_sequence_map


DATABASE_PATH = Path(os.getenv("DELIVERY_DB_PATH", "/app/data/delivery.db"))
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
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATS_TEMPLATE_PATH = TEMPLATE_DIR / "delivery_stats.html"
MONITOR_TEMPLATE_PATH = TEMPLATE_DIR / "delivery_monitor.html"
_ROUTING_SERVICE: RoutingService | None = None
_ROUTING_CACHE_PATH: Path | None = None

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


def _repository() -> OrderRepository:
    if not DATABASE_PATH.is_file():
        raise HTTPException(status_code=503, detail="delivery_database_not_found")
    return OrderRepository(DATABASE_PATH)


def _routing_service() -> RoutingService:
    global _ROUTING_SERVICE, _ROUTING_CACHE_PATH
    cache_path = DATABASE_PATH.parent / "routing-cache.db"
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
    if request.url.path.startswith("/delivery/"):
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
    if not STATS_PASSWORD:
        return JSONResponse({"ok": False, "reason": "password"}, status_code=503)
    if not DATABASE_PATH.is_file():
        return JSONResponse({"ok": False, "reason": "database"}, status_code=503)
    return {"ok": True}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/delivery/stats", response_class=HTMLResponse)
@app.get("/delivery/stats/", response_class=HTMLResponse, include_in_schema=False)
def statistics_page(_username: str = Depends(require_stats_auth)):
    return HTMLResponse(STATS_TEMPLATE_PATH.read_text(encoding="utf-8"))


@app.get("/delivery/monitor", response_class=HTMLResponse)
@app.get("/delivery/monitor/", response_class=HTMLResponse, include_in_schema=False)
def monitor_page(_username: str = Depends(require_stats_auth)):
    return HTMLResponse(MONITOR_TEMPLATE_PATH.read_text(encoding="utf-8"))


@app.get("/delivery/monitor/api/state")
async def monitor_state(_username: str = Depends(require_stats_auth)):
    state = build_delivery_monitor(_repository())
    return await enrich_monitor_routes(state, _routing_service())


@app.get("/delivery/stats/api/report")
async def statistics_report(
    day: str = Query("today", max_length=20),
    courier_id: int | None = Query(None),
    _username: str = Depends(require_stats_auth),
):
    report = _report(day, courier_id)
    return await enrich_stats_routes(report, _routing_service())


@app.get("/delivery/stats/api/analytics")
def statistics_analytics(
    month: str | None = Query(None, max_length=7),
    week: str | None = Query(None, max_length=8),
    _username: str = Depends(require_stats_auth),
):
    try:
        return build_delivery_analytics(
            _repository(),
            month=month,
            week=week,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/delivery/stats/map.png")
async def statistics_map(
    day: str = Query("today", max_length=20),
    courier_id: int | None = Query(None),
    _username: str = Depends(require_stats_auth),
):
    report = await enrich_stats_routes(
        _report(day, courier_id),
        _routing_service(),
    )
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
            cache_dir=DATABASE_PATH.parent / "map-tiles",
            road_routes=report["routes"],
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="map_temporarily_unavailable") from error
    filename = f"texnikach-delivery-{report['day']}.png"
    return StreamingResponse(
        image,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
