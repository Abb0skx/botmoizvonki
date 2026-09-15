import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from telegram.error import BadRequest, ChatMigrated, RetryAfter

from app.database import OrderRepository
from app.handlers.orders import (
    _process_cleanup_messages,
    _publish_location,
    _refresh_delivery_message,
    _retry_order_sync,
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
    async def test_delivery_refresh_propagates_retry_after(self):
        order = Order(
            id=1,
            order_number=1,
            manager_id=1,
            manager_name="Manager",
            client_phone="+998901333999",
            product="A56",
            status="pending",
            delivery_chat_id=-1001,
            delivery_message_id=55,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(edit_message_text=AsyncMock(side_effect=RetryAfter(7))),
            application=SimpleNamespace(bot_data={}),
        )

        with self.assertRaises(RetryAfter):
            await _refresh_delivery_message(context, order)

    async def test_static_location_separators_are_not_reedited_during_sync(self):
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
                location_header_message_id=61,
                location_message_id=60,
                location_details_message_id=63,
                location_footer_message_id=62,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            bot.edit_message_reply_markup.assert_awaited_once()
            bot.edit_message_text.assert_awaited_once()
            self.assertEqual(
                bot.edit_message_text.await_args.kwargs["message_id"],
                63,
            )

    async def test_existing_three_message_location_is_upgraded_to_four_posts(self):
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
                send_message=AsyncMock(side_effect=[
                    SimpleNamespace(chat_id=-1002, message_id=70),
                    SimpleNamespace(chat_id=-1002, message_id=72),
                    SimpleNamespace(chat_id=-1002, message_id=73),
                ]),
                send_location=AsyncMock(return_value=SimpleNamespace(
                    chat_id=-1002,
                    message_id=71,
                )),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(location_channel_id=-1002),
                }),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertEqual(recovered.location_header_message_id, 70)
            self.assertEqual(recovered.location_message_id, 71)
            self.assertEqual(recovered.location_details_message_id, 72)
            self.assertEqual(recovered.location_footer_message_id, 73)
            self.assertEqual(
                bot.delete_message.await_args_list,
                [
                    call(chat_id=-1002, message_id=60),
                    call(chat_id=-1002, message_id=61),
                ],
            )

    async def test_closed_historical_three_post_location_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivered_at="2026-08-23T10:00:00+05:00",
                delivery_chat_id=-5125237049,
                delivery_message_id=55,
                location_chat_id=-1002,
                location_header_message_id=59,
                location_message_id=60,
                location_details_message_id=None,
                location_footer_message_id=61,
            )
            bot = SimpleNamespace(
                send_message=AsyncMock(),
                send_location=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            preserved = repo.get(order.id)
            self.assertEqual(preserved.location_header_message_id, 59)
            self.assertEqual(preserved.location_message_id, 60)
            self.assertIsNone(preserved.location_details_message_id)
            self.assertEqual(preserved.location_footer_message_id, 61)
            bot.send_message.assert_not_awaited()
            bot.send_location.assert_not_awaited()
            bot.edit_message_reply_markup.assert_not_awaited()
            bot.edit_message_text.assert_not_awaited()
            bot.delete_message.assert_not_awaited()

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

    async def test_deleted_completed_delivery_message_retires_linked_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivered_at="2026-08-23T10:00:00+05:00",
                delivery_chat_id=-1004404461980,
                delivery_message_id=55,
                post_delivery_prompt_required=1,
                post_delivery_prompt_chat_id=-1004404461980,
                post_delivery_prompt_message_id=56,
            )
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertTrue(await _refresh_delivery_message(context, order))

            recovered = repo.get(order.id)
            self.assertIsNone(recovered.delivery_chat_id)
            self.assertIsNone(recovered.delivery_message_id)
            self.assertEqual(recovered.post_delivery_prompt_required, 0)
            self.assertIsNone(recovered.post_delivery_prompt_chat_id)
            self.assertIsNone(recovered.post_delivery_prompt_message_id)
            bot.delete_message.assert_awaited_once_with(
                chat_id=-1004404461980,
                message_id=56,
            )

    async def test_missing_completed_main_never_creates_unattached_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivered_at="2026-08-23T10:00:00+05:00",
                delivery_chat_id=-1004404461980,
                delivery_message_id=55,
                post_delivery_prompt_required=1,
            )
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                send_message=AsyncMock(),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1004404461980,
                        location_channel_id=-1002,
                    ),
                }),
            )

            recovered, success = await _sync_order(context, order.id)

            self.assertTrue(success)
            self.assertIsNone(recovered.delivery_message_id)
            self.assertEqual(recovered.post_delivery_prompt_required, 0)
            bot.send_message.assert_not_awaited()

    async def test_second_sync_removes_dead_location_backlink_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivered_at="2026-08-23T10:00:00+05:00",
                delivery_chat_id=-1004404461980,
                delivery_message_id=55,
                location_chat_id=-1002,
                location_header_message_id=59,
                location_message_id=60,
                location_details_message_id=61,
                location_footer_message_id=62,
            )
            detail_edits = 0

            async def edit_message_text(*_args, **kwargs):
                nonlocal detail_edits
                if kwargs.get("message_id") == 55:
                    raise BadRequest("Message to edit not found")
                if kwargs.get("message_id") == 61:
                    detail_edits += 1
                    if detail_edits == 2:
                        raise RuntimeError("temporary Telegram failure")

            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(),
                edit_message_text=AsyncMock(side_effect=edit_message_text),
                delete_message=AsyncMock(),
                send_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1004404461980,
                        location_channel_id=-1002,
                    ),
                }),
            )

            _, first_success = await _sync_order(context, order.id)
            recovered, second_success = await _sync_order(context, order.id)

            self.assertFalse(first_success)
            self.assertTrue(second_success)
            self.assertIsNone(recovered.delivery_message_id)
            self.assertEqual(detail_edits, 3)
            last_detail = [
                call.kwargs["text"]
                for call in bot.edit_message_text.await_args_list
                if call.kwargs.get("message_id") == 61
            ][-1]
            self.assertNotIn("Открыть заказ", last_detail)

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
                manager_chat_id=1,
                manager_message_id=56,
                location_chat_id=-1002,
                location_header_message_id=59,
                location_message_id=60,
                location_details_message_id=61,
                location_footer_message_id=62,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                send_message=AsyncMock(side_effect=[
                    SimpleNamespace(chat_id=-1002, message_id=70),
                    SimpleNamespace(chat_id=-1002, message_id=72),
                    SimpleNamespace(chat_id=-1002, message_id=73),
                ]),
                send_location=AsyncMock(return_value=SimpleNamespace(
                    chat_id=-1002,
                    message_id=71,
                )),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(location_channel_id=-1002),
                }),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertEqual(recovered.location_chat_id, -1002)
            self.assertEqual(recovered.location_header_message_id, 70)
            self.assertEqual(recovered.location_message_id, 71)
            self.assertEqual(recovered.location_details_message_id, 72)
            self.assertEqual(recovered.location_footer_message_id, 73)
            self.assertEqual(
                bot.delete_message.await_args_list,
                [
                    call(chat_id=-1002, message_id=59),
                    call(chat_id=-1002, message_id=60),
                    call(chat_id=-1002, message_id=61),
                    call(chat_id=-1002, message_id=62),
                ],
            )
            self.assertEqual(repo.list_cleanup_messages(order_id=order.id), [])

    async def test_deleted_closed_location_retires_block_without_republishing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivered_at="2026-08-23T10:00:00+05:00",
                delivery_chat_id=-1004404461980,
                delivery_message_id=55,
                location_chat_id=-1002,
                location_header_message_id=59,
                location_message_id=60,
                location_details_message_id=61,
                location_footer_message_id=62,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                edit_message_text=AsyncMock(),
                send_message=AsyncMock(),
                send_location=AsyncMock(),
                delete_message=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertIsNone(recovered.location_chat_id)
            self.assertIsNone(recovered.location_header_message_id)
            self.assertIsNone(recovered.location_message_id)
            self.assertIsNone(recovered.location_details_message_id)
            self.assertIsNone(recovered.location_footer_message_id)
            bot.send_message.assert_not_awaited()
            bot.send_location.assert_not_awaited()
            self.assertEqual(
                bot.delete_message.await_args_list,
                [
                    call(chat_id=-1002, message_id=59),
                    call(chat_id=-1002, message_id=60),
                    call(chat_id=-1002, message_id=61),
                    call(chat_id=-1002, message_id=62),
                ],
            )

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
                location_header_message_id=59,
                location_message_id=60,
                location_details_message_id=61,
                location_footer_message_id=62,
            )
            bot = SimpleNamespace(
                edit_message_reply_markup=AsyncMock(
                    side_effect=BadRequest("Message to edit not found")
                ),
                send_message=AsyncMock(side_effect=[
                    SimpleNamespace(chat_id=-1002, message_id=70),
                    SimpleNamespace(chat_id=-1002, message_id=72),
                    SimpleNamespace(chat_id=-1002, message_id=73),
                ]),
                send_location=AsyncMock(return_value=SimpleNamespace(
                    chat_id=-1002,
                    message_id=71,
                )),
                delete_message=AsyncMock(side_effect=RuntimeError("timeout")),
            )
            context = SimpleNamespace(
                bot=bot,
                application=SimpleNamespace(bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(location_channel_id=-1002),
                }),
            )

            self.assertTrue(await _set_location_marker(context, order, 1))

            recovered = repo.get(order.id)
            self.assertEqual(recovered.location_message_id, 71)
            with repo.connect() as db:
                queued = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM telegram_cleanup_queue WHERE order_id=? ORDER BY id",
                        (order.id,),
                    ).fetchall()
                ]
            self.assertEqual(
                {(item["chat_id"], item["message_id"]) for item in queued},
                {(-1002, 59), (-1002, 60), (-1002, 61), (-1002, 62)},
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
            with repo.connect() as db:
                delayed = db.execute(
                    "SELECT * FROM telegram_cleanup_queue WHERE order_id=?", (order.id,)
                ).fetchone()
            self.assertEqual(delayed["attempts"], 1)
            self.assertIsNotNone(delayed["next_attempt_at"])
            future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(
                timespec="microseconds"
            )
            with patch("app.database.repository.now", return_value=future):
                self.assertTrue(await _process_cleanup_messages(context))
            self.assertEqual(repo.list_cleanup_messages(), [])

    async def test_permanent_cleanup_failure_moves_item_to_dead_letter(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            repo.enqueue_cleanup_messages(order.id, [(-1002, 72)])
            context = SimpleNamespace(
                bot=SimpleNamespace(
                    delete_message=AsyncMock(
                        side_effect=BadRequest("Message can't be deleted")
                    )
                ),
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertFalse(await _process_cleanup_messages(context))
            self.assertEqual(repo.list_cleanup_messages(order_id=order.id), [])
            dead_letters = repo.list_terminal_cleanup_messages(order_id=order.id)
            self.assertEqual(len(dead_letters), 1)
            self.assertEqual(dead_letters[0]["attempts"], 1)

    async def test_cleanup_rate_limit_is_propagated_without_marking_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            repo.enqueue_cleanup_messages(order.id, [(-1002, 72)])
            context = SimpleNamespace(
                bot=SimpleNamespace(delete_message=AsyncMock(side_effect=RetryAfter(7))),
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            with self.assertRaises(RetryAfter):
                await _process_cleanup_messages(context)

            queued = repo.list_cleanup_messages(order_id=order.id)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["attempts"], 0)
            self.assertGreater(
                context.application.bot_data["cleanup_retry_not_before"],
                asyncio.get_running_loop().time(),
            )

            # The shared flood-wait gate prevents another cleanup request from
            # being issued immediately by a foreground or background path.
            self.assertFalse(await _process_cleanup_messages(context))
            context.bot.delete_message.assert_awaited_once()

    async def test_cleanup_retires_old_basic_group_id_after_chat_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            repo.enqueue_cleanup_messages(order.id, [(-5125237049, 72)])
            context = SimpleNamespace(
                bot=SimpleNamespace(
                    delete_message=AsyncMock(
                        side_effect=ChatMigrated(-1004404461980)
                    )
                ),
                application=SimpleNamespace(bot_data={"repo": repo}),
            )

            self.assertTrue(await _process_cleanup_messages(context))
            self.assertEqual(repo.list_cleanup_messages(order_id=order.id), [])
            self.assertEqual(
                repo.list_terminal_cleanup_messages(order_id=order.id),
                [],
            )
            context.bot.delete_message.assert_awaited_once_with(
                chat_id=-5125237049,
                message_id=72,
            )


class ConcurrentPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_pass_cannot_mark_a_newer_business_revision_synced(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "client_phone": "+998901333999",
                    "product": "OLD",
                    "amount_usd": 100,
                },
            )
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-1001,
                delivery_message_id=55,
                manager_chat_id=1,
                manager_message_id=56,
            )
            application = SimpleNamespace(
                bot=SimpleNamespace(),
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1001,
                        location_channel_id=-1002,
                        orders_channel_id=None,
                    ),
                },
            )
            context = SimpleNamespace(bot=application.bot, application=application)

            async def update_during_delivery_refresh(
                _context,
                stale_order,
                **_kwargs,
            ):
                self.assertEqual(stale_order.product, "OLD")
                repo.update(stale_order.id, product="NEW")
                return True

            with patch(
                "app.handlers.orders._refresh_delivery_message",
                side_effect=update_during_delivery_refresh,
            ):
                latest, success = await _sync_order(context, order.id)

            self.assertFalse(success)
            self.assertEqual(latest.product, "NEW")
            self.assertEqual(latest.sync_needed, 1)

    async def test_sync_pass_cannot_accept_external_prompt_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "client_phone": "+998901333999",
                    "product": "Pad 2",
                    "amount_usd": 100,
                },
            )
            order = repo.update(
                order.id,
                status="completed",
                courier_id=1799690992,
                courier_name="Muzrob Oka",
                delivered_at="2026-09-15T11:00:00+05:00",
                delivery_chat_id=-1004404461980,
                delivery_message_id=55,
                post_delivery_prompt_required=1,
            )
            application = SimpleNamespace(
                bot=SimpleNamespace(),
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1004404461980,
                        location_channel_id=-1002,
                        orders_channel_id=None,
                    ),
                },
            )
            context = SimpleNamespace(bot=application.bot, application=application)

            async def attach_during_delivery_refresh(
                _context,
                stale_order,
                **_kwargs,
            ):
                attached = repo.update(
                    stale_order.id,
                    expected_updated_at=stale_order.updated_at,
                    expected_publications={
                        "post_delivery_prompt_chat_id": None,
                        "post_delivery_prompt_message_id": None,
                    },
                    post_delivery_prompt_chat_id=-1004404461980,
                    post_delivery_prompt_message_id=56,
                )
                self.assertIsNotNone(attached)
                return True

            with patch(
                "app.handlers.orders._refresh_delivery_message",
                side_effect=attach_during_delivery_refresh,
            ) as refresh:
                latest, success = await _sync_order(context, order.id)

            self.assertFalse(success)
            self.assertEqual(refresh.await_count, 1)
            self.assertEqual(latest.post_delivery_prompt_message_id, 56)
            self.assertEqual(latest.sync_needed, 1)

    async def test_location_posts_are_deleted_when_database_attach_raises(self):
        order = Order(
            id=1,
            order_number=1,
            manager_id=1,
            manager_name="Manager",
            client_phone="+998901333999",
            product="A56",
            latitude=41.31,
            longitude=69.24,
            updated_at="version-1",
        )
        repo = SimpleNamespace(
            update=Mock(side_effect=RuntimeError("database unavailable")),
            enqueue_cleanup_messages=Mock(side_effect=RuntimeError("database unavailable")),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[
                SimpleNamespace(chat_id=-1002, message_id=69),
                SimpleNamespace(chat_id=-1002, message_id=71),
                SimpleNamespace(chat_id=-1002, message_id=72),
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

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await _publish_location(context, repo, order, 1)

        self.assertEqual(
            bot.delete_message.await_args_list,
            [
                call(chat_id=-1002, message_id=69),
                call(chat_id=-1002, message_id=70),
                call(chat_id=-1002, message_id=71),
                call(chat_id=-1002, message_id=72),
            ],
        )

    async def test_active_partial_location_block_is_replaced_once(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                delivery_chat_id=-1001,
                delivery_message_id=55,
                manager_chat_id=1,
                manager_message_id=56,
                location_chat_id=-1002,
                location_header_message_id=59,
                location_message_id=None,
                location_details_message_id=None,
                location_footer_message_id=None,
            )
            business_revision = order.updated_at
            event_count = len(repo.list_events(order.id))
            sent_ids = iter((101, 103, 104))
            bot = SimpleNamespace(
                send_message=AsyncMock(
                    side_effect=lambda **_kwargs: SimpleNamespace(
                        chat_id=-1002,
                        message_id=next(sent_ids),
                    )
                ),
                send_location=AsyncMock(
                    return_value=SimpleNamespace(chat_id=-1002, message_id=102)
                ),
                edit_message_reply_markup=AsyncMock(),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
            )
            application = SimpleNamespace(
                bot=bot,
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1001,
                        location_channel_id=-1002,
                        orders_channel_id=None,
                    ),
                },
            )
            context = SimpleNamespace(bot=bot, application=application)

            first, first_success = await _sync_order(context, order.id)
            second, second_success = await _sync_order(context, order.id)

            self.assertTrue(first_success)
            self.assertTrue(second_success)
            self.assertEqual(bot.send_message.await_count, 3)
            self.assertEqual(bot.send_location.await_count, 1)
            self.assertEqual(first.updated_at, business_revision)
            self.assertEqual(second.updated_at, business_revision)
            self.assertEqual(len(repo.list_events(order.id)), event_count)
            self.assertEqual(
                (
                    second.location_header_message_id,
                    second.location_message_id,
                    second.location_details_message_id,
                    second.location_footer_message_id,
                ),
                (101, 102, 103, 104),
            )
            bot.delete_message.assert_awaited_once_with(
                chat_id=-1002,
                message_id=59,
            )

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
                {"id": 4, "chat_id": -1002, "message_id": 72},
            ]),
            mark_cleanup_done=Mock(return_value=True),
            mark_cleanup_failed=Mock(return_value=True),
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[
                SimpleNamespace(chat_id=-1002, message_id=69),
                SimpleNamespace(chat_id=-1002, message_id=71),
                SimpleNamespace(chat_id=-1002, message_id=72),
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
                call(chat_id=-1002, message_id=72),
            ],
        )
        repo.update.assert_called_once_with(
            1,
            expected_updated_at="version-1",
            expected_publications={
                "location_chat_id": None,
                "location_header_message_id": None,
                "location_message_id": None,
                "location_details_message_id": None,
                "location_footer_message_id": None,
            },
            location_chat_id=-1002,
            location_header_message_id=69,
            location_message_id=70,
            location_details_message_id=71,
            location_footer_message_id=72,
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

    async def test_sync_order_propagates_retry_after_to_background_workers(self):
        application = SimpleNamespace(bot_data={})
        context = SimpleNamespace(application=application)

        with patch(
            "app.handlers.orders._sync_order_locked",
            new=AsyncMock(side_effect=RetryAfter(60)),
        ):
            with self.assertRaises(RetryAfter):
                await _sync_order(context, 1)

    async def test_location_refresh_retry_after_keeps_sync_tuple_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            application = SimpleNamespace(
                bot=SimpleNamespace(),
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1001,
                        location_channel_id=-1002,
                        orders_channel_id=None,
                    ),
                },
            )
            context = SimpleNamespace(bot=application.bot, application=application)

            with patch(
                "app.handlers.orders._set_location_marker",
                new=AsyncMock(side_effect=RetryAfter(7)),
            ):
                with self.assertRaises(RetryAfter):
                    await _sync_order(context, order.id)

    async def test_two_process_style_location_publish_keeps_one_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.db"
            first_repo = OrderRepository(path)
            second_repo = OrderRepository(path)
            first_repo.initialize()
            order = create_order(first_repo)
            first_repo.update(order.id, status="pending")
            first_order = first_repo.get(order.id)
            second_order = second_repo.get(order.id)
            both_finished_sending = asyncio.Event()
            footer_count = 0

            def make_bot(base: int):
                message_calls = 0

                async def send_message(**_kwargs):
                    nonlocal message_calls, footer_count
                    message_calls += 1
                    message_id = base + {1: 1, 2: 3, 3: 4}[message_calls]
                    if message_calls == 3:
                        footer_count += 1
                        if footer_count == 2:
                            both_finished_sending.set()
                        await both_finished_sending.wait()
                    return SimpleNamespace(chat_id=-1002, message_id=message_id)

                return SimpleNamespace(
                    send_message=AsyncMock(side_effect=send_message),
                    send_location=AsyncMock(return_value=SimpleNamespace(
                        chat_id=-1002,
                        message_id=base + 2,
                    )),
                    delete_message=AsyncMock(),
                )

            first_bot = make_bot(100)
            second_bot = make_bot(200)
            first_context = SimpleNamespace(
                bot=first_bot,
                application=SimpleNamespace(bot_data={
                    "repo": first_repo,
                    "settings": SimpleNamespace(location_channel_id=-1002),
                }),
            )
            second_context = SimpleNamespace(
                bot=second_bot,
                application=SimpleNamespace(bot_data={
                    "repo": second_repo,
                    "settings": SimpleNamespace(location_channel_id=-1002),
                }),
            )

            results = await asyncio.gather(
                _publish_location(first_context, first_repo, first_order),
                _publish_location(second_context, second_repo, second_order),
                return_exceptions=True,
            )

            self.assertEqual(sum(isinstance(result, Order) for result in results), 1)
            self.assertEqual(sum(isinstance(result, RuntimeError) for result in results), 1)
            canonical = first_repo.get(order.id)
            canonical_base = canonical.location_header_message_id - 1
            self.assertIn(canonical_base, {100, 200})
            losing_bot = second_bot if canonical_base == 100 else first_bot
            expected_losing_ids = (
                [201, 202, 203, 204]
                if canonical_base == 100
                else [101, 102, 103, 104]
            )
            self.assertEqual(
                [call.kwargs["message_id"] for call in losing_bot.delete_message.await_args_list],
                expected_losing_ids,
            )
            self.assertEqual(first_repo.list_cleanup_messages(order_id=order.id), [])

    async def test_retry_task_honors_telegram_flood_wait(self):
        order = SimpleNamespace(id=1, sync_needed=1)
        repo = SimpleNamespace(get=Mock(return_value=order))
        application = SimpleNamespace(
            bot=SimpleNamespace(),
            bot_data={"repo": repo, "sync_retry_orders": {1}},
        )

        with (
            patch("app.handlers.orders.asyncio.sleep", new=AsyncMock()) as sleep,
            patch("app.handlers.orders._sync_retry_remaining", return_value=0),
            patch(
                "app.handlers.orders._sync_order",
                new=AsyncMock(side_effect=[RetryAfter(7), (order, True)]),
            ) as sync,
        ):
            await _retry_order_sync(application, 1)

        self.assertEqual(sync.await_count, 2)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 7.0])
        self.assertNotIn(1, application.bot_data["sync_retry_orders"])
        self.assertGreater(
            application.bot_data["sync_retry_not_before"][1],
            asyncio.get_running_loop().time(),
        )


class StartupLegacyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_muzrob_order_moves_once_to_new_supergroup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="pending",
                assigned_courier_id=1799690992,
                assigned_courier_name="Muzrob Oka",
                delivery_chat_id=-5125237049,
                delivery_message_id=55,
                manager_chat_id=1,
                manager_message_id=56,
                location_chat_id=-1002,
                location_header_message_id=59,
                location_message_id=60,
                location_details_message_id=61,
                location_footer_message_id=62,
            )
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(
                    chat_id=-1004404461980,
                    message_id=101,
                )),
                edit_message_reply_markup=AsyncMock(),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
            )
            application = SimpleNamespace(
                bot=bot,
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1004404461980,
                        location_channel_id=-1002,
                        orders_channel_id=None,
                    ),
                },
            )
            context = SimpleNamespace(bot=bot, application=application)

            migrated, success = await _sync_order(context, order.id)
            await _process_cleanup_messages(context, order_id=order.id)

            self.assertTrue(success)
            self.assertEqual(migrated.delivery_chat_id, -1004404461980)
            self.assertEqual(migrated.delivery_message_id, 101)
            bot.send_message.assert_awaited_once()
            self.assertEqual(bot.send_message.await_args.args[0], -1004404461980)
            bot.delete_message.assert_awaited_once_with(
                chat_id=-5125237049,
                message_id=55,
            )
            detail_texts = [
                item.kwargs["text"]
                for item in bot.edit_message_text.await_args_list
                if item.kwargs.get("message_id") == 61
            ]
            self.assertTrue(detail_texts)
            self.assertIn(
                "https://t.me/c/4404461980/101",
                detail_texts[-1],
            )

    async def test_closed_muzrob_order_is_not_reposted_to_new_supergroup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = repo.create(
                manager_id=1,
                manager_name="Manager",
                data={
                    "client_phone": "+998901333999",
                    "product": "A56",
                    "amount_usd": 375,
                },
            )
            order = repo.update(
                order.id,
                status="completed",
                assigned_courier_id=1799690992,
                assigned_courier_name="Muzrob Oka",
                courier_id=1799690992,
                courier_name="Muzrob Oka",
                delivered_at="2026-09-15T10:00:00+05:00",
                delivery_chat_id=-5125237049,
                delivery_message_id=55,
            )
            bot = SimpleNamespace(
                send_message=AsyncMock(),
                delete_message=AsyncMock(),
            )
            application = SimpleNamespace(
                bot=bot,
                bot_data={
                    "repo": repo,
                    "settings": SimpleNamespace(
                        delivery_group_id=-1004404461980,
                        location_channel_id=-1002,
                        orders_channel_id=None,
                    ),
                },
            )
            context = SimpleNamespace(bot=bot, application=application)

            migrated, success = await _sync_order(context, order.id)
            await _process_cleanup_messages(context, order_id=order.id)

            self.assertTrue(success)
            self.assertIsNone(migrated.delivery_chat_id)
            self.assertIsNone(migrated.delivery_message_id)
            bot.send_message.assert_not_awaited()
            bot.delete_message.assert_awaited_once_with(
                chat_id=-5125237049,
                message_id=55,
            )

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

    async def test_changed_delivery_group_retires_linked_completed_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = OrderRepository(Path(directory) / "delivery.db")
            repo.initialize()
            order = create_order(repo)
            order = repo.update(
                order.id,
                status="completed",
                delivered_at="2026-08-23T10:00:00+05:00",
                delivery_chat_id=-1001,
                delivery_message_id=10,
                post_delivery_prompt_required=1,
                post_delivery_prompt_chat_id=-1001,
                post_delivery_prompt_message_id=11,
            )

            reset = _reset_mismatched_publications(
                repo,
                SimpleNamespace(delivery_group_id=-2001, location_channel_id=-1002),
                order,
            )

            self.assertIsNone(reset.delivery_chat_id)
            self.assertIsNone(reset.delivery_message_id)
            self.assertEqual(reset.post_delivery_prompt_required, 0)
            self.assertIsNone(reset.post_delivery_prompt_chat_id)
            self.assertIsNone(reset.post_delivery_prompt_message_id)
            queued = repo.list_cleanup_messages(order_id=order.id)
            self.assertEqual(
                {(item["chat_id"], item["message_id"]) for item in queued},
                {(-1001, 10), (-1001, 11)},
            )


if __name__ == "__main__":
    unittest.main()
