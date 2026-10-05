"""Two-phase real Telegram Reply smoke; isolated DB, no Instagram send API."""
import asyncio
import json
import sqlite3
import sys
import time

from instagram_inbox.config import Settings
from instagram_inbox.models import Base, Client, Draft, Message
from instagram_inbox.repository import Repository
from instagram_inbox.service import InboxService
from instagram_inbox.telegram import TelegramAPI

MARKER = 'ТЕСТ DIRECT REPLY — НЕ КЛИЕНТ'
REPLY = 'Тест прямого Reply менеджера. В Instagram ничего не отправлять.'


class NoCustomerTransport:
    def __init__(self):
        self.sent = []

    async def profile(self, _):
        return {'name': 'Тест прямого Reply — не клиент'}

    async def send(self, user, text):
        assert user == 'selftest-direct-reply-not-customer'
        self.sent.append(text)
        return 'selftest-direct-reply-out'


async def main():
    config = Settings.from_env()
    assert config.direct_reply_since > 0
    repo = Repository('sqlite:////data/direct-reply-smoke.db')
    Base.metadata.create_all(repo.engine)
    telegram = TelegramAPI(config)
    service = InboxService(repo, config, telegram, NoCustomerTransport(), None)
    try:
        await telegram.check_setup()
        if repo.state('complete'):
            print(json.dumps({'already_verified': True}))
            return
        if sys.argv[1] == 'prepare':
            mid = repo.ingest({'account_id': config.instagram_account_id,
                'sender_id': 'selftest-direct-reply-not-customer', 'external_id': 'selftest-direct-reply-in',
                'text': MARKER, 'attachments': [], 'message_type': 'text', 'raw': {}, 'created_at': time.time()}, 0)
            m = repo.get(Message, mid) if mid else repo.rows(Message, Message.direction == 'incoming')[0]
            await service.publish_incoming(m.id)
            c = repo.get(Client, m.client_id)
            print(json.dumps({'topic_id': c.topic_id, 'source_message_id': repo.get(Message, m.id).telegram_message_id,
                              'group_id': config.telegram_group_id, 'reply_text': REPLY}, ensure_ascii=False))
            return
        assert sys.argv[1] == 'verify'
        c = repo.get(Client, 1)
        m = repo.rows(Message, Message.direction == 'incoming')[0]
        with sqlite3.connect('file:/data/inbox.db?mode=ro', uri=True) as live:
            assert not live.execute('SELECT 1 FROM inbox_clients WHERE topic_id=?', (c.topic_id,)).fetchone()
            events = live.execute("SELECT payload FROM inbox_jobs WHERE kind='telegram' AND "
                "json_extract(payload,'$.message.message_thread_id')=? AND "
                "json_extract(payload,'$.message.reply_to_message.message_id')=? ORDER BY id DESC LIMIT 5",
                (c.topic_id, m.telegram_message_id)).fetchall()
        bodies = [json.loads(row[0]) for row in events]
        body = next((p for p in bodies if p['message'].get('text') == REPLY), None)
        assert body, 'Test Reply not yet received by the live Telegram poller'
        assert body['message']['from']['id'] in config.manager_ids
        assert not body['message'].get('sender_chat')
        await service.telegram_update(body)
        draft = repo.rows(Draft, Draft.category == 'manual_reply')[0]
        await service.present(draft.id)
        await service.dispatch(draft.id)
        assert service.instagram.sent == [REPLY]
        await service.telegram_update(body)
        await service.dispatch(draft.id)
        assert service.instagram.sent == [REPLY]
        assert repo.get(Draft, draft.id).status == 'sent'
        await telegram.text(c.topic_id, '✅ Проверено: настоящий Reply менеджера прошёл через рабочий Telegram poller '
            'и отправился без кнопки. Instagram в тесте заменён имитацией — клиентам ничего не отправлялось.')
        await telegram.close_topic(c.topic_id)
        with repo.transaction() as s:
            repo.put_state(s, 'complete', 'yes')
        await telegram.text(None, 'Обновление: текстовый Reply менеджера на сообщение клиента сразу отправляется в '
            'Instagram Direct, без кнопки. Обычный текст в теме остаётся внутренним. Пишите от своего аккаунта, '
            'не от имени группы. Шаблоны бота по-прежнему подтверждаются кнопкой. Старые ответы не рассылались.')
        print(json.dumps({'passed': True, 'topic_id': c.topic_id, 'actual_poll_sender_verified': True,
                          'immediate_reply': True, 'fake_sends': 1, 'instagram_customer_test_sends': 0}))
    finally:
        await telegram.bot.session.close()


asyncio.run(main())
