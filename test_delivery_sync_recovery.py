import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from telegram.error import BadRequest

from app.database import OrderRepository
from app.handlers.orders import (
    _process_cleanup_messages,
    _publish_location,
    _refresh_delivery_message,
    _reset_mismatched_publications,
    _set_location_marker,
    _sync_order,
    reconcile_orders_on_start,
)
from app.models import Order


def create_order(repo: OrderRepository):
    return repo.create(
        manager_id=1,
        manager_name="Manager",
        data={
            "seller_name": "Ali",
            "client_phone": "+998901333999",
            "product": "A56",
            "amount_usd": 375,
            "latitude": 41.31,
            "longitude": 69.24,
        },
    )


class DeletedMessageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_location_moves_buttons_to_pin_and_removes_reply_text(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-5125237049,
                delivery_message_id=55,
                location_chat_id=-1002,
                location_message_id=60,
                location_details_message_id=61,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertEqual(recovered.location_message_id, 60)
            self.assertIsNone(recovered.location_details_message_id)
            keyboard = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
            self.assertEqual(len(keyboard.inline_keyboard), 3)
            self.assertEqual(
                {row[0].callback_data for row in keyboard.inline_keyboard},
                {f"location_label:{order.id}"},
            )
            bot.delete_message.assert_awaited_once_with(chat_id=-1002, message_id=61)

    async def test_deleted_delivery_message_reference_is_cleared_for_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-1001,
                delivery_message_id=55,
            )
            repo.mark_synced(order.id, expected_updated_at=order.updated_at)
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                )
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertFalse(await _refresh_delivery_message(context, order))

            recovered = repo.get(order.id)
            self.assertIsNone(recovered.delivery_chat_id)
            self.assertIsNone(recovered.delivery_message_id)
            self.assertEqual(recovered.sync_needed, 1)

    async def test_deleted_completed_delivery_message_is_never_republished(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivery_chat_id=-5125237049,
                delivery_message_id=55,
                delivered_at="2026-08-23T10:00:00+05:00",
            )
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                send_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-5125237049,
                        location_channel_id=-1004398605075,
                    ),
                }),
            )

            recovered, success = await _sync_order(context, order.id)

            self.assertTrue(success)
            self.assertIsNone(recovered.delivery_chat_id)
            self.assertIsNone(recovered.delivery_message_id)
            self.assertEqual(recovered.sync_needed, 0)
            bot.send_message.assert_not_awaited()

    async def test_deleted_native_pin_clears_pin_and_details_references(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-1001,
                delivery_message_id=55,
                location_chat_id=-1002,
                location_message_id=60,
                location_details_message_id=61,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertFalse(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertIsNone(recovered.location_chat_id)
            self.assertIsNone(recovered.location_message_id)
            self.assertIsNone(recovered.location_details_message_id)
            self.assertEqual(
                bot.delete_message.await_args_list,
                [
                    call(chat_id=-1002, message_id=61),
                    call(chat_id=-1002, message_id=60),
                ],
            )
            self.assertEqual(repo.list_cleanup_messages(order_id=order.id), [])

    async def test_deleted_pin_cleanup_survives_a_transient_delete_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-1001,
                delivery_message_id=55,
                location_chat_id=-1002,
                location_message_id=60,
                location_details_message_id=61,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                delete_message=AsyncMock(side_effect=RuntimeError("timeout")),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertFalse(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertIsNone(recovered.location_message_id)
            queued = repo.list_cleanup_messages(order_id=order.id)
            self.assertEqual(
                {(item["chat_id"], item["message_id"]) for item in queued},
                {(-1002, 60), (-1002, 61)},
            )
            self.assertTrue(all(item["attempts"] == 1 for item in queued))

    async def test_cleanup_outbox_retries_and_removes_deleted_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            repo.transition(
                order.id,
                {"draft"},
                status="pending",
                cleanup_messages=[(-1002, 72)],
            )
            bot = SimpleNamespace(
                delete_message=AsyncMock(side_effect=[RuntimeError("timeout"), None]),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertFalse(await _process_cleanup_messages(context))
            self.assertEqual(repo.list_cleanup_messages()[0]["attempts"], 1)
            self.assertTrue(await _process_cleanup_messages(context))
            self.assertEqual(repo.list_cleanup_messages(), [])


class ConcurrentPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_losing_location_publication_is_removed_after_cas_conflict(self):
        order = Order(
            id=1,
            order_number=1,
            manager_id=1,
            manager_name="Manager",
            client_phone="+998901333999",
            product="A56",
            latitude=41.31,
            longitude=69.24,
            delivery_chat_id=-1001,
            delivery_message_id=55,
            updated_at="version-1",
        )
        repo = SimpleNamespace(
            update=Mock(return_value=None),
            enqueue_cleanup_messages=Mock(return_value=1),
            list_cleanup_messages=Mock(return_value=[
                {"id": 1, "chat_id": -1002, "message_id": 69},
                {"id": 2, "chat_id": -1002, "message_id": 70},
                {"id": 3, "chat_id": -1002, "message_id": 71},
            ]),
            mark_cleanup_done=Mock(return_value=True),
            mark_cleanup_failed=Mock(return_value=True),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[
                SimpleNamespace(chat_id=-1002, message_id=69),
                SimpleNamespace(chat_id=-1002, message_id=71),
            ]),
            send_location=AsyncMock(
                return_value=SimpleNamespace(chat_id=-1002, message_id=70)
            ),
            delete_message=AsyncMock(),
        )
        application = SimpleNamespace(
            bot_data={
                "settings": SimpleNamespace(location_channel_id=-1002),
                "repo": repo,
            }
        )
        context = SimpleNamespace(bot=bot, application=application)

        with self.assertRaisesRegex(RuntimeError, "changed"):
            await _publish_location(context, repo, order, 1)

        self.assertEqual(
            bot.delete_message.await_args_list,
            [
                call(chat_id=-1002, message_id=69),
                call(chat_id=-1002, message_id=70),
                call(chat_id=-1002, message_id=71),
            ],
        )
        repo.update.assert_called_once_with(
            1,
            expected_updated_at="version-1",
            location_chat_id=-1002,
            location_message_id=70,
            location_details_message_id=69,
            location_footer_message_id=71,
        )

    async def test_same_order_sync_calls_are_serialized(self):
        application = SimpleNamespace(bot_data={})
        context = SimpleNamespace(application=application)
        concurrent = 0
        maximum = 0

        async def fake_locked(_context, order_id):
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            return order_id, True

        with patch("app.handlers.orders._sync_order_locked", side_effect=fake_locked):
            await asyncio.gather(_sync_order(context, 1), _sync_order(context, 1))

        self.assertEqual(maximum, 1)


class StartupLegacyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_active_order_is_validated_including_legacy_pin(self):
        legacy = SimpleNamespace(id=4)
        repo = SimpleNamespace(
            list_needing_sync=Mock(return_value=[]),
            list_open=Mock(return_value=[legacy]),
            list_cleanup_messages=Mock(return_value=[]),
        )
        bot = SimpleNamespace(
            id=99,
            get_chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(type="supergroup"),
                    SimpleNamespace(type="channel"),
                ]
            ),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(
                    status="administrator",
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                )
            ),
        )
        application = SimpleNamespace(
            bot=bot,
            bot_data={
                "repo": repo,
                "settings": SimpleNamespace(
                    delivery_group_id=-1001,
                    location_channel_id=-1002,
                ),
            },
        )

        with (
            patch("app.handlers.orders._known_delivery_groups", return_value=frozenset({-1001})),
            patch("app.handlers.orders._sync_order", AsyncMock()) as sync,
        ):
            await reconcile_orders_on_start(application)

        sync.assert_awaited_once()
        self.assertEqual(sync.await_args.args[1], 4)

    async def test_changed_chat_ids_clear_old_references_and_queue_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-1001,
                delivery_message_id=10,
                location_chat_id=-1002,
                location_message_id=20,
                location_details_message_id=21,
            )

            reset = _reset_mismatched_publications(
                repo,
                SimpleNamespace(delivery_group_id=-2001, location_channel_id=-2002),
                order,
            )

            self.assertIsNone(reset.delivery_message_id)
            self.assertIsNone(reset.location_message_id)
            queued = repo.list_cleanup_messages(order_id=order.id)
            self.assertEqual(
                {(item["chat_id"], item["message_id"]) for item in queued},
                {(-1001, 10), (-1002, 20), (-1002, 21)},
            )


if __name__ == "__main__":
    unittest.main()
