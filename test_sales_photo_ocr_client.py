from __future__ import annotations

import json
import unittest

import httpx

from sales_photo_bot.formatting import build_caption, product_label_from_card
from sales_photo_bot.models import (
    ProductIdentifiers,
    merge_product_identifiers,
    product_display_name,
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


class ProductIdentifierTests(unittest.TestCase):
    def test_legacy_positional_fields_remain_compatible(self):
        result = ProductIdentifiers("1", "2", "serial")

        self.assertEqual((result.imei, result.imei2, result.serial_number), (
            "1",
            "2",
            "serial",
        ))
        self.assertIsNone(result.product_info)
        self.assertEqual(result.phone_numbers, ())

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


class OCRResponseTests(unittest.TestCase):
    def test_strict_response_parsing(self):
        parsed = parse_ocr_response(json.dumps(response_payload()).encode())

        self.assertEqual(parsed.product_info, "Samsung Galaxy A16 8/256")
        self.assertEqual(parsed.product_model, "SM-A166B")
        self.assertEqual(parsed.imei, "490154203237518")
        self.assertEqual(parsed.phone_numbers, ("+998 90 123 45 67",))

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
                json=response_payload(),
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
