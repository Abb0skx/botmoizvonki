from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .config import MonitoringSettings
from .database import MonitoringStore, StoredSession


SESSION_COOKIE = "__Host-texnikach_monitoring"
CSRF_COOKIE = "__Host-texnikach_monitoring_csrf"
OAUTH_COOKIE = "__Secure-texnikach_monitoring_oauth"
TELEGRAM_ISSUER = "https://oauth.telegram.org"
TELEGRAM_AUTH_URL = TELEGRAM_ISSUER + "/auth"
TELEGRAM_TOKEN_URL = TELEGRAM_ISSUER + "/token"
TELEGRAM_JWKS_URL = TELEGRAM_ISSUER + "/.well-known/jwks.json"


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 32_768:
        raise ValueError("invalid_base64url")
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def _safe_json_segment(value: str) -> dict[str, Any]:
    parsed = json.loads(_b64url_decode(value))
    if not isinstance(parsed, dict):
        raise ValueError("jwt_segment_must_be_object")
    return parsed


def _safe_next(value: str | None) -> str:
    candidate = (value or "/monitoring").strip()
    if not (
        candidate == "/monitoring"
        or candidate.startswith("/monitoring/")
        or candidate.startswith("/monitoring?")
        or candidate == "/price"
        or candidate.startswith("/price?")
        or candidate.startswith("/price#")
    ):
        return "/monitoring"
    if candidate.startswith("//") or "\\" in candidate or "\x00" in candidate:
        return "/monitoring"
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or parsed.path == "/monitoring/auth/callback":
        return "/monitoring"
    return candidate[:1000]


def _client_ip(request: Request) -> str:
    # The proxy chain is deployment-owned. Do not trust a user-supplied
    # forwarding header here; the value is audit-only, never authorization.
    return request.client.host if request.client else ""


@dataclass(frozen=True)
class ManagerPrincipal:
    telegram_user_id: int
    display_name: str
    role: str
    session: StoredSession


