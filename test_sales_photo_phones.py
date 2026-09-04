from __future__ import annotations

import unittest

from telegram import MessageEntity

from sales_photo_bot.phones import (
    extract_caption_phones,
    extract_product_label,
    extract_uzbek_phones,
    normalize_caption_phone_field,
    normalize_uzbek_phone,
    parse_caption_phone_field,
    phone_line,
)


BOT_CARD_MARKER = "\u2063\u2063"


def utf16_offset(value: str, needle: str) -> int:
    index = value.index(needle)
    return len(value[:index].encode("utf-16-le")) // 2


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


class NormalizeUzbekPhoneTests(unittest.TestCase):
    def test_normalizes_supported_international_and_national_forms(self):
        expected = "+998 90 123 45 67"
        values = (
            "+998 90 123 45 67",
            "+998(90)123-45-67",
            "998901234567",
            "998 90 123 45 67",
            "00998901234567",
            "00 998 (90) 123 45 67",
            "901234567",
            "90 123-45-67",
            "90‑123‑45‑67",
        )

        for value in values:
            with self.subTest(value=value):
                self.assertEqual(normalize_uzbek_phone(value), expected)

    def test_accepts_current_destination_codes_20_and_87(self):
        self.assertEqual(
            normalize_uzbek_phone("20 123 45 67"),
            "+998 20 123 45 67",
        )
        self.assertEqual(
            normalize_uzbek_phone("+998 87 765 43 21"),
            "+998 87 765 43 21",
        )
        self.assertEqual(
            normalize_uzbek_phone("90 000 00 00"),
            "+998 90 000 00 00",
        )

    def test_rejects_wrong_country_length_and_unallocated_shape(self):
        values = (
            "+7 916 123 45 67",
            "+90 123 45 67",
            "+998 90 123 45",
            "+998 90 123 45 678",
            "1234",
        )

        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(normalize_uzbek_phone(value))


