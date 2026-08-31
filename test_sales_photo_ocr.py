from __future__ import annotations

import asyncio
import io
import shutil
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import zxingcpp
from PIL import Image, ImageDraw, ImageFont

from sales_photo_bot.models import ProductIdentifiers
from sales_photo_bot.ocr import (
    TesseractIdentifierRecognizer,
    _AnchorEvidence,
    _BarcodeEvidence,
    _Box,
    _result_from_anchors,
    extract_identifiers,
    valid_imei,
)


IMEI_1 = "490154203237518"
IMEI_2 = "352099001761481"


def jpeg(width: int = 80, height: int = 40) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="JPEG")
    return output.getvalue()


def synthetic_serial_jpeg(serial: str, rotate: int = 0) -> bytes:
    image = Image.new("RGB", (1500, 700), "white")
    drawing = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=72)
    drawing.text((80, 80), f"S/N: {serial}", fill="black", font=font)
    barcode = zxingcpp.create_barcode(serial, zxingcpp.BarcodeFormat.Code128)
    barcode_image = Image.fromarray(barcode.to_image(scale=4)).convert("RGB")
    image.paste(barcode_image, (120, 240))
    if rotate:
        image = image.rotate(rotate, expand=True)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=94)
    return output.getvalue()


def synthetic_sample_style_jpeg(serial: str, label_count: int = 3) -> bytes:
    image = Image.new("RGB", (2560, 1922), "#eeeeee")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((250, 500, 2300, 1100), fill="white")
    font = ImageFont.load_default(size=42)
    positions = ((330, 650), (1150, 650), (1150, 840))
    for left, top in positions[:label_count]:
        drawing.text(
            (left, top),
            f"S/N: {serial}",
            fill="black",
            font=font,
        )
        barcode = zxingcpp.create_barcode(
            serial,
            zxingcpp.BarcodeFormat.Code128,
        )
        barcode_image = Image.fromarray(barcode.to_image(scale=2)).convert("RGB")
        image.paste(barcode_image, (left, top + 55))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue()


def ocr_line_data(words: tuple[str, ...]) -> dict[str, list[object]]:
    count = len(words)
    return {
        "text": list(words),
        "left": [20 + index * 100 for index in range(count)],
        "top": [30] * count,
        "width": [90] * count,
        "height": [30] * count,
        "page_num": [1] * count,
        "block_num": [1] * count,
        "par_num": [1] * count,
        "line_num": [1] * count,
    }


