from __future__ import annotations

from typing import Any

import httpx

from monitoring.config import MonitoringSettings


class DeliveryAdapter:
    def __init__(self, settings: MonitoringSettings):
        self.settings = settings

    def configured(self) -> bool:
        return bool(
            self.settings.delivery_base_url
            and self.settings.delivery_service_token
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.configured():
            raise RuntimeError("delivery_source_not_configured")
        url = self.settings.delivery_base_url + path
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": (
                        "Bearer " + self.settings.delivery_service_token
                    ),
                    "Accept": "application/json",
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"delivery_source_http_{response.status_code}")
        if len(response.content) > 8 * 1024 * 1024:
            raise RuntimeError("delivery_source_response_too_large")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("delivery_source_invalid_json")
        return payload

    async def get_bytes(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        accept: str = "application/octet-stream",
        max_bytes: int = 12 * 1024 * 1024,
    ) -> tuple[bytes, str]:
        if not self.configured():
            raise RuntimeError("delivery_source_not_configured")
        url = self.settings.delivery_base_url + path
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": (
                        "Bearer " + self.settings.delivery_service_token
                    ),
                    "Accept": accept,
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"delivery_source_http_{response.status_code}")
        if len(response.content) > max_bytes:
            raise RuntimeError("delivery_source_response_too_large")
        return response.content, response.headers.get("content-type", "")
