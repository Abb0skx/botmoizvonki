from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from price_server.config import PriceSettings
from price_server.sheets_registry import (
    BOT_SETTINGS_READ_RANGE,
    BotSettingsRegistry,
    BotSettingsRegistryError,
    BotSettingsSchemaError,
)


def settings() -> PriceSettings:
    return PriceSettings(
        enabled=True,
        db_path=Path("/tmp/price-server-bot-settings-test.db"),
        legacy_html_path=Path("/tmp/price-server-bot-settings-test.html"),
        admin_username="admin",
        admin_password="secret",
        sync_api_key="sync",
        telegram_bot_token="fake-token",
        telegram_channel_id="-1001234567890",
        telegram_channel_username="testchannel",
        product_sort_sheet_id="product-sort-sheet",
        posts_sheet_name="Telegram Posts",
        timezone="Asia/Tashkent",
        scheduler_poll_seconds=1,
        sync_max_bytes=2_000_000,
        bot_settings_sheet_id="bot-settings-sheet",
        bot_settings_sheet_name="bot_settings",
    )


def column_index(cell_range: str) -> int:
    letters = "".join(character for character in cell_range if character.isalpha())
    result = 0
    for character in letters:
        result = result * 26 + ord(character.upper()) - ord("A") + 1
    return result - 1


def row_index(cell_range: str) -> int:
    digits = "".join(character for character in cell_range if character.isdigit())
    return int(digits) - 1


class FakeWorksheet:
    def __init__(self, values, *, apply_updates=True):
        self.values = copy.deepcopy(values)
        self.apply_updates = apply_updates
        self.get_calls = []
        self.batch_calls = []

    def get(self, range_name, **kwargs):
        self.get_calls.append((range_name, kwargs))
        return copy.deepcopy(self.values)

    def batch_update(self, requests, *, value_input_option):
        self.batch_calls.append((copy.deepcopy(requests), value_input_option))
        if not self.apply_updates:
            return
        for request in requests:
            row = row_index(request["range"])
            column = column_index(request["range"])
            while len(self.values) <= row:
                self.values.append([])
            while len(self.values[row]) <= column:
                self.values[row].append("")
            self.values[row][column] = request["values"][0][0]


class FakeBook:
    def __init__(self, worksheet):
        self.target = worksheet
        self.worksheet_calls = []

    def worksheet(self, name):
        self.worksheet_calls.append(name)
        return self.target


class FakeClient:
    def __init__(self, worksheet):
        self.book = FakeBook(worksheet)
        self.open_calls = []

    def open_by_key(self, spreadsheet_id):
        self.open_calls.append(spreadsheet_id)
        return self.book


