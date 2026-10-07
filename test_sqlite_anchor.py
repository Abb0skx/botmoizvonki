import sqlite3
import tempfile
import unittest
from pathlib import Path

from forwarding.sqlite_anchor import SQLiteWalAnchor


class SQLiteWalAnchorTests(unittest.TestCase):
    def test_wal_files_remain_while_other_connections_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.db"
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
            anchor = SQLiteWalAnchor(path)
            try:
                anchor.start()
                anchor.start()
                for _ in range(3):
                    connection = sqlite3.connect(path)
                    connection.execute("CREATE TABLE IF NOT EXISTS sample (id INTEGER)")
                    connection.commit()
                    connection.close()
                    self.assertTrue(Path(str(path) + "-wal").exists())
                    self.assertTrue(Path(str(path) + "-shm").exists())
            finally:
                anchor.close()
                anchor.close()
