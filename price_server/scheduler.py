"""Durable price publication scheduler.

Scheduling state lives in SQLite through ``PriceRepository``. The in-process
loop only materializes recurring occurrences, claims one leased job at a time,
and delegates domain work to the injected service.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable


LOG = logging.getLogger("price_server.scheduler")


class PriceScheduler:
    def __init__(
        self,
        settings,
        repository,
        service,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 180,
        batch_size: int = 20,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_seconds = max(1, int(settings.scheduler_poll_seconds))
        self.lease_seconds = max(30, int(lease_seconds))
        self.batch_size = max(1, min(100, int(batch_size)))
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def configured(self) -> bool:
        return bool(
            getattr(self.settings, "enabled", False)
            and getattr(self.settings, "telegram_configured", False)
        )

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> bool:
        if self.running:
            return True
        if not self.configured:
            LOG.info(
                "price_scheduler_disabled enabled=%s telegram_configured=%s",
                bool(getattr(self.settings, "enabled", False)),
                bool(getattr(self.settings, "telegram_configured", False)),
            )
            return False

        self._stop_event = asyncio.Event()
        await self._recover_stale_jobs()
        self._task = asyncio.create_task(
            self._run(),
            name="price-publication-scheduler",
        )
        return True

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stop_event = None

    async def _repository_call(self, method_name: str, *args, **kwargs):
        method = getattr(self.repository, method_name, None)
        if method is None:
            return None
        return await asyncio.to_thread(method, *args, **kwargs)

    async def _recover_stale_jobs(self) -> None:
        try:
            recovered = await self._repository_call(
                "recover_stale_jobs",
                self.clock(),
            )
            if recovered:
                LOG.info("price_scheduler_recovered count=%s", recovered)
        except Exception:
            LOG.exception("price_scheduler_recovery_failed")
        try:
            recovered = await self._repository_call(
                "recover_stale_quick_link_rotations",
                self.clock(),
            )
            if recovered:
                LOG.info(
                    "price_quick_link_rotations_recovered count=%s",
                    recovered,
                )
        except Exception:
            LOG.exception("price_quick_link_rotation_recovery_failed")

    async def _materialize_schedules(self, now: datetime) -> None:
        # Optional during the first migration: repositories without recurring
        # schedules can still process manually-created one-off jobs.
        try:
            await self._repository_call("materialize_due_schedules", now)
        except Exception:
            LOG.exception("price_schedule_materialization_failed")

    async def _materialize_quick_link_rotations(self, now: datetime) -> None:
        try:
            await self._repository_call(
                "materialize_due_quick_link_rotations",
                now,
            )
        except Exception:
            LOG.exception("price_quick_link_rotation_materialization_failed")

    @staticmethod
    def _as_jobs(claimed: Any) -> list[Any]:
        if claimed is None:
            return []
        if isinstance(claimed, Mapping):
            return [claimed]
        if isinstance(claimed, Sequence) and not isinstance(
            claimed, (str, bytes, bytearray)
        ):
            return list(claimed)
        return [claimed]

    async def _claim_one(self, now: datetime) -> Any | None:
        claimed = await self._repository_call(
            "claim_due_jobs",
            now,
            limit=1,
            lease_seconds=self.lease_seconds,
        )
        jobs = self._as_jobs(claimed)
        return jobs[0] if jobs else None

    @staticmethod
    def _job_value(job: Any, key: str, default: Any = None) -> Any:
        try:
            return job[key]
        except (KeyError, IndexError, TypeError):
            return getattr(job, key, default)

    async def _execute(self, job: Any) -> Any:
        method = self.service.execute_job
        if inspect.iscoroutinefunction(method):
            return await method(job)
        result = await asyncio.to_thread(method, job)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _service_call(self, method_name: str, *args) -> Any:
        method = getattr(self.service, method_name, None)
        if method is None:
            return None
        if inspect.iscoroutinefunction(method):
            return await method(*args)
        result = await asyncio.to_thread(method, *args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _complete(self, job: Any, result: Any) -> None:
        job_id = self._job_value(job, "job_id")
        lease_token = self._job_value(job, "lease_token")
        completed = await self._repository_call(
            "complete_job",
            job_id,
            lease_token,
            self.clock(),
            result=result if isinstance(result, Mapping) else None,
        )
        if completed is False:
            LOG.warning("price_job_completion_lease_lost job_id=%s", job_id)

    async def _fail(self, job: Any, error: BaseException) -> None:
        job_id = self._job_value(job, "job_id")
        lease_token = self._job_value(job, "lease_token")
        retry_after = getattr(error, "retry_after", None)
        retryable = bool(getattr(error, "retryable", True))
        ambiguous = bool(getattr(error, "ambiguous", False))
        message = str(error)[:1000] or type(error).__name__

        result = await self._repository_call(
            "retry_job",
            job_id,
            lease_token,
            self.clock(),
            message,
            retry_after_seconds=retry_after,
            permanent=not retryable and not ambiguous,
            needs_review=ambiguous,
        )
        if result is False:
            LOG.warning("price_job_failure_lease_lost job_id=%s", job_id)
        LOG.error(
            "price_job_failed job_id=%s type=%s retryable=%s ambiguous=%s",
            job_id,
            type(error).__name__,
            retryable,
            ambiguous,
        )

    async def _process_due_jobs(self) -> int:
        processed = 0
        for _ in range(self.batch_size):
            if self._stop_event is not None and self._stop_event.is_set():
                break
            now = self.clock()
            try:
                job = await self._claim_one(now)
            except Exception:
                LOG.exception("price_job_claim_failed")
                break
            if job is None:
                break

            job_id = self._job_value(job, "job_id")
            action = self._job_value(job, "action", "unknown")
            try:
                result = await self._execute(job)
                await self._complete(job, result)
            except Exception as exc:
                try:
                    await self._fail(job, exc)
                except Exception:
                    LOG.exception("price_job_failure_persistence_failed job_id=%s", job_id)
            processed += 1
            LOG.info(
                "price_job_processed job_id=%s action=%s",
                job_id,
                action,
            )
        return processed

    async def _sync_sheets_outbox(self) -> int:
        method = getattr(self.service, "sync_sheets_outbox", None)
        if method is None:
            return 0
        try:
            if inspect.iscoroutinefunction(method):
                result = await method()
            else:
                result = await asyncio.to_thread(method)
                if inspect.isawaitable(result):
                    result = await result
            return int(result or 0)
        except Exception:
            # Telegram publication is already durable in SQLite. A temporary
            # Sheets outage must never roll it back or stop later jobs.
            LOG.exception("price_sheets_outbox_cycle_failed")
            return 0

    async def run_once(self) -> int:
        """Process one scheduler cycle; public primarily for deterministic tests."""

        if not self.configured:
            return 0
        now = self.clock()
        quick_links_ready = getattr(
            self.service, "ensure_quick_link_registry", None
        ) is None
        if not quick_links_ready:
            try:
                await self._service_call("ensure_quick_link_registry")
                quick_links_ready = True
            except Exception:
                LOG.exception("price_quick_link_registry_failed")
        if quick_links_ready:
            try:
                await self._repository_call(
                    "ensure_quick_link_catalog_date",
                    now,
                )
            except Exception:
                LOG.exception("price_quick_link_catalog_date_failed")
        await self._materialize_schedules(now)
        if quick_links_ready:
            await self._materialize_quick_link_rotations(now)
        for method_name, args, log_name in (
            ("poll_preview_updates", (), "price_preview_update_poll_failed"),
            ("cleanup_terminal_previews", (), "price_preview_cleanup_failed"),
            ("ensure_scheduled_previews", (now,), "price_preview_creation_failed"),
        ):
            try:
                await self._service_call(method_name, *args)
            except Exception:
                LOG.exception(log_name)
        exchange_rate_progress = 0
        if quick_links_ready:
            try:
                exchange_rate_progress = int(
                    await self._service_call(
                        "process_exchange_rate_updates",
                        self.clock(),
                    )
                    or 0
                )
            except Exception:
                LOG.exception("price_exchange_rate_update_failed")
        processed = await self._process_due_jobs() + exchange_rate_progress
        if processed:
            try:
                await self._service_call("cleanup_terminal_previews")
            except Exception:
                LOG.exception("price_preview_cleanup_failed")
        if quick_links_ready:
            try:
                processed += int(
                    await self._service_call(
                        "process_quick_link_rotations",
                        self.clock(),
                    )
                    or 0
                )
            except Exception:
                LOG.exception("price_quick_link_rotation_failed")
        # Link edits may involve several network retries. Run them only after
        # due publications, then permit deletion of the now-unreferenced posts.
        for method_name, log_name in (
            ("refresh_quick_link_posts", "price_quick_link_update_failed"),
            (
                "process_exchange_rate_updates",
                "price_exchange_rate_update_failed",
            ),
            (
                "process_quick_link_rotations",
                "price_quick_link_rotation_failed",
            ),
            (
                "cleanup_superseded_posts",
                "price_superseded_post_cleanup_failed",
            ),
            (
                "ensure_manual_deletion_requests",
                "price_manual_deletion_request_failed",
            ),
            (
                "cleanup_completed_manual_deletion_requests",
                "price_manual_deletion_request_cleanup_failed",
            ),
        ):
            if not quick_links_ready:
                continue
            try:
                if method_name == "process_quick_link_rotations":
                    processed += int(
                        await self._service_call(method_name, self.clock())
                        or 0
                    )
                elif method_name == "refresh_quick_link_posts":
                    await self._service_call(method_name, self.clock())
                elif method_name == "process_exchange_rate_updates":
                    processed += int(
                        await self._service_call(method_name, self.clock())
                        or 0
                    )
                else:
                    await self._service_call(method_name)
            except Exception:
                LOG.exception(log_name)
        await self._sync_sheets_outbox()
        return processed

    async def _run(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                processed = 0
                LOG.exception("price_scheduler_cycle_failed")

            # Drain due work without waiting, but yield so the app remains
            # responsive. Otherwise sleep until the next poll or shutdown.
            if processed:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_seconds,
                )
            except asyncio.TimeoutError:
                pass


# Backwards-friendly explicit name for integration code.
DurablePriceScheduler = PriceScheduler