class BotSettingsRegistryTests(unittest.TestCase):
    def test_reordered_headers_update_only_mapped_cells_and_verify(self):
        worksheet = FakeWorksheet([
            ["updated_at", "setting", "value"],
            [46_000.0, "kurs", 11_930],
        ])
        client = FakeClient(worksheet)
        registry = BotSettingsRegistry(settings(), client=client)
        changed_at = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)

        result = registry.update_exchange_rate(11_900, changed_at)

        self.assertEqual(client.open_calls, ["bot-settings-sheet"])
        self.assertEqual(client.book.worksheet_calls, ["bot_settings"])
        self.assertEqual(len(worksheet.batch_calls), 1)
        requests, value_input_option = worksheet.batch_calls[0]
        self.assertEqual(value_input_option, "RAW")
        self.assertEqual(
            [request["range"] for request in requests],
            ["C2", "A2"],
        )
        self.assertEqual(requests[0]["values"], [[11_900]])
        self.assertIsInstance(requests[1]["values"][0][0], float)
        self.assertAlmostEqual(
            requests[1]["values"][0][0], 46_262.73259259259
        )
        self.assertEqual(worksheet.values[0], [
            "updated_at", "setting", "value",
        ])
        self.assertEqual(worksheet.values[1][1], "kurs")
        self.assertEqual(result, {
            "setting": "kurs",
            "value": 11_900,
            "updated_at": "2026-08-28T17:34:56+05:00",
            "row_number": 2,
        })
        self.assertEqual(len(worksheet.get_calls), 2)
        for range_name, options in worksheet.get_calls:
            self.assertEqual(range_name, BOT_SETTINGS_READ_RANGE)
            self.assertEqual(
                options["value_render_option"], "UNFORMATTED_VALUE"
            )
            self.assertEqual(
                options["date_time_render_option"], "SERIAL_NUMBER"
            )

    def test_requires_exact_unique_headers_and_one_exact_kurs_row(self):
        cases = {
            "empty": [],
            "wrong header": [
                ["setting", "amount", "updated_at"],
                ["kurs", 11_930, 46_000],
            ],
            "duplicate header": [
                ["setting", "value", "value"],
                ["kurs", 11_930, 46_000],
            ],
            "missing kurs": [
                ["setting", "value", "updated_at"],
                ["currency", 11_930, 46_000],
            ],
            "duplicate kurs": [
                ["setting", "value", "updated_at"],
                ["kurs", 11_930, 46_000],
                ["kurs", 11_920, 46_001],
            ],
            "non-exact kurs": [
                ["setting", "value", "updated_at"],
                [" KURS ", 11_930, 46_000],
            ],
        }
        changed_at = datetime(2026, 8, 28, tzinfo=timezone.utc)
        for label, values in cases.items():
            with self.subTest(label=label):
                worksheet = FakeWorksheet(values)
                registry = BotSettingsRegistry(
                    settings(), client=FakeClient(worksheet)
                )
                with self.assertRaises(BotSettingsRegistryError):
                    registry.update_exchange_rate(11_900, changed_at)
                self.assertEqual(worksheet.batch_calls, [])
        self.assertFalse(BotSettingsSchemaError.retryable)

    def test_requires_timezone_aware_updated_at_before_sheet_access(self):
        worksheet = FakeWorksheet([
            ["setting", "value", "updated_at"],
            ["kurs", 11_930, 46_000],
        ])
        client = FakeClient(worksheet)
        registry = BotSettingsRegistry(settings(), client=client)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            registry.update_exchange_rate(11_900, datetime(2026, 8, 28))

        self.assertEqual(client.open_calls, [])

    def test_failed_unformatted_verification_is_reported(self):
        worksheet = FakeWorksheet(
            [
                ["setting", "value", "updated_at"],
                ["kurs", 11_930, 46_000],
            ],
            apply_updates=False,
        )
        registry = BotSettingsRegistry(settings(), client=FakeClient(worksheet))

        with self.assertRaisesRegex(
            BotSettingsRegistryError, "exchange-rate verification failed"
        ):
            registry.update_exchange_rate(
                11_900,
                datetime(2026, 8, 28, tzinfo=timezone.utc),
            )

        self.assertEqual(len(worksheet.batch_calls), 1)

    def test_missing_tab_is_not_created(self):
        import gspread

        class MissingBook:
            def worksheet(self, _name):
                raise gspread.WorksheetNotFound("missing bot_settings")

        class MissingClient:
            @staticmethod
            def open_by_key(_spreadsheet_id):
                return MissingBook()

        registry = BotSettingsRegistry(settings(), client=MissingClient())
        with self.assertRaisesRegex(
            BotSettingsSchemaError, "worksheet does not exist"
        ):
            registry.update_exchange_rate(
                11_900,
                datetime(2026, 8, 28, tzinfo=timezone.utc),
            )

    def test_settings_load_separate_bot_settings_defaults_and_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            defaults = PriceSettings.load()
        self.assertEqual(
            defaults.bot_settings_sheet_id,
            "1TrS6C4oHe6nzQTPTa_4se_upXBFF6rmbfnE7RqznR8U",
        )
        self.assertEqual(defaults.bot_settings_sheet_name, "bot_settings")
        self.assertTrue(defaults.daily_post_refresh_enabled)

        with patch.dict(
            os.environ,
            {
                "PRICE_BOT_SETTINGS_SHEET_ID": "separate-sheet",
                "PRICE_BOT_SETTINGS_SHEET_NAME": "strict_settings",
                "PRICE_DAILY_POST_REFRESH_ENABLED": "false",
            },
            clear=True,
        ):
            overridden = PriceSettings.load()
        self.assertEqual(
            overridden.bot_settings_sheet_id, "separate-sheet"
        )
        self.assertEqual(
            overridden.bot_settings_sheet_name, "strict_settings"
        )
        self.assertFalse(overridden.daily_post_refresh_enabled)


if __name__ == "__main__":
    unittest.main()
