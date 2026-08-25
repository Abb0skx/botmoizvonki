from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from datetime import time as day_time
from typing import Any

from .statistics import statistics_rows


LOG = logging.getLogger("telegram_business.scheduler")
_RETRY_AFTER = re.compile(r"retry[_ -]?after\s*[=:]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def retry_after_seconds(error: BaseException) -> float | None:
    """Read a transport-neutral Retry-After contract from an exception."""
    value = getattr(error, "retry_after", None)
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    response = getattr(error, "response", None)
    if response is not None:
        header = getattr(response, "headers", {}).get("Retry-After")
        if header:
            try:
                return max(0.0, float(header))
            except (TypeError, ValueError):
                pass
    match = _RETRY_AFTER.search(str(error))
    return float(match.group(1)) if match else None


class DurableScheduler:
    def __init__(self, service, poll_seconds: float = 1.0):
        self.service = service
        self.poll_seconds = poll_seconds
        self.task = None
        self._sheets_task: asyncio.Task | None = None
        self._next_sheets = 0.0
        self._stop_event: asyncio.Event | None = None

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = None
        try:
            recovered = self.service.repo.recover_stale(self.service.clock())
            if any(recovered.values()):
                LOG.info("telegram_business_recovered %s", recovered)
        except Exception as exc:
            self._record_error("scheduler", "startup_recovery", exc)
            LOG.error("telegram_business_recovery_failed type=%s", type(exc).__name__)
        self._stop_event = asyncio.Event()
        self.task = asyncio.create_task(self.run(), name="telegram-business-scheduler")

    async def stop(self):
        if self.task:
            if self._stop_event:
                self._stop_event.set()
            try:
                # Let the currently claimed database item finish and release its
                # lease. Network calls have their own short timeouts.
                await asyncio.wait_for(asyncio.shield(self.task), timeout=30)
            except asyncio.TimeoutError:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            self.task = None
        sheets_task = self._sheets_task
        if sheets_task:
            try:
                await asyncio.wait_for(asyncio.shield(sheets_task), timeout=30)
            except asyncio.TimeoutError:
                sheets_task.cancel()
                try:
                    await sheets_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            self._sheets_task = None
        self._stop_event = None

    async def _process_updates(self, now) -> None:
        # Claim immediately before execution. Claiming a large batch gives later
        # rows an ageing lease and allows another replica to steal them.
        for _ in range(50):
            if self._stop_event and self._stop_event.is_set():
                break
            rows = self.service.repo.claim_due_updates(
                self.service.clock(), limit=1, lease_seconds=120
            )
            if not rows:
                break
            claimed = rows[0]
            update_id = claimed["update_id"]
            token = claimed["lease_token"]
            try:
                payload = json.loads(claimed["raw_payload"])
                with self.service.repo.bind_update_claim(update_id, token):
                    await asyncio.to_thread(self.service.process_update, payload)
                current = self.service.repo.update(update_id)
                # BusinessService records domain outcome. An internal error is
                # converted back to a retry instead of silently becoming terminal.
                if current and current["status"] == "error" and current["lease_token"] == token:
                    self.service.repo.retry_update(
                        update_id,
                        self.service.clock(),
                        current["error"] or "business update failed",
                        lease_token=token,
                    )
                elif current and current["status"] == "running" and current["lease_token"] == token:
                    self.service.repo.complete_update_claim(
                        update_id, self.service.clock(), token
                    )
            except Exception as exc:
                retry_after = retry_after_seconds(exc)
                self.service.repo.retry_update(
                    update_id,
                    self.service.clock(),
                    str(exc),
                    lease_token=token,
                    retry_after=retry_after,
                )
                self._record_error(
                    "telegram_update_worker", "process_update", exc,
                    attempts=claimed["attempts"],
                )
                LOG.error(
                    "business_update_retry update_id=%s type=%s",
                    update_id,
                    type(exc).__name__,
                )

    async def _process_actions(self, now) -> None:
        for _ in range(50):
            if self._stop_event and self._stop_event.is_set():
                break
            rows = self.service.repo.claim_due_actions(
                self.service.clock(), limit=1, lease_seconds=300
            )
            if not rows:
                break
            action = rows[0]
            action_id = action["action_id"]
            token, generation = action["lease_token"], action["generation"]
            if not self.service.repo.action_is_current(
                action_id, token, generation, self.service.clock()
            ):
                continue
            LOG.info(
                "scheduled_action_claimed id=%s action_type=%s chat_id=%s session_id=%s attempt=%s",
                action_id,
                action["action_type"],
                action["chat_id"],
                action["session_id"],
                action["attempts"],
            )
            error = None
            retry_after = None
            max_attempts = 8
            try:
                with self.service.repo.bind_action_claim(action_id, token, generation):
                    await asyncio.to_thread(self.service.execute, action)
            except Exception as exc:
                error = str(exc)[:500]
                retry_after = retry_after_seconds(exc)
                try:
                    max_attempts = max(
                        max_attempts,
                        int(getattr(exc, "max_attempts", max_attempts)),
                    )
                except (TypeError, ValueError):
                    pass
                if getattr(exc, "retryable", True) is False:
                    max_attempts = 1
                self._record_error(
                    "scheduled_action", action["action_type"], exc,
                    chat_id=action["chat_id"], session_id=action["session_id"],
                    attempts=action["attempts"],
                )
                LOG.error(
                    "scheduled_action_retry id=%s type=%s",
                    action_id,
                    type(exc).__name__,
                )
            finished = self.service.repo.finish_action(
                action_id,
                self.service.clock(),
                error,
                lease_token=token,
                generation=generation,
                retry_after=retry_after,
                max_attempts=max_attempts,
            )
            if finished and error is None:
                LOG.info(
                    "scheduled_action_completed id=%s action_type=%s",
                    action_id,
                    action["action_type"],
                )

    def _record_error(self, source: str, operation: str, error: Exception, **context: Any) -> None:
        try:
            self.service.repo.record_error(
                self.service.clock(), source, operation, error, **context
            )
        except Exception:
            LOG.error("business_error_persistence_failed source=%s operation=%s", source, operation)

    def _sync_sheets_blocking(self) -> None:
        method = self.service.sheets.sync_once
        now = self.service.clock()
        try:
            supports_clock = "clock" in inspect.signature(method).parameters
        except (TypeError, ValueError):
            supports_clock = False
        if supports_clock:
            method(now, clock=self.service.clock)
        else:
            method(now)

    async def _sheets_cycle(self) -> None:
        """Refresh runtime content and flush Sheets outside the Telegram loop."""
        try:
            refresh = getattr(self.service.sheets, "refresh_content", None)
            if refresh:
                try:
                    await asyncio.to_thread(refresh, self.service.clock())
                except Exception as exc:
                    self._record_error("google_sheets", "refresh_content", exc)
                    LOG.error("google_sheets_refresh_failed type=%s", type(exc).__name__)
            try:
                policy_factory = getattr(self.service, "_runtime_policy", None)
                policy = policy_factory(self.service.clock()) if policy_factory else None
                rows = await asyncio.to_thread(
                    statistics_rows,
                    self.service.repo.path,
                    self.service.clock(),
                    getattr(
                        policy, "night_start",
                        getattr(self.service.settings, "night_start", day_time(20)),
                    ),
                    getattr(
                        policy, "night_end",
                        getattr(self.service.settings, "night_end", day_time(9, 30)),
                    ),
                )
                self.service.repo.queue_statistics(rows, self.service.clock())
            except Exception as exc:
                self._record_error("statistics", "queue_snapshot", exc)
                LOG.error("business_statistics_queue_failed type=%s", type(exc).__name__)
            try:
                await asyncio.to_thread(self._sync_sheets_blocking)
            except Exception as exc:
                self._record_error("google_sheets", "sync_once", exc)
                LOG.error("google_sheets_sync_failed type=%s", type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A background maintenance bug must be observed and must never become
            # an unhandled task exception that stops Telegram processing.
            self._record_error("google_sheets", "background_cycle", exc)
            LOG.error("google_sheets_background_failed type=%s", type(exc).__name__)

    def _launch_sheets_cycle(self) -> None:
        if self._sheets_task is not None and not self._sheets_task.done():
            return
        # The task handles and persists its own errors, so retaining it until the
        # next launch/shutdown cannot create an unobserved exception.
        self._sheets_task = asyncio.create_task(
            self._sheets_cycle(),
            name="telegram-business-sheets",
        )

    async def run_once(self, *, sync_sheets: bool = True) -> None:
        now = self.service.clock()
        await self._process_updates(now)
        await self._process_actions(self.service.clock())
        monotonic_now = time.monotonic()
        if sync_sheets and monotonic_now >= self._next_sheets:
            if self._sheets_task is None or self._sheets_task.done():
                self._next_sheets = monotonic_now + self.service.settings.sheets_sync_seconds
                self._launch_sheets_cycle()

    async def run(self):
        while not (self._stop_event and self._stop_event.is_set()):
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No single corrupt row, transient SQLite error, or integration
                # failure is allowed to terminate the durable worker task.
                self._record_error("scheduler", "run_once", exc)
                LOG.error("telegram_business_scheduler_iteration_failed type=%s", type(exc).__name__)
            if self._stop_event:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(self.poll_seconds)