class IdentifierParsingTests(unittest.TestCase):
    def test_imei_requires_ascii_15_digits_and_valid_check_digit(self):
        self.assertTrue(valid_imei(IMEI_1))
        self.assertTrue(valid_imei(IMEI_2))
        self.assertFalse(valid_imei(IMEI_1[:-1] + "9"))
        self.assertFalse(valid_imei("12345678901234"))
        self.assertFalse(valid_imei("٤٩٠١٥٤٢٠٣٢٣٧٥١٨"))
        self.assertFalse(valid_imei("000000000000000"))

    def test_explicit_imei_slots_and_repeated_serial_are_extracted(self):
        result = extract_identifiers(
            (
                f"IMEI 1: {IMEI_1}\nIMEI 2: {IMEI_2}\nS/N: TEST-SN-42",
                "Serial No TEST-SN-42",
            )
        )
        self.assertEqual(
            result,
            ProductIdentifiers(
                imei=IMEI_1,
                imei2=IMEI_2,
                serial_number="TEST-SN-42",
            ),
        )

    def test_unlabelled_numbers_and_ean_are_not_promoted_to_imei(self):
        result = extract_identifiers(
            (f"{IMEI_1}\nEAN 8806097804598\nModel SM-X133",)
        )
        self.assertEqual(result, ProductIdentifiers())

    def test_limited_ocr_digit_correction_still_requires_luhn(self):
        result = extract_identifiers(("IMEI: 49O1542O3237518",))
        self.assertEqual(result.imei, IMEI_1)
        self.assertIsNone(extract_identifiers(("IMEI: 49O1542O3237519",)).imei)
        self.assertIsNone(extract_identifiers(("IMEI: 49O1542O32375IB",)).imei)

    def test_conflicting_equal_readings_fail_closed(self):
        result = extract_identifiers((f"IMEI: {IMEI_1}", f"IMEI: {IMEI_2}"))
        self.assertIsNone(result.imei)

    def test_next_identifier_label_cannot_cross_bind_imei_slots(self):
        result = extract_identifiers((f"IMEI1:\nIMEI2: {IMEI_2}",))
        self.assertIsNone(result.imei)
        self.assertEqual(result.imei2, IMEI_2)

    def test_duplicate_value_never_populates_both_imei_slots(self):
        result = extract_identifiers(
            (f"IMEI1: {IMEI_1}\nIMEI2: {IMEI_1}",)
        )
        self.assertEqual(result, ProductIdentifiers())

    def test_single_serial_read_is_not_enough(self):
        self.assertEqual(
            extract_identifiers(("S/N: ABC12345",)),
            ProductIdentifiers(),
        )

    def test_distant_or_generic_text_is_not_a_serial(self):
        for value in (
            "S/N:\nSAMSUNG GALAXY",
            "Serial No: MADE IN CHINA",
            "SIN: MADEINCHINA",
        ):
            with self.subTest(value=value):
                self.assertEqual(extract_identifiers((value,)), ProductIdentifiers())

    def test_sample_style_distinct_labels_win_without_character_guessing(self):
        result = extract_identifiers(
            (
                "S/N : R8YL50R510N\nS/N: R8YLSORSION\nS/N : R8YL50R510N",
            )
        )
        self.assertEqual(result.serial_number, "R8YL50R510N")

    def test_ignored_token_does_not_hide_later_serial(self):
        result = extract_identifiers(
            ("S/N: BARCODE R8YL50R510N\nS/N: BARCODE R8YL50R510N",)
        )
        self.assertEqual(result.serial_number, "R8YL50R510N")

    def test_equal_serial_conflict_is_omitted(self):
        result = extract_identifiers(
            (
                "S/N: ABC12345\nS/N: ABCI2345\n"
                "S/N: ABC12345\nS/N: ABCI2345",
            )
        )
        self.assertIsNone(result.serial_number)


class SpatialEvidenceTests(unittest.TestCase):
    def test_one_physical_serial_anchor_is_not_enough(self):
        anchors = [
            _AnchorEvidence("serial_number", _Box(10, 10, 60, 30), {"ABC12345"})
        ]
        self.assertIsNone(_result_from_anchors(anchors).serial_number)

    def test_two_physical_serial_anchors_confirm_exact_value(self):
        anchors = [
            _AnchorEvidence("serial_number", _Box(10, 10, 60, 30), {"ABC12345"}),
            _AnchorEvidence("serial_number", _Box(10, 100, 60, 120), {"ABC12345"}),
        ]
        self.assertEqual(_result_from_anchors(anchors).serial_number, "ABC12345")

    def test_near_barcode_resolves_ocr_confusion_but_far_one_does_not(self):
        near = _AnchorEvidence(
            "serial_number", _Box(100, 100, 180, 130), {"R8YL5OR510N"}
        )
        TesseractIdentifierRecognizer._attach_barcodes(
            [near],
            [_BarcodeEvidence("R8YL50R510N", _Box(190, 80, 650, 180))],
        )
        self.assertEqual(_result_from_anchors([near]).serial_number, "R8YL50R510N")

        far = _AnchorEvidence(
            "serial_number", _Box(100, 100, 180, 130), {"R8YL5OR510N"}
        )
        TesseractIdentifierRecognizer._attach_barcodes(
            [far],
            [_BarcodeEvidence("R8YL50R510N", _Box(1200, 900, 1500, 1000))],
        )
        self.assertIsNone(_result_from_anchors([far]).serial_number)

    def test_repeated_serial_barcode_can_fill_two_empty_sn_labels(self):
        anchors = [
            _AnchorEvidence(
                "serial_number",
                _Box(100, 100, 180, 130),
                set(),
            ),
            _AnchorEvidence(
                "serial_number",
                _Box(100, 300, 180, 330),
                set(),
            ),
        ]
        TesseractIdentifierRecognizer._attach_barcodes(
            anchors,
            [
                _BarcodeEvidence("R8YL50R510N", _Box(190, 80, 650, 180)),
                _BarcodeEvidence("R8YL50R510N", _Box(190, 280, 650, 380)),
            ],
        )
        self.assertEqual(
            _result_from_anchors(anchors).serial_number,
            "R8YL50R510N",
        )

    def test_one_unverified_barcode_near_empty_sn_label_is_not_promoted(self):
        anchor = _AnchorEvidence(
            "serial_number", _Box(100, 100, 180, 130), set()
        )
        TesseractIdentifierRecognizer._attach_barcodes(
            [anchor],
            [_BarcodeEvidence("MODEL12345", _Box(190, 80, 650, 180))],
        )
        self.assertIsNone(_result_from_anchors([anchor]).serial_number)


class TesseractRecognizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_photo_is_downscaled_before_ocr(self):
        fitted = await asyncio.to_thread(
            TesseractIdentifierRecognizer._fit,
            Image.new("RGB", (4000, 3000), "white"),
        )
        self.assertEqual(max(fitted.size), 1800)
        self.assertLessEqual(fitted.width * fitted.height, 2_500_000)

    async def test_large_jpeg_draft_target_forces_native_half_decode(self):
        recognizer = TesseractIdentifierRecognizer()
        self.assertEqual(
            recognizer._barcode_draft_size(6531, 4899),
            (3265, 2449),
        )
        self.assertEqual(
            recognizer._barcode_draft_size(2560, 1922),
            (2560, 1922),
        )

    async def test_invalid_or_oversized_decoded_image_is_rejected(self):
        recognizer = TesseractIdentifierRecognizer(max_pixels=100)
        with self.assertRaisesRegex(ValueError, "поддерживаемым JPEG"):
            await recognizer.recognize(b"not-an-image", "image/jpeg")
        with self.assertRaisesRegex(ValueError, "размер изображения"):
            await recognizer.recognize(jpeg(20, 20), "image/jpeg")

    async def test_preflight_requires_english_tesseract_data(self):
        recognizer = TesseractIdentifierRecognizer()
        with patch(
            "sales_photo_bot.ocr.pytesseract.get_languages",
            return_value=["eng"],
        ):
            await recognizer.preflight()
        with patch(
            "sales_photo_bot.ocr.pytesseract.get_languages",
            return_value=["osd"],
        ):
            with self.assertRaisesRegex(RuntimeError, "язык eng"):
                await recognizer.preflight()

    async def test_worker_gate_is_held_until_thread_really_finishes(self):
        recognizer = TesseractIdentifierRecognizer(max_parallel=1)
        active = 0
        maximum = 0
        lock = threading.Lock()

        def slow(_: bytes) -> ProductIdentifiers:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return ProductIdentifiers()

        with patch.object(recognizer, "_recognize_sync", side_effect=slow):
            await asyncio.gather(
                recognizer.recognize(b"one", "image/jpeg"),
                recognizer.recognize(b"two", "image/jpeg"),
            )
        self.assertEqual(maximum, 1)

    async def test_cancelled_waiter_does_not_release_running_worker_gate(self):
        recognizer = TesseractIdentifierRecognizer(max_parallel=1)
        first_started = threading.Event()
        first_can_finish = threading.Event()
        starts = 0
        lock = threading.Lock()

        def controlled(_: bytes) -> ProductIdentifiers:
            nonlocal starts
            with lock:
                starts += 1
                ordinal = starts
            if ordinal == 1:
                first_started.set()
                first_can_finish.wait(timeout=2)
            return ProductIdentifiers()

        with patch.object(recognizer, "_recognize_sync", side_effect=controlled):
            first = asyncio.create_task(recognizer.recognize(b"one", "image/jpeg"))
            self.assertTrue(await asyncio.to_thread(first_started.wait, 1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            second = asyncio.create_task(recognizer.recognize(b"two", "image/jpeg"))
            await asyncio.sleep(0.05)
            self.assertEqual(starts, 1)
            first_can_finish.set()
            await asyncio.wait_for(second, timeout=1)
            self.assertEqual(starts, 2)

    async def test_barcode_roi_ocr_has_one_global_six_call_budget(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=30)
        barcodes = tuple(
            _BarcodeEvidence(
                f"ABC{index}12345",
                _Box(
                    100 + (index % 3) * 550,
                    100 + (index // 3) * 400,
                    200 + (index % 3) * 550,
                    130 + (index // 3) * 400,
                ),
            )
            for index in range(12)
        )
        budget = [6]
        ocr_data = MagicMock(return_value={})
        with patch.object(
            recognizer,
            "_decode_barcodes",
            return_value=barcodes,
        ), patch.object(recognizer, "_ocr_data", ocr_data):
            result = recognizer._scan_barcode_regions(
                Image.new("RGB", (1800, 1800), "white"),
                time.monotonic() + 30,
                budget,
            )

        self.assertEqual(result, ProductIdentifiers())
        self.assertEqual(ocr_data.call_count, 6)
        self.assertEqual(budget, [0])

    async def test_threshold_recovers_value_after_enhanced_finds_only_sn_label(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=30)
        barcode = _BarcodeEvidence(
            "R8YL50R510N",
            _Box(300, 200, 700, 250),
        )
        with patch.object(
            recognizer,
            "_decode_barcodes",
            return_value=(barcode,),
        ), patch.object(
            recognizer,
            "_ocr_data",
            side_effect=(
                ocr_line_data(("S/N:",)),
                ocr_line_data(("S/N:", "R8YL50R510N")),
            ),
        ) as ocr_data:
            result = recognizer._scan_barcode_regions(
                Image.new("RGB", (1000, 600), "white"),
                time.monotonic() + 30,
                [6],
            )

        self.assertEqual(result.serial_number, "R8YL50R510N")
        self.assertEqual(ocr_data.call_count, 2)

    async def test_first_valid_full_frame_result_stops_rotation_scan(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=30)
        expected = ProductIdentifiers(serial_number="R8YL50R510N")
        with patch.object(
            recognizer,
            "_scan_barcode_regions",
            return_value=ProductIdentifiers(),
        ), patch.object(
            recognizer,
            "_scan_orientation",
            return_value=expected,
        ) as full_scan:
            result = recognizer._recognize_sync(jpeg(1600, 800))

        self.assertEqual(result, expected)
        self.assertEqual(full_scan.call_count, 1)

    async def test_barcode_orientations_merge_complementary_fields(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=30)
        with patch.object(
            recognizer,
            "_scan_barcode_regions",
            side_effect=(
                ProductIdentifiers(serial_number="R8YL50R510N"),
                ProductIdentifiers(imei=IMEI_1, imei2=IMEI_2),
                ProductIdentifiers(),
                ProductIdentifiers(),
            ),
        ), patch.object(
            recognizer,
            "_scan_orientation",
            side_effect=AssertionError("full scan should not run"),
        ):
            result = recognizer._recognize_sync(jpeg(1600, 800))

        self.assertEqual(
            result,
            ProductIdentifiers(
                imei=IMEI_1,
                imei2=IMEI_2,
                serial_number="R8YL50R510N",
            ),
        )

    async def test_conflicting_barcode_orientation_values_fail_closed(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=30)
        with patch.object(
            recognizer,
            "_scan_barcode_regions",
            side_effect=(
                ProductIdentifiers(serial_number="R8YL50R510N"),
                ProductIdentifiers(serial_number="OTHER12345"),
                ProductIdentifiers(),
                ProductIdentifiers(),
            ),
        ), patch.object(
            recognizer,
            "_scan_orientation",
            side_effect=AssertionError("full scan must not override ROI conflict"),
        ):
            result = recognizer._recognize_sync(jpeg(1600, 800))

        self.assertEqual(result, ProductIdentifiers())

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    async def test_real_rotated_label_and_code128_are_read_locally(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=25)
        result = await recognizer.recognize(
            synthetic_serial_jpeg("TEST-SN-42", rotate=180),
            "image/jpeg",
        )
        self.assertEqual(result.serial_number, "TEST-SN-42")

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    async def test_real_sample_sized_label_uses_barcode_regions(self):
        recognizer = TesseractIdentifierRecognizer(timeout_seconds=15)
        with patch.object(
            recognizer,
            "_scan_orientation",
            side_effect=AssertionError("barcode ROI path should succeed"),
        ):
            result = await asyncio.wait_for(
                recognizer.recognize(
                    synthetic_sample_style_jpeg(
                        "R8YL50R510N",
                        label_count=1,
                    ),
                    "image/jpeg",
                ),
                timeout=20,
            )
        self.assertEqual(result.serial_number, "R8YL50R510N")


if __name__ == "__main__":
    unittest.main()
