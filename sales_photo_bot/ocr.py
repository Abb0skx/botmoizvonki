from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence

import pytesseract
import zxingcpp
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
from pytesseract import Output

from .models import EMPTY_IDENTIFIERS, ProductIdentifiers


logger = logging.getLogger(__name__)


class IdentifierRecognizer(Protocol):
    async def recognize(
        self, image_bytes: bytes, mime_type: str
    ) -> ProductIdentifiers: ...


_OCR_DIGIT_MAP = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "Z": "2",
        "S": "5",
        "G": "6",
        "B": "8",
    }
)
_DIGITLIKE = "0-9OQDISBZGL"
_IMEI_CANDIDATE_RE = re.compile(
    rf"(?<![A-Z0-9])((?:[{_DIGITLIKE}][\s.:-]*){{15}})(?![A-Z0-9])",
    re.IGNORECASE,
)
_IMEI_LABEL_RE = re.compile(
    r"(?<![A-Z0-9._/-])(?:IMEI|IME1|1MEI)\s*(?P<slot>[12])?"
    r"(?![A-Z0-9._/-])",
    re.IGNORECASE,
)
_SERIAL_LABEL_RE = re.compile(
    r"(?<![A-Z0-9._/-])(?:S\s*[/\\]\s*N|S\.?\s*N\.?|"
    r"SERIAL(?:\s*(?:NO\.?|NUMBER|NUM\.?|#))?)(?![A-Z0-9._/-])",
    re.IGNORECASE,
)
_SERIAL_TOKEN_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9][A-Z0-9._/-]{4,39})(?![A-Z0-9])",
    re.IGNORECASE,
)
_SERIAL_FULL_RE = re.compile(r"[A-Z0-9][A-Z0-9._/-]{4,39}", re.IGNORECASE)
_SERIAL_IGNORE = {
    "BARCODE",
    "CHINA",
    "COLOR",
    "IMEI",
    "MADE",
    "MODEL",
    "NUMBER",
    "PRODUCT",
    "SAMSUNG",
    "SERIAL",
    "UNKNOWN",
}


def valid_imei(value: object) -> bool:
    """Validate the 15-digit IMEI check digit (Luhn algorithm)."""

    digits = str(value or "")
    if (
        len(digits) != 15
        or not digits.isdecimal()
        or not digits.isascii()
        or len(set(digits)) == 1
    ):
        return False
    total = 0
    for index, char in enumerate(digits[:-1]):
        number = int(char)
        if index % 2:
            number *= 2
            number = number // 10 + number % 10
        total += number
    check_digit = (10 - total % 10) % 10
    return check_digit == int(digits[-1])


def _imei_from_candidate(value: object) -> str | None:
    raw = str(value or "").upper()
    ambiguous_count = sum(ord(char) in _OCR_DIGIT_MAP for char in raw)
    if ambiguous_count > 2:
        return None
    translated = raw.translate(_OCR_DIGIT_MAP)
    digits = "".join(char for char in translated if char.isascii() and char.isdigit())
    if valid_imei(digits):
        return digits
    return None


def _imeis_in(value: object) -> tuple[str, ...]:
    found: list[str] = []
    for match in _IMEI_CANDIDATE_RE.finditer(str(value or "")):
        imei = _imei_from_candidate(match.group(1))
        if imei and imei not in found:
            found.append(imei)
    return tuple(found)


def _serial_candidates(value: object) -> tuple[str, ...]:
    found: list[str] = []
    raw = str(value or "").upper()
    for match in _SERIAL_TOKEN_RE.finditer(raw):
        serial = match.group(1).strip("._/-")
        if (
            not 5 <= len(serial) <= 40
            or serial in _SERIAL_IGNORE
            or not any(char.isascii() and char.isdigit() for char in serial)
            or _IMEI_LABEL_RE.search(serial)
            or _SERIAL_LABEL_RE.fullmatch(serial)
            or (serial.isdecimal() and valid_imei(serial))
        ):
            continue
        if serial not in found:
            found.append(serial)
    return tuple(found)


