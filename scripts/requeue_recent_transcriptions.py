"""Requeue a bounded set without sending SMS or creating Telegram messages."""
import argparse
import json
from pathlib import Path
import sqlite3
import time


def requeue(database, limit=10, call_ids=None):
    if not 1 <= limit <= 30:
        raise ValueError("limit: 1–30")
    now = int(time.time())
    # mode=rw avoids silently creating an empty database on a typo.
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=rw", uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        params = []
        scope = ""
        if call_ids:
            scope = " AND c.id IN (" + ",".join("?" for _ in call_ids) + ")"
            params.extend(call_ids)
        params.append(limit)
        rows = conn.execute("""SELECT t.call_id FROM call_transcriptions t JOIN calls c ON c.id=t.call_id
            WHERE t.status IN ('queued','completed','error') AND c.recording IS NOT NULL
              AND c.recording != '' AND c.duration > 0 AND COALESCE(c.is_internal_contact,0)=0"""
            + scope + " ORDER BY c.id DESC LIMIT ?", params).fetchall()
        ids = [r['call_id'] for r in rows]
        for call_id in ids:
            conn.execute("""UPDATE call_transcriptions SET status='queued', attempts=0,
                next_attempt_at=?, queued_at=0, updated_at=?, error=NULL,
                lease_token=NULL, lease_until=NULL WHERE call_id=?""", (-call_id, now, call_id))
        conn.commit()
    return ids


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--call-id", action="append", type=int)
    args = parser.parse_args()
    print(json.dumps({"queued_call_ids": requeue(args.db, args.limit, args.call_id)}))
