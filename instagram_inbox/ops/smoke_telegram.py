"""Real Telegram, isolated test database, fake Instagram transport (cannot contact customers)."""
import asyncio
import json
import time
from pathlib import Path
from instagram_inbox.ai import Assistant
from instagram_inbox.config import Settings
from instagram_inbox.instagram import normalize_events
from instagram_inbox.models import Base, Draft, Job, Message
from instagram_inbox.repository import Repository
from instagram_inbox.service import InboxService, HELP
from instagram_inbox.telegram import TelegramAPI


class NoCustomerTransport:
    def __init__(self):
        self.sent = []

    async def profile(self, _):
        return {'name': 'Проверка системы (не клиент)'}

    async def send(self, user, text):
        self.sent.append(text)
        return 'selftest-out-1'


async def main():
    config = Settings.from_env()
    config.database_url = 'sqlite:////data/telegram-smoke.db'
    repo = Repository(config.database_url)
    Base.metadata.create_all(repo.engine)
    telegram = TelegramAPI(config)
    try:
        me = await telegram.check_setup()
        if repo.state('smoke_complete'):
            print(json.dumps({'already_verified': True, 'bot': me.username}))
            return
        service = InboxService(repo, config, telegram, NoCustomerTransport(), Assistant(config))
        payload = {'object': 'instagram', 'entry': [{'id': config.instagram_account_id, 'messaging': [{
            'sender': {'id': 'selftest-not-customer'}, 'recipient': {'id': config.instagram_account_id},
            'timestamp': time.time()*1000, 'message': {'mid': 'smoke-credit-1', 'text': 'Можно в рассрочку?'}}]}]}
        event = next(normalize_events(payload, config.instagram_account_id))
        mid = repo.ingest(event, 0)
        if mid:
            await service.publish_incoming(mid)
            await service.classify_client(1)
        drafts = repo.rows(Draft)
        if not drafts:
            raise RuntimeError('Expected credit draft')
        d = drafts[0]
        await service.present(d.id)
        repo.approve(d.id, {'id': config.manager_ids[0], 'first_name': 'Тест (без отправки в Instagram)'})
        await service.dispatch(d.id)
        assert len(service.instagram.sent) == 1
        await service.dispatch(d.id)
        assert len(service.instagram.sent) == 1
        with repo.transaction() as s:
            repo.put_state(s, 'smoke_complete', 'yes')
        from instagram_inbox.models import Client
        c = repo.get(Client, 1)
        await telegram.text(c.topic_id, '✅ Проверка завершена. Это тестовая тема. '
                            'В Instagram ничего не отправлялось. Реальные обращения появятся в отдельных темах.')
        await telegram.close_topic(c.topic_id)
        await telegram.text(None, 'Система Instagram подключена.\n\n' + HELP +
            ('\n\nAI пока ожидает ключ OpenAI. Ручные ответы и проверенные шаблоны доступны.' if not config.openai_api_key else ''))
        print(json.dumps({'bot': me.username, 'forum_ok': True, 'topic_id': c.topic_id,
                          'test_draft_sent_to_fake_transport': True, 'instagram_customer_sends': 0}))
    finally:
        await telegram.bot.session.close()


asyncio.run(main())
