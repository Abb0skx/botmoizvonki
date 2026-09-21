"""Supplier price entry. Sheets is the shared source during the transition.

Only explicit, validated price cells may be written. No Telegram or import
side effects. A durable intent is committed before the external write; an
ambiguous response is never automatically retried.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sheets_registry import _google_client

DEFAULT_SHEET = "1Obi7ZVFJKu2Lpd3l8Ij5K6nNk04CdPFJNXQdLuyep2k"
HEADERS = ("product_id", "model_name", "color", "memory", "version_id_1",
           "price_1", "version_id_12", "price_12", "min_price", "category_name")
PRICE_COLUMNS = {"price_1": 5, "price_12": 7}
MAX_CHANGES = 200


class EntryError(Exception):
    def __init__(self, code: str, status: int = 400, **details: Any):
        self.code, self.status, self.details = code, status, details
        super().__init__(code)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def price(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,6}", str(value)):
        raise EntryError("invalid_price")
    value = int(value)
    if value > 100000:
        raise EntryError("invalid_price")
    return value


def identity(row: dict) -> str:
    return ":".join(str(row[name]) for name in
                    ("product_id", "version_id_1", "version_id_12"))


def revision(row: dict) -> str:
    # Include model/colour/memory too: a reassigned identity must not receive
    # a price intended for its previous product description.
    data = [row.get(name) for name in HEADERS if name != "min_price"]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


class SheetsPriceSource:
    def __init__(self, sheet_id: str | None = None):
        self.sheet_id = sheet_id or os.getenv("PRICE_ENTRY_SHEET_ID", DEFAULT_SHEET)
        self._book = None

    def book(self):
        if self._book is not None:
            return self._book
        client = _google_client()
        client.set_timeout(12)
        self._book = client.open_by_key(self.sheet_id)
        return self._book

    def suppliers(self) -> list[dict]:
        data = self.book().fetch_sheet_metadata(params={
            "fields": "sheets(properties)"})
        result = []
        for sheet in data.get("sheets", []):
            p = sheet["properties"]
            match = re.fullmatch(r"([1-9][0-9]*)-(.+)", p["title"])
            if match and p.get("sheetType", "GRID") == "GRID":
                result.append({"sheet_id": p["sheetId"], "name": match[2],
                               "title": p["title"], "supplier_id": int(match[1])})
        return sorted(result, key=lambda item: item["supplier_id"])

    def read(self, sheet_id: int) -> dict:
        book = self.book()
        metadata = book.fetch_sheet_metadata(params={"fields": "sheets(properties)"})
        props = next((s["properties"] for s in metadata["sheets"]
                      if s["properties"]["sheetId"] == sheet_id), None)
        if not props or not re.fullmatch(r"[1-9][0-9]*-.+", props["title"]):
            raise EntryError("supplier_not_found", 404)
        if props["gridProperties"]["rowCount"] > 25000:
            raise EntryError("supplier_too_large", 409)
        title = "'" + props["title"].replace("'", "''") + "'"
        data = book.fetch_sheet_metadata(params={
            "ranges": f"{title}!A1:J{props['gridProperties']['rowCount']}",
            "includeGridData": True,
            "fields": "sheets(properties,protectedRanges,data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,dataValidation))))",
        })
        sheet = next(s for s in data["sheets"] if s["properties"]["sheetId"] == sheet_id)
        grid = sheet.get("data", [{}])[0].get("rowData", [])

        def effective(cell):
            value = cell.get("effectiveValue", cell.get("userEnteredValue", {}))
            result = next(iter(value.values()), "")
            return int(result) if isinstance(result, float) and result.is_integer() else result

        headers = [effective(c) for c in grid[0].get("values", [])] if grid else []
        if tuple(headers) != HEADERS:
            raise EntryError("supplier_schema_changed", 409)
        rows, seen = [], set()
        for index, source in enumerate(grid[1:], 2):
            cells = source.get("values", [])
            cells += [{} for _ in range(10 - len(cells))]
            values = [effective(c) for c in cells[:10]]
            if not any(values[:8]):
                continue
            row = dict(zip(HEADERS, values))
            valid = all(re.fullmatch(r"[1-9][0-9]*", str(row[k])) for k in
                        ("product_id", "version_id_1", "version_id_12"))
            row["key"] = identity(row)
            if row["key"] in seen:
                raise EntryError("duplicate_product_identity", 409)
            seen.add(row["key"])
            row["row"] = index
            row["locked"] = []
            for field, col in PRICE_COLUMNS.items():
                cell = cells[col]
                try:
                    row[field] = price(row[field])
                except EntryError:
                    row["locked"].append(field)
                if not valid or "formulaValue" in cell.get("userEnteredValue", {}) or cell.get("dataValidation"):
                    row["locked"].append(field)
                for protection in sheet.get("protectedRanges", []):
                    if protection.get("warningOnly"):
                        continue
                    bounds = protection.get("range", {})
                    if (bounds.get("startRowIndex", 0) <= index - 1 < bounds.get("endRowIndex", 25000)
                            and bounds.get("startColumnIndex", 0) <= col < bounds.get("endColumnIndex", 10)):
                        row["locked"].append(field)
            row["revision"] = revision(row)
            rows.append(row)
        return {"sheet_id": sheet_id, "title": props["title"], "rows": rows,
                "fetched_at": now(), "source": "google_sheets",
                "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit#gid={sheet_id}"}

    def write(self, sheet_id: int, changes: list[dict]) -> None:
        requests = []
        for change in changes:
            col = PRICE_COLUMNS[change["field"]]
            cell = {} if change["after"] is None else {"userEnteredValue": {"numberValue": change["after"]}}
            requests.append({"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": change["row"] - 1,
                          "endRowIndex": change["row"], "startColumnIndex": col,
                          "endColumnIndex": col + 1},
                "rows": [{"values": [cell]}], "fields": "userEnteredValue"}})
        # One atomic Sheets batch. No automatic retry after network failure.
        self.book().batch_update({"requests": requests})


class PriceEntryService:
    def __init__(self, db_path: Path, source=None):
        self.db_path = Path(db_path)
        self._transaction = None
        if source is None:
            mode = os.getenv("PRICE_ENTRY_SOURCE", "sqlite")
            if mode == "sqlite":
                from .entry_store import SQLitePriceSource
                source = SQLitePriceSource(self.db_path)
            elif mode == "google_sheets":
                source = SheetsPriceSource()
            else:
                raise EntryError("invalid_price_source", 503)
        self.source = source

    @contextmanager
    def database(self):
        if self._transaction is not None:
            yield self._transaction
            return
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS price_entry_operations (
                operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                sheet_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, status TEXT NOT NULL,
                changes_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}'
            )""")
            # SQLite indexes include rowid: supplier lookup + reverse journal
            # pagination does not need a second copy of every cell change.
            connection.execute("CREATE INDEX IF NOT EXISTS idx_price_entry_operations_supplier ON price_entry_operations(sheet_id)")
            connection.commit()
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def atomic_local_save(self):
        from .entry_store import SQLitePriceSource
        if not isinstance(self.source, SQLitePriceSource):
            yield
            return
        with self.database() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            self._transaction = self.source.connection = db
            try:
                yield
            except BaseException:
                db.rollback()
                raise
            finally:
                self._transaction = self.source.connection = None

    @contextmanager
    def write_lock(self):
        with open(str(self.db_path) + ".entry.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EntryError("save_in_progress", 409) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def history(self) -> dict:
        with self.database() as db:
            items = db.execute("SELECT * FROM price_entry_operations ORDER BY created_at DESC, rowid DESC LIMIT 100").fetchall()
        return {"operations": [{"operation_id": r["operation_id"], "sheet_id": r["sheet_id"],
                                "created_at": r["created_at"], "status": r["status"],
                                "changes": json.loads(r["changes_json"])} for r in items]}

    def cell_history(self, sheet_id: int, product_key: str, field: str, before: int = 0) -> dict:
        """Full journal for one cell, newest first; not limited to global last 100.

        Stable rowids paginate through concurrent inserts. A save cannot contain
        two changes for the same cell, so a cursor cannot split an operation.
        Uncertain Google writes remain labelled intents, not proven changes.
        """
        if (type(sheet_id) is not int or not 0 <= sheet_id < 10**12
                or not isinstance(product_key, str)
                or not re.fullmatch(r"[1-9][0-9]{0,18}:[1-9][0-9]{0,18}:[1-9][0-9]{0,18}", product_key)
                or field not in PRICE_COLUMNS
                or type(before) is not int or not 0 <= before < 2**63):
            raise EntryError("invalid_history_cell")
        with self.database() as db:
            items = db.execute("""SELECT o.rowid AS sequence, o.operation_id,
                    o.created_at, o.status, c.value AS change_json
                FROM price_entry_operations o, json_each(o.changes_json) c
                WHERE o.sheet_id=? AND o.rowid < ?
                  AND json_extract(c.value, '$.key')=?
                  AND json_extract(c.value, '$.field')=?
                ORDER BY o.rowid DESC LIMIT 51""",
                (sheet_id, before or 2**63 - 1, product_key, field)).fetchall()
        entries = []
        for row in items[:50]:
            change = json.loads(row["change_json"])
            entries.append({"operation_id": row["operation_id"],
                            "created_at": row["created_at"], "status": row["status"],
                            "before": change["before"], "after": change["after"]})
        return {"sheet_id": sheet_id, "product_key": product_key, "field": field,
                "entries": entries,
                "next_before": items[49]["sequence"] if len(items) > 50 else None}

    def finish(self, operation_id, status, result):
        with self.database() as db:
            db.execute("UPDATE price_entry_operations SET status=?, updated_at=?, result_json=? WHERE operation_id=?",
                       (status, now(), json.dumps(result, ensure_ascii=False), operation_id))

    def save(self, sheet_id: int, body: dict, operation_id: str) -> dict:
        if os.getenv("PRICE_ENTRY_WRITES_PAUSED", "").lower() in {"1", "true", "yes"}:
            raise EntryError("price_entry_maintenance", 503)
        try:
            uuid.UUID(operation_id)
        except (ValueError, TypeError, AttributeError):
            raise EntryError("idempotency_key_required") from None
        if not isinstance(body, dict) or set(body) != {"changes"}:
            raise EntryError("invalid_request")
        edits = body["changes"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_CHANGES:
            raise EntryError("invalid_change_count")
        request_hash = hashlib.sha256(json.dumps([sheet_id, body], sort_keys=True).encode()).hexdigest()
        with self.write_lock(), self.atomic_local_save():
            with self.database() as db:
                existing = db.execute("SELECT * FROM price_entry_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise EntryError("idempotency_conflict", 409)
                if existing["status"] == "applied":
                    return json.loads(existing["result_json"])
                raise EntryError("save_outcome_unknown", 409, operation_id=operation_id)
            current = self.source.read(sheet_id)
            rows = {row["key"]: row for row in current["rows"]}
            changes, conflicts, seen = [], [], set()
            for edit in edits:
                if not isinstance(edit, dict) or set(edit) != {"key", "field", "revision", "value"}:
                    raise EntryError("invalid_request")
                if not all(isinstance(edit[k], str) for k in ("key", "field", "revision")):
                    raise EntryError("invalid_request")
                field, key = edit["field"], edit["key"]
                if field not in PRICE_COLUMNS or (key, field) in seen:
                    raise EntryError("invalid_or_duplicate_field")
                seen.add((key, field))
                value = price(edit["value"])
                row = rows.get(key)
                if not row or row["revision"] != edit["revision"]:
                    conflicts.append({"key": key, "field": field, "current": row.get(field) if row else None})
                    continue
                if field in row["locked"]:
                    raise EntryError("cell_read_only", 409, key=key, field=field)
                if row[field] != value:
                    changes.append({"key": key, "row": row["row"], "field": field,
                                    "before": row[field], "after": value,
                                    "model": row["model_name"], "memory": row["memory"], "color": row["color"]})
            if conflicts:
                raise EntryError("prices_changed", 409, conflicts=conflicts)
            if not changes:
                return {"status": "unchanged", "changed": 0}
            with self.database() as db:
                db.execute("INSERT INTO price_entry_operations (operation_id,request_hash,sheet_id,created_at,updated_at,status,changes_json) VALUES (?,?,?,?,?,?,?)",
                           (operation_id, request_hash, sheet_id, now(), now(), "sending", json.dumps(changes, ensure_ascii=False)))
            try:
                self.source.write(sheet_id, changes)
                verify = {row["key"]: row for row in self.source.read(sheet_id)["rows"]}
                if any(c["key"] not in verify or verify[c["key"]][c["field"]] != c["after"] for c in changes):
                    raise EntryError("verification_failed", 409)
            except Exception:
                self.finish(operation_id, "uncertain", {"status": "uncertain"})
                raise EntryError("save_outcome_unknown", 409, operation_id=operation_id) from None
            result = {"status": "applied", "changed": len(changes), "operation_id": operation_id,
                      "saved_at": now(), "import": "next_scheduled_import"}
            self.finish(operation_id, "applied", result)
            return result
