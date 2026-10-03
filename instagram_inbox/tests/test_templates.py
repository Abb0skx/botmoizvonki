import time
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from instagram_inbox.config import Settings
from instagram_inbox.models import Draft, Message, Client
from instagram_inbox.replies import TemplateResponder, DEFAULT_TEXTS, price_text
from instagram_inbox.sources import SheetSource, ProductIndex, parse_catalog, parse_config
from telegram_business.products import ProductVariant
from .test_inbox import service, ingest, drain


class Source:
    def __init__(self):
        self.settings = dict(DEFAULT_TEXTS)
        self.calls = []
        self.failed = set()
        self.rules = [dict(priority=90, keywords=['kredit', 'кредит', 'bolip tolash'], match_type='contains_any',
                           reply_text='Кредита нет. / Kredit yo‘q.', row_number=3),
                      dict(priority=10, keywords=['доставка'], match_type='contains_any', reply_text='Доставка: {manager_url}', row_number=4),
                      dict(priority=0, keywords=[], match_type='default', reply_text='Наш канал: {telegram_url}\nМенеджер: {manager_url}', row_number=5)]
        self.index = ProductIndex([
            ProductVariant('Samsung Galaxy S25', '8/256Gb', 'Black', Decimal('9000000'), 1),
            ProductVariant('Samsung Galaxy S25 Ultra', '12/256Gb', 'Black', Decimal('11000000'), 2),
            ProductVariant('Samsung Galaxy S25 Ultra', '12/512Gb', 'White', Decimal('12000000'), 3),
            ProductVariant('Apple iPhone 17 Pro (eSIM)', '256Gb', 'Silver', Decimal('13000000'), 4),
            ProductVariant('Apple iPhone 17 Pro Max (eSIM)', '256Gb', 'Orange', Decimal('15000000'), 5),
        ])

    async def get(self, kind, force=False):
        self.calls.append((kind, force))
        if kind in self.failed:
            raise RuntimeError('source unavailable')
        return ({'settings': self.settings, 'rules': self.rules} if kind == 'config' else (self.index, Decimal(11850)))

    async def close(self):
        pass


def msg(text, ident=1, direction='incoming'):
    return SimpleNamespace(id=ident, text=text, direction=direction, message_type='text', created_at=time.time())


async def classify(text, source=None, history=None):
    source = source or Source()
    responder = TemplateResponder(Settings(), source)
    batch = [msg(text, 99)]
    return await responder.classify(batch, (history or []) + batch)


@pytest.mark.asyncio
@pytest.mark.parametrize('text,model,price', [
    ('Сколько стоит S25 Ultra 256 черный?', 'Samsung Galaxy S25 Ultra', '11 000 000'),
    ('айфон17 про 256', 'Apple iPhone 17 Pro', '13 000 000'),
    ('S25 ultra 512 oq bormi', 'Samsung Galaxy S25 Ultra', '12 000 000'),
    ('iPhone 17 Pro Max', 'Apple iPhone 17 Pro Max', '15 000 000'),
])
async def test_models_memory_colors_ru_uz(text, model, price):
    result = await classify(text)
    assert result.category == 'price_question'
    assert model in result.reply_text and price in result.reply_text and 'сум' in result.reply_text
    assert '$' not in result.reply_text and 'USD' not in result.reply_text
    assert 'https://t.me/texnikach_admin' in result.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['S26 Ultra', 'iPhone 18 Pro', 'iPhone 17', 'S25 Ultra 1024', 'S25 Ultra 256 белый'])
async def test_no_substitute_for_unknown_or_ambiguous_or_missing_variant(text):
    result = await classify(text)
    assert result.category == 'product_question'
    assert '000' not in result.reply_text


@pytest.mark.asyncio
async def test_credit_has_priority_over_model_and_is_sheet_editable():
    source = Source()
    result = await classify('S25 Ultra в кредит?', source)
    assert result.category == 'credit_or_installment' and 'Кредита нет.' in result.reply_text
    source.rules[0]['reply_text'] = 'Изменённый текст'
    assert (await classify('kredit bormi?', source)).reply_text == 'Изменённый текст'


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Можно оплатить по месяцам?', 'Alif Nasiya', 'variant', 'kredt'])
async def test_credit_fallbacks(text):
    assert (await classify(text)).category == 'credit_or_installment'


