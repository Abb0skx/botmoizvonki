from __future__ import annotations

import unittest
from datetime import date

from sales_photo_bot.dates import extract_sale_date, remove_sale_date


class SaleDateTests(unittest.TestCase):
    def test_normalizes_short_date_in_current_year(self):
        match = extract_sale_date("31/08", today=date(2026, 9, 1))

        self.assertIsNotNone(match)
        self.assertEqual(match.value, date(2026, 8, 31))

    def test_uses_previous_year_across_new_year(self):
        match = extract_sale_date("31/12", today=date(2026, 1, 1))

        self.assertIsNotNone(match)
        self.assertEqual(match.value, date(2025, 12, 31))

    def test_finds_the_latest_valid_leap_day(self):
        match = extract_sale_date("29/02", today=date(2026, 3, 1))

        self.assertIsNotNone(match)
        self.assertEqual(match.value, date(2024, 2, 29))

    def test_accepts_single_digits_and_existing_full_year(self):
        short = extract_sale_date("1/8 A16", today=date(2026, 9, 1))
        full = extract_sale_date("01/08/2025 A16", today=date(2026, 9, 1))

        self.assertEqual(short.value, date(2026, 8, 1))
        self.assertEqual(full.value, date(2025, 8, 1))

    def test_invalid_date_and_memory_are_not_dates(self):
        self.assertIsNone(
            extract_sale_date("31/02", today=date(2026, 9, 1))
        )
        self.assertIsNone(
            extract_sale_date("A16 8/256", today=date(2026, 9, 1))
        )

    def test_removes_only_the_date_and_compacts_the_caption(self):
        raw = "31/08\nA16 8/256\n901234567"
        match = extract_sale_date(raw, today=date(2026, 9, 1))

        self.assertEqual(
            remove_sale_date(raw, match),
            "A16 8/256 901234567",
        )


if __name__ == "__main__":
    unittest.main()
