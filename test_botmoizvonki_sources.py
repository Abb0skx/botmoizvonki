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
        self.assertIn("✅ Купил", button_texts)

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

        def send_telegram(*args, **kwargs):
            delivery_order.append("telegram")
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


if __name__ == "__main__":
    unittest.main()