@pytest.mark.asyncio
async def test_greeting_keywords_default_and_manager_only():
    source = Source()
    source.settings['greeting_uz'] = 'Salom test'
    assert (await classify('Salom', source)).reply_text == 'Salom test'
    assert (await classify('доставка')).reply_text.startswith('Доставка:')
    assert (await classify('непонятный текст')).reply_text.startswith('Наш канал:')
    assert (await classify('S25 Ultra не работает')).action == 'manager_only'


@pytest.mark.asyncio
async def test_media_thanks_emoji_ignored_question_not_ignored():
    source = Source()
    for text in ('', 'Спасибо', '🙏'):
        assert (await classify(text, source)).action == 'ignore'
    assert source.calls == []
    assert (await classify('?', source)).requires_reply
    assert (await classify('Есть такой?', source)).requires_reply


@pytest.mark.asyncio
async def test_context_recent_incoming_model_only():
    result = await classify('А белый 512?', history=[msg('S25 Ultra')])
    assert result.category == 'price_question' and '12 000 000' in result.reply_text
    assert '11 000 000' not in result.reply_text
    assert (await classify('S26 Ultra', history=[msg('S25 Ultra')])).category != 'price_question'
    assert (await classify('А белый 512?', history=[msg('S25 Ultra', direction='outgoing')])).category != 'price_question'
    old = msg('S25 Ultra')
    old.created_at -= 8000
    assert (await classify('А белый 512?', history=[old])).category != 'price_question'


@pytest.mark.asyncio
async def test_unavailable_catalog_never_returns_old_prices():
    source = Source()
    source.failed.add('catalog')
    result = await classify('S25 Ultra', source)
    assert result.action == 'manager_only' and not result.reply_text
    assert (await classify('кредит', source)).requires_reply


@pytest.mark.asyncio
async def test_long_template_fails_without_truncating_links():
    source = Source()
    source.rules[-1]['reply_text'] = 'a' * 1100
    with pytest.raises(ValueError):
        await classify('непонятный текст', source)


def test_csv_schema_currency_and_nan():
    prices = 'product_id,model_name,memory,color,price\n1,Samsung Galaxy S25,256Gb,Black,123.50\n'
    settings = 'setting,value\nkurs,11850\n'
    index, rate = parse_catalog(prices, settings)
    assert index.search('S25').variants[0].price_uzs == Decimal('1463475')
    assert '1 463 000 сум' in price_text(index.search('S25'), DEFAULT_TEXTS)
    for bad in ('NaN', 'Infinity', '-1', '0'):
        with pytest.raises(ValueError):
            parse_catalog(prices.replace('123.50', bad), settings)
        with pytest.raises(ValueError):
            parse_catalog(prices, settings.replace('11850', bad))
    with pytest.raises(ValueError):
        parse_catalog('<html>Login</html>', settings)


def test_same_product_different_warranty_is_a_valid_separate_offer():
    prices = ('product_id,model_name,memory,color,price,warranty_period\n'
              '1,Dyson HD16,,Pink,330,1\n1,Dyson HD16,,Pink,420,12\n')
    index, _ = parse_catalog(prices, 'setting,value\nkurs,11850\n')
    result = price_text(index.search('Dyson HD16'), DEFAULT_TEXTS)
    assert len(index._variants) == 2
    assert '1 мес.' in result and '12 мес.' in result
    with pytest.raises(ValueError):
        parse_catalog(prices + '1,Dyson HD16,,Pink,330,1\n', 'setting,value\nkurs,11850\n')


def test_rules_enabled_priorities_exact_all_and_default():
    raw = ('priority,enabled,keywords,match_type,reply_text\n'
           '90,FALSE,no,contains_any,disabled\n'
           '5,TRUE,"a,b",contains_all,ab\n'
           '6,TRUE,yes,exact,y\n'
           '0,TRUE,DEFAULT,default,d\n')
    config = parse_config(raw, 'setting,value\nmanager_url,https://t.me/texnikach_admin\n')
    from instagram_inbox.replies import resolve_rule
    assert resolve_rule('a b', config['rules'])['reply_text'] == 'ab'
    assert resolve_rule('yes', config['rules'])['reply_text'] == 'y'
    assert resolve_rule('yesterday', config['rules'])['reply_text'] == 'd'
    assert len(config['rules']) == 3


