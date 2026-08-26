from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .config import PriceSettings


REGISTRY_HEADERS = (
    "record_key",
    "section_key",
    "section_name",
    "channel_id",
    "channel_username",
    "part_no",
    "part_count",
    "message_id",
    "post_url",
    "snapshot_id",
    "content_hash",
    "publication_mode",
    "is_current",
    "status",
    "sent_at",
    "updated_at",
    "last_sync_at",
    "last_error",
)

POST_INDEX_HEADERS = (
    "section_key",
    "section_name",
    "position",
    "main_channel_id",
    "main_message_ids",
    "main_post_urls",
    "has_current_post",
    "preview_channel_id",
    "preview_job_ids",
    "preview_message_ids",
    "preview_execute_at",
    "updated_at",
)

CALENDAR_HEADERS = (
    "day_of_month",
    "slot",
    "subposition",
    "requested_label",
    "section_key",
    "section_name",
    "publish_time",
    "timezone",
    "enabled",
    "updated_at",
)


class RegistryNotConfigured(RuntimeError):
    """Google Sheets credentials are not available in this process."""


def _google_client():
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    content = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_SA_JSON_CONTENT")
    )
    path = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
        or os.getenv("GOOGLE_SA_JSON_PATH")
    )

    if content:
        try:
            info = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RegistryNotConfigured(
                "Google service account JSON is invalid"
            ) from exc
        credentials = Credentials.from_service_account_info(
            info,
            scopes=scopes,
        )
    elif path:
        credentials = Credentials.from_service_account_file(
            path,
            scopes=scopes,
        )
    else:
        raise RegistryNotConfigured(
            "Google service account is not configured"
        )

    return gspread.authorize(credentials)


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


@dataclass
class ProductSortPostRegistry:
    """Non-authoritative, human-readable mirror of Telegram post IDs."""

    settings: PriceSettings
    client: Any | None = None

    def _worksheet(self):
        import gspread

        client = self.client or _google_client()
        book = client.open_by_key(self.settings.product_sort_sheet_id)
        try:
            worksheet = book.worksheet(self.settings.posts_sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(
                title=self.settings.posts_sheet_name,
                rows=1000,
                cols=len(REGISTRY_HEADERS),
            )
            worksheet.update(
                range_name=(
                    f"A1:{_column_letter(len(REGISTRY_HEADERS))}1"
                ),
                values=[list(REGISTRY_HEADERS)],
                value_input_option="RAW",
            )
            worksheet.freeze(rows=1)
            worksheet.format(
                f"A1:{_column_letter(len(REGISTRY_HEADERS))}1",
                {
                    "backgroundColor": {
                        "red": 0.14,
                        "green": 0.36,
                        "blue": 0.24,
                    },
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {
                            "red": 1,
                            "green": 1,
                            "blue": 1,
                        },
                    },
                },
            )
        return worksheet

    def upsert(self, records: Iterable[Mapping[str, Any]]) -> int:
        records = list(records)
        if not records:
            return 0

        worksheet = self._worksheet()
        existing = worksheet.get_all_values()
        headers = (
            [str(value).strip() for value in existing[0]]
            if existing
            else []
        )
        if not headers:
            headers = list(REGISTRY_HEADERS)
        missing = [name for name in REGISTRY_HEADERS if name not in headers]
        if missing:
            headers.extend(missing)

        normalized_rows = [
            list(row) + [""] * max(0, len(headers) - len(row))
            for row in existing[1:]
        ]
        key_index = headers.index("record_key")
        row_by_key = {
            row[key_index]: row_number
            for row_number, row in enumerate(normalized_rows, start=2)
            if key_index < len(row) and row[key_index]
        }
        now = datetime.now(timezone.utc).isoformat()
        updates: dict[int, list[Any]] = {}
        next_row = max(2, len(existing) + 1)

        for source in records:
            payload = dict(source)
            record_key = str(payload.get("record_key") or "").strip()
            if not record_key:
                raise ValueError("Telegram registry row has no record_key")
            payload["record_key"] = record_key
            payload["last_sync_at"] = now
            row_number = row_by_key.get(record_key)
            if row_number is None:
                row_number = next_row
                next_row += 1
                row_by_key[record_key] = row_number
            updates[row_number] = [_cell(payload.get(name)) for name in headers]

        last_column = _column_letter(len(headers))
        requests = []
        if not existing or headers != [
            str(value).strip() for value in (existing[0] if existing else [])
        ]:
            requests.append(
                {
                    "range": f"A1:{last_column}1",
                    "values": [headers],
                }
            )
        for row_number, values in sorted(updates.items()):
            requests.append(
                {
                    "range": f"A{row_number}:{last_column}{row_number}",
                    "values": [values],
                }
            )

        worksheet.batch_update(
            requests,
            value_input_option="RAW",
        )
        return len(records)


