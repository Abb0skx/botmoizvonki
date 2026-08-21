import json
import secrets
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse

from .catalog import CATEGORIES, REASONS
from .config import (
    REVIEWS_COMPLAINT_PHONE,
    REVIEWS_COMPLAINT_TELEGRAM,
    REVIEWS_INSTAGRAM_URL,
    REVIEWS_DB_PATH,
    REVIEWS_SITE_URL,
    REVIEWS_TELEGRAM_URL,
)
from .database import list_active_managers
from .service import (
    ReviewRateLimitError,
    ReviewValidationError,
    create_review,
    send_critical_review_notification,
)


router = APIRouter(tags=["customer-reviews"])
TEMPLATE_PATH = Path(__file__).parent / "templates" / "rating.html"


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return request.client.host if request.client else None


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
        )
    except ReviewRateLimitError:
        raise HTTPException(status_code=429, detail="rate_limit") from None
    except ReviewValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    if review["needs_attention"]:
        background_tasks.add_task(send_critical_review_notification, review)
    return {"ok": True, "review_id": review["id"]}


@router.get("/reviews/status")
def reviews_status():
    return {"ok": True, "module": "customer-reviews"}
