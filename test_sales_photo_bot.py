from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from telegram.error import NetworkError, RetryAfter

from sales_photo_bot.application import _prepare_polling, build_application
from sales_photo_bot.config import ConfigError, Settings
from sales_photo_bot.formatting import (
    add_manager_selection,
    build_caption,
    remove_manager_selection,
    selected_manager_from_caption,
)
from sales_photo_bot.keyboards import manager_keyboard
from sales_photo_bot.models import ProductIdentifiers
from sales_photo_bot.repository import SalesPhotoRepository, utc_now
from sales_photo_bot.service import BOT_CARD_MARKER, SalesPhotoService


CHAT_ID = -1001234567890
BOT_ID = 777
TOKEN = "1234567890:" + "A" * 35


class StaticRecognizer:
    def __init__(
        self,
        result: ProductIdentifiers | None = None,
        error: Exception | None = None,
    ):
        self.result = result or ProductIdentifiers()
        self.error = error
        self.calls = 0

    async def recognize(
        self, image_bytes: bytes, mime_type: str
    ) -> ProductIdentifiers:
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def settings(tmp: Path, allowed: frozenset[int] = frozenset()) -> Settings:
    return Settings(
        bot_token=TOKEN,
        chat_id=CHAT_ID,
        db_path=tmp / "sales.db",
        heartbeat_path=tmp / "heartbeat",
        allowed_user_ids=allowed,
        delete_retry_seconds=1,
    )


def photo_message(
    message_id: int = 10,
    caption: str | None = "+998 90 123 45 67",
    sender_id: int = 50,
):
    return SimpleNamespace(
        chat_id=CHAT_ID,
        chat=SimpleNamespace(id=CHAT_ID),
        message_id=message_id,
        message_thread_id=None,
        caption=caption,
        photo=(
            SimpleNamespace(
                file_id="small",
                file_unique_id="unique-small",
                file_size=100,
                width=100,
                height=100,
            ),
            SimpleNamespace(
                file_id="large",
                file_unique_id="unique-large",
                file_size=500,
                width=1000,
                height=1000,
            ),
        ),
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
    )


def telegram_bot(events: list[str] | None = None):
    events = events if events is not None else []
    bot = SimpleNamespace()
    telegram_file = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"jpeg"))
    )
    bot.get_file = AsyncMock(return_value=telegram_file)

    async def send_photo(**kwargs):
        events.append("send")
        return SimpleNamespace(message_id=200)

    async def delete_message(chat_id, message_id):
        events.append(f"delete:{message_id}")
        return True

    bot.send_photo = AsyncMock(side_effect=send_photo)
    bot.delete_message = AsyncMock(side_effect=delete_message)
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="administrator")
    )
    return bot


