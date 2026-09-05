import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import unittest

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from forwarding.config import (
    DEVICES,
    OPERATOR,
    ROUTES,
    ForwardingSettings,
    load_forwarding_settings,
)
from forwarding.repository import ForwardingRepository
from forwarding.service import ForwardingService


UZ_TZ = timezone(timedelta(hours=5))
CHAT_ID = -100123456789
ADMIN_ID = 202134293
REDMI_CONTROLLER_ID = 7636344727
TECNO_CONTROLLER_ID = 702960146

_IMPORT_TMP = tempfile.TemporaryDirectory()
_IMPORT_DIR = Path(_IMPORT_TMP.name)
os.environ.setdefault("DB_PATH", str(_IMPORT_DIR / "calls.db"))
os.environ.setdefault("INSTAGRAM_DB_PATH", str(_IMPORT_DIR / "instagram.db"))
os.environ.setdefault("REVIEWS_DB_PATH", str(_IMPORT_DIR / "reviews.db"))
os.environ.setdefault("BUSINESS_DB_PATH", str(_IMPORT_DIR / "business.db"))
os.environ.setdefault("PRODUCT_URLS_PATH", str(_IMPORT_DIR / "products.xlsx"))
os.environ.setdefault("TRANSCRIPTION_ENABLED", "false")


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_message_id = 100

    def __call__(self, method, *, data=None, files=None, timeout=30):
        payload = dict(data or {})
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_message_id += 1
            return {
                "ok": True,
                "result": {"message_id": self.next_message_id},
            }
        return {"ok": True, "result": True}


class ForwardingControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "calls.db"

        def connect():
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn

        self.repository = ForwardingRepository(
            connect,
            OPERATOR,
            DEVICES,
            ROUTES,
        )
        self.repository.init_schema()
        self.telegram = FakeTelegram()
        self.api_calls = []

        def make_call(user_login, service_number):
            self.api_calls.append((user_login, service_number))
            return {
                "http_status": 200,
                "body": {"status": "Call posted"},
            }

        self.settings = ForwardingSettings(
            enabled=True,
            post_hour=20,
            post_minute=0,
            poll_seconds=2,
            command_cooldown_seconds=90,
            confirmation_timeout_seconds=120,
            correlation_window_seconds=5 * 60,
            admin_ids=frozenset({ADMIN_ID}),
            device_controller_ids={
                "redmi": frozenset({REDMI_CONTROLLER_ID}),
                "tecno": frozenset({TECNO_CONTROLLER_ID}),
            },
        )
        self.service = ForwardingService(
            repository=self.repository,
            settings=self.settings,
            chat_id=CHAT_ID,
            telegram_api=self.telegram,
            make_call=make_call,
            local_timezone=UZ_TZ,
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def timestamp(day=4, hour=20, minute=0, second=0):
        return int(
            datetime(
                2026,
                9,
                day,
                hour,
                minute,
                second,
                tzinfo=UZ_TZ,
            ).timestamp()
        )

    def activate_post(self, day=4):
        result = self.service.ensure_daily_post(
            self.timestamp(day=day)
        )
        return result["post"]

    def queue(
        self,
        callback_data,
        callback_id="callback-1",
        now_ts=None,
        actor_id=ADMIN_ID,
        message_id=None,
    ):
        post = self.repository.get_current_post()
        return self.service.queue_callback(
            callback_query_id=callback_id,
            callback_data=callback_data,
            telegram_user={"id": actor_id, "username": "AbbosTch"},
            chat_id=CHAT_ID,
            message_id=(
                message_id
                if message_id is not None
                else post["message_id"]
            ),
            now_ts=now_ts or self.timestamp(second=1),
        )

    def test_exact_routes_and_poco_has_no_controls(self):
        expected = {
            ("redmi", "poco"): "**21*+998901313999*11#",
            ("redmi", "tecno"): "**21*+998908456162*11#",
            ("tecno", "poco"): "**21*+998901313999*11#",
            ("tecno", "redmi"): "**21*+998908534466*11#",
            ("redmi", "off"): "##21#",
            ("tecno", "off"): "##21#",
        }
        for (source, target), exact_number in expected.items():
            _, service_number, _, _ = self.service.service_number(
                source,
                target,
            )
            self.assertEqual(service_number, exact_number)

        with self.assertRaises(ValueError):
            self.service.service_number("poco", "off")

        callbacks = {
            button["callback_data"]
            for row in self.service.build_keyboard()["inline_keyboard"]
            for button in row
        }
        self.assertFalse(any(value.startswith("fwd:poco:") for value in callbacks))

    def test_daily_post_is_due_once_and_rotates_exact_old_pin(self):
        before = self.service.ensure_daily_post(
            self.timestamp(hour=19, minute=59, second=59)
        )
        self.assertEqual(before["reason"], "not_due")
        self.assertEqual(self.telegram.calls, [])

        first = self.service.ensure_daily_post(self.timestamp())
        self.assertTrue(first["created"])
        again = self.service.ensure_daily_post(
            self.timestamp(hour=23)
        )
        self.assertFalse(again["created"])

        send_calls = [call for call in self.telegram.calls if call[0] == "sendMessage"]
        pin_calls = [call for call in self.telegram.calls if call[0] == "pinChatMessage"]
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(len(pin_calls), 1)
        self.assertIs(send_calls[0][1]["disable_notification"], True)
        self.assertIs(pin_calls[0][1]["disable_notification"], True)

        old_message_id = first["post"]["message_id"]
        second = self.service.ensure_daily_post(
            self.timestamp(day=5)
        )
        self.assertTrue(second["created"])
        unpins = [call for call in self.telegram.calls if call[0] == "unpinChatMessage"]
        self.assertEqual(len(unpins), 1)
        self.assertEqual(unpins[0][1]["message_id"], old_message_id)

    def test_failed_new_pin_keeps_previous_post_as_current(self):
        first = self.service.ensure_daily_post(self.timestamp())
        old_message_id = first["post"]["message_id"]
        original_telegram = self.service.telegram_api

        def fail_new_pin(method, **kwargs):
            if method == "pinChatMessage":
                raise RuntimeError("bot cannot pin this message")
            return original_telegram(method, **kwargs)

        self.service.telegram_api = fail_new_pin
        with self.assertRaisesRegex(RuntimeError, "cannot pin"):
            self.service.ensure_daily_post(self.timestamp(day=5))

        current = self.repository.get_current_post()
        self.assertEqual(current["message_id"], old_message_id)
        self.assertEqual(current["status"], "active")

        self.service.telegram_api = original_telegram
        waiting = self.service.ensure_daily_post(
            self.timestamp(day=5, second=1)
        )
        self.assertEqual(waiting["reason"], "retry_wait")
        recovered = self.service.ensure_daily_post(
            self.timestamp(day=5) + 301
        )
        self.assertEqual(recovered["reason"], "active")
        sends = [
            call for call in self.telegram.calls
            if call[0] == "sendMessage"
        ]
        self.assertEqual(len(sends), 2)

    def test_only_admin_current_post_can_queue_and_replay_is_idempotent(self):
        post = self.activate_post()
        forbidden = self.queue(
            "fwd:redmi:poco",
            actor_id=999,
        )
        self.assertEqual(forbidden["reason"], "forbidden")

        stale = self.queue(
            "fwd:redmi:poco",
            callback_id="stale",
            message_id=post["message_id"] - 1,
        )
        self.assertEqual(stale["reason"], "stale_post")

        queued = self.queue("fwd:redmi:poco")
        replay = self.queue("fwd:redmi:poco")
        busy = self.queue(
            "fwd:redmi:tecno",
            callback_id="callback-2",
        )
        self.assertTrue(queued["queued"])
        self.assertEqual(replay["reason"], "replay")
        self.assertEqual(busy["reason"], "busy")

        with self.repository.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forwarding_operations"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_device_controllers_are_limited_to_their_own_phone(self):
        self.activate_post()

        redmi = self.queue(
            "fwd:redmi:poco",
            callback_id="redmi-owner",
            actor_id=REDMI_CONTROLLER_ID,
        )
        self.assertTrue(redmi["queued"])

        redmi_for_tecno = self.queue(
            "fwd:tecno:poco",
            callback_id="redmi-owner-tries-tecno",
            actor_id=REDMI_CONTROLLER_ID,
            now_ts=self.timestamp(second=3),
        )
        self.assertEqual(redmi_for_tecno["reason"], "forbidden")
        self.assertIn("Tecno", redmi_for_tecno["message"])

        tecno = self.queue(
            "fwd:tecno:redmi",
            callback_id="tecno-owner",
            actor_id=TECNO_CONTROLLER_ID,
            now_ts=self.timestamp(second=3),
        )
        self.assertTrue(tecno["queued"])

        tecno_for_redmi = self.queue(
            "fwd:redmi:off",
            callback_id="tecno-owner-tries-redmi",
            actor_id=TECNO_CONTROLLER_ID,
            now_ts=self.timestamp(second=4),
        )
        self.assertEqual(tecno_for_redmi["reason"], "forbidden")
        self.assertIn("Redmi", tecno_for_redmi["message"])

        with self.repository.connect() as conn:
            rows = conn.execute(
                """
                SELECT employee_id, requested_by
                FROM forwarding_operations
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [(row["employee_id"], row["requested_by"]) for row in rows],
            [
                ("redmi", REDMI_CONTROLLER_ID),
                ("tecno", TECNO_CONTROLLER_ID),
            ],
        )

    def test_superadmin_controls_both_phones_but_poco_stays_disabled(self):
        self.activate_post()
        redmi = self.queue(
            "fwd:redmi:off",
            callback_id="super-redmi",
            actor_id=ADMIN_ID,
        )
        self.assertTrue(redmi["queued"])

        tecno = self.queue(
            "fwd:tecno:off",
            callback_id="super-tecno",
            actor_id=ADMIN_ID,
            now_ts=self.timestamp(second=3),
        )
        self.assertTrue(tecno["queued"])

        poco = self.queue(
            "fwd:poco:off",
            callback_id="super-poco",
            actor_id=ADMIN_ID,
            now_ts=self.timestamp(second=4),
        )
        self.assertEqual(poco["reason"], "invalid")

    def test_invalid_callback_never_uses_device_acl(self):
        self.activate_post()
        for index, callback_data in enumerate(
            ("", "fwd", "fwd:unknown:off", "other:redmi:off")
        ):
            result = self.queue(
                callback_data,
                callback_id=f"invalid-{index}",
                actor_id=REDMI_CONTROLLER_ID,
            )
            self.assertEqual(result["reason"], "invalid")

    def test_username_cannot_spoof_numeric_telegram_id(self):
        post = self.activate_post()
        result = self.service.queue_callback(
            callback_query_id="spoofed-name",
            callback_data="fwd:redmi:poco",
            telegram_user={"id": 999, "username": "AbbosTch"},
            chat_id=CHAT_ID,
            message_id=post["message_id"],
            now_ts=self.timestamp(second=1),
        )
        self.assertEqual(result["reason"], "forbidden")
        with self.repository.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forwarding_operations"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_actor_id_must_be_an_exact_positive_json_integer(self):
        post = self.activate_post()
        for index, actor_id in enumerate(
            (
                float(REDMI_CONTROLLER_ID),
                REDMI_CONTROLLER_ID + 0.9,
                str(REDMI_CONTROLLER_ID),
                True,
                0,
                -REDMI_CONTROLLER_ID,
                None,
            )
        ):
            result = self.service.queue_callback(
                callback_query_id=f"invalid-actor-{index}",
                callback_data="fwd:redmi:poco",
                telegram_user={"id": actor_id, "username": "AbbosTch"},
                chat_id=CHAT_ID,
                message_id=post["message_id"],
                now_ts=self.timestamp(second=1),
            )
            self.assertEqual(result["reason"], "forbidden")

        with self.repository.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forwarding_operations"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_callback_query_id_is_required_and_denial_does_not_poison_it(self):
        post = self.activate_post()
        for callback_id in (None, "", "   "):
            result = self.service.queue_callback(
                callback_query_id=callback_id,
                callback_data="fwd:redmi:poco",
                telegram_user={"id": REDMI_CONTROLLER_ID},
                chat_id=CHAT_ID,
                message_id=post["message_id"],
                now_ts=self.timestamp(second=1),
            )
            self.assertEqual(result["reason"], "invalid")

        denied = self.service.queue_callback(
            callback_query_id="not-poisoned",
            callback_data="fwd:redmi:poco",
            telegram_user={"id": 999},
            chat_id=CHAT_ID,
            message_id=post["message_id"],
            now_ts=self.timestamp(second=1),
        )
        self.assertEqual(denied["reason"], "forbidden")
        allowed = self.service.queue_callback(
            callback_query_id="not-poisoned",
            callback_data="fwd:redmi:poco",
            telegram_user={"id": REDMI_CONTROLLER_ID},
            chat_id=CHAT_ID,
            message_id=post["message_id"],
            now_ts=self.timestamp(second=1),
        )
        self.assertTrue(allowed["queued"])

    def test_acl_environment_defaults_and_superadmin_are_stable(self):
        with mock.patch.dict(
            os.environ,
            {
                "FORWARDING_ADMIN_IDS": "999, bad-value",
                "FORWARDING_REDMI_CONTROLLER_IDS": (
                    f"{REDMI_CONTROLLER_ID}, 111"
                ),
                "FORWARDING_TECNO_CONTROLLER_IDS": "",
            },
            clear=False,
        ):
            settings = load_forwarding_settings()

        self.assertEqual(settings.admin_ids, frozenset({ADMIN_ID, 999}))
        self.assertEqual(
            settings.device_controller_ids["redmi"],
            frozenset({REDMI_CONTROLLER_ID, 111}),
        )
        self.assertEqual(
            settings.device_controller_ids["tecno"],
            frozenset({TECNO_CONTROLLER_ID}),
        )

    def test_forwarding_defaults_to_disabled(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORWARDING_ENABLED", None)
            settings = load_forwarding_settings()
        self.assertFalse(settings.enabled)

    def test_parallel_clicks_create_only_one_active_operation(self):
        self.activate_post()

        def submit(index):
            return self.queue(
                "fwd:redmi:poco",
                callback_id=f"parallel-{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(8)))

        self.assertEqual(sum(bool(item["queued"]) for item in results), 1)
        with self.repository.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forwarding_operations"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_dispatch_preserves_star_plus_hash_and_waits_for_finish(self):
        self.activate_post()
        queued = self.queue("fwd:redmi:poco")
        operation_id = queued["operation"]["id"]

        dispatched = self.service.dispatch_one(
            self.timestamp(second=2)
        )
        self.assertEqual(dispatched["status"], "api_accepted")
        self.assertEqual(
            self.api_calls,
            [
                (
                    "aashshdjdjdjsj@gmail.com",
                    "**21*+998901313999*11#",
                )
            ],
        )
        self.assertEqual(
            self.repository.get_operation(operation_id)["status"],
            "api_accepted",
        )

        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            {
                "direction": 1,
                "client_number": "**21*+998901313999*11#",
                "answered": 1,
                "db_call_id": 7001,
                "event_pbx_call_id": "fwd-7001",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998908534466",
                "src_slot": 0,
            },
            now_ts=self.timestamp(second=9),
        )
        self.assertTrue(result["handled"])
        self.assertTrue(result["terminal"])
        operation = self.repository.get_operation(operation_id)
        self.assertEqual(operation["status"], "call_completed")
        device = self.repository.get_device("redmi")
        self.assertEqual(device["forwarding_status"], "enabled_unverified")
        self.assertEqual(device["forwarding_target_code"], "poco")

    def test_queued_command_cannot_be_completed_before_api_dispatch(self):
        self.activate_post()
        queued = self.queue(
            "fwd:redmi:poco",
            now_ts=self.timestamp(second=20),
        )
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            {
                "direction": 1,
                "client_number": "**21*+998901313999*11#",
                "answered": 1,
                "db_call_id": 7099,
                "event_pbx_call_id": "before-dispatch",
                "start_time": self.timestamp(second=10),
                "end_time": self.timestamp(second=15),
                "src_number": "+998908534466",
                "src_slot": 0,
            },
            now_ts=self.timestamp(second=21),
        )
        self.assertTrue(result["handled"])
        self.assertTrue(result["external"])
        self.assertEqual(
            self.repository.get_operation(
                queued["operation"]["id"]
            )["status"],
            "queued",
        )
        self.assertEqual(self.api_calls, [])

    def test_cancel_not_answered_is_reported_as_not_completed(self):
        self.activate_post()
        queued = self.queue("fwd:tecno:off")
        self.service.dispatch_one(self.timestamp(second=2))
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            {
                "direction": 1,
                "client_number": "##21#",
                "answered": 0,
                "db_call_id": 7002,
                "event_pbx_call_id": "fwd-7002",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=4),
            },
            now_ts=self.timestamp(second=5),
        )
        self.assertTrue(result["handled"])
        operation = self.repository.get_operation(
            queued["operation"]["id"]
        )
        self.assertEqual(operation["status"], "call_not_completed")
        self.assertEqual(
            self.repository.get_device("tecno")["forwarding_status"],
            "unknown",
        )

    def test_wrong_sim_never_marks_forwarding_as_enabled(self):
        self.activate_post()
        queued = self.queue("fwd:redmi:poco")
        self.service.dispatch_one(self.timestamp(second=2))
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            {
                "direction": 1,
                "client_number": "**21*+998901313999*11#",
                "answered": 1,
                "db_call_id": 7101,
                "event_pbx_call_id": "wrong-sim",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998900000000",
                "src_slot": 1,
                "src_id": 99,
            },
            now_ts=self.timestamp(second=9),
        )
        self.assertTrue(result["handled"])
        operation = self.repository.get_operation(
            queued["operation"]["id"]
        )
        self.assertEqual(operation["status"], "call_wrong_sim")
        self.assertEqual(operation["provider_src_slot"], 1)
        self.assertEqual(operation["provider_src_id"], 99)
        device = self.repository.get_device("redmi")
        self.assertEqual(device["forwarding_status"], "unknown")
        self.assertIsNone(device["forwarding_target_code"])

    def test_missing_sim_identity_is_terminal_but_not_success(self):
        self.activate_post()
        queued = self.queue("fwd:tecno:poco")
        self.service.dispatch_one(self.timestamp(second=2))
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            {
                "direction": 1,
                "client_number": "**21*+998901313999*11#",
                "answered": 1,
                "db_call_id": 7102,
                "event_pbx_call_id": "missing-sim",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
            },
            now_ts=self.timestamp(second=9),
        )
        self.assertTrue(result["terminal"])
        operation = self.repository.get_operation(
            queued["operation"]["id"]
        )
        self.assertEqual(
            operation["status"],
            "call_completed_sim_unverified",
        )
        self.assertEqual(
            self.repository.get_device("tecno")["forwarding_status"],
            "unknown",
        )

    def test_finish_reuses_matching_sim_identity_from_start_event(self):
        self.activate_post()
        queued = self.queue("fwd:tecno:redmi")
        self.service.dispatch_one(self.timestamp(second=2))
        webhook = {"user_login": "texnikacholx@gmail.com"}
        self.service.handle_provider_event(
            "call.start",
            webhook,
            {
                "direction": 1,
                "client_number": "**21*+998908534466*11#",
                "event_pbx_call_id": "identity-on-start",
                "start_time": self.timestamp(second=3),
                "src_number": "+998908456162",
                "src_slot": 0,
            },
            now_ts=self.timestamp(second=3),
        )
        self.service.handle_provider_event(
            "call.finish",
            webhook,
            {
                "direction": 1,
                "client_number": "**21*+998908534466*11#",
                "event_pbx_call_id": "identity-on-start",
                "db_call_id": 7103,
                "answered": 1,
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
            },
            now_ts=self.timestamp(second=9),
        )
        self.assertEqual(
            self.repository.get_operation(
                queued["operation"]["id"]
            )["status"],
            "call_completed",
        )

    def test_wrong_sim_seen_on_start_cannot_be_hidden_by_finish(self):
        self.activate_post()
        queued = self.queue("fwd:tecno:redmi")
        self.service.dispatch_one(self.timestamp(second=2))
        webhook = {"user_login": "texnikacholx@gmail.com"}
        self.service.handle_provider_event(
            "call.start",
            webhook,
            {
                "direction": 1,
                "client_number": "**21*+998908534466*11#",
                "event_pbx_call_id": "contradictory-sim",
                "start_time": self.timestamp(second=3),
                "src_number": "+998900000000",
                "src_slot": 1,
            },
            now_ts=self.timestamp(second=3),
        )
        self.service.handle_provider_event(
            "call.finish",
            webhook,
            {
                "direction": 1,
                "client_number": "**21*+998908534466*11#",
                "event_pbx_call_id": "contradictory-sim",
                "db_call_id": 7104,
                "answered": 1,
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998908456162",
                "src_slot": 0,
            },
            now_ts=self.timestamp(second=9),
        )
        operation = self.repository.get_operation(
            queued["operation"]["id"]
        )
        self.assertEqual(operation["status"], "call_wrong_sim")
        self.assertEqual(operation["provider_src_slot"], 1)
        self.assertEqual(
            self.repository.get_device("tecno")["forwarding_status"],
            "unknown",
        )

    def test_duplicate_target_only_finish_stays_out_of_customer_pipeline(self):
        self.activate_post()
        self.queue("fwd:redmi:poco")
        self.service.dispatch_one(self.timestamp(second=2))
        webhook = {"user_login": "aashshdjdjdjsj@gmail.com"}
        event = {
            "direction": 1,
            # Some Android dialers may report only the destination part.
            "client_number": "+998901313999",
            "answered": 1,
            "db_call_id": 7003,
            "event_pbx_call_id": "fwd-7003",
            "start_time": self.timestamp(second=3),
            "end_time": self.timestamp(second=8),
            "src_number": "+998908534466",
            "src_slot": 0,
        }
        first = self.service.handle_provider_event(
            "call.finish",
            webhook,
            event,
            now_ts=self.timestamp(second=9),
        )
        duplicate = self.service.handle_provider_event(
            "call.finish",
            webhook,
            event,
            now_ts=self.timestamp(second=10),
        )
        self.assertTrue(first["handled"])
        self.assertTrue(duplicate["handled"])
        self.assertTrue(duplicate["duplicate"])

    def test_collapsed_service_number_is_suppressed_without_active_operation(self):
        event = {
            "direction": 1,
            # Failed Android URI handling may strip every star/hash and move
            # the plus to the front of the complete MMI digit signature.
            "client_number": "+2199890131399911",
            "answered": 0,
            "db_call_id": 7110,
            "event_pbx_call_id": "collapsed-service",
            "start_time": self.timestamp(second=3),
            "end_time": self.timestamp(second=8),
            "src_number": "+998908456162",
            "src_slot": 1,
        }
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            event,
            now_ts=self.timestamp(second=9),
        )
        self.assertTrue(result["handled"])
        self.assertTrue(result["external"])
        self.assertTrue(
            self.service.is_known_service_event(
                {"user_login": "texnikacholx@gmail.com"},
                event,
            )
        )

    def test_encoded_transport_artifact_is_suppressed_and_correlated(self):
        self.activate_post()
        queued = self.queue("fwd:redmi:poco")
        self.service.dispatch_one(self.timestamp(second=2))
        event = {
            "direction": 1,
            # Observed on Redmi after calls.make_call received
            # **21*%2B998901313999*11%23.
            "client_number": "+2129989013139991123",
            "answered": 0,
            "db_call_id": 7113,
            "event_pbx_call_id": "encoded-transport-artifact",
            "start_time": self.timestamp(second=3),
            "end_time": self.timestamp(second=3),
            "src_number": "+998908534466",
            "src_slot": 0,
        }
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            event,
            now_ts=self.timestamp(second=8),
        )
        self.assertTrue(result["handled"])
        self.assertEqual(
            self.repository.get_operation(
                queued["operation"]["id"]
            )["status"],
            "call_not_completed",
        )
        self.assertTrue(
            self.service.is_known_service_event(
                {"user_login": "aashshdjdjdjsj@gmail.com"},
                event,
            )
        )

    def test_encoded_transport_artifacts_are_exactly_scoped(self):
        cancel_event = {
            "direction": 1,
            "client_number": "+23232123",
        }
        unrelated_event = {
            "direction": 1,
            "client_number": "+2129989013139991124",
        }
        webhook = {"user_login": "texnikacholx@gmail.com"}
        self.assertTrue(
            self.service.is_known_service_event(webhook, cancel_event)
        )
        self.assertFalse(
            self.service.is_known_service_event(webhook, unrelated_event)
        )

    def test_legacy_collapsed_service_number_is_also_suppressed(self):
        event = {
            "direction": 1,
            "client_number": "+21998901313999",
            "answered": 0,
            "db_call_id": 7111,
            "event_pbx_call_id": "legacy-collapsed-service",
            "start_time": self.timestamp(second=3),
            "end_time": self.timestamp(second=8),
            "src_number": "+998908456162",
            "src_slot": 1,
        }
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            event,
            now_ts=self.timestamp(second=9),
        )
        self.assertTrue(result["handled"])
        self.assertTrue(result["external"])

    def test_plain_target_is_not_service_without_active_operation(self):
        event = {
            "direction": 1,
            "client_number": "+998901313999",
            "answered": 0,
            "db_call_id": 7112,
            "event_pbx_call_id": "ordinary-target-call",
            "start_time": self.timestamp(second=3),
            "end_time": self.timestamp(second=8),
            "src_number": "+998908456162",
            "src_slot": 1,
        }
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            event,
            now_ts=self.timestamp(second=9),
        )
        self.assertFalse(result["handled"])
        self.assertFalse(
            self.service.is_known_service_event(
                {"user_login": "texnikacholx@gmail.com"},
                event,
            )
        )

    def test_exact_sim_number_wins_over_zero_based_slot(self):
        self.activate_post()
        queued = self.queue("fwd:tecno:poco")
        self.service.dispatch_one(self.timestamp(second=2))
        self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            {
                "direction": 1,
                "client_number": "**21*+998901313999*11#",
                "answered": 1,
                "db_call_id": 7113,
                "event_pbx_call_id": "zero-based-slot",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998908456162",
                "src_slot": 0,
            },
            now_ts=self.timestamp(second=9),
        )
        operation = self.repository.get_operation(
            queued["operation"]["id"]
        )
        self.assertEqual(operation["status"], "call_completed")

    def test_second_distinct_finish_cannot_overwrite_terminal_result(self):
        self.activate_post()
        queued = self.queue("fwd:redmi:poco")
        operation_id = queued["operation"]["id"]
        self.service.dispatch_one(self.timestamp(second=2))
        first = self.repository.mark_provider_event(
            operation_id,
            "call.finish",
            {
                "answered": 1,
                "db_call_id": 7201,
                "event_pbx_call_id": "terminal-first",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998908534466",
                "src_slot": 0,
            },
            self.timestamp(second=9),
        )
        second = self.repository.mark_provider_event(
            operation_id,
            "call.finish",
            {
                "answered": 0,
                "db_call_id": 7202,
                "event_pbx_call_id": "terminal-second",
                "start_time": self.timestamp(second=4),
                "end_time": self.timestamp(second=5),
                "src_number": "+998908534466",
                "src_slot": 0,
            },
            self.timestamp(second=10),
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        operation = self.repository.get_operation(operation_id)
        self.assertEqual(operation["status"], "call_completed")
        self.assertEqual(operation["provider_db_call_id"], 7201)

    def test_start_answer_finish_with_same_pbx_reaches_terminal_state(self):
        self.activate_post()
        queued = self.queue("fwd:tecno:redmi")
        operation_id = queued["operation"]["id"]
        self.service.dispatch_one(self.timestamp(second=2))
        webhook = {"user_login": "texnikacholx@gmail.com"}
        base_event = {
            "direction": 1,
            "client_number": "**21*+998908534466*11#",
            "event_pbx_call_id": "fwd-sequence",
            "start_time": self.timestamp(second=3),
            "src_number": "+998908456162",
            "src_slot": 0,
        }

        started = self.service.handle_provider_event(
            "call.start",
            webhook,
            dict(base_event),
            now_ts=self.timestamp(second=3),
        )
        answered = self.service.handle_provider_event(
            "call.answer",
            webhook,
            dict(base_event),
            now_ts=self.timestamp(second=4),
        )
        finished_event = dict(
            base_event,
            answered=1,
            db_call_id=7004,
            end_time=self.timestamp(second=9),
        )
        finished = self.service.handle_provider_event(
            "call.finish",
            webhook,
            finished_event,
            now_ts=self.timestamp(second=10),
        )

        self.assertTrue(started["handled"])
        self.assertTrue(answered["handled"])
        self.assertTrue(finished["handled"])
        self.assertTrue(finished["terminal"])
        self.assertEqual(
            self.repository.get_operation(operation_id)["status"],
            "call_completed",
        )

    def test_timeout_is_ambiguous_and_never_retries_api(self):
        self.activate_post()
        queued = self.queue("fwd:redmi:tecno")
        self.service.dispatch_one(self.timestamp(second=2))
        expired = self.repository.expire_operations(
            self.timestamp(hour=20, minute=3),
            timeout_seconds=120,
        )
        self.assertEqual(expired, 1)
        self.assertEqual(
            self.repository.get_operation(
                queued["operation"]["id"]
            )["status"],
            "unconfirmed",
        )
        self.service.dispatch_one(self.timestamp(hour=20, minute=4))
        self.assertEqual(len(self.api_calls), 1)

    def test_disabled_setting_rejects_callbacks_without_queueing(self):
        self.activate_post()
        disabled = ForwardingService(
            repository=self.repository,
            settings=ForwardingSettings(
                **{
                    **self.settings.__dict__,
                    "enabled": False,
                }
            ),
            chat_id=CHAT_ID,
            telegram_api=self.telegram,
            make_call=lambda *_: self.fail("API must not be called"),
            local_timezone=UZ_TZ,
        )
        post = self.repository.get_current_post()
        result = disabled.queue_callback(
            callback_query_id="disabled",
            callback_data="fwd:redmi:poco",
            telegram_user={"id": ADMIN_ID},
            chat_id=CHAT_ID,
            message_id=post["message_id"],
            now_ts=self.timestamp(second=1),
        )
        self.assertEqual(result["reason"], "disabled")
        with self.repository.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forwarding_operations"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertTrue(disabled.refresh_current_post())
        method, payload = self.telegram.calls[-1]
        self.assertEqual(method, "editMessageText")
        self.assertIn("временно отключены", payload["text"])
        self.assertEqual(
            json.loads(payload["reply_markup"]),
            {"inline_keyboard": []},
        )

    def test_unconfirmed_blocks_repeat_until_correlation_window_ends(self):
        self.activate_post()
        old = self.queue("fwd:redmi:poco", callback_id="old")
        self.service.dispatch_one(self.timestamp(second=2))
        self.repository.expire_operations(
            self.timestamp(hour=20, minute=3),
            timeout_seconds=120,
        )

        new_time = self.timestamp(hour=20, minute=4)
        new = self.queue(
            "fwd:redmi:poco",
            callback_id="new",
            now_ts=new_time,
        )
        self.assertFalse(new["queued"])
        self.assertEqual(new["reason"], "unconfirmed")
        self.assertEqual(new["operation"]["id"], old["operation"]["id"])

        with self.repository.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM forwarding_operations"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_delayed_old_finish_uses_call_time_not_delivery_time(self):
        self.activate_post()
        old = self.queue("fwd:redmi:poco", callback_id="old")
        self.service.dispatch_one(self.timestamp(second=2))
        self.repository.expire_operations(
            self.timestamp(hour=20, minute=3),
            timeout_seconds=120,
        )

        new_time = self.timestamp(hour=20, minute=5, second=2)
        new = self.queue(
            "fwd:redmi:poco",
            callback_id="new-after-window",
            now_ts=new_time,
        )
        self.assertTrue(new["queued"])
        self.service.dispatch_one(new_time + 1)
        late = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            {
                "direction": 1,
                # Android may upload this two hours later and keep only the
                # destination part of the original control command.
                "client_number": "+998901313999",
                "answered": 1,
                "db_call_id": 7005,
                "event_pbx_call_id": "old-late",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998908534466",
                "src_slot": 0,
            },
            now_ts=self.timestamp(hour=22),
        )
        self.assertTrue(late["handled"])
        self.assertEqual(
            late["operation"]["id"],
            old["operation"]["id"],
        )
        self.assertEqual(
            self.repository.get_operation(
                new["operation"]["id"]
            )["status"],
            "api_accepted",
        )

    def test_provider_ids_are_scoped_by_moizvonki_user(self):
        self.activate_post()
        redmi = self.queue(
            "fwd:redmi:poco",
            callback_id="redmi-same-id",
        )
        tecno = self.queue(
            "fwd:tecno:poco",
            callback_id="tecno-same-id",
        )
        self.service.dispatch_one(self.timestamp(second=2))
        self.service.dispatch_one(self.timestamp(second=3))
        shared_event = {
            "direction": 1,
            "client_number": "**21*+998901313999*11#",
            "answered": 1,
            "db_call_id": 7777,
            "event_pbx_call_id": "shared-pbx-id",
            "start_time": self.timestamp(second=4),
            "end_time": self.timestamp(second=9),
        }
        first = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            dict(
                shared_event,
                src_number="+998908534466",
                src_slot=0,
            ),
            now_ts=self.timestamp(second=10),
        )
        second = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "texnikacholx@gmail.com"},
            dict(
                shared_event,
                src_number="+998908456162",
                src_slot=0,
            ),
            now_ts=self.timestamp(second=10),
        )
        self.assertTrue(first["handled"])
        self.assertTrue(second["handled"])
        self.assertEqual(
            self.repository.get_operation(
                redmi["operation"]["id"]
            )["status"],
            "call_completed",
        )
        self.assertEqual(
            self.repository.get_operation(
                tecno["operation"]["id"]
            )["status"],
            "call_completed",
        )

    def test_external_exact_command_on_wrong_sim_is_logged_not_applied(self):
        self.activate_post()
        result = self.service.handle_provider_event(
            "call.finish",
            {"user_login": "aashshdjdjdjsj@gmail.com"},
            {
                "direction": 1,
                "client_number": "**21*+998901313999*11#",
                "answered": 1,
                "db_call_id": 7888,
                "event_pbx_call_id": "external-wrong-sim",
                "start_time": self.timestamp(second=3),
                "end_time": self.timestamp(second=8),
                "src_number": "+998900000000",
                "src_slot": 1,
            },
            now_ts=self.timestamp(second=9),
        )
        self.assertTrue(result["handled"])
        self.assertTrue(result["external"])
        self.assertEqual(result["operation"]["status"], "call_wrong_sim")
        self.assertEqual(
            self.repository.get_device("redmi")["forwarding_status"],
            "unknown",
        )

    def test_parallel_duplicate_external_finish_is_one_audit_row(self):
        self.activate_post()
        webhook = {"user_login": "aashshdjdjdjsj@gmail.com"}
        event = {
            "direction": 1,
            "client_number": "##21#",
            "answered": 1,
            "db_call_id": 7889,
            "event_pbx_call_id": "parallel-external",
            "start_time": self.timestamp(second=3),
            "end_time": self.timestamp(second=8),
            "src_number": "+998908534466",
            "src_slot": 0,
        }

        def submit(_):
            return self.service.handle_provider_event(
                "call.finish",
                webhook,
                dict(event),
                now_ts=self.timestamp(second=9),
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(16)))

        self.assertTrue(all(item["handled"] for item in results))
        with self.repository.connect() as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM forwarding_operations
                WHERE moizvonki_user = ? AND provider_db_call_id = ?
                """,
                ("aashshdjdjdjsj@gmail.com", 7889),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_concurrent_schema_upgrade_adds_columns_once(self):
        with self.repository.connect() as conn:
            conn.execute(
                "ALTER TABLE forwarding_operations "
                "DROP COLUMN provider_src_number"
            )
            conn.execute(
                "ALTER TABLE forwarding_operations "
                "DROP COLUMN provider_src_slot"
            )
            conn.execute(
                "ALTER TABLE forwarding_operations "
                "DROP COLUMN provider_src_id"
            )
            conn.commit()

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(
                lambda _: self.repository.init_schema(),
                range(8),
            ))

        with self.repository.connect() as conn:
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(forwarding_operations)"
                ).fetchall()
            }
        self.assertTrue(
            {
                "provider_src_number",
                "provider_src_slot",
                "provider_src_id",
            }.issubset(columns)
        )

    def test_late_http_timeout_cannot_erase_terminal_phone_result(self):
        self.activate_post()
        queued = self.queue("fwd:redmi:poco")

        def finish_then_timeout(user_login, service_number):
            result = self.service.handle_provider_event(
                "call.finish",
                {"user_login": user_login},
                {
                    "direction": 1,
                    "client_number": service_number,
                    "answered": 1,
                    "db_call_id": 7999,
                    "event_pbx_call_id": "finish-before-http",
                    "start_time": self.timestamp(second=2),
                    "end_time": self.timestamp(second=4),
                    "src_number": "+998908534466",
                    "src_slot": 0,
                },
                now_ts=self.timestamp(second=5),
            )
            self.assertTrue(result["terminal"])
            import requests

            raise requests.Timeout("late HTTP timeout")

        racing_service = ForwardingService(
            repository=self.repository,
            settings=self.settings,
            chat_id=CHAT_ID,
            telegram_api=self.telegram,
            make_call=finish_then_timeout,
            local_timezone=UZ_TZ,
        )
        dispatched = racing_service.dispatch_one(self.timestamp(second=2))
        self.assertEqual(dispatched["status"], "call_completed")
        operation = self.repository.get_operation(
            queued["operation"]["id"]
        )
        self.assertEqual(operation["status"], "call_completed")
        self.assertEqual(
            self.repository.get_device("redmi")["forwarding_status"],
            "enabled_unverified",
        )


class ForwardingBotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_moizvonki_webhook_rejects_wrong_secret_before_json(self):
        import botmoizvonki as bot

        class Request:
            headers = {}
            query_params = {"secret": "wrong"}

            @staticmethod
            async def json():
                raise AssertionError("JSON must not be read before auth")

        with mock.patch.object(
            bot,
            "MOIZVONKI_WEBHOOK_SECRET",
            "expected",
        ):
            with self.assertRaises(bot.HTTPException) as raised:
                await bot.moizvonki_webhook(Request())
        self.assertEqual(raised.exception.status_code, 403)

    async def test_telegram_webhook_rejects_wrong_secret_before_json(self):
        import botmoizvonki as bot

        class Request:
            headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}

            @staticmethod
            async def json():
                raise AssertionError("JSON must not be read before auth")

        with mock.patch.object(
            bot,
            "TELEGRAM_WEBHOOK_SECRET",
            "expected",
        ):
            with self.assertRaises(bot.HTTPException) as raised:
                await bot.telegram_webhook(Request())
        self.assertEqual(raised.exception.status_code, 403)

    async def test_access_log_filter_removes_query_secrets(self):
        import botmoizvonki as bot

        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=(
                "127.0.0.1:1234",
                "POST",
                "/webhooks/moizvonki?secret=never-log-this",
                "1.1",
                200,
            ),
            exc_info=None,
        )
        filter_instance = bot._RedactAccessQueryFilter()
        self.assertTrue(filter_instance.filter(record))
        self.assertEqual(record.args[2], "/webhooks/moizvonki")

    async def test_make_call_rejects_mmi_before_http(self):
        import botmoizvonki as bot

        fake_http = mock.Mock()
        with (
            mock.patch.object(bot, "HTTP", fake_http),
            mock.patch.object(bot, "MOIZVONKI_API_URL", "https://example.test/api/v1"),
            mock.patch.object(bot, "MOIZVONKI_API_KEY", "secret"),
        ):
            for command in (
                "**21*+998901313999*11#",
                "##21#",
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "безопасную отправку MMI",
                ):
                    bot.moizvonki_make_call(
                        "aashshdjdjdjsj@gmail.com",
                        command,
                    )
        fake_http.post.assert_not_called()

    async def test_invalid_e164_number_never_reaches_sms_api(self):
        import botmoizvonki as bot

        malformed = "+2129989013139991123"
        with mock.patch.object(
            bot,
            "connect_db",
            side_effect=AssertionError("database must not be touched"),
        ):
            promo = bot.reserve_client_sms(
                123,
                malformed,
                "aashshdjdjdjsj@gmail.com",
            )
            rating = bot.reserve_call_rating(
                123,
                malformed,
                "aashshdjdjdjsj@gmail.com",
            )
        self.assertEqual(promo["reason"], "invalid_number")
        self.assertEqual(rating["reason"], "invalid_number")

        fake_http = mock.Mock()
        with (
            mock.patch.object(bot, "HTTP", fake_http),
            mock.patch.object(bot, "MOIZVONKI_API_URL", "https://example.test/api/v1"),
            mock.patch.object(bot, "MOIZVONKI_API_KEY", "secret"),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "Некорректная длина",
            ):
                bot.send_client_sms(
                    malformed,
                    "aashshdjdjdjsj@gmail.com",
                )
        fake_http.post.assert_not_called()

    async def test_matching_service_finish_never_enters_customer_pipeline(self):
        import botmoizvonki as bot

        service = mock.Mock()
        service.handle_provider_event.return_value = {
            "handled": True,
            "terminal": True,
        }

        class Request:
            headers = {}
            query_params = {"secret": "provider-secret"}

            @staticmethod
            async def json():
                return {
                    "webhook": {
                        "action": "call.finish",
                        "user_login": "aashshdjdjdjsj@gmail.com",
                    },
                    "event": {
                        "direction": 1,
                        "client_number": "##21#",
                        "answered": 1,
                        "db_call_id": 8001,
                    },
                }

        with (
            mock.patch.object(bot, "get_forwarding_service", return_value=service),
            mock.patch.object(bot, "save_call") as save_call,
            mock.patch.object(
                bot,
                "MOIZVONKI_WEBHOOK_SECRET",
                "provider-secret",
            ),
        ):
            result = await bot.moizvonki_webhook(Request())

        self.assertTrue(result["ok"])
        self.assertIn("forwarding", result)
        save_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