def _barcode_serial(value: object) -> str | None:
    serial = str(value or "").strip().upper().strip("._/-")
    if not _SERIAL_FULL_RE.fullmatch(serial):
        return None
    if serial in _SERIAL_IGNORE or not any(char.isdigit() for char in serial):
        return None
    if serial.isdecimal() and (valid_imei(serial) or len(serial) in {8, 12, 13, 14}):
        return None
    return serial


def _comparison_key(value: str) -> str:
    compact = "".join(char for char in value.upper() if char.isalnum())
    return compact.translate(_OCR_DIGIT_MAP)


@dataclass(frozen=True)
class _LabelMatch:
    start: int
    end: int
    field_name: str


def _label_matches(value: str) -> tuple[_LabelMatch, ...]:
    labels: list[_LabelMatch] = []
    for match in _IMEI_LABEL_RE.finditer(value):
        labels.append(
            _LabelMatch(
                match.start(),
                match.end(),
                "imei2" if match.group("slot") == "2" else "imei",
            )
        )
    for match in _SERIAL_LABEL_RE.finditer(value):
        labels.append(_LabelMatch(match.start(), match.end(), "serial_number"))
    labels.sort(key=lambda item: (item.start, item.end))
    return tuple(labels)


def _label_segments(value: str) -> tuple[tuple[_LabelMatch, str], ...]:
    labels = _label_matches(value)
    result: list[tuple[_LabelMatch, str]] = []
    for index, label in enumerate(labels):
        segment_end = labels[index + 1].start if index + 1 < len(labels) else len(value)
        result.append((label, value[label.end : segment_end]))
    return tuple(result)


def _unique_winner(counter: Counter[str], minimum: int = 1) -> str | None:
    if not counter:
        return None
    highest = max(counter.values())
    winners = sorted(value for value, score in counter.items() if score == highest)
    if highest < minimum or len(winners) != 1:
        return None
    return winners[0]


def extract_identifiers(texts: Sequence[str]) -> ProductIdentifiers:
    """Conservatively parse same-line labelled OCR text.

    This helper is also useful for tests and diagnostics. Runtime recognition uses
    bounding boxes below, so repeated preprocessing of one physical label never
    counts as multiple independent serial-number labels.
    """

    imei1: Counter[str] = Counter()
    imei2: Counter[str] = Counter()
    serials: Counter[str] = Counter()
    for text in texts:
        for line in str(text or "").splitlines():
            for label, segment in _label_segments(line):
                if label.field_name == "serial_number":
                    candidates = _serial_candidates(segment)
                    if candidates:
                        serials[candidates[0]] += 1
                else:
                    candidates = _imeis_in(segment)
                    if candidates:
                        target = imei2 if label.field_name == "imei2" else imei1
                        target[candidates[0]] += 1

    first = _unique_winner(imei1)
    second = _unique_winner(imei2)
    if first and second and first == second:
        first = None
        second = None
    return ProductIdentifiers(
        imei=first,
        imei2=second,
        serial_number=_unique_winner(serials, minimum=2),
    )


@dataclass(frozen=True)
class _Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


def _union_boxes(boxes: Iterable[_Box]) -> _Box:
    values = tuple(boxes)
    return _Box(
        min(item.left for item in values),
        min(item.top for item in values),
        max(item.right for item in values),
        max(item.bottom for item in values),
    )


def _same_physical_label(first: _Box, second: _Box) -> bool:
    scale = max(first.height, second.height, 12)
    return (
        abs(first.center_x - second.center_x) <= 1.5 * scale
        and abs(first.center_y - second.center_y) <= 1.2 * scale
    )


def _association_distance(label: _Box, barcode: _Box) -> float | None:
    horizontal_gap = max(0, label.left - barcode.right, barcode.left - label.right)
    vertical_gap = max(0, label.top - barcode.bottom, barcode.top - label.bottom)
    scale = max(label.height, 16)
    if horizontal_gap > 8 * scale or vertical_gap > 8 * scale:
        return None
    return (
        horizontal_gap / scale
        + vertical_gap / scale
        + abs(label.center_y - barcode.center_y) / (10 * scale)
    )


@dataclass
class _AnchorEvidence:
    field_name: str
    box: _Box
    candidates: set[str] = field(default_factory=set)
    barcode_values: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _BarcodeEvidence:
    text: str
    box: _Box


@dataclass(frozen=True)
class _DataLine:
    text: str
    spans: tuple[tuple[int, int, _Box], ...]


