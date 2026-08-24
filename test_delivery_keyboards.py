import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.keyboards import (
    DELIVERY_TIME_QUICK_CHOICES,
    DELIVERY_TIME_SLOTS,
    delivery_time_keyboard,
    edit_input_keyboard,
    main_keyboard,
    orders_channel_keyboard,
    orders_page_keyboard,
    statistics_keyboard,
)
from app.models import Order
from app.handlers.orders import COMMENT, DELIVERY_TIME, delivery_time, payment
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

    def test_delivery_time_keyboard_has_presets_slots_skip_and_free_text_hint(self):
        keyboard = delivery_time_keyboard()
        labels = self._labels(keyboard)

        self.assertEqual(labels[:3], list(DELIVERY_TIME_QUICK_CHOICES))
        self.assertEqual(labels[3:-1], list(DELIVERY_TIME_SLOTS))
        self.assertEqual(labels[-1], "Пропустить")
        self.assertEqual(keyboard.input_field_placeholder, "Или напишите время текстом")
        self.assertTrue(keyboard.one_time_keyboard)

    def test_delivery_time_edit_keeps_presets_skip_and_cancel(self):
        keyboard = edit_input_keyboard("delivery_time")
        labels = self._labels(keyboard)

        self.assertEqual(labels[:3], list(DELIVERY_TIME_QUICK_CHOICES))
        self.assertEqual(labels[3:3 + len(DELIVERY_TIME_SLOTS)], list(DELIVERY_TIME_SLOTS))
        self.assertEqual(labels[-2:], ["Пропустить", "❌ Отменить изменение"])


class DeliveryTimeCreationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_payment_step_opens_delivery_time_presets(self):
        message = SimpleNamespace(
            text=next(iter(PAYMENT_LABELS.values())),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(user_data={"draft": {}})

        state = await payment(update, context)

        self.assertEqual(state, DELIVERY_TIME)
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertIn("Срочно 🚨🚨🚨", labels)
        self.assertIn("22:00", labels)
        self.assertIn("Пропустить", labels)

    async def test_time_step_accepts_both_preset_and_free_text(self):
        for value in ("2–3 часа", "После 18:15"):
            with self.subTest(value=value):
                message = SimpleNamespace(text=value, reply_text=AsyncMock())
                update = SimpleNamespace(message=message)
                context = SimpleNamespace(user_data={"draft": {}})

                state = await delivery_time(update, context)

                self.assertEqual(state, COMMENT)
                self.assertEqual(context.user_data["draft"]["delivery_time"], value)


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
    def test_journal_card_has_no_chronology_button(self):
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

        self.assertNotIn("daily_log:today", callbacks)


class StatisticsKeyboardTests(unittest.TestCase):
    def test_main_menu_hides_statistics_while_direct_links_remain_available(self):
        menu_labels = [
            button.text
            for row in main_keyboard().keyboard
            for button in row
        ]
        self.assertNotIn("📊 Статистика", menu_labels)

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
