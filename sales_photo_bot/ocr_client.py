from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import ProductIdentifiers
from .phones import normalize_uzbek_phone


logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024
MAX_HEALTH_BYTES = 4 * 1024
MAX_PRODUCT_INFO = 160
MAX_PRODUCT_MODEL = 80
MAX_SERIAL_NUMBER = 40
_RESPONSE_FIELDS = frozenset(
    {
        "product_info",
        "product_model",
        "imei",
        "imei2",
        "serial_number",
        "phone_numbers",
    }
)
_IMEI_RE = re.compile(r"^[0-9]{15}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{2,39}$")
_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class OCRResponseError(ValueError):
    """The OCR service returned data outside its small, trusted contract."""


def validate_ocr_base_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OCR URL содержит неверный host или port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or (port is not None and not 1 <= port <= 65535)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in raw
        or "#" in raw
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("OCR URL должен быть обычным http(s) base URL")
    return raw


def _reject_constant(value: str) -> None:
    raise OCRResponseError(f"Недопустимое JSON-значение: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OCRResponseError("OCR JSON содержит повторяющиеся ключи")
        result[key] = value
    return result


def _text_field(payload: dict[str, Any], name: str, limit: int) -> str | None:
    value = payload[name]
    if value is None:
        return None
    if type(value) is not str:
        raise OCRResponseError(f"OCR поле {name} должно быть строкой или null")
    if any(
        unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise OCRResponseError(f"OCR поле {name} содержит управляющий символ")
    compact = " ".join(value.split())
    if not compact or len(compact) > limit:
        raise OCRResponseError(f"OCR поле {name} имеет неверную длину")
    return compact


def _valid_imei(value: str) -> bool:
    if _IMEI_RE.fullmatch(value) is None:
        return False
    total = 0
    for index, character in enumerate(reversed(value)):
        digit = int(character)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def parse_ocr_response(data: bytes) -> ProductIdentifiers:
    if not data or len(data) > MAX_RESPONSE_BYTES:
        raise OCRResponseError("OCR JSON пустой или слишком большой")
    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OCRResponseError("OCR вернул некорректный JSON") from exc
    if type(payload) is not dict or set(payload) != _RESPONSE_FIELDS:
        raise OCRResponseError("OCR JSON не соответствует ожидаемой схеме")

    product_info = _text_field(payload, "product_info", MAX_PRODUCT_INFO)
    product_model = _text_field(payload, "product_model", MAX_PRODUCT_MODEL)
    imei = _text_field(payload, "imei", 15)
    imei2 = _text_field(payload, "imei2", 15)
    serial_number = _text_field(payload, "serial_number", MAX_SERIAL_NUMBER)
    if imei is not None and not _valid_imei(imei):
        raise OCRResponseError("OCR поле imei имеет неверный формат")
    if imei2 is not None and not _valid_imei(imei2):
        raise OCRResponseError("OCR поле imei2 имеет неверный формат")
    if (
        serial_number is not None
        and _SERIAL_RE.fullmatch(serial_number) is None
    ):
        raise OCRResponseError("OCR поле serial_number имеет неверный формат")

    raw_phones = payload["phone_numbers"]
    if type(raw_phones) is not list or len(raw_phones) > 2:
        raise OCRResponseError("OCR поле phone_numbers должно содержать до 2 номеров")
    phones: list[str] = []
    for value in raw_phones:
        if type(value) is not str:
            raise OCRResponseError("OCR номер телефона должен быть строкой")
        normalized = normalize_uzbek_phone(value)
        if normalized is None or normalized != value.strip():
            raise OCRResponseError("OCR номер телефона не нормализован")
        if normalized not in phones:
            phones.append(normalized)

    return ProductIdentifiers(
        imei=imei,
        imei2=imei2,
        serial_number=serial_number,
        product_info=product_info,
        product_model=product_model,
        phone_numbers=tuple(phones),
    )


class RemoteOCRRecognizer:
    """Small async client for the separately hosted PaddleOCR service."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 45.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = validate_ocr_base_url(base_url)
        timeout = float(timeout_seconds)
        if not 0.5 <= timeout <= 60.0:
            raise ValueError("OCR timeout должен быть от 0.5 до 60 секунд")
        self._request_timeout_seconds = timeout
        short_timeout = min(5.0, timeout)
        self._client = httpx.AsyncClient(
            base_url=self.base_url + "/",
            timeout=httpx.Timeout(
                connect=short_timeout,
                pool=short_timeout,
                read=timeout,
                write=min(10.0, timeout),
            ),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    @staticmethod
    async def _read_bounded(
        response: httpx.Response,
        limit: int,
    ) -> bytes:
        raw_length = response.headers.get("content-length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise OCRResponseError("OCR вернул неверный Content-Length") from exc
            if content_length < 0:
                raise OCRResponseError("OCR вернул неверный Content-Length")
            if content_length > limit:
                raise OCRResponseError("Ответ OCR слишком большой")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise OCRResponseError("Ответ OCR слишком большой")
            chunks.append(chunk)
        return b"".join(chunks)

    async def recognize(
        self,
        image_bytes: bytes,
        mime_type: str,
    ) -> ProductIdentifiers:
        if not isinstance(image_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("image_bytes должен содержать байты")
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise ValueError("Изображение пустое или слишком большое")
        media_type = str(mime_type or "").strip().casefold()
        if media_type not in _MIME_TYPES:
            raise ValueError("Неподдерживаемый формат изображения")

        async with asyncio.timeout(self._request_timeout_seconds):
            async with self._client.stream(
                "POST",
                "v1/recognize",
                content=bytes(image_bytes),
                headers={"content-type": media_type, "accept": "application/json"},
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if not content_type.startswith("application/json"):
                    raise OCRResponseError("OCR вернул не JSON")
                data = await self._read_bounded(response, MAX_RESPONSE_BYTES)
        return parse_ocr_response(data)

    async def healthcheck(self) -> bool:
        async with asyncio.timeout(min(5.0, self._request_timeout_seconds)):
            async with self._client.stream(
                "GET",
                "health",
                headers={"accept": "application/json"},
            ) as response:
                response.raise_for_status()
                await self._read_bounded(response, MAX_HEALTH_BYTES)
        return True

    async def preflight(self) -> bool:
        """Probe OCR without preventing the Telegram bot from starting."""

        try:
            return await self.healthcheck()
        except Exception as exc:
            logger.warning(
                "sales_photo_ocr_preflight_failed error_type=%s",
                type(exc).__name__[:80],
            )
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
