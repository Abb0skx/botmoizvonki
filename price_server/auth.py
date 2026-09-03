from __future__ import annotations

import base64
import secrets

from fastapi import HTTPException, Request

from .config import PriceSettings


def _monitoring_session(request: Request, *, admin: bool = False):
    """Use the unified portal session when it is present and enabled.

    Imports stay local so the price subsystem can still run on its own during
    rollback, without making monitoring a startup dependency.
    """
    from monitoring.auth import SESSION_COOKIE
    from monitoring import router as monitoring_router

    if not monitoring_router.settings.enabled:
        return None
    if not request.cookies.get(SESSION_COOKIE):
        return None
    return monitoring_router.get_auth().principal(request, admin=admin)


def require_admin(request: Request, settings: PriceSettings):
    principal = _monitoring_session(request)
    if principal is not None:
        return principal
    if not settings.admin_password:
        raise HTTPException(
            status_code=503,
            detail="price_admin_password_not_configured",
        )

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="authentication_required",
            headers={
                "WWW-Authenticate": 'Basic realm="Texnikach Price"'
            },
        )

    try:
        decoded = base64.b64decode(
            authorization[6:].strip(),
            validate=True,
        ).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        username = password = ""

    if not (
        secrets.compare_digest(username, settings.admin_username)
        and secrets.compare_digest(password, settings.admin_password)
    ):
        raise HTTPException(
            status_code=401,
            detail="invalid_credentials",
            headers={
                "WWW-Authenticate": 'Basic realm="Texnikach Price"'
            },
        )
    return None


def require_admin_action(
    request: Request,
    settings: PriceSettings,
) -> None:
    principal = _monitoring_session(request, admin=True)
    if principal is not None:
        from monitoring import router as monitoring_router

        monitoring_router.get_auth().verify_csrf(request, principal)
        return
    require_admin(request, settings)
    if not secrets.compare_digest(
        request.headers.get("x-requested-with", ""),
        "TexnikachPriceAdmin",
    ):
        raise HTTPException(status_code=403, detail="csrf_header_required")


def require_sync_key(request: Request, settings: PriceSettings) -> None:
    if not settings.sync_api_key:
        raise HTTPException(
            status_code=503,
            detail="price_sync_api_key_not_configured",
        )
    supplied = request.headers.get("x-price-sync-key", "")
    if not secrets.compare_digest(supplied, settings.sync_api_key):
        raise HTTPException(status_code=403, detail="invalid_sync_key")
