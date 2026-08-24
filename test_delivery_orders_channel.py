import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.database import OrderRepository
from app.handlers.orders import (
    _sync_order,
    courier_assignment_action,
    reconcile_orders_on_start,
    validate_delivery_configuration,
)
from app.utils.couriers import courier_ids as known_courier_ids
from app.utils.formatters import orders_channel_card


ORDERS_CHANNEL_ID = -1004459657817


def order_data() -> dict:
    return {
        "seller_name": "Ali",
        "client_phone": "+998901333999",
        "client_phone_2": "+998998904713",
        "product": "A57 Pro",
        "amount_usd": 375,
        "delivery_time": "До 17:00",
        "comment": "Позвонить перед приездом",
        "latitude": 41.338586,
        "longitude": 69.272757,
        "address_text": "Рынок Малика, Ташкент",
        "district": "Шайхантахурский район",
        "mahalla": "Малика",
    }


class OrdersChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        self.settings = SimpleNamespace(
            manager_ids=frozenset({11}),
            courier_ids=frozenset({7636344727, 202134293, 1799690992}),
            delivery_group_id=-5125237049,
            location_channel_id=-1004398605075,
            orders_channel_id=ORDERS_CHANNEL_ID,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def context(self, bot):
        return SimpleNamespace(
            bot=bot,
            application=SimpleNamespace(
                bot_data={"settings": self.settings, "repo": self.repo},
            ),
        )

    async def test_draft_gets_full_orders_channel_card_without_courier_post(self):
        order = self.repo.create(manager_id=11, manager_name="Abbos", data=order_data())
        order = self.repo.update(
            order.id,
            manager_chat_id=11,
            manager_message_id=90,
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(
                chat_id=ORDERS_CHANNEL_ID,
                message_id=501,
            )),
            edit_message_text=AsyncMock(),
        )

        synchronized, success = await _sync_order(self.context(bot), order.id)

        self.assertTrue(success)
        self.assertEqual(synchronized.orders_channel_chat_id, ORDERS_CHANNEL_ID)
        self.assertEqual(synchronized.orders_channel_message_id, 501)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], ORDERS_CHANNEL_ID)
        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("Заказ №1", text)
        self.assertIn("A57 Pro", text)
        self.assertIn("+998 90 133 39 99", text)
        self.assertIn("Позвонить перед приездом", text)

    async def test_existing_orders_channel_card_is_edited_not_duplicated(self):
        order = self.repo.create(manager_id=11, manager_name="Abbos", data=order_data())
        order = self.repo.update(
            order.id,
            manager_chat_id=11,
            manager_message_id=90,
            orders_channel_chat_id=ORDERS_CHANNEL_ID,
            orders_channel_message_id=501,
        )
        order = self.repo.update(order.id, product="A58 Ultra")
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())

        synchronized, success = await _sync_order(self.context(bot), order.id)

        self.assertTrue(success)
        bot.send_message.assert_not_awaited()
        channel_edits = [
            call
            for call in bot.edit_message_text.await_args_list
            if call.kwargs.get("chat_id") == ORDERS_CHANNEL_ID
        ]
        self.assertEqual(len(channel_edits), 1)
        self.assertIn("A58 Ultra", channel_edits[0].kwargs["text"])
        self.assertEqual(synchronized.orders_channel_message_id, 501)

    async def test_completed_order_backfill_does_not_republish_to_delivery_group(self):
        order = self.repo.create(manager_id=11, manager_name="Abbos", data=order_data())
        order = self.repo.update(
            order.id,
            status="completed",
            delivered_at="2026-08-23T10:00:00+00:00",
            courier_name="Muzrob Oka",
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(
                chat_id=ORDERS_CHANNEL_ID,
                message_id=502,
            )),
            edit_message_text=AsyncMock(),
        )

        synchronized, success = await _sync_order(self.context(bot), order.id)

        self.assertTrue(success)
        self.assertEqual(synchronized.orders_channel_message_id, 502)
        self.assertIsNone(synchronized.delivery_message_id)
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], ORDERS_CHANNEL_ID)
        self.assertIn("✅✅✅✅✅✅✅", bot.send_message.await_args.kwargs["text"])

    async def test_startup_backfills_closed_orders_missing_from_journal(self):
        order = self.repo.create(manager_id=11, manager_name="Abbos", data=order_data())
        self.repo.update(order.id, status="completed")
        application = SimpleNamespace(
            bot=SimpleNamespace(),
            bot_data={"settings": self.settings, "repo": self.repo},
        )

        with (
            patch("app.handlers.orders.validate_delivery_configuration", new=AsyncMock()),
            patch("app.handlers.orders._sync_order", new=AsyncMock(return_value=(order, True))) as sync,
            patch("app.handlers.orders._process_cleanup_messages", new=AsyncMock()),
        ):
            await reconcile_orders_on_start(application)

        sync.assert_awaited_once()
        self.assertEqual(sync.await_args.args[1], order.id)

    async def test_manager_can_open_courier_menu_from_current_journal_card(self):
        order = self.repo.create(manager_id=11, manager_name="Abbos", data=order_data())
        order = self.repo.update(
            order.id,
            orders_channel_chat_id=ORDERS_CHANNEL_ID,
            orders_channel_message_id=501,
        )
        query = SimpleNamespace(
            data=f"control_courier_menu:{order.id}",
            from_user=SimpleNamespace(id=11),
            message=SimpleNamespace(chat_id=ORDERS_CHANNEL_ID, message_id=501),
            edit_message_reply_markup=AsyncMock(),
            answer=AsyncMock(),
        )

        await courier_assignment_action(
            SimpleNamespace(callback_query=query),
            self.context(SimpleNamespace()),
        )

        query.answer.assert_awaited_once_with("Выберите курьера")
        keyboard = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertIn(f"control_courier_assign:{order.id}:1799690992", callbacks)

    async def test_preflight_requires_orders_channel_admin_rights(self):
        async def member(chat_id, user_id):
            if chat_id == ORDERS_CHANNEL_ID and user_id == 99:
                return SimpleNamespace(status="member")
            if chat_id == self.settings.location_channel_id and user_id == 99:
                return SimpleNamespace(
                    status="administrator",
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                )
            return SimpleNamespace(status="member")

        bot = SimpleNamespace(
            id=99,
            get_chat=AsyncMock(side_effect=[
                SimpleNamespace(type="group"),
                SimpleNamespace(type="channel"),
                SimpleNamespace(type="channel"),
            ]),
            get_chat_member=AsyncMock(side_effect=member),
        )
        application = SimpleNamespace(
            bot=bot,
            bot_data={"settings": self.settings},
        )

        with patch(
            "app.handlers.orders._known_delivery_groups",
            return_value=frozenset({self.settings.delivery_group_id}),
        ):
            with self.assertRaisesRegex(RuntimeError, "administrator in the orders channel"):
                await validate_delivery_configuration(application)

    async def test_preflight_rejects_courier_who_left_own_group(self):
        courier_id = 202134293
        courier_group_id = -5216093690
        settings = SimpleNamespace(
            manager_ids=frozenset({11}),
            courier_ids=frozenset({courier_id}),
            delivery_group_id=courier_group_id,
            location_channel_id=-1004398605075,
            orders_channel_id=ORDERS_CHANNEL_ID,
        )

        async def member(chat_id, user_id):
            if (chat_id, user_id) == (courier_group_id, courier_id):
                return SimpleNamespace(status="left")
            return SimpleNamespace(status="member")

        bot = SimpleNamespace(
            id=99,
            get_chat=AsyncMock(return_value=SimpleNamespace(type="group")),
            get_chat_member=AsyncMock(side_effect=member),
        )
        application = SimpleNamespace(bot=bot, bot_data={"settings": settings})

        with patch(
            "app.handlers.orders._known_delivery_groups",
            return_value=frozenset({courier_group_id}),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"Courier Abbos must be a member of group -5216093690",
            ):
                await validate_delivery_configuration(application)

    async def test_preflight_warns_when_courier_left_location_channel(self):
        courier_id = 202134293
        courier_group_id = -5216093690
        location_channel_id = -1004398605075
        settings = SimpleNamespace(
            manager_ids=frozenset({11}),
            courier_ids=frozenset({courier_id}),
            delivery_group_id=courier_group_id,
            location_channel_id=location_channel_id,
            orders_channel_id=ORDERS_CHANNEL_ID,
        )

        async def chat(chat_id):
            return SimpleNamespace(
                type="group" if chat_id == courier_group_id else "channel",
            )

        async def member(chat_id, user_id):
            if (chat_id, user_id) == (location_channel_id, courier_id):
                return SimpleNamespace(status="left")
            if (chat_id, user_id) == (location_channel_id, 99):
                return SimpleNamespace(
                    status="administrator",
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                )
            if (chat_id, user_id) == (ORDERS_CHANNEL_ID, 99):
                return SimpleNamespace(
                    status="administrator",
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                )
            return SimpleNamespace(status="member")

        bot = SimpleNamespace(
            id=99,
            get_chat=AsyncMock(side_effect=chat),
            get_chat_member=AsyncMock(side_effect=member),
        )
        application = SimpleNamespace(bot=bot, bot_data={"settings": settings})

        with patch(
            "app.handlers.orders._known_delivery_groups",
            return_value=frozenset({courier_group_id}),
        ):
            with self.assertLogs("app.handlers.orders", level="WARNING") as logs:
                await validate_delivery_configuration(application)

        self.assertTrue(any("Courier Abbos is not a member" in line for line in logs.output))


