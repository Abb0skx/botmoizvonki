from __future__ import annotations

import asyncio
import copy
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from price_server.config import PriceSettings
from price_server.quick_links import (
    CATALOG_QUICK_POST_KEY,
    QUICK_LINK_POST_SPECS,
)
from price_server.repository import PriceRepository
from price_server.scheduler import PriceScheduler
from price_server.service import PricePublicationService
from price_server.telegram_api import TelegramMessage


MAIN_CHANNEL_ID = "-1001463992448"
CONTROL_CHANNEL_ID = "-1003922029862"


class RetryableExternalError(RuntimeError):
    retryable = True
    ambiguous = False
    retry_after = 0


class PermanentExternalError(RuntimeError):
    retryable = False
    ambiguous = False
    retry_after = None


class AmbiguousExternalError(RuntimeError):
    retryable = False
    ambiguous = True
    retry_after = None


class FakeTelegram:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.next_id = 7000
        self.updates: list[dict] = []
        self.get_updates_calls: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[str, int, str]] = []
        self.deleted: list[tuple[str, int]] = []
        self.delete_attempts: list[tuple[str, int]] = []
        self.edit_error: Exception | None = None
        self.send_error: Exception | None = None
        self.delete_errors: dict[tuple[str, int], Exception] = {}

    @staticmethod
    def _message(
        chat_id: str,
        message_id: int,
        html_text: str,
    ) -> TelegramMessage:
        return TelegramMessage(
            message_id=int(message_id),
            chat_id=str(chat_id),
            post_url=f"https://t.me/testchannel/{int(message_id)}",
            html=html_text,
            content_hash=hashlib.sha256(html_text.encode()).hexdigest(),
        )

    def get_updates(self, **kwargs):
        self.get_updates_calls.append(dict(kwargs))
        updates, self.updates = self.updates, []
        return updates

    def edit_message(self, chat_id, message_id, html_text, **_kwargs):
        key = (str(chat_id), int(message_id))
        self.events.append(("main_edit_attempt", *key, html_text))
        if self.edit_error is not None:
            raise self.edit_error
        self.edited.append((*key, html_text))
        self.events.append(("main_edited", *key))
        return self._message(*key, html_text)

    def send_message(self, chat_id, html_text, **_kwargs):
        self.events.append(("confirmation_attempt", str(chat_id), html_text))
        if self.send_error is not None:
            raise self.send_error
        self.next_id += 1
        self.sent.append((str(chat_id), html_text))
        self.events.append(("confirmation_sent", str(chat_id), self.next_id))
        return self._message(str(chat_id), self.next_id, html_text)

    def delete_message(self, chat_id, message_id):
        key = (str(chat_id), int(message_id))
        self.delete_attempts.append(key)
        self.events.append(("source_delete_attempt", *key))
        error = self.delete_errors.get(key)
        if error is not None:
            raise error
        self.deleted.append(key)
        self.events.append(("source_deleted", *key))
        return True


class FakeBotSettingsRegistry:
    def __init__(
        self,
        events: list[tuple],
        failures: list[Exception] | None = None,
    ) -> None:
        self.events = events
        self.failures = list(failures or [])
        self.calls: list[tuple[int, datetime]] = []

    def update_exchange_rate(self, rate: int, updated_at: datetime):
        self.calls.append((int(rate), updated_at))
        self.events.append(("sheet_update_attempt", int(rate)))
        if self.failures:
            raise self.failures.pop(0)
        self.events.append(("sheet_updated", int(rate)))
        return {"setting": "kurs", "value": int(rate)}


class ExchangeRateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.events: list[tuple] = []
        self.settings = PriceSettings(
            enabled=True,
            db_path=Path(self.temp.name) / "price.db",
            legacy_html_path=Path(self.temp.name) / "price.html",
            admin_username="admin",
            admin_password="secret",
            sync_api_key="sync",
            telegram_bot_token="fake-token",
            telegram_channel_id=MAIN_CHANNEL_ID,
            telegram_channel_username="testchannel",
            product_sort_sheet_id="product-sort-sheet",
            posts_sheet_name="Telegram Posts",
            timezone="Asia/Tashkent",
            scheduler_poll_seconds=1,
            sync_max_bytes=2_000_000,
            telegram_preview_channel_id=CONTROL_CHANNEL_ID,
            bot_settings_sheet_id="bot-settings-sheet",
            bot_settings_sheet_name="bot_settings",
        )
        self.repository = PriceRepository(self.settings)
        self.telegram = FakeTelegram(self.events)
        self.service = PricePublicationService(
            self.settings,
            self.repository,
            telegram=self.telegram,
        )
        self.assertEqual(self.service.ensure_quick_link_registry(), 9)
        initial_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        self.assertEqual(
            self.service.refresh_quick_link_posts(initial_refresh_at),
            9,
        )
        self.events.clear()
        self.telegram.edited.clear()
        self.now = datetime.now(timezone.utc) + timedelta(seconds=10)
        self.sheet = FakeBotSettingsRegistry(self.events)
        self.service._bot_settings_registry = self.sheet

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _channel_post(
        update_id: int,
        message_id: int,
        text: str,
        *,
        chat_id: str = CONTROL_CHANNEL_ID,
        from_bot: bool = False,
        field: str = "channel_post",
        via_bot: bool = False,
    ) -> dict:
        message: dict = {
            "message_id": int(message_id),
            "chat": {"id": int(chat_id)},
            "text": text,
        }
        if from_bot:
            message["from"] = {"id": 123, "is_bot": True}
        if via_bot:
            message["via_bot"] = {"id": 456, "is_bot": True}
        return {"update_id": int(update_id), field: message}

    def _submit(self, rate: int, message_id: int, update_id: int) -> None:
        self.telegram.updates = [
            self._channel_post(update_id, message_id, str(rate))
        ]
        self.assertEqual(self.service.poll_preview_updates(), 1)

    def _queue_and_apply_main(self, at: datetime) -> None:
        self.assertEqual(self.service.process_exchange_rate_updates(at), 1)
        self.assertEqual(self.service.refresh_quick_link_posts(at), 1)

    def test_parser_is_control_channel_only_and_request_is_idempotent(self):
        self.telegram.updates = [
            self._channel_post(10, 100, "11900", chat_id=MAIN_CHANNEL_ID),
            self._channel_post(11, 101, "11 900"),
            self._channel_post(12, 102, "11900", from_bot=True),
            self._channel_post(13, 103, "11900", via_bot=True),
            self._channel_post(
                14, 104, "11900", field="edited_channel_post"
            ),
            self._channel_post(15, 105, "4999"),
            self._channel_post(16, 106, "50001"),
            self._channel_post(17, 107, " 11900\n"),
        ]

        self.assertEqual(self.service.poll_preview_updates(), 8)
        requests = self.repository.list_exchange_rate_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["source_update_id"], 17)
        self.assertEqual(requests[0]["source_message_id"], 107)
        self.assertEqual(requests[0]["rate"], 11_900)
        self.assertEqual(requests[0]["formatted_rate"], "11 900")
        self.assertEqual(
            self.repository.get_runtime_state("telegram_update_offset"),
            "18",
        )

        # Replaying a persisted update must not enqueue a second command.
        self.repository.set_runtime_state("telegram_update_offset", "0")
        self.telegram.updates = [self._channel_post(17, 107, "11900")]
        self.assertEqual(self.service.poll_preview_updates(), 1)
        self.assertEqual(
            len(self.repository.list_exchange_rate_requests()),
            1,
        )

    def test_full_success_orders_main_sheet_confirmation_then_source_delete(self):
        self._submit(11_900, message_id=201, update_id=20)

        self._queue_and_apply_main(self.now)
        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            1,
        )

        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "done", "completed",
        ))
        self.assertEqual(
            [event[0] for event in self.events],
            [
                "main_edit_attempt",
                "main_edited",
                "sheet_update_attempt",
                "sheet_updated",
                "confirmation_attempt",
                "confirmation_sent",
                "source_delete_attempt",
                "source_deleted",
            ],
        )
        self.assertIn("<b>Курс:</b> 1 $ = 11 900 сум", self.telegram.edited[0][2])
        self.assertEqual(self.sheet.calls[0][0], 11_900)
        self.assertEqual(self.telegram.sent, [
            (CONTROL_CHANNEL_ID, "✅ Курс изменён: 1 $ = 11 900 сум")
        ])
        self.assertEqual(self.telegram.deleted, [(CONTROL_CHANNEL_ID, 201)])

    def test_scheduler_cycle_runs_the_complete_order_without_real_sheets(self):
        self.telegram.updates = [
            self._channel_post(25, 251, "11900")
        ]
        self.service.sync_sheets_outbox = lambda **_kwargs: 0
        scheduler = PriceScheduler(
            self.settings,
            self.repository,
            self.service,
            clock=lambda: self.now,
        )

        self.assertGreaterEqual(asyncio.run(scheduler.run_once()), 1)

        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "done", "completed",
        ))
        self.assertEqual(self.sheet.calls[0][0], 11_900)
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.telegram.deleted, [(CONTROL_CHANNEL_ID, 251)])

    def test_main_edit_failure_blocks_sheet_confirmation_and_delete(self):
        self._submit(11_900, message_id=301, update_id=30)
        self.assertEqual(self.service.process_exchange_rate_updates(self.now), 1)
        self.telegram.edit_error = PermanentExternalError("edit forbidden")

        self.assertEqual(self.service.refresh_quick_link_posts(self.now), 0)
        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            1,
        )

        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual(request["status"], "failed")
        self.assertEqual(request["phase"], "main_queued")
        self.assertEqual(self.sheet.calls, [])
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.delete_attempts, [])

    def test_sheet_failure_blocks_confirmation_and_delete_then_retries(self):
        self.sheet.failures = [RetryableExternalError("temporary Sheets error")]
        self._submit(11_900, message_id=401, update_id=40)
        self._queue_and_apply_main(self.now)

        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            0,
        )
        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "pending", "main_applied",
        ))
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.delete_attempts, [])

        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=20)
            ),
            1,
        )
        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "done", "completed",
        ))
        self.assertEqual([call[0] for call in self.sheet.calls], [11_900, 11_900])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.telegram.deleted, [(CONTROL_CHANNEL_ID, 401)])
        self.assertEqual(
            sum(event[0] == "main_edited" for event in self.events),
            1,
        )

    def test_fixed_sheet_schema_error_can_recover_without_another_main_edit(self):
        self.sheet.failures = [PermanentExternalError("kurs row missing")]
        self._submit(11_900, message_id=451, update_id=45)
        self._queue_and_apply_main(self.now)

        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            0,
        )
        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "pending", "main_applied",
        ))

        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=20)
            ),
            1,
        )
        self.assertEqual(
            self.repository.list_exchange_rate_requests()[0]["status"],
            "done",
        )
        self.assertEqual(len(self.telegram.edited), 1)

    def test_ambiguous_confirmation_needs_review_and_never_deletes_source(self):
        self._submit(11_900, message_id=501, update_id=50)
        self._queue_and_apply_main(self.now)
        self.telegram.send_error = AmbiguousExternalError(
            "sendMessage outcome unknown"
        )

        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            0,
        )

        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "needs_review", "confirmation_inflight",
        ))
        self.assertEqual(len(self.sheet.calls), 1)
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.delete_attempts, [])
        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(minutes=10)
            ),
            0,
        )

    def test_restart_after_confirmation_intent_needs_review_without_resend(self):
        self._submit(11_900, message_id=551, update_id=55)
        self._queue_and_apply_main(self.now)

        claimed = self.repository.claim_exchange_rate_request(
            self.now + timedelta(seconds=1)
        )
        self.assertIsNotNone(claimed)
        request_id = int(claimed["request_id"])
        token = str(claimed["lease_token"])
        self.assertEqual(
            self.repository.observe_exchange_rate_catalog_update(
                request_id,
                token,
                self.now + timedelta(seconds=1),
            ),
            "applied",
        )
        self.sheet.update_exchange_rate(
            int(claimed["rate"]),
            datetime.fromisoformat(str(claimed["requested_at"])),
        )
        self.assertTrue(self.repository.mark_exchange_rate_sheet_updated(
            request_id,
            token,
            self.now + timedelta(seconds=1),
        ))
        self.assertTrue(
            self.repository.mark_exchange_rate_confirmation_inflight(
                request_id,
                token,
                self.now + timedelta(seconds=1),
            )
        )

        # A process crash here has an unknown sendMessage outcome. A reopened
        # worker must fence the command instead of sending a duplicate.
        reopened = PriceRepository(self.settings)
        self.assertIsNone(reopened.claim_exchange_rate_request(
            self.now + timedelta(minutes=4)
        ))
        request = reopened.get_exchange_rate_request(request_id)
        self.assertEqual((request["status"], request["phase"]), (
            "needs_review", "confirmation_inflight",
        ))
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.delete_attempts, [])

    def test_delete_is_retried_without_repeating_sheet_or_confirmation(self):
        source = (CONTROL_CHANNEL_ID, 601)
        self.telegram.delete_errors[source] = RetryableExternalError(
            "temporary delete failure"
        )
        self._submit(11_900, message_id=source[1], update_id=60)
        self._queue_and_apply_main(self.now)

        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            0,
        )
        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "pending", "confirmed",
        ))
        self.assertEqual(len(self.sheet.calls), 1)
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.telegram.delete_attempts, [source])

        del self.telegram.delete_errors[source]
        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=20)
            ),
            1,
        )
        request = self.repository.list_exchange_rate_requests()[0]
        self.assertEqual((request["status"], request["phase"]), (
            "done", "completed",
        ))
        self.assertEqual(len(self.sheet.calls), 1)
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.telegram.delete_attempts, [source, source])
        self.assertEqual(self.telegram.deleted, [source])

    def test_context_migration_adds_rate_and_preserves_date_and_custom_rate(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = copy.copy(self.settings)
        object.__setattr__(settings, "db_path", Path(temp.name) / "migration.db")
        repository = PriceRepository(settings)
        old_specs = copy.deepcopy(QUICK_LINK_POST_SPECS)
        for spec in old_specs:
            if spec["quick_post_key"] != CATALOG_QUICK_POST_KEY:
                continue
            spec["template_html"] = spec["template_html"].replace(
                "{{context:exchange_rate}}", "11 930"
            )
            spec["initial_context"] = {"catalog_date": "27.08.2026"}
        self.assertEqual(repository.ensure_quick_link_posts(
            old_specs,
            channel_id=MAIN_CHANNEL_ID,
            channel_username="testchannel",
            enqueue_initial=False,
        ), 9)

        service = PricePublicationService(
            settings,
            repository,
            telegram=FakeTelegram([]),
        )
        self.assertEqual(service.ensure_quick_link_registry(), 1)
        context = repository.resolve_quick_link_post(
            CATALOG_QUICK_POST_KEY
        )["context"]
        self.assertEqual(context, {
            "catalog_date": "27.08.2026",
            "exchange_rate": "11 900",
        })

        request = repository.record_exchange_rate_request(
            source_update_id=70,
            source_channel_id=CONTROL_CHANNEL_ID,
            source_message_id=701,
            rate=11_900,
            now=self.now,
        )
        claimed = repository.claim_exchange_rate_request(self.now)
        self.assertEqual(claimed["request_id"], request["request_id"])
        self.assertTrue(repository.prepare_exchange_rate_catalog_update(
            request["request_id"],
            claimed["lease_token"],
            self.now,
        ))
        self.assertEqual(service.ensure_quick_link_registry(), 0)
        context = repository.resolve_quick_link_post(
            CATALOG_QUICK_POST_KEY
        )["context"]
        self.assertEqual(context, {
            "catalog_date": "27.08.2026",
            "exchange_rate": "11 900",
        })

    def test_requests_are_applied_fifo(self):
        self.telegram.updates = [
            self._channel_post(80, 801, "11900"),
            self._channel_post(81, 802, "12000"),
        ]
        self.assertEqual(self.service.poll_preview_updates(), 2)

        self._queue_and_apply_main(self.now)
        self.assertEqual(
            self.service.process_exchange_rate_updates(
                self.now + timedelta(seconds=1)
            ),
            1,
        )
        self.assertEqual([call[0] for call in self.sheet.calls], [11_900])

        second_at = self.now + timedelta(seconds=2)
        self.assertEqual(
            self.service.process_exchange_rate_updates(second_at),
            1,
        )
        self.assertEqual(self.service.refresh_quick_link_posts(second_at), 1)
        self.assertEqual(
            self.service.process_exchange_rate_updates(
                second_at + timedelta(seconds=1)
            ),
            1,
        )

        self.assertEqual([call[0] for call in self.sheet.calls], [11_900, 12_000])
        self.assertEqual(
            [message for _chat, message in self.telegram.sent],
            [
                "✅ Курс изменён: 1 $ = 11 900 сум",
                "✅ Курс изменён: 1 $ = 12 000 сум",
            ],
        )
        self.assertEqual(
            self.telegram.deleted,
            [(CONTROL_CHANNEL_ID, 801), (CONTROL_CHANNEL_ID, 802)],
        )
        requests = sorted(
            self.repository.list_exchange_rate_requests(),
            key=lambda item: item["request_id"],
        )
        self.assertEqual(
            [(item["rate"], item["status"]) for item in requests],
            [(11_900, "done"), (12_000, "done")],
        )


if __name__ == "__main__":
    unittest.main()
