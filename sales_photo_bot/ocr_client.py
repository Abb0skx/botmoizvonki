from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import EMPTY_IDENTIFIERS, ProductIdentifiers
from .phones import normalize_uzbek_phone


logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024
MAX_HEALTH_BYTES = 4 * 1024
MAX_PRODUCT_INFO = 160
MAX_PRODUCT_MODEL = 80
MAX_SERIAL_NUMBER = 40
MAX_PRODUCT_NAMES = 8
MAX_IMEIS = 16
MAX_SERIAL_NUMBERS = 16
_RESPONSE_V1_FIELDS = frozenset(
    {
        "product_info",
        "product_model",
        "imei",
        "imei2",
        "serial_number",
        "phone_numbers",
    }
)
_RESPONSE_V2_FIELDS = _RESPONSE_V1_FIELDS | frozenset(
    {
        "product_names",
        "imeis",
        "serial_numbers",
        "warranty_card_detected",
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


def _text_list(
    payload: dict[str, Any],
    name: str,
    *,
    count_limit: int,
    item_limit: int,
    product_keys: bool = False,
) -> tuple[str, ...]:
    raw_values = payload[name]
    if type(raw_values) is not list or len(raw_values) > count_limit:
        raise OCRResponseError(
            f"OCR поле {name} должно быть списком "
            f"до {count_limit} значений"
        )
    values: list[str] = []
    keys: set[str] = set()
    for raw_value in raw_values:
        item = _text_field({name: raw_value}, name, item_limit)
        if item is None:
            raise OCRResponseError(f"OCR поле {name} содержит null")
        key = (
            "".join(character for character in item.casefold() if character.isalnum())
            if product_keys
            else item.casefold()
        )
        if key not in keys:
            keys.add(key)
            values.append(item)
    return tuple(values)


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
    if type(payload) is not dict:
        raise OCRResponseError("OCR JSON не соответствует ожидаемой схеме")
    fields = frozenset(payload)
    if fields not in {_RESPONSE_V1_FIELDS, _RESPONSE_V2_FIELDS}:
        raise OCRResponseError("OCR JSON не соответствует ожидаемой схеме")
    is_v2 = fields == _RESPONSE_V2_FIELDS

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

    # The original response shape cannot prove either side of the new trust
    # boundary: phones must come only from a confirmed warranty-card region,
    # while product fields and identifiers must come only from the box. Keep
    # accepting and validating v1 during a rolling deployment, but publish no
    # values from it.
    if not is_v2:
        return EMPTY_IDENTIFIERS

    product_names = _text_list(
        payload,
        "product_names",
        count_limit=MAX_PRODUCT_NAMES,
        item_limit=MAX_PRODUCT_INFO,
        product_keys=True,
    )
    imeis = _text_list(
        payload,
        "imeis",
        count_limit=MAX_IMEIS,
        item_limit=15,
    )
    if any(not _valid_imei(value) for value in imeis):
        raise OCRResponseError("OCR поле imeis содержит неверный IMEI")
    serial_numbers = _text_list(
        payload,
        "serial_numbers",
        count_limit=MAX_SERIAL_NUMBERS,
        item_limit=MAX_SERIAL_NUMBER,
    )
    if any(_SERIAL_RE.fullmatch(value) is None for value in serial_numbers):
        raise OCRResponseError(
            "OCR поле serial_numbers содержит неверный серийный номер"
        )
    raw_warranty = payload["warranty_card_detected"]
    if type(raw_warranty) is not bool:
        raise OCRResponseError(
            "OCR поле warranty_card_detected должно быть boolean"
        )
    warranty_card_detected = raw_warranty
    if phones and not warranty_card_detected:
        raise OCRResponseError(
            "OCR номера допустимы только при обнаруженной гарантийной карточке"
        )

    # The v2 service keeps the original scalar keys for old clients. Make
    # their mirror relationship explicit so malformed or mixed responses
    # cannot silently change meaning during a rolling deployment.
    if imei != (imeis[0] if imeis else None):
        raise OCRResponseError("OCR поля imei и imeis не согласованы")
    if imei2 != (imeis[1] if len(imeis) > 1 else None):
        raise OCRResponseError("OCR поля imei2 и imeis не согласованы")
    expected_serial = serial_numbers[0] if len(serial_numbers) == 1 else None
    if serial_number != expected_serial:
        raise OCRResponseError(
            "OCR поля serial_number и serial_numbers не согласованы"
        )
    expected_info = product_names[0] if len(product_names) == 1 else None
    if product_info != expected_info:
        raise OCRResponseError(
            "OCR поля product_info и product_names не согласованы"
        )

    return ProductIdentifiers(
        imei=imei,
        imei2=imei2,
        serial_number=serial_number,
        product_info=product_info,
        product_model=product_model,
        phone_numbers=tuple(phones),
        product_names=product_names,
        imeis=imeis,
        serial_numbers=serial_numbers,
        warranty_card_detected=warranty_card_detected,
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
