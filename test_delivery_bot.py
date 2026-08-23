import tempfile
import unittest
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.bot.keyboards import (
    all_locations_keyboard, completed_keyboard, courier_cancelled_keyboard, courier_keyboard,
    delivery_pending_keyboard, location_channel_keyboard, manager_cancelled_keyboard,
    manager_sent_keyboard, review_keyboard,
)
from app.database import OrderRepository
from app.database.repository import MIGRATION_COLUMNS, SCHEMA
from app.handlers.orders import (
    DETAILS, PAYMENT, SECOND_LOCATION, _publish_location, courier_action,
    delivery_input, details, save_edit, second_location,
)
from app.utils.formatters import (
    all_locations_card, courier_card, location_post_text, manager_card, short_address,
    telegram_location_url, telegram_message_url, yandex_map_url,
    yandex_route_url,
)
from app.utils.geocoding import extract_address, resolve_map_url
from app.utils.parsers import normalize_phone, parse_amount, parse_location_url, parse_order_details
from app.utils.payments import PAID_AT_ASSEMBLY, normalize_payment
from app.utils.sellers import normalize_seller


class ParserTests(unittest.TestCase):
    def test_phone_normalization(self):
        for raw in ("901333999", "90 133 39 99", "+998901333999", "998901333999"):
            self.assertEqual(normalize_phone(raw), "+998901333999")

    def test_invalid_phone(self):
        for raw in ("", "123", "+79991234567", "9989013339990"):
            with self.assertRaises(ValueError):
                normalize_phone(raw)

    def test_amount_parsing(self):
        cases = {
            "100": (100, None),
            "100$": (100, None),
            "100$ 1920000": (100, 1920000),
            "100 1920000": (100, 1920000),
            "1 920 000 сум": (None, 1920000),
            "1920000": (None, 1920000),
            "9000": (9000, None),
            "9001": (None, 9001),
            "7️⃣\nA56\n375$": (375, None),
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_amount(raw), expected)

    def test_invalid_amount(self):
        for raw in ("", "нет суммы", "0", "-100"):
            with self.assertRaises(ValueError):
                parse_amount(raw)

    def test_combined_order_details(self):
        result = parse_order_details(
            "Телефон: 90 133 39 99\n"
            "Цена: 100$ 1 920 000\n"
            "Локация: https://yandex.uz/maps/?ll=69.240562%2C41.311081"
        )
        self.assertEqual(result["client_phone"], "+998901333999")
        self.assertEqual((result["amount_usd"], result["amount_uzs"]), (100, 1920000))
        self.assertEqual(
            result["location_url"],
            "https://yandex.uz/maps/?ll=69.240562%2C41.311081",
        )

    def test_order_details_can_arrive_separately(self):
        self.assertEqual(parse_order_details("901333999")["client_phone"], "+998901333999")
        self.assertEqual(parse_order_details("100$ 1920000")["amount_usd"], 100)
        self.assertEqual(
            parse_order_details("https://yandex.uz/maps/?ll=69.24%2C41.31")["location_url"],
            "https://yandex.uz/maps/?ll=69.24%2C41.31",
        )

    def test_large_uzs_amount_is_not_mistaken_for_phone(self):
        result = parse_order_details("100000000 сум")
        self.assertNotIn("client_phone", result)
        self.assertEqual(result["amount_uzs"], 100000000)

    def test_seller_normalization(self):
        self.assertEqual(normalize_seller("otabek"), "Otabek")
        with self.assertRaises(ValueError):
            normalize_seller("Другой")

    def test_payment_normalization(self):
        self.assertEqual(normalize_payment("✅ Оплачено при сборе товара"), PAID_AT_ASSEMBLY)
        with self.assertRaises(ValueError):
            normalize_payment("потом")

    def test_yandex_coordinates(self):
        lat, lon, _ = parse_location_url("https://yandex.uz/maps/?ll=69.240562%2C41.311081")
        self.assertEqual((lat, lon), (41.311081, 69.240562))

    def test_google_and_yandex_route_coordinates(self):
        self.assertEqual(
            parse_location_url("https://maps.google.com/?q=41.311081,69.240562")[:2],
            (41.311081, 69.240562),
        )
        self.assertEqual(
            parse_location_url("https://yandex.uz/maps/?rtext=~41.311081%2C69.240562&rtt=auto")[:2],
            (41.311081, 69.240562),
        )

    def test_short_map_link_is_preserved(self):
        lat, lon, url = parse_location_url("https://yandex.com/maps/-/short")
        self.assertEqual((lat, lon), (None, None))
        self.assertEqual(url, "https://yandex.com/maps/-/short")

    def test_extract_district_and_mahalla(self):
        result = extract_address({
            "display_name": "Дом 1, Махалля Бунёдкор, Чиланзарский район, Ташкент",
            "address": {"city_district": "Чиланзарский район", "neighbourhood": "Махалля Бунёдкор"},
        })
        self.assertEqual(result["district"], "Чиланзарский район")
        self.assertEqual(result["mahalla"], "Махалля Бунёдкор")

    def test_private_channel_message_link(self):
        self.assertEqual(
            telegram_message_url(-1004398605075, 125),
            "https://t.me/c/4398605075/125",
        )
        self.assertIsNone(telegram_message_url(-5125237049, 125))
        self.assertIsNone(telegram_message_url(-1004398605075, None))


class MapUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_parsed_yandex_url_without_network(self):
        result = await resolve_map_url("https://yandex.uz/maps/?ll=69.240562%2C41.311081")
        self.assertEqual(result[:2], (41.311081, 69.240562))

    async def test_reject_non_map_domain(self):
        with self.assertRaises(ValueError):
            await resolve_map_url("https://example.com/?q=41.3,69.2")


class HandlerFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context() -> SimpleNamespace:
        return SimpleNamespace(user_data={"draft": {"seller_name": "Ali", "product": "A7 Pro"}})

    @staticmethod
    def update(text: str) -> SimpleNamespace:
        message = SimpleNamespace(text=text, location=None, reply_text=AsyncMock())
        return SimpleNamespace(message=message)

    async def test_combined_details_complete_collection(self):
        update = self.update(
            "Телефон: 90 133 39 99\n"
            "Цена: 100$ 1920000\n"
            "Локация: https://yandex.uz/maps/?ll=69.24%2C41.31"
        )
        context = self.context()
        location = {
            "location_url": "https://yandex.uz/maps/?ll=69.24%2C41.31",
            "latitude": 41.31,
            "longitude": 69.24,
            "address_text": "Ташкент",
            "district": None,
            "mahalla": None,
        }
        with patch("app.handlers.orders.enrich_location", AsyncMock(return_value=location)):
            state = await details(update, context)
        self.assertEqual(state, PAYMENT)
        self.assertEqual(context.user_data["draft"]["client_phone"], "+998901333999")
        self.assertEqual(context.user_data["draft"]["amount_uzs"], 1920000)

    async def test_details_accumulate_in_any_order(self):
        context = self.context()
        price_update = self.update("100$ 1920000")
        self.assertEqual(await details(price_update, context), DETAILS)
        phone_update = self.update("901333999")
        self.assertEqual(await details(phone_update, context), DETAILS)

        map_update = self.update("https://yandex.uz/maps/?ll=69.24%2C41.31")
        location = {
            "location_url": "https://yandex.uz/maps/?ll=69.24%2C41.31",
            "latitude": 41.31,
            "longitude": 69.24,
            "address_text": None,
            "district": None,
            "mahalla": None,
        }
        with patch("app.handlers.orders.enrich_location", AsyncMock(return_value=location)):
            state = await details(map_update, context)
        self.assertEqual(state, PAYMENT)

    async def test_optional_second_location_is_saved(self):
        context = self.context()
        context.user_data["draft"].update({
            "client_phone": "+998901333999",
            "amount_usd": 100,
            "latitude": 41.31,
            "longitude": 69.24,
        })
        update = self.update("https://yandex.uz/maps/?ll=69.25%2C41.32")
        location = {
            "location_url": "https://yandex.uz/maps/?ll=69.25%2C41.32",
            "latitude": 41.32,
            "longitude": 69.25,
            "address_text": "Вторая точка, Ташкент",
            "district": "Чиланзарский район",
            "mahalla": "Бунёдкор",
        }
        with patch("app.handlers.orders._location_values", AsyncMock(return_value=location)):
            state = await second_location(update, context)
        self.assertEqual(state, PAYMENT)
        self.assertEqual(context.user_data["draft"]["second_latitude"], 41.32)
        self.assertEqual(context.user_data["draft"]["second_longitude"], 69.25)

    async def test_edit_amount_saves_and_confirms_new_price(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "client_phone": "+998901333999",
                    "product": "A7 Pro",
                    "amount_usd": 100,
                },
            )
            message = SimpleNamespace(
                text="120$ 1 536 000",
                location=None,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=1),
                message=message,
            )
            bot = SimpleNamespace(edit_message_text=AsyncMock())
            context = SimpleNamespace(
                user_data={
                    "edit": {
                        "order_id": order.id,
                        "field": "amount",
                        "message_id": 50,
                        "chat_id": 1,
                    }
                },
                application=SimpleNamespace(bot_data={"repo": repo}),
                bot=bot,
            )

            await save_edit(update, context)

            updated = repo.get(order.id)
            self.assertEqual((updated.amount_usd, updated.amount_uzs), (120, 1536000))
            self.assertNotIn("edit", context.user_data)
            self.assertIn("Новая цена сохранена", message.reply_text.await_args.args[0])

    async def test_delivery_completes_from_photo_with_price_caption(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Ali",
                    "payment_status": PAID_AT_ASSEMBLY,
                    "client_phone": "+998901333999",
                    "product": "A7 Pro",
                    "amount_usd": 100,
                },
            )
            repo.update(
                order.id,
                status="awaiting_photo",
                courier_id=2,
                courier_name="Courier",
                delivery_chat_id=-100,
                delivery_message_id=50,
            )
            message = SimpleNamespace(
                photo=[SimpleNamespace(file_id="photo-file-id")],
                text=None,
                caption="100$ 1 920 000",
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=-100),
                effective_user=SimpleNamespace(id=2),
                message=message,
            )
            bot = SimpleNamespace(edit_message_text=AsyncMock(), send_photo=AsyncMock())
            settings = SimpleNamespace(delivery_group_id=-100, courier_ids=frozenset({2}))
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"settings": settings, "repo": repo}),
                bot=bot,
            )

            await delivery_input(update, context)

            completed = repo.get(order.id)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.delivery_photo, "photo-file-id")
            self.assertIsNotNone(completed.delivered_at)
            self.assertEqual((completed.received_usd, completed.received_uzs), (100, 1920000))
            bot.send_photo.assert_awaited_once()
            self.assertIn("✅ Оплачено", bot.send_photo.await_args.kwargs["caption"])
            self.assertIn("100$", bot.send_photo.await_args.kwargs["caption"])
            message.reply_text.assert_awaited_once_with("✅ Доставка подтверждена.")

            collect_order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Olmas",
                    "client_phone": "+998901333999",
                    "product": "S25 Ultra",
                    "amount_usd": 150,
                },
            )
            repo.update(
                collect_order.id,
                status="awaiting_photo",
                courier_id=2,
                courier_name="Courier",
                delivery_chat_id=-100,
                delivery_message_id=51,
            )
            collect_message = SimpleNamespace(
                photo=[SimpleNamespace(file_id="second-photo")],
                text=None,
                caption="150$ 2 400 000",
                reply_text=AsyncMock(),
            )
            update.message = collect_message

            await delivery_input(update, context)

            completed_collect = repo.get(collect_order.id)
            self.assertEqual(completed_collect.status, "completed")
            self.assertEqual(
                (completed_collect.received_usd, completed_collect.received_uzs),
                (150, 2400000),
            )
            collect_message.reply_text.assert_awaited_once_with("✅ Доставка подтверждена.")
            self.assertEqual(bot.send_photo.await_count, 2)

    async def test_delivered_button_completes_immediately_without_prompt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "seller_name": "Abbos",
                    "client_phone": "+998998904713",
                    "product": "A56",
                    "amount_usd": 375,
                },
            )
            repo.update(order.id, status="pending")
            query = SimpleNamespace(
                data=f"complete:{order.id}",
                from_user=SimpleNamespace(id=2, full_name="Courier", username=None),
                message=SimpleNamespace(chat_id=-100),
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            update = SimpleNamespace(callback_query=query)
            bot = SimpleNamespace(send_message=AsyncMock())
            settings = SimpleNamespace(delivery_group_id=-100, courier_ids=frozenset({2}))
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"settings": settings, "repo": repo}),
                bot=bot,
            )

            await courier_action(update, context)

            completed = repo.get(order.id)
            self.assertEqual(completed.status, "completed")
            self.assertIsNone(completed.delivery_photo)
            query.answer.assert_awaited_once_with("Заказ доставлен")
            self.assertIn("📦 A56", query.edit_message_text.await_args.args[0])
            self.assertEqual(
                query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[-1][0].callback_data,
                f"undo_complete:{order.id}",
            )
            bot.send_message.assert_awaited_once()

    async def test_publish_location_saves_channel_message(self):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = OrderRepository(Path(tempdir) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "client_phone": "+998901333999",
                    "product": "A7 Pro",
                    "amount_usd": 100,
                    "latitude": 41.311081,
                    "longitude": 69.240562,
                },
            )
            repo.update(order.id, status="pending")
            bot = SimpleNamespace(
                send_location=AsyncMock(
                    return_value=SimpleNamespace(chat_id=-1004398605075, message_id=88)
                ),
                send_message=AsyncMock(
                    return_value=SimpleNamespace(chat_id=-1004398605075, message_id=188)
                ),
                delete_message=AsyncMock(),
            )
            settings = SimpleNamespace(location_channel_id=-1004398605075)
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"settings": settings}),
                bot=bot,
            )

            published = await _publish_location(context, repo, repo.get(order.id))

            bot.send_location.assert_awaited_once()
            self.assertEqual(published.location_chat_id, -1004398605075)
            self.assertEqual(published.location_message_id, 88)
            self.assertEqual(published.location_details_message_id, 188)
            self.assertEqual(
                telegram_location_url(published),
                "https://t.me/c/4398605075/88",
            )

            published = repo.update(
                published.id,
                second_latitude=41.32,
                second_longitude=69.25,
            )
            bot.send_location.return_value = SimpleNamespace(
                chat_id=-1004398605075,
                message_id=89,
            )
            bot.send_message.return_value = SimpleNamespace(
                chat_id=-1004398605075,
                message_id=189,
            )
            published = await _publish_location(context, repo, published, 2)
            self.assertEqual(
                telegram_location_url(published, 2),
                "https://t.me/c/4398605075/89",
            )
            self.assertEqual(published.second_location_details_message_id, 189)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        self.data = {"client_phone": "+998901333999", "product": "A7 Pro", "amount_usd": 100}

    def tearDown(self):
        self.tempdir.cleanup()

    def test_order_numbers_are_incremental(self):
        self.assertEqual(self.repo.create(manager_id=1, manager_name="A", data=self.data).order_number, 1)
        self.assertEqual(self.repo.create(manager_id=1, manager_name="A", data=self.data).order_number, 2)

    def test_existing_database_gets_delivery_migrations(self):
        legacy_path = Path(self.tempdir.name) / "legacy.db"
        migration_names = set(MIGRATION_COLUMNS)
        legacy_lines = [
            line for line in SCHEMA.splitlines()
            if not any(line.strip().startswith(f"{name} ") for name in migration_names)
        ]
        legacy_schema = "\n".join(legacy_lines).replace(",\n);", "\n);")
        with sqlite3.connect(legacy_path) as db:
            db.executescript(legacy_schema)
        migrated = OrderRepository(legacy_path)
        migrated.initialize()
        with migrated.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        self.assertTrue(set(MIGRATION_COLUMNS).issubset(columns))

    def test_seller_is_saved_and_editable(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "seller_name": "Olmas"},
        )
        self.assertEqual(order.seller_name, "Olmas")
        updated = self.repo.transition(order.id, {"draft"}, seller_name="Ali")
        self.assertEqual(updated.seller_name, "Ali")

    def test_payment_status_is_saved_and_editable(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "payment_status": PAID_AT_ASSEMBLY},
        )
        self.assertEqual(order.payment_status, PAID_AT_ASSEMBLY)
        updated = self.repo.transition(order.id, {"draft"}, payment_status="collect_on_delivery")
        self.assertEqual(updated.payment_status, "collect_on_delivery")

    def test_atomic_courier_claim(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.repo.update(order.id, status="pending")
        claimed = self.repo.transition(
            order.id, {"pending"}, status="on_way", courier_id=10, courier_name="Courier 1",
            guard_courier_id=10, require_unassigned_or_same=True,
        )
        rejected = self.repo.transition(
            order.id, {"pending"}, status="on_way", courier_id=11, courier_name="Courier 2",
            guard_courier_id=11, require_unassigned_or_same=True,
        )
        self.assertEqual(claimed.courier_id, 10)
        self.assertIsNone(rejected)

    def test_delivery_state_survives_repository_recreation(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.repo.update(order.id, status="awaiting_photo", courier_id=10, courier_name="Courier")
        reopened = OrderRepository(self.repo.path)
        self.assertEqual(reopened.get_active_delivery(10).id, order.id)

    def test_address_columns_are_saved(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "latitude": 41.311081,
                "longitude": 69.240562,
                "address_text": "Ташкент",
                "district": "Чиланзарский район",
                "mahalla": "Бунёдкор",
            },
        )
        self.assertEqual(order.district, "Чиланзарский район")
        self.assertEqual(order.mahalla, "Бунёдкор")

    def test_location_channel_message_is_saved_and_linked(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "latitude": 41.31, "longitude": 69.24},
        )
        order = self.repo.update(
            order.id,
            location_chat_id=-1004398605075,
            location_message_id=77,
        )
        self.assertEqual(
            telegram_location_url(order),
            "https://t.me/c/4398605075/77",
        )
        keyboard = manager_sent_keyboard(order)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "📍 Локация")

    def test_second_location_is_saved_and_linked(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "latitude": 41.31,
                "longitude": 69.24,
                "second_latitude": 41.32,
                "second_longitude": 69.25,
                "second_address_text": "Вторая точка, Ташкент",
            },
        )
        order = self.repo.update(
            order.id,
            location_chat_id=-1004398605075,
            location_message_id=77,
            second_location_chat_id=-1004398605075,
            second_location_message_id=78,
        )
        self.assertEqual(
            telegram_location_url(order, 2),
            "https://t.me/c/4398605075/78",
        )
        self.assertEqual(manager_card(order).count("📍"), 2)
        keyboard = courier_keyboard(order)
        button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("📍 Локация", button_texts)
        self.assertIn("📍 Доп. локация", button_texts)
        self.assertNotIn("🧭 Маршрут", button_texts)
        self.assertNotIn("🗺 Карта", button_texts)
        self.assertNotIn("🗺 Все активные заказы", button_texts)

        order = self.repo.update(order.id, second_location_message_id=79)
        refreshed_keyboard = courier_keyboard(order)
        self.assertEqual(
            refreshed_keyboard.inline_keyboard[0][1].url,
            "https://t.me/c/4398605075/79",
        )

    def test_location_channel_only_opens_client_telegram(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        keyboard = location_channel_keyboard(order)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text, "💬 Telegram клиента")
        self.assertEqual(buttons[0].url, "tg://resolve?phone=998901333999")
        self.assertIsNone(buttons[0].callback_data)

    def test_delivery_confirmation_and_compact_edit_buttons_have_back(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        pending_buttons = [
            button for row in delivery_pending_keyboard(order).inline_keyboard for button in row
        ]
        self.assertTrue(any(button.callback_data == f"undo_complete:{order.id}" for button in pending_buttons))
        completed_button = completed_keyboard(order).inline_keyboard[-1][0]
        self.assertEqual(completed_button.callback_data, f"undo_complete:{order.id}")
        compact = review_keyboard(order.id)
        self.assertEqual(compact.inline_keyboard[0][0].text, "✏️ Изменить")
        expanded = review_keyboard(order.id, expanded=True)
        self.assertTrue(any(button.text == "↩️ Назад" for row in expanded.inline_keyboard for button in row))

    def test_undo_on_way_returns_order_to_queue(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.repo.update(order.id, status="on_way", courier_id=10, courier_name="Courier", time_started="now")
        returned = self.repo.transition(
            order.id,
            {"on_way"},
            status="pending",
            courier_id=None,
            courier_name=None,
            time_started=None,
            guard_courier_id=10,
            require_unassigned_or_same=True,
        )
        self.assertEqual(returned.status, "pending")
        self.assertIsNone(returned.courier_id)
        self.assertIsNone(returned.time_started)

    def test_cancelled_order_can_be_restored(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        cancelled = self.repo.update(
            order.id,
            status="cancelled",
            courier_id=10,
            courier_name="Courier",
        )
        restored = self.repo.transition(
            cancelled.id,
            {"cancelled"},
            status="pending",
            courier_id=None,
            courier_name=None,
            guard_courier_id=10,
            require_unassigned_or_same=True,
        )
        self.assertEqual(restored.status, "pending")
        self.assertIsNone(restored.courier_id)

    def test_cancelled_keyboards_have_back_button(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        manager_button = manager_cancelled_keyboard(order.id).inline_keyboard[0][0]
        courier_button = courier_cancelled_keyboard(order).inline_keyboard[-1][0]
        self.assertEqual(manager_button.text, "↩️ Назад")
        self.assertEqual(manager_button.callback_data, f"manager_restore:{order.id}")
        self.assertEqual(courier_button.text, "↩️ Назад")
        self.assertEqual(courier_button.callback_data, f"undo_cancel:{order.id}")

    def test_active_locations_map_contains_models(self):
        first = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "latitude": 41.31, "longitude": 69.24, "mahalla": "Бунёдкор"},
        )
        second = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "product": "S25 Ultra", "latitude": 41.32, "longitude": 69.25},
        )
        self.repo.update(first.id, status="pending")
        self.repo.update(second.id, status="on_way", courier_id=10)
        text, map_url = all_locations_card(self.repo.list_active())
        self.assertIn("A7 Pro", text)
        self.assertIn("S25 Ultra", text)
        self.assertIn("Бунёдкор", text)
        self.assertTrue(map_url.startswith("https://yandex.uz/maps/?"))
        map_button = all_locations_keyboard(map_url).inline_keyboard[0][0]
        self.assertEqual(map_button.text, "🗺 Все локации на карте")
        self.assertEqual(map_button.url, map_url)
        self.assertIn("rtext", yandex_route_url(self.repo.get(first.id)))
        self.assertIn("pt", yandex_map_url(self.repo.get(first.id)))

    def test_active_locations_map_numbers_every_location_in_order(self):
        first = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "latitude": 41.31,
                "longitude": 69.24,
                "second_latitude": 41.32,
                "second_longitude": 69.25,
                "second_address_text": "Чиланзар",
            },
        )
        second = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "product": "S25 Ultra",
                "latitude": 41.33,
                "longitude": 69.26,
            },
        )
        self.repo.update(first.id, status="pending")
        self.repo.update(second.id, status="pending")

        text, map_url = all_locations_card(self.repo.list_active())

        self.assertIn("📌 <b>1</b> · №1 · основная", text)
        self.assertIn("📌 <b>2</b> · №1 · доп.", text)
        self.assertIn("📌 <b>3</b> · №2 · основная", text)
        self.assertEqual(map_url.count("pm2rdm"), 3)

    def test_active_locations_map_legend_matches_25_point_limit(self):
        for index in range(14):
            order = self.repo.create(
                manager_id=1,
                manager_name="A",
                data={
                    **self.data,
                    "product": f"Model {index + 1}",
                    "latitude": 41.30 + index / 1000,
                    "longitude": 69.20 + index / 1000,
                    "second_latitude": 41.40 + index / 1000,
                    "second_longitude": 69.30 + index / 1000,
                },
            )
            self.repo.update(order.id, status="pending")

        text, map_url = all_locations_card(self.repo.list_active())

        self.assertEqual(map_url.count("pm2rdm"), 25)
        self.assertIn("📌 <b>25</b> · №13 · основная", text)
        self.assertNotIn("· №14 ·", text)
        self.assertIn("показаны 25 из 28 точек", text)

    def test_manager_card_shows_status(self):
        order = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        self.assertIn("📝 Черновик", manager_card(order))
        completed = self.repo.update(order.id, status="completed")
        self.assertIn("✅ Доставлен", manager_card(completed))

    def test_location_post_text_contains_compact_order_details(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "seller_name": "Abbos",
                "delivery_time": "До 17:00",
                "comment": "Позвонить",
                "latitude": 41.31,
                "longitude": 69.24,
                "address_text": "Чиланзар, Ташкент",
            },
        )
        text = location_post_text(order)
        for expected in (
            "Заказ №1", "Abbos", "📝 Черновик", "A7 Pro", "100$",
            "+998 90 133 39 99", "До 17:00", "Позвонить", "Чиланзар",
        ):
            self.assertIn(expected, text)

    def test_cards_escape_employee_text(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="<Manager>",
            data={**self.data, "product": "A7 <Pro>", "seller_name": "<Seller>"},
        )
        self.assertIn("A7 &lt;Pro&gt;", manager_card(order))
        self.assertIn("&lt;Seller&gt;", courier_card(order))
        self.assertNotIn("Создал заказ", courier_card(order))
        self.assertNotIn("Получить при доставке", courier_card(order))

    def test_compact_card_hides_empty_time_and_comment_and_keeps_location_last(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={**self.data, "seller_name": "Ali", "address_text": "Чиланзар"},
        )
        card = manager_card(order)
        self.assertNotIn("🕒", card)
        self.assertNotIn("💬", card)
        self.assertGreater(card.index("📍"), card.index("📱"))
        self.assertLess(card.index("📦"), card.index("💰"))

    def test_all_orders_are_listed_for_managers(self):
        first = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        second = self.repo.create(manager_id=2, manager_name="B", data=self.data)
        self.repo.update(first.id, status="completed")
        self.repo.update(second.id, status="cancelled")
        self.assertEqual(
            [order.order_number for order in self.repo.list_all()],
            [second.order_number, first.order_number],
        )

    def test_active_orders_include_drafts_but_hide_closed_orders(self):
        draft = self.repo.create(manager_id=1, manager_name="A", data=self.data)
        pending = self.repo.create(manager_id=2, manager_name="B", data=self.data)
        completed = self.repo.create(manager_id=3, manager_name="C", data=self.data)
        self.repo.update(pending.id, status="pending")
        self.repo.update(completed.id, status="completed")
        self.assertEqual(
            [order.order_number for order in self.repo.list_open()],
            [pending.order_number, draft.order_number],
        )

    def test_address_is_compact(self):
        order = self.repo.create(
            manager_id=1,
            manager_name="A",
            data={
                **self.data,
                "address_text": "Дом 1, улица Мукими, Махалля Бунёдкор, Чиланзарский район, Ташкент, Узбекистан",
                "district": "Чиланзарский район",
                "mahalla": "Махалля Бунёдкор",
            },
        )
        value = short_address(order)
        self.assertIn("Махалля Бунёдкор", value)
        self.assertIn("Чиланзарский район", value)
        self.assertNotIn("Узбекистан", value)
        self.assertLessEqual(len(value.split(",")), 3)


if __name__ == "__main__":
    unittest.main()
