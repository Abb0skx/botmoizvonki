from __future__ import annotations

from typing import Any

import httpx

from monitoring.config import MonitoringSettings


class GoSiteAdapter:
    def __init__(self, settings: MonitoringSettings):
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.go_api_url and self.settings.go_api_token)

    async def stats(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("go_source_not_configured")
        configured_url = httpx.URL(self.settings.go_api_url)
        query = dict(configured_url.params)
        query.update({
            key: value for key, value in params.items() if value is not None
        })
        endpoint = configured_url.copy_with(query=None)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.0),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                endpoint,
                params=query,
                headers={
                    "Authorization": "Bearer " + self.settings.go_api_token,
                    "Accept": "application/json",
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"go_source_http_{response.status_code}")
        if len(response.content) > 2 * 1024 * 1024:
            raise RuntimeError("go_source_response_too_large")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("go_source_invalid_json")
        if payload.get("schema_version") != 1:
            raise RuntimeError("go_source_schema_unsupported")
        return payload