class ExtractUzbekPhonesTests(unittest.TestCase):
    def test_extracts_two_numbers_in_source_order(self):
        self.assertEqual(
            extract_uzbek_phones(
                "клиенты: 90 123-45-67 / +998 (91) 765 43 21"
            ),
            (
                "+998 90 123 45 67",
                "+998 91 765 43 21",
            ),
        )

    def test_extracts_two_numbers_separated_only_by_whitespace(self):
        cases = (
            "901234567 917654321",
            "998901234567 998917654321",
            "90 123 45 67   91 765 43 21",
        )
        expected = (
            "+998 90 123 45 67",
            "+998 91 765 43 21",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(extract_uzbek_phones(value), expected)

    def test_deduplicates_the_same_number_written_differently(self):
        self.assertEqual(
            extract_uzbek_phones(
                "+998 90 123 45 67, 998901234567, 90-123-45-67"
            ),
            ("+998 90 123 45 67",),
        )

    def test_rejects_the_whole_value_when_more_than_two_numbers_exist(self):
        self.assertEqual(
            extract_uzbek_phones(
                "90 123 45 67 / 91 765 43 21 / 93 246 80 24"
            ),
            (),
        )

    def test_does_not_extract_from_imei_alphanumeric_token_or_foreign_number(self):
        values = (
            "IMEI: 490154203237518",
            "артикул SKU901234567X",
            "код ABC998901234567XYZ",
            "товарЖ901234567Я",
            "артикул SKU_901234567_X",
            "телефон +7 916 123 45 67",
        )

        for value in values:
            with self.subTest(value=value):
                self.assertEqual(extract_uzbek_phones(value), ())

    def test_phone_line_has_one_canonical_representation(self):
        self.assertEqual(phone_line(()), "📞:")
        self.assertEqual(
            phone_line(
                (
                    "+998 90 123 45 67",
                    "+998 91 765 43 21",
                )
            ),
            "📞: +998 90 123 45 67 / +998 91 765 43 21",
        )


class ProductLabelTests(unittest.TestCase):
    def test_short_model_is_preserved_and_phone_is_removed(self):
        self.assertEqual(extract_product_label("A16 8/256"), "A16 8/256")
        self.assertEqual(
            extract_product_label("iPhone 16 Pro 901234567"),
            "iPhone 16 Pro",
        )
        self.assertEqual(
            extract_product_label("A16 8/256 901234567"),
            "A16 8/256",
        )
        self.assertEqual(
            extract_uzbek_phones("A16 8/256 901234567"),
            ("+998 90 123 45 67",),
        )

    def test_phone_only_or_long_numeric_value_has_no_product_label(self):
        values = (
            "901234567",
            "+998 90 123 45 67",
            "90 123 45 67 / 91 765 43 21",
            "+7 916 123 45 67",
            "490154203237518",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(extract_product_label(value))

    def test_short_numeric_model_is_not_mistaken_for_phone(self):
        self.assertEqual(extract_product_label("16"), "16")


class CaptionPhoneFieldTests(unittest.TestCase):
    def test_multiline_supplier_section_is_structurally_valid(self):
        for extra_lines in (
            ("Supplier",),
            ("Supplier", "Keyboard gift"),
            ("Supplier", "Keyboard gift", "A33 8/256"),
        ):
            with self.subTest(extra_lines=extra_lines):
                caption = (
                    BOT_CARD_MARKER
                    + "🆔: 1\n\n🛒💵: Toshkent 170$\n"
                    + "\n".join(extra_lines)
                    + "\nrasxod:\n\n📞:+998901234567\n\nНаличка"
                )

                parsed = parse_caption_phone_field(caption)
                normalized = normalize_caption_phone_field(caption)

                self.assertEqual(parsed.state, "valid")
                self.assertEqual(parsed.phones, ("+998 90 123 45 67",))
                self.assertTrue(normalized.changed)
                self.assertIn("📞: +998 90 123 45 67", normalized.caption)

    def test_phone_like_values_outside_phone_row_are_ignored(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵: Supplier 998901112233\n"
            "IMEI 490154203237518\n"
            "rasxod: 998933334455\n\n"
            "📞: 90 123 45 67\n\n"
            "Наличка\n💵: 901234567\n🇺🇿: 998901112233"
        )

        self.assertEqual(
            extract_caption_phones(caption),
            ("+998 90 123 45 67",),
        )

    def test_malformed_structure_is_distinct_from_invalid_phone(self):
        malformed = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\nmanager note\n📞: 901234567\nНаличка"
        )
        duplicate = (
            BOT_CARD_MARKER
            + "🛒💵:\n🛒💵:\nrasxod:\n\n📞: 901234567\nНаличка"
        )
        invalid = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n📞: Akmal 901234567\nНаличка"
        )

        self.assertEqual(parse_caption_phone_field(malformed).state, "malformed")
        self.assertEqual(parse_caption_phone_field(duplicate).state, "malformed")
        self.assertEqual(parse_caption_phone_field(invalid).state, "invalid")

    def test_empty_and_no_phone_placeholders_are_conclusive(self):
        for value in ("", "-", "—", "Без номера"):
            with self.subTest(value=value):
                caption = (
                    BOT_CARD_MARKER
                    + "🛒💵:\nrasxod:\n\n📞: "
                    + value
                    + "\n\nНаличка"
                )

                parsed = parse_caption_phone_field(caption)

                self.assertEqual(parsed.state, "empty")
                self.assertTrue(parsed.conclusive)
                self.assertEqual(parsed.phones, ())

    def test_text_card_can_use_telegram_4096_character_limit(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n📞: 901234567\n\n"
            + "x" * 1200
        )

        caption_result = normalize_caption_phone_field(caption)
        text_result = normalize_caption_phone_field(
            caption,
            max_length=4096,
        )

        self.assertFalse(caption_result.changed)
        self.assertTrue(text_result.changed)
        self.assertIn("📞: +998 90 123 45 67", text_result.caption)

    def test_normalizes_card_with_date_and_product_before_header(self):
        caption = (
            BOT_CARD_MARKER
            + "📆: 31/08/2026\n\n"
            "📦 A16 8/256\n\n"
            "🛒💵:\nrasxod:\n\n📞: 901234567\n\nНаличка"
        )

        result = normalize_caption_phone_field(caption)

        self.assertTrue(result.changed)
        self.assertIn("📞: +998 90 123 45 67", result.caption)

    def test_normalizes_only_the_canonical_phone_field(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵: 90 111 22 33\n"
            "rasxod: 998901112233\n\n"
            "📞: 90 123-45-67\n\n"
            "Наличка\n"
            "💵: 901234567"
        )

        result = normalize_caption_phone_field(caption)

        self.assertTrue(result.changed)
        self.assertEqual(
            result.caption,
            BOT_CARD_MARKER
            + "🛒💵: 90 111 22 33\n"
            "rasxod: 998901112233\n\n"
            "📞: +998 90 123 45 67\n\n"
            "Наличка\n"
            "💵: 901234567",
        )

    def test_similar_labels_are_not_treated_as_the_card_phone_field(self):
        captions = (
            "📞 Клиент: 90 123 45 67",
            "Комментарий 📞: 90 123 45 67",
            "Телефон: 90 123 45 67",
        )

        for caption in captions:
            with self.subTest(caption=caption):
                result = normalize_caption_phone_field(caption)
                self.assertFalse(result.changed)
                self.assertEqual(result.caption, caption)

    def test_invalid_nonblank_phone_field_is_left_for_manager(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n📞: клиент 42\n\nНаличка"
        )

        result = normalize_caption_phone_field(caption)

        self.assertFalse(result.changed)
        self.assertEqual(result.caption, caption)

    def test_mixed_manual_phone_field_is_not_destructively_rewritten(self):
        captions = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n📞: Akmal 90 123 45 67\n\nНаличка",
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n"
            "📞: 90 123 45 67 / +7 916 123 45 67\n\nНаличка",
        )
        for caption in captions:
            with self.subTest(caption=caption):
                result = normalize_caption_phone_field(caption)
                self.assertFalse(result.changed)
                self.assertEqual(result.caption, caption)

    def test_already_canonical_phone_field_is_a_no_op(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n📞: +998 90 123 45 67\n\nНаличка"
        )
        entity = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(caption, "Наличка"),
            length=utf16_length("Наличка"),
        )

        result = normalize_caption_phone_field(caption, (entity,))

        self.assertFalse(result.changed)
        self.assertEqual(result.caption, caption)
        self.assertEqual(result.entities, (entity,))

    def test_more_than_two_numbers_in_phone_field_is_a_no_op(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵:\nrasxod:\n\n"
            "📞: 90 123 45 67 / 91 765 43 21 / 93 246 80 24\n\n"
            "Наличка"
        )

        result = normalize_caption_phone_field(caption)

        self.assertFalse(result.changed)
        self.assertEqual(result.caption, caption)

    def test_preserves_marker_and_shifts_telegram_entities_in_utf16_units(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵: ACME\n"
            "rasxod: 12\n\n"
            "📞: 901234567\n\n"
            "Наличка\n"
            "💵: 500"
        )
        before = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(caption, "ACME"),
            length=utf16_length("ACME"),
        )
        inside_phone_row = MessageEntity(
            type=MessageEntity.PHONE_NUMBER,
            offset=utf16_offset(caption, "901234567"),
            length=utf16_length("901234567"),
        )
        after = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(caption, "Наличка"),
            length=utf16_length("Наличка"),
        )

        result = normalize_caption_phone_field(
            caption,
            (before, inside_phone_row, after),
        )

        self.assertTrue(result.changed)
        self.assertTrue(result.caption.startswith(BOT_CARD_MARKER + "🛒💵:"))
        self.assertEqual(result.caption.count("\u2063"), 2)
        self.assertIn("📞: +998 90 123 45 67", result.caption)
        self.assertEqual(len(result.entities), 2)
        self.assertEqual(result.entities[0], before)
        self.assertEqual(result.entities[1].type, MessageEntity.BOLD)
        self.assertEqual(
            result.entities[1].offset,
            utf16_offset(result.caption, "Наличка"),
        )
        self.assertEqual(
            result.entities[1].length,
            utf16_length("Наличка"),
        )

    def test_entity_crossing_phone_row_makes_normalization_fail_closed(self):
        caption = BOT_CARD_MARKER + "🛒💵:\nrasxod:\n\n📞: 901234567\nПосле"
        crossing = MessageEntity(
            type=MessageEntity.BOLD,
            offset=utf16_offset(caption, "📞"),
            length=utf16_length("📞: 901234567\nПосле"),
        )

        result = normalize_caption_phone_field(caption, (crossing,))

        self.assertFalse(result.changed)
        self.assertEqual(result.caption, caption)
        self.assertEqual(result.entities, (crossing,))


if __name__ == "__main__":
    unittest.main()
