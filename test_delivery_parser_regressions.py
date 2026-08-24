import unittest

from app.utils.parsers import parse_amount, parse_order_details


class DeliveryParserRegressionTests(unittest.TestCase):
    def test_model_digits_do_not_join_explicit_usd_price(self):
        self.assertEqual(parse_amount("A56 375$"), (375, None))
        parsed = parse_order_details("A56 375$")
        self.assertEqual((parsed["amount_usd"], parsed["amount_uzs"]), (375, None))

    def test_model_digits_do_not_become_an_unmarked_price(self):
        self.assertEqual(parse_amount("A56 375"), (375, None))

    def test_two_phones_are_returned_without_affecting_price(self):
        parsed = parse_order_details(
            "Телефон: 90 133 39 99 / +998 91 222 33 44\n"
            "Товар: A56\n"
            "Цена: 375$"
        )
        self.assertEqual(parsed["client_phone"], "+998901333999")
        self.assertEqual(
            parsed["client_phones"],
            ["+998901333999", "+998912223344"],
        )
        self.assertEqual((parsed["amount_usd"], parsed["amount_uzs"]), (375, None))

    def test_duplicate_phone_is_returned_once(self):
        parsed = parse_order_details("90 133 39 99, +998901333999, 100$")
        self.assertEqual(parsed["client_phones"], ["+998901333999"])
        self.assertEqual(parsed["amount_usd"], 100)

    def test_explicit_currency_suffixes_and_prefixes(self):
        cases = {
            "USD 375": (375, None),
            "375 долларов": (375, None),
            "UZS 1 920 000": (None, 1_920_000),
            "1 920 000 сум": (None, 1_920_000),
            "375$ 7 200 000 UZS": (375, 7_200_000),
            "100 1 920 000 сум": (100, 1_920_000),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_amount(value), expected)

    def test_unmarked_currency_threshold_is_preserved(self):
        self.assertEqual(parse_amount("9000"), (9000, None))
        self.assertEqual(parse_amount("9001"), (None, 9001))

    def test_full_phone_then_unmarked_nine_digit_uzs_is_not_a_second_phone(self):
        parsed = parse_order_details("+998901333999\n900000000")

        self.assertEqual(parsed["client_phones"], ["+998901333999"])
        self.assertEqual(parsed["amount_usd"], None)
        self.assertEqual(parsed["amount_uzs"], 900_000_000)

    def test_two_bare_local_phones_then_unmarked_uzs_keeps_all_fields(self):
        parsed = parse_order_details(
            "901333999\n"
            "912223344\n"
            "900000000"
        )

        self.assertEqual(
            parsed["client_phones"],
            ["+998901333999", "+998912223344"],
        )
        self.assertEqual(parsed["amount_uzs"], 900_000_000)

    def test_full_and_local_phone_before_unmarked_uzs_keeps_second_phone(self):
        parsed = parse_order_details(
            "+998901333999\n"
            "912223344\n"
            "900000000"
        )

        self.assertEqual(
            parsed["client_phones"],
            ["+998901333999", "+998912223344"],
        )
        self.assertEqual(parsed["amount_uzs"], 900_000_000)

    def test_labelled_local_phones_still_win_over_amount_heuristic(self):
        parsed = parse_order_details(
            "Телефон: 90 133 39 99\n"
            "Телефон: 91 222 33 44\n"
            "900000000"
        )

        self.assertEqual(
            parsed["client_phones"],
            ["+998901333999", "+998912223344"],
        )
        self.assertEqual(parsed["amount_uzs"], 900_000_000)

    def test_explicit_uzs_amount_is_protected_before_phone_extraction(self):
        parsed = parse_order_details("90 133 39 99\n900 000 000 сум")

        self.assertEqual(parsed["client_phones"], ["+998901333999"])
        self.assertEqual(parsed["amount_uzs"], 900_000_000)

    def test_amounts_outside_sqlite_integer_range_are_rejected(self):
        self.assertEqual(parse_amount("9223372036854775807 сум"), (None, 9223372036854775807))
        with self.assertRaisesRegex(ValueError, "слишком большая"):
            parse_amount("9223372036854775808 сум")


if __name__ == "__main__":
    unittest.main()
