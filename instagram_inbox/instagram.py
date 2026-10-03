import hashlib
import hmac
import json
import logging
import time
import httpx

log = logging.getLogger(__name__)


def signature(secret, body):
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def valid_signature(secret, body, received):
    return bool(secret and received and hmac.compare_digest(signature(secret, body), received))


def normalize_events(payload, account_id):
    """Retain unsupported media/reactions too; captions and reply references are context."""
    if payload.get('object') != 'instagram':
        return
    for entry in payload.get('entry', []):
        if str(entry.get('id', '')) != account_id:
            continue
        for event in entry.get('messaging', []):
            sender = str(event.get('sender', {}).get('id', ''))
            recipient = str(event.get('recipient', {}).get('id', ''))
            message = event.get('message') or {}
            reaction = event.get('reaction') or message.get('reaction')
            echo = bool(message.get('is_echo')) or sender == account_id
            if not sender or not recipient or account_id not in (sender, recipient):
                continue
            if not message and not reaction:
                continue
            external_id = message.get('mid')
            kind = 'text'
            attachments = message.get('attachments') or []
            text = message.get('text') or message.get('caption') or ''
            if reaction:
                kind = 'reaction'
                external_id = 'reaction:' + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                text = reaction.get('emoji', '') if isinstance(reaction, dict) else str(reaction)
            elif message.get('is_deleted'):
                kind = 'deleted'
                external_id = 'deleted:' + str(external_id)
            elif attachments:
                kind = str(attachments[0].get('type', 'attachment'))[:32]
                captions = [a.get('payload', {}).get('caption', '') for a in attachments]
                text = '\n'.join(t for t in [text, *captions] if isinstance(t, str) and t)
            if not external_id:
                external_id = 'event:' + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            try:
                created_at = float(event.get('timestamp', time.time() * 1000)) / 1000
            except (TypeError, ValueError):
                created_at = time.time()
            yield dict(account_id=account_id, sender_id=recipient if echo else sender,
                external_id=str(external_id), text=str(text), attachments=attachments,
                raw=event, message_type=kind, created_at=created_at,
                direction='outgoing' if echo else 'incoming',
                reply_to=str((message.get('reply_to') or {}).get('mid', '')),
                username=str(event.get('sender', {}).get('username', '')))


class SendError(Exception):
    def __init__(self, message, retry_safe=False):
        super().__init__(message)
        self.retry_safe = retry_safe


class InstagramAPI:
    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=25)

    async def send(self, recipient, text):
        try:
            r = await self.http.post(
                f'{self.settings.graph_base_url}/{self.settings.instagram_account_id}/messages',
                headers={'Authorization': f'Bearer {self.settings.meta_access_token}'},
                json={'recipient': {'id': recipient}, 'message': {'text': text}})
        except httpx.HTTPError:
            raise SendError('Результат отправки неизвестен. Проверьте переписку в Instagram.') from None
        try:
            body = r.json()
        except ValueError:
            raise SendError('Не удалось подтвердить отправку. Проверьте Instagram.') from None
        if r.is_error:
            error = body.get('error', {})
            # Keep diagnostic codes and trace id; strip secrets from provider text.
            detail = str(error).replace(self.settings.meta_access_token, '[redacted]')
            log.error('instagram_send_failure status=%s details=%s', r.status_code, detail[:3000])
            code = error.get('code')
            if code in (10, 200, 551):
                msg = 'Instagram не разрешает ответ: проверьте окно переписки, доступ и настройки Direct.'
            elif code == 190:
                msg = 'Нужно обновить подключение Instagram.'
            elif r.status_code == 429 or code in (4, 32, 613):
                msg = 'Instagram ограничил частоту запросов. Повторите позже.'
            else:
                msg = f'Instagram отклонил запрос (код {code or r.status_code}).'
            raise SendError(msg, retry_safe=r.status_code < 500)
        if not body.get('message_id'):
            raise SendError('Instagram не вернул подтверждение. Проверьте переписку.')
        return str(body['message_id'])

    async def profile(self, user_id):
        try:
            r = await self.http.get(f'{self.settings.graph_base_url}/{user_id}',
                params={'fields': 'name,username'},
                headers={'Authorization': f'Bearer {self.settings.meta_access_token}'})
            return r.json() if r.is_success else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def relay(self, payload):
        if not self.settings.legacy_webhook_url:
            raise RuntimeError('Legacy comment relay is not configured')
        raw = json.dumps(payload, separators=(',', ':')).encode()
        r = await self.http.post(self.settings.legacy_webhook_url, content=raw,
            headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': signature(self.settings.meta_app_secret, raw)})
        if r.is_error:
            raise RuntimeError(f'Comment relay HTTP {r.status_code}')
