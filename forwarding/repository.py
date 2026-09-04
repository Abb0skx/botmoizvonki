import json
import sqlite3

from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from .config import DeviceConfig, OperatorConfig, RouteConfig


ACTIVE_OPERATION_STATUSES = (
    "queued",
    "sending",
    "api_accepted",
    "call_started",
)

CORRELATABLE_OPERATION_STATUSES = (
    *ACTIVE_OPERATION_STATUSES,
    "unconfirmed",
)


def utc_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _row_dict(row):
    return dict(row) if row is not None else None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _phone_digits(value) -> str:
    return "".join(
        char for char in str(value or "") if char.isdigit()
    )


def _record_value(record, key):
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return None


def _sim_evidence(operation, event: dict) -> str:
    """Classify whether Android used the configured SIM."""
    expected_number = _phone_digits(operation["sim_number"])
    expected_slot = _int_or_none(operation["sim_slot"])
    numbers = {
        _phone_digits(value)
        for value in (
            _record_value(operation, "provider_src_number"),
            event.get("src_number"),
        )
        if _phone_digits(value)
    }
    slots = {
        slot
        for slot in (
            _int_or_none(
                _record_value(operation, "provider_src_slot")
            ),
            _int_or_none(event.get("src_slot")),
        )
        if slot is not None
    }

    if any(number != expected_number for number in numbers):
        return "wrong"
    if any(slot != expected_slot for slot in slots):
        return "wrong"
    if expected_number in numbers:
        return "matched"
    return "unknown"


