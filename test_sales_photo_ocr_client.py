from __future__ import annotations

import json
import unittest
from datetime import date

import httpx

from sales_photo_bot.formatting import (
    MAX_CAPTION_TEXT_UNITS,
    MAX_MESSAGE_TEXT_UNITS,
    _telegram_text_units,
    build_caption,
    product_label_from_card,
)
from sales_photo_bot.models import (
    ProductIdentifiers,
    identifier_imeis,
    identifier_serial_numbers,
    merge_product_identifiers,
    product_display_name,
    product_display_values,
)
from sales_photo_bot.ocr_client import (
    OCRResponseError,
    RemoteOCRRecognizer,
    parse_ocr_response,
    validate_ocr_base_url,
)


def response_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "product_info": "Samsung Galaxy A16 8/256",
        "product_model": "SM-A166B",
        "imei": "490154203237518",
        "imei2": "352099001761481",
        "serial_number": "R8YL50R510N",
        "phone_numbers": ["+998 90 123 45 67"],
    }
    payload.update(changes)
    return payload


def extended_response_payload(**changes: object) -> dict[str, object]:
    payload = response_payload()
    payload.update(
        {
            "product_names": ["Samsung Galaxy A16 8/256"],
            "imeis": ["490154203237518", "352099001761481"],
            "serial_numbers": ["R8YL50R510N"],
            "warranty_card_detected": True,
        }
    )
    payload.update(changes)
    return payload


def valid_imei(number: int) -> str:
    base = f"860000000{number:05d}"[:14]
    total = 0
    for index, character in enumerate(base):
        digit = int(character)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return base + str((-total) % 10)