class ConfigTests(unittest.TestCase):
    def test_settings_parse_and_hide_bot_token(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Settings.from_env(
                {
                    "SALES_PHOTO_BOT_TOKEN": TOKEN,
                    "SALES_PHOTO_CHAT_ID": str(CHAT_ID),
                    "SALES_PHOTO_DB_PATH": str(Path(directory) / "db.sqlite"),
                    "SALES_PHOTO_ALLOWED_USER_IDS": "1,2,2",
                }
            )
        self.assertEqual(parsed.allowed_user_ids, frozenset({1, 2}))
        self.assertNotIn(TOKEN, repr(parsed))
        self.assertEqual(parsed.heartbeat_path, Path("/tmp/sales-photo-heartbeat"))

    def test_settings_require_negative_chat_id_and_no_external_api_keys(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(
                {
                    "SALES_PHOTO_BOT_TOKEN": TOKEN,
                    "SALES_PHOTO_CHAT_ID": "123",
                }
            )
        parsed = Settings.from_env(
            {
                "SALES_PHOTO_BOT_TOKEN": TOKEN,
                "SALES_PHOTO_CHAT_ID": str(CHAT_ID),
            }
        )
        self.assertEqual(parsed.ocr_timeout_seconds, 30)


class CaptionFormattingTests(unittest.TestCase):
    def test_complete_card(self):
        caption = build_caption(
            "+998 90 123 45 67",
            ProductIdentifiers(
                imei="490154203237518",
                imei2="352099001761481",
                serial_number="R8YL50R510N",
            ),
        )
        self.assertEqual(
            caption,
            "📞 Клиент: +998 90 123 45 67\n"
            "\n🛒💵:\n"
            "rasxod:\n\n"
            "<blockquote>IMEI: 490154203237518</blockquote>\n"
            "<blockquote>IMEI2: 352099001761481</blockquote>\n"
            "<blockquote>S/N: R8YL50R510N</blockquote>\n\n"
            "<b>Наличка</b>\n"
            "💵:\n"
            "🇺🇿:\n\n"
            "<b>Card/Terminal/Paynet</b>\n"
            "💵:\n"
            "🇺🇿:",
        )

    def test_missing_identifiers_are_omitted(self):
        caption = build_caption(None, ProductIdentifiers())
        self.assertNotIn("IMEI", caption)
        self.assertNotIn("S/N", caption)
        self.assertTrue(caption.startswith("🛒💵:"))

    def test_product_model_fields_are_never_rendered(self):
        caption = build_caption(None, ProductIdentifiers(serial_number="ABC12345"))
        self.assertNotIn("SM-X133", caption)
        self.assertNotIn("📦", caption)
        self.assertNotIn("💾", caption)
        self.assertNotIn("🎨", caption)

    def test_external_values_are_escaped_and_bounded(self):
        caption = build_caption(
            "<b>client</b> & " + "x" * 500,
            ProductIdentifiers(serial_number="ABC<123>"),
        )
        self.assertIn("&lt;b&gt;client&lt;/b&gt; &amp;", caption)
        self.assertIn("ABC&lt;123&gt;", caption)
        self.assertLess(len(caption), 1024)

    def test_identifier_quotes_do_not_nest_telegram_entities(self):
        caption = build_caption(
            None,
            ProductIdentifiers(imei="490154203237518", serial_number="ABC12345"),
        )
        self.assertIn("<blockquote>IMEI: 490154203237518</blockquote>", caption)
        self.assertNotIn("<code>", caption)

    def test_manager_can_be_added_detected_and_removed(self):
        base = build_caption(None, ProductIdentifiers(serial_number="ABC12345"))
        selected = add_manager_selection(base, "Olmas")
        self.assertEqual(selected_manager_from_caption(selected), "Olmas")
        self.assertEqual(remove_manager_selection(selected), base)


class RepositoryTests(unittest.TestCase):
    def test_obsolete_model_cache_is_removed_during_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE sales_photo_name_cache "
                    "(cache_key TEXT PRIMARY KEY, model_name TEXT)"
                )
                db.execute(
                    "INSERT INTO sales_photo_name_cache VALUES (?, ?)",
                    ("old-key", "Old Product Name"),
                )
                db.commit()
            SalesPhotoRepository(path)
            with sqlite3.connect(path) as db:
                row = db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='sales_photo_name_cache'"
                ).fetchone()
            self.assertIsNone(row)

    def test_job_manager_and_bootstrap_state_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repo = SalesPhotoRepository(path)
            self.assertFalse(repo.is_bootstrapped(BOT_ID, CHAT_ID))
            self.assertFalse(repo.is_bootstrapped(BOT_ID + 1, CHAT_ID))
            self.assertFalse(repo.is_bootstrapped(BOT_ID, CHAT_ID - 1))
            repo.mark_bootstrapped(BOT_ID, CHAT_ID)
            self.assertTrue(repo.claim_photo(CHAT_ID, 10, "file"))
            self.assertFalse(repo.claim_photo(CHAT_ID, 10, "file"))
            repo.mark_reposted(CHAT_ID, 10, 20)
            self.assertTrue(repo.is_replacement(CHAT_ID, 20))
            self.assertTrue(repo.set_manager(CHAT_ID, 20, "Ali"))
            self.assertEqual(repo.selected_manager(CHAT_ID, 20), "Ali")
            self.assertTrue(repo.clear_manager(CHAT_ID, 20, "Ali"))
            repo.mark_complete(CHAT_ID, 10)
            reopened = SalesPhotoRepository(path)
            self.assertTrue(reopened.is_bootstrapped(BOT_ID, CHAT_ID))
            self.assertTrue(reopened.is_replacement(CHAT_ID, 20))
            self.assertEqual(reopened.pending_deletions(CHAT_ID), ())

    def test_replacement_reconciliation_and_failed_job_reclaim(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            self.assertTrue(repo.claim_photo(CHAT_ID, 10, "file"))
            repo.mark_failed(CHAT_ID, 10, "NetworkError")
            self.assertTrue(repo.claim_photo(CHAT_ID, 10, "file"))
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200), "recorded")
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200), "same")
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 201), "conflict")
            self.assertEqual(
                [
                    job.message_id
                    for job in repo.pending_duplicate_cleanups(CHAT_ID)
                ],
                [201],
            )

    def test_callback_generation_is_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            repo.claim_photo(CHAT_ID, 10, "file")
            repo.mark_reposted(CHAT_ID, 10, 200)
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 0)
            self.assertTrue(repo.apply_manager_selection(CHAT_ID, 200, "Ali", 0))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 1)
            self.assertFalse(repo.apply_manager_selection(CHAT_ID, 200, "Abbos", 0))
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Ali")
            self.assertTrue(repo.apply_manager_clear(CHAT_ID, 200, 1))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 2)
            self.assertIsNone(repo.selected_manager(CHAT_ID, 200))

    def test_callback_transition_reservation_is_atomic_and_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            repo.claim_photo(CHAT_ID, 10, "file")
            repo.mark_reposted(CHAT_ID, 10, 200)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertFalse(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 1)
            self.assertTrue(repo.release_ui_transition(CHAT_ID, 200, 0))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 200), 0)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Ali")
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 1))
            self.assertTrue(repo.commit_reserved_manager_clear(CHAT_ID, 200, 1))
            self.assertIsNone(repo.selected_manager(CHAT_ID, 200))

    def test_missing_or_wrong_repository_key_fails_closed(self):
        for replacement in (None, Fernet.generate_key()):
            with self.subTest(replacement=bool(replacement)):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "sales.db"
                    repo = SalesPhotoRepository(path)
                    repo.claim_photo(
                        CHAT_ID,
                        10,
                        "file",
                        source_file_id="telegram-file-id",
                    )
                    key_path = path.with_name("sales.db.key")
                    if replacement is None:
                        key_path.unlink()
                    else:
                        key_path.write_bytes(replacement)
                    with self.assertRaisesRegex(RuntimeError, "ключ|Ключ"):
                        SalesPhotoRepository(path)

    def test_legacy_database_without_key_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            with sqlite3.connect(path) as db:
                db.executescript(
                    """
                    CREATE TABLE sales_photo_jobs (
                        chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        source_file_unique_id TEXT NOT NULL,
                        replacement_message_id INTEGER,
                        status TEXT NOT NULL,
                        delete_attempts INTEGER NOT NULL DEFAULT 0,
                        last_error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(chat_id, source_message_id),
                        UNIQUE(chat_id, replacement_message_id)
                    );
                    INSERT INTO sales_photo_jobs(
                        chat_id,source_message_id,source_file_unique_id,
                        replacement_message_id,status,created_at,updated_at
                    ) VALUES(-1001234567890,10,'legacy',20,'complete','now','now');
                    """
                )
            repo = SalesPhotoRepository(path)
            self.assertTrue(repo.is_replacement(CHAT_ID, 20))
            self.assertEqual(repo.ui_generation_for_replacement(CHAT_ID, 20), 0)

    def test_corrupt_retry_payload_does_not_hide_later_valid_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            old = utc_now() - timedelta(minutes=5)
            for source_id in (10, 11):
                repo.claim_photo(
                    CHAT_ID,
                    source_id,
                    f"unique-{source_id}",
                    source_file_id=f"file-{source_id}",
                    at=old,
                )
                repo.mark_failed(CHAT_ID, source_id, "temporary", at=old)
            with sqlite3.connect(repo.path) as db:
                db.execute(
                    "UPDATE sales_photo_jobs SET encrypted_payload=? "
                    "WHERE chat_id=? AND source_message_id=10",
                    (b"corrupt", CHAT_ID),
                )
                db.commit()
            pending = repo.retryable_photos(CHAT_ID)
            self.assertEqual([job.source_message_id for job in pending], [11])
            with sqlite3.connect(repo.path) as db:
                payload, attempts, error = db.execute(
                    "SELECT encrypted_payload,processing_attempts,last_error_code "
                    "FROM sales_photo_jobs WHERE chat_id=? AND source_message_id=10",
                    (CHAT_ID,),
                ).fetchone()
            self.assertEqual(payload, b"corrupt")
            self.assertEqual(attempts, 3)
            self.assertEqual(error, "retry_payload_invalid")

    def test_maintenance_queries_are_scoped_to_the_configured_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            other_chat = CHAT_ID - 1
            old = utc_now() - timedelta(minutes=5)
            for chat_id in (CHAT_ID, other_chat):
                repo.claim_photo(
                    chat_id,
                    10,
                    f"file-{chat_id}",
                    source_file_id=f"telegram-{chat_id}",
                    at=old,
                )
                repo.mark_failed(chat_id, 10, "temporary", at=old)
                repo.queue_duplicate_cleanup(chat_id, 500, at=old)
            self.assertEqual(
                {job.chat_id for job in repo.retryable_photos(CHAT_ID)},
                {CHAT_ID},
            )
            self.assertEqual(
                {job.chat_id for job in repo.pending_duplicate_cleanups(CHAT_ID)},
                {CHAT_ID},
            )

    def test_retry_payload_is_encrypted_durable_and_cleared_after_repost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repo = SalesPhotoRepository(path)
            secret_file_id = "telegram-secret-file-id"
            private_caption = "private client 998901234567"
            old = utc_now() - timedelta(minutes=5)
            self.assertTrue(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-file",
                    source_file_id=secret_file_id,
                    client_caption=private_caption,
                    message_thread_id=77,
                    at=old,
                )
            )
            repo.mark_failed(CHAT_ID, 10, "crash", at=old)

            stored_bytes = b"".join(
                candidate.read_bytes()
                for candidate in Path(directory).glob("sales.db*")
                if candidate.is_file()
            )
            self.assertNotIn(secret_file_id.encode(), stored_bytes)
            self.assertNotIn(private_caption.encode(), stored_bytes)
            self.assertEqual(
                path.with_name("sales.db.key").stat().st_mode & 0o777,
                0o600,
            )

            pending = repo.retryable_photos(CHAT_ID)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].source_file_id, secret_file_id)
            self.assertEqual(pending[0].client_caption, private_caption)
            self.assertEqual(pending[0].message_thread_id, 77)
            self.assertTrue(repo.claim_retry(CHAT_ID, 10, pending[0].attempts))
            self.assertEqual(repo.record_replacement(CHAT_ID, 10, 200), "recorded")
            with sqlite3.connect(path) as db:
                payload = db.execute(
                    "SELECT encrypted_payload FROM sales_photo_jobs "
                    "WHERE chat_id=? AND source_message_id=?",
                    (CHAT_ID, 10),
                ).fetchone()[0]
            self.assertIsNone(payload)

    def test_photo_retry_is_bounded_to_three_processing_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            old = utc_now() - timedelta(minutes=5)
            self.assertTrue(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-file",
                    source_file_id="file-id",
                    at=old,
                )
            )
            repo.mark_failed(CHAT_ID, 10, "first", at=old)
            self.assertTrue(repo.claim_retry(CHAT_ID, 10, 1, at=old))
            repo.mark_failed(CHAT_ID, 10, "second", at=old)
            self.assertTrue(repo.claim_retry(CHAT_ID, 10, 2, at=old))
            repo.mark_failed(CHAT_ID, 10, "third", at=old)
            self.assertEqual(repo.retryable_photos(CHAT_ID), ())
            with sqlite3.connect(repo.path) as db:
                payload = db.execute(
                    "SELECT encrypted_payload FROM sales_photo_jobs "
                    "WHERE chat_id=? AND source_message_id=?",
                    (CHAT_ID, 10),
                ).fetchone()[0]
            self.assertIsNone(payload)
            self.assertFalse(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-file",
                    source_file_id="file-id",
                )
            )


class PhotoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_send_happens_before_original_delete_and_largest_photo_is_used(self):
        events: list[str] = []
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        recognizer = StaticRecognizer(
            ProductIdentifiers(
                imei="490154203237518",
                serial_number="R8YL50R510N",
            )
        )
        service = SalesPhotoService(settings(self.root), repo, recognizer)
        bot = telegram_bot(events)
        await service.handle_photo(photo_message(), bot)
        self.assertEqual(events, ["send", "delete:10"])
        bot.get_file.assert_awaited_once_with("large")
        sent = bot.send_photo.await_args.kwargs
        self.assertEqual(sent["photo"], "large")
        self.assertTrue(sent["caption"].startswith(BOT_CARD_MARKER))
        self.assertIn("<blockquote>IMEI:", sent["caption"])
        self.assertIn("<blockquote>S/N:", sent["caption"])
        self.assertNotIn("📦", sent["caption"])
        self.assertEqual(len(sent["reply_markup"].inline_keyboard), 2)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))

    async def test_failed_ocr_still_reposts_template_without_identifiers(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(
            settings(self.root), repo, StaticRecognizer(error=TimeoutError())
        )
        bot = telegram_bot()
        await service.handle_photo(photo_message(caption=None), bot)
        caption = bot.send_photo.await_args.kwargs["caption"]
        self.assertNotIn("📦", caption)
        self.assertIn("🛒💵:", caption)
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_send_failure_never_deletes_original(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = RuntimeError("network")
        await service.handle_photo(photo_message(), bot)
        bot.delete_message.assert_not_awaited()

    async def test_ambiguous_ledger_failure_keeps_both_photos(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        with patch.object(
            repo,
            "record_replacement",
            side_effect=RuntimeError("ambiguous commit"),
        ):
            await service.handle_photo(photo_message(), bot)
        bot.send_photo.assert_awaited_once()
        bot.delete_message.assert_not_awaited()

    async def test_missing_ledger_job_keeps_the_new_card(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        with patch.object(repo, "record_replacement", return_value="missing"):
            await service.handle_photo(photo_message(), bot)
        bot.send_photo.assert_awaited_once()
        bot.delete_message.assert_not_awaited()

    async def test_delete_failure_is_persisted_for_retry(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.delete_message.side_effect = RuntimeError("no permission")
        await service.handle_photo(photo_message(), bot)
        pending = repo.pending_deletions(CHAT_ID)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_message_id, 10)

    async def test_stale_processing_job_is_quarantined_without_repost(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        old = utc_now() - timedelta(minutes=10)
        self.assertTrue(
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique-large",
                source_file_id="large",
                client_caption="client 42",
                at=old,
            )
        )
        self.assertEqual(
            repo.fail_stale_processing(
                CHAT_ID,
                utc_now() - timedelta(minutes=1),
                at=old,
            ),
            1,
        )
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        await service.retry_failed_photos(bot)
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))
        self.assertEqual(repo.retryable_photos(CHAT_ID), ())
        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        with sqlite3.connect(repo.path) as db:
            payload = db.execute(
                "SELECT encrypted_payload FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()[0]
        self.assertIsNone(payload)

    async def test_failed_duplicate_delete_is_durable_and_retried(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        repo.claim_photo(CHAT_ID, 10, "file")
        repo.mark_reposted(CHAT_ID, 10, 200)
        repo.mark_complete(CHAT_ID, 10)
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.delete_message.side_effect = RuntimeError("temporary")
        duplicate = photo_message(
            message_id=201,
            caption=BOT_CARD_MARKER + build_caption(None, ProductIdentifiers()),
        )
        duplicate.reply_markup = manager_keyboard(
            10,
            0,
            repo.callback_signature(CHAT_ID, 10, 0),
        )
        await service.handle_photo(duplicate, bot)
        pending = repo.pending_duplicate_cleanups(CHAT_ID)
        self.assertEqual(
            [(job.message_id, job.attempts) for job in pending],
            [(201, 1)],
        )

        repo.mark_duplicate_cleanup_failed(
            CHAT_ID,
            201,
            at=utc_now() - timedelta(minutes=10),
        )
        bot.delete_message.side_effect = None
        bot.delete_message.return_value = True
        await service.retry_duplicate_cleanups(bot)
        self.assertEqual(repo.pending_duplicate_cleanups(CHAT_ID), ())
        bot.delete_message.assert_awaited_with(CHAT_ID, 201)

    async def test_duplicate_and_marked_bot_repost_do_not_recurse(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        original = photo_message()
        await service.handle_photo(original, bot)
        await service.handle_photo(original, bot)
        replacement = photo_message(
            message_id=201,
            caption=BOT_CARD_MARKER + build_caption(None, ProductIdentifiers()),
        )
        replacement.from_user = None
        await service.handle_photo(replacement, bot)
        self.assertEqual(bot.send_photo.await_count, 1)

    async def test_marker_prevents_concurrent_repost_race_before_ledger_write(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        entered_send = asyncio.Event()
        release_send = asyncio.Event()

        async def slow_send(**kwargs):
            entered_send.set()
            await release_send.wait()
            return SimpleNamespace(message_id=200)

        bot.send_photo.side_effect = slow_send
        original_task = asyncio.create_task(service.handle_photo(photo_message(), bot))
        await entered_send.wait()
        replacement = photo_message(
            message_id=200,
            caption=BOT_CARD_MARKER + build_caption(None, ProductIdentifiers()),
        )
        replacement.from_user = None
        replacement.reply_markup = manager_keyboard(
            10,
            0,
            repo.callback_signature(CHAT_ID, 10, 0),
        )
        await service.handle_photo(replacement, bot)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))
        release_send.set()
        await original_task
        self.assertEqual(bot.send_photo.await_count, 1)

    async def test_forged_generated_marker_cannot_claim_a_job(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        repo.claim_photo(CHAT_ID, 10, "file", source_file_id="large")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        forged = photo_message(
            message_id=200,
            caption=BOT_CARD_MARKER + build_caption(None, ProductIdentifiers()),
        )
        forged.reply_markup = manager_keyboard(10, 0, "deadbeefdead")
        await service.handle_photo(forged, bot)
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))
        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_failed_photo_maintenance_processes_one_job_per_tick(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        old = utc_now() - timedelta(minutes=10)
        for source_id in (10, 11):
            repo.claim_photo(
                CHAT_ID,
                source_id,
                f"unique-{source_id}",
                source_file_id=f"file-{source_id}",
                at=old,
            )
            repo.mark_failed(CHAT_ID, source_id, "temporary", at=old)
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        await service.retry_failed_photos(bot)
        self.assertEqual(bot.send_photo.await_count, 1)

    async def test_ambiguous_network_send_is_not_retried(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = NetworkError("response lost")
        await service.handle_photo(photo_message(), bot)
        self.assertEqual(bot.send_photo.await_count, 1)
        self.assertEqual(repo.retryable_photos(CHAT_ID), ())
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))
        bot.delete_message.assert_not_awaited()

    async def test_retry_after_is_safely_retried_once(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = [
            RetryAfter(0.1),
            SimpleNamespace(message_id=200),
        ]
        with patch("sales_photo_bot.service.asyncio.sleep", new=AsyncMock()):
            await service.handle_photo(photo_message(), bot)
        self.assertEqual(bot.send_photo.await_count, 2)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_other_chat_is_ignored(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        message = photo_message()
        message.chat_id = -1001
        message.chat.id = -1001
        await service.handle_photo(message, bot)
        bot.send_photo.assert_not_awaited()

    async def test_preflight_requires_channel_and_admin_rights(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=777)),
            get_chat=AsyncMock(return_value=SimpleNamespace(id=CHAT_ID, type="supergroup")),
            get_chat_member=AsyncMock(),
        )
        with self.assertRaisesRegex(RuntimeError, "должен быть каналом"):
            await service.preflight(bot)

        bot.get_chat.return_value = SimpleNamespace(id=CHAT_ID, type="channel")
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", can_delete_messages=False, can_post_messages=True
        )
        with self.assertRaisesRegex(RuntimeError, "право удаления"):
            await service.preflight(bot)


class ManagerCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SalesPhotoRepository(self.root / "db.sqlite")
        self.repo.claim_photo(CHAT_ID, 10, "file")
        self.repo.mark_reposted(CHAT_ID, 10, 200)
        self.repo.mark_complete(CHAT_ID, 10)
        self.base = BOT_CARD_MARKER + build_caption(
            None, ProductIdentifiers(serial_number="ABC12345")
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    def query(self, data: str, caption_html: str, actor_id: int = 50):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat_id=CHAT_ID,
                chat=SimpleNamespace(id=CHAT_ID),
                message_id=200,
                caption=caption_html,
                caption_html=caption_html,
            ),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
        )

    def callback(self, action: str, generation: int) -> str:
        signature = self.repo.callback_signature(CHAT_ID, 10, generation)
        return f"sp:{action}:10:{generation}:{signature}"

    def context(self, admin: bool = True):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(
                    status="administrator" if admin else "member"
                )
            )
        )
        return SimpleNamespace(bot=bot)

    async def test_manager_selection_and_back(self):
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        selected_query = self.query(self.callback("m:olmas", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=selected_query), self.context()
        )
        selected_caption = selected_query.edit_message_caption.await_args.kwargs["caption"]
        self.assertIn("👤 Менеджер: <b>Olmas</b>", selected_caption)
        back_markup = selected_query.edit_message_caption.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        self.assertEqual(
            back_markup["inline_keyboard"][0][0]["callback_data"],
            self.callback("b", 1),
        )
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Olmas")

        back_query = self.query(self.callback("b", 1), selected_caption)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=back_query), self.context()
        )
        self.assertEqual(
            back_query.edit_message_caption.await_args.kwargs["caption"], self.base
        )
        self.assertIsNone(self.repo.selected_manager(CHAT_ID, 200))

    async def test_non_admin_is_denied_when_allowlist_is_empty(self):
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        query = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=False)
        )
        query.edit_message_caption.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            "У вас нет доступа к выбору менеджера", show_alert=True
        )

    async def test_allowlist_is_additive_and_does_not_exclude_channel_admins(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({999})),
            self.repo,
            StaticRecognizer(),
        )
        query = self.query(self.callback("m:ali", 0), self.base, actor_id=50)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=True)
        )
        query.edit_message_caption.assert_awaited_once()
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_stale_callback_cannot_replace_newer_manager(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        second = self.query(self.callback("m:abbos", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=first), self.context(admin=False)
        )
        await service.on_manager_callback(
            SimpleNamespace(callback_query=second), self.context(admin=False)
        )
        second.edit_message_caption.assert_not_awaited()
        second.answer.assert_awaited_once_with(
            "Кнопка устарела",
            show_alert=True,
        )
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_post_edit_database_failure_still_blocks_stale_click(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        with patch.object(
            self.repo,
            "commit_reserved_manager_selection",
            side_effect=RuntimeError("database unavailable"),
        ):
            await service.on_manager_callback(
                SimpleNamespace(callback_query=first),
                self.context(admin=False),
            )
        selected_caption = first.edit_message_caption.await_args.kwargs["caption"]

        stale = self.query(self.callback("m:abbos", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=stale),
            self.context(admin=False),
        )
        stale.edit_message_caption.assert_not_awaited()
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        repair = self.query(self.callback("b", 1), selected_caption)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=repair),
            self.context(admin=False),
        )
        repair.edit_message_caption.assert_awaited_once()
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 2)

    async def test_ambiguous_manager_edit_invalidates_old_buttons_durably(self):
        first_service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        first.edit_message_caption.side_effect = NetworkError("response lost")
        await first_service.on_manager_callback(
            SimpleNamespace(callback_query=first),
            self.context(admin=False),
        )
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 1)

        restarted_service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        stale = self.query(self.callback("m:abbos", 0), self.base)
        await restarted_service.on_manager_callback(
            SimpleNamespace(callback_query=stale),
            self.context(admin=False),
        )
        stale.edit_message_caption.assert_not_awaited()
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

    async def test_definite_manager_edit_failure_releases_reservation(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        failed = self.query(self.callback("m:ali", 0), self.base)
        failed.edit_message_caption.side_effect = RuntimeError("rejected")
        await service.on_manager_callback(
            SimpleNamespace(callback_query=failed),
            self.context(admin=False),
        )
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 0)

        retry = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=retry),
            self.context(admin=False),
        )
        retry.edit_message_caption.assert_awaited_once()
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")


