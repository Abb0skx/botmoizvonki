import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
from types import SimpleNamespace
import httpx
import pytest
from instagram_inbox.replies import TemplateResponder, Classification, rule_classification, DEFAULT_TEXTS
from instagram_inbox.config import Settings
from instagram_inbox.instagram import normalize_events, signature, SendError
from instagram_inbox.main import create_app
from instagram_inbox.models import Base, Client, Draft, Job, Message
from instagram_inbox.repository import Repository
from instagram_inbox.service import InboxService


class Telegram:
    def __init__(self):
        self.created, self.sent, self.edited, self.reopened = [], [], [], []
        self.counter = 100

    async def create_topic(self, name):
        self.created.append(name)
        return 50 + len(self.created)

    async def text(self, topic, text, markup=None, reply_id=None):
        self.counter += 1
        self.sent.append((topic, text, markup))
        return self.counter

    async def attachment(self, topic, item, reply_id):
        return await self.text(topic, str(item))

    async def reopen(self, topic):
        self.reopened.append(topic)

    async def edit(self, *args):
        self.edited.append(args)

    async def authorized(self, user):
        return user.get('id') in (1, 2)

    async def answer(self, *args):
        pass


class Instagram:
    def __init__(self):
        self.sent, self.relays = [], []
        self.error = None

    async def profile(self, ident):
        return {'username': 'customer'}

    async def send(self, user, text):
        self.sent.append((user, text))
        if self.error:
            raise self.error
        await asyncio.sleep(.01)
        return 'out-' + str(len(self.sent))

    async def relay(self, payload):
        self.relays.append(payload)


class FakeResponder(TemplateResponder):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []

    async def classify(self, batch, history, force=False):
        return rule_classification(batch) or Classification(action='reply', category='product_question',
            requires_reply=True, confidence=.99, reason='Вопрос о товаре')

    async def propose(self, classification, batch, history, previous=''):
        self.calls.append([m.text for m in history])
        if classification.category in ('credit_or_installment', 'greeting'):
            return DEFAULT_TEXTS['credit_reply' if classification.category == 'credit_or_installment' else 'greeting_ru']
        return 'Уточню наличие. Какой объём памяти нужен?'


@pytest.fixture
def service(tmp_path):
    config = Settings(database_url=f'sqlite:///{tmp_path}/test.db', instagram_account_id='account',
        telegram_group_id=-1001, telegram_bot_token='123456:FAKE', meta_app_secret='secret',
        meta_verify_token='verify', meta_access_token='fake', worker_enabled=False, debounce_seconds=0)
    repo = Repository(config.database_url)
    Base.metadata.create_all(repo.engine)
    return InboxService(repo, config, Telegram(), Instagram(), FakeResponder(config))


def ingest(service, text='Есть S25?', mid='m1', attachments=None, sender='customer'):
    raw = {'object': 'instagram', 'entry': [{'id': 'account', 'messaging': [{
        'sender': {'id': sender}, 'recipient': {'id': 'account'}, 'timestamp': time.time() * 1000,
        'message': {'mid': mid, 'text': text, 'attachments': attachments or []}}]}]}
    event = next(normalize_events(raw, 'account'))
    return service.repo.ingest(event, 0)


async def drain(service):
    for _ in range(100):
        job = service.repo.claim_job()
        if not job:
            return
        await service.handle_job(job)
        service.repo.change(Job, job.id, status='done')
    raise AssertionError('queue did not drain')


async def prepare(service, text='Есть S25?'):
    mid = ingest(service, text)
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    return service.repo.get(Message, mid), service.repo.rows(Draft)[-1]


@pytest.mark.asyncio
async def test_new_customer_saved_topic_and_draft_no_send(service):
    m, d = await prepare(service)
    assert service.repo.get(Client, 1).external_id == 'customer'
    assert m.text == 'Есть S25?' and m.telegram_message_id
    assert len(service.telegram.created) == 1 and d.status == 'pending'
    assert service.instagram.sent == []


@pytest.mark.asyncio
async def test_existing_topic_reopened_not_recreated(service):
    await prepare(service)
    service.repo.change(Client, 1, topic_state='closed')
    ingest(service, 'А белый?', 'm2')
    await drain(service)
    assert len(service.telegram.created) == 1
    assert service.telegram.reopened == [51]


@pytest.mark.asyncio
async def test_media_only_saved_forwarded_without_draft(service):
    ingest(service, '', attachments=[{'type': 'share', 'payload': {'url': 'https://www.instagram.com/reel/example/'}}])
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    assert len(service.repo.rows(Message)) == 1
    assert service.telegram.sent and not service.repo.rows(Draft)
    assert not service.assistant.calls and not service.instagram.sent