def _lines_from_tesseract(data: dict[str, list[object]]) -> tuple[_DataLine, ...]:
    groups: dict[tuple[object, ...], list[tuple[int, str, _Box]]] = defaultdict(list)
    texts = data.get("text", [])
    for index, raw_text in enumerate(texts):
        word = str(raw_text or "").strip()
        if not word:
            continue
        try:
            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            key = (
                data["page_num"][index],
                data["block_num"][index],
                data["par_num"][index],
                data["line_num"][index],
            )
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        groups[key].append(
            (index, word, _Box(left, top, left + width, top + height))
        )

    lines: list[_DataLine] = []
    for words in groups.values():
        words.sort(key=lambda item: (item[2].left, item[0]))
        rendered = ""
        spans: list[tuple[int, int, _Box]] = []
        for _, word, box in words:
            if rendered:
                rendered += " "
            start = len(rendered)
            rendered += word
            spans.append((start, len(rendered), box))
        lines.append(_DataLine(rendered, tuple(spans)))
    return tuple(lines)


def _box_for_match(line: _DataLine, label: _LabelMatch) -> _Box | None:
    boxes = [
        box
        for start, end, box in line.spans
        if start < label.end and end > label.start
    ]
    return _union_boxes(boxes) if boxes else None


def _add_anchor(
    anchors: list[_AnchorEvidence],
    field_name: str,
    box: _Box,
    candidates: Iterable[str],
) -> None:
    anchor = next(
        (
            item
            for item in anchors
            if item.field_name == field_name and _same_physical_label(item.box, box)
        ),
        None,
    )
    if anchor is None:
        anchor = _AnchorEvidence(field_name, box)
        anchors.append(anchor)
    anchor.candidates.update(candidates)


def _result_from_anchors(anchors: Sequence[_AnchorEvidence]) -> ProductIdentifiers:
    imei_values: dict[str, set[str]] = {"imei": set(), "imei2": set()}
    serial_counts: Counter[str] = Counter()
    serial_barcode_counts: Counter[str] = Counter()
    for anchor in anchors:
        if anchor.field_name in imei_values:
            imei_values[anchor.field_name].update(anchor.candidates)
            imei_values[anchor.field_name].update(anchor.barcode_values)
            continue
        for value in anchor.candidates:
            serial_counts[value] += 1
        for value in anchor.barcode_values:
            serial_barcode_counts[value] += 1

    first = next(iter(imei_values["imei"])) if len(imei_values["imei"]) == 1 else None
    second = (
        next(iter(imei_values["imei2"]))
        if len(imei_values["imei2"]) == 1
        else None
    )
    if first and second and first == second:
        first = None
        second = None

    serial: str | None = None
    serial_barcodes = set(serial_barcode_counts)
    if len(serial_barcodes) == 1:
        barcode_value = next(iter(serial_barcodes))
        supported_ocr = {
            value for value, count in serial_counts.items() if count >= 2
        }
        has_matching_ocr = any(
            _comparison_key(value) == _comparison_key(barcode_value)
            for value in serial_counts
        )
        if (
            all(
                _comparison_key(value) == _comparison_key(barcode_value)
                for value in supported_ocr
            )
            and (
                has_matching_ocr
                or serial_barcode_counts[barcode_value] >= 2
            )
        ):
            serial = barcode_value
    elif not serial_barcodes:
        supported = {
            value
            for value, count in serial_counts.items()
            if count >= 2 and not value.isdecimal()
        }
        if len(supported) == 1:
            serial = next(iter(supported))

    return ProductIdentifiers(first, second, serial)


def _merge_results(results: Sequence[ProductIdentifiers]) -> ProductIdentifiers:
    def only(field_name: str) -> str | None:
        values = {
            value
            for result in results
            if (value := getattr(result, field_name)) is not None
        }
        return next(iter(values)) if len(values) == 1 else None

    first = only("imei")
    second = only("imei2")
    if first and second and first == second:
        first = None
        second = None
    return ProductIdentifiers(first, second, only("serial_number"))


