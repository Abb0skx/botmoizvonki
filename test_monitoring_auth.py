from __future__ import annotations

import sqlite3
import tempfile
import unittest
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from monitoring.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    MonitoringAuth,
    TelegramOIDC,
    _safe_next,
)
from monitoring.config import MonitoringSettings
from monitoring.database import MonitoringStore
import monitoring.router as monitoring_router
import monitoring.adapters.go_site as go_site_module
from monitoring.adapters.go_site import GoSiteAdapter
from price_server.auth import require_admin, require_admin_action


class PriceAuthSettings:
    admin_username = "legacy"
    admin_password = "legacy-secret"


def settings(path: Path) -> MonitoringSettings:
    return MonitoringSettings(
        enabled=True,
        base_url="https://bot.texnikach.uz/monitoring",
        session_db_path=path,
        session_ttl_seconds=43_200,
        idle_ttl_seconds=7_200,
        manager_ids=frozenset({101, 202}),
        admin_ids=frozenset({202}),
        telegram_client_id="123456",
        telegram_client_secret="test-secret",
        telegram_redirect_uri=(
            "https://bot.texnikach.uz/monitoring/auth/callback"
        ),
    )


class MonitoringStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sessions.db"
        self.store = MonitoringStore(self.path)
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_oauth_attempt_is_single_use_and_browser_bound(self):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.store.create_oauth_attempt(
            state="state", browser_token="browser", nonce="nonce",
            code_verifier="verifier", next_path="/monitoring/calls", now=now,
        )
        self.assertIsNone(self.store.consume_oauth_attempt(
            state="state", browser_token="wrong", now=now
        ))
        attempt = self.store.consume_oauth_attempt(
            state="state", browser_token="browser", now=now
        )
        self.assertEqual(attempt["next_path"], "/monitoring/calls")
        self.assertIsNone(self.store.consume_oauth_attempt(
            state="state", browser_token="browser", now=now
        ))

    def test_raw_session_and_csrf_are_not_stored(self):
        raw, csrf = self.store.create_session(
            telegram_user_id=101, display_name="Manager",
            absolute_ttl_seconds=1000, idle_ttl_seconds=500,
        )
        with sqlite3.connect(self.path) as database:
            row = database.execute(
                "SELECT token_hash, csrf_token_hash FROM monitoring_sessions"
            ).fetchone()
        self.assertNotEqual(row[0], raw)
        self.assertNotEqual(row[1], csrf)
        self.assertNotIn(raw, self.path.read_bytes().decode("latin1"))
        self.assertNotIn(csrf, self.path.read_bytes().decode("latin1"))

    def test_expiry_and_revoke_are_fail_closed(self):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        raw, _ = self.store.create_session(
            telegram_user_id=101, display_name="Manager",
            absolute_ttl_seconds=100, idle_ttl_seconds=50, now=now,
        )
        self.assertIsNotNone(self.store.get_session(
            raw, idle_ttl_seconds=50, now=now + timedelta(seconds=20)
        ))
        self.assertIsNone(self.store.get_session(
            raw, idle_ttl_seconds=50, now=now + timedelta(seconds=51)
        ))
        raw2, _ = self.store.create_session(
            telegram_user_id=101, display_name="Manager",
            absolute_ttl_seconds=100, idle_ttl_seconds=50, now=now,
        )
        self.assertTrue(self.store.revoke_session(raw2, reason="logout", now=now))
        self.assertIsNone(self.store.get_session(raw2, idle_ttl_seconds=50, now=now))


class MonitoringSettingsTests(unittest.TestCase):
    def test_existing_delivery_manager_ids_are_the_safe_default(self):
        with patch.dict(os.environ, {
            "MONITORING_MANAGER_IDS": "",
            "DELIVERY_MANAGER_IDS": "101,202,303",
            "MONITORING_ADMIN_IDS": "",
        }, clear=False):
            current = MonitoringSettings.load()
        self.assertEqual(current.manager_ids, frozenset({101, 202, 303}))
        self.assertEqual(current.admin_ids, frozenset())

    def test_standalone_service_urls_and_tokens_are_loaded(self):
        with patch.dict(os.environ, {
            "MONITORING_DELIVERY_BASE_URL": "http://delivery-stats:8080/",
            "MONITORING_DELIVERY_SERVICE_TOKEN": "delivery-secret",
            "MONITORING_PRICE_BASE_URL": "http://price-web:8080/",
            "MONITORING_PRICE_SERVICE_TOKEN": "price-secret",
        }, clear=False):
            current = MonitoringSettings.load()
        self.assertEqual(current.delivery_base_url, "http://delivery-stats:8080")
        self.assertEqual(current.delivery_service_token, "delivery-secret")
        self.assertEqual(current.price_base_url, "http://price-web:8080")
        self.assertEqual(current.price_service_token, "price-secret")


class MonitoringRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        current = settings(Path(self.temp.name) / "sessions.db")
        monitoring_router.settings = current
        monitoring_router._store = None
        monitoring_router._auth = None
        app = FastAPI()
        app.include_router(monitoring_router.router)

        @app.get("/test/price/read")
        def price_read(request: Request):
            require_admin(request, PriceAuthSettings())
            return {"ok": True}

        @app.post("/test/price/action")
        def price_action(request: Request):
            require_admin_action(request, PriceAuthSettings())
            return {"ok": True}

        self.client = TestClient(app, base_url="https://bot.texnikach.uz")

    def tearDown(self):
        self.temp.cleanup()

    def login(self, user_id: int = 101):
        raw, csrf = monitoring_router.get_store().create_session(
            telegram_user_id=user_id, display_name="Olmas",
            absolute_ttl_seconds=43_200, idle_ttl_seconds=7_200,
        )
        self.client.cookies.set(SESSION_COOKIE, raw, path="/")
        self.client.cookies.set(CSRF_COOKIE, csrf, path="/")
        return csrf

    def test_html_redirects_but_api_returns_json_401(self):
        page = self.client.get("/monitoring", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertIn("/monitoring/login", page.headers["location"])
        api = self.client.get("/monitoring/api/me")
        self.assertEqual(api.status_code, 401)
        self.assertNotIn("www-authenticate", api.headers)

    def test_manager_session_opens_portal_without_password_form(self):
        self.login()
        response = self.client.get("/monitoring")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Olmas", response.text)
        self.assertNotIn('type="password"', response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_allowlist_is_checked_on_every_request(self):
        self.login(101)
        monitoring_router.settings = MonitoringSettings(
            **{
                **monitoring_router.settings.__dict__,
                "manager_ids": frozenset({999}),
            }
        )
        monitoring_router._auth = MonitoringAuth(
            monitoring_router.settings, monitoring_router.get_store()
        )
        self.assertEqual(self.client.get("/monitoring/api/me").status_code, 401)

    def test_logout_requires_origin_and_revokes_session(self):
        csrf = self.login()
        denied = self.client.post(
            "/monitoring/auth/logout",
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        self.assertEqual(denied.status_code, 403)
        response = self.client.post(
            "/monitoring/auth/logout",
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://bot.texnikach.uz",
                "Content-Type": "application/json",
            },
            content="{}",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get("/monitoring/api/me").status_code, 401)

    def test_admin_role_is_derived_from_allowlist(self):
        self.login(202)
        self.assertEqual(
            self.client.get("/monitoring/api/me").json()["role"], "admin"
        )

    def test_price_read_uses_shared_session_but_actions_require_admin(self):
        csrf = self.login(101)
        self.assertEqual(self.client.get("/test/price/read").status_code, 200)
        denied = self.client.post(
            "/test/price/action",
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://bot.texnikach.uz",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        self.assertEqual(denied.status_code, 403)

    def test_price_admin_action_uses_shared_csrf_protection(self):
        csrf = self.login(202)
        denied = self.client.post(
            "/test/price/action",
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.post(
            "/test/price/action",
            headers={
                "X-CSRF-Token": csrf,
                "Origin": "https://bot.texnikach.uz",
                "Content-Type": "application/json",
            },
            content="{}",
        )
        self.assertEqual(allowed.status_code, 200)

    def test_delivery_map_is_session_protected_and_validates_media_type(self):
        self.assertEqual(
            self.client.get("/monitoring/api/delivery/map.png").status_code,
            401,
        )
        self.login()
        get_bytes = AsyncMock(return_value=(b"png-data", "image/png"))
        with patch.object(
            monitoring_router.DeliveryAdapter,
            "get_bytes",
            new=get_bytes,
        ):
            response = self.client.get(
                "/monitoring/api/delivery/map.png",
                params={"day": "yesterday", "delivery_courier_id": "42"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, b"png-data")
        self.assertEqual(
            get_bytes.await_args.kwargs["params"]["courier_id"], "42"
        )

    def test_price_summary_and_catalog_are_session_protected(self):
        self.assertEqual(
            self.client.get("/monitoring/api/prices").status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/monitoring/prices/catalog").status_code,
            401,
        )
        self.login()
        summary = {"status": "enabled", "snapshot": None, "sections": []}
        with patch.object(
            monitoring_router.prices_adapter.PriceAdapter,
            "summary",
            new=AsyncMock(return_value=summary),
        ):
            response = self.client.get("/monitoring/api/prices")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], summary)

        with patch.object(
            monitoring_router.prices_adapter.PriceAdapter,
            "catalog",
            new=AsyncMock(return_value=(
                b"<html>price</html>",
                "text/html; charset=utf-8",
            )),
        ):
            catalog = self.client.get("/monitoring/prices/catalog")
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.text, "<html>price</html>")
        self.assertEqual(catalog.headers["x-frame-options"], "SAMEORIGIN")
        self.assertIn(
            "frame-ancestors 'self'",
            catalog.headers["content-security-policy"],
        )

    def test_overview_survives_one_unavailable_source(self):
        self.login()
        calls = {"stats": {"calls": 12, "answered": 10}}
        reviews = {"summary": {"total": 4, "attention": 1}}
        prices = {"status": "enabled", "sections": []}

        async def delivery_or_go(path=None, params=None):
            return {"summary": {"active": 2}}

        with patch.object(
            monitoring_router.calls_adapter, "calls_summary", return_value=calls
        ), patch.object(
            monitoring_router.reviews_adapter,
            "reviews_dashboard",
            return_value=reviews,
        ), patch.object(
            monitoring_router.prices_adapter.PriceAdapter,
            "summary",
            new=AsyncMock(return_value=prices),
        ), patch.object(
            monitoring_router.DeliveryAdapter, "get", new=AsyncMock(return_value={"summary": {"active": 2}})
        ), patch.object(
            monitoring_router.GoSiteAdapter,
            "stats",
            new=AsyncMock(side_effect=RuntimeError("go_source_not_configured")),
        ):
            response = self.client.get("/monitoring/api/overview")
        self.assertEqual(response.status_code, 200)
        sources = response.json()["sources"]
        self.assertEqual(sources["calls"]["meta"]["status"], "ok")
        self.assertEqual(sources["go_site"]["meta"]["status"], "unavailable")
        self.assertEqual(sources["delivery"]["data"]["summary"]["active"], 2)


class SafeNextTests(unittest.TestCase):
    def test_only_monitoring_relative_paths_are_allowed(self):
        self.assertEqual(_safe_next("/monitoring/calls?period=7d"), "/monitoring/calls?period=7d")
        for unsafe in (
            "https://evil.example", "//evil.example/monitoring",
            "/dashboard", "/monitoringevil", "/monitoring\\evil",
            "/monitoring/auth/callback",
        ):
            self.assertEqual(_safe_next(unsafe), "/monitoring")


class TelegramOIDCTests(unittest.IsolatedAsyncioTestCase):
    async def test_rs256_token_claims_and_nonce_are_verified(self):
        current = settings(Path("unused.db"))
        oidc = TelegramOIDC(current)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private_key.public_key().public_numbers()

        def encoded_int(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        async def jwks(*, force: bool = False):
            return {"keys": [{
                "kid": "test-key", "kty": "RSA",
                "n": encoded_int(numbers.n), "e": encoded_int(numbers.e),
            }]}

        oidc._get_jwks = jwks

        def segment(payload: dict) -> str:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        now = int(time.time())
        header = segment({"alg": "RS256", "kid": "test-key"})
        claims = segment({
            "iss": "https://oauth.telegram.org", "aud": "123456",
            "sub": "101", "id": 101, "name": "Olmas",
            "iat": now, "exp": now + 600, "nonce": "expected",
        })
        signing_input = f"{header}.{claims}".encode()
        signature = private_key.sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        token = f"{header}.{claims}." + base64.urlsafe_b64encode(
            signature
        ).rstrip(b"=").decode()
        validated = await oidc.validate_id_token(token, nonce="expected")
        self.assertEqual(validated["id"], 101)
        with self.assertRaisesRegex(ValueError, "nonce"):
            await oidc.validate_id_token(token, nonce="wrong")


class GoSiteAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_php_route_query_is_preserved(self):
        current = MonitoringSettings(**{
            **settings(Path("unused.db")).__dict__,
            "go_api_url": (
                "https://texnikach.uz/index.php?"
                "route=api/monitoring/go/stats"
            ),
            "go_api_token": "service-secret",
        })
        captured = {}

        class FakeResponse:
            status_code = 200
            content = b'{"schema_version":1}'

            def json(self):
                return {"schema_version": 1}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, *, params, headers):
                captured["url"] = str(url)
                captured["params"] = params
                captured["headers"] = headers
                return FakeResponse()

        with patch.object(go_site_module.httpx, "AsyncClient", FakeClient):
            result = await GoSiteAdapter(current).stats({"period": "today"})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(captured["url"], "https://texnikach.uz/index.php")
        self.assertEqual(
            captured["params"]["route"], "api/monitoring/go/stats"
        )
        self.assertEqual(captured["params"]["period"], "today")
        self.assertEqual(
            captured["headers"]["Authorization"], "Bearer service-secret"
        )


if __name__ == "__main__":
    unittest.main()
