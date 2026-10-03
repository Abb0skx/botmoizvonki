from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from telegram_business.security import redact_payment_data

from .analyzer import MessageAnalysis


SCHEMA = """
CREATE TABLE IF NOT EXISTS market_sources (
  group_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_competitors (
  telegram_user_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_messages (
  group_id TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  sender_id TEXT,
  telegram_date TEXT NOT NULL,
  edited_at TEXT,
  text_hash TEXT NOT NULL,
  text_excerpt TEXT,
  intent TEXT NOT NULL CHECK(intent IN ('demand','supply','unknown')),
  is_competitor INTEGER NOT NULL DEFAULT 0,
  has_model INTEGER NOT NULL DEFAULT 0,
  processed_at TEXT NOT NULL,
  PRIMARY KEY(group_id,message_id)
);
CREATE INDEX IF NOT EXISTS market_messages_date_idx
  ON market_messages(telegram_date);
CREATE INDEX IF NOT EXISTS market_messages_sender_idx
  ON market_messages(sender_id,telegram_date);
CREATE TABLE IF NOT EXISTS market_model_mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  model_key TEXT NOT NULL,
  model_name TEXT NOT NULL,
  memory TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '',
  intent TEXT NOT NULL CHECK(intent IN ('demand','supply','unknown')),
  sender_id TEXT,
  is_competitor INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL,
  telegram_date TEXT NOT NULL,
  UNIQUE(group_id,message_id,model_key,memory,color),
  FOREIGN KEY(group_id,message_id)
    REFERENCES market_messages(group_id,message_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS market_mentions_stats_idx
  ON market_model_mentions(intent,telegram_date,model_key);
CREATE INDEX IF NOT EXISTS market_mentions_competitor_idx
  ON market_model_mentions(is_competitor,sender_id,telegram_date);
CREATE TABLE IF NOT EXISTS market_checkpoints (
  group_id TEXT PRIMARY KEY,
  last_message_id INTEGER NOT NULL DEFAULT 0,
  backfill_before_message_id INTEGER,
  backfill_scanned INTEGER NOT NULL DEFAULT 0,
  backfill_complete INTEGER NOT NULL DEFAULT 0,
  initialized_at TEXT,
  last_collected_at TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_collector_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  scanned INTEGER NOT NULL DEFAULT 0,
  messages_with_models INTEGER NOT NULL DEFAULT 0,
  demand_mentions INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error_type TEXT
);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class MarketStatsRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(market_checkpoints)")
            }
            additions = {
                "backfill_before_message_id": "INTEGER",
                "backfill_scanned": "INTEGER NOT NULL DEFAULT 0",
                "backfill_complete": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE market_checkpoints ADD COLUMN {name} {definition}"
                    )
            columns = {row['name'] for row in db.execute('PRAGMA table_info(market_messages)')}
            for name, definition in {
                'reply_to_message_id': 'INTEGER', 'reply_external': 'INTEGER NOT NULL DEFAULT 0',
                'sender_name': 'TEXT', 'sender_username': 'TEXT',
                'reply_metadata_loaded': 'INTEGER NOT NULL DEFAULT 0',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE market_messages ADD COLUMN {name} {definition}')
            db.execute('CREATE INDEX IF NOT EXISTS market_messages_group_date_idx ON market_messages(group_id,telegram_date)')

    def missing_quote_metadata(self, group_id: int, now: datetime, limit: int = 200) -> list[int]:
        """Hydrate historical seven-minute windows around our requests only."""
        with self.connect() as db:
            return [int(row[0]) for row in db.execute(
                """SELECT m.message_id FROM market_messages m
                   WHERE m.group_id=? AND m.reply_metadata_loaded=0 AND m.telegram_date>=?
                   AND EXISTS (
                     SELECT 1 FROM market_messages q JOIN market_competitors c ON c.telegram_user_id=q.sender_id
                     WHERE q.group_id=m.group_id AND c.label='TEXNIKACH' AND c.enabled=1
                       AND q.telegram_date<=m.telegram_date
                       AND q.telegram_date>=strftime('%Y-%m-%dT%H:%M:%S',m.telegram_date,'-7 minutes')||'+00:00')
                   ORDER BY m.message_id DESC LIMIT ?""",
                (str(group_id), _iso(now-timedelta(days=30)), limit),
            )]

    def unavailable_metadata(self, group_id: int, ids: list[int]) -> None:
        with self.connect() as db:
            db.executemany(
                'UPDATE market_messages SET reply_metadata_loaded=-1 WHERE group_id=? AND message_id=?',
                ((str(group_id), message_id) for message_id in ids),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def configure(
        self,
        group_id: int,
        title: str,
        competitors: dict[int, str],
        now: datetime,
    ) -> None:
        stamp = _iso(now)
        with self.connect() as db:
            previous_ids = {int(row[0]) for row in db.execute(
                "SELECT telegram_user_id FROM market_competitors WHERE enabled=1 AND label!='TEXNIKACH'"
            )}
            db.execute(
                """INSERT INTO market_sources(group_id,title,created_at,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET
                   title=excluded.title,updated_at=excluded.updated_at""",
                (str(group_id), title, stamp, stamp),
            )
            for user_id, label in competitors.items():
                db.execute(
                    """INSERT INTO market_competitors(
                         telegram_user_id,label,enabled,created_at,updated_at)
                       VALUES(?,?,1,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET
                       label=excluded.label,enabled=1,updated_at=excluded.updated_at""",
                    (str(user_id), label, stamp, stamp),
                )
            if competitors:
                placeholders = ",".join("?" for _ in competitors)
                db.execute(
                    f"""UPDATE market_competitors SET enabled=0,updated_at=?
                        WHERE telegram_user_id NOT IN ({placeholders})""",
                    (stamp, *(str(value) for value in competitors)),
                )
            else:
                db.execute("UPDATE market_competitors SET enabled=0,updated_at=?", (stamp,))
            competitor_ids = {user_id for user_id, label in competitors.items() if label != "TEXNIKACH"}
            for user_id in previous_ids ^ competitor_ids:
                for table in ("market_messages", "market_model_mentions"):
                    db.execute(
                        f"UPDATE {table} SET is_competitor=? WHERE sender_id=?",
                        (int(user_id in competitor_ids), str(user_id)),
                    )

    def checkpoint(self, group_id: int) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM market_checkpoints WHERE group_id=?",
                (str(group_id),),
            ).fetchone()

    def save_checkpoint(
        self,
        group_id: int,
        last_message_id: int,
        now: datetime,
        *,
        initialize: bool = False,
        backfill_before_message_id: int | None = None,
        backfill_scanned: int = 0,
        backfill_complete: bool = False,
        error: str | None = None,
    ) -> None:
        stamp = _iso(now)
        with self.connect() as db:
            db.execute(
                """INSERT INTO market_checkpoints(
                     group_id,last_message_id,backfill_before_message_id,
                     backfill_scanned,backfill_complete,initialized_at,
                     last_collected_at,last_error,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET
                     last_message_id=MAX(market_checkpoints.last_message_id,
                                         excluded.last_message_id),
                     backfill_before_message_id=COALESCE(
                         excluded.backfill_before_message_id,
                         market_checkpoints.backfill_before_message_id),
                     backfill_scanned=MAX(market_checkpoints.backfill_scanned,
                                          excluded.backfill_scanned),
                     backfill_complete=MAX(market_checkpoints.backfill_complete,
                                           excluded.backfill_complete),
                     initialized_at=COALESCE(market_checkpoints.initialized_at,
                                             excluded.initialized_at),
                     last_collected_at=excluded.last_collected_at,
                     last_error=excluded.last_error,
                     updated_at=excluded.updated_at""",
                (
                    str(group_id), int(last_message_id), backfill_before_message_id,
                    int(backfill_scanned), int(backfill_complete),
                    stamp if initialize else None, stamp,
                    error[:200] if error else None, stamp,
                ),
            )

    def start_run(self, group_id: int, now: datetime) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """INSERT INTO market_collector_runs(group_id,started_at,status)
                   VALUES(?,?,'running')""",
                (str(group_id), _iso(now)),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        now: datetime,
        *,
        scanned: int,
        messages_with_models: int,
        demand_mentions: int,
        error_type: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE market_collector_runs SET finished_at=?,scanned=?,
                   messages_with_models=?,demand_mentions=?,status=?,error_type=?
                   WHERE id=?""",
                (
                    _iso(now), scanned, messages_with_models, demand_mentions,
                    "failed" if error_type else "done", error_type, run_id,
                ),
            )

    def upsert_message(
        self,
        *,
        group_id: int,
        message_id: int,
        sender_id: int | None,
        telegram_date: datetime,
        edited_at: datetime | None,
        text: str,
        analysis: MessageAnalysis,
        competitor_ids: set[int],
        processed_at: datetime,
        reply_to_message_id: int | None = None,
        reply_external: bool = False,
        sender_name: str | None = None,
        sender_username: str | None = None,
        reply_metadata_loaded: bool = False,
    ) -> bool:
        safe = (redact_payment_data(text) or "").strip()
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        is_competitor = int(sender_id in competitor_ids if sender_id else False)
        excerpt = safe[:4000] or None
        with self.connect() as db:
            db.execute(
                """INSERT INTO market_messages(
                     group_id,message_id,sender_id,telegram_date,edited_at,
                     text_hash,text_excerpt,intent,is_competitor,has_model,processed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(group_id,message_id)
                   DO UPDATE SET sender_id=excluded.sender_id,
                     telegram_date=excluded.telegram_date,edited_at=excluded.edited_at,
                     text_hash=excluded.text_hash,text_excerpt=excluded.text_excerpt,
                     intent=excluded.intent,is_competitor=excluded.is_competitor,
                     has_model=excluded.has_model,processed_at=excluded.processed_at""",
                (
                    str(group_id), int(message_id), str(sender_id) if sender_id else None,
                    _iso(telegram_date), _iso(edited_at), digest, excerpt,
                    analysis.intent, is_competitor, int(bool(analysis.mentions)),
                    _iso(processed_at),
                ),
            )
            db.execute(
                "DELETE FROM market_model_mentions WHERE group_id=? AND message_id=?",
                (str(group_id), int(message_id)),
            )
            if reply_metadata_loaded:
                db.execute(
                    """UPDATE market_messages SET reply_to_message_id=?,reply_external=?,sender_name=?,
                       sender_username=?,reply_metadata_loaded=1 WHERE group_id=? AND message_id=?""",
                    (reply_to_message_id, int(reply_external), (sender_name or '')[:160],
                     (sender_username or '')[:64], str(group_id), int(message_id)),
                )
            for mention in analysis.mentions:
                db.execute(
                    """INSERT INTO market_model_mentions(
                         group_id,message_id,model_key,model_name,memory,color,
                         intent,sender_id,is_competitor,confidence,telegram_date)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(group_id), int(message_id), mention.model_key,
                        mention.model_name, mention.memory or "", mention.color or "",
                        analysis.intent, str(sender_id) if sender_id else None,
                        is_competitor, mention.confidence, _iso(telegram_date),
                    ),
                )
        return bool(analysis.mentions)

    def model_summary(self, *, days: int = 7, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        since = _iso(now - timedelta(days=max(1, days)))
        with self.connect() as db:
            rows = db.execute(
                """SELECT model_name,COUNT(*) AS searches,
                          COUNT(DISTINCT sender_id) AS unique_senders,
                          SUM(is_competitor) AS competitor_searches,
                          MAX(telegram_date) AS last_search_at
                   FROM market_model_mentions
                   WHERE intent='demand' AND telegram_date>=?
                   GROUP BY model_key,model_name
                   ORDER BY searches DESC,model_name COLLATE NOCASE""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def competitor_summary(
        self, *, days: int = 7, now: datetime | None = None
    ) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        since = _iso(now - timedelta(days=max(1, days)))
        with self.connect() as db:
            rows = db.execute(
                """SELECT c.telegram_user_id,c.label,
                          COUNT(m.id) AS searches,
                          COUNT(DISTINCT m.model_key) AS unique_models,
                          MAX(m.telegram_date) AS last_search_at
                   FROM market_competitors c
                   LEFT JOIN market_model_mentions m
                     ON m.sender_id=c.telegram_user_id
                    AND m.intent='demand' AND m.telegram_date>=?
                   WHERE c.enabled=1
                   GROUP BY c.telegram_user_id,c.label
                   ORDER BY searches DESC,c.label COLLATE NOCASE""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]