@pytest.mark.asyncio
async def test_reel_then_question_keeps_context(service):
    ingest(service, '', attachments=[{'type': 'share'}])
    await drain(service)
    await service.classify_client(1)
    ingest(service, 'Есть такой?', 'm2')
    await drain(service)
    await service.classify_client(1)
    assert len(service.repo.rows(Draft)) == 1
    assert len(service.assistant.calls[-1]) == 2


@pytest.mark.asyncio
async def test_credit_is_not_spam_and_no_auto_send(service):
    _, d = await prepare(service, 'Можно в рассрочку?')
    assert d.category == 'credit_or_installment'
    assert 'нет кредита и рассрочки' in d.text
    assert not service.instagram.sent


@pytest.mark.asyncio
async def test_two_managers_send_once(service):
    _, d = await prepare(service)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda uid: service.repo.approve(d.id, {'id': uid, 'first_name': str(uid)}), [1, 2]))
    assert results.count('approved') == 1
    await drain(service)
    await service.dispatch(d.id)
    assert len(service.instagram.sent) == 1
    assert service.repo.get(Draft, d.id).status == 'sent'
    assert len(service.repo.rows(Message, Message.direction == 'outgoing')) == 1


def test_duplicate_does_not_increment_revision(service):
    assert ingest(service)
    assert ingest(service) is None
    assert len(service.repo.rows(Message)) == 1
    assert service.repo.get(Client, 1).revision == 1


@pytest.mark.asyncio
async def test_internal_note_never_sends_or_enters_reply_context(service):
    await prepare(service)
    await service.telegram_update({'message': {'message_id': 900, 'message_thread_id': 51,
        'chat': {'id': -1001}, 'from': {'id': 1}, 'text': 'Проверь цену у поставщика'}})
    assert len(service.repo.rows(Message, Message.direction == 'internal')) == 1
    assert not service.instagram.sent
    assert all(m.direction != 'internal' for m in service.repo.history(1, 20))


@pytest.mark.asyncio
async def test_manual_reply_requires_confirmation_and_is_idempotent(service):
    m, ai = await prepare(service)
    update = {'message': {'message_id': 901, 'message_thread_id': 51, 'chat': {'id': -1001},
        'from': {'id': 1}, 'text': '14 200 000 сум.', 'reply_to_message': {'message_id': m.telegram_message_id}}}
    await service.telegram_update(update)
    await service.telegram_update(update)
    await drain(service)
    manual = service.repo.rows(Draft, Draft.origin == 'manual')
    assert len(manual) == 1 and not service.instagram.sent
    assert service.repo.get(Draft, ai.id).status == 'cancelled'
    assert service.repo.approve(manual[0].id, {'id': 1}) == 'approved'
    await drain(service)
    assert service.instagram.sent == [('customer', '14 200 000 сум.')]


@pytest.mark.asyncio
async def test_new_inbound_blocks_stale_and_queued_draft(service):
    _, d = await prepare(service)
    assert service.repo.approve(d.id, {'id': 1}) == 'approved'
    ingest(service, 'Мне 256 Black.', 'm2')
    await drain(service)
    assert not service.instagram.sent
    assert service.repo.approve(d.id, {'id': 2}) == 'superseded'


@pytest.mark.asyncio
async def test_debounce_generates_one_draft(service):
    for n, text in enumerate(['Здравствуйте', 'Айфон 17 про', '256', 'чёрный есть?']):
        ingest(service, text, f'm{n}')
    await drain(service)
    await service.classify_client(1)
    await service.classify_client(1)
    assert len(service.repo.rows(Message)) == 4 and len(service.repo.rows(Draft)) == 1


@pytest.mark.asyncio
async def test_ambiguous_send_cannot_retry(service):
    _, d = await prepare(service)
    service.instagram.error = SendError('Результат неизвестен.', retry_safe=False)
    service.repo.approve(d.id, {'id': 1})
    await drain(service)
    assert service.repo.get(Draft, d.id).status == 'failed'
    assert service.repo.approve(d.id, {'id': 2}, retry=True) != 'approved'
    await drain(service)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_expired_window_blocks_send(service):
    _, d = await prepare(service)
    service.repo.change(Client, 1, last_customer_at=time.time() - 90000)
    service.repo.approve(d.id, {'id': 1})
    await drain(service)
    assert not service.instagram.sent
    assert '24-часовое' in service.repo.get(Draft, d.id).error


@pytest.mark.asyncio
async def test_foreign_callback_cannot_confirm(service):
    _, d = await prepare(service)
    await service.callback({'id': 'cb', 'from': {'id': 999}, 'data': f'ig:send:{d.id}',
        'message': {'chat': {'id': -1001}, 'message_id': d.telegram_message_id, 'message_thread_id': 51}})
    assert service.repo.get(Draft, d.id).status == 'pending'
    assert not service.instagram.sent