class SettingsValidationTests(unittest.TestCase):
    def test_load_rejects_unknown_courier_id(self):
        unknown_id = 999999999
        configured_couriers = known_courier_ids() | frozenset({unknown_id})
        environment = {
            "DELIVERY_BOT_TOKEN": "test-token",
            "DELIVERY_GROUP_ID": "-5216093690",
            "DELIVERY_LOCATION_CHANNEL_ID": "-1004398605075",
            "DELIVERY_ORDERS_CHANNEL_ID": str(ORDERS_CHANNEL_ID),
            "DELIVERY_MANAGER_IDS": "11",
            "DELIVERY_COURIER_IDS": ",".join(
                str(value) for value in sorted(configured_couriers)
            ),
        }

        with (
            patch.dict(os.environ, environment, clear=True),
            patch("app.config.load_dotenv"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"couriers without a configured name/group: 999999999",
            ):
                Settings.load()

    def test_load_rejects_missing_configured_courier(self):
        environment = {
            "DELIVERY_BOT_TOKEN": "test-token",
            "DELIVERY_GROUP_ID": "-5125237049",
            "DELIVERY_LOCATION_CHANNEL_ID": "-1004398605075",
            "DELIVERY_ORDERS_CHANNEL_ID": "-1004459657817",
            "DELIVERY_MANAGER_IDS": "11",
            "DELIVERY_COURIER_IDS": "202134293,1799690992",
        }
        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(
                RuntimeError,
                r"missing configured couriers: 7636344727",
            ):
                Settings.load()


class OrdersChannelFormatterTests(unittest.TestCase):
    def test_full_card_contains_management_picture(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = repo.create(manager_id=11, manager_name="Abbos", data=order_data())
            order = repo.update(
                order.id,
                status="on_way",
                assigned_courier_name="Muzrob Oka",
                courier_name="Muzrob Oka",
                time_started="2026-08-23T05:30:00+00:00",
            )

            text = orders_channel_card(order)

        for value in (
            "Курьер едет",
            "Продавец: <b>Ali</b>",
            "Создал: Abbos",
            "A57 Pro",
            "375$",
            "+998 90 133 39 99",
            "+998 99 890 47 13",
            "До 17:00",
            "Позвонить перед приездом",
            "Назначен курьер: <b>Muzrob Oka</b>",
            "Малика",
        ):
            self.assertIn(value, text)
