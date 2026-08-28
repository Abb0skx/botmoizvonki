from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

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

QUICK_LINK_HEADERS = (
    "quick_post_key",
    "role",
    "rotation_position",
    "title",
    "channel_id",
    "channel_username",
    "message_id",
    "post_url",
    "linked_section_keys",
    "linked_quick_post_keys",
    "target_message_ids",
    "target_post_urls",
    "context",
    "desired_revision",
    "applied_revision",
    "status",
    "last_render_hash",
    "last_edited_at",
    "updated_at",
    "last_sync_at",
    "last_error",
)

QUICK_LINK_ROTATION_HEADERS = (
    "rotation_id",
    "trigger_source",
    "scheduled_for",
    "local_date",
    "rotation_index",
    "secondary_quick_post_key",
    "secondary_title",
    "previous_main_message_id",
    "previous_main_post_url",
    "previous_secondary_message_id",
    "previous_secondary_post_url",
    "new_main_message_id",
    "new_main_post_url",
    "phase",
    "status",
    "pinned_at",
    "unpinned_at",
    "completed_at",
    "updated_at",
    "last_sync_at",
    "last_error",
)

BOT_SETTINGS_HEADERS = (
    "setting",
    "value",
    "updated_at",
)
BOT_SETTINGS_READ_RANGE = "A1:C100"


class RegistryNotConfigured(RuntimeError):
    """Google Sheets credentials are not available in this process."""


class BotSettingsRegistryError(RuntimeError):
    """A bot_settings read, write, or verification failed."""


class BotSettingsSchemaError(BotSettingsRegistryError):
    """The existing bot_settings sheet does not match its strict contract."""

    retryable = False


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


def _google_datetime_serial(
    value: datetime,
    timezone_name: str,
) -> tuple[float, datetime]:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("updated_at must be timezone-aware")
    local_value = value.astimezone(ZoneInfo(timezone_name)).replace(
        microsecond=0
    )
    local_naive = local_value.replace(tzinfo=None)
    epoch = datetime(1899, 12, 30)
    serial = (local_naive - epoch).total_seconds() / 86_400
    if serial < 0:
        raise ValueError("updated_at is earlier than the Google Sheets epoch")
    return serial, local_value


def _unformatted_values(worksheet: Any, range_name: str) -> list[list[Any]]:
    values = worksheet.get(
        range_name,
        value_render_option="UNFORMATTED_VALUE",
        date_time_render_option="SERIAL_NUMBER",
    )
    return [list(row) for row in values]


def _bot_settings_layout(
    values: list[list[Any]],
) -> tuple[dict[str, int], int]:
    if not values:
        raise BotSettingsSchemaError("bot_settings is empty")
    headers = list(values[0][:3])
    headers.extend([""] * (3 - len(headers)))
    if any(not isinstance(header, str) for header in headers):
        raise BotSettingsSchemaError(
            "bot_settings headers must be text"
        )
    if len(set(headers)) != len(BOT_SETTINGS_HEADERS) or set(headers) != set(
        BOT_SETTINGS_HEADERS
    ):
        raise BotSettingsSchemaError(
            "bot_settings headers must be exactly: setting, value, updated_at"
        )
    columns = {header: index for index, header in enumerate(headers)}
    setting_index = columns["setting"]
    matching_rows = []
    for row_number, source in enumerate(values[1:], start=2):
        row = list(source[:3])
        row.extend([""] * (3 - len(row)))
        if row[setting_index] == "kurs":
            matching_rows.append(row_number)
    if len(matching_rows) != 1:
        raise BotSettingsSchemaError(
            "bot_settings must contain exactly one kurs row"
        )
    return columns, matching_rows[0]


def _numeric_equal(actual: Any, expected: float) -> bool:
    if isinstance(actual, bool):
        return False
    try:
        return abs(float(actual) - float(expected)) <= 1e-9
    except (TypeError, ValueError):
        return False


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


