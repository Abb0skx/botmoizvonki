import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import hmac
import json
import logging
from pathlib import Path
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from .ai import Assistant
from .config import Settings
from .instagram import InstagramAPI, normalize_events, valid_signature
from .repository import Repository
from .service import InboxService
from .telegram import TelegramAPI
from .worker import poll_telegram, process_queue

log = logging.getLogger(__name__)


def create_app(settings=None, service=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        logging.basicConfig(level=settings.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
        # HTTP libraries include request URLs; never log Bot API token-bearing URLs.
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
        app.state.service = service or InboxService(Repository(settings.database_url), settings,
            TelegramAPI(settings), InstagramAPI(settings), Assistant(settings))
        running = app.state.service
        tasks, lock = [], None
        try:
            if settings.worker_enabled:
                Path(settings.worker_lock_path).parent.mkdir(parents=True, exist_ok=True)
                lock = open(settings.worker_lock_path, 'a')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                running.repo.recover()
                await running.telegram.check_setup()
                hook = await running.telegram.bot.get_webhook_info()
                if hook.url:
                    raise RuntimeError('Dedicated inbox bot must use polling, not an existing webhook')
                tasks = [asyncio.create_task(process_queue(running)), asyncio.create_task(poll_telegram(running))]
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not service:
                await running.telegram.bot.session.close()
                await running.instagram.http.aclose()
                if running.assistant.client:
                    await running.assistant.client.close()
                running.repo.engine.dispose()
            if lock:
                lock.close()

    app = FastAPI(title='TEXNIKACH Instagram Inbox', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if service:
        app.state.service = service

    @app.get('/webhooks/instagram', response_class=PlainTextResponse)
    async def verify(request: Request):
        p = request.query_params
        if (p.get('hub.mode') == 'subscribe' and settings.meta_verify_token
            and hmac.compare_digest(p.get('hub.verify_token', ''), settings.meta_verify_token)
            and p.get('hub.challenge') is not None):
            return p['hub.challenge']
        raise HTTPException(403, 'Invalid verification token')

    @app.post('/webhooks/instagram')
    async def instagram_webhook(request: Request):
        # Bounded streaming read avoids allocating arbitrary untrusted bodies.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2 * 1024 * 1024:
                raise HTTPException(413, 'Webhook too large')
        body = bytes(raw)
        if not valid_signature(settings.meta_app_secret, body, request.headers.get('x-hub-signature-256')):
            raise HTTPException(403, 'Invalid webhook signature')
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get('entry', []), list):
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Invalid JSON') from None
        repo = app.state.service.repo
        count = 0
        try:
            for event in normalize_events(payload, settings.instagram_account_id):
                mid = repo.ingest(event, settings.debounce_seconds)
                if mid is not None:
                    count += 1
                    log.info('instagram_message_received message=%s', mid)
                else:
                    log.info('duplicate_skipped')
            changes = [dict(id=e['id'], changes=e['changes']) for e in payload.get('entry', [])
                       if isinstance(e, dict) and str(e.get('id')) == settings.instagram_account_id and e.get('changes')]
            if changes:
                relay = {'object': 'instagram', 'entry': changes}
                key = hashlib.sha256(json.dumps(relay, sort_keys=True).encode()).hexdigest()
                with repo.transaction() as s:
                    repo.enqueue(s, 'relay:' + key, 'relay', relay)
        except (TypeError, AttributeError, KeyError):
            raise HTTPException(400, 'Malformed webhook event') from None
        log.info('webhook_received saved=%s', count)
        return {'ok': True, 'saved': count}

    @app.get('/healthz')
    @app.get('/instagram/inbox/health')
    async def health():
        repo = app.state.service.repo
        worker = float(repo.state('worker_heartbeat', '0'))
        polling = float(repo.state('telegram_poll_heartbeat', '0'))
        healthy = not settings.worker_enabled or (time.time() - worker < 180 and time.time() - polling < 90)
        return JSONResponse({'status': 'ok' if healthy else 'starting_or_degraded',
                             'mode': 'manager_approval', 'ai_configured': bool(settings.openai_api_key)},
                            status_code=200 if healthy else 503)
    return app


app = create_app()
