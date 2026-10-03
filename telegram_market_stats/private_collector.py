"""Read supplier DMs through the existing authorized MTProto client only.

No send/read-acknowledgement methods. Customers outside known supplier groups
are skipped before requesting history; only price/model/forward context within
our seven-minute request windows is retained. Raw payment data is redacted.
"""
import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from telegram_business.security import redact_payment_data
from .private_store import PrivateStore
from .quotes import prices, timestamp

LOG = logging.getLogger('telegram_market_stats.private')


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


class PrivateQuotesCollector:
    def __init__(self, repository, analyzer, supplier_db, *, clock=None):
        self.repo = repository
        self.store = PrivateStore(repository)
        self.analyzer = analyzer
        self.supplier_db = supplier_db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def suppliers(self):
        # Group participants are supplier identities, not arbitrary private chats.
        with self.repo.connect() as db:
            known = {str(r[0]) for r in db.execute('SELECT DISTINCT sender_id FROM market_messages WHERE sender_id IS NOT NULL')}
            own = {str(r[0]) for r in db.execute("SELECT telegram_user_id FROM market_competitors WHERE label='TEXNIKACH'")}
        if self.supplier_db.exists():
            db = sqlite3.connect(self.supplier_db.as_uri() + '?mode=ro', uri=True)
            try:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_supplier_group_members'").fetchone():
                    known.update(str(r[0]) for r in db.execute('SELECT user_id FROM telegram_supplier_group_members'))
            finally:
                db.close()
        return known - own

    async def collect_once(self, client):
        now = self.clock()
        retry = self.store.retry_at()
        if retry and timestamp(retry) > now:
            return {'deferred': True}
        cutoff = now - timedelta(days=2)
        requests = self.store.requests(iso(cutoff), iso(now))
        if not requests:
            self.store.status(iso(now))
            return {'saved': 0, 'chats': 0}
        windows = [(timestamp(q['telegram_date']), timestamp(q['telegram_date']) + timedelta(seconds=420)) for q in requests]
        first = min(start for start, _ in windows)
        known, cursors, dialogs = self.suppliers(), self.store.cursors(), []
        async for dialog in client.iter_dialogs(limit=2000):
            last = getattr(dialog, 'message', None)
            if not getattr(dialog, 'pinned', False) and last and last.date < first:
                break
            if (not getattr(dialog, 'is_user', False) or str(dialog.id) not in known
                    or getattr(getattr(dialog, 'entity', None), 'bot', False) or not last or last.date < first):
                continue
            cursor = cursors.get(str(dialog.id), {})
            dialogs.append((dialog, cursor))
        # New activity first, then fair persisted polling order for backfill.
        dialogs.sort(key=lambda item: (item[0].message.id <= item[1].get('last_message_id', 0), item[1].get('polled_at', '')))
        saved = chats = 0
        for dialog, cursor in dialogs[:12]:
            chat = str(dialog.id)
            try:
                last_id = cursor.get('last_message_id', 0)
                before, complete = cursor.get('before_id'), bool(cursor.get('backfill_complete'))
                if not cursor:
                    history = list(await client.get_messages(dialog.input_entity, limit=100))
                    forward = history
                    backfill = history
                else:
                    forward = list(await client.get_messages(dialog.input_entity, limit=100, min_id=last_id, reverse=True))
                    history = list(await client.get_messages(dialog.input_entity, limit=30))
                    backfill = None
                    if not complete and before:
                        backfill = list(await client.get_messages(dialog.input_entity, limit=100, max_id=before))
                        history += backfill
                ids = self.store.recheck_ids(chat, iso(cutoff))
                checked = list(await client.get_messages(dialog.input_entity, ids=ids)) if ids else []
                removed = set(ids) - {m.id for m in checked if getattr(m, 'date', None) is not None}
                history += checked
                valid = [m for m in [*forward, *history] if getattr(m, 'date', None) is not None]
                # Never advance the forward cursor from a newer rescan batch.
                # Revisit the last ten minutes: group collection may lag the DM.
                last_id = max([last_id, *(m.id for m in forward if getattr(m, 'date', None) and m.date <= now-timedelta(minutes=10))])
                if backfill is not None:
                    older = [m for m in backfill if getattr(m, 'date', None)]
                    if older:
                        before = min([m.id for m in older] + ([before] if before else []))
                    complete = complete or len(backfill) < 100 or any(m.date < first for m in older)
                output = []
                for message in {m.id: m for m in valid}.values():
                    if not any(start <= message.date <= end for start, end in windows):
                        continue
                    text = redact_payment_data(str(getattr(message, 'message', '') or ''))[:1000]
                    analysis = self.analyzer.analyze(text)
                    fwd = getattr(message, 'fwd_from', None)
                    reply = getattr(message, 'reply_to', None)
                    if not (prices(text, has_model=bool(analysis.mentions)) or analysis.mentions or fwd):
                        removed.add(message.id)  # An edit may have removed a former price.
                        continue
                    sender = getattr(dialog, 'entity', None)
                    name = ' '.join(filter(None, (getattr(sender, 'first_name', ''), getattr(sender, 'last_name', ''))))
                    from telethon.utils import get_peer_id
                    origin = getattr(fwd, 'from_id', None)
                    origin_id = str(get_peer_id(origin)) if origin else None
                    output.append(dict(chat_id=chat, message_id=message.id, telegram_date=iso(message.date),
                        text_excerpt=text, sender_name=name[:160], sender_username=getattr(sender, 'username', None),
                        outgoing=int(bool(getattr(message, 'out', False))), reply_to_message_id=getattr(reply, 'reply_to_msg_id', None),
                        forwarded=int(fwd is not None), forward_from_id=origin_id, forward_date=iso(getattr(fwd, 'date', None)),
                        forward_group_id=origin_id if origin_id and origin_id.startswith('-') else None,
                        forward_message_id=getattr(fwd, 'channel_post', None),
                        mentions_json=json.dumps([asdict(m) for m in analysis.mentions], ensure_ascii=False),
                        edited_at=iso(getattr(message, 'edit_date', None)), deleted_at=None, processed_at=iso(now)))
                self.store.save(chat, output, last_id, before, complete, iso(now), removed)
                saved += len(output)
                chats += 1
            except Exception as exc:
                seconds = getattr(exc, 'seconds', None)
                delay = max(30, int(seconds or 30))
                self.store.status(iso(now), type(exc).__name__, iso(now + timedelta(seconds=delay)))
                LOG.warning('private_quote_retry type=%s', type(exc).__name__)
                return {'saved': saved, 'chats': chats, 'error': type(exc).__name__}
        self.store.status(iso(now))
        return {'saved': saved, 'chats': chats}

    async def run(self, client):
        while True:
            try:
                result = await self.collect_once(client)
                LOG.info('private_quotes_collected counts=%s', result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning('private_quote_collection_failed type=%s', type(exc).__name__)
                self.store.status(iso(self.clock()), type(exc).__name__)
            await asyncio.sleep(30)
