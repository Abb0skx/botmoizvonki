import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from telegram import Update
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    ContextTypes,
    PersistenceInput,
    PicklePersistence,
)
from telegram.ext._picklepersistence import _BotPickler

from app.config import Settings
from app.database import OrderRepository
from app.handlers import register_handlers
from app.handlers.orders import (
    _process_cleanup_messages,
    _sync_order,
    reconcile_orders_on_start,
    validate_delivery_configuration,
)
from app.routing_service import RoutingService

logger = logging.getLogger(__name__)
CONVERSATION_PERSISTENCE_NAME = "delivery_order_creation"
PERSISTENCE_UPDATE_INTERVAL = 5
SYNC_RECONCILIATION_INTERVAL = 30
FULL_RECONCILIATION_EVERY = 10
SYNC_RECONCILIATION_BATCH_SIZE = 100
SYNC_WORKER_TASK_KEY = "delivery_sync_worker_task"
HEALTH_SIGNAL_FILENAME = "delivery-heartbeat"


class AtomicPicklePersistence(PicklePersistence):
    """PicklePersistence with crash-safe writes and corrupt-file recovery.

    The upstream single-file writer truncates the live file before writing.
    A power loss in that small window would otherwise leave the container in a
    permanent restart loop. SQLite remains the source of truth; only unfinished
    chat input is quarantined if an older pickle is already corrupt.
    """

    def _load_singlefile(self) -> None:
        try:
            super()._load_singlefile()
        except TypeError:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            quarantine = self.filepath.with_name(f"{self.filepath.name}.corrupt-{timestamp}")
            try:
                self.filepath.replace(quarantine)
            except OSError:
                logger.exception("Could not quarantine corrupt delivery conversation state")
                raise
            logger.critical(
                "Corrupt delivery conversation state moved to %s; starting with empty drafts",
                quarantine,
            )
            super()._load_singlefile()

    def _dump_singlefile(self) -> None:
        data = {
            "conversations": self.conversations,
            "user_data": self.user_data,
            "chat_data": self.chat_data,
            "bot_data": self.bot_data,
            "callback_data": self.callback_data,
        }
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.filepath.parent,
                prefix=f".{self.filepath.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                _BotPickler(
                    self.bot,
                    temporary,
                    protocol=-1,
                ).dump(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.filepath)
            temporary_name = None
            # Persist the directory entry as well as the file contents.
            directory_fd = os.open(self.filepath.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try: await update.effective_message.reply_text("Произошла ошибка. Попробуйте ещё раз или используйте /cancel.")
        except Exception: logger.exception("Could not notify user")


def _touch_health_signal(application: Application) -> None:
    """Signal that the bot event loop and its reconciliation worker are alive."""
    settings: Settings = application.bot_data["settings"]
    health_path = Path(settings.database_path).with_name(HEALTH_SIGNAL_FILENAME)
    try:
        health_path.touch(exist_ok=True)
    except OSError:
        # A heartbeat failure must not stop polling. The container healthcheck
        # will still verify SQLite and report the stale/missing signal.
        logger.exception("Could not update delivery bot health signal")


async def reconcile_pending_orders(application: Application) -> None:
    """Retry every order that SQLite still marks as not synchronized."""
    repo: OrderRepository = application.bot_data["repo"]
    context = SimpleNamespace(application=application, bot=application.bot)
    for order in repo.list_needing_sync(limit=SYNC_RECONCILIATION_BATCH_SIZE):
        try:
            _, success = await _sync_order(context, order.id)
            if not success:
                repo.mark_sync_attempted(
                    order.id,
                    expected_updated_at=getattr(order, "updated_at", None),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # One broken Telegram message/order must not starve later orders.
            repo.mark_sync_attempted(
                order.id,
                expected_updated_at=getattr(order, "updated_at", None),
            )
            logger.exception("Background reconciliation failed for order %s", order.id)
    try:
        await _process_cleanup_messages(context, limit=SYNC_RECONCILIATION_BATCH_SIZE)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Background cleanup reconciliation failed")


async def _delivery_sync_worker(application: Application) -> None:
    """Continuously retry interrupted Telegram publications."""
    # The first background pass after startup is a full repair. Startup itself
    # only validates permissions so polling is not blocked by hundreds of API
    # calls on installations with a long active-order list.
    cycles_since_full = FULL_RECONCILIATION_EVERY
    try:
        while True:
            _touch_health_signal(application)
            await asyncio.sleep(SYNC_RECONCILIATION_INTERVAL)

            preflight_validated = application.bot_data.get("delivery_preflight_validated", False)
            if not preflight_validated or cycles_since_full >= FULL_RECONCILIATION_EVERY:
                try:
                    # Besides validating the configured chats, the full pass
                    # also repairs old rows that predate the sync_needed flag.
                    await reconcile_orders_on_start(application)
                except RetryAfter as error:
                    retry_after = error.retry_after
                    delay = (
                        retry_after.total_seconds()
                        if hasattr(retry_after, "total_seconds")
                        else float(retry_after)
                    )
                    logger.warning(
                        "Telegram rate-limited delivery reconciliation for %.1f seconds",
                        delay,
                    )
                    await asyncio.sleep(max(1.0, delay))
                    continue
                except BadRequest:
                    # ``BadRequest`` inherits from ``NetworkError`` in PTB,
                    # but a confirmed "chat not found"/invalid ID is a fatal
                    # configuration error, not a transient outage.
                    logger.critical("Fatal delivery bot preflight error", exc_info=True)
                    application.stop_running()
                    return
                except NetworkError as error:
                    logger.warning(
                        "Telegram is temporarily unavailable during delivery reconciliation: %s",
                        error,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A successful Telegram response that proves a bad chat
                    # type or missing channel rights remains a fatal setup
                    # error, even when the first preflight was only timed out.
                    logger.critical("Fatal delivery bot preflight error", exc_info=True)
                    application.stop_running()
                    return
                application.bot_data["delivery_preflight_validated"] = True
                cycles_since_full = 0
            else:
                await reconcile_pending_orders(application)
                cycles_since_full += 1
    finally:
        _touch_health_signal(application)


def _start_delivery_sync_worker(application: Application) -> None:
    existing = application.bot_data.get(SYNC_WORKER_TASK_KEY)
    if existing and not existing.done():
        return
    task = asyncio.create_task(
        _delivery_sync_worker(application),
        name="delivery-sync-worker",
    )
    application.bot_data[SYNC_WORKER_TASK_KEY] = task

    def worker_finished(finished: asyncio.Task) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            logger.critical(
                "Delivery sync worker stopped unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )
            application.stop_running()

    task.add_done_callback(worker_finished)


async def initialize_delivery_runtime(application: Application) -> None:
    """Run startup checks without turning a temporary outage into a crash loop."""
    try:
        await validate_delivery_configuration(application)
    except BadRequest:
        # Keep this before NetworkError: PTB models Telegram's confirmed
        # BadRequest responses as a NetworkError subclass.
        raise
    except RetryAfter as error:
        application.bot_data["delivery_preflight_validated"] = False
        logger.warning(
            "Telegram rate-limited startup preflight for %s seconds; polling will start",
            error.retry_after,
        )
    except NetworkError as error:
        application.bot_data["delivery_preflight_validated"] = False
        logger.warning(
            "Telegram startup preflight timed out; polling will start and retry in background: %s",
            error,
        )
    else:
        application.bot_data["delivery_preflight_validated"] = True
    _start_delivery_sync_worker(application)


async def shutdown_delivery_runtime(application: Application) -> None:
    task = application.bot_data.pop(SYNC_WORKER_TASK_KEY, None)
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def build_application(settings: Settings) -> Application:
    repo = OrderRepository(settings.database_path)
    repo.initialize()
    routing_service = RoutingService(
        settings.database_path.with_name("routing-cache.db")
    )
    persistence = AtomicPicklePersistence(
        filepath=settings.database_path.with_name("delivery-state.pickle"),
        store_data=PersistenceInput(
            bot_data=False,
            chat_data=False,
            user_data=True,
            callback_data=False,
        ),
        update_interval=PERSISTENCE_UPDATE_INTERVAL,
    )
    application = (
        Application.builder()
        .token(settings.bot_token)
        .persistence(persistence)
        .post_init(initialize_delivery_runtime)
        .post_shutdown(shutdown_delivery_runtime)
        .build()
    )
    application.bot_data.update(
        settings=settings,
        repo=repo,
        routing_service=routing_service,
    )
    register_handlers(application)
    application.add_error_handler(error_handler)
    return application
