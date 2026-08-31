from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from telegram.error import NetworkError, RetryAfter

from sales_photo_bot.application import _prepare_polling, build_application
from sales_photo_bot.config import ConfigError, Settings
from sales_photo_bot.formatting import (
    add_manager_selection,
    build_caption,
    remove_manager_selection,
    selected_manager_from_caption,
)
from sales_photo_bot.keyboards import manager_keyboard
from sales_photo_bot.models import Recognition
from sales_photo_bot.recognition import (
    GeminiProductRecognizer,
    _Extracted,
    _cache_keys,
    _field_supports_name,
    _identifier,
    _parse_json,
    _registrable_domain,
    _result_supports_identifier_and_name,
    _strong_candidate,
    _valid_commercial_name,
    normalize_color,
    normalize_memory,
)
from sales_photo_bot.repository import SalesPhotoRepository, utc_now
from sales_photo_bot.search import SearchResult, SerperProductSearch
from sales_photo_bot.service import BOT_CARD_MARKER, SalesPhotoService


CHAT_ID = -1001234567890
BOT_ID = 777
TOKEN = "1234567890:" + "A" * 35


class StaticRecognizer:
    def __init__(self, result: Recognition | None = None, error: Exception | None = None):
        self.result = result or Recognition()
        self.error = error
        self.calls = 0

    async def recognize(self, image_bytes: bytes, mime_type: str) -> Recognition:
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeSearch:
    def __init__(
        self,
        results: tuple[SearchResult, ...] = (),
        error: Exception | None = None,
    ):
        self.results = results
        self.error = error
        self.queries: list[str] = []

    async def search(self, query: str) -> tuple[SearchResult, ...]:
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results

    async def aclose(self) -> None:
        return None


def search_result(position: int, domain: str, text: str) -> SearchResult:
    return SearchResult(
        position=position,
        title=text,
        snippet=f"Official specification: {text}",
        link=f"https://{domain}/product/{position}",
        domain=domain,
    )


def extraction_json(
    *,
    brand: str = "Samsung",
    product_type: str = "tablet",
    model_code: str = "SM-X133",
    model_kind: str = "model_code",
    model_confidence: float = 0.99,
    sku: str = "SM-X133NZSAMEA",
    sku_kind: str = "model_variant",
    sku_confidence: float = 0.99,
    candidate: str = "",
    candidate_confidence: float = 0.0,
    memory: str = "4GB | 64GB",
    memory_confidence: float = 0.99,
    color: str = "Silver",
    color_confidence: float = 0.98,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=json.dumps(
            {
                "brand": brand,
                "product_type": product_type,
                "model_code": model_code,
                "model_code_kind": model_kind,
                "model_code_confidence": model_confidence,
                "sku": sku,
                "sku_kind": sku_kind,
                "sku_confidence": sku_confidence,
                "visual_candidate_name": candidate,
                "visual_candidate_confidence": candidate_confidence,
                "memory": memory,
                "memory_confidence": memory_confidence,
                "color": color,
                "color_confidence": color_confidence,
            }
        )
    )


def settings(tmp: Path, allowed: frozenset[int] = frozenset()) -> Settings:
    return Settings(
        bot_token=TOKEN,
        chat_id=CHAT_ID,
        gemini_api_key="test-gemini-key",
        serper_api_key="test-serper-key",
        db_path=tmp / "sales.db",
        heartbeat_path=tmp / "heartbeat",
        allowed_user_ids=allowed,
        delete_retry_seconds=1,
    )


def photo_message(
    message_id: int = 10,
    caption: str | None = "+998 90 123 45 67",
    sender_id: int = 50,
):
    return SimpleNamespace(
        chat_id=CHAT_ID,
        chat=SimpleNamespace(id=CHAT_ID),
        message_id=message_id,
        message_thread_id=None,
        caption=caption,
        photo=(
            SimpleNamespace(
                file_id="small",
                file_unique_id="unique-small",
                file_size=100,
                width=100,
                height=100,
            ),
            SimpleNamespace(
                file_id="large",
                file_unique_id="unique-large",
                file_size=500,
                width=1000,
                height=1000,
            ),
        ),
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
    )


def telegram_bot(events: list[str] | None = None):
    events = events if events is not None else []
    bot = SimpleNamespace()
    telegram_file = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"jpeg"))
    )
    bot.get_file = AsyncMock(return_value=telegram_file)

    async def send_photo(**kwargs):
        events.append("send")
        return SimpleNamespace(message_id=200)

    async def delete_message(chat_id, message_id):
        events.append(f"delete:{message_id}")
        return True

    bot.send_photo = AsyncMock(side_effect=send_photo)
    bot.delete_message = AsyncMock(side_effect=delete_message)
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="administrator")
    )
    return bot


