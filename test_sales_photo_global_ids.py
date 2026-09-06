from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from sales_photo_bot.repository import SalesPhotoRepository


CHAT_ID = -1001234567890


class PermanentSaleIdTests(unittest.TestCase):
    @staticmethod
    def _claim(
        repository: SalesPhotoRepository,
        source_id: int,
        sale_day: date,
    ) -> tuple[date, int, int]:
        assert repository.claim_photo(
            CHAT_ID,
            source_id,
            f"unique-{source_id}",
            source_file_id=f"file-{source_id}",
            sale_date=sale_day,
            allocate_order=False,
        )
        return repository.ensure_order_numbers(CHAT_ID, source_id, sale_day)

    def test_daily_quantity_compacts_but_permanent_id_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SalesPhotoRepository(Path(directory) / "sales.db")
            first_day = date(2026, 9, 6)

            for source_id, expected in ((10, 1), (11, 2), (12, 3)):
                self.assertEqual(
                    self._claim(repository, source_id, first_day),
                    (first_day, expected, expected),
                )
                repository.mark_reposted(CHAT_ID, source_id, source_id + 100)

            removed_day, changed = repository.mark_order_card_removed(CHAT_ID, 11)

            self.assertEqual(removed_day, first_day)
            self.assertEqual(changed, 1)
            self.assertEqual(repository.global_order_for_source(CHAT_ID, 11), 2)
            self.assertEqual(
                repository.order_numbers_for_source(CHAT_ID, 12),
                (first_day, 2, 3),
            )

            self.assertEqual(
                self._claim(repository, 13, first_day),
                (first_day, 3, 4),
            )
            second_day = date(2026, 9, 7)
            self.assertEqual(
                self._claim(repository, 14, second_day),
                (second_day, 1, 5),
            )

            reopened = SalesPhotoRepository(repository.path)
            self.assertEqual(reopened.global_order_for_source(CHAT_ID, 11), 2)
            self.assertEqual(
                reopened.order_numbers_for_source(CHAT_ID, 14),
                (second_day, 1, 5),
            )

    def test_retry_of_same_source_keeps_reserved_permanent_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SalesPhotoRepository(Path(directory) / "sales.db")
            sale_day = date(2026, 9, 6)

            first = self._claim(repository, 10, sale_day)
            second = repository.ensure_order_numbers(CHAT_ID, 10, sale_day)

            self.assertEqual(first, second)
            self.assertEqual(first[2], 1)

    def test_counter_is_atomic_across_concurrent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repository = SalesPhotoRepository(path)
            sale_day = date(2026, 9, 6)
            sources = tuple(range(100, 112))
            for source_id in sources:
                self.assertTrue(
                    repository.claim_photo(
                        CHAT_ID,
                        source_id,
                        f"unique-{source_id}",
                        source_file_id=f"file-{source_id}",
                        sale_date=sale_day,
                        allocate_order=False,
                    )
                )

            def allocate(source_id: int) -> int:
                connection = SalesPhotoRepository(path)
                return connection.ensure_order_numbers(
                    CHAT_ID,
                    source_id,
                    sale_day,
                )[2]

            with ThreadPoolExecutor(max_workers=6) as executor:
                global_ids = tuple(executor.map(allocate, sources))

            self.assertEqual(sorted(global_ids), list(range(1, len(sources) + 1)))
            self.assertEqual(len(global_ids), len(set(global_ids)))

    def test_legacy_migration_uses_publication_order_and_keeps_deleted_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repository = SalesPhotoRepository(path)
            sale_day = date(2026, 9, 6)
            for source_id, replacement_id in ((10, 300), (11, 200), (12, 400)):
                self._claim(repository, source_id, sale_day)
                repository.mark_reposted(CHAT_ID, source_id, replacement_id)
            repository.mark_order_card_removed(CHAT_ID, 11)

            # Recreate the state of a database from before permanent IDs.
            with sqlite3.connect(path) as db:
                db.execute("DROP TRIGGER trg_sales_photo_global_order_immutable")
                db.execute("UPDATE sales_photo_jobs SET global_order_id=NULL")
                db.execute(
                    "DELETE FROM sales_photo_meta WHERE key=?",
                    (f"global_order_counter:{CHAT_ID}",),
                )
                db.commit()

            migrated = SalesPhotoRepository(path)

            self.assertEqual(migrated.global_order_for_source(CHAT_ID, 11), 1)
            self.assertEqual(migrated.global_order_for_source(CHAT_ID, 10), 2)
            self.assertEqual(migrated.global_order_for_source(CHAT_ID, 12), 3)
            self.assertIsNone(migrated.daily_order_for_source(CHAT_ID, 11))
            self.assertEqual(
                [job.global_id for job in migrated.pending_order_backfills(CHAT_ID)],
                [2, 3],
            )
            self.assertEqual(
                self._claim(migrated, 13, sale_day),
                (sale_day, 3, 4),
            )

    def test_database_rejects_a_change_to_an_assigned_permanent_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.db"
            repository = SalesPhotoRepository(path)
            self._claim(repository, 10, date(2026, 9, 6))

            with sqlite3.connect(path) as db:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "global_order_id is immutable",
                ):
                    db.execute(
                        """UPDATE sales_photo_jobs SET global_order_id=99
                           WHERE chat_id=? AND source_message_id=?""",
                        (CHAT_ID, 10),
                    )

    def test_stale_daily_value_cannot_mark_a_card_as_current(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SalesPhotoRepository(Path(directory) / "sales.db")
            sale_day = date(2026, 9, 6)
            for source_id in (10, 11):
                self._claim(repository, source_id, sale_day)
                repository.mark_reposted(CHAT_ID, source_id, source_id + 100)
                repository.mark_order_card_applied(
                    CHAT_ID,
                    source_id,
                    expected_order_id=source_id - 9,
                    expected_global_id=source_id - 9,
                )

            repository.mark_order_card_removed(CHAT_ID, 10)

            self.assertFalse(
                repository.mark_order_card_applied(
                    CHAT_ID,
                    11,
                    expected_order_id=2,
                    expected_global_id=2,
                )
            )
            self.assertEqual(
                [job.source_message_id for job in repository.pending_order_backfills(
                    CHAT_ID
                )],
                [11],
            )


if __name__ == "__main__":
    unittest.main()
