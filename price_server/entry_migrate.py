"""Explicit capture/check/install commands; never runs at application startup."""
import argparse
import gzip
import json
import os
from pathlib import Path

from .entry_store import SQLitePriceSource, initialize
from .price_entry import HEADERS, SheetsPriceSource


def capture(path):
    source = SheetsPriceSource()
    suppliers = source.suppliers()
    catalogs = [source.read(s["sheet_id"]) for s in suppliers]
    # A second pass catches concurrent edits during the multi-tab read.
    for catalog in catalogs:
        again = source.read(catalog["sheet_id"])
        if ([r["revision"] for r in catalog["rows"]] != [r["revision"] for r in again["rows"]]
                or catalog["title"] != again["title"]):
            raise RuntimeError("Source changed during capture; stop writes and capture again")
    if suppliers != source.suppliers():
        raise RuntimeError("Supplier list changed during capture")
    with open(path, "xb") as handle:
        os.chmod(path, 0o600)
        with gzip.GzipFile(fileobj=handle, mode="wb") as archive:
            archive.write(json.dumps(catalogs, ensure_ascii=False).encode())
    with gzip.open(path, "rt") as archive:
        if json.load(archive) != catalogs:
            raise RuntimeError("Capture archive verification failed")
    return {"suppliers": len(catalogs), "rows": [len(c["rows"]) for c in catalogs]}


def check(db_path, catalogs):
    source = SQLitePriceSource(db_path)
    compared = 0
    for catalog in catalogs:
        actual = source.read(catalog["sheet_id"])["rows"]
        expected = catalog["rows"]
        if len(actual) != len(expected):
            raise RuntimeError("Migration row count differs")
        for before, after in zip(expected, actual):
            # MIN is calculated dynamically, not a frozen cached Sheets formula.
            if any(before[k] != after[k] for k in HEADERS if k != "min_price"):
                raise RuntimeError("Migration values differ")
            if before["revision"] != after["revision"]:
                raise RuntimeError("Migration revisions differ")
            compared += 1
    return {"status": "equal", "supplier_product_rows": compared}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "install", "check"])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.action == "capture":
        result = capture(args.archive)
    else:
        if not args.database or not args.database.is_file():
            parser.error("--database must be an existing SQLite file")
        with gzip.open(args.archive, "rt") as archive:
            catalogs = json.load(archive)
        result = (initialize(args.database, catalogs) if args.action == "install"
                  else check(args.database, catalogs))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
