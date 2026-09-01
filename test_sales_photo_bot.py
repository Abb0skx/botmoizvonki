from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from telegram import Chat, Message, MessageEntity, PhotoSize, Update
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ExtBot

from sales_photo_bot.application import (
    StartupDrainBot,
    _prepare_polling,
    build_application,
    run,
)
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
        source_edit_grace_seconds=0,
        startup_drain_seconds=0,
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


def text_message(
    text: str,
    message_id: int = 10,
    sender_id: int = 50,
):
    return SimpleNamespace(
        chat_id=CHAT_ID,
        chat=SimpleNamespace(id=CHAT_ID),
        message_id=message_id,
        message_thread_id=None,
        text=text,
        entities=(),
        photo=(),
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
    )


def album_photo_message(
    message_id: int,
    media_group_id: str = "album-1",
    caption: str | None = None,
):
    message = photo_message(message_id=message_id, caption=caption)
    message.media_group_id = media_group_id
    message.photo = (
        SimpleNamespace(
            file_id=f"album-file-{message_id}",
            file_unique_id=f"album-unique-{message_id}",
            file_size=500,
            width=1000,
            height=1000,
        ),
    )
    return message


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

    async def send_message(**kwargs):
        events.append("send-message")
        return SimpleNamespace(message_id=300)

    async def send_media_group(**kwargs):
        events.append("send-album")
        return tuple(
            SimpleNamespace(message_id=200 + index)
            for index, _ in enumerate(kwargs["media"])
        )

    async def delete_message(chat_id, message_id):
        events.append(f"delete:{message_id}")
        return True

    bot.send_photo = AsyncMock(side_effect=send_photo)
    bot.send_message = AsyncMock(side_effect=send_message)
    bot.send_media_group = AsyncMock(side_effect=send_media_group)
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
        self.assertEqual(parsed.source_edit_grace_seconds, 3)
        self.assertEqual(parsed.startup_drain_seconds, 10)

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
        self.assertFalse(hasattr(parsed, "ocr_timeout_seconds"))
        with self.assertRaises(ConfigError):
            Settings.from_env(
                {
                    "SALES_PHOTO_BOT_TOKEN": TOKEN,
                    "SALES_PHOTO_CHAT_ID": str(CHAT_ID),
                    "SALES_PHOTO_STARTUP_DRAIN_SECONDS": "0",
                }
            )


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
            "🛒💵:\n"
            "rasxod:\n\n"
            "📞: +998 90 123 45 67\n\n"
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

    def test_manually_typed_product_label_is_rendered_safely(self):
        caption = build_caption(
            "901234567",
            ProductIdentifiers(),
            product_label="A16 <8/256>",
        )
        self.assertTrue(caption.startswith("📦 A16 &lt;8/256&gt;\n\n🛒💵:"))
        self.assertIn("📞: +998 90 123 45 67", caption)

    def test_sale_date_is_the_first_card_field(self):
        caption = build_caption(
            "901234567",
            ProductIdentifiers(),
            product_label="A16",
            sale_date=date(2026, 8, 31),
        )

        self.assertTrue(
            caption.startswith(
                "📆: 31/08/2026\n\n📦 A16\n\n🛒💵:"
            )
        )

    def test_only_two_normalized_phones_survive_the_source_caption(self):
        caption = build_caption(
            "клиент 42: 90‑123‑45‑67 / +998 (91) 765 43 21",
            ProductIdentifiers(),
        )
        self.assertIn(
            "📞: +998 90 123 45 67 / +998 91 765 43 21",
            caption,
        )
        self.assertNotIn("клиент", caption)
        self.assertNotIn("42:", caption)

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
        self.assertNotIn("client", caption)
        self.assertEqual(caption.count("📞:"), 1)
        self.assertIn("📞:\n", caption)
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

    def test_album_retry_payload_and_members_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            old = utc_now() - timedelta(minutes=5)
            self.assertTrue(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "album-unique",
                    source_file_id="file-10",
                    source_file_ids=("file-10", "file-11"),
                    source_message_ids=(10, 11),
                    source_kind="album",
                    client_caption="901234567",
                    at=old,
                )
            )
            repo.mark_failed(CHAT_ID, 10, "temporary", at=old)

            pending = repo.retryable_photos(CHAT_ID)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].source_kind, "album")
            self.assertEqual(pending[0].source_file_ids, ("file-10", "file-11"))
            self.assertEqual(pending[0].source_message_ids, (10, 11))
            self.assertEqual(repo.pending_source_members(CHAT_ID, 10), (10, 11))

            self.assertTrue(repo.claim_retry(CHAT_ID, 10, pending[0].attempts))
            self.assertEqual(
                repo.record_replacement(
                    CHAT_ID,
                    10,
                    300,
                    output_message_ids=(200, 201, 300),
                ),
                "recorded",
            )
            self.assertEqual(
                repo.output_message_ids(CHAT_ID, 10),
                (200, 201, 300),
            )

            self.assertEqual(
                repo.cancel_edited_source(CHAT_ID, 10),
                (True, 300),
            )
            self.assertEqual(
                [
                    job.message_id
                    for job in repo.pending_duplicate_cleanups(CHAT_ID)
                ],
                [200, 201, 300],
            )
            self.assertFalse(repo.is_replacement(CHAT_ID, 300))

    def test_edited_album_member_blocks_later_album_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            self.assertEqual(
                repo.cancel_edited_source(CHAT_ID, 11),
                (True, None),
            )
            self.assertFalse(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "album-unique",
                    source_file_id="file-10",
                    source_file_ids=("file-10", "file-11"),
                    source_message_ids=(10, 11),
                    source_kind="album",
                )
            )

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

    async def test_phone_only_text_message_is_ignored(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        await service.on_text(
            SimpleNamespace(effective_message=text_message("+998 90 123 45 67")),
            SimpleNamespace(bot=bot),
        )

        self.assertEqual(service._photo_tasks, set())
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_date_only_text_message_is_ignored(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        with patch(
            "sales_photo_bot.dates.tashkent_today",
            return_value=date(2026, 9, 1),
        ):
            await service.on_text(
                SimpleNamespace(effective_message=text_message("31/08")),
                SimpleNamespace(bot=bot),
            )

        self.assertEqual(service._photo_tasks, set())
        bot.send_message.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_short_model_text_becomes_one_text_card(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        await service.on_text(
            SimpleNamespace(
                effective_message=text_message("A16 8/256\n901234567")
            ),
            SimpleNamespace(bot=bot),
        )
        await asyncio.gather(*tuple(service._photo_tasks))

        bot.send_photo.assert_not_awaited()
        bot.send_media_group.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        sent = bot.send_message.await_args.kwargs
        self.assertIn("📦 A16 8/256", sent["text"])
        self.assertIn("📞: +998 90 123 45 67", sent["text"])
        self.assertIsNotNone(sent["reply_markup"])
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)
        self.assertTrue(repo.is_replacement(CHAT_ID, 300))

    async def test_text_model_with_date_gets_a_normalized_date(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        with patch(
            "sales_photo_bot.dates.tashkent_today",
            return_value=date(2026, 9, 1),
        ):
            await service.on_text(
                SimpleNamespace(
                    effective_message=text_message(
                        "31/08 A16 8/256 901234567"
                    )
                ),
                SimpleNamespace(bot=bot),
            )
            await asyncio.gather(*tuple(service._photo_tasks))

        sent = bot.send_message.await_args.kwargs["text"]
        self.assertTrue(
            sent.startswith(
                BOT_CARD_MARKER
                + "📆: 31/08/2026\n🆔: 1\n\n📦 A16 8/256\n\n🛒💵:"
            )
        )
        self.assertIn("📞: +998 90 123 45 67", sent)
        self.assertNotIn("31/08 A16", sent)

    async def test_photo_caption_with_date_gets_a_normalized_date(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        with patch(
            "sales_photo_bot.dates.tashkent_today",
            return_value=date(2026, 9, 1),
        ):
            await service.handle_photo(
                photo_message(caption="31/08 901234567"),
                bot,
            )

        caption = bot.send_photo.await_args.kwargs["caption"]
        self.assertTrue(
            caption.startswith(
                BOT_CARD_MARKER + "📆: 31/08/2026\n🆔: 1\n\n🛒💵:"
            )
        )
        self.assertIn("📞: +998 90 123 45 67", caption)

    async def test_text_card_retry_remains_text_only(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        old = utc_now() - timedelta(minutes=5)
        self.assertTrue(
            repo.claim_photo(
                CHAT_ID,
                10,
                "text-unique",
                source_file_id="sales-photo:text",
                client_caption="A16",
                source_kind="text",
                at=old,
            )
        )
        repo.mark_failed(CHAT_ID, 10, "temporary", at=old)
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        await service.retry_failed_photos(bot)

        bot.send_message.assert_awaited_once()
        bot.send_photo.assert_not_awaited()
        bot.send_media_group.assert_not_awaited()
        self.assertIn("📦 A16", bot.send_message.await_args.kwargs["text"])
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_photo_album_with_receipt_becomes_one_product(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()

        with patch(
            "sales_photo_bot.service.asyncio.sleep",
            new=AsyncMock(),
        ):
            await service.on_photo(
                SimpleNamespace(
                    effective_message=album_photo_message(
                        10,
                        caption="901234567",
                    )
                ),
                SimpleNamespace(bot=bot),
            )
            await service.on_photo(
                SimpleNamespace(
                    effective_message=album_photo_message(11)
                ),
                SimpleNamespace(bot=bot),
            )
            await asyncio.gather(*tuple(service._album_tasks.values()))
            await asyncio.gather(*tuple(service._photo_tasks))

        bot.send_photo.assert_not_awaited()
        bot.send_media_group.assert_awaited_once()
        media = bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual([item.media for item in media], [
            "album-file-10",
            "album-file-11",
        ])
        bot.send_message.assert_awaited_once()
        card = bot.send_message.await_args.kwargs
        self.assertIn("📞: +998 90 123 45 67", card["text"])
        self.assertIsNotNone(card["reply_markup"])
        self.assertEqual(
            [call.args[1] for call in bot.delete_message.await_args_list],
            [10, 11],
        )
        self.assertTrue(repo.is_replacement(CHAT_ID, 300))
        self.assertEqual(repo.output_message_ids(CHAT_ID, 10), (200, 201, 300))

    async def test_album_date_is_added_to_the_shared_card(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()
        claim = service._claim_album(
            (
                album_photo_message(10, caption="31/08 901234567"),
                album_photo_message(11),
            ),
            "album-1",
        )

        with patch(
            "sales_photo_bot.dates.tashkent_today",
            return_value=date(2026, 9, 1),
        ):
            await service._run_photo_claim(claim, bot)

        card = bot.send_message.await_args.kwargs["text"]
        self.assertTrue(
            card.startswith(
                BOT_CARD_MARKER + "📆: 31/08/2026\n🆔: 1\n\n🛒💵:"
            )
        )

    async def test_album_card_failure_removes_copied_album_and_keeps_sources(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()
        bot.send_message.side_effect = BadRequest("text rejected")
        claim = service._claim_album(
            (
                album_photo_message(10),
                album_photo_message(11),
            ),
            "album-1",
        )
        self.assertIsNotNone(claim)

        await service._run_photo_claim(claim, bot)

        self.assertEqual(
            [call.args[1] for call in bot.delete_message.await_args_list],
            [200, 201],
        )
        self.assertEqual(
            [job.source_message_id for job in repo.retryable_photos(CHAT_ID)],
            [10],
        )
        self.assertEqual(repo.pending_source_members(CHAT_ID, 10), (10, 11))

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
        self.assertIn("📞: +998 90 123 45 67", sent["caption"])
        self.assertNotIn("📦", sent["caption"])
        self.assertEqual(len(sent["reply_markup"].inline_keyboard), 2)
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))

    async def test_failed_optional_recognizer_reposts_empty_template(self):
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

    async def test_production_passthrough_never_downloads_or_reads_photo(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo)
        bot = telegram_bot()
        bot.get_file.side_effect = AssertionError("photo must not be downloaded")

        await service.handle_photo(photo_message(caption=None), bot)

        bot.get_file.assert_not_awaited()
        sent = bot.send_photo.await_args.kwargs
        self.assertEqual(sent["photo"], "large")
        self.assertNotIn("<blockquote>IMEI:", sent["caption"])
        self.assertNotIn("<blockquote>S/N:", sent["caption"])
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_source_edit_during_optional_recognizer_cancels_repost(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingRecognizer:
            async def recognize(self, image_bytes: bytes, mime_type: str):
                entered.set()
                await release.wait()
                return ProductIdentifiers(serial_number="OLD12345")

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, BlockingRecognizer())
        bot = telegram_bot()
        original = photo_message(caption="+998 90 123 45 67")
        task = asyncio.create_task(service.handle_photo(original, bot))
        await entered.wait()

        edited = photo_message(caption="+998 91 765 43 21")
        edited.photo = (
            SimpleNamespace(
                file_id="new-file",
                file_unique_id="new-unique",
                file_size=600,
                width=1200,
                height=1200,
            ),
        )
        await service.handle_edited_photo(edited, bot, update_id=101)
        release.set()
        await task

        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        with sqlite3.connect(repo.path) as db:
            status, error = db.execute(
                "SELECT status,last_error_code FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()
        self.assertEqual((status, error), ("complete", "source_edited"))

    async def test_source_media_change_away_from_photo_cancels_stale_work(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingRecognizer:
            async def recognize(self, image_bytes: bytes, mime_type: str):
                entered.set()
                await release.wait()
                return ProductIdentifiers(serial_number="OLD12345")

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, BlockingRecognizer())
        bot = telegram_bot()
        await service.on_photo(
            SimpleNamespace(effective_message=photo_message()),
            SimpleNamespace(bot=bot),
        )
        await entered.wait()
        edited = photo_message(caption="заменено на документ")
        edited.photo = ()
        edited.document = SimpleNamespace(file_id="new-document")

        await service.handle_edited_photo(edited, bot, update_id=106)
        release.set()
        await asyncio.gather(*tuple(service._photo_tasks))

        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_on_photo_claims_before_delayed_background_first_slice(self):
        background_entered = asyncio.Event()
        release_background = asyncio.Event()
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        original_run = service._run_photo_claim

        async def delayed_run(claim, target_bot):
            background_entered.set()
            await release_background.wait()
            await original_run(claim, target_bot)

        service._run_photo_claim = delayed_run
        await service.on_photo(
            SimpleNamespace(effective_message=photo_message()),
            SimpleNamespace(bot=bot),
        )
        await background_entered.wait()
        with sqlite3.connect(repo.path) as db:
            status = db.execute(
                "SELECT status FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()[0]
        self.assertEqual(status, "processing")
        await service.handle_edited_photo(
            photo_message(caption="+998 91 765 43 21"),
            bot,
            update_id=101,
        )
        release_background.set()
        await asyncio.gather(*tuple(service._photo_tasks))

        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))

    async def test_on_photo_bounds_workers_and_defers_overflow_durably(self):
        entered_count = 0
        all_entered = asyncio.Event()
        release = asyncio.Event()

        class BoundedRecognizer:
            async def recognize(self, image_bytes: bytes, mime_type: str):
                nonlocal entered_count
                entered_count += 1
                if entered_count == 2:
                    all_entered.set()
                await release.wait()
                return ProductIdentifiers()

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        configured = replace(settings(self.root), concurrent_updates=2)
        service = SalesPhotoService(configured, repo, BoundedRecognizer())
        bot = telegram_bot()
        next_replacement = 200

        async def unique_send(**kwargs):
            nonlocal next_replacement
            result = SimpleNamespace(message_id=next_replacement)
            next_replacement += 1
            return result

        bot.send_photo.side_effect = unique_send
        for message_id in range(10, 15):
            await service.on_photo(
                SimpleNamespace(
                    effective_message=photo_message(message_id=message_id)
                ),
                SimpleNamespace(bot=bot),
            )
        await all_entered.wait()

        self.assertEqual(len(service._photo_tasks), 2)
        self.assertEqual(bot.get_file.await_count, 2)
        deferred = repo.retryable_photos(CHAT_ID)
        self.assertEqual(
            [job.source_message_id for job in deferred],
            [12, 13, 14],
        )

        release.set()
        await asyncio.gather(*tuple(service._photo_tasks))

    async def test_graceful_stop_during_optional_recognizer_is_retryable(self):
        entered = asyncio.Event()

        class BlockingRecognizer:
            async def recognize(self, image_bytes: bytes, mime_type: str):
                entered.set()
                await asyncio.Event().wait()

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, BlockingRecognizer())
        bot = telegram_bot()
        await service.on_photo(
            SimpleNamespace(effective_message=photo_message()),
            SimpleNamespace(bot=bot),
        )
        await entered.wait()

        await service.stop()

        jobs = repo.retryable_photos(CHAT_ID)
        self.assertEqual([job.source_message_id for job in jobs], [10])
        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_source_edit_db_error_fails_closed_before_send(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingRecognizer:
            async def recognize(self, image_bytes: bytes, mime_type: str):
                entered.set()
                await release.wait()
                return ProductIdentifiers(serial_number="OLD12345")

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, BlockingRecognizer())
        bot = telegram_bot()
        await service.on_photo(
            SimpleNamespace(effective_message=photo_message()),
            SimpleNamespace(bot=bot),
        )
        await entered.wait()

        with patch.object(
            repo,
            "cancel_edited_source",
            side_effect=RuntimeError("database unavailable"),
        ):
            await service.handle_edited_photo(
                photo_message(caption="+998 91 765 43 21"),
                bot,
                update_id=104,
            )
        release.set()
        await asyncio.gather(*tuple(service._photo_tasks))

        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        with sqlite3.connect(repo.path) as db:
            status, error = db.execute(
                "SELECT status,last_error_code FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()
        self.assertEqual((status, error), ("complete", "source_edit_fail_safe"))

    async def test_source_edit_classification_error_fails_closed(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        recognizer = StaticRecognizer()

        async def blocked_recognize(image_bytes, mime_type):
            entered.set()
            await release.wait()
            return ProductIdentifiers()

        recognizer.recognize = blocked_recognize
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, recognizer)
        bot = telegram_bot()
        await service.on_photo(
            SimpleNamespace(effective_message=photo_message()),
            SimpleNamespace(bot=bot),
        )
        await entered.wait()

        with patch.object(
            repo,
            "is_replacement",
            side_effect=RuntimeError("database unavailable"),
        ):
            await service.handle_edited_photo(
                photo_message(caption="+998 91 765 43 21"),
                bot,
                update_id=105,
            )
        release.set()
        await asyncio.gather(*tuple(service._photo_tasks))

        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    async def test_source_edit_after_repost_keeps_source_and_removes_card(self):
        sent = asyncio.Event()
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        configured = replace(settings(self.root), source_edit_grace_seconds=1)
        service = SalesPhotoService(configured, repo, StaticRecognizer())
        bot = telegram_bot()

        async def signal_send(**kwargs):
            sent.set()
            return SimpleNamespace(message_id=200)

        bot.send_photo.side_effect = signal_send
        task = asyncio.create_task(service.handle_photo(photo_message(), bot))
        await sent.wait()
        await asyncio.sleep(0.05)
        await service.handle_edited_photo(
            photo_message(caption="+998 91 765 43 21"),
            bot,
            update_id=102,
        )
        await task

        bot.send_photo.assert_awaited_once()
        deleted_ids = [
            call.args[1] for call in bot.delete_message.await_args_list
        ]
        self.assertIn(200, deleted_ids)
        self.assertNotIn(10, deleted_ids)
        self.assertFalse(repo.is_replacement(CHAT_ID, 200))

    async def test_source_edit_during_delete_keeps_replacement_fallback(self):
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        deleted_ids: list[int] = []

        async def blocking_delete(chat_id, message_id):
            deleted_ids.append(message_id)
            if message_id == 10:
                delete_started.set()
                await release_delete.wait()
            return True

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.delete_message.side_effect = blocking_delete
        task = asyncio.create_task(service.handle_photo(photo_message(), bot))
        await delete_started.wait()

        await service.handle_edited_photo(
            photo_message(caption="+998 91 765 43 21"),
            bot,
            update_id=103,
        )
        release_delete.set()
        await task

        self.assertEqual(deleted_ids, [10])
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))
        with sqlite3.connect(repo.path) as db:
            status, error = db.execute(
                "SELECT status,last_error_code FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()
        self.assertEqual(
            (status, error),
            ("complete", "source_edited_delete_ambiguous"),
        )

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

    async def test_stale_before_send_job_is_safely_retried(self):
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
        self.assertTrue(repo.is_replacement(CHAT_ID, 200))
        self.assertEqual(repo.retryable_photos(CHAT_ID), ())
        bot.send_photo.assert_awaited_once()
        bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)
        with sqlite3.connect(repo.path) as db:
            payload = db.execute(
                "SELECT encrypted_payload FROM sales_photo_jobs "
                "WHERE chat_id=? AND source_message_id=?",
                (CHAT_ID, 10),
            ).fetchone()[0]
        self.assertIsNone(payload)

    async def test_stale_send_started_job_is_quarantined_without_duplicate(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        old = utc_now() - timedelta(minutes=10)
        self.assertTrue(
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique-large",
                source_file_id="large",
                at=old,
            )
        )
        self.assertTrue(repo.mark_send_started(CHAT_ID, 10, at=old))
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

        bot.send_photo.assert_not_awaited()
        bot.delete_message.assert_not_awaited()
        self.assertEqual(repo.retryable_photos(CHAT_ID), ())

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

    async def test_long_retry_after_remains_durably_retryable(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = RetryAfter(61)

        await service.handle_photo(photo_message(), bot)

        self.assertEqual(bot.send_photo.await_count, 1)
        self.assertEqual(
            [job.source_message_id for job in repo.retryable_photos(CHAT_ID)],
            [10],
        )
        bot.delete_message.assert_not_awaited()

    async def test_two_retry_after_responses_remain_retryable(self):
        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = (RetryAfter(0.1), RetryAfter(0.2))

        with patch("sales_photo_bot.service.asyncio.sleep", new=AsyncMock()):
            await service.handle_photo(photo_message(), bot)

        self.assertEqual(bot.send_photo.await_count, 2)
        self.assertEqual(
            [job.source_message_id for job in repo.retryable_photos(CHAT_ID)],
            [10],
        )
        bot.delete_message.assert_not_awaited()

    async def test_cancel_during_retry_after_wait_remains_retryable(self):
        wait_started = asyncio.Event()

        async def blocked_wait(delay):
            wait_started.set()
            await asyncio.Event().wait()

        repo = SalesPhotoRepository(self.root / "db.sqlite")
        service = SalesPhotoService(settings(self.root), repo, StaticRecognizer())
        bot = telegram_bot()
        bot.send_photo.side_effect = RetryAfter(30)
        with patch(
            "sales_photo_bot.service.asyncio.sleep",
            new=AsyncMock(side_effect=blocked_wait),
        ):
            task = asyncio.create_task(service.handle_photo(photo_message(), bot))
            await wait_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(
            [job.source_message_id for job in repo.retryable_photos(CHAT_ID)],
            [10],
        )
        bot.delete_message.assert_not_awaited()

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


class EditedCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SalesPhotoRepository(self.root / "db.sqlite")
        self.repo.claim_photo(CHAT_ID, 10, "file")
        self.repo.mark_reposted(CHAT_ID, 10, 200)
        self.repo.mark_complete(CHAT_ID, 10)
        self.service = SalesPhotoService(
            settings(self.root), self.repo, StaticRecognizer()
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    @staticmethod
    def message(caption: str, message_id: int = 200, entities=()):
        return SimpleNamespace(
            chat_id=CHAT_ID,
            chat=SimpleNamespace(id=CHAT_ID),
            message_id=message_id,
            caption=caption,
            caption_entities=entities,
            photo=(SimpleNamespace(file_id="card"),),
        )

    @staticmethod
    def bot():
        return SimpleNamespace(edit_message_caption=AsyncMock())

    async def test_manual_edit_normalizes_two_phones_and_preserves_finance(self):
        caption = (
            BOT_CARD_MARKER
            + "🛒💵: ACME / $100\n"
            "rasxod: $3\n\n"
            "📞: 90 123-45-67 / 998 91 765 43 21\n\n"
            "Наличка\n"
            "💵: 101\n"
            "🇺🇿: 1 250 000"
        )
        cash_start = len(caption[: caption.index("Наличка")].encode("utf-16-le")) // 2
        entities = (
            MessageEntity(
                type=MessageEntity.BOLD,
                offset=cash_start,
                length=len("Наличка"),
            ),
        )
        bot = self.bot()

        await self.service.handle_edited_photo(
            self.message(caption, entities=entities), bot, update_id=100
        )

        bot.edit_message_caption.assert_awaited_once()
        kwargs = bot.edit_message_caption.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHAT_ID)
        self.assertEqual(kwargs["message_id"], 200)
        self.assertIn(
            "📞: +998 90 123 45 67 / +998 91 765 43 21",
            kwargs["caption"],
        )
        self.assertIn("🛒💵: ACME / 100$", kwargs["caption"])
        self.assertIn("rasxod: $3", kwargs["caption"])
        self.assertIn("💵: 101$", kwargs["caption"])
        self.assertIn("🇺🇿: 1 250 000 So'm", kwargs["caption"])
        self.assertNotIn("parse_mode", kwargs)
        markup = kwargs["reply_markup"].to_dict()
        self.assertEqual(
            [[button["text"] for button in row] for row in markup["inline_keyboard"]],
            [["Olmas", "Otabek"], ["Ali", "Abbos"]],
        )
        self.assertEqual(kwargs["caption_entities"][0].type, MessageEntity.BOLD)

    async def test_manual_edit_restores_the_persisted_order_id(self):
        self.repo.ensure_daily_order(CHAT_ID, 10, date(2026, 8, 31))
        caption = (
            BOT_CARD_MARKER
            + "🛒💵: ACME\nrasxod: $3\n\n📞:\n\nНаличка"
        )
        bot = self.bot()

        await self.service.handle_edited_photo(
            self.message(caption),
            bot,
            update_id=105,
        )

        normalized = bot.edit_message_caption.await_args.kwargs["caption"]
        self.assertTrue(normalized.startswith(BOT_CARD_MARKER + "🆔: 1\n\n"))
        self.assertIn("🛒💵: ACME", normalized)
        self.assertIn("rasxod: $3", normalized)
        self.assertEqual(self.repo.pending_order_backfills(CHAT_ID), ())

    async def test_manual_edit_normalizes_a_text_card(self):
        caption = (
            BOT_CARD_MARKER
            + "📦 A16\n\n🛒💵:\nrasxod:\n\n"
            "📞: 901234567\n\nНаличка"
        )
        bot = SimpleNamespace(
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await self.service.handle_edited_photo(
            text_message(caption, message_id=200),
            bot,
            update_id=99,
        )

        bot.edit_message_caption.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertIn("📦 A16", kwargs["text"])
        self.assertIn("📞: +998 90 123 45 67", kwargs["text"])

    async def test_phone_edit_rebuilds_current_selected_manager_keyboard(self):
        self.assertTrue(
            self.repo.apply_manager_selection(CHAT_ID, 200, "Otabek", 0)
        )
        bot = self.bot()
        caption = "🛒💵:\nrasxod:\n\n📞: 901234567\n\nНаличка"

        await self.service.handle_edited_photo(
            self.message(caption), bot, update_id=100
        )

        markup = bot.edit_message_caption.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "👤 Otabek · ↩️ Назад")
        self.assertIn(":1:", button["callback_data"])

    async def test_card_is_found_by_ledger_even_when_marker_was_removed(self):
        bot = self.bot()
        caption = "🛒💵:\nrasxod:\n\n📞: 901234567\n\nНаличка"

        await self.service.handle_edited_photo(
            self.message(caption), bot, update_id=101
        )

        normalized = bot.edit_message_caption.await_args.kwargs["caption"]
        self.assertTrue(normalized.startswith("🛒💵:"))
        self.assertIn("📞: +998 90 123 45 67", normalized)

    async def test_unknown_card_is_ignored(self):
        bot = self.bot()
        await self.service.handle_edited_photo(
            self.message(
                "🛒💵:\nrasxod:\n\n📞: 901234567",
                message_id=999,
            ),
            bot,
            update_id=200,
        )

        bot.edit_message_caption.assert_not_awaited()

    async def test_known_invalid_and_canonical_snapshots_are_reasserted(self):
        bot = self.bot()
        captions = (
            "🛒💵:\nrasxod:\n\n📞: клиент 42",
            "🛒💵:\nrasxod:\n\n📞: +998 90 123 45 67",
        )

        for revision, caption in enumerate(captions, start=201):
            await self.service.handle_edited_photo(
                self.message(caption), bot, update_id=revision
            )

        self.assertEqual(bot.edit_message_caption.await_count, 2)
        self.assertEqual(
            [
                call.kwargs["caption"]
                for call in bot.edit_message_caption.await_args_list
            ],
            list(captions),
        )

    async def test_canonical_snapshot_treats_not_modified_as_success(self):
        bot = self.bot()
        bot.edit_message_caption.side_effect = BadRequest(
            "Message is not modified"
        )
        caption = "🛒💵:\nrasxod:\n\n📞: +998 90 123 45 67"

        await self.service.handle_edited_photo(
            self.message(caption), bot, update_id=203
        )

        bot.edit_message_caption.assert_awaited_once()

    async def test_newer_manual_edit_wins_when_older_update_arrives_late(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        applied: list[str] = []

        async def slow_edit(**kwargs):
            applied.append(kwargs["caption"])
            entered.set()
            await release.wait()
            return True

        bot = SimpleNamespace(edit_message_caption=AsyncMock(side_effect=slow_edit))
        newer = self.message(
            "🛒💵: NEW\nrasxod: 2\n\n📞: 911112233\n\nНаличка"
        )
        older = self.message(
            "🛒💵: OLD\nrasxod: 1\n\n📞: 901234567\n\nНаличка"
        )

        newer_task = asyncio.create_task(
            self.service.handle_edited_photo(newer, bot, update_id=301)
        )
        await entered.wait()
        older_task = asyncio.create_task(
            self.service.handle_edited_photo(older, bot, update_id=300)
        )
        release.set()
        await asyncio.gather(newer_task, older_task)

        self.assertEqual(bot.edit_message_caption.await_count, 1)
        self.assertIn("🛒💵: NEW", applied[-1])
        self.assertIn("📞: +998 91 111 22 33", applied[-1])

    async def test_newer_canonical_snapshot_is_reapplied_after_old_write(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        applied: list[str] = []

        async def slow_first_edit(**kwargs):
            applied.append(kwargs["caption"])
            if len(applied) == 1:
                entered.set()
                await release.wait()
            return True

        bot = SimpleNamespace(
            edit_message_caption=AsyncMock(side_effect=slow_first_edit)
        )
        older = self.message(
            "🛒💵: OLD\nrasxod: 1\n\n📞: 901234567\n\nНаличка"
        )
        newer = self.message(
            "🛒💵: NEW\nrasxod: 2\n\n"
            "📞: +998 91 111 22 33\n\nНаличка"
        )

        older_task = asyncio.create_task(
            self.service.handle_edited_photo(older, bot, update_id=300)
        )
        await entered.wait()
        newer_task = asyncio.create_task(
            self.service.handle_edited_photo(newer, bot, update_id=301)
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(older_task, newer_task)

        self.assertEqual(bot.edit_message_caption.await_count, 2)
        self.assertIn("🛒💵: OLD", applied[0])
        self.assertEqual(applied[-1], newer.caption)
        self.assertIn("rasxod: 2", applied[-1])

    async def test_sequential_newer_canonical_snapshot_replaces_old_write(self):
        applied: list[str] = []

        async def record_edit(**kwargs):
            applied.append(kwargs["caption"])
            return True

        bot = SimpleNamespace(
            edit_message_caption=AsyncMock(side_effect=record_edit)
        )
        older = self.message(
            "🛒💵: OLD\nrasxod: 1\n\n📞: 901234567\n\nНаличка"
        )
        newer = self.message(
            "🛒💵: NEW\nrasxod: 2\n\n"
            "📞: +998 91 111 22 33\n\nНаличка"
        )

        await self.service.handle_edited_photo(older, bot, update_id=300)
        await self.service.handle_edited_photo(newer, bot, update_id=301)

        self.assertEqual(applied[-1], newer.caption)
        self.assertEqual(len(applied), 2)

    async def test_sequential_older_snapshot_is_rejected_by_watermark(self):
        applied: list[str] = []

        async def record_edit(**kwargs):
            applied.append(kwargs["caption"])
            return True

        bot = SimpleNamespace(
            edit_message_caption=AsyncMock(side_effect=record_edit)
        )
        newer = self.message(
            "🛒💵: NEW\nrasxod: 2\n\n📞: 911112233\n\nНаличка"
        )
        older = self.message(
            "🛒💵: OLD\nrasxod: 1\n\n📞: 901234567\n\nНаличка"
        )

        await self.service.handle_edited_photo(newer, bot, update_id=301)
        await self.service.handle_edited_photo(older, bot, update_id=300)

        self.assertEqual(len(applied), 1)
        self.assertIn("🛒💵: NEW", applied[0])
        self.assertIn("📞: +998 91 111 22 33", applied[0])

    async def test_expired_watermark_allows_telegram_revision_reset(self):
        bot = self.bot()
        first = self.message(
            "🛒💵: FIRST\nrasxod:\n\n📞: 901234567\n\nНаличка"
        )
        after_reset = self.message(
            "🛒💵: RESET\nrasxod:\n\n📞: 911112233\n\nНаличка"
        )

        with patch(
            "sales_photo_bot.service.time.monotonic",
            side_effect=(1000.0, 1701.0),
        ):
            await self.service.handle_edited_photo(first, bot, update_id=300)
            await self.service.handle_edited_photo(after_reset, bot, update_id=10)

        self.assertEqual(bot.edit_message_caption.await_count, 2)
        self.assertIn(
            "🛒💵: RESET",
            bot.edit_message_caption.await_args.kwargs["caption"],
        )


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

    def query(
        self,
        data: str,
        caption_html: str,
        actor_id: int = 50,
        reply_markup=None,
    ):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat_id=CHAT_ID,
                chat=SimpleNamespace(id=CHAT_ID),
                message_id=200,
                caption=caption_html,
                caption_html=caption_html,
                reply_markup=reply_markup,
            ),
            answer=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )

    def text_query(
        self,
        data: str,
        text: str,
        actor_id: int = 50,
        reply_markup=None,
    ):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=actor_id),
            message=SimpleNamespace(
                chat_id=CHAT_ID,
                chat=SimpleNamespace(id=CHAT_ID),
                message_id=200,
                text=text,
                entities=(),
                reply_markup=reply_markup,
            ),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
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
        selected_query.edit_message_caption.assert_not_awaited()
        back_markup_object = selected_query.edit_message_reply_markup.await_args.kwargs[
            "reply_markup"
        ]
        back_markup = back_markup_object.to_dict()
        self.assertEqual(
            back_markup["inline_keyboard"][0][0]["text"],
            "👤 Olmas · ↩️ Назад",
        )
        self.assertEqual(
            back_markup["inline_keyboard"][0][0]["callback_data"],
            self.callback("b", 1),
        )
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Olmas")

        back_query = self.query(
            self.callback("b", 1),
            self.base,
            reply_markup=back_markup_object,
        )
        await service.on_manager_callback(
            SimpleNamespace(callback_query=back_query), self.context()
        )
        back_query.edit_message_caption.assert_not_awaited()
        manager_markup = back_query.edit_message_reply_markup.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        self.assertEqual(
            [[button["text"] for button in row] for row in manager_markup["inline_keyboard"]],
            [["Olmas", "Otabek"], ["Ali", "Abbos"]],
        )
        self.assertIsNone(self.repo.selected_manager(CHAT_ID, 200))

    async def test_manager_click_normalizes_phone_when_edit_update_was_missing(self):
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        raw_caption = self.base.replace("📞:", "📞: 901234567")
        query = self.query(self.callback("m:olmas", 0), raw_caption)

        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context()
        )

        query.edit_message_reply_markup.assert_not_awaited()
        query.edit_message_caption.assert_awaited_once()
        kwargs = query.edit_message_caption.await_args.kwargs
        self.assertIn("📞: +998 90 123 45 67", kwargs["caption"])
        self.assertEqual(
            kwargs["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"],
            "👤 Olmas · ↩️ Назад",
        )

    async def test_manager_click_restores_missing_order_id(self):
        self.repo.ensure_daily_order(CHAT_ID, 10, date(2026, 8, 31))
        service = SalesPhotoService(settings(self.root), self.repo)
        query = self.query(self.callback("m:olmas", 0), self.base)

        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context()
        )

        query.edit_message_reply_markup.assert_not_awaited()
        query.edit_message_caption.assert_awaited_once()
        caption = query.edit_message_caption.await_args.kwargs["caption"]
        self.assertTrue(caption.startswith(BOT_CARD_MARKER + "🆔: 1\n\n"))

    async def test_manager_click_normalizes_a_text_card(self):
        service = SalesPhotoService(settings(self.root), self.repo)
        raw_text = (
            BOT_CARD_MARKER
            + "📦 A16\n\n🛒💵:\nrasxod:\n\n📞: 901234567\n\nНаличка"
        )
        query = self.text_query(self.callback("m:olmas", 0), raw_text)

        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context()
        )

        query.edit_message_reply_markup.assert_not_awaited()
        query.edit_message_caption.assert_not_awaited()
        query.edit_message_text.assert_awaited_once()
        kwargs = query.edit_message_text.await_args.kwargs
        self.assertIn("📞: +998 90 123 45 67", kwargs["text"])
        self.assertEqual(
            kwargs["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"],
            "👤 Olmas · ↩️ Назад",
        )

    async def test_back_click_normalizes_phone_when_edit_update_was_missing(self):
        self.assertTrue(self.repo.apply_manager_selection(CHAT_ID, 200, "Ali", 0))
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        raw_caption = self.base.replace("📞:", "📞: 998917654321")
        query = self.query(self.callback("b", 1), raw_caption)

        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context()
        )

        query.edit_message_reply_markup.assert_not_awaited()
        query.edit_message_caption.assert_awaited_once()
        kwargs = query.edit_message_caption.await_args.kwargs
        self.assertIn("📞: +998 91 765 43 21", kwargs["caption"])
        self.assertEqual(
            [
                [button["text"] for button in row]
                for row in kwargs["reply_markup"].to_dict()["inline_keyboard"]
            ],
            [["Olmas", "Otabek"], ["Ali", "Abbos"]],
        )

    async def test_non_admin_is_denied_when_allowlist_is_empty(self):
        service = SalesPhotoService(settings(self.root), self.repo, StaticRecognizer())
        query = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=False)
        )
        query.edit_message_reply_markup.assert_not_awaited()
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
        query.edit_message_reply_markup.assert_awaited_once()
        query.edit_message_caption.assert_not_awaited()
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
        second.edit_message_reply_markup.assert_awaited_once()
        repaired = second.edit_message_reply_markup.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        self.assertEqual(
            repaired["inline_keyboard"][0][0]["text"],
            "👤 Ali · ↩️ Назад",
        )
        second.answer.assert_awaited_once_with(
            "Клавиатура обновлена. Нажмите ещё раз.",
            show_alert=True,
        )
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_database_failure_prevents_ui_change_and_releases_button(self):
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
        first.edit_message_reply_markup.assert_not_awaited()
        first.answer.assert_awaited_once_with(
            "Не удалось обновить карточку", show_alert=True
        )
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 0)
        self.assertIsNone(self.repo.selected_manager(CHAT_ID, 200))

        repair = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=repair),
            self.context(admin=False),
        )
        repair.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 1)
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_ambiguous_manager_edit_invalidates_old_buttons_durably(self):
        first_service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        first.edit_message_reply_markup.side_effect = NetworkError("response lost")
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
        stale.edit_message_reply_markup.assert_awaited_once()
        repaired = stale.edit_message_reply_markup.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        self.assertEqual(
            repaired["inline_keyboard"][0][0]["text"],
            "👤 Ali · ↩️ Назад",
        )
        stale.answer.assert_awaited_once_with(
            "Клавиатура обновлена. Нажмите ещё раз.",
            show_alert=True,
        )

    async def test_ambiguous_manager_edit_retries_idempotently(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        query = self.query(self.callback("m:ali", 0), self.base)
        query.edit_message_reply_markup.side_effect = (
            NetworkError("response lost"),
            BadRequest("Message is not modified"),
        )

        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=False)
        )

        self.assertEqual(query.edit_message_reply_markup.await_count, 2)
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 1)
        query.answer.assert_awaited_once_with("Выбран менеджер: Ali")

    async def test_definite_bad_request_rolls_back_manager_state(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        failed = self.query(self.callback("m:ali", 0), self.base)
        failed.edit_message_reply_markup.side_effect = BadRequest(
            "BUTTON_DATA_INVALID"
        )

        await service.on_manager_callback(
            SimpleNamespace(callback_query=failed), self.context(admin=False)
        )

        failed.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 0)
        self.assertIsNone(self.repo.selected_manager(CHAT_ID, 200))

        retry = self.query(self.callback("m:ali", 0), self.base)
        await service.on_manager_callback(
            SimpleNamespace(callback_query=retry), self.context(admin=False)
        )
        retry.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")

    async def test_cancelled_manager_edit_is_repaired_by_stale_button(self):
        entered = asyncio.Event()

        async def blocked_edit(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        first = self.query(self.callback("m:ali", 0), self.base)
        first.edit_message_reply_markup.side_effect = blocked_edit
        task = asyncio.create_task(
            service.on_manager_callback(
                SimpleNamespace(callback_query=first),
                self.context(admin=False),
            )
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 1)

        restarted = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        stale = self.query(self.callback("m:ali", 0), self.base)
        await restarted.on_manager_callback(
            SimpleNamespace(callback_query=stale),
            self.context(admin=False),
        )
        stale.edit_message_reply_markup.assert_awaited_once()
        repaired = stale.edit_message_reply_markup.await_args.kwargs[
            "reply_markup"
        ].to_dict()
        self.assertEqual(
            repaired["inline_keyboard"][0][0]["text"],
            "👤 Ali · ↩️ Назад",
        )

    async def test_ambiguous_back_edit_retries_idempotently(self):
        self.assertTrue(self.repo.apply_manager_selection(CHAT_ID, 200, "Ali", 0))
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        query = self.query(self.callback("b", 1), self.base)
        query.edit_message_reply_markup.side_effect = (
            NetworkError("response lost"),
            True,
        )

        await service.on_manager_callback(
            SimpleNamespace(callback_query=query), self.context(admin=False)
        )

        self.assertEqual(query.edit_message_reply_markup.await_count, 2)
        self.assertIsNone(self.repo.selected_manager(CHAT_ID, 200))
        self.assertEqual(self.repo.ui_generation_for_replacement(CHAT_ID, 200), 2)
        query.answer.assert_awaited_once_with()

    async def test_definite_manager_edit_failure_releases_reservation(self):
        service = SalesPhotoService(
            settings(self.root, allowed=frozenset({50})),
            self.repo,
            StaticRecognizer(),
        )
        failed = self.query(self.callback("m:ali", 0), self.base)
        failed.edit_message_reply_markup.side_effect = RuntimeError("rejected")
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
        retry.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(self.repo.selected_manager(CHAT_ID, 200), "Ali")


class ApplicationWiringTests(unittest.TestCase):
    def test_application_has_photo_callback_handlers_and_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            app = build_application(
                settings(root),
                repository=repo,
            )
            self.assertIsInstance(app.bot, StartupDrainBot)
            self.assertIsNone(
                app.bot_data["sales_photo_service"].recognizer
            )
            self.assertEqual(len(app.handlers[0]), 4)
            self.assertEqual(app.update_processor.max_concurrent_updates, 4)
            keyboard = manager_keyboard().to_dict()
            self.assertEqual(
                [[button["text"] for button in row] for row in keyboard["inline_keyboard"]],
                [["Olmas", "Otabek"], ["Ali", "Abbos"]],
            )
            signature = repo.callback_signature(CHAT_ID, 10, 0)
            callback_data = f"sp:m:olmas:10:0:{signature}"
            self.assertIsNotNone(app.handlers[0][3].pattern.fullmatch(callback_data))

            chat = Chat(id=CHAT_ID, type="channel")
            photo = PhotoSize(
                file_id="file",
                file_unique_id="unique",
                width=100,
                height=100,
            )
            message = Message(
                message_id=10,
                date=datetime.now(timezone.utc),
                chat=chat,
                photo=(photo,),
            )
            channel_post = Update(update_id=1, channel_post=message)
            edited_post = Update(update_id=2, edited_channel_post=message)
            edited_text_message = Message(
                message_id=10,
                date=datetime.now(timezone.utc),
                chat=chat,
                text="photo replaced",
            )
            edited_text_post = Update(
                update_id=3,
                edited_channel_post=edited_text_message,
            )
            text_channel_post = Update(
                update_id=4,
                channel_post=Message(
                    message_id=11,
                    date=datetime.now(timezone.utc),
                    chat=chat,
                    text="A16",
                ),
            )
            self.assertTrue(app.handlers[0][0].check_update(channel_post))
            self.assertFalse(app.handlers[0][0].check_update(edited_post))
            self.assertFalse(app.handlers[0][1].check_update(channel_post))
            self.assertTrue(app.handlers[0][1].check_update(edited_post))
            self.assertTrue(app.handlers[0][1].check_update(edited_text_post))
            self.assertFalse(app.handlers[0][2].check_update(channel_post))
            self.assertTrue(app.handlers[0][2].check_update(text_channel_post))

    def test_polling_subscribes_to_edited_channel_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_app = SimpleNamespace(run_polling=MagicMock())
            with patch(
                "sales_photo_bot.application.build_application",
                return_value=fake_app,
            ):
                run(settings(Path(directory)))

        kwargs = fake_app.run_polling.call_args.kwargs
        self.assertIn("edited_channel_post", kwargs["allowed_updates"])


class StartupDrainBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_successful_empty_long_poll_opens_startup_gate(self):
        bot = StartupDrainBot(TOKEN)
        update = Update(update_id=1)
        mocked_get_updates = AsyncMock(
            side_effect=[(update,), (), (), NetworkError("offline")]
        )
        with patch.object(ExtBot, "get_updates", new=mocked_get_updates):
            self.assertEqual(
                await bot.get_updates(timeout=timedelta(seconds=10)),
                (update,),
            )
            self.assertFalse(bot.startup_updates_drained.is_set())

            self.assertEqual(await bot.get_updates(timeout=0), ())
            self.assertFalse(bot.startup_updates_drained.is_set())

            self.assertEqual(await bot.get_updates(timeout=10), ())
            self.assertTrue(bot.startup_updates_drained.is_set())

            with self.assertRaises(NetworkError):
                await bot.get_updates(timeout=10)

    async def test_edited_source_tombstone_blocks_later_original_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            service = SalesPhotoService(settings(root), repo, StaticRecognizer())
            bot = telegram_bot()

            await service.handle_edited_photo(
                photo_message(caption="+998 91 765 43 21"),
                bot,
                update_id=90,
            )

            self.assertFalse(
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-large",
                    source_file_id="large",
                )
            )
            with sqlite3.connect(repo.path) as db:
                status, error = db.execute(
                    "SELECT status,last_error_code FROM sales_photo_jobs "
                    "WHERE chat_id=? AND source_message_id=?",
                    (CHAT_ID, 10),
                ).fetchone()
            self.assertEqual(
                (status, error),
                ("complete", "source_edited_before_claim"),
            )

    async def test_startup_barrier_applies_edits_before_old_retry_or_delete(self):
        for job_kind in ("failed", "delete_pending"):
            with self.subTest(job_kind=job_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = SalesPhotoRepository(root / "db.sqlite")
                old = utc_now() - timedelta(minutes=10)
                repo.claim_photo(
                    CHAT_ID,
                    10,
                    "unique-large",
                    source_file_id="large",
                    at=old,
                )
                if job_kind == "failed":
                    repo.mark_failed(CHAT_ID, 10, "temporary", at=old)
                else:
                    repo.record_replacement(CHAT_ID, 10, 200, at=old)
                    repo.mark_delete_pending(CHAT_ID, 10, "offline", at=old)

                service = SalesPhotoService(
                    settings(root),
                    repo,
                    StaticRecognizer(),
                )
                bot = telegram_bot()
                ready = False
                update_queue = SimpleNamespace(join=AsyncMock())
                barrier = asyncio.create_task(
                    service._wait_for_startup_drain(
                        lambda: ready,
                        update_queue,
                    )
                )
                await asyncio.sleep(0)
                bot.send_photo.assert_not_awaited()
                bot.delete_message.assert_not_awaited()

                await service.handle_edited_photo(
                    photo_message(caption="+998 91 765 43 21"),
                    bot,
                    update_id=91,
                )
                ready = True
                await asyncio.wait_for(barrier, timeout=2)

                await service.retry_failed_photos(bot)
                await service.retry_pending_deletions(bot)
                bot.send_photo.assert_not_awaited()
                bot.delete_message.assert_not_awaited()
                self.assertEqual(repo.retryable_photos(CHAT_ID), ())
                self.assertEqual(repo.pending_deletions(CHAT_ID), ())
                self.assertEqual(update_queue.join.await_count, 2)

    async def test_startup_backlog_photo_is_deferred_until_queued_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            service = SalesPhotoService(settings(root), repo, StaticRecognizer())
            bot = telegram_bot()
            ready = False
            update_queue = SimpleNamespace(join=AsyncMock())
            service.start_maintenance(
                bot,
                startup_ready=lambda: ready,
                update_queue=update_queue,
            )

            await service.on_photo(
                SimpleNamespace(effective_message=photo_message()),
                SimpleNamespace(bot=bot),
            )
            self.assertEqual(
                [job.source_message_id for job in repo.retryable_photos(CHAT_ID)],
                [10],
            )
            self.assertEqual(service._photo_tasks, set())
            bot.send_photo.assert_not_awaited()
            bot.delete_message.assert_not_awaited()

            await service.handle_edited_photo(
                photo_message(caption="+998 91 765 43 21"),
                bot,
                update_id=92,
            )
            ready = True
            for _ in range(30):
                if not service._startup_gate_active:
                    break
                await asyncio.sleep(0.05)
            self.assertFalse(service._startup_gate_active)
            await service.stop()

            bot.send_photo.assert_not_awaited()
            bot.delete_message.assert_not_awaited()
            self.assertEqual(repo.retryable_photos(CHAT_ID), ())
            self.assertEqual(repo.pending_deletions(CHAT_ID), ())

    async def test_startup_generated_marker_defers_source_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            repo.claim_photo(
                CHAT_ID,
                10,
                "unique-large",
                source_file_id="large",
            )
            service = SalesPhotoService(settings(root), repo, StaticRecognizer())
            bot = telegram_bot()
            service.start_maintenance(
                bot,
                startup_ready=lambda: False,
                update_queue=SimpleNamespace(join=AsyncMock()),
            )
            marker = photo_message(
                message_id=200,
                caption=BOT_CARD_MARKER + build_caption(None, ProductIdentifiers()),
            )
            marker.reply_markup = manager_keyboard(
                10,
                0,
                repo.callback_signature(CHAT_ID, 10, 0),
            )

            await service.on_photo(
                SimpleNamespace(effective_message=marker),
                SimpleNamespace(bot=bot),
            )

            self.assertTrue(repo.is_replacement(CHAT_ID, 200))
            bot.delete_message.assert_not_awaited()
            self.assertEqual(
                [job.source_message_id for job in repo.pending_deletions(CHAT_ID)],
                [10],
            )
            await service.stop()

    async def test_startup_deferred_photo_runs_immediately_after_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = SalesPhotoRepository(root / "db.sqlite")
            service = SalesPhotoService(settings(root), repo, StaticRecognizer())
            bot = telegram_bot()
            ready = False
            service.start_maintenance(
                bot,
                startup_ready=lambda: ready,
                update_queue=SimpleNamespace(join=AsyncMock()),
            )
            await service.on_photo(
                SimpleNamespace(effective_message=photo_message()),
                SimpleNamespace(bot=bot),
            )
            bot.send_photo.assert_not_awaited()

            ready = True
            for _ in range(40):
                if bot.send_photo.await_count:
                    break
                await asyncio.sleep(0.05)
            await service.stop()

            bot.send_photo.assert_awaited_once()
            bot.delete_message.assert_awaited_once_with(CHAT_ID, 10)

    async def test_stop_cancels_startup_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = SalesPhotoService(
                settings(root),
                SalesPhotoRepository(root / "db.sqlite"),
                StaticRecognizer(),
            )
            service.start_maintenance(
                telegram_bot(),
                startup_ready=lambda: False,
                update_queue=SimpleNamespace(join=AsyncMock()),
            )
            await asyncio.sleep(0)
            await service.stop()
            self.assertIsNone(service._maintenance_task)


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
            service.start_maintenance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
