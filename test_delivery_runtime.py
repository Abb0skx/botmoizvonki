import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
from telegram.error import BadRequest, RetryAfter, TimedOut

from app.bot.application import (
    HEALTH_SIGNAL_FILENAME,
    PICKUP_REMINDER_INTERVAL,
    PICKUP_REMINDER_TASK_KEY,
    SYNC_RECONCILIATION_BATCH_SIZE,
    _delivery_sync_worker,
    _pickup_reminder_worker,
    _pickup_reminder_slot,
    _seconds_until_next_pickup_reminder,
    _start_pickup_reminder_worker,
    error_handler,
    _sleep_with_health_signal,
    _start_delivery_sync_worker,
    _touch_health_signal,
    initialize_delivery_runtime,
    reconcile_pending_orders,
    reconcile_sales_card_requests,
    send_waiting_pickup_reminders,
    shutdown_delivery_runtime,
)


class DeliveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def application(**bot_data):
        return SimpleNamespace(
            bot=SimpleNamespace(),
            bot_data=bot_data,
            stop_running=Mock(),
        )

    async def test_temporary_preflight_timeout_does_not_abort_startup(self):
        application = self.application()
        with (
            patch(
                "app.bot.application.validate_delivery_configuration",
                AsyncMock(side_effect=TimedOut()),
            ),
            patch("app.bot.application._start_delivery_sync_worker") as start_worker,
            patch("app.bot.application._start_pickup_reminder_worker") as start_reminders,
        ):
            with self.assertLogs("app.bot.application", level="WARNING"):
                await initialize_delivery_runtime(application)

        self.assertFalse(application.bot_data["delivery_preflight_validated"])
        start_worker.assert_called_once_with(application)
        start_reminders.assert_called_once_with(application)

    async def test_startup_rate_limit_is_treated_as_temporary(self):
        application = self.application()
        with (
            patch(
                "app.bot.application.validate_delivery_configuration",
                AsyncMock(side_effect=RetryAfter(7)),
            ),
            patch("app.bot.application._start_delivery_sync_worker") as start_worker,
            patch("app.bot.application._start_pickup_reminder_worker") as start_reminders,
        ):
            with self.assertLogs("app.bot.application", level="WARNING"):
                await initialize_delivery_runtime(application)

        self.assertFalse(application.bot_data["delivery_preflight_validated"])
        start_worker.assert_called_once_with(application)
        start_reminders.assert_called_once_with(application)

    async def test_confirmed_invalid_chat_configuration_remains_fatal(self):
        application = self.application()
        with (
            patch(
                "app.bot.application.validate_delivery_configuration",
                AsyncMock(side_effect=RuntimeError("wrong chat type")),
            ),
            patch("app.bot.application._start_delivery_sync_worker") as start_worker,
        ):
            with self.assertRaisesRegex(RuntimeError, "wrong chat type"):
                await initialize_delivery_runtime(application)

        start_worker.assert_not_called()

    async def test_bad_request_is_not_mistaken_for_a_temporary_network_error(self):
        application = self.application()
        with (
            patch(
                "app.bot.application.validate_delivery_configuration",
                AsyncMock(side_effect=BadRequest("Chat not found")),
            ),
            patch("app.bot.application._start_delivery_sync_worker") as start_worker,
        ):
            with self.assertRaisesRegex(BadRequest, "Chat not found"):
                await initialize_delivery_runtime(application)

        start_worker.assert_not_called()

    async def test_pending_reconciliation_is_bounded_and_continues_after_one_failure(self):
        orders = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        repo = SimpleNamespace(
            list_needing_sync=Mock(return_value=orders),
            mark_sync_attempted=Mock(),
            list_cleanup_messages=Mock(return_value=[]),
        )
        application = self.application(repo=repo)
        sync = AsyncMock(side_effect=[RuntimeError("broken message"), (orders[1], True)])

        with patch("app.bot.application._sync_order", sync):
            with self.assertLogs("app.bot.application", level="ERROR"):
                await reconcile_pending_orders(application)

        repo.list_needing_sync.assert_called_once_with(limit=SYNC_RECONCILIATION_BATCH_SIZE)
        self.assertEqual([call.args[1] for call in sync.await_args_list], [1, 2])

    async def test_pending_reconciliation_propagates_rate_limit_and_stops_batch(self):
        orders = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        repo = SimpleNamespace(
            list_needing_sync=Mock(return_value=orders),
            mark_sync_attempted=Mock(),
            list_cleanup_messages=Mock(return_value=[]),
        )
        application = self.application(repo=repo)
        sync = AsyncMock(side_effect=RetryAfter(7))

        with patch("app.bot.application._sync_order", sync):
            with self.assertRaises(RetryAfter):
                await reconcile_pending_orders(application)

        sync.assert_awaited_once()
        repo.mark_sync_attempted.assert_not_called()

    async def test_sales_card_recovery_queues_each_missing_product_photo_once(self):
        orders = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        repo = SimpleNamespace(
            claim_periodic_job=Mock(return_value=True),
            list_sales_cards_needing_queue=Mock(return_value=orders),
        )
        application = self.application(repo=repo)
        queue = AsyncMock(side_effect=[(orders[0], True), (orders[1], False)])

        with patch("app.bot.application._queue_product_photo_sales_card", queue):
            recovered = await reconcile_sales_card_requests(application, slot=42)

        self.assertEqual(recovered, 1)
        repo.claim_periodic_job.assert_called_once_with("sales_card_autoqueue", 42)
        repo.list_sales_cards_needing_queue.assert_called_once()
        self.assertEqual(queue.await_count, 2)
        self.assertTrue(
            all(call.kwargs["actor_role"] == "integration" for call in queue.await_args_list)
        )

    async def test_sales_card_recovery_duplicate_slot_does_not_scan(self):
        repo = SimpleNamespace(
            claim_periodic_job=Mock(return_value=False),
            list_sales_cards_needing_queue=Mock(),
        )
        application = self.application(repo=repo)

        recovered = await reconcile_sales_card_requests(application, slot=42)

        self.assertEqual(recovered, 0)
        repo.list_sales_cards_needing_queue.assert_not_called()

    async def test_callback_error_is_an_alert_and_never_a_group_message(self):
        query = SimpleNamespace(answer=AsyncMock())
        update = Update(update_id=1, callback_query=query)
        context = SimpleNamespace(error=RuntimeError("boom"))

        with self.assertLogs("app.bot.application", level="ERROR"):
            await error_handler(update, context)

        query.answer.assert_awaited_once_with(
            "Произошла ошибка. Попробуйте ещё раз.",
            show_alert=True,
        )

    async def test_worker_runs_full_reconciliation_on_first_background_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(database_path=Path(directory) / "delivery.db")
            application = self.application(
                settings=settings,
                delivery_preflight_validated=True,
            )
            full = AsyncMock()
            sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError()])

            with (
                patch("app.bot.application.reconcile_orders_on_start", full),
                patch("app.bot.application.asyncio.sleep", sleeps),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await _delivery_sync_worker(application)

            full.assert_awaited_once_with(application)

    async def test_unexpected_worker_failure_stops_application_for_container_restart(self):
        application = self.application()
        with patch(
            "app.bot.application._delivery_sync_worker",
            AsyncMock(side_effect=RuntimeError("worker crashed")),
        ):
            _start_delivery_sync_worker(application)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        application.stop_running.assert_called_once()

    def test_pickup_reminder_is_aligned_to_the_next_half_hour(self):
        timestamp = PICKUP_REMINDER_INTERVAL * 10 + 1_200

        self.assertEqual(_pickup_reminder_slot(timestamp), 10)
        self.assertEqual(_seconds_until_next_pickup_reminder(timestamp), 600)
        self.assertEqual(
            _seconds_until_next_pickup_reminder(PICKUP_REMINDER_INTERVAL * 11),
            PICKUP_REMINDER_INTERVAL,
        )

    async def test_pickup_reminder_waits_until_boundary_before_first_snapshot(self):
        application = self.application()
        sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        publish = AsyncMock()

        with (
            patch("app.bot.application.asyncio.sleep", sleeps),
            patch(
                "app.bot.application._seconds_until_next_pickup_reminder",
                return_value=125.0,
            ),
            patch("app.bot.application._pickup_reminder_slot", return_value=42),
            patch("app.bot.application.send_waiting_pickup_reminders", publish),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _pickup_reminder_worker(application)

        self.assertEqual(sleeps.await_args_list[0].args, (125.0,))
        publish.assert_awaited_once_with(application, slot=42)

    async def test_pickup_reminder_retries_next_cycle_after_failure(self):
        application = self.application()
        sleeps = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        publish = AsyncMock(side_effect=[RuntimeError("temporary"), None])

        with (
            patch("app.bot.application.asyncio.sleep", sleeps),
            patch(
                "app.bot.application._seconds_until_next_pickup_reminder",
                return_value=1.0,
            ),
            patch(
                "app.bot.application._pickup_reminder_slot",
                side_effect=[42, 43],
            ),
            patch("app.bot.application.send_waiting_pickup_reminders", publish),
        ):
            with self.assertLogs("app.bot.application", level="ERROR"):
                with self.assertRaises(asyncio.CancelledError):
                    await _pickup_reminder_worker(application)

        self.assertEqual(publish.await_count, 2)

    async def test_pickup_reminder_sends_every_digest_page_to_log(self):
        repo = SimpleNamespace(claim_periodic_job=Mock(return_value=True))
        settings = SimpleNamespace(orders_channel_id=-1004459657817)
        application = self.application(repo=repo, settings=settings)
        messages = ["первая", "вторая"]
        notify = AsyncMock()

        with (
            patch(
                "app.bot.application._waiting_pickup_reminder_messages",
                return_value=messages,
            ) as formatter,
            patch("app.bot.application._notify_log", notify),
        ):
            sent = await send_waiting_pickup_reminders(application, slot=42)

        self.assertEqual(sent, 2)
        repo.claim_periodic_job.assert_called_once_with(
            "waiting_pickup:-1004459657817",
            42,
        )
        formatter.assert_called_once_with(repo)
        self.assertEqual([call.args[1] for call in notify.await_args_list], messages)

    async def test_pickup_reminder_duplicate_slot_is_not_built_or_sent(self):
        repo = SimpleNamespace(claim_periodic_job=Mock(return_value=False))
        settings = SimpleNamespace(orders_channel_id=-1004459657817)
        application = self.application(repo=repo, settings=settings)

        with (
            patch("app.bot.application._waiting_pickup_reminder_messages") as formatter,
            patch("app.bot.application._notify_log", AsyncMock()) as notify,
        ):
            sent = await send_waiting_pickup_reminders(application, slot=42)

        self.assertEqual(sent, 0)
        formatter.assert_not_called()
        notify.assert_not_awaited()

    async def test_pickup_reminder_is_singleton_and_shutdown_cancels_it(self):
        application = self.application()

        _start_pickup_reminder_worker(application)
        first = application.bot_data[PICKUP_REMINDER_TASK_KEY]
        _start_pickup_reminder_worker(application)

        self.assertIs(application.bot_data[PICKUP_REMINDER_TASK_KEY], first)
        await shutdown_delivery_runtime(application)
        self.assertTrue(first.cancelled())
        self.assertNotIn(PICKUP_REMINDER_TASK_KEY, application.bot_data)

    async def test_worker_stops_polling_after_later_confirmed_preflight_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(database_path=Path(directory) / "delivery.db")
            application = self.application(
                settings=settings,
                delivery_preflight_validated=False,
            )

            with (
                patch("app.bot.application.asyncio.sleep", AsyncMock(return_value=None)),
                patch(
                    "app.bot.application.reconcile_orders_on_start",
                    AsyncMock(side_effect=RuntimeError("bot is not an administrator")),
                ),
            ):
                with self.assertLogs("app.bot.application", level="CRITICAL"):
                    await _delivery_sync_worker(application)

            application.stop_running.assert_called_once_with()

    async def test_worker_stops_after_later_bad_request_preflight_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(database_path=Path(directory) / "delivery.db")
            application = self.application(
                settings=settings,
                delivery_preflight_validated=False,
            )

            with (
                patch("app.bot.application.asyncio.sleep", AsyncMock(return_value=None)),
                patch(
                    "app.bot.application.reconcile_orders_on_start",
                    AsyncMock(side_effect=BadRequest("Chat not found")),
                ),
            ):
                with self.assertLogs("app.bot.application", level="CRITICAL"):
                    await _delivery_sync_worker(application)

            application.stop_running.assert_called_once_with()

    async def test_worker_waits_and_retries_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(database_path=Path(directory) / "delivery.db")
            application = self.application(
                settings=settings,
                delivery_preflight_validated=False,
            )
            sleeps = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])

            with (
                patch("app.bot.application.asyncio.sleep", sleeps),
                patch(
                    "app.bot.application.reconcile_orders_on_start",
                    AsyncMock(side_effect=RetryAfter(7)),
                ),
            ):
                with self.assertLogs("app.bot.application", level="WARNING"):
                    with self.assertRaises(asyncio.CancelledError):
                        await _delivery_sync_worker(application)

            self.assertEqual(sleeps.await_args_list[1].args, (7.0,))
            application.stop_running.assert_not_called()

    async def test_incremental_worker_waits_after_rate_limit_without_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(database_path=Path(directory) / "delivery.db")
            application = self.application(
                settings=settings,
                delivery_preflight_validated=True,
            )
            sleeps = AsyncMock(
                side_effect=[None, None, None, asyncio.CancelledError()]
            )
            pending = AsyncMock(side_effect=RetryAfter(9))

            with (
                patch("app.bot.application.asyncio.sleep", sleeps),
                patch(
                    "app.bot.application.reconcile_orders_on_start",
                    AsyncMock(),
                ),
                patch("app.bot.application.reconcile_pending_orders", pending),
            ):
                with self.assertLogs("app.bot.application", level="WARNING"):
                    with self.assertRaises(asyncio.CancelledError):
                        await _delivery_sync_worker(application)

            pending.assert_awaited_once_with(application)
            self.assertEqual(sleeps.await_args_list[2].args, (9.0,))
            application.stop_running.assert_not_called()

    async def test_long_rate_limit_wait_keeps_health_signal_fresh(self):
        application = self.application()
        with (
            patch("app.bot.application.asyncio.sleep", AsyncMock()) as sleep,
            patch("app.bot.application._touch_health_signal") as touch,
        ):
            await _sleep_with_health_signal(application, 185)

        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [60.0, 60.0, 60.0, 5.0],
        )
        self.assertEqual(touch.call_count, 5)

    def test_health_signal_is_written_next_to_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "delivery.db"
            application = self.application(
                settings=SimpleNamespace(database_path=database_path),
            )

            _touch_health_signal(application)

            self.assertTrue((database_path.parent / HEALTH_SIGNAL_FILENAME).is_file())


