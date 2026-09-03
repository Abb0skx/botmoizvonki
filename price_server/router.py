from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .auth import require_admin, require_admin_action, require_sync_key
from .config import PriceSettings
from .contracts import ContractError, SECTION_KEY_RE, validate_sync_payload
from .repository import (
    CurrentSnapshotUnavailableError,
    IdempotencyConflictError,
    PriceRepository,
    QuickLinkRotationConflictError,
    QuickLinkRotationUnavailableError,
    SnapshotValidationError,
    StaleSnapshotError,
)
from .scheduler import PriceScheduler
from .service import PricePublicationService


LOG = logging.getLogger("price_server.router")
router = APIRouter()
settings = PriceSettings.load()

_repository: PriceRepository | None = None
_service: PricePublicationService | None = None
_scheduler: PriceScheduler | None = None
_startup_error = ""
_runtime_lock = threading.RLock()
_ADMIN_JS = Path(__file__).resolve().parent / "static" / "admin.js"


def get_repository() -> PriceRepository:
    global _repository
    if _repository is None:
        with _runtime_lock:
            if _repository is None:
                _repository = PriceRepository(settings)
    return _repository


def get_service() -> PricePublicationService:
    global _service
    if not settings.telegram_configured:
        raise HTTPException(
            status_code=503,
            detail="price_telegram_not_configured",
        )
    if _service is None:
        with _runtime_lock:
            if _service is None:
                _service = PricePublicationService(
                    settings,
                    get_repository(),
                )
    return _service


def _require_enabled() -> None:
    if not settings.enabled:
        raise HTTPException(status_code=503, detail="price_server_disabled")
    if _startup_error:
        raise HTTPException(
            status_code=503,
            detail="price_server_configuration_blocked",
        )


async def start_price_server() -> None:
    global _scheduler, _startup_error
    if not settings.enabled:
        LOG.info("price_server_disabled")
        return

    # The standalone price deployment deliberately fences the legacy
    # monolith with ``disabled`` (and removes its sync key).  Recognize that
    # state before validation so an intentional safety fence is not reported
    # as a broken application startup and no stale repository is opened.
    if settings.scheduler_mode == "disabled":
        _startup_error = "scheduler_disabled"
        LOG.info("price_server_start_fenced scheduler_mode=disabled")
        return

    try:
        settings.validate_runtime()
    except RuntimeError as exc:
        _startup_error = type(exc).__name__
        LOG.error(
            "price_server_start_blocked type=%s reason=%s",
            type(exc).__name__,
            str(exc).replace("\n", " ")[:500],
        )
        return

    try:
        repository = get_repository()
        if (
            settings.telegram_configured
            and settings.scheduler_mode == "embedded"
        ):
            service = get_service()
            _scheduler = PriceScheduler(settings, repository, service)
            await _scheduler.start()
        _startup_error = ""
        LOG.info(
            "price_server_started telegram=%s scheduler_mode=%s",
            settings.telegram_configured,
            settings.scheduler_mode,
        )
    except Exception as exc:
        _startup_error = type(exc).__name__
        LOG.error("price_server_start_blocked type=%s", type(exc).__name__)


async def stop_price_server() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
        _scheduler = None


def _page_html() -> str | None:
    if settings.enabled and not _startup_error:
        snapshot = get_repository().get_current_snapshot()
        if snapshot:
            payload = snapshot.get("payload") or {}
            document = payload.get("html_document")
            if isinstance(document, str) and document.strip():
                return document
    try:
        return settings.legacy_html_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None


def _inject_admin_script(document: str) -> str:
    marker = '<script src="/price/assets/admin.js" defer></script>'
    if marker in document:
        return document
    lowered = document.casefold()
    body_index = lowered.rfind("</body>")
    if body_index >= 0:
        return document[:body_index] + marker + document[body_index:]
    return document + marker


def _secure_html(document: str, status_code: int = 200) -> HTMLResponse:
    response = HTMLResponse(
        content=document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data: https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
            ),
        },
    )
    return response


@router.get("/price/healthz")
async def price_health() -> dict[str, Any]:
    snapshot_available = False
    if settings.enabled and not _startup_error:
        try:
            snapshot_available = bool(
                get_repository().get_current_snapshot(include_payload=False)
            )
        except Exception:
            LOG.exception("price_health_repository_error")
    status = (
        "blocked"
        if _startup_error
        else "enabled"
        if settings.enabled
        else "disabled"
    )
    return {
        "status": status,
        "snapshot_available": snapshot_available,
        "telegram_configured": settings.telegram_configured,
        "preview_configured": settings.preview_configured,
        "scheduler_running": bool(_scheduler and _scheduler.running),
        "quick_link_rotation_configured": bool(
            settings.telegram_configured
            and settings.telegram_channel_username
            and settings.preview_configured
        ),
    }