@dataclass
class ProductSortPostIndex:
    """One row per current price section, including blank unsent IDs."""

    settings: PriceSettings
    client: Any | None = None

    def _worksheet(self):
        import gspread

        client = self.client or _google_client()
        book = client.open_by_key(self.settings.product_sort_sheet_id)
        try:
            worksheet = book.worksheet(self.settings.post_index_sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(
                title=self.settings.post_index_sheet_name,
                rows=1000,
                cols=len(POST_INDEX_HEADERS),
            )
            worksheet.update(
                range_name=f"A1:{_column_letter(len(POST_INDEX_HEADERS))}1",
                values=[list(POST_INDEX_HEADERS)],
                value_input_option="RAW",
            )
            worksheet.freeze(rows=1)
            worksheet.format(
                f"A1:{_column_letter(len(POST_INDEX_HEADERS))}1",
                {
                    "backgroundColor": {
                        "red": 0.12,
                        "green": 0.30,
                        "blue": 0.48,
                    },
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {
                            "red": 1,
                            "green": 1,
                            "blue": 1,
                        },
                    },
                },
            )
        return worksheet

    def upsert(self, records: Iterable[Mapping[str, Any]]) -> int:
        records = list(records)
        if not records:
            return 0
        worksheet = self._worksheet()
        existing = worksheet.get_all_values()
        headers = (
            [str(value).strip() for value in existing[0]]
            if existing else []
        )
        if not headers:
            headers = list(POST_INDEX_HEADERS)
        missing = [name for name in POST_INDEX_HEADERS if name not in headers]
        if missing:
            headers.extend(missing)
        key_index = headers.index("section_key")
        row_by_key = {
            row[key_index]: row_number
            for row_number, row in enumerate(existing[1:], start=2)
            if key_index < len(row) and row[key_index]
        }
        now = datetime.now(timezone.utc).isoformat()
        next_row = max(2, len(existing) + 1)
        updates: dict[int, list[Any]] = {}
        for source in records:
            payload = dict(source)
            section_key = str(payload.get("section_key") or "").strip()
            if not section_key:
                raise ValueError("Post index row has no section_key")
            payload["section_key"] = section_key
            payload["updated_at"] = now
            row_number = row_by_key.get(section_key)
            if row_number is None:
                row_number = next_row
                next_row += 1
                row_by_key[section_key] = row_number
            updates[row_number] = [_cell(payload.get(name)) for name in headers]

        last_column = _column_letter(len(headers))
        requests = []
        current_headers = [
            str(value).strip() for value in (existing[0] if existing else [])
        ]
        if not existing or headers != current_headers:
            requests.append({
                "range": f"A1:{last_column}1",
                "values": [headers],
            })
        for row_number, values in sorted(updates.items()):
            requests.append({
                "range": f"A{row_number}:{last_column}{row_number}",
                "values": [values],
            })
        worksheet.batch_update(requests, value_input_option="RAW")
        return len(records)


@dataclass
class ProductSortCalendarRegistry:
    """Human-readable mirror of the authoritative monthly calendar."""

    settings: PriceSettings
    client: Any | None = None

    def _worksheet(self):
        import gspread

        client = self.client or _google_client()
        book = client.open_by_key(self.settings.product_sort_sheet_id)
        try:
            worksheet = book.worksheet(self.settings.calendar_sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(
                title=self.settings.calendar_sheet_name,
                rows=1000,
                cols=len(CALENDAR_HEADERS),
            )
            worksheet.update(
                range_name=f"A1:{_column_letter(len(CALENDAR_HEADERS))}1",
                values=[list(CALENDAR_HEADERS)],
                value_input_option="RAW",
            )
            worksheet.freeze(rows=1)
        return worksheet

    def replace(self, records: Iterable[Mapping[str, Any]]) -> int:
        records = list(records)
        worksheet = self._worksheet()
        now = datetime.now(timezone.utc).isoformat()
        values = [list(CALENDAR_HEADERS)]
        for source in records:
            payload = dict(source)
            payload["updated_at"] = now
            values.append([
                _cell(payload.get(header)) for header in CALENDAR_HEADERS
            ])
        last_column = _column_letter(len(CALENDAR_HEADERS))
        worksheet.clear()
        worksheet.update(
            range_name=f"A1:{last_column}{max(1, len(values))}",
            values=values,
            value_input_option="RAW",
        )
        worksheet.freeze(rows=1)
        worksheet.format(
            f"A1:{last_column}1",
            {
                "backgroundColor": {
                    "red": 0.34,
                    "green": 0.22,
                    "blue": 0.52,
                },
                "textFormat": {
                    "bold": True,
                    "foregroundColor": {
                        "red": 1,
                        "green": 1,
                        "blue": 1,
                    },
                },
            },
        )
        return len(records)