class TelegramOIDC:
    def __init__(self, settings: MonitoringSettings):
        self.settings = settings
        self._jwks: dict[str, Any] | None = None
        self._jwks_loaded_at = 0.0

    async def _get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._jwks and time.monotonic() - self._jwks_loaded_at < 3600:
            return self._jwks
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            response = await client.get(TELEGRAM_JWKS_URL)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise ValueError("invalid_telegram_jwks")
        self._jwks = payload
        self._jwks_loaded_at = time.monotonic()
        return payload

    async def exchange_code(self, code: str, code_verifier: str) -> str:
        if not code or len(code) > 4096:
            raise ValueError("invalid_authorization_code")
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                TELEGRAM_TOKEN_URL,
                auth=httpx.BasicAuth(
                    self.settings.telegram_client_id,
                    self.settings.telegram_client_secret,
                ),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.telegram_redirect_uri,
                    "client_id": self.settings.telegram_client_id,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            raise ValueError("telegram_token_exchange_failed")
        payload = response.json()
        token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise ValueError("telegram_id_token_missing")
        return token

    async def validate_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > 32_768:
            raise ValueError("invalid_id_token")
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("invalid_id_token")
        header = _safe_json_segment(parts[0])
        claims = _safe_json_segment(parts[1])
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("unsupported_id_token_algorithm")

        key = await self._find_key(header["kid"])
        public_key = rsa.RSAPublicNumbers(
            _b64url_uint(key["e"]), _b64url_uint(key["n"])
        ).public_key()
        signature = _b64url_decode(parts[2])
        try:
            public_key.verify(
                signature,
                (parts[0] + "." + parts[1]).encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise ValueError("invalid_id_token_signature") from exc

        now = int(time.time())
        if claims.get("iss") != TELEGRAM_ISSUER:
            raise ValueError("invalid_id_token_issuer")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.settings.telegram_client_id not in {str(item) for item in audiences}:
            raise ValueError("invalid_id_token_audience")
        try:
            issued_at = int(claims["iat"])
            expires_at = int(claims["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid_id_token_time") from exc
        if issued_at > now + 60 or expires_at <= now - 30 or expires_at - issued_at > 7200:
            raise ValueError("invalid_id_token_time")
        if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise ValueError("invalid_id_token_nonce")
        return claims

    async def _find_key(self, kid: str) -> dict[str, str]:
        for force in (False, True):
            jwks = await self._get_jwks(force=force)
            for item in jwks["keys"]:
                if (
                    isinstance(item, dict)
                    and item.get("kid") == kid
                    and item.get("kty") == "RSA"
                    and isinstance(item.get("n"), str)
                    and isinstance(item.get("e"), str)
                ):
                    return item
        raise ValueError("telegram_signing_key_not_found")


class MonitoringAuth:
    def __init__(self, settings: MonitoringSettings, store: MonitoringStore):
        self.settings = settings
        self.store = store
        self.oidc = TelegramOIDC(settings)

    def ensure_configured(self) -> None:
        try:
            self.settings.validate_auth()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    def password_login(
        self, request: Request, *, password: str, next_path: str | None
    ) -> tuple[str, str, str]:
        self.ensure_configured()
        client_ip = _client_ip(request)
        if not self.store.register_login_attempt(client_ip):
            self.store.audit("password_login", result="rate_limited", route=request.url.path)
            raise HTTPException(status_code=429, detail="too_many_login_attempts")
        if not secrets.compare_digest(password, self.settings.password):
            self.store.audit("password_login", result="invalid", route=request.url.path)
            raise HTTPException(status_code=401, detail="invalid_password")
        raw_session, csrf_token = self.store.create_session(
            telegram_user_id=0,
            display_name="Сотрудник TEXNIKACH",
            absolute_ttl_seconds=self.settings.session_ttl_seconds,
            idle_ttl_seconds=self.settings.idle_ttl_seconds,
            user_agent=request.headers.get("user-agent", ""),
            ip_address=_client_ip(request),
        )
        self.store.clear_login_attempts(client_ip)
        self.store.audit(
            "password_login", result="success", telegram_user_id=0,
            role="admin", route=request.url.path,
        )
        return raw_session, csrf_token, _safe_next(next_path)

    def begin_login(self, next_path: str | None) -> RedirectResponse:
        self.ensure_configured()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        browser_token = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.store.create_oauth_attempt(
            state=state,
            browser_token=browser_token,
            nonce=nonce,
            code_verifier=verifier,
            next_path=_safe_next(next_path),
        )
        query = urlencode({
            "client_id": self.settings.telegram_client_id,
            "redirect_uri": self.settings.telegram_redirect_uri,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        response = RedirectResponse(TELEGRAM_AUTH_URL + "?" + query, status_code=303)
        response.set_cookie(
            OAUTH_COOKIE,
            browser_token,
            max_age=600,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/monitoring/auth",
        )
        return response

    async def finish_login(
        self,
        request: Request,
        *,
        code: str,
        state: str,
    ) -> tuple[str, str, str]:
        self.ensure_configured()
        browser_token = request.cookies.get(OAUTH_COOKIE, "")
        attempt = self.store.consume_oauth_attempt(
            state=state,
            browser_token=browser_token,
        )
        if attempt is None:
            self.store.audit("oidc_callback", result="invalid_state", route=request.url.path)
            raise HTTPException(status_code=400, detail="invalid_or_expired_login")
        try:
            id_token = await self.oidc.exchange_code(code, attempt["code_verifier"])
            claims = await self.oidc.validate_id_token(id_token, nonce=attempt["nonce"])
            telegram_id = int(claims.get("id") or claims.get("sub"))
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            self.store.audit("oidc_callback", result=type(exc).__name__, route=request.url.path)
            raise HTTPException(status_code=401, detail="telegram_login_failed") from None
        role = self.settings.role_for(telegram_id)
        if role is None:
            self.store.audit(
                "oidc_callback", result="not_allowed",
                telegram_user_id=telegram_id, route=request.url.path,
            )
            raise HTTPException(status_code=403, detail="manager_access_required")
        display_name = str(
            claims.get("name")
            or claims.get("preferred_username")
            or f"Telegram {telegram_id}"
        )[:160]
        raw_session, csrf_token = self.store.create_session(
            telegram_user_id=telegram_id,
            display_name=display_name,
            absolute_ttl_seconds=self.settings.session_ttl_seconds,
            idle_ttl_seconds=self.settings.idle_ttl_seconds,
            user_agent=request.headers.get("user-agent", ""),
            ip_address=_client_ip(request),
        )
        self.store.audit(
            "login", result="success", telegram_user_id=telegram_id,
            role=role, route=request.url.path,
        )
        return raw_session, csrf_token, attempt["next_path"]

    def principal(self, request: Request, *, admin: bool = False) -> ManagerPrincipal:
        self.ensure_configured()
        raw_token = request.cookies.get(SESSION_COOKIE, "")
        session = self.store.get_session(
            raw_token,
            idle_ttl_seconds=self.settings.idle_ttl_seconds,
        )
        if session is None:
            raise HTTPException(status_code=401, detail="monitoring_session_required")
        if session.telegram_user_id != 0:
            self.store.revoke_session(raw_token, reason="telegram_auth_removed")
            raise HTTPException(status_code=401, detail="monitoring_session_required")
        role = "admin"
        if admin and role != "admin":
            raise HTTPException(status_code=403, detail="monitoring_admin_required")
        return ManagerPrincipal(
            telegram_user_id=session.telegram_user_id,
            display_name=session.display_name,
            role=role,
            session=session,
        )

    def verify_csrf(self, request: Request, principal: ManagerPrincipal) -> None:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="json_content_type_required")
        origin = request.headers.get("origin", "")
        base = urlparse(self.settings.base_url)
        expected_origin = f"{base.scheme}://{base.netloc}"
        if origin != expected_origin:
            raise HTTPException(status_code=403, detail="invalid_origin")
        supplied = request.headers.get("x-csrf-token", "")
        cookie_value = request.cookies.get(CSRF_COOKIE, "")
        if not supplied or not cookie_value or not hmac.compare_digest(supplied, cookie_value):
            raise HTTPException(status_code=403, detail="csrf_failed")
        supplied_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied_hash, principal.session.csrf_token_hash):
            raise HTTPException(status_code=403, detail="csrf_failed")

    def attach_session_cookies(
        self,
        response: Response,
        *,
        raw_session: str,
        csrf_token: str,
    ) -> None:
        common = {
            "max_age": self.settings.session_ttl_seconds,
            "secure": True,
            "samesite": "lax",
            "path": "/",
        }
        response.set_cookie(SESSION_COOKIE, raw_session, httponly=True, **common)
        response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, **common)

    def clear_cookies(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, samesite="lax")
        response.delete_cookie(CSRF_COOKIE, path="/", secure=True, samesite="lax")
        response.delete_cookie(OAUTH_COOKIE, path="/monitoring/auth", secure=True, samesite="lax")


def monitoring_security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    return response


__all__ = [
    "CSRF_COOKIE", "ManagerPrincipal", "MonitoringAuth", "SESSION_COOKIE",
    "_safe_next", "monitoring_security_headers",
]
