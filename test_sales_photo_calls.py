from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from sales_photo_bot.calls import CallReader
from sales_photo_bot.config import Settings
from sales_photo_bot.dates import TASHKENT_TZ
from sales_photo_bot.repository import SalesPhotoRepository
from sales_photo_bot.service import SalesPhotoService


CHAT_ID = -1001234567890
TOKEN = "1234567890:" + "A" * 35
SALE_DATE = date(2026, 9, 3)


def create_calls_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE calls(
                id INTEGER PRIMARY KEY,
                client_number TEXT,
                client_key TEXT,
                answered INTEGER,
                duration INTEGER,
                start_time INTEGER,
                duplicate_of_call_id INTEGER,
                is_internal_contact INTEGER,
                talk_manager_code TEXT,
                talk_manager_name TEXT
            );
            """
        )


def timestamp(hour: int = 12) -> int:
    return int(
        datetime(2026, 9, 3, hour, 0, tzinfo=TASHKENT_TZ).timestamp()
    )


def add_call(
    path: Path,
    call_id: int,
    manager: str,
    *,
    phone: str = "+998901234567",
    at: int | None = None,
    answered: int = 1,
    duration: int = 30,
    duplicate_of_call_id: int | None = None,
    internal: int = 0,
) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                call_id,
                phone,
                phone,
                answered,
                duration,
                at if at is not None else timestamp(),
                duplicate_of_call_id,
                internal,
                manager.casefold(),
                manager,
            ),
        )


def create_sale(repo: SalesPhotoRepository, source_id: int, replacement_id: int) -> None:
    repo.claim_photo(
        CHAT_ID,
        source_id,
        f"unique-{source_id}",
        source_file_id=f"file-{source_id}",
        sale_date=SALE_DATE,
    )
    repo.mark_reposted(CHAT_ID, source_id, replacement_id)
    repo.mark_complete(CHAT_ID, source_id)
    repo.sync_sale_details(
        CHAT_ID,
        replacement_id,
        ("+998 90 123 45 67",),
        "Phone",
    )


class CallReaderTests(unittest.TestCase):
    def test_same_manager_can_match_many_calls_and_many_sales(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.db"
            create_calls_db(path)
            add_call(path, 1, "Otabek", at=timestamp(10))
            add_call(path, 2, "Otabek", at=timestamp(14))

            index = CallReader(path).index(SALE_DATE, SALE_DATE)
            first = index.match(("90 123 45 67",), SALE_DATE)
            second = index.match(("+998901234567",), SALE_DATE)

            self.assertEqual(first, second)
            self.assertEqual(first.manager, "Otabek")
            self.assertEqual(first.call_ids, (1, 2))
            self.assertFalse(first.ambiguous)

    def test_different_managers_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.db"
            create_calls_db(path)
            add_call(path, 1, "Otabek")
            add_call(path, 2, "Ali")

            match = CallReader(path).index(SALE_DATE, SALE_DATE).match(
                ("+998 90 123 45 67",),
                SALE_DATE,
            )

            self.assertIsNone(match.manager)
            self.assertTrue(match.ambiguous)
            self.assertEqual(match.call_ids, (1, 2))

    def test_unanswered_duplicate_internal_and_zero_duration_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.db"
            create_calls_db(path)
            add_call(path, 1, "Olmas", answered=0)
            add_call(path, 2, "Olmas", duration=0)
            add_call(path, 3, "Olmas", duplicate_of_call_id=99)
            add_call(path, 4, "Olmas", internal=1)

            match = CallReader(path).index(SALE_DATE, SALE_DATE).match(
                ("+998901234567",),
                SALE_DATE,
            )

            self.assertEqual(match.matched_count, 0)
            self.assertIsNone(match.manager)


class ManagerPriorityTests(unittest.TestCase):
    def test_call_wins_over_delivery_then_falls_back_to_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            create_sale(repo, 10, 200)
            repo.upsert_delivery_link(
                CHAT_ID,
                10,
                delivery_order_id=1,
                delivery_order_number=1,
                matched_phone="+998 90 123 45 67",
                sale_date=SALE_DATE,
            )
            repo.sync_auto_delivery_manager(CHAT_ID, 10, "Abbos")

            repo.sync_auto_call_manager(
                CHAT_ID,
                10,
                "Otabek",
                sale_date=SALE_DATE,
                call_ids=(1,),
            )
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Otabek")
            self.assertEqual(repo.manager_state(CHAT_ID, 10).source, "call")

            repo.sync_auto_call_manager(
                CHAT_ID,
                10,
                None,
                sale_date=SALE_DATE,
                call_ids=(1, 2),
                ambiguous=True,
            )
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Abbos")
            self.assertEqual(repo.manager_state(CHAT_ID, 10).source, "delivery")

    def test_manual_choice_is_never_overwritten_by_call(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SalesPhotoRepository(Path(directory) / "sales.db")
            create_sale(repo, 10, 200)
            self.assertTrue(repo.reserve_ui_transition(CHAT_ID, 200, 0))
            self.assertTrue(
                repo.commit_reserved_manager_selection(CHAT_ID, 200, "Ali", 0)
            )
            self.assertTrue(repo.mark_delivery_manager_manual(CHAT_ID, 200))

            _, changed = repo.sync_auto_call_manager(
                CHAT_ID,
                10,
                "Olmas",
                sale_date=SALE_DATE,
                call_ids=(1,),
            )

            self.assertFalse(changed)
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Ali")
            self.assertTrue(repo.manager_state(CHAT_ID, 10).manual_override)


class CallSyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_sync_updates_all_cards_for_one_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls_path = root / "calls.db"
            create_calls_db(calls_path)
            add_call(calls_path, 1, "Otabek")
            repo = SalesPhotoRepository(root / "sales.db")
            create_sale(repo, 10, 200)
            create_sale(repo, 11, 201)
            service = SalesPhotoService(
                Settings(
                    bot_token=TOKEN,
                    chat_id=CHAT_ID,
                    db_path=root / "sales.db",
                    heartbeat_path=root / "heartbeat",
                    calls_db_path=calls_path,
                ),
                repo,
            )
            bot = AsyncMock()

            changed = await service.sync_call_managers(
                bot,
                reference_date=SALE_DATE,
            )

            self.assertEqual(changed, 2)
            self.assertEqual(repo.selected_manager(CHAT_ID, 200), "Otabek")
            self.assertEqual(repo.selected_manager(CHAT_ID, 201), "Otabek")
            self.assertEqual(bot.edit_message_reply_markup.await_count, 2)


if __name__ == "__main__":
    unittest.main()
