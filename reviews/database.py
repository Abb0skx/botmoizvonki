import sqlite3
from pathlib import Path

from .config import REVIEWS_DB_PATH


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_MANAGERS = (
    ("olmas", "Olmas", 10),
    ("otabek", "Otabek", 20),
    ("muhammadali", "MuhammadAli", 30),
    ("abbos", "Abbos", 40),
)

REVIEW_MIGRATION_COLUMNS = {
    "accept_language": "TEXT",
    "referer": "TEXT",
    "request_headers_json": "TEXT",
    "device_data_json": "TEXT",
    "device_data_updated_at": "TEXT",
    "should_notify": "INTEGER NOT NULL DEFAULT 0",
    "notification_status": "TEXT NOT NULL DEFAULT 'not_required'",
    "notified_at": "TEXT",
    "notification_error": "TEXT",
}


def connect_reviews_db(db_path: Path | str = REVIEWS_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_reviews_db(db_path: Path | str = REVIEWS_DB_PATH) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect_reviews_db(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(schema)
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(reviews)")
        }
        for column, definition in REVIEW_MIGRATION_COLUMNS.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE reviews ADD COLUMN {column} {definition}"
                )
        connection.executemany(
            """
            INSERT INTO managers (code, name, sort_order)
            VALUES (?, ?, ?)
            ON CONFLICT(code) DO NOTHING
            """,
            DEFAULT_MANAGERS,
        )


def list_active_managers(db_path: Path | str = REVIEWS_DB_PATH) -> list[dict]:
    init_reviews_db(db_path)
    with connect_reviews_db(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, code, name
            FROM managers
            WHERE active = 1
            ORDER BY sort_order, name
            """
        ).fetchall()
    return [dict(row) for row in rows]
