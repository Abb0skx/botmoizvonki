import base64
import json
import secrets
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from .catalog import CATEGORIES, REASONS
from .config import (
    REVIEWS_ADMIN_PASSWORD,
    REVIEWS_ADMIN_USERNAME,
    REVIEWS_COMPLAINT_PHONE,
    REVIEWS_COMPLAINT_TELEGRAM,
    REVIEWS_INSTAGRAM_URL,
    REVIEWS_DB_PATH,
    REVIEWS_SITE_URL,
    REVIEWS_TELEGRAM_URL,
    REVIEWS_TELEGRAM_BOT_TOKEN,
    REVIEWS_TELEGRAM_CHAT_ID,
)
from .analytics import (
    AnalyticsFilterError,
    get_review_detail,
    get_reviews_dashboard,
)
from .database import list_active_managers
from .service import (
    ReviewRateLimitError,
    ReviewValidationError,
    create_review,
    send_review_notification,
)


router = APIRouter(tags=["customer-reviews"])
TEMPLATE_PATH = Path(__file__).parent / "templates" / "rating.html"
ADMIN_TEMPLATE_PATH = Path(__file__).parent / "templates" / "admin_reviews.html"
METADATA_HEADERS = (
    "user-agent", "accept-language", "referer", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-ch-ua-model",
    "sec-ch-ua-platform-version", "sec-ch-ua-arch", "sec-ch-ua-bitness",
    "sec-ch-ua-form-factors", "sec-ch-device-memory", "sec-ch-dpr",
    "sec-ch-viewport-width", "sec-ch-viewport-height", "save-data",
    "downlink", "ect", "rtt",
)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    connecting = request.headers.get("cf-connecting-ip", "")
    if connecting:
        return connecting[:100]
    return request.client.host if request.client else None


def _request_headers(request: Request) -> dict:
    return {
        name: request.headers[name][:1000]
        for name in METADATA_HEADERS
        if request.headers.get(name)
    }


def _require_admin(request: Request) -> None:
    if not REVIEWS_ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="reviews_admin_password_not_configured",
        )
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="authentication_required",
            headers={"WWW-Authenticate": 'Basic realm="Texnikach Reviews"'},
        )
    try:
        decoded = base64.b64decode(
            authorization[6:].strip(), validate=True
        ).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        username = password = ""
    if not (
        secrets.compare_digest(username, REVIEWS_ADMIN_USERNAME)
        and secrets.compare_digest(password, REVIEWS_ADMIN_PASSWORD)
    ):
        raise HTTPException(
            status_code=401,
            detail="invalid_credentials",
            headers={"WWW-Authenticate": 'Basic realm="Texnikach Reviews"'},
        )


def _page_response(request: Request) -> HTMLResponse:
    managers = list_active_managers()
    source = request.query_params.get("source", "website")
    csrf_token = secrets.token_urlsafe(32)
    page_config = {
        "categories": CATEGORIES,
        "reasons": {
            category: {
                code: {"ru": labels[0], "uz": labels[1]}
                for code, labels in items.items()
            }
            for category, items in REASONS.items()
        },
        "managers": managers,
        "source": source,
        "csrfToken": csrf_token,
        "complaintPhone": REVIEWS_COMPLAINT_PHONE,
        "complaintTelegram": REVIEWS_COMPLAINT_TELEGRAM,
        "siteUrl": REVIEWS_SITE_URL,
        "instagramUrl": REVIEWS_INSTAGRAM_URL,
        "telegramUrl": REVIEWS_TELEGRAM_URL,
    }
    config_json = json.dumps(page_config, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__REVIEWS_CONFIG__", config_json
    )
    response = HTMLResponse(html)
    response.set_cookie(
        "reviews_csrf",
        csrf_token,
        max_age=3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@router.get("/rating", response_class=HTMLResponse)
def rating_page(request: Request):
    return _page_response(request)


@router.get("/review", response_class=HTMLResponse)
def review_page_alias(request: Request):
    return _page_response(request)


@router.post("/api/reviews", status_code=201)
async def submit_review(request: Request, background_tasks: BackgroundTasks):
    cookie_token = request.cookies.get("reviews_csrf", "")
    header_token = request.headers.get("x-csrf-token", "")
    if not cookie_token or not header_token or not secrets.compare_digest(
        cookie_token, header_token
    ):
        raise HTTPException(status_code=403, detail="csrf_failed")
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None

    try:
        review = create_review(
            payload,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            db_path=REVIEWS_DB_PATH,
            accept_language=request.headers.get("accept-language"),
            referer=request.headers.get("referer"),
            request_headers=_request_headers(request),
        )
    except ReviewRateLimitError:
        raise HTTPException(status_code=429, detail="rate_limit") from None
    except ReviewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    if review["should_notify"]:
        background_tasks.add_task(
            send_review_notification, review, REVIEWS_DB_PATH
        )
    return {"ok": True, "review_id": review["id"]}


@router.get("/reviews/status")
def reviews_status():
    return {
        "ok": True,
        "module": "customer-reviews",
        "telegram_configured": bool(
            REVIEWS_TELEGRAM_BOT_TOKEN and REVIEWS_TELEGRAM_CHAT_ID
        ),
        "admin_configured": bool(REVIEWS_ADMIN_PASSWORD),
    }


@router.get("/call", response_class=HTMLResponse)
def open_phone_call():
    html = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Позвонить клиенту</title>
</head>
<body style="font-family:system-ui;text-align:center;padding:40px 16px">
  <p id="status">Открываем приложение телефона…</p>
  <p><a id="call" href="#" style="font-size:22px">📞 Позвонить</a></p>
  <script>
    const digits = window.location.hash.replace(/\D/g, "");
    const link = document.getElementById("call");
    if (digits.length === 12 && digits.startsWith("998")) {
      const target = `tel:+${digits}`;
      link.href = target;
      link.textContent = `📞 Позвонить +${digits}`;
      window.setTimeout(() => window.location.href = target, 50);
    } else {
      document.getElementById("status").textContent = "Номер телефона не найден";
      link.hidden = true;
    }
  </script>
</body>
</html>"""
    response = HTMLResponse(html)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/admin/reviews", response_class=HTMLResponse)
def reviews_admin_page(request: Request):
    _require_admin(request)
    config = {
        "categories": {
            code: item["ru"] for code, item in CATEGORIES.items()
        },
        "reasons": {
            category: {
                code: labels[0] for code, labels in items.items()
            }
            for category, items in REASONS.items()
        },
    }
    html = ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__ADMIN_CONFIG__",
        json.dumps(config, ensure_ascii=False).replace("<", "\\u003c"),
    )
    response = HTMLResponse(html)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/api/admin/reviews/stats")
def reviews_admin_stats(request: Request, response: Response):
    _require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_reviews_dashboard(request.query_params, REVIEWS_DB_PATH)
    except AnalyticsFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/api/admin/reviews/{review_id}")
def reviews_admin_detail(review_id: int, request: Request, response: Response):
    _require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    review = get_review_detail(review_id, REVIEWS_DB_PATH)
    if not review:
        raise HTTPException(status_code=404, detail="review_not_found")
    return review
