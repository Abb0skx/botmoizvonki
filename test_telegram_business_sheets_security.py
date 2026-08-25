from __future__ import annotations

import copy
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

from telegram_business.config import BusinessSettings
from telegram_business.migrations import connect
from telegram_business.products import ProductMatch
from telegram_business.repository import BusinessRepository
from telegram_business.service import BusinessService
from telegram_business.security import redact_payment_data, sanitize_telegram_payload
from telegram_business.sheets import BusinessSheets, INTENT_SEED, IntentOverride, SHEETS
from telegram_business.telegram_api import (
    TelegramAPIError,
    TelegramBusinessAPI,
    make_callback_data,
    normalize_inline_keyboard,
    parse_callback_data,
)
from telegram_business.templates import normalize_template_code, render as builtin_render
from telegram_business.timeutils import manager_phrases


def _column_number(label: str) -> int:
    result = 0
    for char in label:
        result = result * 26 + ord(char) - 64
    return result


class FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self.id = abs(hash(title)) % 1_000_000 + 1
        self.rows = copy.deepcopy(rows or [])
        self.fail_reads = False
        self.frozen = None
        self.filter_range = None
        self.formats = []
        self.read_calls = 0
        self.write_calls = 0

    def get_all_values(self):
        self.read_calls += 1
        if self.fail_reads:
            raise RuntimeError("provider failed with secret material")
        return copy.deepcopy(self.rows)

    def update_title(self, title):
        self.title = title

    def update(self, *args, **kwargs):
        self.write_calls += 1
        if isinstance(args[0], str):
            range_name, values = args[0], args[1]
        else:
            values, range_name = args[0], kwargs["range_name"]
        match = re.match(r"([A-Z]+)(\d+)", range_name)
        assert match
        start_col = _column_number(match.group(1)) - 1
        start_row = int(match.group(2)) - 1
        for row_offset, row in enumerate(values):
            target_row = start_row + row_offset
            while len(self.rows) <= target_row:
                self.rows.append([])
            needed = start_col + len(row)
            self.rows[target_row].extend([""] * max(0, needed - len(self.rows[target_row])))
            self.rows[target_row][start_col:needed] = copy.deepcopy(row)

    def batch_update(self, updates, raw=True):
        assert raw is True
        self.write_calls += 1
        for update in updates:
            range_name, values = update["range"], update["values"]
            match = re.match(r"([A-Z]+)(\d+)", range_name)
            assert match
            start_col = _column_number(match.group(1)) - 1
            start_row = int(match.group(2)) - 1
            for row_offset, row in enumerate(values):
                target_row = start_row + row_offset
                while len(self.rows) <= target_row:
                    self.rows.append([])
                needed = start_col + len(row)
                self.rows[target_row].extend(
                    [""] * max(0, needed - len(self.rows[target_row]))
                )
                self.rows[target_row][start_col:needed] = copy.deepcopy(row)

    def append_row(self, values, **kwargs):
        self.rows.append(copy.deepcopy(values))

    def freeze(self, rows):
        self.frozen = rows

    def set_basic_filter(self, value):
        self.filter_range = value

    def format(self, range_name, value):
        self.formats.append((range_name, value))


class FakeBook:
    def __init__(self, sheets):
        self.sheets = list(sheets)
        self.batch_requests = []

    def worksheets(self):
        return list(self.sheets)

    def worksheet(self, title):
        for sheet in self.sheets:
            if sheet.title == title:
                return sheet
        raise KeyError(title)

    def add_worksheet(self, title, rows, cols):
        sheet = FakeWorksheet(title)
        self.sheets.append(sheet)
        return sheet

    def batch_update(self, request):
        self.batch_requests.append(request)


class FakeClient:
    def __init__(self, book):
        self.book = book

    def open_by_key(self, key):
        return self.book


class FakeRepo:
    def __init__(self, outbox=None):
        self.rows = list(outbox or [])
        self.done = []
        self.retried = []
        self.claims = []

    def outbox_due(self, now, limit=100, lease_seconds=120):
        selected = self.rows[:limit]
        del self.rows[:len(selected)]
        self.claims.append((now, limit, lease_seconds, [row["id"] for row in selected]))
        return selected

    def outbox_done(self, row_id, now):
        self.done.append(row_id)

    def outbox_retry(self, row_id, now, attempts, error):
        self.retried.append((row_id, attempts, error))


def _content_book():
    auto = FakeWorksheet(
        "Автоответы",
        [
            SHEETS["Автоответы"],
            ["credit", "TRUE", "all", "100", "Кастом: {catalog_url}", "Maxsus: {catalog_url}", "720", "", ""],
        ],
    )
    intents = FakeWorksheet(
        "Интенты",
        [
            SHEETS["Интенты"],
            ["credit", "TRUE", "all", "100", "кредит;рассрочка", "kredit;nasiya", "кредит не нужен", "credit", "FALSE", ""],
        ],
    )
    settings = FakeWorksheet(
        "Настройки",
        [
            SHEETS["Настройки"],
            ["catalog_url", "https://example.test/catalog", "string", "", ""],
            ["template_cache_seconds", "300", "int", "", ""],
        ],
    )
    return FakeBook([auto, intents, settings])