class TesseractIdentifierRecognizer:
    """Bounded local OCR and barcode reader for IMEI/IMEI2/S/N labels."""

    def __init__(
        self,
        timeout_seconds: int = 30,
        max_pixels: int = 32_000_000,
        max_parallel: int = 1,
    ):
        self.timeout_seconds = int(timeout_seconds)
        self.max_pixels = int(max_pixels)
        self._gate = asyncio.Semaphore(max(1, int(max_parallel)))

    async def preflight(self) -> None:
        try:
            languages = await asyncio.wait_for(
                asyncio.to_thread(pytesseract.get_languages, config=""),
                timeout=10,
            )
        except Exception as exc:
            raise RuntimeError("Tesseract OCR недоступен") from exc
        if "eng" not in languages:
            raise RuntimeError("Для Tesseract не установлен язык eng")

    @staticmethod
    def _fit(image: Image.Image) -> Image.Image:
        longest = max(image.size)
        # Full 12 MP Telegram photos can make a single Tesseract pass exceed
        # its deadline on the one-CPU VPS. Labels remain comfortably readable
        # at this bound, while the pixel count (and OCR cost) drops sharply.
        if longest < 1200:
            scale = min(2.5, 1600 / max(1, longest))
            image = image.resize(
                (
                    max(1, round(image.width * scale)),
                    max(1, round(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        elif longest > 1800:
            image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        return image

    @staticmethod
    def _fit_barcode_image(image: Image.Image) -> Image.Image:
        # Preserve the narrow bars present in typical 2.5K phone photos. The
        # full-frame Tesseract path gets a smaller copy via ``_fit`` below,
        # while ZXing and its small OCR crops use this barcode-safe bound.
        if max(image.size) > 3200:
            image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
        return image

    @staticmethod
    def _barcode_draft_size(width: int, height: int) -> tuple[int, int]:
        # Pillow's JPEG ``draft`` only offers 1/2, 1/4 and 1/8 reductions.
        # Asking for an exact 3200-pixel aspect-ratio box can still select the
        # full decode when half-size rounds just below the request. Force the
        # first native reduction once pixel pressure becomes material.
        if width * height > 16_000_000:
            return max(1, width // 2), max(1, height // 2)
        return width, height

    @staticmethod
    def _enhanced(image: Image.Image) -> Image.Image:
        gray = ImageOps.grayscale(image)
        enhanced = ImageOps.autocontrast(gray, cutoff=1)
        enhanced = ImageEnhance.Contrast(enhanced).enhance(1.35)
        return ImageEnhance.Sharpness(enhanced).enhance(1.8)

    @staticmethod
    def _threshold(enhanced: Image.Image) -> Image.Image:
        return enhanced.filter(ImageFilter.MedianFilter(3)).point(
            lambda value: 255 if value >= 165 else 0
        )

    @classmethod
    def _variants(cls, image: Image.Image) -> tuple[Image.Image, Image.Image]:
        enhanced = cls._enhanced(image)
        return enhanced, cls._threshold(enhanced)

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _ocr_data(
        self,
        image: Image.Image,
        deadline: float,
        max_seconds: float = 10.0,
    ) -> dict[str, list[object]]:
        remaining = self._remaining(deadline)
        if remaining < 0.5:
            return {}
        try:
            return dict(
                pytesseract.image_to_data(
                    image,
                    lang="eng",
                    config="--oem 1 --psm 11",
                    output_type=Output.DICT,
                    timeout=max(0.5, min(max_seconds, remaining)),
                )
                or {}
            )
        except RuntimeError:
            logger.warning("sales_photo_ocr_pass_timeout")
            return {}
        except Exception as exc:
            logger.warning(
                "sales_photo_ocr_pass_failed error_type=%s", type(exc).__name__
            )
            return {}

    def _ocr_line(self, image: Image.Image, deadline: float) -> str:
        remaining = self._remaining(deadline)
        if remaining < 0.5:
            return ""
        try:
            return str(
                pytesseract.image_to_string(
                    image,
                    lang="eng",
                    config="--oem 1 --psm 7",
                    timeout=max(0.5, min(5.0, remaining)),
                )
                or ""
            )
        except RuntimeError:
            logger.warning("sales_photo_ocr_roi_timeout")
            return ""
        except Exception as exc:
            logger.warning(
                "sales_photo_ocr_roi_failed error_type=%s", type(exc).__name__
            )
            return ""

    @staticmethod
    def _decode_barcodes(image: Image.Image) -> tuple[_BarcodeEvidence, ...]:
        try:
            decoded = zxingcpp.read_barcodes(
                image,
                try_rotate=True,
                try_downscale=True,
                try_invert=True,
            )
        except Exception as exc:
            logger.warning(
                "sales_photo_barcode_failed error_type=%s", type(exc).__name__
            )
            return ()
        result: list[_BarcodeEvidence] = []
        for barcode in decoded[:24]:
            text = str(getattr(barcode, "text", "") or "").strip()
            position = getattr(barcode, "position", None)
            if not text or position is None:
                continue
            try:
                points = (
                    position.top_left,
                    position.top_right,
                    position.bottom_left,
                    position.bottom_right,
                )
                box = _Box(
                    min(int(point.x) for point in points),
                    min(int(point.y) for point in points),
                    max(int(point.x) for point in points),
                    max(int(point.y) for point in points),
                )
            except (AttributeError, TypeError, ValueError):
                continue
            result.append(_BarcodeEvidence(text, box))
        return tuple(result)

    @staticmethod
    def _candidates(field_name: str, value: str) -> tuple[str, ...]:
        if field_name == "serial_number":
            return _serial_candidates(value)
        return _imeis_in(value)

    def _append_data_anchors(
        self,
        anchors: list[_AnchorEvidence],
        data: dict[str, list[object]],
        offset_x: int = 0,
        offset_y: int = 0,
    ) -> None:
        for line in _lines_from_tesseract(data):
            for label, segment in _label_segments(line.text):
                box = _box_for_match(line, label)
                if box is None:
                    continue
                translated = _Box(
                    box.left + offset_x,
                    box.top + offset_y,
                    box.right + offset_x,
                    box.bottom + offset_y,
                )
                _add_anchor(
                    anchors,
                    label.field_name,
                    translated,
                    self._candidates(label.field_name, segment),
                )

    def _collect_anchors(
        self,
        image: Image.Image,
        deadline: float,
    ) -> tuple[list[_AnchorEvidence], Image.Image, Image.Image]:
        enhanced, threshold = self._variants(image)
        anchors: list[_AnchorEvidence] = []
        for variant in (enhanced, threshold):
            data = self._ocr_data(variant, deadline)
            self._append_data_anchors(anchors, data)
            if self._remaining(deadline) < 0.5:
                break

        # Only use a tight strip immediately right of an already located label.
        # This recovers faint values without ever attaching the next arbitrary
        # page line to S/N or IMEI.
        for anchor in anchors[:12]:
            if anchor.candidates or self._remaining(deadline) < 0.5:
                continue
            height = max(anchor.box.height, 16)
            crop_box = (
                max(0, anchor.box.right - height // 5),
                max(0, anchor.box.top - height),
                min(image.width, anchor.box.right + 18 * height),
                min(image.height, anchor.box.bottom + height),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                continue
            for variant in (enhanced, threshold):
                text = self._ocr_line(variant.crop(crop_box), deadline)
                anchor.candidates.update(self._candidates(anchor.field_name, text))
                if self._remaining(deadline) < 0.5:
                    break
        return anchors, enhanced, threshold

    @staticmethod
    def _barcode_crop_box(
        barcode: _BarcodeEvidence,
        image: Image.Image,
    ) -> tuple[int, int, int, int]:
        horizontal_pad = max(60, min(500, barcode.box.width // 2))
        vertical_pad = max(
            90,
            min(500, max(barcode.box.height * 4, barcode.box.width // 3)),
        )
        return (
            max(0, barcode.box.left - horizontal_pad),
            max(0, barcode.box.top - vertical_pad),
            min(image.width, barcode.box.right + horizontal_pad),
            min(image.height, barcode.box.bottom + vertical_pad),
        )

    @staticmethod
    def _merge_crop_boxes(
        boxes: Sequence[tuple[int, int, int, int]],
    ) -> tuple[tuple[int, int, int, int], ...]:
        merged: list[tuple[int, int, int, int]] = []
        for candidate in boxes:
            current = candidate
            index = 0
            while index < len(merged):
                existing = merged[index]
                overlaps = not (
                    current[2] <= existing[0]
                    or existing[2] <= current[0]
                    or current[3] <= existing[1]
                    or existing[3] <= current[1]
                )
                if not overlaps:
                    index += 1
                    continue
                current = (
                    min(current[0], existing[0]),
                    min(current[1], existing[1]),
                    max(current[2], existing[2]),
                    max(current[3], existing[3]),
                )
                merged.pop(index)
                index = 0
            merged.append(current)
        return tuple(merged)

    def _scan_barcode_regions(
        self,
        image: Image.Image,
        deadline: float,
        ocr_budget: list[int] | None = None,
        max_ocr_calls: int | None = None,
    ) -> ProductIdentifiers:
        """Read small label regions before expensive full-frame OCR."""

        budget = ocr_budget if ocr_budget is not None else [6]
        if budget[0] <= 0 or self._remaining(deadline) < 0.5:
            return EMPTY_IDENTIFIERS
        enhanced = self._enhanced(image)
        barcodes = self._decode_barcodes(enhanced)
        barcode_counts = Counter(barcode.text for barcode in barcodes)
        relevant_barcodes = tuple(
            sorted(
                (
                    barcode
                    for barcode in barcodes
                    if valid_imei(barcode.text)
                    or _barcode_serial(barcode.text) is not None
                ),
                key=lambda barcode: (
                    0 if valid_imei(barcode.text) else 1,
                    -barcode_counts[barcode.text],
                    barcode.box.top,
                    barcode.box.left,
                ),
            )
        )
        if not relevant_barcodes:
            return EMPTY_IDENTIFIERS

        anchors: list[_AnchorEvidence] = []
        crop_boxes = self._merge_crop_boxes(
            tuple(
                self._barcode_crop_box(barcode, image)
                for barcode in relevant_barcodes[:12]
            )
        )
        call_limit = min(
            budget[0],
            max(1, int(max_ocr_calls))
            if max_ocr_calls is not None
            else budget[0],
        )
        calls_used = 0
        for crop_box in crop_boxes[:6]:
            if (
                budget[0] <= 0
                or calls_used >= call_limit
                or self._remaining(deadline) < 0.5
            ):
                break
            enhanced_crop = enhanced.crop(crop_box)
            candidate_count_before = sum(
                len(anchor.candidates) for anchor in anchors
            )
            data = self._ocr_data(
                enhanced_crop,
                deadline,
                max_seconds=3.0,
            )
            budget[0] -= 1
            calls_used += 1
            self._append_data_anchors(
                anchors,
                data,
                offset_x=crop_box[0],
                offset_y=crop_box[1],
            )
            if (
                sum(len(anchor.candidates) for anchor in anchors)
                == candidate_count_before
                and budget[0] > 0
                and calls_used < call_limit
                and self._remaining(deadline) >= 0.5
            ):
                data = self._ocr_data(
                    self._threshold(enhanced_crop),
                    deadline,
                    max_seconds=3.0,
                )
                budget[0] -= 1
                calls_used += 1
                self._append_data_anchors(
                    anchors,
                    data,
                    offset_x=crop_box[0],
                    offset_y=crop_box[1],
                )
            if budget[0] <= 0 or calls_used >= call_limit:
                break

        self._attach_barcodes(anchors, relevant_barcodes)
        return _result_from_anchors(anchors)

    @staticmethod
    def _attach_barcodes(
        anchors: list[_AnchorEvidence],
        barcodes: Sequence[_BarcodeEvidence],
    ) -> None:
        for barcode in barcodes:
            exact_imei = barcode.text if valid_imei(barcode.text) else None
            exact_serial = _barcode_serial(barcode.text)
            eligible: list[tuple[float, _AnchorEvidence, str]] = []
            for anchor in anchors:
                value: str | None
                if anchor.field_name in {"imei", "imei2"}:
                    value = exact_imei
                else:
                    value = exact_serial
                    if (
                        value is not None
                        and anchor.candidates
                        and not any(
                            _comparison_key(candidate) == _comparison_key(value)
                            for candidate in anchor.candidates
                        )
                    ):
                        value = None
                if value is None:
                    continue
                distance = _association_distance(anchor.box, barcode.box)
                if distance is not None:
                    eligible.append((distance, anchor, value))
            if not eligible:
                continue
            _, nearest, value = min(eligible, key=lambda item: item[0])
            nearest.barcode_values.add(value)
            if nearest.field_name in {"imei", "imei2"}:
                nearest.candidates.add(value)

    def _scan_orientation(
        self,
        image: Image.Image,
        deadline: float,
    ) -> ProductIdentifiers:
        anchors, enhanced, _ = self._collect_anchors(image, deadline)
        if not anchors:
            return EMPTY_IDENTIFIERS
        if self._remaining(deadline) < 0.2:
            return _result_from_anchors(anchors)
        barcodes = self._decode_barcodes(enhanced)
        self._attach_barcodes(anchors, barcodes)
        return _result_from_anchors(anchors)

    def _recognize_sync(self, image_bytes: bytes) -> ProductIdentifiers:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with Image.open(io.BytesIO(image_bytes), formats=("JPEG",)) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > self.max_pixels:
                    raise ValueError("Недопустимый размер изображения")
                # Preserve enough JPEG detail for thin product-label barcodes.
                # Draft decoding still bounds very large inputs before the
                # exact resize; the original dimensions above remain what the
                # decompression-bomb limit validates.
                source.draft("RGB", self._barcode_draft_size(width, height))
                source.load()
                barcode_image = self._fit_barcode_image(
                    ImageOps.exif_transpose(source).convert("RGB")
                )
                image = self._fit(barcode_image.copy())
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            OSError,
        ) as exc:
            raise ValueError("Файл не является поддерживаемым JPEG") from exc

        # Barcode-guided crops are much smaller than a full photo and usually
        # contain the printed IMEI/S/N label itself. Try all orientations here
        # first so an upside-down label does not spend the entire budget on one
        # full-frame Tesseract pass.
        barcode_deadline = min(
            deadline,
            time.monotonic()
            + min(12.0, max(2.0, float(self.timeout_seconds) - 10.0)),
        )
        barcode_ocr_budget = [6]
        barcode_results: list[ProductIdentifiers] = []
        for angle, orientation_calls in zip(
            (0, 180, 90, 270),
            (2, 2, 1, 1),
        ):
            if barcode_ocr_budget[0] <= 0:
                break
            if self._remaining(barcode_deadline) < 0.5:
                logger.info("sales_photo_barcode_budget_reached")
                break
            oriented = (
                barcode_image
                if angle == 0
                else barcode_image.rotate(angle, expand=True)
            )
            result = self._scan_barcode_regions(
                oriented,
                barcode_deadline,
                barcode_ocr_budget,
                max_ocr_calls=orientation_calls,
            )
            if result != EMPTY_IDENTIFIERS:
                barcode_results.append(result)
        # Any non-empty ROI evidence is authoritative for this recognition
        # attempt. An empty merge means the independently scanned orientations
        # disagreed, so fail closed instead of letting a later full-frame pass
        # promote one side of the conflict.
        if barcode_results:
            return _merge_results(barcode_results)

        for angle in (0, 180, 90, 270):
            if self._remaining(deadline) < 0.5:
                logger.warning("sales_photo_ocr_deadline_reached")
                break
            oriented = image if angle == 0 else image.rotate(angle, expand=True)
            result = self._scan_orientation(oriented, deadline)
            if result != EMPTY_IDENTIFIERS:
                return result
        return EMPTY_IDENTIFIERS

    async def recognize(
        self, image_bytes: bytes, mime_type: str
    ) -> ProductIdentifiers:
        if not image_bytes:
            return EMPTY_IDENTIFIERS
        # The semaphore is released by the worker callback, not by the awaiting
        # request. If Telegram cancels the request, the thread and its Tesseract
        # child can finish without allowing another memory-heavy OCR job to overlap.
        await self._gate.acquire()
        try:
            worker = asyncio.create_task(
                asyncio.to_thread(self._recognize_sync, image_bytes)
            )
        except BaseException:
            self._gate.release()
            raise

        def release_gate(done: asyncio.Task[ProductIdentifiers]) -> None:
            self._gate.release()
            if not done.cancelled():
                done.exception()  # Mark detached worker failures as observed.

        worker.add_done_callback(release_gate)
        return await asyncio.shield(worker)
