from __future__ import annotations

import logging
import time
from dataclasses import replace

from .analyzer import MarketMessageAnalyzer, ProductModelIndex
from .collector import MarketStatsCollector
from .config import MarketStatsSettings
from .repository import MarketStatsRepository


LOG = logging.getLogger("telegram_market_stats")


class MultiGroupMarketStatsCollector:
    """Read configured groups with independent checkpoints on one MTProto client."""

    def __init__(self, settings: MarketStatsSettings, *, repository=None, analyzer=None, clock=None):
        self.settings = settings
        self.repo = repository or MarketStatsRepository(settings.db_path)
        self.analyzer = analyzer
        self.clock = clock
        self.client = None
        self.collectors: list[MarketStatsCollector] = []
        self._last_run: float | None = None

    async def start(self, client) -> None:
        self.client = client
        if self.analyzer is None:
            self.analyzer = MarketMessageAnalyzer(ProductModelIndex.from_xlsx(self.settings.catalog_path))
        me = await client.get_me()
        participants = {**self.settings.competitors, int(me.id): "TEXNIKACH"}
        self.collectors = [
            MarketStatsCollector(
                replace(self.settings, group_id=group.group_id, group_title=group.title,
                        additional_groups=(), competitors=participants),
                repository=self.repo, analyzer=self.analyzer, clock=self.clock,
            )
            for group in self.settings.groups
        ]
        self._last_run = None

    async def stop(self) -> None:
        for collector in self.collectors:
            await collector.stop()
        self.client = None

    async def collect_once(self) -> dict[str, dict]:
        if self.client is None:
            raise RuntimeError("market collector is not started")
        results = {}
        for collector in self.collectors:
            group_id = collector.settings.group_id
            try:
                if collector.entity is None:
                    await collector.start(self.client)
                result = await collector.collect_once()
                results[str(group_id)] = result
                LOG.info("market_stats_collected group_id=%s scanned=%s", group_id, result["scanned"])
            except Exception as exc:
                # An unavailable group must not stop collection in the others or
                # manager-folder synchronization. Retry on the next polling cycle.
                error_type = type(exc).__name__
                results[str(group_id)] = {"error": error_type}
                LOG.error("market_stats_collection_failed group_id=%s type=%s", group_id, error_type)
        return results

    async def run_if_due(self) -> dict | None:
        current = time.monotonic()
        if self._last_run is not None and current - self._last_run < self.settings.poll_seconds:
            return None
        self._last_run = current
        return await self.collect_once()
