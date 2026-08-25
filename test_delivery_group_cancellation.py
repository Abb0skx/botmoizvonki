import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.database import OrderRepository
from app.handlers.orders import group_cancel_action


ABBOS_ID = 202134293
ABBOS_GROUP_ID = -5216093690


class GroupCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = OrderRepository(Path(self.tempdir.name) / "delivery.db")
        self.repo.initialize()
        draft = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data={
                "seller_name": "Ali",
                "client_phone": "+998901333999",
                "product": "A57",
                "amount_usd": 100,
            },
        )
        self.order = self.repo.transition(
            draft.id,
            {"draft"},
            status="pending",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            delivery_chat_id=ABBOS_GROUP_ID,
            delivery_message_id=77,
        )
        self.settings = SimpleNamespace(
            manager_ids=frozenset({11}),
            courier_ids=frozenset({ABBOS_ID}),
            delivery_group_id=ABBOS_GROUP_ID,
            orders_channel_id=-1004459657817,
        )
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member")
            ),
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"settings": self.settings, "repo": self.repo}
            ),
            bot=self.bot,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def query(self, action: str, *, user_id: int = 900, message_id: int = 77):
        return SimpleNamespace(
            data=f"{action}:{self.order.id}",
            from_user=SimpleNamespace(
                id=user_id,
                full_name="Сотрудник <склада>",
                username="warehouse_staff",
                is_bot=False,
            ),
            message=SimpleNamespace(
                chat_id=ABBOS_GROUP_ID,
                message_id=message_id,
            ),
            answer=AsyncMock(),
        )

    async def run_action(self, query):
        finish = AsyncMock(return_value=True)
        notify = AsyncMock()
        with (
            patch(
                "app.handlers.orders._finish_status_change_locked",
                new=finish,
            ),
            patch("app.handlers.orders._notify_log", new=notify),
        ):
            await group_cancel_action(
                SimpleNamespace(callback_query=query),
                self.context,
            )
        return finish, notify

    async def test_any_current_group_member_can_cancel_and_is_saved(self):
        query = self.query("cancel")

        finish, notify = await self.run_action(query)

        cancelled = self.repo.get(self.order.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.cancelled_from_status, "pending")
        self.assertEqual(cancelled.cancelled_by_id, 900)
        self.assertEqual(cancelled.cancelled_by_name, "Сотрудник <склада>")
        self.assertEqual(cancelled.cancelled_by_username, "warehouse_staff")
        self.assertIsNotNone(cancelled.cancelled_at)
        self.assertEqual(cancelled.assigned_courier_id, ABBOS_ID)
        self.assertIsNone(cancelled.courier_id)
        self.bot.get_chat_member.assert_awaited_once_with(ABBOS_GROUP_ID, 900)
        query.answer.assert_awaited_once_with("Заказ отменён")
        finish.assert_awaited_once()
        log_text = notify.await_args.args[1]
        self.assertIn("Сотрудник &lt;склада&gt;", log_text)
        self.assertIn("@warehouse_staff", log_text)
        self.assertIn("900", log_text)

        events = self.repo.list_events(self.order.id)
        cancellation = [event for event in events if event.event_type == "order_cancelled"]
        self.assertEqual(len(cancellation), 1)
        self.assertEqual(cancellation[0].actor_id, 900)
        self.assertEqual(cancellation[0].actor_username, "warehouse_staff")
        self.assertEqual(cancellation[0].actor_role, "group_member")

    async def test_cancel_does_not_replace_courier_custody(self):
        self.order = self.repo.transition(
            self.order.id,
            {"pending"},
            status="on_way",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            picked_up_at="2026-08-25T12:00:00+05:00",
            time_started="2026-08-25T12:10:00+05:00",
        )

        await self.run_action(self.query("cancel", user_id=901))

        cancelled = self.repo.get(self.order.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.cancelled_from_status, "on_way")
        self.assertEqual(cancelled.courier_id, ABBOS_ID)
        self.assertEqual(cancelled.courier_name, "Abbos")
        self.assertEqual(cancelled.cancelled_by_id, 901)

    async def test_different_group_member_can_restore_previous_status(self):
        await self.run_action(self.query("cancel", user_id=900))

        query = self.query("undo_cancel", user_id=901)
        await self.run_action(query)

        restored = self.repo.get(self.order.id)
        self.assertEqual(restored.status, "pending")
        # The last cancellation snapshot remains available for audit, while
        # formatters hide it because the order is active again.
        self.assertEqual(restored.cancelled_by_id, 900)
        query.answer.assert_awaited_once_with("Заказ возвращён")
        events = self.repo.list_events(self.order.id)
        restored_event = [
            event for event in events if event.event_type == "order_cancel_restored"
        ]
        self.assertEqual(len(restored_event), 1)
        self.assertEqual(restored_event[0].actor_id, 901)

    async def test_non_member_cannot_cancel(self):
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        query = self.query("cancel")

        finish, notify = await self.run_action(query)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        finish.assert_not_awaited()
        notify.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_restricted_current_member_can_cancel(self):
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status="restricted",
            is_member=True,
        )

        await self.run_action(self.query("cancel"))

        self.assertEqual(self.repo.get(self.order.id).status, "cancelled")

    async def test_membership_api_failure_is_fail_closed(self):
        self.bot.get_chat_member.side_effect = RuntimeError("temporary Telegram error")
        query = self.query("cancel")

        finish, _ = await self.run_action(query)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        finish.assert_not_awaited()
        self.assertIn("Повторите", query.answer.await_args.args[0])

    async def test_stale_card_cannot_cancel(self):
        query = self.query("cancel", message_id=76)

        finish, _ = await self.run_action(query)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        self.bot.get_chat_member.assert_not_awaited()
        finish.assert_not_awaited()
        self.assertIn("устарела", query.answer.await_args.args[0])

    async def test_button_from_another_group_cannot_cancel(self):
        query = self.query("cancel")
        query.message.chat_id = -5111626405

        finish, _ = await self.run_action(query)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        self.bot.get_chat_member.assert_not_awaited()
        finish.assert_not_awaited()

    async def test_bot_or_anonymous_service_actor_cannot_cancel(self):
        query = self.query("cancel")
        query.from_user.is_bot = True

        finish, _ = await self.run_action(query)

        self.assertEqual(self.repo.get(self.order.id).status, "pending")
        self.bot.get_chat_member.assert_not_awaited()
        finish.assert_not_awaited()

    async def test_dual_role_assigned_courier_is_audited_as_courier(self):
        self.settings.manager_ids = frozenset({11, ABBOS_ID})

        await self.run_action(self.query("cancel", user_id=ABBOS_ID))

        event = next(
            event
            for event in self.repo.list_events(self.order.id)
            if event.event_type == "order_cancelled"
        )
        self.assertEqual(event.actor_role, "courier")
        self.assertEqual(event.courier_id, ABBOS_ID)

    async def test_missing_username_is_saved_and_rendered_safely(self):
        query = self.query("cancel")
        query.from_user.username = None

        _, notify = await self.run_action(query)

        cancelled = self.repo.get(self.order.id)
        self.assertIsNone(cancelled.cancelled_by_username)
        self.assertNotIn("(@", notify.await_args.args[1])

    async def test_on_way_cancel_and_restore_preserves_trip(self):
        self.order = self.repo.transition(
            self.order.id,
            {"pending"},
            status="on_way",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            picked_up_at="2026-08-25T12:00:00+05:00",
            time_started="2026-08-25T12:10:00+05:00",
        )
        await self.run_action(self.query("cancel", user_id=900))

        await self.run_action(self.query("undo_cancel", user_id=901))

        restored = self.repo.get(self.order.id)
        self.assertEqual(restored.status, "on_way")
        self.assertEqual(restored.courier_id, ABBOS_ID)
        self.assertEqual(restored.time_started, "2026-08-25T12:10:00+05:00")

    async def test_on_way_restore_is_blocked_when_courier_started_another_order(self):
        self.order = self.repo.transition(
            self.order.id,
            {"pending"},
            status="on_way",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            picked_up_at="2026-08-25T12:00:00+05:00",
            time_started="2026-08-25T12:10:00+05:00",
        )
        await self.run_action(self.query("cancel", user_id=900))
        second_draft = self.repo.create(
            manager_id=11,
            manager_name="Otabek",
            data={
                "client_phone": "+998901111111",
                "product": "A58",
                "amount_usd": 110,
            },
        )
        second = self.repo.transition(
            second_draft.id,
            {"draft"},
            status="on_way",
            assigned_courier_id=ABBOS_ID,
            assigned_courier_name="Abbos",
            courier_id=ABBOS_ID,
            courier_name="Abbos",
            picked_up_at="2026-08-25T12:20:00+05:00",
            time_started="2026-08-25T12:30:00+05:00",
        )
        query = self.query("undo_cancel", user_id=901)

        finish, _ = await self.run_action(query)

        self.assertEqual(self.repo.get(self.order.id).status, "cancelled")
        self.assertEqual(self.repo.get(second.id).status, "on_way")
        self.assertIn(f"№{second.order_number}", query.answer.await_args.args[0])
        finish.assert_not_awaited()

    async def test_two_simultaneous_cancellations_create_one_event(self):
        first = self.query("cancel", user_id=900)
        second = self.query("cancel", user_id=901)
        finish = AsyncMock(return_value=True)
        notify = AsyncMock()
        with (
            patch(
                "app.handlers.orders._finish_status_change_locked",
                new=finish,
            ),
            patch("app.handlers.orders._notify_log", new=notify),
        ):
            await asyncio.gather(
                group_cancel_action(
                    SimpleNamespace(callback_query=first),
                    self.context,
                ),
                group_cancel_action(
                    SimpleNamespace(callback_query=second),
                    self.context,
                ),
            )

        events = [
            event
            for event in self.repo.list_events(self.order.id)
            if event.event_type == "order_cancelled"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(finish.await_count, 1)
        self.assertEqual(notify.await_count, 1)


if __name__ == "__main__":
    unittest.main()