class ConfigTests(unittest.TestCase):
    def test_settings_parse_and_hide_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Settings.from_env(
                {
                    "SALES_PHOTO_BOT_TOKEN": TOKEN,
                    "SALES_PHOTO_CHAT_ID": str(CHAT_ID),
                    "GEMINI_API_KEY": "gemini-secret",
                    "SERPER_API_KEY": "serper-secret",
                    "SALES_PHOTO_DB_PATH": str(Path(directory) / "db.sqlite"),
                    "SALES_PHOTO_ALLOWED_USER_IDS": "1,2,2",
                }
            )
        self.assertEqual(parsed.allowed_user_ids, frozenset({1, 2}))
        self.assertNotIn(TOKEN, repr(parsed))
        self.assertNotIn("gemini-secret", repr(parsed))
        self.assertNotIn("serper-secret", repr(parsed))
        self.assertEqual(parsed.heartbeat_path, Path("/tmp/sales-photo-heartbeat"))

    def test_settings_require_negative_chat_id_and_both_api_keys(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(
                {
                    "SALES_PHOTO_BOT_TOKEN": TOKEN,
                    "SALES_PHOTO_CHAT_ID": "123",
                    "GEMINI_API_KEY": "key",
                    "SERPER_API_KEY": "key",
                }
            )
        with self.assertRaisesRegex(ConfigError, "SERPER_API_KEY"):
            Settings.from_env(
                {
                    "SALES_PHOTO_BOT_TOKEN": TOKEN,
                    "SALES_PHOTO_CHAT_ID": str(CHAT_ID),
                    "GEMINI_API_KEY": "key",
                }
            )


class CaptionFormattingTests(unittest.TestCase):
    def test_complete_card(self):
        caption = build_caption(
            "+998 90 123 45 67",
            Recognition(
                model_name="Samsung Galaxy Tab A11",
                model_code="SM-X133",
                memory="4/64 GB",
                color="Silver",
            ),
        )
        self.assertEqual(
            caption,
            "📞 Клиент: +998 90 123 45 67\n"
            "📦 Samsung Galaxy Tab A11\n"
            "💾 4/64 GB\n"
            "🎨 Silver\n\n"
            "🛒💵:\n"
            "rasxod:\n\n"
            "<b>Наличка</b>\n"
            "💵:\n"
            "🇺🇿:\n\n"
            "<b>Card/Terminal/Paynet</b>\n"
            "💵:\n"
            "🇺🇿:",
        )

    def test_unknown_model_omits_model_but_keeps_independent_fields(self):
        caption = build_caption(None, Recognition(memory="256 GB", color="Black"))
        self.assertNotIn("📦", caption)
        self.assertIn("💾 256 GB", caption)
        self.assertIn("🎨 Black", caption)

    def test_raw_code_is_not_rendered_as_name(self):
        caption = build_caption(None, Recognition(model_code="SM-X133"))
        self.assertNotIn("SM-X133", caption)
        self.assertNotIn("📦", caption)

    def test_external_values_are_escaped_and_bounded(self):
        caption = build_caption(
            "<b>client</b> & " + "x" * 500,
            Recognition(model_name="Phone <script>"),
        )
        self.assertIn("&lt;b&gt;client&lt;/b&gt; &amp;", caption)
        self.assertIn("Phone &lt;script&gt;", caption)
        self.assertLess(len(caption), 1024)

    def test_manager_can_be_added_detected_and_removed(self):
        base = build_caption(None, Recognition(model_name="Product"))
        selected = add_manager_selection(base, "Olmas")
        self.assertEqual(selected_manager_from_caption(selected), "Olmas")
        self.assertEqual(remove_manager_selection(selected), base)


class RecognitionParsingTests(unittest.TestCase):
    def test_memory_normalization(self):
        self.assertEqual(normalize_memory("4GB | 64GB"), "4/64 GB")
        self.assertEqual(normalize_memory("256 гб"), "256 GB")
        self.assertEqual(normalize_memory("44 mm"), "")
        self.assertEqual(normalize_color("Titanium Gray"), "Titanium Gray")
        self.assertEqual(normalize_color("Starlight"), "Starlight")
        self.assertEqual(normalize_color("Made in China"), "")
        self.assertEqual(normalize_color("IMEI 490154203237518"), "")
        self.assertEqual(normalize_color("Black 256"), "")

    def test_json_and_commercial_name_validation(self):
        self.assertEqual(_parse_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(_valid_commercial_name("SM-X133", ["SM-X133"]), "")
        self.assertEqual(
            _valid_commercial_name(
                "Samsung SM-X133",
                ["SM-X133"],
                "Samsung",
            ),
            "",
        )
        self.assertEqual(
            _valid_commercial_name(
                "Samsung Galaxy Tab A11",
                ["SM-X133"],
                "Samsung",
            ),
            "Samsung Galaxy Tab A11",
        )

    def test_sensitive_numeric_identifiers_are_rejected(self):
        self.assertEqual(_identifier("490154203237518"), "")  # IMEI
        self.assertEqual(_identifier("8806097804598"), "")  # EAN
        self.assertEqual(_identifier("998901234567"), "")  # phone
        self.assertEqual(_identifier("IMEI490154203237518"), "")
        self.assertEqual(_identifier("EAN5901234123457"), "")
        self.assertEqual(_identifier("S/N:SN123456789"), "")
        self.assertEqual(_identifier("R8YL5OR510N"), "")
        self.assertEqual(_identifier("SM-X133"), "SM-X133")
        self.assertEqual(_identifier("ZX9A123456"), "")
        self.assertEqual(_identifier("SN850X"), "SN850X")
        self.assertEqual(_identifier("BX8071514900K"), "BX8071514900K")

    def test_commercial_name_rejects_listing_metadata_and_internal_codes(self):
        invalid = (
            "Samsung Galaxy S24 256 GB",
            "Samsung Galaxy S24 8/256 GB",
            "Samsung Galaxy S24 Black",
            "Samsung Galaxy S24 Global Version",
            "Samsung Galaxy S24 $799",
            "Buy Samsung Galaxy S24 Online",
            "Samsung Galaxy Tab A11 SM-X133",
            "Samsung Model SM-X133",
            "Samsung Galaxy S24 IMEI 490154203237518",
        )
        for name in invalid:
            with self.subTest(name=name):
                self.assertEqual(
                    _valid_commercial_name(name, ["SM-X133"], "Samsung"),
                    "",
                )

    def test_legitimate_commercial_codes_and_color_named_brands_are_allowed(self):
        cases = (
            ("Sony WH-1000XM5", ["WH-1000XM5"], "Sony"),
            ("HP LaserJet M404dn", ["M404dn"], "HP"),
            ("Black Shark 5 Pro", [], "Black Shark"),
            ("Orange Pi 5", [], "Orange Pi"),
            ("Red Magic 9 Pro", [], "Red Magic"),
            ("Apple iPhone SE", [], "Apple"),
            ("Google Pixel Fold", [], "Google"),
        )
        for name, identifiers, brand in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    _valid_commercial_name(name, identifiers, brand),
                    name,
                )

    def test_exact_name_matcher_does_not_collapse_variants(self):
        rejected = (
            ("Nintendo Switch 2", "Nintendo Switch"),
            ("Nintendo Switch (OLED)", "Nintendo Switch"),
            ("Samsung Galaxy S25 Edge", "Samsung Galaxy S25"),
            ("Samsung Galaxy A15 5G Smartphone", "Samsung Galaxy A15"),
            ("Apple Watch Series 10", "Apple Watch Series"),
            ("Apple iPhone 15 (Pro)", "Apple iPhone 15"),
            ("Samsung Galaxy S24+", "Samsung Galaxy S24"),
        )
        for field, base_name in rejected:
            with self.subTest(field=field):
                self.assertFalse(_field_supports_name(field, base_name))
        self.assertTrue(
            _field_supports_name(
                "Samsung Galaxy S24 - 5G smartphone",
                "Samsung Galaxy S24",
            )
        )

    def test_identifier_and_name_must_be_in_the_same_local_clause(self):
        unrelated = SearchResult(
            position=1,
            title="Related products",
            snippet=(
                "SM-X133 is discontinued. Customers also viewed "
                "Samsung Galaxy Tab A11."
            ),
            link="https://example.com/item",
            domain="example.com",
        )
        adjacent = search_result(
            1,
            "example.com",
            "SM-X133 Samsung Galaxy Tab A11",
        )
        self.assertFalse(
            _result_supports_identifier_and_name(
                unrelated,
                "SM-X133",
                "Samsung Galaxy Tab A11",
            )
        )
        self.assertTrue(
            _result_supports_identifier_and_name(
                adjacent,
                "SM-X133",
                "Samsung Galaxy Tab A11",
            )
        )

    def test_generic_visual_candidates_are_rejected(self):
        for candidate, brand in (
            ("Samsung Black Tablet", "Samsung"),
            ("Samsung Tablet 2026", "Samsung"),
            ("Samsung 128GB Tablet", "Samsung"),
            ("Samsung Model", "Samsung"),
            ("Samsung Best Tablet", "Samsung"),
            ("Android Phone", ""),
            ("Самсунг черный планшет", "Самсунг"),
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(
                    _strong_candidate(
                        _Extracted(brand=brand, candidate_name=candidate)
                    )
                )
        self.assertTrue(
            _strong_candidate(
                _Extracted(
                    brand="Black Shark",
                    candidate_name="Black Shark 5 Pro",
                )
            )
        )

    def test_public_suffix_domains_are_counted_correctly(self):
        self.assertEqual(_registrable_domain("a.shop.example.co.uk"), "example.co.uk")
        self.assertEqual(_registrable_domain("samsung.com.evil.com"), "evil.com")

    def test_cache_keys_are_hashed_and_policy_scoped(self):
        keys = _cache_keys(
            _Extracted(brand="Samsung", model_code="SM-X133", sku="SM-X133NZSAMEA")
        )
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(len(key) == 64 for key in keys))
        self.assertNotIn("SM-X133", " ".join(keys))


class GeminiRecognizerTests(unittest.IsolatedAsyncioTestCase):
    def recognizer_with_responses(self, search: FakeSearch, *responses):
        recognizer = object.__new__(GeminiProductRecognizer)
        recognizer.model = "gemini-test"
        recognizer.timeout_seconds = 5
        recognizer.minimum_confidence = 0.72
        recognizer.cache_days = 30
        recognizer.repository = None
        recognizer.search = search
        recognizer._generate = AsyncMock(side_effect=responses)
        return recognizer

    async def test_two_source_resolution_for_sample_product(self):
        results = (
            search_result(1, "samsung.com", "Samsung Galaxy Tab A11 SM-X133"),
            search_result(2, "gsmarena.com", "Samsung Galaxy Tab A11 SM-X133"),
        )
        resolver = SimpleNamespace(
            text=(
                '{"commercial_name":"Samsung Galaxy Tab A11",'
                '"confidence":0.99,"evidence_positions":[1,2]}'
            )
        )
        search = FakeSearch(results)
        recognizer = self.recognizer_with_responses(
            search, extraction_json(), resolver
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertEqual(result.model_name, "Samsung Galaxy Tab A11")
        self.assertEqual(result.model_code, "SM-X133")
        self.assertEqual(result.memory, "4/64 GB")
        self.assertEqual(result.color, "Silver")
        self.assertEqual(result.source_count, 2)
        self.assertIn('"SM-X133NZSAMEA"', search.queries[0])
        resolver_config = recognizer._generate.await_args_list[1].kwargs["config"]
        self.assertFalse(getattr(resolver_config, "tools", None))

    async def test_one_domain_or_unrelated_source_never_publishes_name(self):
        search = FakeSearch(
            (
                search_result(1, "shop.example.com", "Brand Phone ABC-1"),
                search_result(2, "cdn.shop.example.com", "Brand Phone ABC-1"),
                search_result(3, "other.net", "Different Product XYZ-9"),
            )
        )
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                brand="Brand",
                product_type="phone",
                model_code="ABC-1",
                sku="",
                sku_confidence=0,
                memory="256GB",
                color="Black",
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(result.memory, "256 GB")
        self.assertEqual(result.color, "Black")
        self.assertEqual(recognizer._generate.await_count, 1)

    async def test_invalid_evidence_positions_or_name_fail_closed(self):
        results = (
            search_result(1, "one.com", "Brand Phone ABC-1"),
            search_result(2, "two.net", "Brand Phone ABC-1"),
        )
        resolver = SimpleNamespace(
            text=(
                '{"commercial_name":"Totally Wrong Product",'
                '"confidence":1,"evidence_positions":[1,99]}'
            )
        )
        recognizer = self.recognizer_with_responses(
            FakeSearch(results),
            extraction_json(
                brand="Brand", model_code="ABC-1", sku="", sku_confidence=0
            ),
            resolver,
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(recognizer._generate.await_count, 2)

    async def test_similar_model_number_cannot_pass_name_verifier(self):
        results = (
            search_result(1, "one.com", "ABC-1 Apple iPhone 14 Pro Max"),
            search_result(2, "two.net", "ABC-1 Apple iPhone 14 Pro Max"),
        )
        resolver = SimpleNamespace(
            text=(
                '{"commercial_name":"Apple iPhone 15 Pro Max",'
                '"confidence":1,"evidence_positions":[1,2]}'
            )
        )
        recognizer = self.recognizer_with_responses(
            FakeSearch(results),
            extraction_json(
                brand="Apple", model_code="ABC-1", sku="", sku_confidence=0
            ),
            resolver,
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(recognizer._generate.await_count, 2)

    async def test_name_token_boundary_rejects_a11_vs_a110(self):
        results = (
            search_result(1, "one.com", "SM-X133 Samsung Galaxy Tab A110"),
            search_result(2, "two.net", "SM-X133 Samsung Galaxy Tab A110"),
        )
        resolver = SimpleNamespace(
            text=(
                '{"commercial_name":"Samsung Galaxy Tab A11",'
                '"confidence":1,"evidence_positions":[1,2]}'
            )
        )
        recognizer = self.recognizer_with_responses(
            FakeSearch(results), extraction_json(sku="", sku_confidence=0), resolver
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)

    async def test_variant_suffixes_must_match_exactly(self):
        cases = (
            (
                "Samsung",
                "ABC-1 Samsung Galaxy S24",
                "Samsung Galaxy S24+",
            ),
            (
                "Apple",
                "ABC-1 Apple iPhone 15 Pro Max",
                "Apple iPhone 15 Pro",
            ),
        )
        for brand, evidence_text, resolved_name in cases:
            with self.subTest(resolved_name=resolved_name):
                results = (
                    search_result(1, "one.com", evidence_text),
                    search_result(2, "two.net", evidence_text),
                )
                resolver = SimpleNamespace(
                    text=json.dumps(
                        {
                            "commercial_name": resolved_name,
                            "confidence": 1,
                            "evidence_positions": [1, 2],
                        }
                    )
                )
                recognizer = self.recognizer_with_responses(
                    FakeSearch(results),
                    extraction_json(
                        brand=brand,
                        model_code="ABC-1",
                        sku="",
                        sku_confidence=0,
                    ),
                    resolver,
                )
                result = await recognizer.recognize(b"image", "image/jpeg")
                self.assertIsNone(result.model_name)

    async def test_relationship_brand_and_privacy_evidence_fail_closed(self):
        cases = (
            (
                "Samsung",
                "ABC-1 vs Samsung Galaxy Tab A11",
                "Samsung Galaxy Tab A11",
            ),
            (
                "Samsung",
                "Samsung Galaxy Tab A11 ABC-1 Ultra",
                "Samsung Galaxy Tab A11",
            ),
            (
                "Samsung",
                "ABC-1 Acme Rocket Two",
                "Acme Rocket Two",
            ),
            (
                "Samsung",
                "ABC-1 Samsung Galaxy S24 IMEI 490154203237518",
                "Samsung Galaxy S24 IMEI 490154203237518",
            ),
        )
        for brand, evidence_text, resolved_name in cases:
            with self.subTest(evidence=evidence_text):
                results = (
                    search_result(1, "one.com", evidence_text),
                    search_result(2, "two.net", evidence_text),
                )
                resolver = SimpleNamespace(
                    text=json.dumps(
                        {
                            "commercial_name": resolved_name,
                            "confidence": 1,
                            "evidence_positions": [1, 2],
                        }
                    )
                )
                recognizer = self.recognizer_with_responses(
                    FakeSearch(results),
                    extraction_json(
                        brand=brand,
                        model_code="ABC-1",
                        sku="",
                        sku_confidence=0,
                    ),
                    resolver,
                )
                result = await recognizer.recognize(b"image", "image/jpeg")
                self.assertIsNone(result.model_name)

    async def test_sensitive_text_cannot_be_returned_as_color(self):
        recognizer = self.recognizer_with_responses(
            FakeSearch(),
            extraction_json(
                brand="",
                model_code="",
                model_confidence=0,
                sku="",
                sku_confidence=0,
                color="IMEI 490154203237518",
                color_confidence=0.99,
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.color)
        self.assertIsNone(result.model_name)

    async def test_provider_preflight_checks_model_and_search_key(self):
        search = FakeSearch()
        recognizer = object.__new__(GeminiProductRecognizer)
        recognizer.model = "gemini-test"
        recognizer.timeout_seconds = 5
        model_get = AsyncMock(return_value=SimpleNamespace(name="gemini-test"))
        recognizer._aio = SimpleNamespace(models=SimpleNamespace(get=model_get))
        recognizer.search = search
        await recognizer.preflight()
        model_get.assert_awaited_once_with(model="gemini-test")
        self.assertEqual(search.queries, ['"SM-X133" Samsung'])

    async def test_two_domains_must_support_the_same_identifier(self):
        search = FakeSearch(
            (
                search_result(
                    1,
                    "one.com",
                    "SKU-A1 Samsung Galaxy Tab A11",
                ),
                search_result(
                    2,
                    "two.net",
                    "MOD-B2 Samsung Galaxy Tab A11",
                ),
            )
        )
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                model_code="MOD-B2",
                sku="SKU-A1",
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(recognizer._generate.await_count, 1)

    async def test_only_verified_identifier_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            results = (
                search_result(
                    1,
                    "one.com",
                    "SKU-A1 Samsung Galaxy Tab A11",
                ),
                search_result(
                    2,
                    "two.net",
                    "SKU-A1 Samsung Galaxy Tab A11",
                ),
                search_result(
                    3,
                    "one.com",
                    "MOD-B2 Samsung Galaxy Tab A11",
                ),
            )
            resolver = SimpleNamespace(
                text=(
                    '{"commercial_name":"Samsung Galaxy Tab A11",'
                    '"confidence":1,"evidence_positions":[1,2]}'
                )
            )
            recognizer = self.recognizer_with_responses(
                FakeSearch(results),
                extraction_json(model_code="MOD-B2", sku="SKU-A1"),
                resolver,
            )
            recognizer.repository = repo
            result = await recognizer.recognize(b"image", "image/jpeg")
            self.assertEqual(result.model_name, "Samsung Galaxy Tab A11")
            sku_key, model_key = _cache_keys(
                _Extracted(
                    brand="Samsung",
                    product_type="tablet",
                    model_code="MOD-B2",
                    sku="SKU-A1",
                )
            )
            self.assertIsNotNone(repo.cached_name((sku_key,)))
            self.assertIsNone(repo.cached_name((model_key,)))

    async def test_conflicting_plus_variant_cache_entries_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            extracted = _Extracted(
                brand="Samsung",
                product_type="phone",
                model_code="CODE-1",
                sku="SKU-1",
            )
            sku_key, model_key = _cache_keys(extracted)
            expires = utc_now() + timedelta(days=1)
            repo.cache_name(
                (sku_key,),
                "Samsung Galaxy S24",
                0.9,
                2,
                "test",
                expires,
            )
            repo.cache_name(
                (model_key,),
                "Samsung Galaxy S24+",
                0.9,
                2,
                "test",
                expires,
            )
            recognizer = self.recognizer_with_responses(
                FakeSearch(),
                extraction_json(
                    brand="Samsung",
                    product_type="phone",
                    model_code="CODE-1",
                    sku="SKU-1",
                    memory="",
                    memory_confidence=0,
                    color="",
                    color_confidence=0,
                ),
            )
            recognizer.repository = repo
            result = await recognizer.recognize(b"image", "image/jpeg")
            self.assertIsNone(result.model_name)
            self.assertEqual(recognizer._generate.await_count, 1)

    async def test_search_budget_is_balanced_across_identifiers(self):
        class IdentifierSearch:
            def __init__(self):
                self.queries: list[str] = []

            async def search(self, query: str) -> tuple[SearchResult, ...]:
                self.queries.append(query)
                identifier = "SKU-A1" if "SKU-A1" in query else "MOD-B2"
                count = 10 if identifier == "SKU-A1" else 2
                domains = (
                    ["one.com"] * count
                    if identifier == "SKU-A1"
                    else ["two.net", "three.org"]
                )
                return tuple(
                    SearchResult(
                        position=index,
                        title=f"{identifier} Samsung Galaxy Tab A11",
                        snippet="",
                        link=f"https://{domain}/{identifier}/{index}",
                        domain=domain,
                    )
                    for index, domain in enumerate(domains, start=1)
                )

            async def aclose(self) -> None:
                return None

        search = IdentifierSearch()
        recognizer = object.__new__(GeminiProductRecognizer)
        recognizer.search = search
        evidence = await recognizer._search_evidence(
            _Extracted(
                brand="Samsung",
                product_type="tablet",
                model_code="MOD-B2",
                sku="SKU-A1",
            )
        )
        self.assertEqual(len(search.queries), 2)
        self.assertEqual(len(evidence), 7)
        self.assertTrue(any("MOD-B2" in result.title for result in evidence))

    async def test_identifier_requires_alphanumeric_boundary(self):
        results = (
            search_result(1, "one.com", "Brand Phone A150"),
            search_result(2, "two.net", "Brand Phone A150"),
        )
        recognizer = self.recognizer_with_responses(
            FakeSearch(results),
            extraction_json(
                brand="Brand", model_code="A15", sku="", sku_confidence=0
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(recognizer._generate.await_count, 1)

    async def test_generic_candidate_does_not_select_random_model(self):
        search = FakeSearch(
            (
                search_result(1, "one.example", "Samsung Galaxy Tab A9 tablet"),
                search_result(2, "two.example", "Samsung Galaxy Tab A9 tablet"),
            )
        )
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                model_code="",
                model_confidence=0,
                sku="",
                sku_confidence=0,
                candidate="Samsung Android tablet",
                candidate_confidence=0.99,
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(search.queries, [])

    async def test_candidate_only_path_cannot_change_the_printed_name(self):
        evidence_text = "Samsung Alpha One / Samsung Galaxy Tab A9"
        search = FakeSearch(
            (
                search_result(1, "one.com", evidence_text),
                search_result(2, "two.net", evidence_text),
            )
        )
        resolver = SimpleNamespace(
            text=(
                '{"commercial_name":"Samsung Galaxy Tab A9",'
                '"confidence":1,"evidence_positions":[1,2]}'
            )
        )
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                model_code="",
                model_kind="unknown",
                model_confidence=0,
                sku="",
                sku_kind="unknown",
                sku_confidence=0,
                candidate="Samsung Alpha One",
                candidate_confidence=0.99,
            ),
            resolver,
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(recognizer._generate.await_count, 1)

    async def test_weak_identifier_does_not_trigger_search(self):
        search = FakeSearch()
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                model_code="490154203237518",
                model_confidence=0.99,
                sku="",
                sku_confidence=0,
                candidate="Samsung tablet",
                candidate_confidence=0.5,
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(search.queries, [])

    async def test_unlabelled_alphanumeric_serial_does_not_trigger_search(self):
        search = FakeSearch()
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                model_code="ZX9A123456",
                model_kind="unknown",
                model_confidence=0.99,
                sku="",
                sku_kind="unknown",
                sku_confidence=0,
                candidate="",
                candidate_confidence=0,
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(search.queries, [])

    async def test_search_error_keeps_extracted_memory_and_color(self):
        search = FakeSearch(error=TimeoutError("search unavailable"))
        recognizer = self.recognizer_with_responses(
            search,
            extraction_json(
                brand="Brand",
                model_code="ABC-1",
                sku="",
                sku_confidence=0,
                memory="128GB",
                color="Blue",
            ),
        )
        result = await recognizer.recognize(b"image", "image/jpeg")
        self.assertIsNone(result.model_name)
        self.assertEqual(result.memory, "128 GB")
        self.assertEqual(result.color, "Blue")


class SerperClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_contract_and_bounded_response(self):
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "title": "Samsung Galaxy Tab A11 SM-X133",
                            "link": "https://samsung.com/item",
                            "snippet": "SM-X133 Samsung Galaxy Tab A11",
                        },
                        {
                            "title": "Unsafe",
                            "link": "http://unsafe.example/item",
                            "snippet": "ignored",
                        },
                    ]
                },
            )

        client = SerperProductSearch("secret", country="uz", language="ru")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            results = await client.search('"SM-X133" Samsung')
        finally:
            await client.aclose()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url, httpx.URL("https://google.serper.dev/search"))
        self.assertEqual(captured[0].headers["x-api-key"], "secret")
        self.assertNotIn("secret", str(captured[0].url))
        payload = json.loads(captured[0].content)
        self.assertEqual((payload["gl"], payload["hl"], payload["num"]), ("uz", "ru", 10))
        self.assertEqual(len(results), 1)

    async def test_401_is_not_retried_and_503_is_bounded(self):
        for status, expected_calls in ((401, 1), (503, 2)):
            calls = 0

            async def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(status, request=request)

            client = SerperProductSearch("secret")
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                with patch("sales_photo_bot.search.asyncio.sleep", new=AsyncMock()):
                    with self.assertRaises(httpx.HTTPStatusError):
                        await client.search("SM-X133")
            finally:
                await client.aclose()
            self.assertEqual(calls, expected_calls)


class RepositoryTests(unittest.TestCase):
    def test_job_manager_and_bootstrap_state_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repo = SalesPhotoRepository(path)
            self.assertFalse(repo.is_bootstrapped(BOT_ID, CHAT_ID))
            self.assertFalse(repo.is_bootstrapped(BOT_ID + 1, CHAT_ID))
            self.assertFalse(repo.is_bootstrapped(BOT_ID, CHAT_ID - 1))
            repo.mark_bootstrapped(BOT_ID, CHAT_ID)
            self.assertTrue(repo.claim_photo(CHAT_ID, 10, "file"))
            self.assertFalse(repo.claim_photo(CHAT_ID, 10, "file"))
            repo.mark_reposted(CHAT_ID, 10, 20)
            self.assertTrue(repo.is_replacement(CHAT_ID, 20))
            self.assertTrue(repo.set_manager(CHAT_ID, 20, "Ali"))
            self.assertEqual(repo.selected_manager(CHAT_ID, 20), "Ali")
            self.assertTrue(repo.clear_manager(CHAT_ID, 20, "Ali"))
            repo.mark_complete(CHAT_ID, 10)
            reopened = SalesPhotoRepository(path)
            self.assertTrue(reopened.is_bootstrapped(BOT_ID, CHAT_ID))
            self.assertTrue(reopened.is_replacement(CHAT_ID, 20))
            self.assertEqual(reopened.pending_deletions(CHAT_ID), ())

    def test_replacement_reconciliation_and_failed_job_reclaim(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            self.assertTrue(repo.claim_photo(CHAT_ID, 10, "file"))
            repo.mark_failed(CHAT_ID, 10, "NetworkError")
            self.assertTrue(repo.claim_photo(CHAT_ID, 10, "file"))
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200), "recorded")
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200), "same")
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 201), "conflict")
            self.assertEqual(
                [
                    job.message_id
                    for job in repo.pending_duplicate_cleanups(CHAT_ID)
                ],
                [201],
            )

    def test_callback_generation_is_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            repo.claim_photo(CHAT_ID, 10, "file")
            repo.mark_reposted(CHAT_ID, 10, 200)
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 0)
            self.assertTrue(repo.apply_manager_selection(CHAT_ID, 200, "Ali", 0))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 1)
            self.assertFalse(repo.apply_manager_selection(CHAT_ID, 200, "Abbos", 0))
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Ali")
            self.assertTrue(repo.apply_manager_clear(CHAT_ID, 200, 1))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 2)
            self.assertIsNone(repo.selected_manager(CHAT_ID, 200))

    def test_callback_transition_reservation_is_atomic_and_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            repo.claim_photo(CHAT_ID, 10, "file")
            repo.mark_reposted(CHAT_ID, 10, 200)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertFalse(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 1)
            self.assertTrue(repo.release_ui_transition(CHAT_ID, 200, 0))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 0)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Ali")
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 1))
            self.assertTrue(repo.commit_reserved_manager_clear(CHAT_ID, 200, 1))
            self.assertIsNone(repo.selected_manager(CHAT_ID, 200))

    def test_missing_or_wrong_repository_key_fails_closed(self):
        for replacement in (None, Fernet.generate_key()):
            with self.subTest(replacement=bool(replacement)):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "sales.db"
                    repo = SalesPhotoRepository(path)
                    repo.claim_photo(
                        CHAT_ID,
                        10,
                        "file",
                        source_file_id="telegram-file-id",
                    )
                    key_path = path.with_name("sales.db.key")
                    if replacement is None:
                        key_path.unlink()
                    else:
                        key_path.write_bytes(replacement)
                    with self.assertRaisesRegex(RuntimeError, "ключ|Ключ"):
                        SalesPhotoRepository(path)

    def test_legacy_database_without_key_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            with sqlite3.connect(path) as db:
                db.executescript(
                    """
                    CREATE TABLE sales_photo_jobs (
                        chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        source_file_unique_id TEXT NOT NULL,
                        replacement_message_id INTEGER,
                        status TEXT NOT NULL,
                        delete_attempts INTEGER NOT NULL DEFAULT 0,
                        last_error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(chat_id, source_message_id),
                        UNIQUE(chat_id, replacement_message_id)
                    );
                    INSERT INTO sales_photo_jobs(
                        chat_id,source_message_id,source_file_unique_id,
                        replacement_message_id,status,created_at,updated_at
                    ) VALUES(-1001234567890,10,'legacy',20,'complete','now','now');
                    """
                )
            repo = SalesPhotoRepository(path)
            self.assertTrue(repo.is_replacement(CHAT_ID, 20))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 20), 0)

    def test_corrupt_retry_payload_does_not_hide_later_valid_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            old = utc_now() - timedelta(minutes=5)
            for source_id in (10, 11):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    at=old,
                )
                repo.mark_failed(CHAT_ID, source_id, "temporary", at=old)
            with sqlite3.connect(repo.path) as db:
                db.execute(
                    "UPDATE sales_photo_jobs SET encrypted_payload=? "
                    "WHERE chat_id=? AND source_message_id=10",
                    (b"corrupt", CHAT_ID),
                )
                db.commit()
            pending = repo.retryable_photos(CHAT_ID)
            self.assertEqual([job.source_message_id for job in pending], [11])
            with sqlite3.connect(repo.path) as db:
                payload, attempts, error = db.execute(
                    "SELECT encrypted_payload,processing_attempts,last_error_code "
                    "FROM sales_photo_jobs WHERE chat_id=? AND source_message_id=10",
                    (CHAT_ID,),
                ).fetchone()
            self.assertEqual(payload, b"corrupt")
            self.assertEqual(attempts, 3)
            self.assertEqual(error, "retry_payload_invalid")

    def test_maintenance_queries_are_scoped_to_the_configured_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            other_chat = CHAT_ID - 1
            old = utc_now() - timedelta(minutes=5)
            for chat_id in (CHAT_ID, other_chat):
                repo.claim_photo(
                    chat_id,
                    10,
                    f"file-{chat_id}",
                    source_file_id=f"telegram-{chat_id}",
                    at=old,
                )
                repo.mark_failed(chat_id, 10, "temporary", at=old)
                repo.queue_duplicate_cleanup(chat_id, 500, at=old)
            self.assertEqual(
                {job.chat_id for job in repo.retryable_photos(CHAT_ID)},
                {CHAT_ID},
            )
            self.assertEqual(
                {job.chat_id for job in repo.pending_duplicate_cleanups(CHAT_ID)},
                {CHAT_ID},
            )

    def test_retry_payload_is_encrypted_durable_and_cleared_after_repost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repo = SalesPhotoRepository(path)
            secret_file_id = "telegram-secret-file-id"
            private_caption = "private client 998901234567"
            old = utc_now() - timedelta(minutes=5)
            self.assertTrue(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-file",
                    source_file_id=secret_file_id,
                    client_caption=private_caption,
                    message_thread_id=77,
                    at=old,
                )
            )
            repo.mark_failed(CHAT_ID, 10, "crash", at=old)

            stored_bytes = b"".join(
                candidate.read_bytes()
                for candidate in Path(directory).glob("sales.db*")
                if candidate.is_file()
            )
            self.assertNotIn(secret_file_id.encode(), stored_bytes)
            self.assertNotIn(private_caption.encode(), stored_bytes)
            self.assertEqual(
                path.with_name("sales.db.key").stat().st_mode & 0o777,
                0o600,
            )

            pending = repo.retryable_photos(CHAT_ID)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].source_file_id, secret_file_id)
            self.assertEqual(pending[0].client_caption, private_caption)
            self.assertEqual(pending[0].message_thread_id, 77)
            self.assertTrue(repo.claim_retry(CHAT_ID, 10, pending[0].attempts))
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200), "recorded")
            with sqlite3.connect(path) as db:
                payload = db.execute(
                    "SELECT encrypted_payload FROM sales_photo_jobs "
                    "WHERE chat_id=? AND source_message_id=?",
                    (CHAT_ID, 10),
                ).fetchone()[0]
            self.assertIsNone(payload)

    def test_photo_retry_is_bounded_to_three_processing_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            old = utc_now() - timedelta(minutes=5)
            self.assertTrue(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-file",
                    source_file_id="file-id",
                    at=old,
                )
            )
            repo.mark_failed(CHAT_ID, 10, "first", at=old)
            self.assertTrue(repo.claim_retry(CHAT_ID, 10, 1, at=old))
            repo.mark_failed(CHAT_ID, 10, "second", at=old)
            self.assertTrue(repo.claim_retry(CHAT_ID, 10, 2, at=old))
            repo.mark_failed(CHAT_ID, 10, "third", at=old)
            self.assertEqual(repo.retryable_photos(CHAT_ID), ())
            with sqlite3.connect(repo.path) as db:
                payload = db.execute(
                    "SELECT encrypted_payload FROM sales_photo_jobs "
                    "WHERE chat_id=? AND source_message_id=?",
                    (CHAT_ID, 10),
                ).fetchone()[0]
            self.assertIsNone(payload)
            self.assertFalse(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-file",
                    source_file_id="file-id",
                )
            )


class PhotoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_send_happens_before_original_delete_and_largest_photo_is_used(self):
        events: list[str] = []
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        recognizer = StaticRecognizer(
            Recognition(
                model_name="Samsung Galaxy Tab A11",
                memory="4/64 GB",
                color="Silver",
            )
        )
        service = SalesPhotoService(settings(self.root), repo, recognizer)
        bot = telegram_bot(events)
        await service.handle_photo(photo_message(), bot)
        self.assertEqual(events, ["send", "delete:10"])
        bot.get_file.assert_awaited_once_with("large")
        sent = bot.send_photo.await_args.kwargs
        self.assertEqual(sent["photo"], "large")
        self.assertTrue(sent["caption"].startswith(BOT_CARD_MARKER))
        self.assertIn("📦 Samsung Galaxy Tab A11", sent["caption"])
        self.assertEqual(len(sent["reply_markup"].inline_keyboard), 2)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))

    async def test_unknown_recognition_still_reposts_template_without_model(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(
            settings(self.root), repo, StaticRecognizer(error=TimeoutError())
        )
        bot = telegram_bot()
        await service.handle_photo(photo_message(caption=None), bot)
        caption = bot.send_photo.await_args.kwargs["caption"]
        self.assertNotIn("📦", caption)
        self.assertIn("🛒💵:", caption)
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_send_failure_never_deletes_original(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = RuntimeError("network")
        await service.handle_photo(photo_message(), bot)
        bot.delete_message.assert_not_awaited()

    async def test_ambiguous_ledger_failure_keeps_both_photos(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        with patch.object(
            repo,
            "record_replacement",
            side_effect=RuntimeError("ambiguous commit"),
        ):
            await service.handle_photo(photo_message(), bot)
        bot.send_photo.assert_awaited_once()
        bot.delete_message.assert_not_awaited()

    async def test_missing_ledger_job_keeps_the_new_card(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        with patch.object(repo, "record_replacement", return_value="missing"):
            await service.handle_photo(photo_message(), bot)
        bot.send_photo.assert_awaited_once()
        bot.delete_message.assert_not_awaited()

    async def test_delete_failure_is_persisted_for_retry(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.delete_message.side_effect = RuntimeError("no permission")
        await service.handle_photo(photo_message(), bot)
        pending = repo.pending_deletions(CHAT_ID)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_message_id, 10)

    async def test_stale_processing_job_is_quarantined_without_repost(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        old = utc_now() - timedelta(minutes=10)
        self.assertTrue(
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique-large",
                source_file_id="large",
                client_caption="client 42",
                at=old,
            )
        )
        self.assertEqual(
            repo.fail_stale_processing(
                CHAT_ID,
                utc_now() - timedelta(minutes=1),
                at=old,
            ),
            1,
        )
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        await service.retry_failed_photos(bot)
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))
        self.assertEqual(repo.retryable_photos(CHAT_ID), ())
        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        with sqlite3.connect(repo.path) as db:
            payload = db.execute(
                "SELECT encrypted_payload FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()[0]
        self.assertIsNone(payload)

    async def test_failed_duplicate_delete_is_durable_and_retried(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        repo.claim_photo(CHAT_ID, 10, "file")
        repo.mark_reposted(CHAT_ID, 10, 200)
        repo.mark_complete(CHAT_ID, 10)
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.delete_message.side_effect = RuntimeError("temporary")
        duplicate = photo_message(
            message_id=201,
            caption=BOT_CARD_MARKER + build_caption(None, Recognition()),
        )
        duplicate.reply_markup = manager_keyboard(
            10,
            0,
            repo.callback_signature(CHAT_ID, 10, 0),
        )
        await service.handle_photo(duplicate, bot)
        pending = repo.pending_duplicate_cleanups(CHAT_ID)
        self.assertEqual(
            [(job.message_id, job.attempts) for job in pending],
            [(201, 1)],
        )

        repo.mark_duplicate_cleanup_failed(
            CHAT_ID,
            201,
            at=utc_now() - timedelta(minutes=10),
        )
        bot.delete_message.side_effect = None
        bot.delete_message.return_value = True
        await service.retry_duplicate_cleanups(bot)
        self.assertEqual(repo.pending_duplicate_cleanups(CHAT_ID), ())
        bot.delete_message.assert_awaited_with(CHAT_ID, 201)

    async def test_duplicate_and_marked_bot_repost_do_not_recurse(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        original = photo_message()
        await service.handle_photo(original, bot)
        await service.handle_photo(original, bot)
        replacement = photo_message(
            message_id=201,
            caption=BOT_CARD_MARKER + build_caption(None, Recognition()),
        )
        replacement.from_user = None
        await service.handle_photo(replacement, bot)
        self.assertEqual(bot.send_photo.await_count, 1)

    async def test_marker_prevents_concurrent_repost_race_before_ledger_write(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        entered_send = asyncio.Event()
        release_send = asyncio.Event()

        async def slow_send(**kwargs):
            entered_send.set()
            await release_send.wait()
            return SimpleNamespace(message_id=200)

        bot.send_photo.side_effect = slow_send
        original_task = asyncio.create_task(service.handle_photo(photo_message(), bot))
        await entered_send.wait()
        replacement = photo_message(
            message_id=200,
            caption=BOT_CARD_MARKER + build_caption(None, Recognition()),
        )
        replacement.from_user = None
        replacement.reply_markup = manager_keyboard(
            10,
            0,
            repo.callback_signature(CHAT_ID, 10, 0),
        )
        await service.handle_photo(replacement, bot)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))
        release_send.set()
        await original_task
        self.assertEqual(bot.send_photo.await_count, 1)

    async def test_forged_generated_marker_cannot_claim_a_job(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        repo.claim_photo(CHAT_ID, 10, "file", source_file_id="large")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        forged = photo_message(
            message_id=200,
            caption=BOT_CARD_MARKER + build_caption(None, Recognition()),
        )
        forged.reply_markup = manager_keyboard(10, 0, "deadbeefdead")
        await service.handle_photo(forged, bot)
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))
        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_failed_photo_maintenance_processes_one_job_per_tick(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        old = utc_now() - timedelta(minutes=10)
        for source_id in (10, 11):
            repo.claim_photo(
                CHAT_ID,
                source_id,
                f"unique-{source_id}",
                source_file_id=f"file-{source_id}",
                at=old,
            )
            repo.mark_failed(CHAT_ID, source_id, "temporary", at=old)
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        await service.retry_failed_photos(bot)
        self.assertEqual(bot.send_photo.await_count, 1)

    async def test_ambiguous_network_send_is_not_retried(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = NetworkError("response lost")
        await service.handle_photo(photo_message(), bot)
        self.assertEqual(bot.send_photo.await_count, 1)
        self.assertEqual(repo.retryable_photos(CHAT_ID), ())
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))
        bot.delete_message.assert_not_awaited()

    async def test_retry_after_is_safely_retried_once(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = [
            RetryAfter(0.1),
            SimpleNamespace(message_id=200),
        ]
        with patch("sales_photo_bot.service.asyncio.sleep", new=AsyncMock()):
            await service.handle_photo(photo_message(), bot)
        self.assertEqual(bot.send_photo.await_count, 2)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_other_chat_is_ignored(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        message = photo_message()
        message.chat_id = -1001
        message.chat.id = -1001
        await service.handle_photo(message, bot)
        bot.send_photo.assert_not_awaited()

    async def test_preflight_requires_channel_and_admin_rights(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=777)),
            get_chat=AsyncMock(return_value=SimpleNamespace(id=CHAT_ID, type="supergroup")),
            get_chat_member=AsyncMock(),
        )
        with self.assertRaisesRegex(RuntimeError, "должен быть каналом"):
            await service.preflight(bot)

        bot.get_chat.return_value = SimpleNamespace(id=CHAT_ID, type="channel")
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", can_delete_messages=False, can_post_messages=True
        )
        with self.assertRaisesRegex(RuntimeError, "право удаления"):
            await service.preflight(bot)


class ManagerCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SalesPhotoRepository(self.root / "db.sqlite")
        self.repo.claim_photo(CHAT_ID, 10, "file")
        self.repo.mark_reposted(CHAT_ID, 10, 200)
        self.repo.mark_complete(CHAT_ID, 10)
        self.base = BOT_CARD_MARKER + build_caption(
            None, Recognition(model_name="Samsung Galaxy Tab A11")
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    def query(self, data: str, caption_html: str, actor_id: int = 50):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat_id=CHAT_ID,
                chat=SimpleNamespace(id=CHAT_ID),
                message_id=200,
                caption=caption_html,
                caption_html=caption_html,
            ),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
        )

    def callback(self, action: str, generation: int) -> str:
        signature = self.repo.callback_signature(CHAT_ID, 10, generation)
        return f"sp:{action}:10:{generation}:{signature}"

    def context(self, admin: bool = True):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(
                    status="administrator" if admin else "member"
                )
            )
        )
        return SimpleNamespace(bot=bot)

    async def test_manager_selection_and_back(self):
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        selected_query = self.query(self.callback("m:olmas", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=selected_query), self.context()
        )
        selected_caption = selected_query.edit_message_caption.await_args.kwargs["caption"]
        self.assertIn("👤 Менеджер: <b>Olmas</b>", selected_caption)
        back_markup = selected_query.edit_message_caption.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        self.assertEqual(
            back_markup["inline_keyboard"][0][0]["callback_data"],
            self.callback("b", 1),
        )
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Olmas")

        back_query = self.query(self.callback("b", 1), selected_caption)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=back_query), self.context()
        )
        self.assertEqual(
            back_query.edit_message_caption.await_args.kwargs["caption"], self.base
        )
        self.assertIsNone(self.repo.selected_manager(CHAT_ID, 200))

    async def test_non_admin_is_denied_when_allowlist_is_empty(self):
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        query = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=False)
        )
        query.edit_message_caption.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            "У вас нет доступа к выбору менеджера", show_alert=True
        )

    async def test_allowlist_is_additive_and_does_not_exclude_channel_admins(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({999})),
            self.repo,
            StaticRecognizer(),
        )
        query = self.query(self.callback("m:ali", 0), self.base, actor_id=50)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=True)
        )
        query.edit_message_caption.assert_awaited_once()
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_stale_callback_cannot_replace_newer_manager(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        second = self.query(self.callback("m:abbos", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=first), self.context(admin=False)
        )
        await service.on_manager_callback(
            SimpleNamespace(callback_query=second), self.context(admin=False)
        )
        second.edit_message_caption.assert_not_awaited()
        second.answer.assert_awaited_once_with(
            "Кнопка устарела",
            show_alert=True,
        )
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_post_edit_database_failure_still_blocks_stale_click(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        with patch.object(
            self.repo,
            "commit_reserved_manager_selection",
            side_effect=RuntimeError("database unavailable"),
        ):
            await service.on_manager_callback(
                SimpleNamespace(callback_query=first),
                self.context(admin=False),
            )
        selected_caption = first.edit_message_caption.await_args.kwargs["caption"]

        stale = self.query(self.callback("m:abbos", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=stale),
            self.context(admin=False),
        )
        stale.edit_message_caption.assert_not_awaited()
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        repair = self.query(self.callback("b", 1), selected_caption)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=repair),
            self.context(admin=False),
        )
        repair.edit_message_caption.assert_awaited_once()
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 2)

    async def test_ambiguous_manager_edit_invalidates_old_buttons_durably(self):
        first_service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        first.edit_message_caption.side_effect = NetworkError("response lost")
        await first_service.on_manager_callback(
            SimpleNamespace(callback_query=first),
            self.context(admin=False),
        )
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 1)

        restarted_service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        stale = self.query(self.callback("m:abbos", 0), self.base)
        await restarted_service.on_manager_callback(
            SimpleNamespace(callback_query=stale),
            self.context(admin=False),
        )
        stale.edit_message_caption.assert_not_awaited()
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

    async def test_definite_manager_edit_failure_releases_reservation(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        failed = self.query(self.callback("m:ali", 0), self.base)
        failed.edit_message_caption.side_effect = RuntimeError("rejected")
        await service.on_manager_callback(
            SimpleNamespace(callback_query=failed),
            self.context(admin=False),
        )
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 0)

        retry = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=retry),
            self.context(admin=False),
        )
        retry.edit_message_caption.assert_awaited_once()
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")