class ForwardingRepository:
    """SQLite persistence and atomic claims for forwarding operations."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        operator: OperatorConfig,
        devices: dict[str, DeviceConfig],
        routes: dict[tuple[str, str], RouteConfig],
    ):
        self.connect = connect
        self.operator = operator
        self.devices = devices
        self.routes = routes

    def init_schema(self, conn=None) -> None:
        owns_connection = conn is None
        if conn is None:
            conn = self.connect()

        try:
            conn.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS forwarding_operators (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enable_template TEXT NOT NULL,
                    disable_number TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS forwarding_devices (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    moizvonki_user TEXT NOT NULL UNIQUE,
                    operator_code TEXT NOT NULL,
                    sim_slot INTEGER NOT NULL,
                    sim_number TEXT NOT NULL,
                    controls_enabled INTEGER NOT NULL DEFAULT 0,
                    forwarding_target_code TEXT,
                    forwarding_status TEXT NOT NULL DEFAULT 'unknown',
                    last_operation_id INTEGER,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(operator_code)
                        REFERENCES forwarding_operators(code)
                );

                CREATE TABLE IF NOT EXISTS forwarding_routes (
                    source_code TEXT NOT NULL,
                    target_code TEXT NOT NULL,
                    target_number TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(source_code, target_code),
                    FOREIGN KEY(source_code)
                        REFERENCES forwarding_devices(code),
                    FOREIGN KEY(target_code)
                        REFERENCES forwarding_devices(code)
                );

                CREATE TABLE IF NOT EXISTS forwarding_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    callback_query_id TEXT UNIQUE,
                    origin TEXT NOT NULL DEFAULT 'telegram',
                    employee_id TEXT NOT NULL,
                    employee_name TEXT NOT NULL,
                    moizvonki_user TEXT NOT NULL,
                    operator_code TEXT NOT NULL,
                    sim_slot INTEGER NOT NULL,
                    sim_number TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_employee_id TEXT,
                    target_number TEXT,
                    service_number TEXT NOT NULL,
                    request_time INTEGER NOT NULL,
                    requested_by INTEGER,
                    requested_username TEXT,
                    telegram_chat_id TEXT,
                    telegram_message_id INTEGER,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    lease_until INTEGER,
                    api_requested_at INTEGER,
                    api_http_status INTEGER,
                    api_response TEXT,
                    provider_event_action TEXT,
                    provider_db_call_id INTEGER,
                    provider_pbx_call_id TEXT,
                    provider_answered INTEGER,
                    provider_start_time INTEGER,
                    provider_end_time INTEGER,
                    provider_src_number TEXT,
                    provider_src_slot INTEGER,
                    provider_src_id INTEGER,
                    completed_at INTEGER,
                    result TEXT,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CHECK(action IN ('enable', 'disable'))
                );

                CREATE TABLE IF NOT EXISTS forwarding_control_posts (
                    local_date TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL,
                    reserved_at INTEGER NOT NULL,
                    lease_until INTEGER,
                    sent_at INTEGER,
                    pinned_at INTEGER,
                    previous_message_id INTEGER,
                    previous_unpinned_at INTEGER,
                    unpinned_at INTEGER,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS
                    idx_forwarding_operations_device_time
                ON forwarding_operations(employee_id, request_time DESC);

                CREATE INDEX IF NOT EXISTS
                    idx_forwarding_operations_status_time
                ON forwarding_operations(status, request_time);

                DROP INDEX IF EXISTS idx_forwarding_operations_db_call;
                DROP INDEX IF EXISTS idx_forwarding_operations_pbx_call;

                CREATE UNIQUE INDEX
                    idx_forwarding_operations_db_call
                ON forwarding_operations(
                    moizvonki_user,
                    provider_db_call_id
                )
                WHERE provider_db_call_id IS NOT NULL;

                CREATE UNIQUE INDEX
                    idx_forwarding_operations_pbx_call
                ON forwarding_operations(
                    moizvonki_user,
                    provider_pbx_call_id
                )
                WHERE provider_pbx_call_id IS NOT NULL
                    AND provider_pbx_call_id != '';
                """
            )
            post_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(forwarding_control_posts)"
                ).fetchall()
            }
            if "unpinned_at" not in post_columns:
                conn.execute(
                    """
                    ALTER TABLE forwarding_control_posts
                    ADD COLUMN unpinned_at INTEGER
                    """
                )
            operation_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(forwarding_operations)"
                ).fetchall()
            }
            operation_migrations = {
                "provider_src_number": "TEXT",
                "provider_src_slot": "INTEGER",
                "provider_src_id": "INTEGER",
            }
            for column, column_type in operation_migrations.items():
                if column not in operation_columns:
                    conn.execute(
                        f"""
                        ALTER TABLE forwarding_operations
                        ADD COLUMN {column} {column_type}
                        """
                    )
            self._seed_config(conn)
            conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def _seed_config(self, conn) -> None:
        now_ts = utc_timestamp()
        conn.execute(
            """
            INSERT INTO forwarding_operators (
                code, name, enable_template, disable_number, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                enable_template = excluded.enable_template,
                disable_number = excluded.disable_number,
                updated_at = excluded.updated_at
            """,
            (
                self.operator.code,
                self.operator.name,
                self.operator.enable_template,
                self.operator.disable_number,
                now_ts,
            ),
        )

        for device in self.devices.values():
            conn.execute(
                """
                INSERT INTO forwarding_devices (
                    code, name, moizvonki_user, operator_code,
                    sim_slot, sim_number, controls_enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    moizvonki_user = excluded.moizvonki_user,
                    operator_code = excluded.operator_code,
                    sim_slot = excluded.sim_slot,
                    sim_number = excluded.sim_number,
                    controls_enabled = excluded.controls_enabled
                """,
                (
                    device.code,
                    device.name,
                    device.moizvonki_user.casefold(),
                    device.operator_code,
                    device.sim_slot,
                    device.sim_number,
                    int(device.controls_enabled),
                    now_ts,
                ),
            )

        for route in self.routes.values():
            conn.execute(
                """
                INSERT INTO forwarding_routes (
                    source_code, target_code, target_number, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_code, target_code) DO UPDATE SET
                    target_number = excluded.target_number,
                    updated_at = excluded.updated_at
                """,
                (
                    route.source_code,
                    route.target_code,
                    route.target_number,
                    now_ts,
                ),
            )

    def get_device(self, code: str):
        with self.connect() as conn:
            return _row_dict(
                conn.execute(
                    "SELECT * FROM forwarding_devices WHERE code = ?",
                    (code,),
                ).fetchone()
            )

    def device_by_login(self, user_login: str | None):
        normalized = str(user_login or "").strip().casefold()
        if not normalized:
            return None
        with self.connect() as conn:
            return _row_dict(
                conn.execute(
                    """
                    SELECT *
                    FROM forwarding_devices
                    WHERE moizvonki_user = ?
                    """,
                    (normalized,),
                ).fetchone()
            )

    def list_device_states(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    device.*,
                    operation.status AS operation_status,
                    operation.action AS operation_action,
                    operation.target_employee_id AS operation_target_code,
                    operation.request_time AS operation_request_time,
                    operation.completed_at AS operation_completed_at,
                    operation.result AS operation_result,
                    operation.error AS operation_error
                FROM forwarding_devices AS device
                LEFT JOIN forwarding_operations AS operation
                    ON operation.id = device.last_operation_id
                ORDER BY CASE device.code
                    WHEN 'redmi' THEN 1
                    WHEN 'tecno' THEN 2
                    WHEN 'poco' THEN 3
                    ELSE 9
                END
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def reserve_daily_post(
        self,
        local_date: str,
        chat_id: str,
        now_ts: int,
        lease_seconds: int = 300,
    ) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM forwarding_control_posts
                WHERE local_date = ?
                """,
                (local_date,),
            ).fetchone()

            if row and row["message_id"] is not None:
                conn.commit()
                return {"claimed": False, "post": dict(row)}

            if row and int(row["lease_until"] or 0) > now_ts:
                conn.commit()
                return {"claimed": False, "post": dict(row)}

            lease_until = now_ts + lease_seconds
            if row:
                conn.execute(
                    """
                    UPDATE forwarding_control_posts
                    SET status = 'reserved', chat_id = ?, reserved_at = ?,
                        lease_until = ?, last_error = NULL, updated_at = ?
                    WHERE local_date = ?
                    """,
                    (
                        str(chat_id),
                        now_ts,
                        lease_until,
                        now_ts,
                        local_date,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO forwarding_control_posts (
                        local_date, chat_id, status, reserved_at,
                        lease_until, updated_at
                    ) VALUES (?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        local_date,
                        str(chat_id),
                        now_ts,
                        lease_until,
                        now_ts,
                    ),
                )
            conn.commit()
            return {
                "claimed": True,
                "post": self.get_daily_post(local_date),
            }

    def get_daily_post(self, local_date: str):
        with self.connect() as conn:
            return _row_dict(
                conn.execute(
                    """
                    SELECT * FROM forwarding_control_posts
                    WHERE local_date = ?
                    """,
                    (local_date,),
                ).fetchone()
            )

    def get_current_post(self):
        with self.connect() as conn:
            return _row_dict(
                conn.execute(
                    """
                    SELECT *
                    FROM forwarding_control_posts
                    WHERE message_id IS NOT NULL
                        AND status IN ('pinned', 'active')
                    ORDER BY local_date DESC
                    LIMIT 1
                    """
                ).fetchone()
            )

    def mark_post_sent(
        self,
        local_date: str,
        message_id: int,
        now_ts: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE forwarding_control_posts
                SET message_id = ?, status = 'sent', sent_at = ?,
                    lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE local_date = ?
                """,
                (message_id, now_ts, now_ts, local_date),
            )
            conn.commit()

    def mark_post_pinned(
        self,
        local_date: str,
        now_ts: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE forwarding_control_posts
                SET status = 'pinned', pinned_at = ?,
                    lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE local_date = ?
                """,
                (now_ts, now_ts, local_date),
            )
            conn.commit()

    def previous_pinned_post(self, local_date: str):
        with self.connect() as conn:
            return _row_dict(
                conn.execute(
                    """
                    SELECT *
                    FROM forwarding_control_posts
                    WHERE local_date < ?
                        AND message_id IS NOT NULL
                        AND pinned_at IS NOT NULL
                        AND unpinned_at IS NULL
                    ORDER BY local_date DESC
                    LIMIT 1
                    """,
                    (local_date,),
                ).fetchone()
            )

    def mark_post_active(
        self,
        local_date: str,
        previous_local_date: str | None,
        previous_message_id: int | None,
        now_ts: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE forwarding_control_posts
                SET status = 'active', previous_message_id = ?,
                    previous_unpinned_at = ?, last_error = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE local_date = ?
                """,
                (
                    previous_message_id,
                    now_ts,
                    now_ts,
                    local_date,
                ),
            )
            if previous_message_id is not None:
                conn.execute(
                    """
                    UPDATE forwarding_control_posts
                    SET unpinned_at = ?, updated_at = ?
                    WHERE local_date = ? AND message_id = ?
                    """,
                    (
                        now_ts,
                        now_ts,
                        previous_local_date,
                        previous_message_id,
                    ),
                )
            conn.commit()

    def mark_post_error(
        self,
        local_date: str,
        error: str,
        now_ts: int,
        release_reservation: bool = False,
        retry_seconds: int = 300,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE forwarding_control_posts
                SET status = CASE
                        WHEN message_id IS NULL THEN 'error'
                        ELSE status
                    END,
                    lease_until = CASE WHEN ? THEN NULL ELSE ? END,
                    last_error = ?, updated_at = ?
                WHERE local_date = ?
                """,
                (
                    int(release_reservation),
                    now_ts + max(1, int(retry_seconds)),
                    str(error)[:2000],
                    now_ts,
                    local_date,
                ),
            )
            conn.commit()

    def is_current_post(
        self,
        chat_id,
        message_id,
    ) -> bool:
        current = self.get_current_post()
        return bool(
            current
            and str(current["chat_id"]) == str(chat_id)
            and int(current["message_id"]) == int(message_id)
        )

    def queue_operation(
        self,
        *,
        callback_query_id: str,
        employee: DeviceConfig,
        action: str,
        target: DeviceConfig | None,
        target_number: str | None,
        service_number: str,
        requested_by: int,
        requested_username: str,
        telegram_chat_id,
        telegram_message_id: int,
        now_ts: int,
        cooldown_seconds: int,
        correlation_window_seconds: int,
    ) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            replay = conn.execute(
                """
                SELECT * FROM forwarding_operations
                WHERE callback_query_id = ?
                """,
                (callback_query_id,),
            ).fetchone()
            if replay:
                conn.commit()
                return {
                    "queued": False,
                    "reason": "replay",
                    "operation": dict(replay),
                }

            active = conn.execute(
                """
                SELECT * FROM forwarding_operations
                WHERE employee_id = ?
                    AND status IN ('queued', 'sending', 'api_accepted',
                                   'call_started')
                ORDER BY id DESC
                LIMIT 1
                """,
                (employee.code,),
            ).fetchone()
            if active:
                conn.commit()
                return {
                    "queued": False,
                    "reason": "busy",
                    "operation": dict(active),
                }

            ambiguous = conn.execute(
                """
                SELECT * FROM forwarding_operations
                WHERE employee_id = ?
                    AND status = 'unconfirmed'
                    AND attempt_count > 0
                    AND request_time > ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    employee.code,
                    now_ts - correlation_window_seconds,
                ),
            ).fetchone()
            if ambiguous:
                conn.commit()
                return {
                    "queued": False,
                    "reason": "unconfirmed",
                    "operation": dict(ambiguous),
                    "retry_after": max(
                        1,
                        int(ambiguous["request_time"])
                        + correlation_window_seconds
                        - now_ts,
                    ),
                }

            recent = conn.execute(
                """
                SELECT * FROM forwarding_operations
                WHERE employee_id = ?
                    AND COALESCE(completed_at, request_time) > ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (employee.code, now_ts - cooldown_seconds),
            ).fetchone()
            if recent:
                conn.commit()
                return {
                    "queued": False,
                    "reason": "cooldown",
                    "operation": dict(recent),
                    "retry_after": max(
                        1,
                        int(
                            recent["completed_at"]
                            or recent["request_time"]
                        )
                        + cooldown_seconds
                        - now_ts,
                    ),
                }

            cursor = conn.execute(
                """
                INSERT INTO forwarding_operations (
                    callback_query_id, origin, employee_id, employee_name,
                    moizvonki_user, operator_code, sim_slot, sim_number,
                    action, target_employee_id, target_number,
                    service_number, request_time, requested_by,
                    requested_username, telegram_chat_id,
                    telegram_message_id, status
                ) VALUES (
                    ?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, 'queued'
                )
                """,
                (
                    callback_query_id,
                    employee.code,
                    employee.name,
                    employee.moizvonki_user.casefold(),
                    employee.operator_code,
                    employee.sim_slot,
                    employee.sim_number,
                    action,
                    target.code if target else None,
                    target_number,
                    service_number,
                    now_ts,
                    requested_by,
                    requested_username[:255],
                    str(telegram_chat_id),
                    int(telegram_message_id),
                ),
            )
            operation_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE forwarding_devices
                SET forwarding_status = 'pending',
                    last_operation_id = ?, updated_at = ?
                WHERE code = ?
                """,
                (operation_id, now_ts, employee.code),
            )
            conn.commit()

        return {
            "queued": True,
            "reason": "queued",
            "operation": self.get_operation(operation_id),
        }

    def get_operation(self, operation_id: int):
        with self.connect() as conn:
            return _row_dict(
                conn.execute(
                    """
                    SELECT * FROM forwarding_operations WHERE id = ?
                    """,
                    (operation_id,),
                ).fetchone()
            )

    def claim_next_operation(
        self,
        now_ts: int,
        lease_seconds: int = 60,
    ):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            stale_rows = conn.execute(
                """
                SELECT id, employee_id
                FROM forwarding_operations
                WHERE status = 'sending' AND COALESCE(lease_until, 0) <= ?
                """,
                (now_ts,),
            ).fetchall()
            for stale in stale_rows:
                conn.execute(
                    """
                    UPDATE forwarding_operations
                    SET status = 'unconfirmed', completed_at = ?,
                        result = ?,
                        error = 'dispatch_interrupted', lease_until = NULL
                    WHERE id = ? AND status = 'sending'
                    """,
                    (
                        now_ts,
                        "Процесс перезапущен во время отправки; "
                        "автоповтор отключён",
                        stale["id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE forwarding_devices
                    SET forwarding_status = 'unknown', updated_at = ?
                    WHERE code = ? AND last_operation_id = ?
                    """,
                    (now_ts, stale["employee_id"], stale["id"]),
                )

            row = conn.execute(
                """
                SELECT * FROM forwarding_operations
                WHERE status = 'queued'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if not row:
                conn.commit()
                return None

            changed = conn.execute(
                """
                UPDATE forwarding_operations
                SET status = 'sending', attempt_count = attempt_count + 1,
                    lease_until = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_ts + lease_seconds, row["id"]),
            ).rowcount
            conn.commit()
            return self.get_operation(row["id"]) if changed else None

    def mark_api_accepted(
        self,
        operation_id: int,
        *,
        now_ts: int,
        http_status: int | None,
        response,
    ) -> None:
        response_text = json.dumps(
            response,
            ensure_ascii=False,
            default=str,
        )[:10000]
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE forwarding_operations
                SET status = 'api_accepted', api_requested_at = ?,
                    api_http_status = ?, api_response = ?,
                    lease_until = NULL,
                    result = 'Команда принята API; ожидается звонок телефона'
                WHERE id = ? AND status = 'sending'
                """,
                (now_ts, http_status, response_text, operation_id),
            )
            conn.commit()

    def mark_dispatch_error(
        self,
        operation_id: int,
        *,
        now_ts: int,
        error: str,
        ambiguous: bool,
    ) -> None:
        status = "unconfirmed" if ambiguous else "api_failed"
        result = (
            "Ответ API не получен; автоповтор отключён"
            if ambiguous
            else "МоиЗвонки отклонил команду"
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                "SELECT * FROM forwarding_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if not operation:
                return
            changed = conn.execute(
                """
                UPDATE forwarding_operations
                SET status = ?, completed_at = ?, result = ?, error = ?,
                    lease_until = NULL
                WHERE id = ? AND status = 'sending'
                """,
                (
                    status,
                    now_ts,
                    result,
                    str(error)[:4000],
                    operation_id,
                ),
            ).rowcount
            if changed:
                conn.execute(
                    """
                    UPDATE forwarding_devices
                    SET forwarding_status = 'unknown', updated_at = ?
                    WHERE code = ? AND last_operation_id = ?
                    """,
                    (now_ts, operation["employee_id"], operation_id),
                )
            conn.commit()

    def expire_operations(
        self,
        now_ts: int,
        timeout_seconds: int,
    ) -> int:
        cutoff = now_ts - timeout_seconds
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            queued_rows = conn.execute(
                """
                SELECT id, employee_id
                FROM forwarding_operations
                WHERE status = 'queued' AND request_time <= ?
                """,
                (cutoff,),
            ).fetchall()
            for row in queued_rows:
                conn.execute(
                    """
                    UPDATE forwarding_operations
                    SET status = 'unconfirmed', completed_at = ?,
                        result = ?,
                        error = 'queue_expired'
                    WHERE id = ? AND status = 'queued'
                    """,
                    (
                        now_ts,
                        "Команда устарела до отправки; "
                        "автозапуск отменён",
                        row["id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE forwarding_devices
                    SET forwarding_status = 'unknown', updated_at = ?
                    WHERE code = ? AND last_operation_id = ?
                    """,
                    (now_ts, row["employee_id"], row["id"]),
                )
            rows = conn.execute(
                """
                SELECT id, employee_id
                FROM forwarding_operations
                WHERE status IN ('api_accepted', 'call_started')
                    AND COALESCE(api_requested_at, request_time) <= ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE forwarding_operations
                    SET status = 'unconfirmed', completed_at = ?,
                        result = ?,
                        error = 'confirmation_timeout'
                    WHERE id = ?
                    """,
                    (
                        now_ts,
                        "Телефон не подтвердил завершение "
                        "служебного звонка вовремя",
                        row["id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE forwarding_devices
                    SET forwarding_status = 'unknown', updated_at = ?
                    WHERE code = ? AND last_operation_id = ?
                    """,
                    (now_ts, row["employee_id"], row["id"]),
                )
            conn.commit()
            return len(queued_rows) + len(rows)

    def correlatable_operations(
        self,
        user_login: str,
        reference_ts: int,
        window_seconds: int,
    ) -> list[dict]:
        placeholders = ",".join(
            "?" for _ in ACTIVE_OPERATION_STATUSES
        )
        params: list = [
            str(user_login or "").strip().casefold(),
            reference_ts - window_seconds,
            reference_ts + 30,
            *ACTIVE_OPERATION_STATUSES,
        ]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM forwarding_operations
                WHERE moizvonki_user = ?
                    AND request_time BETWEEN ? AND ?
                    AND attempt_count > 0
                    AND (
                        status IN ({placeholders})
                        OR status = 'unconfirmed'
                    )
                ORDER BY id DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def operation_by_provider_identity(
        self,
        user_login: str,
        event: dict,
    ):
        normalized_login = str(user_login or "").strip().casefold()
        db_call_id = event.get("db_call_id")
        pbx_call_id = str(event.get("event_pbx_call_id") or "")
        with self.connect() as conn:
            if db_call_id is not None:
                row = conn.execute(
                    """
                    SELECT * FROM forwarding_operations
                    WHERE moizvonki_user = ?
                        AND provider_db_call_id = ?
                    LIMIT 1
                    """,
                    (normalized_login, db_call_id),
                ).fetchone()
                if row:
                    return dict(row)
            if pbx_call_id:
                row = conn.execute(
                    """
                    SELECT * FROM forwarding_operations
                    WHERE moizvonki_user = ?
                        AND provider_pbx_call_id = ?
                    LIMIT 1
                    """,
                    (normalized_login, pbx_call_id),
                ).fetchone()
                if row:
                    return dict(row)
        return None

    def mark_provider_event(
        self,
        operation_id: int,
        action: str,
        event: dict,
        now_ts: int,
    ) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                "SELECT * FROM forwarding_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if not operation:
                conn.commit()
                return {"matched": False}

            db_call_id = event.get("db_call_id")
            pbx_call_id = str(event.get("event_pbx_call_id") or "")
            answered = int(event.get("answered") or 0)
            start_time = int(event.get("start_time") or 0) or None
            end_time = int(event.get("end_time") or 0) or None
            src_number = str(event.get("src_number") or "") or None
            src_slot = _int_or_none(event.get("src_slot"))
            src_id = _int_or_none(event.get("src_id"))

            duplicate = bool(
                (
                    db_call_id is not None
                    and operation["provider_db_call_id"] == db_call_id
                )
                or (
                    pbx_call_id
                    and operation["provider_pbx_call_id"] == pbx_call_id
                    and operation["provider_event_action"] == action
                )
            )

            if operation["status"] not in CORRELATABLE_OPERATION_STATUSES:
                conn.commit()
                return {
                    "matched": True,
                    "duplicate": True,
                    "terminal": action == "call.finish",
                    "operation": dict(operation),
                }

            if action in {"call.start", "call.answer"}:
                conn.execute(
                    """
                    UPDATE forwarding_operations
                    SET status = CASE
                            WHEN status = 'unconfirmed' THEN status
                            ELSE 'call_started'
                        END,
                        provider_event_action = ?,
                        provider_pbx_call_id = COALESCE(NULLIF(?, ''),
                                                       provider_pbx_call_id),
                        provider_start_time = COALESCE(?, provider_start_time),
                        provider_src_number = COALESCE(provider_src_number, ?),
                        provider_src_slot = COALESCE(provider_src_slot, ?),
                        provider_src_id = COALESCE(provider_src_id, ?),
                        result = 'Телефон начал служебный звонок'
                    WHERE id = ?
                    """,
                    (
                        action,
                        pbx_call_id,
                        start_time,
                        src_number,
                        src_slot,
                        src_id,
                        operation_id,
                    ),
                )
                conn.commit()
                return {
                    "matched": True,
                    "duplicate": duplicate,
                    "terminal": False,
                    "operation": self.get_operation(operation_id),
                }

            sim_evidence = _sim_evidence(operation, event)
            if sim_evidence == "wrong":
                terminal_status = "call_wrong_sim"
                result = (
                    "Служебный звонок выполнен через другую SIM; "
                    "нужная SIM не подтверждена"
                )
                error = "wrong_sim"
            elif answered and sim_evidence == "unknown":
                terminal_status = "call_completed_sim_unverified"
                result = (
                    "Служебный звонок завершён, но МоиЗвонки не передал "
                    "номер ожидаемой SIM"
                )
                error = "sim_identity_missing"
            elif answered:
                terminal_status = "call_completed"
                result = (
                    "Служебный звонок завершён; "
                    "статус оператора не проверен"
                )
                error = None
            else:
                terminal_status = "call_not_completed"
                result = "Служебный звонок не состоялся или был отменён"
                error = "service_call_not_answered"
            conn.execute(
                """
                UPDATE forwarding_operations
                SET status = ?, provider_event_action = ?,
                    provider_db_call_id = COALESCE(?, provider_db_call_id),
                    provider_pbx_call_id = COALESCE(NULLIF(?, ''),
                                                   provider_pbx_call_id),
                    provider_answered = ?,
                    provider_start_time = COALESCE(?, provider_start_time),
                    provider_end_time = COALESCE(?, provider_end_time),
                    provider_src_number = COALESCE(provider_src_number, ?),
                    provider_src_slot = COALESCE(provider_src_slot, ?),
                    provider_src_id = COALESCE(provider_src_id, ?),
                    completed_at = ?, result = ?, error = ?,
                    lease_until = NULL
                WHERE id = ?
                """,
                (
                    terminal_status,
                    action,
                    db_call_id,
                    pbx_call_id,
                    answered,
                    start_time,
                    end_time,
                    src_number,
                    src_slot,
                    src_id,
                    now_ts,
                    result,
                    error,
                    operation_id,
                ),
            )

            if terminal_status == "call_completed":
                forwarding_status = (
                    "enabled_unverified"
                    if operation["action"] == "enable"
                    else "disabled_unverified"
                )
                target_code = (
                    operation["target_employee_id"]
                    if operation["action"] == "enable"
                    else None
                )
            else:
                forwarding_status = "unknown"
                target_code = None

            conn.execute(
                """
                UPDATE forwarding_devices
                SET forwarding_status = ?, forwarding_target_code = ?,
                    updated_at = ?
                WHERE code = ? AND last_operation_id = ?
                """,
                (
                    forwarding_status,
                    target_code,
                    now_ts,
                    operation["employee_id"],
                    operation_id,
                ),
            )
            conn.commit()
            return {
                "matched": True,
                "duplicate": duplicate,
                "terminal": True,
                "operation": self.get_operation(operation_id),
            }

    def record_external_event(
        self,
        *,
        employee: DeviceConfig,
        action: str,
        target: DeviceConfig | None,
        target_number: str | None,
        service_number: str,
        event_action: str,
        event: dict,
        now_ts: int,
    ) -> dict:
        db_call_id = event.get("db_call_id")
        pbx_call_id = str(event.get("event_pbx_call_id") or "")
        answered = int(event.get("answered") or 0)
        src_number = str(event.get("src_number") or "") or None
        src_slot = _int_or_none(event.get("src_slot"))
        src_id = _int_or_none(event.get("src_id"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = None
            if db_call_id is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM forwarding_operations
                    WHERE moizvonki_user = ?
                        AND provider_db_call_id = ?
                    """,
                    (
                        employee.moizvonki_user.casefold(),
                        db_call_id,
                    ),
                ).fetchone()
            if not existing and pbx_call_id:
                existing = conn.execute(
                    """
                    SELECT * FROM forwarding_operations
                    WHERE moizvonki_user = ?
                        AND provider_pbx_call_id = ?
                    """,
                    (
                        employee.moizvonki_user.casefold(),
                        pbx_call_id,
                    ),
                ).fetchone()
            if existing:
                return {
                    "matched": True,
                    "duplicate": True,
                    "terminal": event_action == "call.finish",
                    "operation": dict(existing),
                }

            request_time = int(event.get("start_time") or now_ts)
            sim_evidence = _sim_evidence(
                {
                    "sim_number": employee.sim_number,
                    "sim_slot": employee.sim_slot,
                },
                event,
            )
            if event_action != "call.finish":
                status = "external_call_started"
                result = "Обнаружен внешний служебный звонок"
                error = None
            elif sim_evidence == "wrong":
                status = "call_wrong_sim"
                result = (
                    "Служебный звонок выполнен через другую SIM; "
                    "нужная SIM не подтверждена"
                )
                error = "wrong_sim"
            elif answered and sim_evidence == "unknown":
                status = "call_completed_sim_unverified"
                result = (
                    "Служебный звонок завершён, но МоиЗвонки не передал "
                    "номер ожидаемой SIM"
                )
                error = "sim_identity_missing"
            elif answered:
                status = "external_call_completed"
                result = (
                    "Внешний служебный звонок завершён; "
                    "статус оператора не проверен"
                )
                error = None
            else:
                status = "external_call_not_completed"
                result = "Служебный звонок не состоялся или был отменён"
                error = "service_call_not_answered"
            cursor = conn.execute(
                """
                INSERT INTO forwarding_operations (
                    origin, employee_id, employee_name, moizvonki_user,
                    operator_code, sim_slot, sim_number, action,
                    target_employee_id, target_number, service_number,
                    request_time, status, provider_event_action,
                    provider_db_call_id, provider_pbx_call_id,
                    provider_answered, provider_start_time,
                    provider_end_time, provider_src_number,
                    provider_src_slot, provider_src_id,
                    completed_at, result, error
                ) VALUES (
                    'external', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    employee.code,
                    employee.name,
                    employee.moizvonki_user.casefold(),
                    employee.operator_code,
                    employee.sim_slot,
                    employee.sim_number,
                    action,
                    target.code if target else None,
                    target_number,
                    service_number,
                    request_time,
                    status,
                    event_action,
                    db_call_id,
                    pbx_call_id or None,
                    answered if event_action == "call.finish" else None,
                    int(event.get("start_time") or 0) or None,
                    int(event.get("end_time") or 0) or None,
                    src_number,
                    src_slot,
                    src_id,
                    now_ts if event_action == "call.finish" else None,
                    result,
                    error,
                ),
            )
            operation_id = int(cursor.lastrowid)
            if event_action == "call.finish":
                conn.execute(
                    """
                    UPDATE forwarding_devices
                    SET forwarding_status = ?, forwarding_target_code = ?,
                        last_operation_id = ?, updated_at = ?
                    WHERE code = ? AND updated_at <= ?
                    """,
                    (
                        (
                            "enabled_unverified"
                            if status == "external_call_completed"
                            and action == "enable"
                            else (
                                "disabled_unverified"
                                if status == "external_call_completed"
                                else "unknown"
                            )
                        ),
                        (
                            target.code
                            if status == "external_call_completed"
                            and action == "enable" and target
                            else None
                        ),
                        operation_id,
                        now_ts,
                        employee.code,
                        request_time,
                    ),
                )
            conn.commit()
            return {
                "matched": True,
                "duplicate": False,
                "terminal": event_action == "call.finish",
                "operation": self.get_operation(operation_id),
            }
