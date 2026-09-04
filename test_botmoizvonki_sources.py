import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_IMPORT_TMP = tempfile.TemporaryDirectory()
_IMPORT_DIR = Path(_IMPORT_TMP.name)

os.environ["DB_PATH"] = str(_IMPORT_DIR / "calls.db")
os.environ["INSTAGRAM_DB_PATH"] = str(_IMPORT_DIR / "instagram.db")
os.environ["REVIEWS_DB_PATH"] = str(_IMPORT_DIR / "reviews.db")
os.environ["BUSINESS_DB_PATH"] = str(_IMPORT_DIR / "business.db")
os.environ["PRODUCT_URLS_PATH"] = str(_IMPORT_DIR / "products.xlsx")
os.environ["TRANSCRIPTION_ENABLED"] = "false"

import botmoizvonki as bot


class CallSourceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.DB_PATH = Path(self.tmp.name) / "calls.db"
        bot.SALES_PHOTO_DB_PATH = (
            Path(self.tmp.name) / "sales_photo.db"
        )
        bot.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def event(db_call_id, client_number, slot, src_number="+998000000000"):
        return {
            "db_call_id": db_call_id,
            "event_pbx_call_id": f"pbx-{db_call_id}",
            "client_number": client_number,
            "direction": 0,
            "answered": 1,
            "src_number": src_number,
            "src_slot": slot,
            "start_time": 1_800_000_000 + db_call_id,
            "answer_time": 1_800_000_001 + db_call_id,
            "end_time": 1_800_000_010 + db_call_id,
            "duration": 9,
        }

    @staticmethod
    def webhook(login):
        return {
            "action": "call.finish",
            "user_login": login,
            "account_id": 98073,
            "account_name": "texnikachuz",
        }

    def save(self, login, event):
        return bot.save_call(
            self.webhook(login),
            event,
            9,
            "api",
        )

    def save_sales_cards(self, cards):
        with bot.sqlite3.connect(
            bot.SALES_PHOTO_DB_PATH
        ) as conn:
            conn.execute(
                """
                CREATE TABLE sales_photo_jobs (
                    chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    replacement_message_id INTEGER,
                    manager TEXT,
                    client_phone TEXT,
                    client_phone_2 TEXT,
                    product_label TEXT,
                    sale_date TEXT NOT NULL,
                    created_at TEXT,
                    status TEXT NOT NULL,
                    order_removed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO sales_photo_jobs (
                    chat_id,
                    source_message_id,
                    replacement_message_id,
                    manager,
                    client_phone,
                    client_phone_2,
                    product_label,
                    sale_date,
                    created_at,
                    status,
                    order_removed
                )
                VALUES (
                    -100,
                    :source_message_id,
                    :replacement_message_id,
                    :manager,
                    :client_phone,
                    :client_phone_2,
                    :product_label,
                    :sale_date,
                    :created_at,
                    'complete',
                    0
                )
                """,
                cards,
            )

    def test_main_account_corrects_both_sim_slots_and_assigns_abbos(self):
        first = self.save(
            "TEXNIKACH@gmail.com",
            self.event(1, "+998900000001", 0),
        )
        second = self.save(
            "texnikach@gmail.com",
            self.event(2, "+998900000002", 1),
        )

        call_one = bot.get_call(first["call_id"])
        call_two = bot.get_call(second["call_id"])

        self.assertEqual(call_one["src_number"], "+998998446162")
        self.assertEqual(call_two["src_number"], "+998901313999")
        self.assertEqual(call_one["provider_src_number"], "+998000000000")
        self.assertEqual(call_one["device_name"], "Poco")
        self.assertEqual(call_one["talk_manager_code"], "abbos")
        self.assertEqual(call_two["talk_manager_code"], "abbos")
        self.assertEqual(call_one["effective_lead_source_code"], "olx")
        self.assertIsNone(call_two["effective_lead_source_code"])

        keyboard = bot.build_call_state_keyboard(call_one)
        button_texts = {
            button["text"]
            for row in keyboard["inline_keyboard"]
            for button in row
        }
        self.assertIn("👤 Менеджер: Abbos", button_texts)
        self.assertIn("📣 Источник: OLX", button_texts)
        self.assertTrue(
            {f"{score} ★" for score in range(1, 6)}
            .issubset(button_texts)
        )
        self.assertNotIn("✅ Купил", button_texts)
        self.assertNotIn("🕓 В работе / ожидает", button_texts)

    def test_temporary_device_manager_uses_call_start_and_expires_at_midnight(self):
        event = self.event(
            10,
            "+998900000010",
            0,
        )
        assignment_start = (
            event["start_time"]
            - 10
        )

        assignment = (
            bot.set_device_manager_assignment(
                "texnikacholx@gmail.com",
                "ali",
                changed_by="test-admin",
                now_ts=assignment_start,
            )
        )

        before = self.event(
            11,
            "+998900000011",
            0,
        )
        before["start_time"] = (
            assignment_start
            - 1
        )

        before_call = self.save(
            "texnikacholx@gmail.com",
            before,
        )
        active_call = self.save(
            "texnikacholx@gmail.com",
            event,
        )

        expired = self.event(
            12,
            "+998900000012",
            0,
        )
        expired["start_time"] = (
            assignment["effective_until"]
            + 1
        )
        expired_call = self.save(
            "texnikacholx@gmail.com",
            expired,
        )

        self.assertEqual(
            bot.get_call(before_call["call_id"])[
                "talk_manager_code"
            ],
            "otabek",
        )
        self.assertEqual(
            bot.get_call(active_call["call_id"])[
                "talk_manager_code"
            ],
            "ali",
        )
        self.assertEqual(
            bot.get_call(expired_call["call_id"])[
                "talk_manager_code"
            ],
            "otabek",
        )
        self.assertEqual(
            assignment["effective_until"],
            bot.next_uz_midnight_timestamp(
                assignment_start
            ),
        )

    def test_reset_restores_default_without_changing_existing_calls(self):
        event = self.event(
            13,
            "+998900000013",
            0,
        )
        assignment_start = (
            event["start_time"]
            - 1
        )

        bot.set_device_manager_assignment(
            "texnikach@gmail.com",
            "ali",
            changed_by="test-admin",
            now_ts=assignment_start,
        )
        temporary_call = self.save(
            "texnikach@gmail.com",
            event,
        )

        bot.reset_device_manager_assignment(
            "texnikach@gmail.com",
            now_ts=(
                event["start_time"]
                + 1
            ),
        )

        after = self.event(
            14,
            "+998900000014",
            0,
        )
        after["start_time"] = (
            event["start_time"]
            + 2
        )
        default_call = self.save(
            "texnikach@gmail.com",
            after,
        )

        bot.init_db()

        self.assertEqual(
            bot.get_call(temporary_call["call_id"])[
                "talk_manager_code"
            ],
            "ali",
        )
        self.assertEqual(
            bot.get_call(default_call["call_id"])[
                "talk_manager_code"
            ],
            "abbos",
        )

        with bot.connect_db() as conn:
            history = conn.execute(
                """
                SELECT *
                FROM device_manager_assignments
                WHERE user_login = ?
                """,
                (
                    "texnikach@gmail.com",
                ),
            ).fetchall()

        self.assertEqual(len(history), 1)
        self.assertEqual(
            history[0]["changed_by"],
            "test-admin",
        )
        self.assertEqual(
            history[0]["effective_until"],
            event["start_time"] + 1,
        )
        self.assertEqual(
            history[0]["ended_by"],
            "dashboard",
        )

    def test_permanent_manager_survives_restart_and_uses_call_start_time(self):
        change_time = 1_800_000_100

        before = self.event(
            40,
            "+998900000040",
            0,
        )
        before["start_time"] = change_time - 1

        bot.set_permanent_device_manager(
            "texnikacholx@gmail.com",
            "ali",
            changed_by="owner",
            now_ts=change_time,
        )
        bot.init_db()

        delayed_old_call = self.save(
            "texnikacholx@gmail.com",
            before,
        )

        after = self.event(
            41,
            "+998900000041",
            0,
        )
        after["start_time"] = change_time + 1
        new_call = self.save(
            "texnikacholx@gmail.com",
            after,
        )

        self.assertEqual(
            bot.get_call(delayed_old_call["call_id"])[
                "talk_manager_code"
            ],
            "otabek",
        )
        self.assertEqual(
            bot.get_call(new_call["call_id"])[
                "talk_manager_code"
            ],
            "ali",
        )

        current = bot.get_effective_device_manager(
            "texnikacholx@gmail.com",
            change_time + 2,
        )
        self.assertEqual(current["manager_code"], "ali")
        self.assertTrue(current["permanent_custom"])

    def test_temporary_manager_overrides_permanent_then_returns_to_it(self):
        change_time = 1_800_000_200

        bot.set_permanent_device_manager(
            "texnikach@gmail.com",
            "olmas",
            now_ts=change_time,
        )
        temporary = bot.set_device_manager_assignment(
            "texnikach@gmail.com",
            "ali",
            now_ts=change_time + 1,
        )

        active = bot.get_effective_device_manager(
            "texnikach@gmail.com",
            change_time + 2,
        )
        expired = bot.get_effective_device_manager(
            "texnikach@gmail.com",
            temporary["effective_until"] + 1,
        )

        self.assertEqual(active["manager_code"], "ali")
        self.assertTrue(active["temporary"])
        self.assertEqual(
            active["permanent_manager_code"],
            "olmas",
        )
        self.assertEqual(expired["manager_code"], "olmas")
        self.assertFalse(expired["temporary"])
        self.assertTrue(expired["permanent_custom"])

    def test_reset_permanent_manager_restores_configured_manager(self):
        change_time = 1_800_000_300

        bot.set_permanent_device_manager(
            "texnikach@gmail.com",
            "ali",
            changed_by="owner",
            now_ts=change_time,
        )
        result = bot.reset_permanent_device_manager(
            "texnikach@gmail.com",
            changed_by="owner",
            now_ts=change_time + 1,
        )

        self.assertEqual(result["manager_code"], "abbos")
        self.assertFalse(result["permanent_custom"])

        with bot.connect_db() as conn:
            current_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM device_manager_defaults
                WHERE user_login = ?
                """,
                ("texnikach@gmail.com",),
            ).fetchone()[0]
            history = conn.execute(
                """
                SELECT action, old_manager_code, new_manager_code
                FROM device_manager_default_history
                WHERE user_login = ?
                ORDER BY id
                """,
                ("texnikach@gmail.com",),
            ).fetchall()

        self.assertEqual(current_count, 0)
        self.assertEqual(
            [row["action"] for row in history],
            ["set", "reset"],
        )
        self.assertEqual(
            history[-1]["new_manager_code"],
            "abbos",
        )

    def test_dashboard_manager_updates_need_no_password_but_check_origin(self):
        class FakeRequest:
            def __init__(self, origin=""):
                self.headers = {
                    "Host": "bot.texnikach.uz",
                }
                if origin:
                    self.headers["Origin"] = origin

        bot.require_dashboard_same_origin(
            FakeRequest()
        )
        bot.require_dashboard_same_origin(
            FakeRequest(
                "https://bot.texnikach.uz"
            )
        )

        with self.assertRaises(
            bot.HTTPException
        ) as forbidden:
            bot.require_dashboard_same_origin(
                FakeRequest(
                    "https://example.com"
                )
            )

        self.assertEqual(
            forbidden.exception.status_code,
            403,
        )

    def test_only_configured_admin_can_rate_manager(self):
        saved = self.save(
            "texnikach@gmail.com",
            self.event(20, "+998900000020", 0),
        )

        with self.assertRaises(PermissionError):
            bot.mark_manager_rating(
                saved["call_id"],
                5,
                {"id": 999, "username": "other"},
            )

        with bot.connect_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM manager_ratings"
            ).fetchone()[0]

        self.assertEqual(count, 0)

    def test_admin_rating_replaces_same_client_window_and_updates_stats(self):
        first = self.save(
            "texnikach@gmail.com",
            self.event(21, "+998900000021", 0),
        )
        second = self.save(
            "texnikach@gmail.com",
            self.event(22, "+998900000021", 0),
        )
        admin = {
            "id": bot.MANAGER_RATING_ADMIN_ID,
            "username": "abbos",
        }

        created = bot.mark_manager_rating(
            first["call_id"],
            3,
            admin,
        )
        replaced = bot.mark_manager_rating(
            second["call_id"],
            5,
            admin,
        )

        self.assertFalse(created["replaced"])
        self.assertTrue(replaced["replaced"])

        first_call = bot.get_call(first["call_id"])
        second_call = bot.get_call(second["call_id"])

        self.assertEqual(first_call["effective_manager_rating"], 5)
        self.assertEqual(second_call["effective_manager_rating"], 5)
        self.assertEqual(
            second_call["effective_manager_rating_call_id"],
            second["call_id"],
        )

        with bot.connect_db() as conn:
            rows = conn.execute(
                "SELECT * FROM manager_ratings"
            ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 5)
        self.assertEqual(
            rows[0]["marked_by"],
            bot.MANAGER_RATING_ADMIN_ID,
        )

        period = {
            "period": "custom",
            "date_from": "2027-01-01",
            "date_to": "2027-02-01",
        }
        manager = bot.stats_managers(**period)["results"][0]
        summary = bot.stats(**period)["stats"]
        daily = bot.stats_manager_ratings_daily(
            **period
        )["results"]
        recent = bot.stats_recent(
            **period
        )["results"]

        self.assertEqual(manager["manager_average_rating"], 5.0)
        self.assertEqual(manager["manager_ratings_count"], 1)
        self.assertEqual(summary["manager_average_rating"], 5.0)
        self.assertEqual(summary["manager_ratings_count"], 1)
        self.assertEqual(daily[0]["average_rating"], 5.0)
        self.assertEqual(daily[0]["ratings_count"], 1)
        self.assertEqual(
            {row["manager_rating"] for row in recent},
            {5},
        )

        keyboard = bot.build_call_state_keyboard(
            second_call
        )
        button_texts = {
            button["text"]
            for row in keyboard["inline_keyboard"]
            for button in row
        }
        self.assertIn("✅ 5 ★", button_texts)

    def test_changing_manager_updates_existing_internal_rating(self):
        saved = self.save(
            "texnikach@gmail.com",
            self.event(23, "+998900000023", 0),
        )
        admin = {
            "id": bot.MANAGER_RATING_ADMIN_ID,
            "username": "abbos",
        }

        bot.mark_manager_rating(
            saved["call_id"],
            4,
            admin,
        )
        bot.mark_talk_manager(
            saved["call_id"],
            "olmas",
            admin,
        )

        with bot.connect_db() as conn:
            rating = conn.execute(
                "SELECT * FROM manager_ratings"
            ).fetchone()

        self.assertEqual(rating["talk_manager_code"], "olmas")
        self.assertEqual(rating["talk_manager_name"], "Olmas")

    def test_rating_callback_rejects_other_telegram_users(self):
        saved = self.save(
            "texnikach@gmail.com",
            self.event(24, "+998900000024", 0),
        )

        class FakeRequest:
            headers = {}

            async def json(self):
                return {
                    "callback_query": {
                        "id": "callback-24",
                        "data": (
                            "manager_rating:5:"
                            f'{saved["call_id"]}'
                        ),
                        "from": {
                            "id": 999,
                            "username": "other",
                        },
                        "message": {
                            "message_id": 24,
                            "chat": {"id": -100},
                        },
                    }
                }

        with mock.patch.object(
            bot,
            "answer_callback_query",
        ) as answer, mock.patch.object(
            bot,
            "edit_reply_markup",
        ) as edit:
            result = asyncio.run(
                bot.telegram_webhook(
                    FakeRequest()
                )
            )

        self.assertTrue(result["forbidden"])
        answer.assert_called_once_with(
            "callback-24",
            "Только Abbos может ставить оценку",
        )
        edit.assert_not_called()

        with bot.connect_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM manager_ratings"
            ).fetchone()[0]

        self.assertEqual(count, 0)

    def test_admin_rating_callback_saves_score_and_old_sales_button_is_disabled(self):
        saved = self.save(
            "texnikach@gmail.com",
            self.event(25, "+998900000025", 0),
        )

        class FakeRequest:
            headers = {}

            def __init__(self, callback_data):
                self.callback_data = callback_data

            async def json(self):
                return {
                    "callback_query": {
                        "id": "callback-25",
                        "data": self.callback_data,
                        "from": {
                            "id": bot.MANAGER_RATING_ADMIN_ID,
                            "username": "abbos",
                        },
                        "message": {
                            "message_id": 25,
                            "chat": {"id": -100},
                        },
                    }
                }

        with mock.patch.object(
            bot,
            "answer_callback_query",
        ), mock.patch.object(
            bot,
            "edit_reply_markup",
        ):
            rated = asyncio.run(
                bot.telegram_webhook(
                    FakeRequest(
                        "manager_rating:4:"
                        f'{saved["call_id"]}'
                    )
                )
            )
            disabled = asyncio.run(
                bot.telegram_webhook(
                    FakeRequest(
                        "result:bought:"
                        f'{saved["call_id"]}'
                    )
                )
            )

        self.assertEqual(rated["manager_rating"], 4)
        self.assertTrue(disabled["disabled"])

        call = bot.get_call(saved["call_id"])
        self.assertEqual(call["effective_manager_rating"], 4)
        self.assertIsNone(call["effective_sale_status"])

    def test_dashboard_replaces_sales_controls_with_manager_ratings(self):
        html = bot.dashboard()

        self.assertIn(
            "Кто работает с телефонами",
            html,
        )
        self.assertIn(
            "/admin/device-managers",
            html,
        )
        self.assertIn(
            "На сегодня",
            html,
        )
        self.assertIn("Постоянно", html)
        self.assertNotIn("Код доступа", html)
        self.assertNotIn("DASHBOARD_ADMIN_TOKEN", html)
        self.assertIn("Моя средняя оценка менеджеров", html)
        self.assertIn("Мои оценки менеджеров по дням", html)
        self.assertIn("<th>Моя оценка</th>", html)
        self.assertNotIn("Почему не купили", html)
        self.assertNotIn("<th>Результат</th>", html)
        self.assertNotIn("<h2 class=\"section-title\">\n    Продажи", html)

    def test_real_sales_count_buyers_without_prior_calls(self):
        called_phone = "+998 90 111 22 33"
        no_call_phone = "+998902223344"

        self.save(
            "texnikach@gmail.com",
            self.event(30, called_phone, 0),
        )
        self.save_sales_cards(
            [
                {
                    "source_message_id": 100,
                    "replacement_message_id": 1100,
                    "manager": "Abbos",
                    "client_phone": called_phone,
                    "client_phone_2": None,
                    "product_label": None,
                    "sale_date": "2027-01-20",
                    "created_at": "2027-01-20T10:00:00+05:00",
                },
                {
                    "source_message_id": 101,
                    "replacement_message_id": 1101,
                    "manager": "Otabek",
                    "client_phone": no_call_phone,
                    "client_phone_2": "+998903334455",
                    "product_label": "📦 iPhone 15",
                    "sale_date": "2027-01-20",
                    "created_at": "2027-01-20T11:00:00+05:00",
                },
                {
                    "source_message_id": 102,
                    "replacement_message_id": 1102,
                    "manager": "Olmas",
                    "client_phone": None,
                    "client_phone_2": None,
                    "product_label": None,
                    "sale_date": "2027-01-20",
                    "created_at": "2027-01-20T12:00:00+05:00",
                },
            ]
        )

        period = {
            "period": "custom",
            "date_from": "2027-01-01",
            "date_to": "2027-01-31",
        }
        summary = bot.stats(**period)["stats"]
        customers = bot.stats_sales_customers(
            **period
        )
        managers = {
            row["manager_code"]: row
            for row in bot.stats_managers(
                **period
            )["results"]
        }

        self.assertTrue(customers["configured"])
        self.assertEqual(summary["real_sales_total"], 3)
        self.assertEqual(summary["real_sales_without_phone"], 1)
        self.assertEqual(summary["real_buyers_total"], 2)
        self.assertEqual(
            summary["real_buyers_called_before_purchase"],
            1,
        )
        self.assertEqual(
            summary["real_buyers_without_prior_call"],
            1,
        )
        self.assertEqual(
            managers["otabek"][
                "real_buyers_without_prior_call"
            ],
            1,
        )
        self.assertEqual(
            managers["otabek"]["calls"],
            0,
        )
        self.assertIn(
            "📦 iPhone 15",
            {
                row["product_label"]
                for row in customers["results"]
            },
        )
        self.assertIn(
            "—",
            {
                row["product_label"]
                for row in customers["results"]
            },
        )

    def test_real_sales_merge_phone_aliases_and_ignore_later_calls(self):
        first_phone = "+998904445566"
        second_phone = "+998905556677"
        self.save_sales_cards(
            [
                {
                    "source_message_id": 200,
                    "replacement_message_id": 1200,
                    "manager": "Olmas",
                    "client_phone": first_phone,
                    "client_phone_2": second_phone,
                    "product_label": None,
                    "sale_date": "2027-01-10",
                    "created_at": "2027-01-10T10:00:00+05:00",
                },
                {
                    "source_message_id": 201,
                    "replacement_message_id": 1201,
                    "manager": "Olmas",
                    "client_phone": second_phone,
                    "client_phone_2": None,
                    "product_label": None,
                    "sale_date": "2027-01-11",
                    "created_at": "2027-01-11T10:00:00+05:00",
                },
            ]
        )
        later_event = self.event(
            31,
            first_phone,
            0,
        )
        later_event["start_time"] = 1_900_000_000
        later_event["answer_time"] = 1_900_000_001
        later_event["end_time"] = 1_900_000_010
        self.save(
            "texnikach@gmail.com",
            later_event,
        )

        summary = bot.stats(
            period="custom",
            date_from="2027-01-01",
            date_to="2027-01-31",
        )["stats"]

        self.assertEqual(summary["real_sales_total"], 2)
        self.assertEqual(summary["real_buyers_total"], 1)
        self.assertEqual(
            summary["real_buyers_without_prior_call"],
            1,
        )

    def test_known_sim_conflict_is_visible_but_slot_mapping_wins(self):
        resolved = bot.resolve_call_device(
            "texnikach@gmail.com",
            "+998998446162",
            1,
        )

        self.assertTrue(resolved["mapping_conflict"])
        self.assertEqual(resolved["src_number"], "+998901313999")
        self.assertEqual(resolved["sim_label"], "SIM 2")

    def test_telegram_message_uses_saved_call_on_sparse_retry(self):
        event = self.event(10, "+998900000010", 1)
        saved = self.save("texnikach@gmail.com", event)
        call = bot.get_call(saved["call_id"])

        message = bot.build_telegram_message(
            {},
            {},
            0,
            call=call,
        )

        self.assertIn("Входящий звонок", message)
        self.assertIn("+998900000010", message)
        self.assertIn("9 сек", message)
        self.assertIn(bot.format_call_time(call["start_time"]), message)
        self.assertIn("📱 Устройство: <b>Poco</b>", message)
        self.assertNotIn("texnikach@gmail.com", message)
        self.assertNotIn("Исходящий без ответа", message)

    def test_manual_manager_survives_duplicate_webhook(self):
        event = self.event(3, "+998900000003", 0)
        saved = self.save("texnikach@gmail.com", event)

        bot.mark_talk_manager(
            saved["call_id"],
            "ali",
            {"id": 10, "username": "tester"},
        )
        self.save("texnikach@gmail.com", event)

        call = bot.get_call(saved["call_id"])
        self.assertEqual(call["talk_manager_code"], "ali")

    def test_tecno_sim_one_is_olx_and_assigns_otabek(self):
        saved = self.save(
            "texnikacholx@gmail.com",
            self.event(4, "+998900000004", 0, "+998977777777"),
        )
        call = bot.get_call(saved["call_id"])

        self.assertEqual(call["device_name"], "Tecno")
        self.assertEqual(call["src_number"], "+998908456162")
        self.assertEqual(call["provider_src_number"], "+998977777777")
        self.assertEqual(call["src_slot"], 0)
        self.assertEqual(call["talk_manager_code"], "otabek")
        self.assertEqual(call["effective_lead_source_code"], "olx")

        keyboard = bot.build_call_state_keyboard(call)
        button_texts = {
            button["text"]
            for row in keyboard["inline_keyboard"]
            for button in row
        }
        self.assertIn("👤 Менеджер: Otabek", button_texts)
        self.assertIn("📣 Источник: OLX", button_texts)

    def test_tecno_known_sim_number_is_olx_even_if_slot_is_one(self):
        saved = self.save(
            "texnikacholx@gmail.com",
            self.event(16, "+998900000016", 1, "+998908456162"),
        )
        call = bot.get_call(saved["call_id"])

        self.assertEqual(call["device_name"], "Tecno")
        self.assertEqual(call["talk_manager_code"], "otabek")
        self.assertEqual(call["effective_lead_source_code"], "olx")

    def test_tecno_unknown_sim_two_has_no_automatic_source(self):
        saved = self.save(
            "texnikacholx@gmail.com",
            self.event(19, "+998900000019", 1, "+998977777779"),
        )
        call = bot.get_call(saved["call_id"])

        self.assertEqual(call["device_name"], "Tecno")
        self.assertEqual(call["talk_manager_code"], "otabek")
        self.assertIsNone(call["effective_lead_source_code"])

    def test_redmi_assigns_olmas_and_only_sim_one_is_olx(self):
        first = self.save(
            "AASHSHDJDJDJSJ@gmail.com",
            self.event(17, "+998900000017", 0, "+998955555551"),
        )
        second = self.save(
            "aashshdjdjdjsj@gmail.com",
            self.event(18, "+998900000018", 1, "+998955555552"),
        )

        call_one = bot.get_call(first["call_id"])
        call_two = bot.get_call(second["call_id"])

        self.assertEqual(call_one["device_name"], "Redmi")
        self.assertEqual(call_one["src_number"], "+998908534466")
        self.assertEqual(call_one["provider_src_number"], "+998955555551")
        self.assertEqual(call_one["src_slot"], 0)
        self.assertEqual(call_one["talk_manager_code"], "olmas")
        self.assertEqual(call_one["effective_lead_source_code"], "olx")
        self.assertEqual(call_two["talk_manager_code"], "olmas")
        self.assertIsNone(call_two["effective_lead_source_code"])

    def test_manual_source_overrides_auto_for_the_30_hour_window(self):
        saved = self.save(
            "texnikach@gmail.com",
            self.event(5, "+998900000005", 0),
        )

        bot.mark_lead_source(
            saved["call_id"],
            "instagram",
            {"id": 11, "username": "tester"},
        )

        call = bot.get_call(saved["call_id"])
        self.assertEqual(call["lead_source_auto_code"], "olx")
        self.assertEqual(call["lead_source_manual_code"], "instagram")
        self.assertEqual(call["effective_lead_source_code"], "instagram")
        self.assertEqual(call["effective_lead_source_origin"], "manual")
        self.assertIn(
            "Ручной выбор",
            call["effective_lead_source_evidence"],
        )
        self.assertNotIn(
            "Авто по SIM",
            call["effective_lead_source_evidence"],
        )

    def test_corrected_duplicate_clears_stale_static_source(self):
        event = self.event(13, "+998900000013", 0)
        saved = self.save("texnikach@gmail.com", event)

        first = bot.get_call(saved["call_id"])
        self.assertEqual(first["effective_lead_source_code"], "olx")

        corrected = dict(event)
        corrected["src_slot"] = 1
        corrected["src_number"] = "+998901313999"
        self.save("texnikach@gmail.com", corrected)

        final = bot.get_call(saved["call_id"])
        self.assertEqual(final["src_number"], "+998901313999")
        self.assertIsNone(final["lead_source_auto_code"])
        self.assertIsNone(final["effective_lead_source_code"])

    def test_every_call_in_window_uses_latest_canonical_result(self):
        first_event = self.event(14, "+998900000014", 1)
        second_event = self.event(15, "+998900000014", 1)
        first = self.save("texnikach@gmail.com", first_event)
        second = self.save("texnikach@gmail.com", second_event)
        actor = {"id": 50, "username": "tester"}

        bot.mark_sale_bought(first["call_id"], actor)
        bot.mark_sale_not_bought(
            second["call_id"],
            "price",
            actor,
        )

        first_call = bot.get_call(first["call_id"])
        second_call = bot.get_call(second["call_id"])

        self.assertEqual(first_call["effective_sale_status"], "not_bought")
        self.assertEqual(second_call["effective_sale_status"], "not_bought")
        self.assertEqual(
            first_call["effective_result_call_id"],
            second["call_id"],
        )
        self.assertEqual(
            bot.get_selected_result_text(first_call),
            "💰 Не устроила цена",
        )

    def test_transcript_detector_avoids_ambiguous_equal_platforms(self):
        instagram = bot.detect_lead_source_from_transcript(
            "Клиент увидел нас в Инстаграме."
        )
        olx_ad = bot.detect_lead_source_from_transcript(
            "Я звоню по вашему объявлению."
        )
        ambiguous = bot.detect_lead_source_from_transcript(
            "Вы нашли нас в OLX или Instagram?"
        )

        self.assertEqual(instagram["code"], "instagram")
        self.assertEqual(olx_ad["code"], "olx")
        self.assertIsNone(ambiguous["code"])

    def test_transcript_detector_requires_acquisition_context(self):
        negative_examples = (
            "Я отправлю вам каталог в Telegram.",
            "В Instagram я вас не видел.",
            "На OLX нас нет, номер дал друг.",
            "Напишите нам потом в Инстаграме.",
        )

        for phrase in negative_examples:
            with self.subTest(phrase=phrase):
                detected = bot.detect_lead_source_from_transcript(
                    phrase
                )
                self.assertIsNone(detected["code"])

        positive_examples = {
            "Номер нашёл на OLX.": "olx",
            "Клиент увидел нас в Инстаграме.": "instagram",
            "Telegram kanalidan raqamingizni topdim.": "telegram_channel",
            "OLXdan topdim va qo'ng'iroq qilyapman.": "olx",
        }

        for phrase, expected in positive_examples.items():
            with self.subTest(phrase=phrase):
                detected = bot.detect_lead_source_from_transcript(
                    phrase
                )
                self.assertEqual(detected["code"], expected)

    def test_source_and_sim_endpoints_use_canonical_values(self):
        self.save(
            "texnikach@gmail.com",
            self.event(6, "+998900000006", 0),
        )
        self.save(
            "texnikach@gmail.com",
            self.event(7, "+998900000007", 1),
        )

        period = {
            "period": "custom",
            "date_from": "2027-01-01",
            "date_to": "2027-02-01",
        }

        sims = bot.stats_sims(**period)["results"]
        sources = bot.stats_sources(**period)["results"]
        recent = bot.stats_recent(**period)["results"]

        self.assertEqual(
            {row["sim"] for row in sims},
            {"+998998446162", "+998901313999"},
        )
        self.assertEqual(
            {row["source_code"] for row in sources},
            {"olx", "unmarked"},
        )
        self.assertEqual(
            {row["lead_source"] for row in recent},
            {"OLX", "—"},
        )

    def test_sim_stats_do_not_split_on_missing_account_name(self):
        self.save(
            "texnikach@gmail.com",
            self.event(11, "+998900000011", 0),
        )

        webhook_without_name = self.webhook(
            "texnikach@gmail.com"
        )
        webhook_without_name.pop(
            "account_name"
        )
        bot.save_call(
            webhook_without_name,
            self.event(12, "+998900000012", 0),
            9,
            "api",
        )

        rows = bot.stats_sims(
            period="custom",
            date_from="2027-01-01",
            date_to="2027-02-01",
        )["results"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 2)
        self.assertEqual(rows[0]["sim"], "+998998446162")

    def test_transcription_queue_is_durable_and_deduplicated(self):
        event = self.event(8, "+998900000008", 0)
        event["recording"] = (
            "https://texnikachuz.moizvonki.ru/"
            "calls/recordings/test.mp3/"
        )
        saved = self.save(
            "texnikach@gmail.com",
            event,
        )

        old_enabled = bot.TRANSCRIPTION_ENABLED
        bot.TRANSCRIPTION_ENABLED = True

        try:
            first = bot.enqueue_transcription(saved["call_id"])
            second = bot.enqueue_transcription(saved["call_id"])
        finally:
            bot.TRANSCRIPTION_ENABLED = old_enabled

        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])

        with bot.connect_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM call_transcriptions"
            ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_transcription_outbox_is_atomic_with_call_save(self):
        event = self.event(16, "+998900000016", 0)
        event["recording"] = (
            "https://texnikachuz.moizvonki.ru/"
            "calls/recordings/atomic.mp3/"
        )
        old_enabled = bot.TRANSCRIPTION_ENABLED
        bot.TRANSCRIPTION_ENABLED = True

        try:
            saved = self.save(
                "texnikach@gmail.com",
                event,
            )
        finally:
            bot.TRANSCRIPTION_ENABLED = old_enabled

        with bot.connect_db() as conn:
            queued = conn.execute(
                """
                SELECT status
                FROM call_transcriptions
                WHERE call_id = ?
                """,
                (saved["call_id"],),
            ).fetchone()

        self.assertIsNotNone(queued)
        self.assertEqual(queued["status"], "queued")

    def test_transcription_outbox_error_does_not_lose_call(self):
        old_enabled = bot.TRANSCRIPTION_ENABLED
        bot.TRANSCRIPTION_ENABLED = True

        try:
            with mock.patch.object(
                bot,
                "enqueue_transcription_in_transaction",
                side_effect=bot.sqlite3.OperationalError(
                    "temporary"
                ),
            ):
                saved = self.save(
                    "texnikach@gmail.com",
                    self.event(17, "+998900000017", 0),
                )
        finally:
            bot.TRANSCRIPTION_ENABLED = old_enabled

        self.assertIsNotNone(
            bot.get_call(saved["call_id"])
        )

    def test_recording_url_security_rejects_unsafe_variants(self):
        with mock.patch.object(
            bot.socket,
            "getaddrinfo",
            return_value=[
                (
                    bot.socket.AF_INET,
                    bot.socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", 443),
                )
            ],
        ):
            self.assertTrue(
                bot.recording_host_is_allowed(
                    "https://tenant.moizvonki.ru/call.mp3"
                )
            )
            self.assertFalse(
                bot.recording_host_is_allowed(
                    "http://tenant.moizvonki.ru/call.mp3"
                )
            )
            self.assertFalse(
                bot.recording_host_is_allowed(
                    "https://user:pass@tenant.moizvonki.ru/call.mp3"
                )
            )
            self.assertFalse(
                bot.recording_host_is_allowed(
                    "https://tenant.moizvonki.ru:8443/call.mp3"
                )
            )

    def test_recording_downloader_does_not_follow_redirects(self):
        request_kwargs = {}

        class FakeResponse:
            status_code = 302
            headers = {"Location": "http://127.0.0.1/private"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                request_kwargs.update(kwargs)
                return FakeResponse()

        with mock.patch.object(
            bot,
            "recording_host_is_allowed",
            return_value=True,
        ), mock.patch.object(
            bot.requests,
            "Session",
            FakeSession,
        ):
            with self.assertRaises(ValueError):
                bot.download_recording_limited(
                    "https://tenant.moizvonki.ru/call.mp3",
                    Path(self.tmp.name) / "call.mp3",
                )

        self.assertIs(
            request_kwargs["allow_redirects"],
            False,
        )

    def test_transcription_missing_key_blocks_worker_without_claiming(self):
        old_key = bot.TRANSCRIPTION_API_KEY
        bot.TRANSCRIPTION_API_KEY = ""

        try:
            error = bot.get_transcription_config_error()
        finally:
            bot.TRANSCRIPTION_API_KEY = old_key

        self.assertIn("API_KEY", error)

    def test_moizvonki_secret_is_checked_before_body_is_read(self):
        request = mock.Mock()
        request.headers = {}
        request.query_params = {}
        request.json = mock.AsyncMock()
        old_secret = bot.MOIZVONKI_WEBHOOK_SECRET
        bot.MOIZVONKI_WEBHOOK_SECRET = "expected-secret"

        try:
            with self.assertRaises(bot.HTTPException) as raised:
                asyncio.run(
                    bot.moizvonki_webhook(request)
                )
        finally:
            bot.MOIZVONKI_WEBHOOK_SECRET = old_secret

        self.assertEqual(raised.exception.status_code, 403)
        request.json.assert_not_awaited()

    def test_telegram_call_messages_are_silent_by_default(self):
        with mock.patch.object(
            bot,
            "telegram_api",
            return_value={"ok": True},
        ) as telegram_api:
            bot.send_text_message(
                "Call"
            )
            bot.send_voice_bytes(
                b"voice",
                "Call",
            )

        text_call = telegram_api.call_args_list[0]
        voice_call = telegram_api.call_args_list[1]

        self.assertIs(
            text_call.kwargs["data"][
                "disable_notification"
            ],
            True,
        )
        self.assertIs(
            voice_call.kwargs["data"][
                "disable_notification"
            ],
            True,
        )

    def test_missed_incoming_call_mentions_abbos_and_stays_silent(self):
        event = self.event(
            54,
            "+998900000054",
            0,
        )
        event.update(
            {
                "answered": 0,
                "answer_time": 0,
                "recording": "",
                "upload_time": (
                    event["end_time"] + 2
                ),
                "event_created": (
                    event["end_time"] + 2
                ),
            }
        )

        request = mock.Mock()
        request.headers = {}
        request.query_params = {}
        request.json = mock.AsyncMock(
            return_value={
                "webhook": self.webhook(
                    "texnikach@gmail.com"
                ),
                "event": event,
            }
        )
        sent = {}

        def send_telegram(
            text,
            *args,
            **kwargs,
        ):
            sent["text"] = text
            sent.update(kwargs)
            return {
                "result": {
                    "message_id": 154,
                    "chat": {"id": 456},
                }
            }

        with mock.patch.multiple(
            bot,
            AUTO_SMS_ENABLED=False,
            RATING_SMS_ENABLED=False,
            MOIZVONKI_WEBHOOK_SECRET="",
            MISSED_CALL_ALERT_USERNAME=(
                "@AbbosTch"
            ),
        ), mock.patch.object(
            bot,
            "send_text_message",
            side_effect=send_telegram,
        ):
            result = asyncio.run(
                bot.moizvonki_webhook(
                    request
                )
            )

        self.assertEqual(
            result["telegram"],
            "sent",
        )
        self.assertIn(
            "@AbbosTch",
            sent["text"],
        )
        self.assertIs(
            sent["disable_notification"],
            True,
        )

    def test_webhook_delivers_telegram_before_client_sms(self):
        event = self.event(
            18,
            "+998900000018",
            1,
        )
        event["upload_time"] = event["end_time"] + 8
        event["event_created"] = event["upload_time"]

        request = mock.Mock()
        request.headers = {}
        request.query_params = {}
        request.json = mock.AsyncMock(
            return_value={
                "webhook": self.webhook(
                    "texnikach@gmail.com"
                ),
                "event": event,
            }
        )

        delivery_order = []
        notification_flags = []

        def send_telegram(*args, **kwargs):
            delivery_order.append("telegram")
            notification_flags.append(
                kwargs[
                    "disable_notification"
                ]
            )
            return {
                "result": {
                    "message_id": 123,
                    "chat": {"id": 456},
                }
            }

        def send_sms(*args, **kwargs):
            delivery_order.append("sms")
            return {"success": True}

        old_base_url = bot.PUBLIC_BASE_URL
        old_secret = bot.MOIZVONKI_WEBHOOK_SECRET
        bot.PUBLIC_BASE_URL = "https://example.test"
        bot.MOIZVONKI_WEBHOOK_SECRET = ""

        try:
            with mock.patch.object(
                bot,
                "send_text_message",
                side_effect=send_telegram,
            ), mock.patch.object(
                bot,
                "send_client_sms",
                side_effect=send_sms,
            ):
                result = asyncio.run(
                    bot.moizvonki_webhook(request)
                )
        finally:
            bot.PUBLIC_BASE_URL = old_base_url
            bot.MOIZVONKI_WEBHOOK_SECRET = old_secret

        self.assertTrue(result["ok"])
        self.assertEqual(
            delivery_order,
            ["telegram", "sms", "sms"],
        )
        self.assertEqual(
            notification_flags,
            [True],
        )

    def test_after_hours_sms_uses_call_start_time_boundaries(self):
        def timestamp(hour, minute, second=0):
            return int(
                bot.datetime(
                    2027,
                    1,
                    3,
                    hour,
                    minute,
                    second,
                    tzinfo=bot.UZ_TZ,
                ).timestamp()
            )

        with mock.patch.multiple(
            bot,
            MISSED_CALL_WORK_START_MINUTES=10 * 60,
            MISSED_CALL_WORK_START_LABEL="10:00",
            MISSED_CALL_WORK_END_MINUTES=20 * 60,
            MISSED_CALL_WORK_END_LABEL="20:00",
        ):
            before_opening = (
                bot.build_after_hours_missed_sms(
                    timestamp(9, 59, 59)
                )
            )
            at_opening = (
                bot.build_after_hours_missed_sms(
                    timestamp(10, 0)
                )
            )
            before_closing = (
                bot.build_after_hours_missed_sms(
                    timestamp(19, 59, 59)
                )
            )
            at_closing = (
                bot.build_after_hours_missed_sms(
                    timestamp(20, 0)
                )
            )

        self.assertIn("Сегодня", before_opening)
        self.assertIn("Bugun", before_opening)
        self.assertIsNone(at_opening)
        self.assertIsNone(before_closing)
        self.assertIn("Завтра", at_closing)
        self.assertIn("Ertaga", at_closing)
        self.assertIsNone(
            bot.build_after_hours_missed_sms(
                None
            )
        )

    def test_missed_after_hours_sms_replaces_promo_and_is_deduplicated(self):
        start_time = int(
            bot.datetime(
                2027,
                1,
                3,
                21,
                15,
                tzinfo=bot.UZ_TZ,
            ).timestamp()
        )
        event = self.event(
            19,
            "+998900000019",
            1,
        )
        event.update(
            {
                "answered": 0,
                "answer_time": 0,
                "start_time": start_time,
                "end_time": start_time + 5,
                "upload_time": start_time + 8,
                "event_created": start_time + 8,
            }
        )

        request = mock.Mock()
        request.headers = {}
        request.query_params = {}
        request.json = mock.AsyncMock(
            return_value={
                "webhook": self.webhook(
                    "texnikach@gmail.com"
                ),
                "event": event,
            }
        )

        sent_messages = []

        def send_telegram(*args, **kwargs):
            return {
                "result": {
                    "message_id": 124,
                    "chat": {"id": 456},
                }
            }

        def send_sms(number, login, text=bot.SMS_TEXT):
            sent_messages.append(text)
            return {"success": True}

        with mock.patch.multiple(
            bot,
            AUTO_SMS_ENABLED=True,
            RATING_SMS_ENABLED=True,
            MOIZVONKI_WEBHOOK_SECRET="",
            MISSED_CALL_WORK_START_MINUTES=10 * 60,
            MISSED_CALL_WORK_START_LABEL="10:00",
            MISSED_CALL_WORK_END_MINUTES=20 * 60,
            MISSED_CALL_WORK_END_LABEL="20:00",
        ), mock.patch.object(
            bot,
            "send_text_message",
            side_effect=send_telegram,
        ), mock.patch.object(
            bot,
            "send_client_sms",
            side_effect=send_sms,
        ):
            first = asyncio.run(
                bot.moizvonki_webhook(request)
            )
            duplicate = asyncio.run(
                bot.moizvonki_webhook(request)
            )

        self.assertEqual(first["sms"], "sent")
        self.assertEqual(
            first["sms_kind"],
            "after_hours_missed",
        )
        self.assertEqual(
            first["rating_sms"],
            "not_applicable",
        )
        self.assertEqual(duplicate["sms"], "cooldown")
        self.assertEqual(len(sent_messages), 1)

        text = sent_messages[0]
        self.assertNotEqual(text, bot.SMS_TEXT)
        self.assertIn("Завтра", text)
        self.assertIn("Ertaga", text)
        self.assertIn("бесплатная", text)
        self.assertIn("bepul", text)
        self.assertIn("магазине", text)
        self.assertIn("do‘kondan", text)
        self.assertIn("https://texnikach.uz/go", text)

        with bot.connect_db() as conn:
            history = conn.execute(
                """
                SELECT message_kind
                FROM sms_history
                WHERE call_id = ?
                """,
                (first["call_id"],),
            ).fetchone()

        self.assertEqual(
            history["message_kind"],
            "after_hours_missed",
        )

    def test_transcription_worker_survives_outer_database_error(self):
        calls = 0

        async def immediate_timeout(awaitable, **kwargs):
            awaitable.close()
            raise asyncio.TimeoutError

        async def scenario():
            nonlocal calls
            worker = bot.TranscriptionWorker()

            def flaky_process():
                nonlocal calls
                calls += 1

                if calls == 1:
                    raise bot.sqlite3.OperationalError(
                        "temporary"
                    )

                worker._stop_event.set()
                return False

            with mock.patch.object(
                bot,
                "process_one_transcription_job",
                side_effect=flaky_process,
            ), mock.patch.object(
                bot.asyncio,
                "wait_for",
                side_effect=immediate_timeout,
            ):
                await worker._run()

        asyncio.run(
            scenario()
        )
        self.assertEqual(calls, 2)

    def test_rating_details_contains_device_source_and_transcription_sections(self):
        saved = self.save(
            "texnikach@gmail.com",
            self.event(9, "+998900000009", 0),
        )

        with bot.connect_db() as conn:
            call = conn.execute(
                "SELECT client_key, client_window_id FROM calls WHERE id = ?",
                (saved["call_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO call_ratings (
                    call_id,
                    client_window_id,
                    token_hash,
                    client_key,
                    sms_status,
                    sms_reserved_at,
                    score,
                    rated_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, 'sent', ?, 5, ?, ?)
                """,
                (
                    saved["call_id"],
                    call["client_window_id"],
                    "token-hash-9",
                    call["client_key"],
                    1_800_000_010,
                    1_800_000_020,
                    1_900_000_000,
                ),
            )
            conn.commit()

        details = bot.rating_details(saved["call_id"])
        sections = {
            section["title"]: section["items"]
            for section in details["sections"]
        }

        self.assertEqual(
            sections["Источник клиента"]["Основной источник"],
            "OLX",
        )
        self.assertEqual(
            sections["SIM и аккаунт"]["SIM / номер телефона"],
            "+998998446162",
        )
        self.assertIn("Расшифровка звонка", sections)

    def create_sent_customer_rating(
        self,
        db_call_id=50,
        client_number="+998900000050",
        user_login="texnikach@gmail.com",
        sent_at=1_800_000_100,
        expires_at=1_900_000_000,
        score=None,
        score_source=None,
    ):
        saved = self.save(
            user_login,
            self.event(
                db_call_id,
                client_number,
                0,
            ),
        )

        with bot.connect_db() as conn:
            call = conn.execute(
                """
                SELECT
                    client_key,
                    client_window_id

                FROM calls

                WHERE id = ?
                """,
                (
                    saved["call_id"],
                ),
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO call_ratings (
                    call_id,
                    client_window_id,
                    token_hash,
                    client_key,
                    sender_user_login,
                    sms_status,
                    sms_reserved_at,
                    sms_sent_at,
                    score,
                    score_source,
                    rated_at,
                    expires_at
                )

                VALUES (
                    ?, ?, ?, ?, ?, 'sent', ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    saved["call_id"],
                    call["client_window_id"],
                    f"sms-token-{db_call_id}",
                    call["client_key"],
                    user_login,
                    sent_at - 1,
                    sent_at,
                    score,
                    score_source,
                    sent_at + 1 if score else None,
                    expires_at,
                ),
            )
            conn.commit()

        return saved, cursor.lastrowid

    @staticmethod
    def inbound_sms_request(
        client_number,
        text,
        *,
        direction=0,
        user_login="texnikach@gmail.com",
        event_created=1_800_000_200,
        start_time=1_800_000_190,
        event_pbx_call_id="sms-event-1",
    ):
        request = mock.Mock()
        request.headers = {}
        request.query_params = {}
        request.json = mock.AsyncMock(
            return_value={
                "webhook": {
                    "action": "sms.message",
                    "user_login": user_login,
                    "user_id": 1,
                    "account_id": 98073,
                    "account_name": "texnikachuz",
                },
                "event": {
                    "event_type": 32,
                    "direction": direction,
                    "event_pbx_call_id": event_pbx_call_id,
                    "event_created": event_created,
                    "start_time": start_time,
                    "client_number": client_number,
                    "client_name": "Client",
                    "src_number": "+998998446162",
                    "src_id": 1,
                    "src_slot": 0,
                    "text": text,
                },
            }
        )
        return request

    def test_inbound_sms_rating_is_saved_and_visible_in_all_statistics(self):
        saved, rating_id = self.create_sent_customer_rating()
        request = self.inbound_sms_request(
            "+998900000050",
            " 5 ",
        )

        result = asyncio.run(
            bot.moizvonki_webhook(
                request
            )
        )

        self.assertTrue(
            result["sms_rating"]["processed"]
        )
        self.assertEqual(
            result["sms_rating"]["score"],
            5,
        )

        with bot.connect_db() as conn:
            rating = conn.execute(
                """
                SELECT *
                FROM call_ratings
                WHERE id = ?
                """,
                (
                    rating_id,
                ),
            ).fetchone()
            inbound = conn.execute(
                """
                SELECT *
                FROM inbound_sms_events
                """
            ).fetchone()

        self.assertEqual(rating["score"], 5)
        self.assertEqual(
            rating["score_source"],
            "sms",
        )
        self.assertEqual(
            rating["rated_at"],
            1_800_000_190,
        )
        self.assertEqual(
            rating["inbound_sms_event_id"],
            inbound["id"],
        )
        self.assertEqual(
            inbound["processing_status"],
            "rating_saved",
        )
        self.assertEqual(inbound["text"], " 5 ")

        period = {
            "period": "custom",
            "date_from": "2027-01-01",
            "date_to": "2027-02-01",
        }
        summary = bot.stats(**period)["stats"]
        manager = bot.stats_managers(
            **period
        )["results"][0]
        daily = bot.stats_ratings_daily(
            **period
        )["results"][0]
        recent = bot.stats_recent(
            **period
        )["results"][0]

        self.assertEqual(summary["average_rating"], 5.0)
        self.assertEqual(summary["ratings_count"], 1)
        self.assertEqual(manager["average_rating"], 5.0)
        self.assertEqual(daily["average_rating"], 5.0)
        self.assertEqual(recent["customer_rating"], 5)
        self.assertEqual(
            recent["customer_rating_source"],
            "sms",
        )

        details = bot.rating_details(
            saved["call_id"]
        )
        sections = {
            section["title"]: section["items"]
            for section in details["sections"]
        }
        rating_details = sections[
            "Оценка и SMS"
        ]
        self.assertEqual(
            rating_details["Способ ответа"],
            "Входящее SMS",
        )
        self.assertEqual(
            rating_details["Полученное SMS"],
            " 5 ",
        )

    def test_inbound_sms_webhook_is_idempotent_and_keeps_first_rating(self):
        _, rating_id = self.create_sent_customer_rating(
            db_call_id=51,
            client_number="+998900000051",
        )
        request = self.inbound_sms_request(
            "+998900000051",
            "4",
            event_pbx_call_id="sms-event-51",
        )

        first = asyncio.run(
            bot.moizvonki_webhook(request)
        )
        duplicate = asyncio.run(
            bot.moizvonki_webhook(request)
        )

        self.assertTrue(
            first["sms_rating"]["processed"]
        )
        self.assertTrue(
            duplicate["sms_rating"]["duplicate"]
        )

        with bot.connect_db() as conn:
            inbound_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM inbound_sms_events
                """
            ).fetchone()[0]
            rating = conn.execute(
                """
                SELECT score, score_source
                FROM call_ratings
                WHERE id = ?
                """,
                (
                    rating_id,
                ),
            ).fetchone()

        self.assertEqual(inbound_count, 1)
        self.assertEqual(rating["score"], 4)
        self.assertEqual(rating["score_source"], "sms")

    def test_only_incoming_exact_digit_from_same_phone_can_rate(self):
        _, rating_id = self.create_sent_customer_rating(
            db_call_id=52,
            client_number="+998900000052",
        )

        outgoing = asyncio.run(
            bot.moizvonki_webhook(
                self.inbound_sms_request(
                    "+998900000052",
                    "5",
                    direction=1,
                    event_pbx_call_id="sms-outgoing-52",
                )
            )
        )
        non_rating = asyncio.run(
            bot.moizvonki_webhook(
                self.inbound_sms_request(
                    "+998900000052",
                    "Оценка 5",
                    event_pbx_call_id="sms-text-52",
                )
            )
        )
        wrong_phone = asyncio.run(
            bot.moizvonki_webhook(
                self.inbound_sms_request(
                    "+998900000052",
                    "5",
                    user_login="texnikacholx@gmail.com",
                    event_pbx_call_id="sms-account-52",
                )
            )
        )

        self.assertEqual(
            outgoing["sms_rating"]["reason"],
            "outgoing_ignored",
        )
        self.assertEqual(
            non_rating["sms_rating"]["reason"],
            "non_rating",
        )
        self.assertEqual(
            wrong_phone["sms_rating"]["reason"],
            "no_rating_request",
        )

        with bot.connect_db() as conn:
            rating = conn.execute(
                """
                SELECT score
                FROM call_ratings
                WHERE id = ?
                """,
                (
                    rating_id,
                ),
            ).fetchone()
            inbound = conn.execute(
                """
                SELECT
                    processing_status,
                    text

                FROM inbound_sms_events

                ORDER BY id
                """
            ).fetchall()

        self.assertIsNone(rating["score"])
        self.assertEqual(len(inbound), 2)
        self.assertEqual(
            [row["processing_status"] for row in inbound],
            [
                "non_rating",
                "no_rating_request",
            ],
        )

    def test_existing_web_rating_is_never_overwritten_by_sms(self):
        _, rating_id = self.create_sent_customer_rating(
            db_call_id=53,
            client_number="+998900000053",
            score=2,
            score_source="web",
        )

        result = asyncio.run(
            bot.moizvonki_webhook(
                self.inbound_sms_request(
                    "+998900000053",
                    "5",
                    event_pbx_call_id="sms-rated-53",
                )
            )
        )

        self.assertEqual(
            result["sms_rating"]["reason"],
            "already_rated",
        )
        self.assertEqual(
            result["sms_rating"]["score"],
            2,
        )
        self.assertEqual(
            result["sms_rating"]["received_score"],
            5,
        )

        with bot.connect_db() as conn:
            rating = conn.execute(
                """
                SELECT score, score_source
                FROM call_ratings
                WHERE id = ?
                """,
                (
                    rating_id,
                ),
            ).fetchone()

        self.assertEqual(rating["score"], 2)
        self.assertEqual(rating["score_source"], "web")


if __name__ == "__main__":
    unittest.main()