class ApplicationWiringTests(unittest.TestCase):
    def test_application_has_photo_callback_handlers_and_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            app = build_application(
                settings(root),
                repository=repo,
                recognizer=StaticRecognizer(),
            )
            self.assertEqual(len(app.handlers[0]), 2)
            self.assertEqual(app.update_processor.max_concurrent_updates, 4)
            keyboard = manager_keyboard().to_dict()
            self.assertEqual(
                [[button["text"] for button in row] for row in keyboard["inline_keyboard"]],
                [["Olmas", "Otabek"], ["Ali", "Abbos"]],
            )
            signature = repo.callback_signature(CHAT_ID, 10, 0)
            callback_data = f"sp:m:olmas:10:0:{signature}"
            self.assertIsNotNone(app.handlers[0][1].pattern.fullmatch(callback_data))


class StartupSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_start_is_not_marked_until_telegram_flush_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            service = SimpleNamespace(
                preflight=AsyncMock(return_value=BOT_ID),
                start_maintenance=MagicMock(),
            )
            bot = SimpleNamespace(
                delete_webhook=AsyncMock(side_effect=NetworkError("offline"))
            )
            with self.assertRaises(NetworkError):
                await _prepare_polling(settings(root), repo, service, bot)
            self.assertFalse(repo.is_bootstrapped(BOT_ID, CHAT_ID))
            service.start_maintenance.assert_not_called()

            bot.delete_webhook.side_effect = None
            bot.delete_webhook.return_value = True
            await _prepare_polling(settings(root), repo, service, bot)
            self.assertTrue(repo.is_bootstrapped(BOT_ID, CHAT_ID))
            bot.delete_webhook.assert_awaited_with(drop_pending_updates=True)
            service.start_maintenance.assert_called_once_with(bot)


if __name__ == "__main__":
    unittest.main()
