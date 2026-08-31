from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class SearchResult:
    position: int
    title: str
    link: str
    snippet: str
    domain: str


class ProductSearch(Protocol):
    async def search(self, query: str) -> tuple[SearchResult, ...]: ...

    async def aclose(self) -> None: ...


def _text(value: object, limit: int) -> str:
    compact = " ".join(_CONTROL_RE.sub(" ", str(value or "")).split())
    return compact[:limit].strip()


def _domain(link: str) -> str:
    try:
        parsed = urlsplit(link)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    hostname = parsed.hostname.casefold().strip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname or hostname == "localhost" or "." not in hostname:
        return ""
    return hostname


class SerperProductSearch:
    """Small, bounded client for Serper's Google Search JSON endpoint."""

    endpoint = "https://google.serper.dev/search"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: int = 12,
        country: str = "uz",
        language: str = "ru",
    ):
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.country = country
        self.language = language
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def search(self, query: str) -> tuple[SearchResult, ...]:
        safe_query = _text(query, 220)
        if not safe_query:
            return ()
        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.post(
                    self.endpoint,
                    headers={
                        "X-API-KEY": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "q": safe_query,
                        "gl": self.country,
                        "hl": self.language,
                        "num": 10,
                        "autocorrect": False,
                    },
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                response.raise_for_status()
                break
            except asyncio.CancelledError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise
            except httpx.HTTPStatusError:
                raise
        if response is None:
            if last_error is not None:
                raise last_error
            return ()
        if len(response.content) > 1_000_000:
            raise ValueError("search response is too large")
        payload: Any = response.json()
        if not isinstance(payload, dict):
            return ()
        organic = payload.get("organic")
        if not isinstance(organic, list):
            return ()

        results: list[SearchResult] = []
        seen_links: set[str] = set()
        for item in organic[:20]:
            if not isinstance(item, dict):
                continue
            link = _text(item.get("link"), 700)
            domain = _domain(link)
            title = _text(item.get("title"), 240)
            snippet = _text(item.get("snippet"), 500)
            if not domain or not title or link in seen_links:
                continue
            seen_links.add(link)
            results.append(
                SearchResult(
                    position=len(results) + 1,
                    title=title,
                    link=link,
                    snippet=snippet,
                    domain=domain,
                )
            )
            if len(results) >= 10:
                break
        return tuple(results)

    async def aclose(self) -> None:
        await self._client.aclose()
