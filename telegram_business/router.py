from __future__ import annotations

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from .config import BusinessSettings
from .service import BusinessService

router=APIRouter(tags=["telegram-business"])
settings=BusinessSettings.load()
service: BusinessService | None = None

# Keep this list in the manual setWebhook payload. callback_query is required
# for the opaque inline-button night wizard; no ordinary message is read by
# acknowledging a callback.
TELEGRAM_BUSINESS_ALLOWED_UPDATES = (
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "callback_query",
)


def get_service() -> BusinessService:
    global service
    if service is None:
        settings.validate_enabled()
        service=BusinessService(settings)
    return service


@router.post("/webhooks/telegram-business", status_code=200)
async def telegram_business_webhook(request: Request):
    if not settings.enabled: raise HTTPException(status_code=404,detail="Telegram Business is disabled")
    supplied=request.headers.get("X-Telegram-Bot-Api-Secret-Token","")
    if not settings.webhook_secret or not hmac.compare_digest(supplied,settings.webhook_secret):
        raise HTTPException(status_code=403,detail="Invalid webhook secret")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Telegram update must be an object")
    now=datetime.now(ZoneInfo(settings.timezone)); active=get_service()
    try:
        stored = active.repo.save_update(
            payload,
            now,
            allowed_connection_id=settings.allowed_connection_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not stored: return {"ok":True,"duplicate":True}
    # A durable scheduler claims this row from SQLite. Returning only after the
    # transaction commits ensures a process restart cannot lose an acknowledged
    # Telegram webhook.
    return {"ok":True,"queued":True}
