"""Repair recent local transcript JSON/TXT without re-running ASR.

The script prints call IDs and counts only. It never logs transcript text.
Run against the same local SQLite volume while the web app remains online.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from call_transcription.processing import (
    sanitize_transcript_dict,
    transcript_dict_to_txt,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100")

    path = Path(args.db).resolve()
    now = int(time.time())
    with sqlite3.connect(path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT transcription.call_id, transcription.transcript_json
            FROM call_transcriptions AS transcription
            JOIN calls AS call ON call.id = transcription.call_id
            WHERE transcription.status = 'completed'
              AND transcription.transcript_json IS NOT NULL
              AND COALESCE(call.is_internal_contact, 0) = 0
            ORDER BY transcription.call_id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        changed = []
        for row in rows:
            original = json.loads(row["transcript_json"])
            repaired = sanitize_transcript_dict(original)
            if repaired == original:
                continue
            conn.execute(
                """
                UPDATE call_transcriptions
                SET transcript_json = ?, transcript_txt = ?,
                    telegram_refreshed_at = NULL,
                    telegram_refresh_error = NULL,
                    telegram_refresh_next_attempt_at = 0,
                    telegram_refresh_attempts = 0,
                    updated_at = ?
                WHERE call_id = ? AND status = 'completed'
                """,
                (
                    json.dumps(repaired, ensure_ascii=False, allow_nan=False),
                    transcript_dict_to_txt(repaired),
                    now,
                    row["call_id"],
                ),
            )
            changed.append(row["call_id"])
        conn.commit()
    print(json.dumps({"checked": len(rows), "changed": len(changed), "call_ids": changed}))


if __name__ == "__main__":
    main()
