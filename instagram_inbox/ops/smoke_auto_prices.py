"""Real Sheets/Telegram, isolated DB and fake Instagram. Never contacts customers."""
import asyncio
import json
import time

from instagram_inbox.config import Settings
from instagram_inbox.instagram import normalize_events
from instagram_inbox.models import Base, Client, Draft, Message
from instagram_inbox.repository import Repository
from instagram_inbox.replies import TemplateResponder
from instagram_inbox.service import InboxService
from instagram_inbox.telegram import TelegramAPI


class NoCustomerTransport:
    def __init__(self):
        self.sent = []

    async def profile(self, _):
        return {'name': 'Тест автоответа — не клиент'}

    async def send(self, user, text):
        assert user == 'selftest-auto-not-customer'
        self.sent.append(text)
        return 'selftest-auto-out-' + str(len(self.sent))


async def main():
    config = Settings.from_env()
    assert config.auto_price_enabled and config.auto_price_since > 0
    config.database_url = 'sqlite:////data/auto-price-smoke.db'
    repo = Repository(config.database_url)
    Base.metadata.create_all(repo.engine)
    telegram = TelegramAPI(config)
    responder = TemplateResponder(config)
    service = InboxService(repo, config, telegram, NoCustomerTransport(), responder)
    try:
        await telegram.check_setup()
        if repo.state('complete'):
            print(json.dumps({'already_verified': True}))
            return

        async def incoming(mid, text):
            payload = {'object': 'instagram', 'entry': [{'id': config.instagram_account_id, 'messaging': [{
                'sender': {'id': 'selftest-auto-not-customer'}, 'recipient': {'id': config.instagram_account_id},
                'timestamp': time.time() * 1000, 'message': {'mid': mid, 'text': text}}]}]}
            ident = repo.ingest(next(normalize_events(payload, config.instagram_account_id)), 0)
            assert ident
            await service.publish_incoming(ident)
            await service.classify_client(1)
            return ident

        await incoming('selftest-auto-in-1', 'CMF Buds Pro 2 — цена и наличие?')
        draft = repo.rows(Draft)[0]
        assert draft.origin == 'auto_price' and draft.manager_id is None
        await service.present(draft.id)
        await service.dispatch(draft.id)
        assert len(service.instagram.sent) == 1
        await service.dispatch(draft.id)
        assert len(service.instagram.sent) == 1
        result = service.instagram.sent[0]
        assert 'CMF Buds Pro 2' in result and 'сум' in result and 'номер телефона' in result
        assert 'https://t.me/' not in result
        details = await incoming('selftest-auto-in-2', 'ТЕСТ: адрес доставки и телефон будут здесь (не реальный заказ).')
        assert repo.get(Message, details).classification['category'] == 'order_details'
        assert len(repo.rows(Draft)) == 1 and len(service.instagram.sent) == 1
        c = repo.get(Client, 1)
        await telegram.text(c.topic_id, '✅ Тест пройден: актуальная цена из таблицы + запрос адреса и телефона в Direct. '
            'Продолжение заказа передаётся менеджеру. В Instagram ничего не отправлялось — использован тестовый транспорт.')
        await telegram.close_topic(c.topic_id)
        with repo.transaction() as s:
            repo.put_state(s, 'complete', 'yes')
        await telegram.text(None, 'Включён автоответ в Instagram Direct по точной модели: цена из прайса в сумах + '
            'запрос адреса доставки и телефона. Клиенту больше не нужно переходить в Telegram для заказа.\n\n'
            'Данные приходят в его тему. Заказ, наличие и доставку подтверждает менеджер через Reply и кнопку «Отправить». '
            'Неизвестные модели и остальные ответы по-прежнему требуют подтверждения.\n\n'
            'Тексты запроса: Google Sheets → direct_settings → direct_order_prompt_ru / uz / bilingual.')
        print(json.dumps({'passed': True, 'topic_id': c.topic_id, 'live_price_reply': result,
                          'fake_sends': len(service.instagram.sent), 'instagram_customer_test_sends': 0}, ensure_ascii=False))
    finally:
        await responder.close()
        await telegram.bot.session.close()


asyncio.run(main())