@router.get("/price/assets/admin.js")
async def price_admin_script() -> Response:
    try:
        content = _ADMIN_JS.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        raise HTTPException(status_code=404, detail="asset_not_found")
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/price", response_class=HTMLResponse)
@router.get("/price/", response_class=HTMLResponse, include_in_schema=False)
async def price_page(request: Request) -> HTMLResponse:
    # Staged rollout: while the new subsystem is explicitly disabled, preserve
    # the existing read-only page exactly as it works today. Enabling the
    # subsystem switches this same route to fail-closed Basic authentication.
    principal = require_admin(request, settings) if settings.enabled else None
    document = _page_html()
    if document is None:
        return _secure_html("<h1>Price page not found</h1>", status_code=404)
    if settings.enabled and (
        principal is None or getattr(principal, "role", None) == "admin"
    ):
        document = _inject_admin_script(document)
    return _secure_html(document)


@router.post("/price/api/v1/sync", status_code=201)
async def sync_price_snapshot(request: Request) -> dict[str, Any]:
    _require_enabled()
    require_sync_key(request, settings)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.sync_max_bytes:
                raise HTTPException(status_code=413, detail="snapshot_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_content_length")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.sync_max_bytes:
            raise HTTPException(status_code=413, detail="snapshot_too_large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        source = json.loads(raw)
        payload = validate_sync_payload(source)
        result = get_repository().ingest_snapshot(
            payload,
            content_hash=payload["content_sha256"],
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json")
    except StaleSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ContractError, SnapshotValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "status": "accepted",
        "snapshot_id": result["snapshot_id"],
        "content_sha256": result["content_hash"],
        "created": bool(result.get("created")),
        "duplicate": bool(result.get("duplicate")),
        "product_count": result["product_count"],
        "section_count": result["section_count"],
    }


def _admin(request: Request, *, action: bool = False) -> None:
    _require_enabled()
    if action:
        require_admin_action(request, settings)
    else:
        require_admin(request, settings)


def _section_or_404(section_key: str) -> dict[str, Any]:
    if not SECTION_KEY_RE.fullmatch(section_key):
        raise HTTPException(status_code=404, detail="section_not_found")
    section = get_repository().get_section(section_key)
    if section is None:
        raise HTTPException(status_code=404, detail="section_not_found")
    return section


@router.get("/price/api/v1/state")
async def price_state(request: Request) -> dict[str, Any]:
    _admin(request)
    snapshot = get_repository().get_current_snapshot(include_payload=False)
    return {
        "snapshot": snapshot,
        "telegram_configured": settings.telegram_configured,
        "preview_configured": settings.preview_configured,
        "scheduler_running": bool(_scheduler and _scheduler.running),
        "quick_link_rotations": get_repository().list_quick_link_rotations(
            limit=20
        ),
        "exchange_rate_changes": (
            get_repository().list_exchange_rate_requests(limit=20)
        ),
    }


@router.get("/price/api/v1/sections")
async def price_sections(request: Request) -> dict[str, Any]:
    _admin(request)
    current_posts = get_repository().list_telegram_posts(
        channel_id=settings.telegram_channel_id,
        current_only=True,
        limit=5000,
    )
    message_ids: dict[str, list[int]] = {}
    for post in current_posts:
        message_ids.setdefault(post["section_key"], []).append(
            int(post["message_id"])
        )
    summaries = []
    for section in get_repository().list_sections():
        summary = {
                key: section.get(key)
                for key in (
                    "snapshot_id", "section_key", "position", "title",
                    "content_hash", "product_count", "changed_recent",
                )
        }
        summary["current_post_ids"] = message_ids.get(
            section["section_key"], []
        )
        summary["can_edit"] = bool(summary["current_post_ids"])
        summaries.append(summary)
    return {"sections": summaries}


@router.get("/price/api/v1/posts")
async def price_posts(request: Request) -> dict[str, Any]:
    _admin(request)
    return {"posts": get_repository().list_telegram_posts(limit=500)}


@router.get("/price/api/v1/jobs")
async def price_jobs(request: Request) -> dict[str, Any]:
    _admin(request)
    return {
        "jobs": get_repository().list_jobs(limit=500),
        "edit_batches": (
            get_repository().list_publication_edit_batches(limit=100)
        ),
        "quick_link_rotations": (
            get_repository().list_quick_link_rotations(limit=100)
        ),
        "exchange_rate_changes": (
            get_repository().list_exchange_rate_requests(limit=100)
        ),
    }


@router.get("/price/api/v1/quick-link-rotations")
async def price_quick_link_rotations(request: Request) -> dict[str, Any]:
    _admin(request)
    return {
        "quick_link_rotations": (
            get_repository().list_quick_link_rotations(limit=500)
        )
    }


@router.post(
    "/price/api/v1/quick-link-rotations/publish-now",
    status_code=202,
)
async def publish_main_quick_link_post_now(
    request: Request,
) -> dict[str, Any]:
    """Durably queue the existing rotation state machine for immediate use."""

    _admin(request, action=True)
    try:
        body = await request.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise HTTPException(
            status_code=422,
            detail="explicit_confirmation_required",
        )
    raw_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        request_id = str(uuid.UUID(raw_key))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=422,
            detail="valid_idempotency_key_required",
        )
    get_service()
    try:
        rotation = get_repository().enqueue_manual_quick_link_rotation(
            datetime.now(timezone.utc),
            idempotency_key=request_id,
        )
    except QuickLinkRotationUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except QuickLinkRotationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    secondary = get_repository().get_quick_link_post(
        str(rotation["secondary_quick_post_key"])
    )
    return {
        "status": (
            "existing" if bool(rotation.get("duplicate")) else "queued"
        ),
        "rotation_id": int(rotation["rotation_id"]),
        "scheduled_for": rotation["scheduled_for"],
        "local_date": rotation["local_date"],
        "rotation_index": int(rotation["rotation_index"]),
        "secondary_quick_post_key": rotation[
            "secondary_quick_post_key"
        ],
        "secondary_title": (
            str(secondary.get("title") or "") if secondary else ""
        ),
        "rotation_status": rotation["status"],
        "phase": rotation["phase"],
        "duplicate": bool(rotation.get("duplicate")),
    }