@pytest.mark.asyncio
async def test_signed_webhook_idempotent_and_comments_relay_without_direct(service):
    app = create_app(service.settings, service)
    payload = {'object': 'instagram', 'entry': [{'id': 'account', 'changes': [{'field': 'comments', 'value': {'id': 'c1'}}],
        'messaging': [{'sender': {'id': 'customer'}, 'recipient': {'id': 'account'},
                       'message': {'mid': 'webhook1', 'text': 'Цена?'}}]}]}
    raw = json.dumps(payload).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as http:
        assert (await http.post('/webhooks/instagram', content=raw)).status_code == 403
        for _ in range(2):
            r = await http.post('/webhooks/instagram', content=raw, headers={'X-Hub-Signature-256': signature('secret', raw)})
            assert r.status_code == 200
        assert (await http.get('/webhooks/instagram?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=42')).text == '42'
    assert len(service.repo.rows(Message)) == 1
    await drain(service)
    assert len(service.instagram.relays) == 1
    assert 'messaging' not in service.instagram.relays[0]['entry'][0]


def test_restart_does_not_repeat_uncertain_send_or_topic(service):
    mid = ingest(service)
    service.repo.change(Client, 1, topic_state='creating')
    service.repo.change(Message, mid, telegram_state='sending')
    did = service.repo.draft(1, 1, mid, 'Test', 'greeting')
    service.repo.change(Draft, did, status='sending', dispatch_started_at=time.time())
    service.repo.recover()
    assert service.repo.get(Client, 1).topic_state == 'uncertain'
    assert service.repo.get(Message, mid).telegram_state == 'uncertain'
    assert service.repo.get(Draft, did).status == 'failed'
    assert not service.repo.get(Draft, did).retry_safe


@pytest.mark.parametrize('text', ['Спасибо', 'Rahmat', 'Ок, спасибо', '🙏', ''])
def test_no_reply_rules(text):
    c = rule_classification([SimpleNamespace(text=text, direction='incoming', message_type='text')])
    assert c.action == 'ignore' and not c.requires_reply


@pytest.mark.parametrize('text', ['Uzum nasiya bormi?', 'Alif Nasiya есть?', 'Можно платить по месяцам?', 'Bolip tolash'])
def test_credit_languages(text):
    c = rule_classification([SimpleNamespace(text=text, direction='incoming', message_type='text')])
    assert c.category == 'credit_or_installment' and c.requires_reply


@pytest.mark.asyncio
async def test_new_message_during_lookup_discards_generated_draft(service):
    ingest(service)
    await drain(service)
    original = service.assistant.propose
    async def delayed(*args, **kwargs):
        ingest(service, 'Нет, нужен Ultra', 'm2')
        return await original(*args, **kwargs)
    service.assistant.propose = delayed
    await service.classify_client(1)
    assert not service.repo.rows(Draft)
    assert service.repo.get(Client, 1).classified_revision == 0


@pytest.mark.asyncio
async def test_regenerate_is_idempotent(service):
    _, d = await prepare(service)
    service.repo.cancel(d.id, 1, 'regenerated')
    await service.regenerate(d.id)
    await service.regenerate(d.id)
    assert len(service.repo.rows(Draft)) == 2


@pytest.mark.asyncio
async def test_explicit_rejection_retry_requires_new_confirmation(service):
    _, d = await prepare(service)
    service.instagram.error = SendError('Rate limited', retry_safe=True)
    service.repo.approve(d.id, {'id': 1})
    await drain(service)
    service.instagram.error = None
    await drain(service)
    assert len(service.instagram.sent) == 1
    assert service.repo.approve(d.id, {'id': 1}, retry=True) == 'approved'
    await drain(service)
    assert len(service.instagram.sent) == 2
    assert service.repo.get(Draft, d.id).status == 'sent'


@pytest.mark.asyncio
async def test_caption_with_media_is_replyable(service):
    ingest(service, 'Сколько стоит?', attachments=[{'type': 'image'}])
    await drain(service)
    await service.classify_client(1)
    assert len(service.repo.rows(Draft)) == 1


def test_concurrent_duplicate_webhooks_save_once(service):
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: ingest(service), range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(service.repo.rows(Message)) == 1


def test_forwarded_accounts_are_not_accepted(service):
    payload = {'object': 'instagram', 'entry': [{'id': 'other-account', 'messaging': [{
        'sender': {'id': 'customer'}, 'recipient': {'id': 'other-account'}, 'message': {'mid': 'evil', 'text': 'test'}}]}]}
    assert list(normalize_events(payload, 'account')) == []
