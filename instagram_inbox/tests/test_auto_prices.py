import asyncio
import time
from decimal import Decimal

import pytest

from instagram_inbox.config import Settings
from instagram_inbox.instagram import SendError
from instagram_inbox.models import Client, Draft, Message
from instagram_inbox.replies import TemplateResponder
from instagram_inbox.sources import ProductIndex
from telegram_business.products import ProductVariant
from .test_inbox import service, ingest, drain
from .test_templates import Source


QUERY = 'CMF Buds Pro 2 — цена и наличие?'


def setup_auto(service):
    service.settings.auto_price_enabled = True
    service.settings.auto_price_since = time.time() - 10
    source = Source()
    source.index = ProductIndex([ProductVariant('CMF Buds Pro 2', '', color, Decimal('580650'), i + 1)
        for i, color in enumerate(('Blue', 'Dark Grey', 'Light Grey', 'Orange'))])
    service.assistant = TemplateResponder(service.settings, source)
    return source


async def prepare_auto(service, query=QUERY, send=True):
    ingest(service, query)
    await drain(service)
    await service.classify_client(1)
    if send:
        await drain(service)
    return service.repo.rows(Draft)[-1]


@pytest.mark.asyncio
async def test_cmf_automatic_current_price_and_direct_order_once(service):
    source = setup_auto(service)
    d = await prepare_auto(service)
    assert d.status == 'sent' and d.origin == 'auto_price' and d.manager_id is None
    expected = ('TEXNIKACH\n\nCMF Buds Pro 2\n\nАктуальные цены / Aktual narxlar:\n'
                '• Blue, Dark Grey, Light Grey, Orange — 581 000 сум\n\n'
                'Куда доставить? Напишите адрес доставки и номер телефона здесь, в Direct.')
    assert service.instagram.sent == [('customer', expected)]
    assert ('catalog', True) in source.calls
    assert ('config', True) in source.calls
    outgoing = service.repo.rows(Message, Message.direction == 'outgoing')[0]
    assert outgoing.sender_type == 'bot' and outgoing.raw['automated'] is True
    assert outgoing.raw['approved_by'] is None
    assert service.active_order(1)
    assert 'Автоответ по прайсу' in service.telegram.edited[-1][1]
    assert service.telegram.edited[-1][2] is None
    assert ingest(service, QUERY) is None
    await service.classify_client(1)
    await asyncio.gather(service.dispatch(d.id), service.dispatch(d.id))
    await drain(service)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('query,lang', [('CMF Buds Pro 2 narxi qancha?', 'uz'), ('CMF Buds Pro 2', 'bilingual'), (QUERY, 'ru')])
async def test_sheet_editable_localized_order_prompt(service, query, lang):
    source = setup_auto(service)
    source.settings['direct_order_prompt_' + lang] = 'CUSTOM ORDER ' + lang
    d = await prepare_auto(service, query)
    assert d.status == 'sent' and d.text.endswith('CUSTOM ORDER ' + lang)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['Здравствуйте', 'CMF Buds Pro 3', 'CMF Buds Pro 2 в кредит?', 'непонятный вопрос', 'CMF Budz Pro 2'])
async def test_no_automatic_nonprice_unknown_or_fuzzy(service, query):
    setup_auto(service)
    d = await prepare_auto(service, query)
    assert d.status == 'pending' and d.origin == 'template'
    assert not service.instagram.sent


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['disabled', 'before_activation', 'backlog', 'unpublished'])
async def test_old_or_unpublished_messages_never_auto_sent(service, gate):
    setup_auto(service)
    mid = ingest(service, QUERY)
    await drain(service)
    if gate == 'disabled':
        service.settings.auto_price_enabled = False
    elif gate == 'before_activation':
        service.settings.auto_price_since = time.time() + 1
    elif gate == 'backlog':
        service.repo.change(Message, mid, created_at=time.time() - 1000)
        service.settings.auto_price_since = time.time() - 2000
    else:
        service.repo.change(Message, mid, telegram_state='uncertain')
    await service.classify_client(1)
    await drain(service)
    assert not service.instagram.sent
    assert service.repo.rows(Draft)[0].origin == 'template'


@pytest.mark.asyncio
async def test_refresh_before_auto_send_uses_new_price(service):
    source = setup_auto(service)
    d = await prepare_auto(service, send=False)
    source.index = ProductIndex([ProductVariant('CMF Buds Pro 2', '', 'Orange', Decimal('600000'), 1)])
    await drain(service)
    assert len(service.instagram.sent) == 1 and '600 000 сум' in service.instagram.sent[0][1]
    assert '600 000 сум' in service.repo.get(Draft, d.id).text


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['catalog', 'config', 'disabled', 'new_model', 'new_message', 'expired'])
async def test_auto_refresh_failures_and_races_fail_closed(service, failure):
    source = setup_auto(service)
    d = await prepare_auto(service, send=False)
    if failure in ('catalog', 'config'):
        source.failed.add(failure)
    elif failure == 'disabled':
        service.settings.auto_price_enabled = False
    elif failure == 'new_model':
        source.index = ProductIndex([ProductVariant('Other model', '', '', Decimal('10'), 1)])
    elif failure == 'expired':
        service.repo.change(Client, 1, last_customer_at=time.time() - 25 * 3600)
    else:
        original = service.assistant.classify
        async def with_new_message(*args, **kwargs):
            ingest(service, 'Отмена', 'm2')
            return await original(*args, **kwargs)
        service.assistant.classify = with_new_message
    await drain(service)
    assert not service.instagram.sent
    assert service.repo.get(Draft, d.id).status in ('pending', 'cancelled', 'failed')
    source.failed.clear()
    await drain(service)
    assert not service.instagram.sent


