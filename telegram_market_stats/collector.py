from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .analyzer import MarketMessageAnalyzer, ProductModelIndex
from .config import MarketStatsSettings
from .repository import MarketStatsRepository


LOG = logging.getLogger("telegram_market_stats")


class MarketStatsCollector:
    def __init__(
        self,
        settings: MarketStatsSettings,
        *,
        repository: MarketStatsRepository | None = None,
        analyzer: MarketMessageAnalyzer | None = None,
        clock=None,
    ):
        self.settings = settings
        self.repo = repository or MarketStatsRepository(settings.db_path)
        self.analyzer = analyzer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.client: Any = None
        self.entity: Any = None
        self._last_run = 0.0

    async def start(self, client: Any) -> None:
        self.client = client
        self.entity = await client.get_input_entity(self.settings.group_id)
        if self.analyzer is None:
            self.analyzer = MarketMessageAnalyzer(
                ProductModelIndex.from_xlsx(self.settings.catalog_path)
            )
        self.repo.configure(
            self.settings.group_id,
            self.settings.group_title,
            self.settings.competitors,
            self.clock(),
        )

    async def stop(self) -> None:
        self.client = None
        self.entity = None

    async def _initial_messages(self) -> list[Any]:
        messages = await self.client.get_messages(
            self.entity,
            limit=(self.settings.batch_size if self.settings.backfill_days else 1),
        )
        return list(messages)

    async def _incremental_messages(
        self,
        last_message_id: int,
        *,
        backfill_before_message_id: int | None,
        backfill_complete: bool,
        backfill_scanned: int,
    ) -> tuple[list[Any], list[Any], int]:
        messages = await self.client.get_messages(
            self.entity,
            limit=self.settings.batch_size,
            min_id=last_message_id,
            reverse=True,
        )
        # Only contiguous forward history may advance the checkpoint. Recent
        # edits can include IDs beyond this batch during a busy interval.
        forward_max_id = max((int(message.id) for message in messages), default=last_message_id)
        recent = []
        if self.settings.edit_rescan_messages:
            recent = await self.client.get_messages(
                self.entity, limit=self.settings.edit_rescan_messages
            )
        backfill: list[Any] = []
        if (
            not backfill_complete
            and backfill_before_message_id
            and backfill_scanned < self.settings.backfill_limit
        ):
            remaining = self.settings.backfill_limit - backfill_scanned
            backfill = list(await self.client.get_messages(
                self.entity,
                limit=min(self.settings.batch_size, remaining),
                max_id=backfill_before_message_id,
            ))
        by_id = {
            int(message.id): message
            for message in [*messages, *recent]
            if getattr(message, "id", None) is not None
        }
        return list(by_id.values()), backfill, forward_max_id

    async def collect_once(self) -> dict[str, int]:
        if self.client is None or self.entity is None or self.analyzer is None:
            raise RuntimeError("market collector is not started")
        now = self.clock()
        run_id = self.repo.start_run(self.settings.group_id, now)
        checkpoint = self.repo.checkpoint(self.settings.group_id)
        last_id = int(checkpoint["last_message_id"]) if checkpoint else 0
        initialized = bool(checkpoint and checkpoint["initialized_at"])
        backfill_before = (
            int(checkpoint["backfill_before_message_id"])
            if checkpoint and checkpoint["backfill_before_message_id"] else None
        )
        backfill_scanned = int(checkpoint["backfill_scanned"] or 0) if checkpoint else 0
        backfill_complete = bool(checkpoint and checkpoint["backfill_complete"])
        previous_backfill = (backfill_before, backfill_scanned, backfill_complete)
        scanned = with_models = demand_mentions = 0
        try:
            if initialized:
                messages, backfill, max_id = await self._incremental_messages(
                    last_id,
                    backfill_before_message_id=backfill_before,
                    backfill_complete=backfill_complete,
                    backfill_scanned=backfill_scanned,
                )
            else:
                backfill = await self._initial_messages()
                messages = []
                max_id = max((int(message.id) for message in backfill), default=last_id)
            cutoff = now - timedelta(days=self.settings.backfill_days)
            if backfill:
                backfill_before = min(int(message.id) for message in backfill)
                backfill_scanned += len(backfill)
                eligible = (
                    [message for message in backfill if message.date >= cutoff]
                    if self.settings.backfill_days else backfill[:1]
                )
                messages.extend(eligible)
                oldest_at = min(message.date for message in backfill)
                backfill_complete = (
                    self.settings.backfill_days == 0
                    or oldest_at < cutoff
                    or len(backfill) < min(
                        self.settings.batch_size,
                        self.settings.backfill_limit - (backfill_scanned - len(backfill)),
                    )
                    or backfill_scanned >= self.settings.backfill_limit
                )
            elif not backfill_complete:
                backfill_complete = True
            messages = list({int(message.id): message for message in messages}.values())
            # Old statistics did not retain reply links or display names.
            # Fill only relevant historical windows, without resetting any cursor.
            missing = self.repo.missing_quote_metadata(self.settings.group_id, now)
            if missing:
                try:
                    hydrated = list(await self.client.get_messages(self.entity, ids=missing))
                    valid = [m for m in hydrated if getattr(m, 'date', None) is not None]
                    found = {int(m.id) for m in valid}
                    self.repo.unavailable_metadata(self.settings.group_id, [i for i in missing if i not in found])
                    messages = list({int(m.id): m for m in [*valid, *messages]}.values())
                except Exception as exc:
                    LOG.warning('market_quote_metadata_retry group_id=%s type=%s', self.settings.group_id, type(exc).__name__)
            messages.sort(key=lambda message: int(message.id))
            competitor_ids = {user_id for user_id, label in self.settings.competitors.items() if label != "TEXNIKACH"}
            for message in messages:
                message_id = int(message.id)
                text = str(getattr(message, "message", "") or "")
                analysis = self.analyzer.analyze(text)
                sender = getattr(message, 'sender', None)
                sender_name = ' '.join(filter(None, (
                    getattr(sender, 'first_name', None), getattr(sender, 'last_name', None),
                ))) or getattr(sender, 'title', None)
                reply = getattr(message, 'reply_to', None)
                external = getattr(reply, 'reply_to_peer_id', None)
                if external is not None:
                    from telethon.utils import get_peer_id
                    external = get_peer_id(external) != self.settings.group_id
                has_models = self.repo.upsert_message(
                    group_id=self.settings.group_id,
                    message_id=message_id,
                    sender_id=(
                        int(message.sender_id)
                        if getattr(message, "sender_id", None) is not None else None
                    ),
                    telegram_date=message.date,
                    edited_at=getattr(message, "edit_date", None),
                    text=text,
                    analysis=analysis,
                    competitor_ids=competitor_ids,
                    processed_at=now,
                    reply_to_message_id=getattr(reply, 'reply_to_msg_id', None),
                    reply_external=bool(external), sender_name=sender_name,
                    sender_username=getattr(sender, 'username', None),
                    reply_metadata_loaded=True,
                )
                scanned += 1
                with_models += int(has_models)
                if analysis.intent == "demand":
                    demand_mentions += len(analysis.mentions)
            self.repo.save_checkpoint(
                self.settings.group_id,
                max_id,
                now,
                initialize=not initialized,
                backfill_before_message_id=backfill_before,
                backfill_scanned=backfill_scanned,
                backfill_complete=backfill_complete,
            )
            self.repo.finish_run(
                run_id, now, scanned=scanned,
                messages_with_models=with_models,
                demand_mentions=demand_mentions,
            )
            return {
                "scanned": scanned,
                "messages_with_models": with_models,
                "demand_mentions": demand_mentions,
            }
        except Exception as exc:
            error_type = type(exc).__name__
            backfill_before, backfill_scanned, backfill_complete = previous_backfill
            self.repo.save_checkpoint(
                self.settings.group_id,
                last_id,
                now,
                backfill_before_message_id=backfill_before,
                backfill_scanned=backfill_scanned,
                backfill_complete=backfill_complete,
                error=error_type,
            )
            self.repo.finish_run(
                run_id, now, scanned=scanned,
                messages_with_models=with_models,
                demand_mentions=demand_mentions,
                error_type=error_type,
            )
            raise

    async def run_if_due(self) -> dict[str, int] | None:
        current = time.monotonic()
        if current - self._last_run < self.settings.poll_seconds:
            return None
        self._last_run = current
        try:
            result = await self.collect_once()
            LOG.info(
                "market_stats_collected group_id=%s scanned=%s models=%s demand=%s",
                self.settings.group_id, result["scanned"],
                result["messages_with_models"], result["demand_mentions"],
            )
            return result
        except Exception as exc:
            LOG.error(
                "market_stats_collection_failed group_id=%s type=%s",
                self.settings.group_id, type(exc).__name__,
            )
            return None