class ProductIdentifierTests(unittest.TestCase):
    def test_legacy_positional_fields_remain_compatible(self):
        result = ProductIdentifiers(
            "1",
            "2",
            "serial",
            "product info",
            "model",
            ("+998 90 123 45 67",),
        )

        self.assertEqual((result.imei, result.imei2, result.serial_number), (
            "1",
            "2",
            "serial",
        ))
        self.assertEqual(result.product_info, "product info")
        self.assertEqual(result.product_model, "model")
        self.assertEqual(result.phone_numbers, ("+998 90 123 45 67",))
        self.assertEqual(result.product_names, ())
        self.assertEqual(result.imeis, ())
        self.assertEqual(result.serial_numbers, ())
        self.assertFalse(result.warranty_card_detected)

    def test_product_display_combines_name_and_distinct_model(self):
        identifiers = ProductIdentifiers(
            product_info=" Samsung   Galaxy A16 8/256 ",
            product_model="SM-A166B",
        )
        self.assertEqual(
            product_display_name(identifiers),
            "Samsung Galaxy A16 8/256 — SM-A166B",
        )
        self.assertEqual(
            product_display_name(
                ProductIdentifiers(
                    product_info="Samsung SM-A166B",
                    product_model="SM-A166B",
                )
            ),
            "Samsung SM-A166B",
        )
        self.assertEqual(
            product_display_name(
                ProductIdentifiers(product_info="Redmi\u202e Note 14")
            ),
            "Redmi Note 14",
        )
        self.assertEqual(
            product_display_name(
                ProductIdentifiers(
                    product_info="SMX133",
                    product_model="SM-X133",
                )
            ),
            "SMX133",
        )

    def test_album_merge_keeps_only_compatible_product_variants(self):
        compatible = merge_product_identifiers(
            [
                ProductIdentifiers(
                    product_info="Redmi Note 14 8/256 Black",
                    product_model="24094RAD4G",
                    imei="490154203237518",
                    phone_numbers=("+998 90 123 45 67",),
                ),
                ProductIdentifiers(
                    product_info="REDMI NOTE 14 8 256 BLACK",
                    product_model="24094RAD4G",
                    serial_number="ABC123",
                    phone_numbers=("+998 91 765 43 21",),
                ),
            ]
        )
        self.assertEqual(compatible.product_info, "Redmi Note 14 8/256 Black")
        self.assertEqual(compatible.product_model, "24094RAD4G")
        self.assertEqual(compatible.imei, "490154203237518")
        self.assertEqual(len(compatible.phone_numbers), 2)

        conflict = merge_product_identifiers(
            [
                ProductIdentifiers(
                    product_info="Redmi Note 14 Black",
                    product_model="24094RAD4G",
                ),
                ProductIdentifiers(
                    product_info="Huawei Watch GT 5",
                    product_model="VLI-B19",
                ),
            ]
        )
        self.assertIsNone(conflict.product_info)
        self.assertIsNone(conflict.product_model)

        for different_variant in (
            "iPhone 15 Pro Max",
            "Galaxy A165",
            "Redmi Note 14C",
        ):
            base = (
                "iPhone 15"
                if different_variant.startswith("iPhone")
                else "Galaxy A16"
                if different_variant.startswith("Galaxy")
                else "Redmi Note 14"
            )
            with self.subTest(different_variant=different_variant):
                merged = merge_product_identifiers(
                    [
                        ProductIdentifiers(product_info=base),
                        ProductIdentifiers(product_info=different_variant),
                    ]
                )
                self.assertIsNone(merged.product_info)

    def test_receipt_safe_merge_keeps_serial_context_and_drops_phone_only(self):
        merged = merge_product_identifiers(
            [
                ProductIdentifiers(serial_number="RAYBAN123"),
                ProductIdentifiers(phone_numbers=("+998 71 200 00 00",)),
            ],
            receipt_safe=True,
        )

        self.assertEqual(merged.serial_number, "RAYBAN123")
        self.assertEqual(merged.phone_numbers, ())

    def test_receipt_safe_merge_keeps_phone_from_detected_warranty_card(self):
        merged = merge_product_identifiers(
            [
                ProductIdentifiers(
                    product_names=("Samsung Galaxy Watch Ultra 2",),
                    serial_numbers=("BOX-SERIAL-1",),
                ),
                ProductIdentifiers(
                    phone_numbers=("+998 97 465 11 59",),
                    warranty_card_detected=True,
                ),
            ],
            receipt_safe=True,
        )

        self.assertEqual(
            merged.product_names,
            ("Samsung Galaxy Watch Ultra 2",),
        )
        self.assertEqual(merged.serial_numbers, ("BOX-SERIAL-1",))
        self.assertEqual(merged.phone_numbers, ("+998 97 465 11 59",))
        self.assertTrue(merged.warranty_card_detected)

    def test_album_merge_unions_explicit_multi_box_values_in_stable_order(self):
        imeis = tuple(valid_imei(index) for index in range(4))
        merged = merge_product_identifiers(
            [
                ProductIdentifiers(
                    product_names=("Samsung Galaxy A16", "iPhone 16 Pro"),
                    imeis=imeis[:2],
                    serial_numbers=("SAMSUNG01", "APPLE01"),
                ),
                ProductIdentifiers(
                    product_names=("iPhone 16 Pro", "Redmi Note 14"),
                    imeis=imeis[1:],
                    serial_numbers=("APPLE01", "REDMI01"),
                ),
            ],
            receipt_safe=True,
        )

        self.assertEqual(
            merged.product_names,
            ("Samsung Galaxy A16", "iPhone 16 Pro", "Redmi Note 14"),
        )
        self.assertEqual(identifier_imeis(merged), imeis)
        self.assertEqual(
            identifier_serial_numbers(merged),
            ("SAMSUNG01", "APPLE01", "REDMI01"),
        )
        self.assertEqual(merged.imei, imeis[0])
        self.assertEqual(merged.imei2, imeis[1])
        self.assertIsNone(merged.serial_number)

    def test_multi_box_caption_uses_one_product_line_and_all_quote_rows(self):
        imeis = tuple(valid_imei(index) for index in range(8))
        products = (
            "Samsung Galaxy A16",
            "Apple iPhone 16 Pro",
            "Xiaomi Redmi Note 14",
            "Huawei Watch GT 5",
        )
        serials = ("BOX0001", "BOX0002", "BOX0003", "BOX0004")
        identifiers = ProductIdentifiers(
            product_names=products,
            imeis=imeis,
            serial_numbers=serials,
        )

        caption = build_caption(
            "+998 90 123 45 67",
            identifiers,
            manager="Abbos",
            sale_date=date(2026, 9, 7),
            daily_quantity=123,
            global_order_id=123456,
        )

        self.assertEqual(product_display_values(identifiers), products)
        self.assertIn("📦 О товаре: " + "; ".join(products), caption)
        self.assertEqual(caption.count("📦 О товаре:"), 1)
        for imei in imeis:
            self.assertIn(imei, caption)
        for serial in serials:
            self.assertIn(serial, caption)
        self.assertEqual(caption.count("<blockquote>"), 12)
        self.assertNotIn("… ещё", caption)
        self.assertLessEqual(
            len(caption.encode("utf-16-le")) // 2,
            1000,
        )
        self.assertEqual(
            product_label_from_card(caption),
            "; ".join(products),
        )

    def test_pathological_multi_box_caption_is_bounded_and_marks_overflow(self):
        identifiers = ProductIdentifiers(
            product_names=tuple(
                f"Product {index} " + "X" * 145 for index in range(8)
            ),
            imeis=tuple(valid_imei(index) for index in range(16)),
            serial_numbers=tuple(
                f"SERIAL{index:02d}" + "X" * 30 for index in range(16)
            ),
        )

        caption = build_caption(None, identifiers)

        self.assertLessEqual(
            len(caption.encode("utf-16-le")) // 2,
            1000,
        )
        self.assertIn("… ещё", caption)
        self.assertIn("🛒💵:", caption)
        self.assertIn("<b>Card/Terminal/Paynet</b>", caption)
        self.assertLessEqual(
            _telegram_text_units(caption),
            MAX_CAPTION_TEXT_UNITS,
        )

    def test_message_budget_keeps_maximum_v2_values_without_overflow(self):
        products = tuple(
            f"Catalog Product {index} " + "X" * 135 for index in range(8)
        )
        imeis = tuple(valid_imei(index) for index in range(16))
        serials = tuple(
            f"SERIAL{index:02d}" + "X" * 30 for index in range(16)
        )
        caption = build_caption(
            None,
            ProductIdentifiers(
                product_names=products,
                imeis=imeis,
                serial_numbers=serials,
            ),
            max_text_units=MAX_MESSAGE_TEXT_UNITS,
        )

        for value in (*products, *imeis, *serials):
            self.assertIn(value, caption)
        self.assertNotIn("… ещё", caption)
        self.assertLess(_telegram_text_units(caption), 4096)

    def test_caption_budget_rejects_non_integer_or_out_of_range_values(self):
        for value in (True, 255, 4097, 800.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_caption(
                    None,
                    ProductIdentifiers(),
                    max_text_units=value,
                )

    def test_album_merge_rejects_third_phone_and_duplicate_second_imei(self):
        merged = merge_product_identifiers(
            [
                ProductIdentifiers(
                    imei="490154203237518",
                    imei2="490154203237518",
                    phone_numbers=("+998 90 123 45 67",),
                ),
                ProductIdentifiers(phone_numbers=("+998 91 765 43 21",)),
                ProductIdentifiers(phone_numbers=("+998 93 111 22 33",)),
            ]
        )

        self.assertEqual(merged.imei, "490154203237518")
        self.assertIsNone(merged.imei2)
        self.assertEqual(merged.phone_numbers, ())

    def test_caption_orders_product_identifiers_then_finance_and_combines_phones(self):
        caption = build_caption(
            "+998 90 123 45 67",
            ProductIdentifiers(
                product_info="Samsung <A16>",
                product_model="SM-A166B",
                imei="490154203237518",
                imei2="352099001761481",
                serial_number="ABC123",
                phone_numbers=(
                    "+998 90 123 45 67",
                    "+998 91 765 43 21",
                ),
            ),
        )

        expected = (
            "📦 О товаре: Samsung &lt;A16&gt; — SM-A166B\n"
            "<blockquote>IMEI: 490154203237518</blockquote>\n"
            "<blockquote>IMEI2: 352099001761481</blockquote>\n"
            "<blockquote>S/N: ABC123</blockquote>\n\n"
            "🛒💵:"
        )
        self.assertTrue(caption.startswith(expected))
        self.assertIn(
            "📞: +998 90 123 45 67 / +998 91 765 43 21",
            caption,
        )
        self.assertEqual(product_label_from_card(caption), "Samsung <A16> — SM-A166B")

    def test_caption_phone_merge_is_fail_closed_for_three_distinct_numbers(self):
        caption = build_caption(
            "+998 90 123 45 67 / +998 91 765 43 21",
            ProductIdentifiers(phone_numbers=("+998 93 111 22 33",)),
        )

        self.assertIn("📞:\n\n<b>Наличка</b>", caption)
        self.assertNotIn("+998", caption)

    def test_legacy_imei2_only_keeps_its_original_label(self):
        caption = build_caption(
            None,
            ProductIdentifiers(imei2="352099001761481"),
        )

        self.assertIn(
            "<blockquote>IMEI2: 352099001761481</blockquote>",
            caption,
        )
        self.assertNotIn("<blockquote>IMEI:", caption)

    def test_product_parser_accepts_legacy_and_new_lines(self):
        self.assertEqual(product_label_from_card("📦 A16 8/256\n\n🛒💵:"), "A16 8/256")
        self.assertEqual(
            product_label_from_card("📦 О товаре: A16 8/256\n\n🛒💵:"),
            "A16 8/256",
        )
        quoted = '"' * 120
        self.assertEqual(
            product_label_from_card(
                build_caption(None, ProductIdentifiers(), product_label=quoted)
            ),
            quoted,
        )
        long_product = "X" * 2500
        self.assertEqual(
            product_label_from_card(
                f"📦 О товаре: {long_product}\n\n🛒💵:"
            ),
            long_product[:2048],
        )


class OCRResponseTests(unittest.TestCase):
    def test_legacy_response_is_validated_but_all_unproven_fields_are_ignored(self):
        parsed = parse_ocr_response(json.dumps(response_payload()).encode())

        self.assertEqual(parsed, ProductIdentifiers())
        fallback_caption = build_caption(None, parsed)
        self.assertTrue(fallback_caption.startswith("🛒💵:"))
        self.assertNotIn("📦", fallback_caption)
        self.assertNotIn("<blockquote>", fallback_caption)

        album = merge_product_identifiers(
            [
                parsed,
                ProductIdentifiers(serial_number="SECOND-BOX"),
            ],
            receipt_safe=True,
        )
        self.assertEqual(album.serial_number, "SECOND-BOX")
        self.assertEqual(album.phone_numbers, ())

    def test_extended_response_parses_and_deduplicates_ordered_arrays(self):
        payload = extended_response_payload(
            product_info="Samsung Galaxy A16 8/256",
            product_names=[
                "Samsung Galaxy A16 8/256",
                "samsung-galaxy-a16-8/256",
            ],
            imei="490154203237518",
            imei2=None,
            imeis=["490154203237518", "490154203237518"],
            serial_number="R8YL50R510N",
            serial_numbers=["R8YL50R510N", "r8yl50r510n"],
        )

        parsed = parse_ocr_response(json.dumps(payload).encode())

        self.assertEqual(parsed.product_names, ("Samsung Galaxy A16 8/256",))
        self.assertEqual(parsed.imeis, ("490154203237518",))
        self.assertEqual(parsed.serial_numbers, ("R8YL50R510N",))
        self.assertEqual(parsed.phone_numbers, ("+998 90 123 45 67",))
        self.assertTrue(parsed.warranty_card_detected)

    def test_extended_multi_box_response_has_null_ambiguous_scalars(self):
        imeis = [valid_imei(index) for index in range(4)]
        payload = extended_response_payload(
            product_info=None,
            product_model=None,
            product_names=["Samsung Galaxy A16", "Apple iPhone 16"],
            imei=imeis[0],
            imei2=imeis[1],
            imeis=imeis,
            serial_number=None,
            serial_numbers=["BOX0001", "BOX0002"],
            phone_numbers=[],
            warranty_card_detected=False,
        )

        parsed = parse_ocr_response(json.dumps(payload).encode())

        self.assertEqual(
            parsed.product_names,
            ("Samsung Galaxy A16", "Apple iPhone 16"),
        )
        self.assertEqual(parsed.imeis, tuple(imeis))
        self.assertEqual(parsed.serial_numbers, ("BOX0001", "BOX0002"))

    def test_extended_response_accepts_bounded_collection_maximums(self):
        imeis = [valid_imei(index) for index in range(16)]
        payload = extended_response_payload(
            product_info=None,
            product_model=None,
            product_names=[f"Catalog Product {index}" for index in range(8)],
            imei=imeis[0],
            imei2=imeis[1],
            imeis=imeis,
            serial_number=None,
            serial_numbers=[f"BOX{index:04d}" for index in range(16)],
            phone_numbers=[],
            warranty_card_detected=False,
        )

        parsed = parse_ocr_response(json.dumps(payload).encode())

        self.assertEqual(len(parsed.product_names), 8)
        self.assertEqual(len(parsed.imeis), 16)
        self.assertEqual(len(parsed.serial_numbers), 16)

    def test_response_rejects_unknown_missing_or_unsafe_values(self):
        malformed = response_payload(extra="not allowed")
        with self.assertRaises(OCRResponseError):
            parse_ocr_response(json.dumps(malformed).encode())

        missing = response_payload()
        del missing["imei2"]
        with self.assertRaises(OCRResponseError):
            parse_ocr_response(json.dumps(missing).encode())

        for changes in (
            {"imei": "123"},
            {"imei": "123456789012345"},
            {"phone_numbers": ["90 123 45 67"]},
            {"serial_number": "bad serial"},
            {"product_info": "x" * 161},
            {"product_info": "Redmi\u202e Note 14"},
        ):
            with self.subTest(changes=changes), self.assertRaises(OCRResponseError):
                parse_ocr_response(json.dumps(response_payload(**changes)).encode())

    def test_extended_response_rejects_unsafe_arrays_and_mirror_mismatches(self):
        imeis = [valid_imei(index) for index in range(17)]
        invalid_changes = (
            {"product_names": "Samsung Galaxy A16"},
            {"product_names": ["x" * 161], "product_info": "x" * 160},
            {"product_names": ["Galaxy\u202e A16"], "product_info": "Galaxy A16"},
            {"imeis": imeis, "imei": imeis[0], "imei2": imeis[1]},
            {"imeis": ["123456789012345"], "imei": "123456789012345", "imei2": None},
            {"serial_numbers": ["bad serial"], "serial_number": "bad serial"},
            {"warranty_card_detected": 1},
            {"imei": None},
            {"imei2": None},
            {"serial_number": None},
            {"product_info": None},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(OCRResponseError):
                parse_ocr_response(
                    json.dumps(extended_response_payload(**changes)).encode()
                )

    def test_extended_response_rejects_phone_without_detected_warranty_card(self):
        with self.assertRaisesRegex(OCRResponseError, "гарантийной"):
            parse_ocr_response(
                json.dumps(
                    extended_response_payload(warranty_card_detected=False)
                ).encode()
            )

    def test_url_validation_rejects_credentials_and_query(self):
        self.assertEqual(
            validate_ocr_base_url("http://127.0.0.1:8765/"),
            "http://127.0.0.1:8765",
        )
        for value in (
            "ftp://127.0.0.1",
            "http://user:pass@127.0.0.1",
            "http://127.0.0.1?secret=x",
            "http://127.0.0.1?",
            "http://127.0.0.1#",
            "http://127.0.0.1/internal-prefix",
            "http://127.0.0.1:notaport",
            "http://127.0.0.1:99999",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_ocr_base_url(value)


class RemoteOCRRecognizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_raw_image_to_contract_endpoint(self):
        requests: list[tuple[str, str, bytes, str, str]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = await request.aread()
            requests.append(
                (
                    request.method,
                    request.url.path,
                    body,
                    request.headers["content-type"],
                    request.headers["content-length"],
                )
            )
            return httpx.Response(
                200,
                json=extended_response_payload(),
                headers={"content-type": "application/json"},
            )

        recognizer = RemoteOCRRecognizer(
            "http://ocr.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await recognizer.recognize(b"jpeg-data", "image/jpeg")
        finally:
            await recognizer.aclose()

        self.assertEqual(
            requests,
            [("POST", "/v1/recognize", b"jpeg-data", "image/jpeg", "9")],
        )
        self.assertEqual(result.serial_number, "R8YL50R510N")

    async def test_health_probe_and_failure_are_fail_open(self):
        async def healthy(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/health")
            return httpx.Response(200, json={"status": "ok"})

        recognizer = RemoteOCRRecognizer(
            "http://ocr.test",
            transport=httpx.MockTransport(healthy),
        )
        try:
            self.assertTrue(await recognizer.preflight())
        finally:
            await recognizer.aclose()

        async def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        recognizer = RemoteOCRRecognizer(
            "http://ocr.test",
            transport=httpx.MockTransport(offline),
        )
        try:
            self.assertFalse(await recognizer.preflight())
        finally:
            await recognizer.aclose()

    async def test_rejects_non_json_without_trusting_body(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"not-json",
                headers={"content-type": "text/plain"},
            )

        recognizer = RemoteOCRRecognizer(
            "http://ocr.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(OCRResponseError):
                await recognizer.recognize(b"jpeg", "image/jpeg")
        finally:
            await recognizer.aclose()

    async def test_rejects_negative_content_length(self):
        response = httpx.Response(
            200,
            content=b"{}",
            headers={"content-length": "-1"},
        )

        with self.assertRaises(OCRResponseError):
            await RemoteOCRRecognizer._read_bounded(response, 32)


if __name__ == "__main__":
    unittest.main()
