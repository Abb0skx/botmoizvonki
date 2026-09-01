import asyncio
import logging
import re
import time
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .parsers import map_url_provider, parse_location_url

logger = logging.getLogger(__name__)
_geocode_lock = asyncio.Lock()
_last_geocode_request = 0.0
_geocode_cache: dict[tuple[float, float], dict[str, str | None]] = {}
_map_url_cache: dict[str, tuple[float | None, float | None, str, float]] = {}
_MAX_MAP_REDIRECTS = 8
_MAX_MAP_RESPONSE_BYTES = 640_000
_MAP_RESOLUTION_DEADLINE_SECONDS = 12.0
_MAP_CACHE_TTL_SECONDS = 6 * 60 * 60

# Canonical names keep monthly district statistics stable even when Telegram,
# Nominatim or a manager uses Russian/Uzbek spelling variants.
_TASHKENT_DISTRICTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Алмазарский район", ("алмазар", "олмазар", "almazar", "olmazor")),
    ("Бектемирский район", ("бектемир", "bektemir")),
    ("Мирабадский район", ("мирабад", "миробод", "mirabad", "mirobod")),
    ("Мирзо-Улугбекский район", ("мирзо улугбек", "мирзо-улугбек", "mirzo ulugbek", "mirzo-ulugbek")),
    ("Сергелийский район", ("сергели", "sergeli")),
    ("Учтепинский район", ("учтеп", "uchtepa")),
    ("Чиланзарский район", ("чиланзар", "чилонзор", "chilanzar", "chilonzor")),
    ("Шайхантахурский район", ("шайхантахур", "шайхонтохур", "shaykhantakhur", "shayxontohur")),
    ("Юнусабадский район", ("юнусабад", "юнусобод", "yunusabad", "yunusobod")),
    ("Яккасарайский район", ("яккасарай", "яккасарой", "yakkasaray", "yakkasaroy")),
    ("Янгихаётский район", ("янгихаёт", "янгихаят", "yangihayot")),
    ("Яшнабадский район", ("яшнабад", "яшнобод", "yashnabad", "yashnobod")),
)
_MAHALLA_RE = re.compile(
    r"(?:махалл(?:а|я|аси)?|мфй|mahalla(?:si)?|mahalla)\s*[:\-]?\s*"
    r"([^,;\n]{2,100})",
    re.IGNORECASE,
)


