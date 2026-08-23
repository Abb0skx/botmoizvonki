import unittest

from app.bot.keyboards import (
    edit_input_keyboard,
    main_keyboard,
    orders_channel_keyboard,
    orders_page_keyboard,
    statistics_keyboard,
)
from app.models import Order
from app.utils.payments import PAYMENT_LABELS
from app.utils.sellers import SELLERS


class EditInputKeyboardTests(unittest.TestCase):
    def _labels(self, keyboard) -> list[str]:
        return [button.text for row in keyboard.keyboard for button in row]

    def test_generic_edit_keyboard_has_cancel(self):
        keyboard = edit_input_keyboard()

        self.assertEqual(self._labels(keyboard), ["❌ Отменить изменение"])
        self.assertTrue(keyboard.resize_keyboard)

    def test_seller_edit_keyboard_keeps_choices_and_cancel(self):
        labels = self._labels(edit_input_keyboard("seller"))

        self.assertEqual(labels[:-1], list(SELLERS))
        self.assertEqual(labels[-1], "❌ Отменить изменение")

    def test_payment_edit_keyboard_accepts_both_field_names(self):
        expected = [*PAYMENT_LABELS.values(), "❌ Отменить изменение"]

        self.assertEqual(self._labels(edit_input_keyboard("payment")), expected)
        self.assertEqual(self._labels(edit_input_keyboard("payment_status")), expected)


class OrdersPageKeyboardTests(unittest.TestCase):
    def test_middle_page_has_map_and_both_directions(self):
        keyboard = orders_page_keyboard(
            "active",
            page=1,
            total_pages=3,
            map_url="https://yandex.uz/maps/?pt=1,2",
        )

        self.assertEqual(keyboard.inline_keyboard[0][0].text, "🗺 Все локации на карте")
        self.assertEqual(keyboard.inline_keyboard[0][0].url, "https://yandex.uz/maps/?pt=1,2")
        self.assertEqual(
            [button.callback_data for button in keyboard.inline_keyboard[1]],
            ["orders_page:active:0", "orders_page:active:1", "orders_page:active:2"],
        )

    def test_first_and_last_pages_only_show_available_direction(self):
        first = orders_page_keyboard("all", page=0, total_pages=2)
        last = orders_page_keyboard("all", page=1, total_pages=2)

        self.assertEqual(
            [button.callback_data for button in first.inline_keyboard[0]],
            ["orders_page:all:0", "orders_page:all:1"],
        )
        self.assertEqual(
            [button.callback_data for button in last.inline_keyboard[0]],
            ["orders_page:all:0", "orders_page:all:1"],
        )
        self.assertEqual(first.inline_keyboard[0][0].text, "1/2")
        self.assertEqual(last.inline_keyboard[0][-1].text, "2/2")

    def test_single_page_still_has_page_indicator(self):
        keyboard = orders_page_keyboard("active", page=0, total_pages=1)

        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "1/1")
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            "orders_page:active:0",
        )

    def test_invalid_page_arguments_are_rejected(self):
        for arguments in (
            ("unknown", 0, 1),
            ("active", 0, 0),
            ("active", -1, 1),
            ("all", 2, 2),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                orders_page_keyboard(*arguments)


class OrdersChannelKeyboardTests(unittest.TestCase):
    def test_journal_card_has_today_delivery_log_button(self):
        order = Order(
            id=7,
            order_number=7,
            manager_id=11,
            manager_name="Abbos",
            client_phone="+998901333999",
            product="A57",
        )

        callbacks = [
            button.callback_data
            for row in orders_channel_keyboard(order).inline_keyboard
            for button in row
        ]

        self.assertIn("daily_log:today", callbacks)


class StatisticsKeyboardTests(unittest.TestCase):
    def test_main_menu_and_statistics_links_cover_days_and_couriers(self):
        menu_labels = [
            button.text
            for row in main_keyboard().keyboard
            for button in row
        ]
        self.assertIn("📊 Статистика", menu_labels)

        keyboard = statistics_keyboard("https://bot.texnikach.uz/delivery/stats/")
        urls = [
            button.url
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn(
            "https://bot.texnikach.uz/delivery/stats?day=today",
            urls,
        )
        self.assertIn(
            "https://bot.texnikach.uz/delivery/stats?day=yesterday",
            urls,
        )
        self.assertTrue(any("courier_id=1799690992" in url for url in urls))
        self.assertIn(
            "https://bot.texnikach.uz/delivery/monitor",
            urls,
        )


if __name__ == "__main__":
    unittest.main()