class SheetsContentTests(unittest.TestCase):
    def test_cached_reads_do_not_wait_for_google_refresh(self):
        sheets = BusinessSheets("sheet", FakeRepo())
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        read_finished = threading.Event()

        def slow_initialize():
            refresh_started.set()
            release_refresh.wait(timeout=3)
            raise RuntimeError("Google unavailable")

        def read_cache():
            sheets.settings_cached()
            sheets.intents_cached()
            sheets.render_cached("credit", "ru")
            read_finished.set()

        with patch.object(sheets, "initialize", side_effect=slow_initialize):
            refresh_thread = threading.Thread(
                target=lambda: sheets.refresh_content(
                    datetime(2026, 8, 23, tzinfo=timezone.utc), force=True
                )
            )
            refresh_thread.start()
            self.assertTrue(refresh_started.wait(timeout=1))
            reader_thread = threading.Thread(target=read_cache)
            reader_thread.start()
            try:
                self.assertTrue(
                    read_finished.wait(timeout=0.5),
                    "cached Telegram reads waited for remote Google I/O",
                )
            finally:
                release_refresh.set()
                refresh_thread.join(timeout=2)
                reader_thread.join(timeout=2)

    def test_payment_redaction_removes_unlabelled_expiry_and_cvv(self):
        raw = "4242 4242 4242 4242 12/29 123"
        redacted = redact_payment_data(raw)
        self.assertNotIn("4242", redacted)
        self.assertNotIn("12/29", redacted)
        self.assertNotIn("123", redacted)

        payload = {
            "update_id": 123456789012345,
            "business_message": {
                "chat": {"id": 998901234567},
                "text": raw,
            },
        }
        sanitized = sanitize_telegram_payload(payload)
        self.assertEqual(sanitized["update_id"], payload["update_id"])
        self.assertEqual(
            sanitized["business_message"]["chat"]["id"],
            payload["business_message"]["chat"]["id"],
        )
        self.assertNotIn("12/29", sanitized["business_message"]["text"])

    def test_payment_redaction_removes_contextual_code_and_payment_object(self):
        raw = "карта 8600 1234 1234 1234, срок 12/29, код 123"
        redacted = redact_payment_data(raw)
        self.assertNotIn("8600", redacted)
        self.assertNotIn("12/29", redacted)
        self.assertNotIn("код 123", redacted)

        payload = {
            "update_id": 123456789012345,
            "business_message": {
                "message_id": 777,
                "chat": {"id": 998901234567},
                "successful_payment": {
                    "telegram_payment_charge_id": "charge-secret",
                    "provider_payment_charge_id": "provider-secret",
                    "order_info": {"phone_number": "+998901234567"},
                },
            },
        }
        sanitized = sanitize_telegram_payload(payload)
        self.assertEqual(sanitized["update_id"], payload["update_id"])
        self.assertEqual(
            sanitized["business_message"]["chat"]["id"],
            payload["business_message"]["chat"]["id"],
        )
        self.assertEqual(
            sanitized["business_message"]["successful_payment"],
            "[PAYMENT_DATA_REDACTED]",
        )
        self.assertNotIn("charge-secret", repr(sanitized))
        self.assertNotIn("+998901234567", repr(sanitized))

    def test_bootstrap_only_seeds_empty_sheets_and_preserves_manual_body(self):
        empty_book = FakeBook([FakeWorksheet("Лист1")])
        with patch("telegram_business.sheets.google_client", return_value=FakeClient(empty_book)):
            sheets = BusinessSheets("sheet", FakeRepo())
            sheets.initialize()
        self.assertEqual({sheet.title for sheet in empty_book.sheets}, set(SHEETS))
        auto = empty_book.worksheet("Автоответы")
        self.assertEqual(auto.rows[0], SHEETS["Автоответы"])
        self.assertGreater(len(auto.rows), 2)
        self.assertEqual(auto.frozen, 1)
        self.assertTrue(empty_book.batch_requests)
        requests = [
            request
            for batch in empty_book.batch_requests
            for request in batch.get("requests", [])
        ]
        self.assertTrue(any("setDataValidation" in request for request in requests))

        manual = ["custom", "TRUE", "night", "5", "Ручной RU", "Qo‘lda UZ", "0", "", ""]
        existing = FakeBook(
            [FakeWorksheet(title, [headers, manual] if title == "Автоответы" else [headers, ["KEEP"]]) for title, headers in SHEETS.items()]
        )
        before = copy.deepcopy(existing.worksheet("Автоответы").rows)
        sheets = BusinessSheets("sheet", FakeRepo())
        sheets.book = existing
        sheets.initialize()
        after = existing.worksheet("Автоответы").rows
        # Missing approved codes may be appended, but the header and the
        # operator-authored row must remain byte-for-byte unchanged.
        self.assertEqual(after[:2], before)
        self.assertEqual(sum(row[0] == "custom" for row in after[1:]), 1)
        stable = copy.deepcopy(after)
        sheets.initialize()
        self.assertEqual(existing.worksheet("Автоответы").rows, stable)

    def test_cache_last_known_good_and_builtin_fallback(self):
        book = _content_book()
        sheets = BusinessSheets("sheet", FakeRepo(), cache_seconds=300)
        sheets.book = book
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        self.assertEqual(
            sheets.render("credit", "ru", now=now),
            "Кастом: https://example.test/catalog",
        )
        credit_intent = next(item for item in sheets.intents(now) if item.code == "credit")
        self.assertEqual(credit_intent.keywords_ru, ("кредит", "рассрочка"))
        self.assertEqual(sheets.setting("template_cache_seconds", now=now), 300)

        auto = book.worksheet("Автоответы")
        auto.rows[1][4] = "Новое: {catalog_url}"
        self.assertEqual(
            sheets.render("credit", "ru", now=now + timedelta(seconds=1)),
            "Кастом: https://example.test/catalog",
        )
        self.assertEqual(
            sheets.render("credit", "ru", now=now + timedelta(seconds=301)),
            "Новое: https://example.test/catalog",
        )

        auto.rows[1][4] = "Повреждено: {unknown_placeholder}"
        self.assertEqual(
            sheets.render("credit", "ru", now=now + timedelta(seconds=602)),
            "Новое: https://example.test/catalog",
        )
        auto.fail_reads = True
        self.assertEqual(
            sheets.render("credit", "ru", now=now + timedelta(seconds=903)),
            "Новое: https://example.test/catalog",
        )

        failed = BusinessSheets("sheet", FakeRepo())
        failed.book = _content_book()
        failed.book.worksheet("Автоответы").fail_reads = True
        self.assertIn("нет кредита", failed.render("credit", "ru", now=now))

    def test_cached_accessors_never_attempt_google_io(self):
        sheets = BusinessSheets("sheet", FakeRepo())
        with patch.object(
            sheets,
            "refresh_content",
            side_effect=AssertionError("cached accessor performed remote I/O"),
        ):
            self.assertIn("нет кредита", sheets.render_cached("credit", "ru"))
            self.assertEqual(sheets.intents_cached(), ())
            self.assertEqual(dict(sheets.settings_cached()), {})
            self.assertEqual(sheets.setting_cached("missing", "fallback"), "fallback")

        book = _content_book()
        sheets.book = book
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        sheets.refresh_content(now, force=True)
        book.worksheet("Автоответы").fail_reads = True
        self.assertEqual(
            sheets.render_cached("credit", "ru"),
            "Кастом: https://example.test/catalog",
        )
        self.assertTrue(any(item.code == "credit" for item in sheets.intents_cached()))

    def test_disabled_template_is_explicit_and_aliases_are_supported(self):
        book = _content_book()
        book.worksheet("Автоответы").rows[1][1] = "FALSE"
        sheets = BusinessSheets("sheet", FakeRepo())
        sheets.book = book
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        self.assertIsNone(sheets.render("credit", "ru", now=now))

        auto_rows = book.worksheet("Автоответы").rows
        handoff_index = next(
            index for index, row in enumerate(auto_rows)
            if row and row[0] == "human_handoff"
        )
        auto_rows[handoff_index] = [
            "human_handoff", "TRUE", "night", "100", "Менеджер {manager_time_phrase_ru}", "Menejer {manager_time_phrase_uz}", "0", "", ""
        ]
        self.assertEqual(
            sheets.render(
                "handoff",
                "ru",
                {"manager_time_phrase_ru": "после 10:00"},
                now=now + timedelta(seconds=301),
            ),
            "Менеджер после 10:00",
        )

    def test_malformed_boolean_keeps_last_known_template(self):
        book = _content_book()
        sheets = BusinessSheets("sheet", FakeRepo(), cache_seconds=300)
        sheets.book = book
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        self.assertEqual(
            sheets.render("credit", "ru", now=now),
            "Кастом: https://example.test/catalog",
        )
        book.worksheet("Автоответы").rows[1][1] = "BROKEN"
        self.assertEqual(
            sheets.render("credit", "ru", now=now + timedelta(seconds=301)),
            "Кастом: https://example.test/catalog",
        )

    def test_outbox_routes_and_upserts_all_runtime_tabs(self):
        book = FakeBook(
            [
                FakeWorksheet(title, [headers])
                for title, headers in SHEETS.items()
            ]
        )
        stats = book.worksheet("Статистика")
        stats.rows.append(["today", "total", "1", "old"])
        outbox = [
            {"id": 1, "entity_type": "message", "operation": "upsert", "attempts": 0, "payload": '{"event_id":"m1","chat_id":"42","text":"hi"}'},
            {"id": 2, "entity_type": "dialog", "operation": "upsert", "attempts": 0, "payload": '{"session_id":"s1","cycle_id":"","chat_id":"42"}'},
            {"id": 3, "entity_type": "error", "operation": "upsert", "attempts": 0, "payload": '{"error_id":"e1","message":"safe"}'},
            {"id": 4, "entity_type": "statistics", "operation": "upsert", "attempts": 0, "payload": '{"period":"today","metric":"total","value":2}'},
        ]
        repo = FakeRepo(outbox)
        sheets = BusinessSheets("sheet", repo)
        sheets.book = book
        sheets.sync_once(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(repo.done, [1, 2, 3, 4])
        self.assertFalse(repo.retried)
        self.assertEqual(len(book.worksheet("Сообщения").rows), 2)
        self.assertEqual(len(book.worksheet("Диалоги").rows), 2)
        self.assertEqual(len(book.worksheet("Ошибки").rows), 2)
        self.assertEqual(len(stats.rows), 2)
        self.assertEqual(stats.rows[1][2], "2")
        self.assertTrue(repo.claims)
        self.assertEqual(len(repo.claims), 1)
        self.assertEqual(repo.claims[0][1], 100)
        self.assertEqual(repo.claims[0][3], [1, 2, 3, 4])

    def test_outbox_rows_are_claimed_and_written_as_one_batch(self):
        book = FakeBook(
            [FakeWorksheet(title, [headers]) for title, headers in SHEETS.items()]
        )

        class ObservedRepo(FakeRepo):
            def __init__(self, outbox):
                super().__init__(outbox)
                self.row_counts_at_claim = []

            def outbox_due(self, now, limit=100, lease_seconds=120):
                self.row_counts_at_claim.append(
                    len(book.worksheet("Сообщения").rows)
                )
                return super().outbox_due(now, limit, lease_seconds)

        repo = ObservedRepo(
            [
                {"id": number, "entity_type": "message", "operation": "upsert", "attempts": 0,
                 "payload": '{"event_id":"m%s","chat_id":"42"}' % number}
                for number in range(1, 4)
            ]
        )
        sheets = BusinessSheets("sheet", repo)
        sheets.book = book
        sheets.sync_once(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(repo.row_counts_at_claim, [1])
        self.assertEqual(repo.claims[0][1], 100)
        self.assertEqual(repo.claims[0][3], [1, 2, 3])
        self.assertEqual(len(book.worksheet("Сообщения").rows), 4)
        self.assertEqual(book.worksheet("Сообщения").write_calls, 1)

    def test_statistics_snapshot_uses_one_remote_read_and_write(self):
        book = FakeBook(
            [FakeWorksheet(title, [headers]) for title, headers in SHEETS.items()]
        )
        rows = [
            {
                "id": number + 1,
                "entity_type": "statistic",
                "operation": "upsert",
                "attempts": 0,
                "payload": (
                    '{"period":"p%s","metric":"m%s","value":%s}'
                    % (number // 19, number % 19, number)
                ),
            }
            for number in range(76)
        ]
        repo = FakeRepo(rows)
        sheets = BusinessSheets("sheet", repo)
        sheets.book = book
        sheets._initialized = True
        stats = book.worksheet("Статистика")
        before_reads, before_writes = stats.read_calls, stats.write_calls
        sheets.sync_once(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(stats.read_calls - before_reads, 1)
        self.assertEqual(stats.write_calls - before_writes, 1)
        self.assertEqual(len(stats.rows), 77)
        self.assertEqual(len(repo.done), 76)

    def test_unchanged_remote_snapshot_is_not_written_again(self):
        book = FakeBook(
            [FakeWorksheet(title, [headers]) for title, headers in SHEETS.items()]
        )
        stats = book.worksheet("Статистика")
        stats.rows.append(["today", "total", "7", "2026-08-23T00:00:00+00:00"])
        repo = FakeRepo(
            [
                {
                    "id": 1,
                    "entity_type": "statistic",
                    "operation": "upsert",
                    "attempts": 0,
                    "payload": (
                        '{"period":"today","metric":"total","value":7,'
                        '"updated_at_utc":"2026-08-23T00:00:00+00:00"}'
                    ),
                }
            ]
        )
        sheets = BusinessSheets("sheet", repo)
        sheets.book = book
        sheets._initialized = True
        sheets.sync_once(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(stats.read_calls, 1)
        self.assertEqual(stats.write_calls, 0)
        self.assertEqual(repo.done, [1])

    def test_cross_process_sync_lease_serializes_remote_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            repo = BusinessRepository(Path(tmp) / "business.db")
            repo.queue_statistics(
                [{"period": "today", "metric": "total", "value": 7}], now,
            )
            owner = repo.acquire_sheets_sync_lease(now, lease_seconds=600)
            self.assertTrue(owner)

            book = FakeBook(
                [FakeWorksheet(title, [headers]) for title, headers in SHEETS.items()]
            )
            sheets = BusinessSheets("sheet", BusinessRepository(repo.path))
            sheets.book = book
            sheets._initialized = True
            sheets._next_refresh_at = now + timedelta(seconds=300)
            sheets.sync_once(now)

            stats = book.worksheet("Статистика")
            self.assertEqual(stats.read_calls, 0)
            self.assertEqual(stats.write_calls, 0)
            with connect(repo.path) as db:
                pending = db.execute(
                    "SELECT status FROM sheets_outbox WHERE entity_type='statistic'"
                ).fetchone()
            self.assertEqual(pending["status"], "pending")

            self.assertTrue(repo.release_sheets_sync_lease(owner))
            sheets.sync_once(now + timedelta(seconds=1))
            self.assertEqual(stats.read_calls, 1)
            self.assertEqual(stats.write_calls, 1)
            with connect(repo.path) as db:
                synced = db.execute(
                    "SELECT status FROM sheets_outbox WHERE entity_type='statistic'"
                ).fetchone()
            self.assertEqual(synced["status"], "synced")

    def test_default_intents_keep_payment_specific_and_seed_handoffs(self):
        intents = {row[0]: row for row in INTENT_SEED}
        self.assertEqual(
            intents["payment"][4],
            "оплата картой;перевод на карту;реквизит",
        )
        self.assertNotIn("оплата;", intents["payment"][4] + ";")
        self.assertEqual(intents["active_order"][7], "human_handoff")
        self.assertIn("где заказ", intents["active_order"][4])
        self.assertIn("buyurtma qayerda", intents["active_order"][5])
        self.assertIn("буюртмани бекор қил", intents["active_order"][5])
        self.assertEqual(intents["technical"][7], "human_handoff")
        for keyword in (
            "характеристик",
            "камера",
            "экран",
            "герц",
            "поддерживает",
            "совместим",
            "что лучше",
        ):
            self.assertIn(keyword, intents["technical"][4])

    def test_sync_error_is_redacted(self):
        repo = FakeRepo(
            [{"id": 9, "entity_type": "unknown", "operation": "upsert", "attempts": 2, "payload": '{"secret":"token"}'}]
        )
        sheets = BusinessSheets("sheet", repo)
        sheets.book = FakeBook([FakeWorksheet(title, [headers]) for title, headers in SHEETS.items()])
        sheets.sync_once(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(repo.retried[0][:2], (9, 3))
        self.assertNotIn("token", repo.retried[0][2])

    def test_initialization_failure_does_not_claim_outbox_rows(self):
        repo = FakeRepo(
            [
                {"id": 10, "entity_type": "message", "operation": "upsert", "attempts": 1, "payload": "{}"},
                {"id": 11, "entity_type": "dialog", "operation": "upsert", "attempts": 2, "payload": "{}"},
            ]
        )
        sheets = BusinessSheets("sheet", repo)
        with patch.object(
            sheets,
            "initialize",
            side_effect=RuntimeError("credentials TOP_SECRET"),
        ):
            sheets.sync_once(datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(repo.claims, [])
        self.assertEqual([row["id"] for row in repo.rows], [10, 11])
        self.assertEqual(repo.retried, [])

    def test_partially_failed_bootstrap_is_retried_even_with_open_book(self):
        class FlakyBook(FakeBook):
            def __init__(self, sheets):
                super().__init__(sheets)
                self.fail_once = True

            def batch_update(self, request):
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("temporary metadata failure")
                return super().batch_update(request)

        book = FlakyBook([FakeWorksheet("Лист1")])
        repo = FakeRepo(
            [{"id": 20, "entity_type": "message", "operation": "upsert", "attempts": 0,
              "payload": '{"event_id":"m20","chat_id":"42"}'}]
        )
        sheets = BusinessSheets("sheet", repo, cache_seconds=1)
        with patch("telegram_business.sheets.google_client", return_value=FakeClient(book)):
            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            sheets.sync_once(now)
            self.assertIsNotNone(sheets.book)
            self.assertFalse(sheets._initialized)
            self.assertEqual(repo.claims, [])
            sheets.sync_once(now + timedelta(seconds=2))
        self.assertTrue(sheets._initialized)
        self.assertEqual(repo.done, [20])


class TelegramAPISecurityTests(unittest.TestCase):
    class Response:
        def __init__(self, status, body, headers=None):
            self.status_code = status
            self.body = body
            self.headers = headers or {}

        def json(self):
            return self.body

    class HTTP:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    def test_callback_data_is_opaque_and_within_telegram_limit(self):
        token = "AbcdefghijklmnopQRSTUV"
        value = make_callback_data(token)
        self.assertEqual(value, f"nr1:{token}")
        self.assertEqual(parse_callback_data(value), token)
        self.assertEqual(len(make_callback_data("A" * 60).encode("utf-8")), 64)
        for invalid in (
            "short",
            "A" * 61,
            "+9989012345678901",
            "token:with:action",
            "токен_с_пиими_данными",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    make_callback_data(invalid)
        self.assertIsNone(parse_callback_data("order:123"))
        self.assertIsNone(parse_callback_data("nr1:" + "A" * 61))

    def test_inline_keyboard_rejects_reply_keyboards_and_sensitive_actions(self):
        token = make_callback_data("AbcdefghijklmnopQRSTUV")
        valid = {
            "inline_keyboard": [
                [{"text": "Продолжить", "callback_data": token}],
                [{"text": "Каталог", "url": "https://texnikach.uz/go"}],
            ]
        }
        self.assertEqual(normalize_inline_keyboard(valid), valid)
        self.assertEqual(
            normalize_inline_keyboard({"inline_keyboard": []}),
            {"inline_keyboard": []},
        )
        invalid_markups = (
            {"keyboard": [[{"text": "Телефон", "request_contact": True}]]},
            {"remove_keyboard": True},
            {"force_reply": True},
            {"inline_keyboard": [[{"text": "Телефон", "request_contact": True}]]},
            {"inline_keyboard": [[{"text": "Локация", "request_location": True}]]},
            {"inline_keyboard": [[{"text": "PII", "callback_data": "nr1:+9989012345678901"}]]},
            {"inline_keyboard": [[{"text": "Unsafe", "url": "http://example.test"}]]},
        )
        for markup in invalid_markups:
            with self.subTest(markup=markup):
                with self.assertRaises(ValueError):
                    normalize_inline_keyboard(markup)

    def test_business_callback_api_uses_inline_markup_and_connection_id(self):
        callback_data = make_callback_data("AbcdefghijklmnopQRSTUV")
        markup = {
            "inline_keyboard": [[
                {"text": "Сохранить", "callback_data": callback_data}
            ]]
        }
        http = self.HTTP(
            [
                self.Response(200, {"ok": True, "result": {"message_id": 10}}),
                self.Response(200, {"ok": True, "result": {"message_id": 10}}),
                self.Response(200, {"ok": True, "result": {"message_id": 10}}),
                self.Response(200, {"ok": True, "result": True}),
            ]
        )
        api = TelegramBusinessAPI("123456:secret", http=http)
        api.send_message("connection", "42", "draft", reply_markup=markup)
        api.edit_message_text(
            "connection", "42", 10, "review", reply_markup=markup,
        )
        api.edit_message_reply_markup(
            "connection", "42", 10, {"inline_keyboard": []},
        )
        api.answer_callback_query("callback-1", text="Сохранено")

        methods = [call[0].rsplit("/", 1)[-1] for call in http.calls]
        self.assertEqual(
            methods,
            [
                "sendMessage",
                "editMessageText",
                "editMessageReplyMarkup",
                "answerCallbackQuery",
            ],
        )
        for _, request in http.calls[:3]:
            self.assertEqual(
                request["json"]["business_connection_id"], "connection"
            )
        self.assertNotIn(
            "business_connection_id", http.calls[3][1]["json"]
        )
        self.assertEqual(
            http.calls[0][1]["json"]["reply_markup"], markup
        )

    def test_reply_keyboard_is_rejected_before_network_call(self):
        http = self.HTTP([])
        api = TelegramBusinessAPI("123456:secret", http=http)
        with self.assertRaises(ValueError):
            api.send_message(
                "connection",
                "42",
                "phone",
                reply_markup={
                    "keyboard": [[{"text": "Phone", "request_contact": True}]]
                },
            )
        self.assertEqual(http.calls, [])

    def test_edit_not_modified_is_idempotent_success(self):
        http = self.HTTP(
            [
                self.Response(
                    400,
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message is not modified",
                    },
                )
            ]
        )
        result = TelegramBusinessAPI(
            "123456:secret", http=http
        ).edit_message_text("connection", "42", 10, "same")
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(result["result"]["message_id"], 10)

    def test_rate_limit_is_typed_and_token_never_appears(self):
        token = "123456:TOP_SECRET"
        http = self.HTTP(
            [self.Response(429, {"ok": False, "error_code": 429, "description": f"https://api.telegram.org/bot{token}/x", "parameters": {"retry_after": 17}})]
        )
        with self.assertRaises(TelegramAPIError) as raised:
            TelegramBusinessAPI(token, http=http).send_message("c", "42", "text")
        error = raised.exception
        self.assertTrue(error.retryable)
        self.assertEqual(error.status, 429)
        self.assertEqual(error.retry_after, 17)
        self.assertNotIn(token, str(error))
        self.assertNotIn(token, repr(error))

    def test_http_errors_network_errors_and_safe_probe_methods(self):
        token = "123456:TOP_SECRET"
        http = self.HTTP(
            [
                self.Response(500, {"ok": False, "description": f"/bot{token}/ failed"}),
                requests.ConnectionError(f"https://api.telegram.org/bot{token}/getMe"),
                self.Response(200, {"ok": True, "result": {"id": 123456}}),
                self.Response(200, {"ok": True, "result": {"id": "connection"}}),
            ]
        )
        api = TelegramBusinessAPI(token, http=http)
        with self.assertRaises(TelegramAPIError) as server:
            api.send_message("c", "42", "text")
        self.assertTrue(server.exception.retryable)
        self.assertTrue(server.exception.ambiguous)
        self.assertNotIn(token, str(server.exception))
        with self.assertRaises(TelegramAPIError) as network:
            api.get_me()
        self.assertTrue(network.exception.retryable)
        self.assertNotIn(token, str(network.exception))
        self.assertEqual(api.get_me()["id"], 123456)
        self.assertEqual(api.get_business_connection("connection")["id"], "connection")
        self.assertTrue(http.calls[-1][0].endswith("/getBusinessConnection"))
        self.assertEqual(http.calls[-1][1]["json"]["business_connection_id"], "connection")

    def test_empty_successful_send_is_an_ambiguous_transport_outcome(self):
        http = self.HTTP([self.Response(200, {"ok": True, "result": {}})])
        with self.assertRaises(TelegramAPIError) as raised:
            TelegramBusinessAPI("123456:secret", http=http).send_message(
                "connection", "42", "text"
            )
        self.assertEqual(raised.exception.status, 200)
        self.assertTrue(raised.exception.retryable)
        self.assertTrue(raised.exception.ambiguous)

    def test_non_mutating_http_500_remains_safely_retryable(self):
        http = self.HTTP(
            [self.Response(500, {"ok": False, "description": "temporary"})]
        )
        with self.assertRaises(TelegramAPIError) as raised:
            TelegramBusinessAPI("123456:secret", http=http).get_me()
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(raised.exception.ambiguous)


class BusinessConfigTests(unittest.TestCase):
    def test_invalid_unused_values_do_not_break_disabled_parent_app(self):
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BUSINESS_ENABLED": "false",
                "BUSINESS_NIGHT_START": "broken",
                "BUSINESS_DEBOUNCE_SECONDS": "broken",
                "BUSINESS_WORKDAYS": "broken",
            },
            clear=True,
        ):
            settings = BusinessSettings.load()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.night_start, time(20))
        self.assertEqual(settings.debounce_seconds, 3)
        self.assertEqual(settings.workdays, tuple(range(7)))

    def test_load_derives_bot_id_and_parses_workdays(self):
        env = {
            "TELEGRAM_BUSINESS_ENABLED": "true",
            "TELEGRAM_BUSINESS_BOT_TOKEN": "123456:secret",
            "TELEGRAM_BUSINESS_WEBHOOK_SECRET": "safe_secret-1",
            "TELEGRAM_BUSINESS_ALLOWED_CONNECTION_ID": "connection",
            "BUSINESS_WORKDAYS": "1,3,7",
            "PRODUCT_URLS_PATH": "/tmp/Bot_URLS.xlsx",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = BusinessSettings.load()
        self.assertEqual(settings.bot_id, "123456")
        self.assertEqual(settings.workdays, (0, 2, 6))
        self.assertEqual(str(settings.product_urls_path), "/tmp/Bot_URLS.xlsx")
        settings.validate_enabled()

    def test_invalid_enabled_configuration_fails_without_secret_values(self):
        env = {
            "TELEGRAM_BUSINESS_ENABLED": "true",
            "TELEGRAM_BUSINESS_BOT_TOKEN": "not-a-normal-token",
            "TELEGRAM_BUSINESS_WEBHOOK_SECRET": "bad secret",
            "TELEGRAM_BUSINESS_ALLOWED_CONNECTION_ID": "connection",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = BusinessSettings.load()
        with self.assertRaises(RuntimeError) as raised:
            settings.validate_enabled()
        self.assertNotIn("not-a-normal-token", str(raised.exception))
        self.assertNotIn("bad secret", str(raised.exception))

    def test_enabled_schedule_must_cross_midnight(self):
        settings = BusinessSettings(
            True, "123:token", "safe_secret", "connection", "", "Asia/Tashkent",
            time(9), time(20), time(10), time(20), 300, 3, 120, 720,
            4, 8, Path("business.db"), "sheet", 60, 300,
            "existing_google_bot_prices", "", 1440,
        )
        with self.assertRaisesRegex(RuntimeError, "cross midnight"):
            settings.validate_enabled()


class RuntimeSheets:
    def __init__(self, *, settings=None, intents=(), templates=None):
        self.setting_values = dict(settings or {})
        self.intent_values = tuple(intents)
        self.templates = dict(templates or {})

    def settings(self, now=None):
        return self.setting_values

    def intents(self, now=None):
        return self.intent_values

    def render(self, code, language, values=None, *, now=None, **kwargs):
        canonical = normalize_template_code(code)
        if canonical in self.templates:
            configured = self.templates[canonical]
            if configured is None:
                return None
            selected = configured[language]
            substitutions = dict(values or {})
            substitutions.update(kwargs)
            return selected.format(**substitutions)
        return builtin_render(canonical, language, **dict(values or {}), **kwargs)


class RuntimeAPI:
    def __init__(self):
        self.sent = []

    def send_message(self, connection_id, chat_id, text, **kwargs):
        self.sent.append(text)
        return {"ok": True, "result": {"message_id": len(self.sent)}}


class RuntimeProducts:
    def search(self, query, **kwargs):
        return ProductMatch("not_found")


def _runtime_settings(path):
    return BusinessSettings(
        False, "123:token", "secret", "connection", "", "Asia/Tashkent",
        time(20), time(9, 30), time(10), time(20), 300, 3, 120, 720,
        4, 8, Path(path), "sheet", 60, 300,
        "existing_google_bot_prices", "", 1440,
    )


class BusinessRuntimeSheetTests(unittest.TestCase):
    def test_invalid_sheet_night_interval_uses_safe_environment_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = BusinessService(
                _runtime_settings(Path(tmp) / "business.db"),
                api=RuntimeAPI(),
                products=RuntimeProducts(),
            )
            service.sheets = RuntimeSheets(
                settings={"night_start": "09:00", "night_end": "20:00"},
            )
            policy = service._runtime_policy(
                datetime(2026, 8, 23, 12, tzinfo=ZoneInfo("Asia/Tashkent"))
            )
            self.assertEqual(policy.night_start, time(20))
            self.assertEqual(policy.night_end, time(9, 30))

    def test_runtime_settings_intent_template_cooldown_lock_and_limits_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 8, 23, 19, 0, tzinfo=ZoneInfo("Asia/Tashkent"))
            api = RuntimeAPI()
            service = BusinessService(
                _runtime_settings(Path(tmp) / "business.db"),
                clock=lambda: now,
                api=api,
                products=RuntimeProducts(),
            )
            custom_credit = IntentOverride(
                "credit", True, "all", 100,
                ("sheetcredit",), (), (), "credit", False,
            )
            service.sheets = RuntimeSheets(
                settings={
                    "night_start": "18:00",
                    "night_end": "08:45",
                    "manager_start": "11:15",
                    "manager_end": "19:30",
                    "workdays": ("1",),
                    "debounce_seconds": 7,
                    "final_idle_seconds": 42,
                    "manager_lock_minutes": 33,
                    "credit_cooldown_minutes": 99,
                    "max_bot_messages_10m": 1,
                    "max_bot_messages_session": 2,
                },
                intents=(custom_credit,),
                templates={
                    "credit": {
                        "ru": "SHEET RU — {manager_time_phrase_ru}",
                        "uz": "SHEET UZ — {manager_time_phrase_uz}",
                    },
                },
            )
            service.repo.upsert_connection(
                {"id": "connection", "user": {"id": 100}, "rights": {"can_reply": True}},
                now,
            )
            update = {
                "update_id": 1,
                "business_message": {
                    "business_connection_id": "connection",
                    "message_id": 10,
                    "date": int(now.timestamp()),
                    "chat": {"id": 200, "type": "private"},
                    "from": {"id": 200, "language_code": "ru"},
                    "text": "sheetcredit",
                },
            }
            self.assertTrue(service.repo.save_update(update, now))
            service.process_update(update)
            self.assertFalse(service.repo.due_actions(now + timedelta(seconds=6)))
            action = service.repo.due_actions(now + timedelta(seconds=7))[0]
            self.assertTrue(service.repo.claim_action(action["action_id"]))
            service.execute(action)
            self.assertEqual(api.sent, ["SHEET RU — завтра после 11:15"])
            self.assertFalse(service.repo.credit_allowed("200", now + timedelta(minutes=98), 99))
            self.assertTrue(service.repo.credit_allowed("200", now + timedelta(minutes=99), 99))

            # A second message is blocked by the sheet-provided 10-minute limit.
            session_id = action["session_id"]
            self.assertFalse(service.send("connection", "200", session_id, "second", "test", now))
            self.assertEqual(len(api.sent), 1)

            manual = {
                "update_id": 2,
                "business_message": {
                    "business_connection_id": "connection",
                    "message_id": 11,
                    "date": int(now.timestamp()),
                    "chat": {"id": 200, "type": "private"},
                    "from": {"id": 100},
                    "text": "manager",
                },
            }
            self.assertTrue(service.repo.save_update(manual, now))
            service.process_update(manual)
            lock_until = datetime.fromisoformat(service.repo.client("200")["manager_lock_until"])
            self.assertEqual(lock_until, now + timedelta(minutes=33))

    def test_disabled_runtime_template_sends_nothing_for_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 8, 23, 21, 0, tzinfo=ZoneInfo("Asia/Tashkent"))
            api = RuntimeAPI()
            service = BusinessService(
                _runtime_settings(Path(tmp) / "business.db"),
                clock=lambda: now,
                api=api,
                products=RuntimeProducts(),
            )
            service.sheets = RuntimeSheets(
                intents=(
                    IntentOverride(
                        "media_only", True, "night", 75,
                        ("фото",), ("rasm",), (), "media_only", True,
                    ),
                ),
                templates={"media_only": None},
            )
            service.repo.upsert_connection(
                {"id": "connection", "user": {"id": 100}, "rights": {"can_reply": True}},
                now,
            )
            update = {
                "update_id": 10,
                "business_message": {
                    "business_connection_id": "connection",
                    "message_id": 20,
                    "date": int(now.timestamp()),
                    "chat": {"id": 201, "type": "private"},
                    "from": {"id": 201, "language_code": "ru"},
                    "photo": [{"file_id": "file", "width": 10, "height": 10}],
                },
            }
            self.assertTrue(service.repo.save_update(update, now))
            service.process_update(update)
            action = service.repo.due_actions(now + timedelta(seconds=3))[0]
            self.assertTrue(service.repo.claim_action(action["action_id"]))
            service.execute(action)
            self.assertEqual(api.sent, [])

    def test_manager_phrase_uses_configured_workdays_and_time(self):
        friday_night = datetime(2026, 8, 21, 21, tzinfo=ZoneInfo("Asia/Tashkent"))
        ru, uz = manager_phrases(
            friday_night,
            time(11, 15),
            frozenset({0}),
            time(18),
        )
        self.assertEqual(ru, "в понедельник после 11:15")
        self.assertEqual(uz, "dushanba soat 11:15 dan keyin")


if __name__ == "__main__":
    unittest.main()
