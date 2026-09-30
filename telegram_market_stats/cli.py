from __future__ import annotations

import argparse
import json

from .config import MarketStatsSettings
from .repository import MarketStatsRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Telegram market statistics")
    parser.add_argument("report", choices=("models", "competitors"))
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    settings = MarketStatsSettings.load()
    repository = MarketStatsRepository(settings.db_path)
    rows = (
        repository.model_summary(days=args.days)
        if args.report == "models"
        else repository.competitor_summary(days=args.days)
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
