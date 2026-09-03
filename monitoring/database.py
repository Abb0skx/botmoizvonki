from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredSession:
    token_hash: str
    telegram_user_id: int
    display_name: str
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    csrf_token_hash: str


class MonitoringStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitoring_oauth_attempts (
                    state_hash TEXT PRIMARY KEY,
                    browser_token_hash TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    next_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_monitoring_oauth_expiry
                ON monitoring_oauth_attempts(expires_at);

                CREATE TABLE IF NOT EXISTS monitoring_sessions (
                    token_hash TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    idle_expires_at TEXT NOT NULL,
                    absolute_expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revocation_reason TEXT,
                    csrf_token_hash TEXT NOT NULL,
                    user_agent_hash TEXT,
                    created_ip_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_monitoring_sessions_user
                ON monitoring_sessions(telegram_user_id, revoked_at);
                CREATE INDEX IF NOT EXISTS idx_monitoring_sessions_expiry
                ON monitoring_sessions(absolute_expires_at);

                CREATE TABLE IF NOT EXISTS monitoring_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    telegram_user_id INTEGER,
                    role TEXT,
                    route TEXT,
                    result TEXT NOT NULL,
                    correlation_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitoring_audit_created
                ON monitoring_audit_log(created_at);
                """
            )
            database.execute("PRAGMA optimize")

    def create_oauth_attempt(
        self,
        *,
        state: str,
        browser_token: str,
        nonce: str,
        code_verifier: str,
        next_path: str,
        now: datetime | None = None,
        ttl_seconds: int = 600,
    ) -> None:
        current = now or _utc_now()
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO monitoring_oauth_attempts(
                    state_hash, browser_token_hash, nonce, code_verifier,
                    next_path, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash(state), _hash(browser_token), nonce, code_verifier,
                    next_path, _iso(current),
                    _iso(current + timedelta(seconds=ttl_seconds)),
                ),
            )

    def consume_oauth_attempt(
        self,
        *,
        state: str,
        browser_token: str,
        now: datetime | None = None,
    ) -> dict | None:
        current = now or _utc_now()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """
                SELECT * FROM monitoring_oauth_attempts
                WHERE state_hash=? AND browser_token_hash=?
                """,
                (_hash(state), _hash(browser_token)),
            ).fetchone()
            if (
                not row
                or row["consumed_at"] is not None
                or _parse(row["expires_at"]) <= current
            ):
                database.rollback()
                return None
            cursor = database.execute(
                """
                UPDATE monitoring_oauth_attempts SET consumed_at=?
                WHERE state_hash=? AND consumed_at IS NULL
                """,
                (_iso(current), _hash(state)),
            )
            if cursor.rowcount != 1:
                database.rollback()
                return None
            database.commit()
            return {
                "nonce": row["nonce"],
                "code_verifier": row["code_verifier"],
                "next_path": row["next_path"],
            }

    def create_session(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        absolute_ttl_seconds: int,
        idle_ttl_seconds: int,
        user_agent: str = "",
        ip_address: str = "",
        now: datetime | None = None,
    ) -> tuple[str, str]:
        current = now or _utc_now()
        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO monitoring_sessions(
                    token_hash, telegram_user_id, display_name, created_at,
                    last_seen_at, idle_expires_at, absolute_expires_at,
                    csrf_token_hash, user_agent_hash, created_ip_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash(raw_token), telegram_user_id, display_name[:160],
                    _iso(current), _iso(current),
                    _iso(current + timedelta(seconds=idle_ttl_seconds)),
                    _iso(current + timedelta(seconds=absolute_ttl_seconds)),
                    _hash(csrf_token),
                    _hash(user_agent[:1000]) if user_agent else None,
                    _hash(ip_address[:100]) if ip_address else None,
                ),
            )
        return raw_token, csrf_token

    def get_session(
        self,
        raw_token: str,
        *,
        idle_ttl_seconds: int,
        now: datetime | None = None,
    ) -> StoredSession | None:
        if not raw_token or len(raw_token) > 256:
            return None
        current = now or _utc_now()
        token_hash = _hash(raw_token)
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM monitoring_sessions WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if (
                not row
                or row["revoked_at"] is not None
                or _parse(row["idle_expires_at"]) <= current
                or _parse(row["absolute_expires_at"]) <= current
            ):
                return None
            last_seen = _parse(row["last_seen_at"])
            if current - last_seen >= timedelta(minutes=5):
                next_idle = min(
                    current + timedelta(seconds=idle_ttl_seconds),
                    _parse(row["absolute_expires_at"]),
                )
                database.execute(
                    """
                    UPDATE monitoring_sessions
                    SET last_seen_at=?, idle_expires_at=?
                    WHERE token_hash=? AND revoked_at IS NULL
                    """,
                    (_iso(current), _iso(next_idle), token_hash),
                )
                last_seen = current
                idle_expires = next_idle
            else:
                idle_expires = _parse(row["idle_expires_at"])
            return StoredSession(
                token_hash=token_hash,
                telegram_user_id=int(row["telegram_user_id"]),
                display_name=row["display_name"],
                created_at=_parse(row["created_at"]),
                last_seen_at=last_seen,
                idle_expires_at=idle_expires,
                absolute_expires_at=_parse(row["absolute_expires_at"]),
                csrf_token_hash=row["csrf_token_hash"],
            )

    def revoke_session(
        self,
        raw_token: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        if not raw_token:
            return False
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE monitoring_sessions
                SET revoked_at=?, revocation_reason=?
                WHERE token_hash=? AND revoked_at IS NULL
                """,
                (_iso(now or _utc_now()), reason[:100], _hash(raw_token)),
            )
            return cursor.rowcount == 1

    def revoke_user_sessions(self, telegram_user_id: int, *, reason: str) -> int:
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE monitoring_sessions
                SET revoked_at=?, revocation_reason=?
                WHERE telegram_user_id=? AND revoked_at IS NULL
                """,
                (_iso(_utc_now()), reason[:100], telegram_user_id),
            )
            return cursor.rowcount

    def audit(
        self,
        event: str,
        *,
        result: str,
        telegram_user_id: int | None = None,
        role: str | None = None,
        route: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO monitoring_audit_log(
                    event, telegram_user_id, role, route, result,
                    correlation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event[:80], telegram_user_id, role,
                    (route or "")[:300], result[:80],
                    (correlation_id or "")[:100], _iso(_utc_now()),
                ),
            )

    def cleanup(self, now: datetime | None = None) -> None:
        current = now or _utc_now()
        oauth_cutoff = _iso(current - timedelta(days=1))
        session_cutoff = _iso(current - timedelta(days=30))
        with self._connect() as database:
            database.execute(
                "DELETE FROM monitoring_oauth_attempts WHERE expires_at < ?",
                (oauth_cutoff,),
            )
            database.execute(
                """
                DELETE FROM monitoring_sessions
                WHERE absolute_expires_at < ? OR revoked_at < ?
                """,
                (session_cutoff, session_cutoff),
            )


__all__ = ["MonitoringStore", "StoredSession"]
