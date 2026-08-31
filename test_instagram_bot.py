import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


TEST_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
TEST_DB.close()
os.environ["INSTAGRAM_DB_PATH"] = TEST_DB.name

import instagram_bot


SHEET_CSV = '''priority,enabled,keywords,match_type,private_reply,public_reply
100,TRUE,"kredit, kreditga, рассрочка, bo'lib to'lash, bolip tolash",contains_any,Нет кредита,Ответили в Direct ✅
90,TRUE,"variant, варианты, rang, xotira",contains_any,Ответ про варианты,Ответили в Direct ✅
80,FALSE,narx,contains_any,Выключено,Ответили в Direct ✅
0,TRUE,DEFAULT,default,Общий ответ,Ответили в Direct ✅
'''

PRODUCTS_CSV = '''product_id,model_name,memory,color,price,warranty_period
1,Samsung Galaxy S25 5G,12/128Gb,Icy Blue,685,1
2,Samsung Galaxy S25 5G,12/256Gb,Mint,730,1
3,Samsung Galaxy S25 Ultra 5G,12/256Gb,Titanium Black,900,1
4,Samsung Galaxy S25 Plus 5G,12/256Gb,Navy,840,1
5,Samsung Galaxy S25 FE 5G,8/128Gb,White,565,1
6,Apple iPhone 17 Pro (Sim),256Gb,Silver,1390,1
7,Apple iPhone 17 Pro (eSim),256Gb,Silver,1310,1
8,Apple iPhone 17 Pro Max (Sim),256Gb,Silver,1510,1
9,Brand A56,128Gb,Black,300,1
10,Other A56,128Gb,Black,310,1
11,Xiaomi 17 Pro Max 5G (Global Rom),16/512Gb,Black,1160,1
12,Xiaomi Redmi Note 17 Pro Max 5G,8/256Gb,Black,467,1
13,Apple AirPods Pro 3 USB-C,,White,250,1
14,Apple AirPods 4 USB-C,,White,130,1
'''

SETTINGS_CSV = '''setting,value,updated_at
kurs,11900,2026-08-20
'''

POST_MODELS_CSV = '''enabled,media_id,permalink,models,caption,published_at,updated_at
TRUE,media-one,https://instagram.com/reel/one,"Samsung Galaxy S25, Samsung Galaxy S25 Ultra",Post one,2026-08-20,2026-08-20
FALSE,media-disabled,https://instagram.com/reel/two,Samsung Galaxy S25,Post two,2026-08-20,2026-08-20
TRUE,media-empty,https://instagram.com/reel/three,,Post three,2026-08-20,2026-08-20
'''

DIRECT_RULES_CSV = '''priority,enabled,keywords,match_type,reply_text,description
100,TRUE,"заказ, заказать, купить, buyurtma",contains_any,"Заказ: {manager_url}",Заказ
0,TRUE,DEFAULT,default,"Канал: {telegram_url}\nМенеджер: {manager_url}",По умолчанию
'''

DIRECT_SETTINGS_CSV = '''setting,value,description
telegram_url,https://t.me/texnikach,Канал
manager_url,https://t.me/texnikach_admin,Менеджер
model_intro,TEXNIKACH,Вступление
model_prices_label,Актуальные цены:,Цены
model_course_label,Курс:,Курс
model_footer,"Заказ: {manager_url}",Заказ модели
model_other_variants,Другие варианты у менеджера.,Сокращение
model_from_prefix,от,Префикс минимальной цены
model_empty_memory_label,Цена,Подпись без памяти
model_not_found_reply,"Уточните модель: {manager_url}",Модель не найдена
'''


class TriggerDetectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.product_catalog = instagram_bot.parse_product_catalog(
            PRODUCTS_CSV,
            SETTINGS_CSV,
        )
        cls.direct_config = {
            "rules": instagram_bot.parse_direct_rules_csv(
                DIRECT_RULES_CSV
            ),
            "settings": {
                **instagram_bot.LOCAL_DIRECT_SETTINGS,
                **instagram_bot.parse_direct_settings_csv(
                    DIRECT_SETTINGS_CSV
                ),
            },
        }

    def test_supported_generic_triggers(self):
        triggers = (
            "Нечпул",
            "nechpul",
            "necha pul",
            "Narx",
            "сколько стоит",
            "narxi qancha",
            "qancha turadi?",
            "+++",
        )

        for text in triggers:
            with self.subTest(text=text):
                self.assertTrue(instagram_bot.is_generic_trigger(text))

    def test_normal_comment_is_not_a_trigger(self):
        self.assertFalse(
            instagram_bot.is_generic_trigger("Классный телефон")
        )

    def test_sheet_rules_are_parsed_and_sorted(self):
        rules = instagram_bot.parse_rules_csv(SHEET_CSV)

        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[0]["priority"], 100)
        self.assertEqual(rules[-1]["match_type"], "default")

    def test_keyword_rule_has_priority_over_default(self):
        rules = instagram_bot.parse_rules_csv(SHEET_CSV)
        result = instagram_bot.resolve_response_rule(
            "Bolip tolash bormi?",
            rules=rules,
        )

        self.assertEqual(result["rule"]["private_reply"], "Нет кредита")
        self.assertEqual(result["source"], "google_sheet_rule_row_2")

    def test_unknown_comment_uses_sheet_default(self):
        rules = instagram_bot.parse_rules_csv(SHEET_CSV)
        result = instagram_bot.resolve_response_rule(
            "Классный телефон",
            rules=rules,
        )

        self.assertEqual(result["rule"]["private_reply"], "Общий ответ")
        self.assertEqual(result["source"], "google_sheet_default")

    def test_post_model_mappings_are_loaded_by_media_id(self):
        mappings = instagram_bot.parse_post_models_csv(
            POST_MODELS_CSV
        )

        self.assertEqual(
            mappings["media-one"],
            [
                "Samsung Galaxy S25",
                "Samsung Galaxy S25 Ultra",
            ],
        )
        self.assertNotIn("media-disabled", mappings)
        self.assertNotIn("media-empty", mappings)

    def test_post_mapping_resolves_multiple_real_models(self):
        mappings = instagram_bot.parse_post_models_csv(
            POST_MODELS_CSV
        )
        result = instagram_bot.resolve_post_model_families(
            "media-one",
            catalog=self.product_catalog,
            mappings=mappings,
        )

        self.assertEqual(
            [family["name"] for family in result["families"]],
            [
                "Samsung Galaxy S25",
                "Samsung Galaxy S25 Ultra",
            ],
        )

        message = instagram_bot.build_post_models_message(
            result["families"],
            self.product_catalog["kurs"],
        )

        self.assertIn("Samsung Galaxy S25", message)
        self.assertIn("Samsung Galaxy S25 Ultra", message)
        self.assertIn("So'm", message)
        self.assertNotIn("$", message)

    def test_base_model_does_not_fall_through_to_ultra(self):
        result = instagram_bot.find_price_model_in_text(
            "S25 narx",
            catalog=self.product_catalog,
        )

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["family"]["name"], "Samsung Galaxy S25")

    def test_model_suffixes_are_distinct(self):
        cases = {
            "S25 Ultra narx": "Samsung Galaxy S25 Ultra",
            "S25+ bormi": "Samsung Galaxy S25 Plus",
            "S25 FE": "Samsung Galaxy S25 FE",
            "17 pro": "Apple iPhone 17 Pro",
            "iPhone 17 pro max": "Apple iPhone 17 Pro Max",
            "Xiaomi 17 pro max": "Xiaomi 17 Pro Max",
            "Redmi Note 17 pro max": "Xiaomi Redmi Note 17 Pro Max",
            "AirPods Pro 3": "Apple AirPods Pro 3",
            "AirPods 4": "Apple AirPods 4",
        }

        for text, expected_model in cases.items():
            with self.subTest(text=text):
                result = instagram_bot.find_price_model_in_text(
                    text,
                    catalog=self.product_catalog,
                )
                self.assertEqual(result["status"], "found")
                self.assertEqual(result["family"]["name"], expected_model)

    def test_ambiguous_short_alias_is_not_guessed(self):
        result = instagram_bot.find_price_model_in_text(
            "A56 narx",
            catalog=self.product_catalog,
        )

        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["family"])

        result = instagram_bot.find_price_model_in_text(
            "17 Pro Max narx",
            catalog=self.product_catalog,
        )

        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["family"])

    def test_product_message_uses_live_price_and_course(self):
        result = instagram_bot.find_price_model_in_text(
            "S25 narx",
            catalog=self.product_catalog,
        )
        message = instagram_bot.build_live_product_message(
            result["family"],
            result["kurs"],
        )

        self.assertIn("Samsung Galaxy S25", message)
        self.assertIn("12/128Gb", message)
        self.assertIn("8 152 000 So'm", message)
        self.assertNotIn("$", message)
        self.assertLessEqual(
            len(message),
            instagram_bot.INSTAGRAM_PRICE_MESSAGE_LIMIT,
        )

    def test_large_product_message_is_safely_limited(self):
        family = {
            "name": "Test Phone Pro Max",
            "rows": [
                {
                    "model_name": f"Test Phone Pro Max Variant {index}",
                    "memory": f"{index + 1}Tb",
                    "price": instagram_bot.Decimal(1000 + index),
                }
                for index in range(80)
            ],
        }

        message = instagram_bot.build_live_product_message(
            family,
            instagram_bot.Decimal(11900),
        )

        self.assertLessEqual(
            len(message),
            instagram_bot.INSTAGRAM_PRICE_MESSAGE_LIMIT,
        )
        self.assertIn("Другие варианты", message)

    def test_fallback_direct_precedes_public_reply(self):
        calls = []

        with patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "find_price_model_in_text",
            return_value={
                "status": "not_found",
                "family": None,
                "alias": None,
            },
        ), patch.object(
            instagram_bot,
            "resolve_post_model_families",
            return_value={
                "families": [],
                "invalid_models": [],
                "configured_models": [],
            },
        ), patch.object(
            instagram_bot,
            "resolve_response_rule",
            return_value={
                "rule": {
                    "priority": 0,
                    "match_type": "default",
                    "private_reply": "Общий ответ",
                    "public_reply": "Ответили в Direct ✅",
                    "row_number": 5,
                },
                "source": "google_sheet_default",
            },
        ), patch.object(
            instagram_bot,
            "send_private_reply",
            side_effect=lambda *args: calls.append(("private", args[2])),
        ), patch.object(
            instagram_bot,
            "send_public_comment_reply",
            side_effect=lambda *args: calls.append(("public", args[1])),
        ), patch.object(instagram_bot, "update_comment_status"):
            instagram_bot.process_instagram_comment(
                instagram_account_id="account-id",
                commenter_id="customer-id",
                username="customer",
                media_id="media-id",
                comment_id="comment-id",
                comment_text="Нечпул",
            )

        self.assertEqual(
            calls,
            [
                ("private", "Общий ответ"),
                ("public", "Ответили в Direct ✅"),
            ],
        )

    def test_public_reply_is_not_sent_when_direct_fails(self):
        with patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "find_price_model_in_text",
            return_value={
                "status": "not_found",
                "family": None,
                "alias": None,
            },
        ), patch.object(
            instagram_bot,
            "resolve_post_model_families",
            return_value={
                "families": [],
                "invalid_models": [],
                "configured_models": [],
            },
        ), patch.object(
            instagram_bot,
            "resolve_response_rule",
            return_value={
                "rule": {
                    "priority": 80,
                    "match_type": "contains_any",
                    "private_reply": "Ответ про цену",
                    "public_reply": "Ответили в Direct ✅",
                    "row_number": 4,
                },
                "source": "google_sheet_rule_row_4",
            },
        ), patch.object(
            instagram_bot,
            "send_private_reply",
            side_effect=RuntimeError("direct failed"),
        ), patch.object(
            instagram_bot,
            "send_public_comment_reply",
        ) as public_reply, patch.object(
            instagram_bot,
            "update_comment_status",
        ):
            instagram_bot.process_instagram_comment(
                instagram_account_id="account-id",
                commenter_id="customer-id",
                username="customer",
                media_id="media-id",
                comment_id="comment-id",
                comment_text="Narx",
            )

        public_reply.assert_not_called()

    def test_product_response_precedes_google_sheet_rules(self):
        model_result = instagram_bot.find_price_model_in_text(
            "S25 narx",
            catalog=self.product_catalog,
        )
        sent_messages = []

        with patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "find_price_model_in_text",
            return_value=model_result,
        ), patch.object(
            instagram_bot,
            "resolve_post_model_families",
        ) as post_models, patch.object(
            instagram_bot,
            "resolve_response_rule",
        ) as sheet_rules, patch.object(
            instagram_bot,
            "send_private_reply",
            side_effect=lambda *args: sent_messages.append(args[2]),
        ) as private_reply, patch.object(
            instagram_bot,
            "send_public_comment_reply",
        ), patch.object(
            instagram_bot,
            "update_comment_status",
        ):
            instagram_bot.process_instagram_comment(
                instagram_account_id="account-id",
                commenter_id="customer-id",
                username="customer",
                media_id="media-id",
                comment_id="comment-id",
                comment_text="S25 narx",
            )

        post_models.assert_not_called()
        sheet_rules.assert_not_called()
        private_reply.assert_called_once()
        self.assertIn("Samsung Galaxy S25", sent_messages[0])
        self.assertNotIn("$", sent_messages[0])

    def test_post_mapping_precedes_response_rules(self):
        base_family = instagram_bot.find_price_model_in_text(
            "S25",
            catalog=self.product_catalog,
        )["family"]
        ultra_family = instagram_bot.find_price_model_in_text(
            "S25 Ultra",
            catalog=self.product_catalog,
        )["family"]
        sent_messages = []

        with patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "find_price_model_in_text",
            return_value={
                "status": "not_found",
                "family": None,
                "alias": None,
            },
        ), patch.object(
            instagram_bot,
            "resolve_post_model_families",
            return_value={
                "families": [base_family, ultra_family],
                "invalid_models": [],
                "configured_models": ["S25", "S25 Ultra"],
            },
        ), patch.object(
            instagram_bot,
            "resolve_response_rule",
        ) as sheet_rules, patch.object(
            instagram_bot,
            "send_private_reply",
            side_effect=lambda *args: sent_messages.append(args[2]),
        ), patch.object(
            instagram_bot,
            "send_public_comment_reply",
        ), patch.object(
            instagram_bot,
            "update_comment_status",
        ):
            instagram_bot.process_instagram_comment(
                instagram_account_id="account-id",
                commenter_id="customer-id",
                username="customer",
                media_id="media-id",
                comment_id="comment-id",
                comment_text="Narx",
            )

        sheet_rules.assert_not_called()
        self.assertIn("Samsung Galaxy S25", sent_messages[0])
        self.assertIn("Samsung Galaxy S25 Ultra", sent_messages[0])
        self.assertNotIn("$", sent_messages[0])

    def test_automatic_post_sync_appends_only_new_media(self):
        headers = [
            "enabled",
            "media_id",
            "permalink",
            "models",
            "caption",
            "published_at",
            "updated_at",
        ]

        class FakeWorksheet:
            def __init__(self):
                self.appended = []

            def get_all_values(self):
                return [
                    headers,
                    [
                        "TRUE",
                        "existing-media",
                        "https://instagram.com/reel/existing",
                        "",
                        "Existing",
                        "2026-08-19",
                        "2026-08-19",
                    ],
                ]

            def append_rows(self, rows, value_input_option):
                self.appended.extend(rows)
                self.value_input_option = value_input_option

        worksheet = FakeWorksheet()

        class FakeSpreadsheet:
            def worksheet(self, name):
                self.requested_sheet = name
                return worksheet

        spreadsheet = FakeSpreadsheet()

        class FakeClient:
            def open_by_key(self, spreadsheet_id):
                self.requested_id = spreadsheet_id
                return spreadsheet

        media = [
            {
                "id": "new-media",
                "permalink": "https://instagram.com/reel/new",
                "caption": "New post",
                "timestamp": "2026-08-20T10:00:00+0000",
            },
            {
                "id": "existing-media",
                "permalink": "https://instagram.com/reel/existing",
                "caption": "Existing",
                "timestamp": "2026-08-19T10:00:00+0000",
            },
        ]

        with patch.object(
            instagram_bot,
            "get_google_write_client",
            return_value=FakeClient(),
        ), patch.object(
            instagram_bot,
            "fetch_recent_instagram_media",
            return_value=media,
        ):
            result = instagram_bot.sync_instagram_posts_to_sheet()

        self.assertEqual(result["added"], 1)
        self.assertEqual(len(worksheet.appended), 1)
        self.assertEqual(worksheet.appended[0][1], "new-media")
        self.assertEqual(worksheet.appended[0][3], "")
        self.assertEqual(worksheet.value_input_option, "RAW")

    def test_direct_sheet_config_and_placeholders(self):
        rules = instagram_bot.parse_direct_rules_csv(
            DIRECT_RULES_CSV
        )
        settings = instagram_bot.parse_direct_settings_csv(
            DIRECT_SETTINGS_CSV
        )

        order = instagram_bot.resolve_direct_rule(
            "Хочу заказать телефон",
            rules=rules,
        )
        default = instagram_bot.resolve_direct_rule(
            "Привет",
            rules=rules,
        )

        self.assertEqual(order["source"], "direct_sheet_rule_row_2")
        self.assertEqual(default["source"], "direct_sheet_default")
        self.assertEqual(
            instagram_bot.render_direct_template(
                order["rule"]["reply_text"],
                settings,
            ),
            "Заказ: https://t.me/texnikach_admin",
        )
        self.assertIn(
            "https://t.me/texnikach",
            instagram_bot.render_direct_template(
                default["rule"]["reply_text"],
                settings,
            ),
        )

    def test_direct_known_model_sends_price_and_order_link(self):
        sent = []
        statuses = []

        with patch.object(
            instagram_bot,
            "get_direct_config",
            return_value=self.direct_config,
        ), patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "send_direct_message",
            side_effect=lambda *args: sent.append(args[2]),
        ), patch.object(
            instagram_bot,
            "update_direct_message_status",
            side_effect=lambda *args, **kwargs: statuses.append(
                (args, kwargs)
            ),
        ):
            instagram_bot.process_instagram_direct_message(
                instagram_account_id="account-id",
                sender_id="customer-id",
                message_id="direct-model",
                message_text="Хочу заказать S25",
            )

        self.assertEqual(len(sent), 1)
        self.assertIn("TEXNIKACH", sent[0])
        self.assertIn("Актуальные цены:", sent[0])
        self.assertIn("Курс:", sent[0])
        self.assertIn("Samsung Galaxy S25", sent[0])
        self.assertIn("8 152 000 So'm", sent[0])
        self.assertIn("https://t.me/texnikach_admin", sent[0])
        self.assertNotIn("$", sent[0])
        self.assertEqual(statuses[-1][0][1], "done")
        self.assertEqual(
            statuses[-1][1]["detection_source"],
            "direct_product_model",
        )

    def test_direct_plain_model_name_sends_price(self):
        sent = []

        with patch.object(
            instagram_bot,
            "get_direct_config",
            return_value=self.direct_config,
        ), patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "send_direct_message",
            side_effect=lambda *args: sent.append(args[2]),
        ), patch.object(
            instagram_bot,
            "update_direct_message_status",
        ):
            instagram_bot.process_instagram_direct_message(
                instagram_account_id="account-id",
                sender_id="customer-id",
                message_id="direct-plain-model",
                message_text="S25",
            )

        self.assertEqual(len(sent), 1)
        self.assertIn("Samsung Galaxy S25", sent[0])
        self.assertIn("So'm", sent[0])
        self.assertIn("https://t.me/texnikach_admin", sent[0])

    def test_direct_order_without_model_uses_order_rule(self):
        sent = []

        with patch.object(
            instagram_bot,
            "get_direct_config",
            return_value=self.direct_config,
        ), patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "send_direct_message",
            side_effect=lambda *args: sent.append(args[2]),
        ), patch.object(
            instagram_bot,
            "update_direct_message_status",
        ):
            instagram_bot.process_instagram_direct_message(
                instagram_account_id="account-id",
                sender_id="customer-id",
                message_id="direct-order",
                message_text="Хочу заказать телефон",
            )

        self.assertEqual(
            sent,
            ["Заказ: https://t.me/texnikach_admin"],
        )

    def test_direct_unknown_text_uses_sheet_default(self):
        sent = []

        with patch.object(
            instagram_bot,
            "get_direct_config",
            return_value=self.direct_config,
        ), patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "send_direct_message",
            side_effect=lambda *args: sent.append(args[2]),
        ), patch.object(
            instagram_bot,
            "update_direct_message_status",
        ):
            instagram_bot.process_instagram_direct_message(
                instagram_account_id="account-id",
                sender_id="customer-id",
                message_id="direct-default",
                message_text="Здравствуйте",
            )

        self.assertIn("https://t.me/texnikach", sent[0])
        self.assertIn("https://t.me/texnikach_admin", sent[0])

    def test_direct_ambiguous_model_is_not_guessed(self):
        sent = []

        with patch.object(
            instagram_bot,
            "get_direct_config",
            return_value=self.direct_config,
        ), patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "send_direct_message",
            side_effect=lambda *args: sent.append(args[2]),
        ), patch.object(
            instagram_bot,
            "update_direct_message_status",
        ):
            instagram_bot.process_instagram_direct_message(
                instagram_account_id="account-id",
                sender_id="customer-id",
                message_id="direct-ambiguous",
                message_text="A56 narx",
            )

        self.assertEqual(
            sent,
            ["Уточните модель: https://t.me/texnikach_admin"],
        )
        self.assertNotIn("Brand A56", sent[0])

    def test_direct_event_extraction_ignores_echo_and_service_events(self):
        account_id = instagram_bot.INSTAGRAM_ACCOUNT_ID
        entry = {
            "id": account_id,
            "messaging": [
                {
                    "sender": {"id": "customer-id"},
                    "recipient": {"id": account_id},
                    "message": {
                        "mid": "inbound-mid",
                        "text": "S25 narx",
                    },
                },
                {
                    "sender": {"id": account_id},
                    "recipient": {"id": "customer-id"},
                    "message": {
                        "mid": "echo-mid",
                        "text": "Ответ",
                        "is_echo": True,
                    },
                },
                {
                    "sender": {"id": "customer-id"},
                    "recipient": {"id": account_id},
                    "read": {"mid": "read-mid"},
                },
                {
                    "sender": {"id": "customer-id"},
                    "recipient": {"id": account_id},
                    "message_edit": {"mid": "edit-mid"},
                },
            ],
        }

        result = instagram_bot.extract_direct_message_events(entry)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message_id"], "inbound-mid")
        self.assertEqual(result[0]["sender_id"], "customer-id")

    def test_direct_message_claim_is_atomic_and_failed_can_retry(self):
        message_id = "direct-claim-mid"

        with instagram_bot.connect_instagram_db() as conn:
            conn.execute(
                "DELETE FROM instagram_processed_messages WHERE message_id = ?",
                (message_id,),
            )
            conn.commit()

        first = instagram_bot.claim_direct_message(
            message_id=message_id,
            sender_id="customer-id",
            recipient_id="account-id",
        )
        duplicate = instagram_bot.claim_direct_message(
            message_id=message_id,
            sender_id="customer-id",
            recipient_id="account-id",
        )

        instagram_bot.update_direct_message_status(
            message_id,
            "failed",
            error="temporary",
        )

        retry = instagram_bot.claim_direct_message(
            message_id=message_id,
            sender_id="customer-id",
            recipient_id="account-id",
        )

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertTrue(retry)

    def test_send_direct_message_uses_recipient_id(self):
        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {
                    "recipient_id": "customer-id",
                    "message_id": "sent-mid",
                }

        with patch.object(
            instagram_bot,
            "instagram_headers",
            return_value={"Authorization": "Bearer test"},
        ), patch.object(
            instagram_bot.HTTP,
            "post",
            return_value=FakeResponse(),
        ) as post:
            result = instagram_bot.send_direct_message(
                "account-id",
                "customer-id",
                "Hello",
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["recipient"], {"id": "customer-id"})
        self.assertNotIn("comment_id", payload["recipient"])
        self.assertEqual(payload["message"]["text"], "Hello")
        self.assertEqual(result["message_id"], "sent-mid")

    def test_long_direct_message_preserves_unique_links(self):
        manager_url = "https://t.me/texnikach_admin"
        channel_url = "https://t.me/texnikach"
        message = (
            "Очень длинный текст " * 100
            + "\n"
            + channel_url
            + "\n"
            + manager_url
            + "\n"
            + manager_url
        )

        result = instagram_bot.limit_direct_message(message)

        self.assertLessEqual(
            len(result),
            instagram_bot.INSTAGRAM_PRICE_MESSAGE_LIMIT,
        )
        self.assertIn(channel_url, result)
        self.assertIn(manager_url, result)
        self.assertEqual(result.count(manager_url), 1)

    def test_direct_config_uses_named_sheets_and_protects_links(self):
        settings_csv = '''setting,value,description
telegram_url,,Канал
manager_url,,Менеджер
model_intro,НОВЫЙ ТЕКСТ,Вступление
'''

        with patch.object(
            instagram_bot,
            "fetch_direct_sheet_csv",
            side_effect=[
                DIRECT_RULES_CSV,
                settings_csv,
            ],
        ) as fetch:
            config = instagram_bot.fetch_direct_config()

        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [
                instagram_bot.INSTAGRAM_DIRECT_RULES_SHEET_NAME,
                instagram_bot.INSTAGRAM_DIRECT_SETTINGS_SHEET_NAME,
            ],
        )
        self.assertEqual(config["settings"]["model_intro"], "НОВЫЙ ТЕКСТ")
        self.assertEqual(
            config["settings"]["telegram_url"],
            instagram_bot.LOCAL_DIRECT_SETTINGS["telegram_url"],
        )
        self.assertEqual(
            config["settings"]["manager_url"],
            instagram_bot.LOCAL_DIRECT_SETTINGS["manager_url"],
        )

    def test_direct_message_claim_is_concurrent(self):
        message_id = "direct-concurrent-mid"

        with instagram_bot.connect_instagram_db() as conn:
            conn.execute(
                "DELETE FROM instagram_processed_messages WHERE message_id = ?",
                (message_id,),
            )
            conn.commit()

        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            return instagram_bot.claim_direct_message(
                message_id=message_id,
                sender_id="customer-id",
                recipient_id="account-id",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))

        self.assertEqual(sorted(results), [False, True])

        instagram_bot.update_direct_message_status(
            message_id,
            "done",
        )

        self.assertFalse(
            instagram_bot.claim_direct_message(
                message_id=message_id,
                sender_id="customer-id",
                recipient_id="account-id",
            )
        )

    def test_direct_send_failure_marks_message_failed(self):
        statuses = []

        with patch.object(
            instagram_bot,
            "get_direct_config",
            return_value=self.direct_config,
        ), patch.object(
            instagram_bot,
            "get_product_catalog",
            return_value=self.product_catalog,
        ), patch.object(
            instagram_bot,
            "send_direct_message",
            side_effect=RuntimeError("temporary direct error"),
        ), patch.object(
            instagram_bot,
            "update_direct_message_status",
            side_effect=lambda *args, **kwargs: statuses.append(
                (args, kwargs)
            ),
        ):
            instagram_bot.process_instagram_direct_message(
                instagram_account_id="account-id",
                sender_id="customer-id",
                message_id="direct-failed",
                message_text="Здравствуйте",
            )

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0][0][1], "failed")
        self.assertIn("temporary direct error", statuses[0][1]["error"])

    def test_instagram_webhook_schedules_direct_once(self):
        account_id = instagram_bot.INSTAGRAM_ACCOUNT_ID
        payload = {
            "object": "instagram",
            "entry": [
                {
                    "id": account_id,
                    "messaging": [
                        {
                            "sender": {"id": "customer-id"},
                            "recipient": {"id": account_id},
                            "message": {
                                "mid": "webhook-direct-mid",
                                "text": "S25",
                            },
                        }
                    ],
                }
            ],
        }
        raw_body = json.dumps(payload).encode("utf-8")

        class FakeRequest:
            headers = {"X-Hub-Signature-256": "test"}

            async def body(self):
                return raw_body

            async def json(self):
                return payload

        first_tasks = instagram_bot.BackgroundTasks()

        with patch.object(
            instagram_bot,
            "INSTAGRAM_APP_SECRET",
            "configured-secret",
        ), patch.object(
            instagram_bot,
            "verify_meta_signature",
            return_value=True,
        ), patch.object(
            instagram_bot,
            "claim_direct_message",
            return_value=True,
        ) as claim:
            result = asyncio.run(
                instagram_bot.instagram_webhook_event(
                    FakeRequest(),
                    first_tasks,
                )
            )

        self.assertEqual(result, {"ok": True})
        claim.assert_called_once_with(
            message_id="webhook-direct-mid",
            sender_id="customer-id",
            recipient_id=account_id,
        )
        self.assertEqual(len(first_tasks.tasks), 1)
        self.assertIs(
            first_tasks.tasks[0].func,
            instagram_bot.process_instagram_direct_message,
        )
        self.assertEqual(
            first_tasks.tasks[0].kwargs["message_text"],
            "S25",
        )

        duplicate_tasks = instagram_bot.BackgroundTasks()

        with patch.object(
            instagram_bot,
            "INSTAGRAM_APP_SECRET",
            "configured-secret",
        ), patch.object(
            instagram_bot,
            "verify_meta_signature",
            return_value=True,
        ), patch.object(
            instagram_bot,
            "claim_direct_message",
            return_value=False,
        ):
            asyncio.run(
                instagram_bot.instagram_webhook_event(
                    FakeRequest(),
                    duplicate_tasks,
                )
            )

        self.assertEqual(len(duplicate_tasks.tasks), 0)


if __name__ == "__main__":
    unittest.main()