@router.post(
    "/price/api/v1/quick-link-rotations/{rotation_id}/reconcile",
    status_code=202,
)
async def reconcile_price_quick_link_rotation(
    rotation_id: int,
    request: Request,
) -> dict[str, Any]:
    """Resume only a send-ambiguous run using an administrator-verified ID."""
    _admin(request, action=True)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422,
            detail="request_body_must_be_object",
        )
    outcome = str(body.get("outcome") or "sent").strip().casefold()
    if outcome == "not_sent":
        if body.get("confirm_no_message_was_published") is not True:
            raise HTTPException(
                status_code=422,
                detail="not_sent_confirmation_required",
            )
        resumed = get_repository().confirm_quick_link_rotation_not_sent(
            rotation_id
        )
        if not resumed:
            raise HTTPException(
                status_code=409,
                detail="rotation_cannot_be_reconciled",
            )
        return {
            "status": "queued",
            "rotation_id": rotation_id,
            "outcome": "not_sent",
        }
    if outcome != "sent":
        raise HTTPException(
            status_code=422,
            detail="rotation_outcome_must_be_sent_or_not_sent",
        )
    try:
        message_id = int(body.get("new_main_message_id"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="new_main_message_id_must_be_positive",
        )
    if message_id <= 0:
        raise HTTPException(
            status_code=422,
            detail="new_main_message_id_must_be_positive",
        )
    try:
        resumed = get_repository().reconcile_quick_link_rotation_main_message(
            rotation_id,
            message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not resumed:
        raise HTTPException(
            status_code=409,
            detail="rotation_cannot_be_reconciled",
        )
    return {
        "status": "queued",
        "rotation_id": rotation_id,
        "outcome": "sent",
        "new_main_message_id": message_id,
    }


@router.post(
    "/price/api/v1/quick-link-rotations/{rotation_id}/retry",
    status_code=202,
)
async def retry_price_quick_link_rotation(
    rotation_id: int,
    request: Request,
) -> dict[str, Any]:
    """Resume a failed run without replaying already persisted phases."""
    _admin(request, action=True)
    resumed = get_repository().retry_failed_quick_link_rotation(rotation_id)
    if not resumed:
        raise HTTPException(
            status_code=409,
            detail="rotation_cannot_be_retried",
        )
    return {"status": "queued", "rotation_id": rotation_id}


def _enqueue(section_key: str, action: str, execute_at: datetime) -> dict[str, Any]:
    _section_or_404(section_key)
    get_service()
    if action == "edit" and not get_repository().has_current_telegram_post(
        section_key,
        settings.telegram_channel_id,
    ):
        raise HTTPException(status_code=409, detail="current_post_not_found")
    job = get_repository().enqueue_job(
        section_key,
        action,
        execute_at,
        channel_id=settings.telegram_channel_id,
        channel_key=settings.telegram_channel_username,
        snapshot_policy="latest",
        dedupe_key=uuid.uuid4().hex,
        payload={"source": "price_admin"},
    )
    return {
        "status": "queued",
        "job_id": job["job_id"],
        "execute_at": job["execute_at"],
        "action": job["action"],
    }


@router.post("/price/api/v1/sections/{section_key}/send-now", status_code=202)
async def send_section_now(section_key: str, request: Request) -> dict[str, Any]:
    _admin(request, action=True)
    return _enqueue(section_key, "send", datetime.now(timezone.utc))


@router.post("/price/api/v1/sections/{section_key}/edit-current", status_code=202)
async def edit_section_current(section_key: str, request: Request) -> dict[str, Any]:
    _admin(request, action=True)
    return _enqueue(section_key, "edit", datetime.now(timezone.utc))


@router.post("/price/api/v1/posts/update-all", status_code=202)
async def update_all_current_posts(request: Request) -> dict[str, Any]:
    """Queue fenced edits for every physical current price post."""

    _admin(request, action=True)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise HTTPException(
            status_code=422,
            detail="explicit_confirmation_required",
        )
    raw_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        batch_id = str(uuid.UUID(raw_key))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=422,
            detail="valid_idempotency_key_required",
        )
    get_service()
    try:
        batch = get_repository().enqueue_current_post_edit_batch(
            channel_id=settings.telegram_channel_id,
            channel_key=settings.telegram_channel_username,
            idempotency_key=batch_id,
            now=datetime.now(timezone.utc),
        )
    except CurrentSnapshotUnavailableError:
        raise HTTPException(
            status_code=409,
            detail="current_snapshot_not_available",
        )
    except IdempotencyConflictError:
        raise HTTPException(
            status_code=409,
            detail="idempotency_key_conflict",
        )
    return {
        "status": "queued",
        "batch_id": batch["batch_id"],
        "snapshot_id": batch["snapshot_id"],
        "job_count": batch["job_count"],
        "section_count": batch["section_count"],
        "job_ids": [job["job_id"] for job in batch["jobs"]],
        "duplicate": bool(batch["duplicate"]),
        "skipped": batch["skipped"],
    }


def _parse_schedule(value: Any) -> datetime:
    zone = ZoneInfo(settings.timezone)
    now = datetime.now(timezone.utc)
    text_value = str(value or "").strip()
    if text_value == "tomorrow_0930":
        local_now = now.astimezone(zone)
        local_date = local_now.date() + timedelta(days=1)
        result = datetime.combine(local_date, time(9, 30), tzinfo=zone)
    else:
        normalized = text_value.replace(" ", "T", 1)
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="when_must_be_yyyy_mm_dd_hh_mm",
            ) from exc
        if result.tzinfo is None:
            result = result.replace(tzinfo=zone)
    result_utc = result.astimezone(timezone.utc)
    if result_utc <= now:
        raise HTTPException(status_code=422, detail="schedule_must_be_in_future")
    if result_utc > now + timedelta(days=366):
        raise HTTPException(status_code=422, detail="schedule_is_too_far")
    return result_utc


@router.post("/price/api/v1/sections/{section_key}/schedule", status_code=202)
async def schedule_section(section_key: str, request: Request) -> dict[str, Any]:
    _admin(request, action=True)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request_body_must_be_object")
    mode = str(body.get("mode", "send")).strip().casefold()
    if mode not in {"send", "edit"}:
        raise HTTPException(status_code=422, detail="unsupported_schedule_mode")
    return _enqueue(section_key, mode, _parse_schedule(body.get("when")))


@router.post("/price/api/v1/jobs/{job_id}/cancel")
async def cancel_price_job(job_id: int, request: Request) -> dict[str, Any]:
    _admin(request, action=True)
    cancelled = get_service().cancel_scheduled_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="job_cannot_be_cancelled")
    return {"status": "cancelled", "job_id": job_id}
