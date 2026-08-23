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

from telegram.error import BadRequest, RetryAfter, TimedOut

from app.bot.application import (
    HEALTH_SIGNAL_FILENAME,
    SYNC_RECONCILIATION_BATCH_SIZE,
    _delivery_sync_worker,
    _start_delivery_sync_worker,
    _touch_health_signal,
    initialize_delivery_runtime,
    reconcile_pending_orders,
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
        ):
            with self.assertLogs("app.bot.application", level="WARNING"):
                await initialize_delivery_runtime(application)

        self.assertFalse(application.bot_data["delivery_preflight_validated"])
        start_worker.assert_called_once_with(application)

    async def test_startup_rate_limit_is_treated_as_temporary(self):
        application = self.application()
        with (
            patch(
                "app.bot.application.validate_delivery_configuration",
                AsyncMock(side_effect=RetryAfter(7)),
            ),
            patch("app.bot.application._start_delivery_sync_worker") as start_worker,
        ):
            with self.assertLogs("app.bot.application", level="WARNING"):
                await initialize_delivery_runtime(application)

        self.assertFalse(application.bot_data["delivery_preflight_validated"])
        start_worker.assert_called_once_with(application)

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


if __name__ == "__main__":
    unittest.main()
