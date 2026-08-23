import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ConversationHandler

from app.database import OrderRepository
from app.handlers.orders import DETAILS, PAYMENT, _send_location_messages, details, payment, save_edit
from app.utils.formatters import all_locations_card, manager_card
from app.utils.parsers import parse_order_details


class MultiValueCreationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(text=None, location=None):
        return SimpleNamespace(text=text, location=location, reply_text=AsyncMock())

    async def test_one_message_saves_two_phones_and_two_map_links(self):
        message = self.message(
            "Телефон: 90 133 39 99 / 91 222 33 44\n"
            "Цена: 375$\n"
            "https://yandex.uz/maps/?ll=69.240000%2C41.310000\n"
            "https://yandex.uz/maps/?ll=69.250000%2C41.320000"
        )
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(user_data={"draft": {"seller_name": "Ali", "product": "A56"}})
        locations = [
            {
                "location_url": "https://yandex.uz/maps/?ll=69.240000%2C41.310000",
                "latitude": 41.31,
                "longitude": 69.24,
                "address_text": "Первая",
                "district": None,
                "mahalla": None,
            },
            {
                "location_url": "https://yandex.uz/maps/?ll=69.250000%2C41.320000",
                "latitude": 41.32,
                "longitude": 69.25,
                "address_text": "Вторая",
                "district": None,
                "mahalla": None,
            },
        ]
        with patch("app.handlers.orders.enrich_location", AsyncMock(side_effect=locations)):
            state = await details(update, context)

        draft = context.user_data["draft"]
        self.assertEqual(state, PAYMENT)
        self.assertEqual(draft["client_phone"], "+998901333999")
        self.assertEqual(draft["client_phone_2"], "+998912223344")
        self.assertEqual((draft["latitude"], draft["longitude"]), (41.31, 69.24))
        self.assertEqual((draft["second_latitude"], draft["second_longitude"]), (41.32, 69.25))
        self.assertEqual((draft["amount_usd"], draft["amount_uzs"]), (375, None))

    async def test_second_native_location_is_accepted_after_payment_prompt(self):
        context = SimpleNamespace(user_data={"draft": {
            "seller_name": "Ali",
            "product": "A56",
            "client_phone": "+998901333999",
            "amount_usd": 375,
            "latitude": 41.31,
            "longitude": 69.24,
        }})
        message = self.message(location=SimpleNamespace(latitude=41.32, longitude=69.25))
        update = SimpleNamespace(message=message)
        second = {
            "location_url": "https://yandex.uz/maps/?ll=69.250000%2C41.320000",
            "latitude": 41.32,
            "longitude": 69.25,
            "address_text": "Вторая",
            "district": None,
            "mahalla": None,
        }
        with patch("app.handlers.orders._location_values", AsyncMock(return_value=second)):
            state = await payment(update, context)

        self.assertEqual(state, PAYMENT)
        self.assertEqual(context.user_data["draft"]["second_latitude"], 41.32)
        self.assertIn("Сохранено", message.reply_text.await_args.args[0])

    async def test_map_link_outside_uzbekistan_is_rejected(self):
        message = self.message("https://maps.google.com/?q=40.7128,-74.0060")
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(
            user_data={"draft": {"seller_name": "Ali", "product": "A56"}}
        )
        outside = {
            "location_url": "https://maps.google.com/?q=40.7128,-74.0060",
            "latitude": 40.7128,
            "longitude": -74.0060,
            "address_text": "New York",
            "district": None,
            "mahalla": None,
        }

        with patch("app.handlers.orders.enrich_location", AsyncMock(return_value=outside)):
            state = await details(update, context)

        self.assertEqual(state, DETAILS)
        self.assertNotIn("latitude", context.user_data["draft"])
        self.assertIn("за пределами Узбекистана", message.reply_text.await_args.args[0])


class EditSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_menu_text_never_becomes_edit_value(self):
        message = SimpleNamespace(text="📚 Все заказы", location=None, reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=100),
        )
        context = SimpleNamespace(user_data={"edit": {
            "order_id": 1,
            "field": "product",
            "chat_id": 100,
        }})

        state = await save_edit(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("edit", context.user_data)
        message.reply_text.assert_not_awaited()


class LocationPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_pin_contains_compact_buttons_without_reply_text(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "client_phone_2": "+998912223344",
                    "product": "A56",
                    "amount_usd": 375,
                    "latitude": 41.31,
                    "longitude": 69.24,
                    "address_text": "Яшнабадский район",
                },
            )
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-5125237049,
                delivery_message_id=25,
            )
            bot = SimpleNamespace(
                send_location=AsyncMock(return_value=SimpleNamespace(
                    chat_id=-1004398605075,
                    message_id=10,
                )),
                send_message=AsyncMock(side_effect=[
                    SimpleNamespace(chat_id=-1004398605075, message_id=9),
                    SimpleNamespace(chat_id=-1004398605075, message_id=11),
                ]),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "settings": SimpleNamespace(location_channel_id=-1004398605075),
                }),
            )

            fields = await _send_location_messages(context, order, 1)

            self.assertEqual(fields["location_message_id"], 10)
            self.assertEqual(fields["location_details_message_id"], 9)
            self.assertEqual(fields["location_footer_message_id"], 11)
            self.assertEqual(bot.send_message.await_count, 2)
            self.assertTrue(all(
                call.kwargs["text"].count("📍") == 33
                for call in bot.send_message.await_args_list
            ))
            buttons = bot.send_location.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual(len(buttons), 3)
            self.assertTrue(buttons[0][0].text.startswith("📍 Яшнабадский район"))
            self.assertEqual(buttons[1][0].text, "📦 A56 · №1 · Ali")
            self.assertEqual(buttons[2][0].text, "📱 +998 90 133 39 99")
            self.assertEqual(
                {row[0].callback_data for row in buttons},
                {f"location_label:{order.id}"},
            )


class MapAndCardLimitsTests(unittest.TestCase):
    def test_two_phones_are_visible_and_map_text_fits_telegram(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            for index in range(25):
                order = repo.create(
                    manager_id=1,
                    manager_name="Manager",
                    data={
                        "seller_name": "Очень длинный продавец" * 4,
                        "client_phone": "+998901333999",
                        "client_phone_2": "+998912223344",
                        "product": "Модель <очень длинная> " * 10,
                        "amount_usd": 375,
                        "latitude": 41.30 + index / 1000,
                        "longitude": 69.20 + index / 1000,
                        "address_text": "Очень длинный адрес <район> " * 20,
                    },
                )
                repo.update(order.id, status="pending")

            orders = repo.list_active()
            text, map_url = all_locations_card(orders)
            self.assertLessEqual(len(text), 4096)
            self.assertEqual(map_url.count("pm2rdm"), text.count("📌 <b>"))
            card = manager_card(orders[0])
            for value in ("+998 90 133 39 99", "+998 91 222 33 44"):
                self.assertIn(value, card)


class ParserMultipleUrlTests(unittest.TestCase):
    def test_parser_returns_two_urls_and_does_not_treat_coordinates_as_price(self):
        parsed = parse_order_details(
            "100$\n"
            "https://yandex.uz/maps/?ll=69.24%2C41.31\n"
            "https://yandex.uz/maps/?ll=69.25%2C41.32"
        )
        self.assertEqual(len(parsed["location_urls"]), 2)
        self.assertEqual((parsed["amount_usd"], parsed["amount_uzs"]), (100, None))


if __name__ == "__main__":
    unittest.main()