@dataclass
class ProductSortQuickLinkRegistry:
    """Human-readable mirror of durable Telegram quick-link index posts."""

    settings: PriceSettings
    client: Any | None = None

    def _worksheet(self):
        import gspread

        client = self.client or _google_client()
        book = client.open_by_key(self.settings.product_sort_sheet_id)
        try:
            worksheet = book.worksheet(self.settings.quick_links_sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(
                title=self.settings.quick_links_sheet_name,
                rows=1000,
                cols=len(QUICK_LINK_HEADERS),
            )
            worksheet.update(
                range_name=f"A1:{_column_letter(len(QUICK_LINK_HEADERS))}1",
                values=[list(QUICK_LINK_HEADERS)],
                value_input_option="RAW",
            )
            worksheet.freeze(rows=1)
            worksheet.format(
                f"A1:{_column_letter(len(QUICK_LINK_HEADERS))}1",
                {
                    "backgroundColor": {
                        "red": 0.48,
                        "green": 0.25,
                        "blue": 0.10,
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
            headers = list(QUICK_LINK_HEADERS)
        missing = [name for name in QUICK_LINK_HEADERS if name not in headers]
        if missing:
            headers.extend(missing)
        grid_width = int(getattr(worksheet, "col_count", 0) or 0)
        if grid_width and grid_width < len(headers):
            worksheet.resize(cols=len(headers))
        normalized_rows = [
            list(row) + [""] * max(0, len(headers) - len(row))
            for row in existing[1:]
        ]
        key_index = headers.index("quick_post_key")
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
            key = str(payload.get("quick_post_key") or "").strip()
            if not key:
                raise ValueError("Quick-link registry row has no key")
            payload["quick_post_key"] = key
            payload["last_sync_at"] = now
            row_number = row_by_key.get(key)
            if row_number is None:
                row_number = next_row
                next_row += 1
                row_by_key[key] = row_number
            updates[row_number] = [
                _cell(payload.get(name)) for name in headers
            ]
        last_column = _column_letter(len(headers))
        current_headers = [
            str(value).strip() for value in (existing[0] if existing else [])
        ]
        requests = []
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
class ProductSortQuickLinkRotationRegistry:
    """Append/update mirror of durable catalogue rotation runs."""

    settings: PriceSettings
    client: Any | None = None

    def _worksheet(self):
        import gspread

        client = self.client or _google_client()
        book = client.open_by_key(self.settings.product_sort_sheet_id)
        try:
            worksheet = book.worksheet(
                self.settings.quick_link_rotations_sheet_name
            )
        except gspread.WorksheetNotFound:
            worksheet = book.add_worksheet(
                title=self.settings.quick_link_rotations_sheet_name,
                rows=1000,
                cols=len(QUICK_LINK_ROTATION_HEADERS),
            )
            worksheet.update(
                range_name=(
                    f"A1:{_column_letter(len(QUICK_LINK_ROTATION_HEADERS))}1"
                ),
                values=[list(QUICK_LINK_ROTATION_HEADERS)],
                value_input_option="RAW",
            )
            worksheet.freeze(rows=1)
            worksheet.format(
                f"A1:{_column_letter(len(QUICK_LINK_ROTATION_HEADERS))}1",
                {
                    "backgroundColor": {
                        "red": 0.20,
                        "green": 0.28,
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
            headers = list(QUICK_LINK_ROTATION_HEADERS)
        missing = [
            name for name in QUICK_LINK_ROTATION_HEADERS
            if name not in headers
        ]
        if missing:
            headers.extend(missing)
        normalized_rows = [
            list(row) + [""] * max(0, len(headers) - len(row))
            for row in existing[1:]
        ]
        key_index = headers.index("rotation_id")
        row_by_key = {
            row[key_index]: row_number
            for row_number, row in enumerate(normalized_rows, start=2)
            if key_index < len(row) and row[key_index]
        }
        now = datetime.now(timezone.utc).isoformat()
        next_row = max(2, len(existing) + 1)
        updates: dict[int, list[Any]] = {}
        for source in records:
            payload = dict(source)
            key = str(payload.get("rotation_id") or "").strip()
            if not key:
                raise ValueError("Quick-link rotation row has no ID")
            payload["rotation_id"] = key
            payload["last_sync_at"] = now
            row_number = row_by_key.get(key)
            if row_number is None:
                row_number = next_row
                next_row += 1
                row_by_key[key] = row_number
            updates[row_number] = [
                _cell(payload.get(name)) for name in headers
            ]
        last_column = _column_letter(len(headers))
        current_headers = [
            str(value).strip() for value in (existing[0] if existing else [])
        ]
        requests = []
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
class BotSettingsRegistry:
    """Strict writer for the existing Product Prices bot_settings tab."""

    settings: PriceSettings
    client: Any | None = None

    def _worksheet(self):
        import gspread

        client = self.client or _google_client()
        book = client.open_by_key(self.settings.bot_settings_sheet_id)
        try:
            return book.worksheet(self.settings.bot_settings_sheet_name)
        except gspread.WorksheetNotFound as exc:
            raise BotSettingsSchemaError(
                "bot_settings worksheet does not exist"
            ) from exc

    def update_exchange_rate(
        self,
        rate: int,
        updated_at: datetime,
    ) -> dict[str, Any]:
        if (
            isinstance(rate, bool)
            or not isinstance(rate, int)
            or not 5000 <= rate <= 50000
        ):
            raise ValueError("rate must be between 5000 and 50000")
        serial, local_updated_at = _google_datetime_serial(
            updated_at,
            self.settings.timezone,
        )
        worksheet = self._worksheet()
        values = _unformatted_values(worksheet, BOT_SETTINGS_READ_RANGE)
        columns, row_number = _bot_settings_layout(values)
        value_column = _column_letter(columns["value"] + 1)
        updated_at_column = _column_letter(columns["updated_at"] + 1)
        worksheet.batch_update(
            [
                {
                    "range": f"{value_column}{row_number}",
                    "values": [[rate]],
                },
                {
                    "range": f"{updated_at_column}{row_number}",
                    "values": [[serial]],
                },
            ],
            value_input_option="RAW",
        )

        verified = _unformatted_values(worksheet, BOT_SETTINGS_READ_RANGE)
        verified_columns, verified_row_number = _bot_settings_layout(verified)
        if verified_row_number != row_number:
            raise BotSettingsRegistryError(
                "kurs row moved while bot_settings was being updated"
            )
        verified_row = list(verified[row_number - 1][:3])
        verified_row.extend([""] * (3 - len(verified_row)))
        if not _numeric_equal(
            verified_row[verified_columns["value"]], rate
        ):
            raise BotSettingsRegistryError(
                "bot_settings exchange-rate verification failed"
            )
        if not _numeric_equal(
            verified_row[verified_columns["updated_at"]], serial
        ):
            raise BotSettingsRegistryError(
                "bot_settings updated_at verification failed"
            )
        return {
            "setting": "kurs",
            "value": rate,
            "updated_at": local_updated_at.isoformat(timespec="seconds"),
            "row_number": row_number,
        }
