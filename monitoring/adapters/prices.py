from __future__ import annotations

from typing import Any

import httpx

from monitoring.config import MonitoringSettings


class PriceAdapter:
    def __init__(self, settings: MonitoringSettings):
        self.settings = settings

    def configured(self) -> bool:
        return bool(
            self.settings.price_base_url
            and self.settings.price_service_token
        )

    def admin_configured(self) -> bool:
        return self.settings.price_management_configured()

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.settings.price_service_token,
            "Accept": accept,
        }

    async def summary(self) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("price_source_not_configured")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                self.settings.price_base_url
                + "/internal/monitoring/v1/prices/summary",
                headers=self._headers("application/json"),
            )
        if response.status_code != 200:
            raise RuntimeError(f"price_source_http_{response.status_code}")
        if len(response.content) > 2 * 1024 * 1024:
            raise RuntimeError("price_source_response_too_large")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("price_source_invalid_json")
        return payload

    async def catalog(self) -> tuple[bytes, str]:
        if not self.configured():
            raise RuntimeError("price_source_not_configured")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                self.settings.price_base_url
                + "/internal/monitoring/v1/prices/catalog",
                headers=self._headers("text/html"),
            )
        if response.status_code != 200:
            raise RuntimeError(f"price_source_http_{response.status_code}")
        if len(response.content) > 12 * 1024 * 1024:
            raise RuntimeError("price_source_response_too_large")
        return response.content, response.headers.get("content-type", "")

    async def admin_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        idempotency_key: str = "",
    ) -> tuple[int, bytes, str]:
        if not self.admin_configured():
            raise RuntimeError("price_admin_source_not_configured")
        headers = {
            "Authorization": (
                "Bearer " + self.settings.price_admin_service_token
            ),
            "Accept": "application/json",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method,
                self.settings.price_base_url + path,
                headers=headers,
                content=body if method == "POST" else None,
            )
        if len(response.content) > 4 * 1024 * 1024:
            raise RuntimeError("price_source_response_too_large")
        return (
            response.status_code,
            response.content,
            response.headers.get("content-type", ""),
        )


__all__ = ["PriceAdapter"]
