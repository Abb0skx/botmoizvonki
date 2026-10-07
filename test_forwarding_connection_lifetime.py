import sqlite3
import tempfile
import unittest
from pathlib import Path

from forwarding.config import DEVICES, OPERATOR, ROUTES
from forwarding.repository import ForwardingRepository


class ForwardingConnectionLifetimeTests(unittest.TestCase):
    def test_context_closes_connection_and_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE sample (id INTEGER)")
            repository = ForwardingRepository(
                lambda: sqlite3.connect(path), OPERATOR, DEVICES, ROUTES
            )
            with self.assertRaises(RuntimeError):
                with repository.connect() as connection:
                    connection.execute("INSERT INTO sample VALUES (1)")
                    raise RuntimeError("cancel transaction")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
