"""Keep the shared call database's WAL files stable while the API is running."""

import sqlite3


class SQLiteWalAnchor:
    def __init__(self, path):
        self.path = path
        self.connection = None

    def start(self):
        if self.connection is not None:
            return
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("Call database must use WAL mode")
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except BaseException:
            connection.close()
            raise
        self.connection = connection

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()