def _recognized_district(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[’'ʻ`]+", "", value.casefold().replace("ё", "е"))
    normalized = re.sub(r"[\s_\-]+", " ", normalized).strip()
    for canonical, aliases in _TASHKENT_DISTRICTS:
        if any(alias.replace("-", " ") in normalized for alias in aliases):
            return canonical
    return None


def normalize_district(value: str | None) -> str | None:
    if not value:
        return None
    return _recognized_district(value) or value.strip()[:200] or None


def extract_text_address(value: str) -> dict[str, str | None]:
    """Extract stable district/mahalla fields from a manager-entered address."""
    address_text = value.strip()[:1000]
    mahalla_match = _MAHALLA_RE.search(address_text)
    mahalla = mahalla_match.group(1).strip(" .:-")[:200] if mahalla_match else None
    return {
        "address_text": address_text or None,
        "district": _recognized_district(address_text),
        "mahalla": mahalla or None,
    }


def _allowed_map_url(url: str) -> bool:
    return map_url_provider(url) is not None


def _cache_map_resolution(
    original: str,
    latitude: float | None,
    longitude: float | None,
    resolved: str,
    ttl_seconds: float,
) -> None:
    now = time.monotonic()
    if len(_map_url_cache) >= 256 and original not in _map_url_cache:
        expired = next((key for key, value in _map_url_cache.items() if value[3] <= now), None)
        _map_url_cache.pop(expired or next(iter(_map_url_cache)), None)
    _map_url_cache[original] = (latitude, longitude, resolved, now + ttl_seconds)


class _MapHtmlLinks(HTMLParser):
    """Collect only browser-declared redirect/canonical URLs from bounded HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.yandex_objects: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        if (
            values.get("data-object", "").casefold() == "search-result"
            and values.get("data-id")
            and values.get("data-coordinates")
        ):
            self.yandex_objects.append((values["data-id"], values["data-coordinates"]))
        if tag == "link" and "canonical" in values.get("rel", "").casefold().split():
            if values.get("href"):
                self.urls.append(values["href"])
        elif tag == "meta":
            marker = (values.get("property") or values.get("name") or "").casefold()
            if marker in {"og:url", "twitter:url"} and values.get("content"):
                self.urls.append(values["content"])
            if values.get("http-equiv", "").casefold() == "refresh":
                refresh = re.search(r"(?:^|;)\s*url\s*=\s*(.+)$", values.get("content", ""), re.I)
                if refresh:
                    self.urls.append(refresh.group(1).strip(" \t\"'"))


def _html_map_urls(body: bytes, base_url: str) -> list[str]:
    """Return only canonical/meta/JavaScript-declared navigation targets.

    Arbitrary map URLs embedded in provider HTML are intentionally ignored:
    Google pages, for example, contain static-map viewport coordinates that
    are unrelated to the shared customer location.
    """
    text = body.decode("utf-8", errors="replace")
    text = unescape(text)
    for escaped, plain in (
        (r"\/", "/"),
        (r"\u0026", "&"),
        (r"\u003d", "="),
        (r"\x26", "&"),
        (r"\x3d", "="),
    ):
        text = text.replace(escaped, plain).replace(escaped.upper(), plain)

    parser = _MapHtmlLinks()
    try:
        parser.feed(text)
    except Exception:
        logger.debug("Could not parse map HTML metadata", exc_info=True)

    declared = list(parser.urls)
    javascript_patterns = (
        r"(?:window\.)?location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']",
        r"(?:window\.)?location\.replace\(\s*[\"']([^\"']+)[\"']\s*\)",
    )
    for pattern in javascript_patterns:
        declared.extend(re.findall(pattern, text, re.I))

    if map_url_provider(base_url) == "yandex":
        object_match = re.search(
            r"/org/(?:[^/?#]+/)?(\d+)(?:/|$)",
            unquote(urlsplit(base_url).path),
            re.I,
        )
        expected_id = object_match.group(1) if object_match else None
        if expected_id is not None:
            for object_id, coordinates in parser.yandex_objects:
                if object_id == expected_id:
                    declared.append(
                        "https://yandex.com/maps/?whatshere%5Bpoint%5D=" + coordinates
                    )
                    break

    def normalize(values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            candidate = urljoin(base_url, value.strip().rstrip(".,;:!?)>]}")[:8192])
            if _allowed_map_url(candidate) and candidate not in result:
                result.append(candidate)
        return result

    return normalize(declared)


def extract_address(payload: dict[str, Any]) -> dict[str, str | None]:
    address = payload.get("address") or {}
    district = (
        address.get("city_district")
        or address.get("district")
        or address.get("state_district")
        or address.get("county")
    )
    mahalla = (
        address.get("neighbourhood")
        or address.get("quarter")
        or address.get("suburb")
        or address.get("residential")
    )
    return {
        "address_text": (payload.get("display_name") or "")[:1000] or None,
        "district": normalize_district(str(district)) if district else None,
        "mahalla": str(mahalla)[:200] if mahalla else None,
    }


async def resolve_map_url(url: str) -> tuple[float | None, float | None, str]:
    _, _, original = parse_location_url(url)
    provider = map_url_provider(original)
    if provider is None:
        raise ValueError(
            "Поддерживаются Google, Яндекс, 2GIS, Apple Maps, OpenStreetMap и Waze"
        )
    latitude, longitude, _ = parse_location_url(original)
    if latitude is not None:
        return latitude, longitude, original

    now = time.monotonic()
    cached = _map_url_cache.get(original)
    if cached and cached[3] > now:
        return cached[0], cached[1], cached[2]

    deadline = now + _MAP_RESOLUTION_DEADLINE_SECONDS
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(6.0, connect=4.0),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                    "Chrome/124.0 Mobile Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru,en;q=0.8",
            },
        ) as client:
            resolved = original
            visited: set[str] = set()
            for _ in range(_MAX_MAP_REDIRECTS):
                if time.monotonic() >= deadline:
                    raise ValueError("Map resolution deadline exceeded")
                if map_url_provider(resolved) != provider:
                    raise ValueError("Map redirect host is not allowed")
                if resolved in visited:
                    raise ValueError("Map redirect loop")
                visited.add(resolved)

                latitude, longitude, _ = parse_location_url(resolved)
                if latitude is not None:
                    _cache_map_resolution(
                        original,
                        latitude,
                        longitude,
                        resolved,
                        _MAP_CACHE_TTL_SECONDS,
                    )
                    return latitude, longitude, resolved

                async with client.stream("GET", resolved) as response:
                    if response.is_redirect:
                        target = response.headers.get("location")
                        if not target:
                            raise ValueError("Map redirect has no target")
                        resolved = urljoin(resolved, target)
                        continue
                    response.raise_for_status()
                    response_url = str(response.url)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        if size >= _MAX_MAP_RESPONSE_BYTES:
                            break
                        remaining = _MAX_MAP_RESPONSE_BYTES - size
                        chunks.append(chunk[:remaining])
                        size += min(len(chunk), remaining)

                if map_url_provider(response_url) != provider:
                    raise ValueError("Map response host is not allowed")
                latitude, longitude, _ = parse_location_url(response_url)
                if latitude is not None:
                    resolved = response_url
                    break

                declared = _html_map_urls(b"".join(chunks), response_url)
                found: tuple[float, float, str] | None = None
                for candidate in declared:
                    if map_url_provider(candidate) != provider:
                        continue
                    candidate_lat, candidate_lon, _ = parse_location_url(candidate)
                    if candidate_lat is not None:
                        found = candidate_lat, candidate_lon, candidate
                        break
                if found is not None:
                    latitude, longitude, resolved = found
                    break

                next_url = next(
                    (
                        candidate
                        for candidate in declared
                        if map_url_provider(candidate) == provider and candidate not in visited
                    ),
                    None,
                )
                if next_url is None:
                    resolved = response_url
                    break
                resolved = next_url
            else:
                raise ValueError("Too many map redirects")
        if map_url_provider(resolved) != provider:
            raise ValueError("Map redirect host is not allowed")
        if latitude is None:
            latitude, longitude, _ = parse_location_url(resolved)
        _cache_map_resolution(
            original,
            latitude,
            longitude,
            resolved,
            _MAP_CACHE_TTL_SECONDS if latitude is not None else 300,
        )
        return latitude, longitude, resolved
    except (httpx.HTTPError, ValueError) as error:
        logger.warning(
            "Could not resolve %s map URL: %s (%s)",
            provider,
            type(error).__name__,
            str(error)[:120],
        )
        return None, None, original


async def reverse_geocode(latitude: float, longitude: float) -> dict[str, str | None]:
    global _last_geocode_request
    cache_key = (round(latitude, 5), round(longitude, 5))
    if cache_key in _geocode_cache:
        return dict(_geocode_cache[cache_key])
    try:
        async with _geocode_lock:
            delay = 1.0 - (time.monotonic() - _last_geocode_request)
            if delay > 0:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient(
                timeout=8,
                headers={"User-Agent": "TEXNIKACH-Delivery-Bot/1.0 (texnikach.uz)"},
            ) as client:
                response = await client.get(
                    "https://nominatim.openstreetmap.org/reverse",
                    params={
                        "lat": latitude,
                        "lon": longitude,
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "accept-language": "ru,uz",
                        "zoom": 18,
                    },
                )
                _last_geocode_request = time.monotonic()
                response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Unexpected geocoder response")
        result = extract_address(payload)
        if len(_geocode_cache) >= 256:
            _geocode_cache.pop(next(iter(_geocode_cache)))
        _geocode_cache[cache_key] = result
        return dict(result)
    except (httpx.HTTPError, ValueError, TypeError) as error:
        logger.warning("Could not reverse geocode location: %s", type(error).__name__)
        return {"address_text": None, "district": None, "mahalla": None}


async def enrich_location(
    latitude: float | None,
    longitude: float | None,
    url: str,
) -> dict[str, Any]:
    if latitude is None or longitude is None:
        latitude, longitude, url = await resolve_map_url(url)
    result: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "location_url": url,
        "address_text": None,
        "district": None,
        "mahalla": None,
    }
    if latitude is not None and longitude is not None:
        result.update(await reverse_geocode(latitude, longitude))
    return result
