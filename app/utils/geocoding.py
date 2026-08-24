import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .parsers import parse_location_url

logger = logging.getLogger(__name__)
_geocode_lock = asyncio.Lock()
_last_geocode_request = 0.0
_geocode_cache: dict[tuple[float, float], dict[str, str | None]] = {}

ALLOWED_MAP_HOSTS = {
    "maps.app.goo.gl",
    "maps.google.com",
    "www.google.com",
    "google.com",
    "yandex.com",
    "yandex.ru",
    "yandex.uz",
    "maps.yandex.com",
    "maps.yandex.ru",
    "maps.yandex.uz",
}

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
    host = (urlsplit(url).hostname or "").lower()
    return host in ALLOWED_MAP_HOSTS or host.endswith(".maps.google.com")


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
    if not _allowed_map_url(original):
        raise ValueError("Поддерживаются ссылки Яндекс или Google карт")
    latitude, longitude, _ = parse_location_url(original)
    if latitude is not None:
        return latitude, longitude, original
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=8,
            headers={"User-Agent": "TEXNIKACH-Delivery-Bot/1.0"},
        ) as client:
            resolved = original
            for _ in range(6):
                if not _allowed_map_url(resolved):
                    raise ValueError("Map redirect host is not allowed")
                response = await client.get(resolved)
                if response.is_redirect:
                    target = response.headers.get("location")
                    if not target:
                        raise ValueError("Map redirect has no target")
                    resolved = urljoin(resolved, target)
                    continue
                response.raise_for_status()
                resolved = str(response.url)
                break
            else:
                raise ValueError("Too many map redirects")
        if not _allowed_map_url(resolved):
            raise ValueError("Map redirect host is not allowed")
        latitude, longitude, _ = parse_location_url(resolved)
        return latitude, longitude, resolved
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Could not resolve map URL: %s", type(error).__name__)
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