@pytest.mark.asyncio
async def test_delivery_details_go_to_manager_not_default_rule(service):
    setup_auto(service)
    await prepare_auto(service)
    for n, text in enumerate(('Ташкент, тестовый адрес', '+998 90 000 00 00', 'Исправление: другой подъезд', '')):
        mid = ingest(service, text, 'details-' + str(n), attachments=[{'type': 'location', 'payload': {}}] if not text else [])
        await drain(service)
        await service.classify_client(1)
        await drain(service)
        assert service.repo.get(Message, mid).classification['category'] == 'order_details'
    assert len(service.instagram.sent) == 1 and len(service.repo.rows(Draft)) == 1
    assert any('Заказ ещё не подтверждён' in text for _, text, _ in service.telegram.sent)
    service.close_order(1)
    assert not service.active_order(1)


@pytest.mark.asyncio
async def test_price_followup_during_order_still_gets_current_quote(service):
    setup_auto(service)
    await prepare_auto(service)
    ingest(service, 'А Orange?', 'color')
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    assert len(service.instagram.sent) == 2
    assert '• Orange — 581 000 сум' in service.instagram.sent[-1][1]


@pytest.mark.asyncio
async def test_unknown_send_result_never_retried(service):
    setup_auto(service)
    service.instagram.error = SendError('Unknown result', retry_safe=False)
    d = await prepare_auto(service)
    assert d.status == 'failed' and not d.retry_safe
    assert not service.active_order(1)
    service.repo.recover()
    await service.dispatch(d.id)
    await drain(service)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_manager_manual_reply_cancels_auto_before_dispatch(service):
    setup_auto(service)
    d = await prepare_auto(service, send=False)
    await service.present(d.id)
    original = service.repo.get(Message, d.source_message_id)
    await service.telegram_update({'message': {'chat': {'id': -1001}, 'message_thread_id': 51,
        'message_id': 900, 'from': {'id': 1}, 'text': 'Ответ менеджера',
        'reply_to_message': {'message_id': original.telegram_message_id}}})
    await drain(service)
    assert service.repo.get(Draft, d.id).status == 'cancelled'
    assert not service.instagram.sent
    manual = service.repo.rows(Draft, Draft.origin == 'manual')[0]
    assert manual.status == 'pending'


def test_auto_mode_requires_explicit_activation():
    with pytest.raises(ValueError, match='AUTO_PRICE_SINCE'):
        Settings(auto_price_enabled=True).validate()


@pytest.mark.asyncio
async def test_concurrent_auto_dispatch_claims_once(service):
    setup_auto(service)
    d = await prepare_auto(service, send=False)
    await service.present(d.id)
    await asyncio.gather(service.dispatch(d.id), service.dispatch(d.id))
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_restart_after_unknown_auto_dispatch_never_sends_again(service):
    setup_auto(service)
    d = await prepare_auto(service, send=False)
    await service.present(d.id)
    service.repo.change(Draft, d.id, dispatch_started_at=time.time())
    service.repo.recover()
    await drain(service)
    assert not service.instagram.sent
    assert service.repo.get(Draft, d.id).status == 'failed'
    assert not service.repo.get(Draft, d.id).retry_safe


@pytest.mark.asyncio
async def test_phone_with_model_after_quote_is_an_order_not_new_auto_reply(service):
    setup_auto(service)
    await prepare_auto(service)
    mid = ingest(service, 'CMF Buds Pro 2 +998 90 000 00 00', 'contact-model')
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    assert len(service.instagram.sent) == 1
    assert service.repo.get(Message, mid).classification['category'] == 'order_details'


@pytest.mark.asyncio
async def test_pending_auto_survives_restart_without_duplicate(service):
    setup_auto(service)
    await prepare_auto(service, send=False)
    service.repo.recover()
    await drain(service)
    assert len(service.instagram.sent) == 1
    assert service.active_order(1)
    service.repo.recover()
    await drain(service)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_phone_without_order_not_treated_as_price_followup(service):
    setup_auto(service)
    # Model history exists, but price has not been sent/confirmed.
    service.settings.auto_price_enabled = False
    await prepare_auto(service)
    mid = ingest(service, '+998 90 000 00 00', 'phone-only')
    service.settings.auto_price_enabled = True
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    assert not service.instagram.sent
    assert service.repo.get(Message, mid).classification['category'] != 'price_question'