class DeliveryComposeHealthcheckTests(unittest.TestCase):
    @staticmethod
    def healthcheck_command():
        compose = Path("compose.delivery.yaml").read_text(encoding="utf-8")
        test_line = next(line for line in compose.splitlines() if line.lstrip().startswith("test: ["))
        command = json.loads(test_line.split("test:", 1)[1].strip())
        return [sys.executable, *command[2:]]

    def test_healthcheck_opens_existing_database_read_only_and_checks_schema(self):
        compose = Path("compose.delivery.yaml").read_text(encoding="utf-8")

        self.assertIn("p.is_file()", compose)
        self.assertIn("?mode=ro", compose)
        self.assertIn("uri=True", compose)
        self.assertIn("name='orders'", compose)
        self.assertIn("PRAGMA quick_check", compose)
        self.assertIn(HEALTH_SIGNAL_FILENAME, compose)

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "delivery.db"
            with sqlite3.connect(database_path) as database:
                database.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
            database_path.with_name(HEALTH_SIGNAL_FILENAME).touch()
            environment = os.environ.copy()
            environment["DELIVERY_DB_PATH"] = str(database_path)

            result = subprocess.run(
                self.healthcheck_command(),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_healthcheck_does_not_create_a_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "missing.db"
            environment = os.environ.copy()
            environment["DELIVERY_DB_PATH"] = str(database_path)

            result = subprocess.run(
                self.healthcheck_command(),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(database_path.exists())

    def test_delivery_containers_drop_secrets_and_runtime_privileges(self):
        compose = Path("compose.delivery.yaml").read_text(encoding="utf-8")
        dockerfile = Path("Dockerfile.delivery").read_text(encoding="utf-8")

        self.assertIn('DELIVERY_BOT_TOKEN: ""', compose)
        self.assertIn('DELIVERY_STATS_PASSWORD: ""', compose)
        self.assertEqual(compose.count("read_only: true"), 2)
        self.assertEqual(compose.count("no-new-privileges:true"), 2)
        self.assertEqual(compose.count("- ALL"), 2)
        self.assertIn("condition: service_healthy", compose)
        # A SQLite WAL reader needs directory write access for its transient
        # -shm sidecar even though the database connection uses mode=ro.
        self.assertNotIn("- delivery-data:/app/data:ro", compose)
        self.assertEqual(compose.count("- delivery-data:/app/data"), 2)
        self.assertIn("- delivery-stats-cache:/app/cache", compose)
        self.assertIn("DELIVERY_CACHE_PATH: /app/cache", compose)
        self.assertEqual(compose.count("pids_limit: 256"), 2)
        self.assertIn("COPY app ./app", dockerfile)
        self.assertIn("/app/data /app/cache", dockerfile)
        self.assertNotIn("COPY . .", dockerfile)


if __name__ == "__main__":
    unittest.main()
