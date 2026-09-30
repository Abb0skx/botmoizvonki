from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .config import FolderSettings
from .gateway import TelegramFolderGateway
from .repository import FolderRepository


LOG = logging.getLogger("telegram_folder_manager")


def _retry_after(error: BaseException) -> float | None:
    value = getattr(error, "seconds", None)
    if value is None:
        value = getattr(error, "retry_after", None)
    try:
        return max(1.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


class TelegramFolderService:
    def __init__(
        self,
        settings: FolderSettings,
        *,
        repository: FolderRepository | None = None,
        gateway: TelegramFolderGateway | None = None,
        market_collector=None,
        clock=None,
    ):
        self.settings = settings
        self.repo = repository or FolderRepository(settings.db_path)
        self.gateway = gateway or TelegramFolderGateway(settings)
        self.market_collector = market_collector
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_reconcile = 0.0

    async def start(self) -> None:
        await self.gateway.connect()
        await self.gateway.ensure_folders()
        if self.market_collector is not None:
            await self.market_collector.start(self.gateway.client)
        self.repo.seed_new_clients(
            self.clock(),
            backfill_existing=self.settings.backfill_existing,
            reopen_done=self.settings.reopen_done,
        )

    async def stop(self) -> None:
        if self.market_collector is not None:
            await self.market_collector.stop()
        await self.gateway.disconnect()

    async def process_jobs(self) -> int:
        processed = 0
        rows = self.repo.claim_due(
            self.clock(), limit=20, lease_seconds=self.settings.lease_seconds
        )
        for row in rows:
            token = str(row["lease_token"])
            if not self.repo.is_current(row):
                self.repo.supersede(row["job_id"], token, self.clock())
                continue
            try:
                await self.gateway.move(str(row["chat_id"]), str(row["folder_code"]))
            except Exception as exc:
                self.repo.retry(
                    row["job_id"], token, exc, self.clock(),
                    max_attempts=self.settings.max_attempts,
                    retry_after=_retry_after(exc),
                )
                LOG.warning(
                    "telegram_folder_job_retry job_id=%s chat_id=%s type=%s",
                    row["job_id"], row["chat_id"], type(exc).__name__,
                )
            else:
                self.repo.finish(row["job_id"], token, self.clock())
                processed += 1
        return processed

    def _remote_choice(self, codes: set[str]) -> tuple[str | None, bool]:
        if len(codes) == 1:
            return next(iter(codes)), False
        if "DONE" in codes:
            return "DONE", True
        active = codes - {"NEW"}
        if len(active) == 1:
            return next(iter(active)), True
        return None, False

    async def reconcile_manual_moves(self) -> int:
        changed = 0
        memberships = await self.gateway.snapshot()
        for chat_id, codes in memberships.items():
            choice, normalize = self._remote_choice(codes)
            if choice is None:
                LOG.warning(
                    "telegram_folder_ambiguous chat_id=%s folders=%s",
                    chat_id, ",".join(sorted(codes)),
                )
                continue
            current = self.repo.assignment(chat_id)
            if current is None or current["folder_code"] != choice:
                if normalize:
                    self.repo.assign(
                        chat_id, choice, self.clock(), source="telegram_manual"
                    )
                else:
                    self.repo.accept_remote(chat_id, choice, self.clock())
                changed += 1
        return changed

    async def run_once(self) -> int:
        self.repo.seed_new_clients(
            self.clock(),
            backfill_existing=self.settings.backfill_existing,
            reopen_done=self.settings.reopen_done,
        )
        processed = await self.process_jobs()
        if self.market_collector is not None:
            await self.market_collector.run_if_due()
        monotonic = time.monotonic()
        if monotonic - self._last_reconcile >= self.settings.reconcile_seconds:
            await self.reconcile_manual_moves()
            self._last_reconcile = monotonic
        return processed


class TelegramFolderScheduler:
    def __init__(self, service: TelegramFolderService):
        self.service = service
        self.task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        await self.service.start()
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(self.run(), name="telegram-folder-manager")

    async def stop(self) -> None:
        if self.task is not None:
            if self._stop is not None:
                self._stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=30)
            except asyncio.TimeoutError:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            self.task = None
        await self.service.stop()
        self._stop = None

    async def run(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            try:
                await self.service.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("telegram_folder_cycle_failed type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.service.settings.poll_seconds
                )
            except asyncio.TimeoutError:
                pass
