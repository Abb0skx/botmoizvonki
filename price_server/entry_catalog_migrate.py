"""Install autonomous catalogue metadata from a trusted worker DB export.

Usage (JSON is read from stdin, so credentials never enter argv/history):
  python -m price_server.entry_catalog_migrate --database /app/data/price_server.db

Input:
  {"categories":[{"category_id":1,"name":"..."}],
   "max_product_id":14810,"max_version_id":29600}
"""
from __future__ import annotations

import argparse
import json
import sys

from .entry_catalog import install_catalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    result = install_catalog(
        args.database, payload["categories"],
        max_product_id=payload["max_product_id"],
        max_version_id=payload["max_version_id"],
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
