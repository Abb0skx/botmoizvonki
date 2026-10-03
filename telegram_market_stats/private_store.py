"""Small durable supplier-inbox store in the separate market database."""
import json

from .quotes import is_request

SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_private_messages (
 chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, telegram_date TEXT NOT NULL,
 text_excerpt TEXT NOT NULL, sender_name TEXT, sender_username TEXT,
 outgoing INTEGER NOT NULL DEFAULT 0, reply_to_message_id INTEGER,
 forwarded INTEGER NOT NULL DEFAULT 0, forward_from_id TEXT, forward_date TEXT,
 forward_group_id TEXT, forward_message_id INTEGER, mentions_json TEXT NOT NULL DEFAULT '[]',
 edited_at TEXT, deleted_at TEXT, processed_at TEXT NOT NULL,
 PRIMARY KEY(chat_id,message_id));
CREATE INDEX IF NOT EXISTS market_private_date_idx ON market_private_messages(telegram_date);
CREATE TABLE IF NOT EXISTS market_private_cursors (
 chat_id TEXT PRIMARY KEY, last_message_id INTEGER NOT NULL DEFAULT 0,
 before_id INTEGER, backfill_complete INTEGER NOT NULL DEFAULT 0,
 polled_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS market_private_status (
 id INTEGER PRIMARY KEY CHECK(id=1), last_scan_at TEXT, error TEXT, next_retry_at TEXT);
CREATE TABLE IF NOT EXISTS market_private_links (
 chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, request_key TEXT NOT NULL,
 message_fingerprint TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
 assigned_by TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS market_private_link_requests (
 operation_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, assigned_by TEXT NOT NULL,
 created_at TEXT NOT NULL);
'''


def load_requests(db, since, until):
    rows = [dict(r) for r in db.execute('''SELECT m.* FROM market_messages m
      JOIN market_competitors c ON c.telegram_user_id=m.sender_id AND c.enabled=1 AND c.label='TEXNIKACH'
      WHERE m.telegram_date>=? AND m.telegram_date<=? ORDER BY m.telegram_date,m.message_id''', (since, until))]
    indexed = {(r['group_id'], r['message_id']): r for r in rows}
    for row in rows:
        row['mentions'] = []
    for r in db.execute('''SELECT m.group_id,m.message_id,m.model_key,m.memory,m.color
      FROM market_model_mentions m JOIN market_competitors c
      ON c.telegram_user_id=m.sender_id AND c.enabled=1 AND c.label='TEXNIKACH'
      WHERE m.telegram_date>=? AND m.telegram_date<=?''', (since, until)):
        target = indexed.get((r['group_id'], r['message_id']))
        if target is not None:
            target['mentions'].append(dict(r))
    own = {r['sender_id'] for r in rows}
    return [r for r in rows if is_request(r, own)]


def load_messages(db, since, until):
    rows = [dict(r) for r in db.execute('''SELECT * FROM market_private_messages
        WHERE telegram_date>=? AND telegram_date<=? AND deleted_at IS NULL
        ORDER BY telegram_date,message_id''', (since, until))]
    for row in rows:
        row['mentions'] = json.loads(row.pop('mentions_json'))
    return rows


class PrivateStore:
    def __init__(self, repository):
        self.repo = repository
        with self.repo.connect() as db:
            db.executescript(SCHEMA)

    def requests(self, since, until):
        with self.repo.connect() as db:
            return load_requests(db, since, until)

    def cursors(self):
        with self.repo.connect() as db:
            return {r['chat_id']: dict(r) for r in db.execute('SELECT * FROM market_private_cursors')}

    def recheck_ids(self, chat, since):
        with self.repo.connect() as db:
            return [r[0] for r in db.execute('''SELECT message_id FROM market_private_messages
              WHERE chat_id=? AND telegram_date>=? AND deleted_at IS NULL
              ORDER BY processed_at,message_id LIMIT 25''', (chat, since))]

    def save(self, chat, messages, last_id, before_id, complete, now, removed=()):
        with self.repo.connect() as db:
            db.executemany('UPDATE market_private_messages SET deleted_at=?,processed_at=? WHERE chat_id=? AND message_id=?',
                           ((now, now, chat, mid) for mid in removed))
            for row in messages:
                fields = list(row)
                db.execute(f'''INSERT INTO market_private_messages({','.join(fields)})
                    VALUES({','.join('?' for _ in fields)}) ON CONFLICT(chat_id,message_id)
                    DO UPDATE SET {','.join(f'{f}=excluded.{f}' for f in fields if f not in ('chat_id','message_id'))}''', list(row.values()))
            db.execute('''INSERT INTO market_private_cursors VALUES(?,?,?,?,?,NULL)
              ON CONFLICT(chat_id) DO UPDATE SET last_message_id=MAX(last_message_id,excluded.last_message_id),
              before_id=excluded.before_id,backfill_complete=excluded.backfill_complete,
              polled_at=excluded.polled_at,error=NULL''', (chat, last_id, before_id, int(complete), now))

    def status(self, now, error=None, retry_at=None):
        with self.repo.connect() as db:
            db.execute('''INSERT INTO market_private_status VALUES(1,?,?,?)
              ON CONFLICT(id) DO UPDATE SET last_scan_at=excluded.last_scan_at,
              error=excluded.error,next_retry_at=excluded.next_retry_at''', (now, error, retry_at))

    def retry_at(self):
        with self.repo.connect() as db:
            row = db.execute('SELECT next_retry_at FROM market_private_status WHERE id=1').fetchone()
            return row[0] if row else None