class ApplicationWiringTests(unittest.TestCase):
    def test_application_has_photo_callback_handlers_and_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            app = build_application(
                settings(root),
                repository=repo,
                recognizer=StaticRecognizer(),
            )
            self.assertEqual(len(app.handlers[0]), 2)
            self.assertEqual(app.update_processor.max_concurrent_updates, 4)
            keyboard = manager_keyboard().to_dict()
            self.assertEqual(
                [[button["text"] for button in row] for row in keyboard["inline_keyboard"]],
                [["Olmas", "Otabek"], ["Ali", "Abbos"]],
            )
            signature = repo.callback_signature(CHAT_ID, 10, 0)
            callback_data = f"sp:m:olmas:10:0:{signature}"
            self.assertIsNotNone(app.handlers[0][1].pattern.fullmatch(callback_data))


class StartupSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_start_is_not_marked_until_telegram_flush_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            service = SimpleNamespace(
                preflight=AsyncMock(return_value=BOT_ID),
                start_maintenance=MagicMock(),
            )
            bot = SimpleNamespace(
                delete_webhook=AsyncMock(side_effect=NetworkError("offline"))
            )
            with self.assertRaises(NetworkError):
                await _prepare_polling(settings(root), repo, service, bot)
            self.assertFalse(repo.is_bootstrapped(BOT_ID, CHAT_ID))
            service.start_maintenance.assert_not_called()

            bot.delete_webhook.side_effect = None
            bot.delete_webhook.return_value = True
            await _prepare_polling(settings(root), repo, service, bot)
            self.assertTrue(repo.is_bootstrapped(BOT_ID, CHAT_ID))
            bot.delete_webhook.assert_awaited_with(drop_pending_updates=True)
            service.start_maintenance.assert_called_once_with(bot)


if __name__ == "__main__":
    unittest.main()