@pytest.mark.asyncio
async def test_http_cache_force_refresh_and_fail_closed():
    broken = False
    calls = []
    def handle(request):
        calls.append(request)
        if broken:
            return httpx.Response(503)
        assert request.url.params['range'].startswith('A1:')
        return httpx.Response(200, text=('product_id,model_name,memory,color,price\n1,S25,256Gb,Black,100\n'
            if request.url.params['sheet'] == 'bot_prices' else 'setting,value\nkurs,11850\n'))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    source = SheetSource(Settings(), http)
    first = await source.get('catalog')
    assert await source.get('catalog') is first and len(calls) == 2
    broken = True
    with pytest.raises(httpx.HTTPStatusError):
        await source.get('catalog', force=True)
    # A failed forced refresh must invalidate even a previously fresh cached price.
    with pytest.raises(httpx.HTTPStatusError):
        await source.get('catalog')
    assert source.status()['catalog'] == 'unavailable'
    source.loaded['catalog'] -= 100
    with pytest.raises(httpx.HTTPStatusError):
        await source.get('catalog')
    assert time.monotonic() - source.loaded['catalog'] > 100
    await source.close()


async def real_prepare(service, text='S25 Ultra'):
    service.assistant = TemplateResponder(service.settings, Source())
    mid = ingest(service, text)
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    return service.repo.get(Message, mid), service.repo.rows(Draft)[-1]


@pytest.mark.asyncio
async def test_template_price_requires_manager_and_rechecks_before_send(service):
    _, draft = await real_prepare(service)
    assert not service.instagram.sent and draft.origin == 'template'
    assert service.repo.approve(draft.id, {'id': 1}) == 'approved'
    await drain(service)
    assert len(service.instagram.sent) == 1
    assert ('catalog', True) in service.assistant.source.calls
    await service.dispatch(draft.id)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_changed_price_needs_new_confirmation(service):
    _, draft = await real_prepare(service)
    source = service.assistant.source
    source.index._variants[1] = ProductVariant('Samsung Galaxy S25 Ultra', '12/256Gb', 'Black', Decimal('11500000'), 2)
    service.repo.approve(draft.id, {'id': 1})
    await drain(service)
    assert not service.instagram.sent
    drafts = service.repo.rows(Draft, order=Draft.id)
    assert len(drafts) == 2 and drafts[0].status == 'cancelled' and drafts[1].status == 'pending'
    assert '11 500 000' in drafts[1].text
    service.repo.approve(drafts[1].id, {'id': 2})
    await drain(service)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_price_outage_at_confirmation_does_not_send(service):
    _, draft = await real_prepare(service)
    service.assistant.source.failed.add('catalog')
    service.repo.approve(draft.id, {'id': 1})
    await drain(service)
    assert not service.instagram.sent
    assert service.repo.get(Draft, draft.id).status == 'pending'
    service.assistant.source.failed.clear()
    await drain(service)
    assert not service.instagram.sent
    service.repo.approve(draft.id, {'id': 1})
    await drain(service)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_refresh_preserves_debounced_model_and_memory(service):
    service.assistant = TemplateResponder(service.settings, Source())
    ingest(service, 'S25 Ultra')
    ingest(service, '512', 'm2')
    await drain(service)
    await service.classify_client(1)
    await drain(service)
    draft = service.repo.rows(Draft)[0]
    assert '12 000 000' in draft.text and '11 000 000' not in draft.text
    service.repo.cancel(draft.id, 1, 'regenerated')
    await service.regenerate(draft.id)
    replacement = service.repo.rows(Draft)[-1]
    assert replacement.text == draft.text


@pytest.mark.asyncio
async def test_new_message_during_price_recheck_blocks_send(service):
    _, draft = await real_prepare(service)
    original = service.assistant.classify
    async def incoming(*args, **kwargs):
        ingest(service, 'Нет, хочу iPhone 17 Pro', 'm2')
        return await original(*args, **kwargs)
    service.assistant.classify = incoming
    service.repo.approve(draft.id, {'id': 1})
    await drain(service)
    assert not service.instagram.sent
