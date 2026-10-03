import asyncio
import logging
import time
from .models import Client, Draft, Job

log = logging.getLogger(__name__)


async def poll_telegram(service):
    while True:
        try:
            offset = int(service.repo.state('telegram_offset', '0'))
            updates = await service.telegram.bot.get_updates(offset=offset, timeout=20,
                allowed_updates=['message', 'callback_query'], request_timeout=30)
            for update in updates:
                service.repo.store_telegram_update(update.model_dump(mode='json', exclude_none=True))
            with service.repo.transaction() as s:
                service.repo.put_state(s, 'telegram_poll_heartbeat', time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error('telegram_poll_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(3)


async def process_queue(service):
    last_maintenance = 0
    while True:
        try:
            for _ in range(10):
                job = service.repo.claim_job()
                if job is None:
                    break
                try:
                    await service.handle_job(job)
                    service.repo.change(Job, job.id, status='done', updated_at=time.time())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error('job_failed job=%s kind=%s error_type=%s', job.id, job.kind, type(exc).__name__)
                    service.repo.change(Job, job.id, status='pending' if job.attempts < 5 else 'failed',
                        error=type(exc).__name__, due_at=time.time() + min(300, 2 ** job.attempts), updated_at=time.time())
                    if job.attempts == 5:
                        try:
                            await service.telegram.text(None, f'⚠️ Требуется проверка: задание #{job.id} ({job.kind}). '
                                                       'Сообщение сохранено в БД. Отправка клиенту не подтверждена.')
                        except Exception:
                            pass
            due = service.repo.rows(Client, Client.revision > Client.classified_revision,
                Client.due_at <= time.time(), limit=10, order=Client.due_at)
            for c in due:
                await service.classify_client(c.id)
            # Remove stale send buttons; DB state already blocks them even if Telegram edit fails.
            for d in service.repo.rows(Draft, Draft.status == 'cancelled', Draft.presentation_state == 'sent', limit=20):
                await service.render_status(d.id)
                service.repo.change(Draft, d.id, presentation_state='final')
            now = time.time()
            if now - last_maintenance > 60:
                if service.settings.close_after_hours > 0:
                    cutoff = now - service.settings.close_after_hours * 3600
                    for c in service.repo.rows(Client, Client.topic_state == 'open', Client.last_message_at < cutoff, limit=20):
                        active = service.repo.rows(Draft, Draft.client_id == c.id, Draft.status.in_(['pending', 'sending']), limit=1)
                        if not active:
                            await service.telegram.close_topic(c.topic_id)
                            service.repo.change(Client, c.id, topic_state='closed', status='closed')
                last_maintenance = now
            with service.repo.transaction() as s:
                service.repo.put_state(s, 'worker_heartbeat', time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error('worker_failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(2)
        await asyncio.sleep(.25)
