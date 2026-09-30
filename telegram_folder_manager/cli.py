from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import FOLDER_CODES
from .repository import FolderRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Telegram folder assignments")
    parser.add_argument(
        "--db", type=Path,
        default=Path(os.getenv("BUSINESS_DB_PATH", "/app/data/business_telegram.db")),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    assign = sub.add_parser("assign", help="move a Business chat to a manager folder")
    assign.add_argument("chat_id")
    assign.add_argument("folder", choices=FOLDER_CODES)
    sub.add_parser("list", help="show current assignments without client messages")
    args = parser.parse_args()
    repo = FolderRepository(args.db)
    if args.command == "assign":
        row = repo.assign(
            args.chat_id, args.folder, datetime.now(timezone.utc), source="cli"
        )
        print(f"QUEUED chat_id={row['chat_id']} folder={row['folder_code']}")
        return
    for row in repo.assignments():
        print(
            f"{row['chat_id']}\t{row['folder_code']}\t{row['source']}\t{row['updated_at']}"
        )


if __name__ == "__main__":
    main()
