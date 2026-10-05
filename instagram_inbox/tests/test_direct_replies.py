import asyncio
import time
from types import SimpleNamespace

import pytest
from aiogram.types import Update

from instagram_inbox.models import Client, Draft, Job, Manager, Message
from instagram_inbox.telegram import TelegramAPI
from instagram_inbox.worker import poll_telegram
from .test_inbox import service, prepare, ingest, drain


def reply_update(service, original, *, uid=1, mid=901, text='Здравствуйте! Доставка возможна.', alias=True, topic=51, date=None):
    payload = {'update_id': mid + 1000, 'message': {'message_id': mid, 'date': date or int(time.time()),
        'message_thread_id': topic, 'is_topic_message': True,
        'chat': {'id': service.settings.telegram_group_id, 'type': 'supergroup'},
        'from': {'id': uid, 'is_bot': False, 'first_name': 'Manager'}, 'text': text,
        'reply_to_message': {'message_id': original.telegram_message_id, 'date': int(time.time()),
            'chat': {'id': service.settings.telegram_group_id, 'type': 'supergroup'}, 'text': 'Вопрос клиента'}}}
    return Update.model_validate(payload).model_dump(mode='json', exclude_none=True, by_alias=alias)


async def direct_prepare(service):
    service.settings.direct_reply_since = time.time() - 10
    return await prepare(service)


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', [True, False])
async def test_native_and_previously_serialized_sender_reply_send_without_button(service, alias):
    original, template = await direct_prepare(service)
    body = reply_update(service, original, alias=alias)
    await service.telegram_update(body)
    await service.telegram_update(body)
    await drain(service)
    assert service.instagram.sent == [('customer', 'Здравствуйте! Доставка возможна.')]
    drafts = service.repo.rows(Draft, Draft.origin == 'manual')
    assert len(drafts) == 1 and drafts[0].status == 'sent'
    assert drafts[0].manager_id == 1 and drafts[0].category == 'manual_reply'
    assert service.repo.get(Draft, template.id).status == 'cancelled'
    assert service.repo.get(Manager, 1).first_name == 'Manager'
    outgoing = service.repo.rows(Message, Message.direction == 'outgoing')[0]
    assert outgoing.raw['authorization'] == 'telegram_reply' and outgoing.raw['approved_by'] == 1
    assert outgoing.sender_type == 'manager'
    assert service.telegram.sent[-1][2] is None
    assert 'Менеджер: Manager' in service.telegram.edited[-1][1]
    await service.dispatch(drafts[0].id)
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_actual_aiogram_poll_to_database_to_dispatch(service):
    original, _ = await direct_prepare(service)
    parsed = Update.model_validate(reply_update(service, original))
    calls = 0
    async def get_updates(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError()
        return [parsed]
    service.telegram.bot = SimpleNamespace(get_updates=get_updates)
    with pytest.raises(asyncio.CancelledError):
        await poll_telegram(service)
    job = service.repo.rows(Job, Job.kind == 'telegram')[0]
    assert job.payload['message']['from']['id'] == 1
    assert 'from_user' not in job.payload['message']
    await drain(service)
    assert len(service.instagram.sent) == 1
    assert service.repo.state('telegram_offset') == str(parsed.update_id + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['plain_note', 'wrong_target', 'other_client', 'unauthorized', 'old', 'disabled', 'edited', 'long'])
async def test_only_new_authorized_text_reply_to_this_client_sends(service, case):
    original, template = await direct_prepare(service)
    body = reply_update(service, original)
    msg = body['message']
    if case == 'plain_note':
        msg.pop('reply_to_message')
    elif case == 'wrong_target':
        msg['reply_to_message']['message_id'] = template.telegram_message_id
    elif case == 'other_client':
        ingest(service, 'Вопрос', 'other', sender='another-customer')
        await drain(service)
        msg['message_thread_id'] = 52
    elif case == 'unauthorized':
        msg['from']['id'] = 999
    elif case == 'old':
        msg['date'] = int(service.settings.direct_reply_since) - 1
    elif case == 'disabled':
        service.settings.direct_reply_since = 0
    elif case == 'edited':
        body['edited_message'] = body.pop('message')
    elif case == 'long':
        msg['text'] = 'a' * 1001
    await service.telegram_update(body)
    await drain(service)
    assert not service.instagram.sent
    if case in ('old', 'disabled'):
        manual = service.repo.rows(Draft, Draft.origin == 'manual')[0]
        assert manual.status == 'pending' and manual.manager_id is None


@pytest.mark.asyncio
async def test_anonymous_sender_warns_without_bypassing_manager_check(service):
    original, _ = await direct_prepare(service)
    body = reply_update(service, original)
    body['message']['sender_chat'] = body['message']['chat']
    body['message']['from'] = {'id': 1087968824, 'is_bot': True}
    await service.telegram_update(body)
    await drain(service)
    assert not service.instagram.sent
    assert not service.repo.rows(Draft, Draft.origin == 'manual')
    assert any('личного аккаунта' in text for _, text, _ in service.telegram.sent)


@pytest.mark.asyncio
async def test_two_explicit_replies_do_not_cancel_each_other(service):
    original, _ = await direct_prepare(service)
    await service.telegram_update(reply_update(service, original, text='Первая часть'))
    await service.telegram_update(reply_update(service, original, mid=902, text='Вторая часть'))
    await drain(service)
    assert service.instagram.sent == [('customer', 'Первая часть'), ('customer', 'Вторая часть')]


@pytest.mark.asyncio
async def test_expired_instagram_window_reports_failure_without_sending(service):
    original, _ = await direct_prepare(service)
    service.repo.change(Client, 1, last_customer_at=time.time() - 90000)
    await service.telegram_update(reply_update(service, original))
    await drain(service)
    manual = service.repo.rows(Draft, Draft.origin == 'manual')[0]
    assert manual.status == 'failed' and '24-часовое' in manual.error
    assert not service.instagram.sent
    assert '24-часовое' in service.telegram.edited[-1][1]


@pytest.mark.asyncio
async def test_manual_reply_cancels_unsent_auto_price(service):
    from .test_auto_prices import setup_auto, prepare_auto
    setup_auto(service)
    service.settings.direct_reply_since = time.time() - 10
    auto = await prepare_auto(service, send=False)
    await service.present(auto.id)
    original = service.repo.get(Message, auto.source_message_id)
    await service.telegram_update(reply_update(service, original))
    await drain(service)
    assert service.repo.get(Draft, auto.id).status == 'cancelled'
    assert service.instagram.sent == [('customer', 'Здравствуйте! Доставка возможна.')]


@pytest.mark.asyncio
async def test_manager_reply_prevents_late_automatic_draft(service):
    service.settings.direct_reply_since = time.time() - 10
    mid = ingest(service)
    await drain(service)
    original = service.repo.get(Message, mid)
    await service.telegram_update(reply_update(service, original))
    await service.classify_client(1)
    await drain(service)
    assert len(service.repo.rows(Draft)) == 1
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', [True, False])
async def test_callback_sender_survives_aiogram_roundtrip(service, alias):
    _, draft = await direct_prepare(service)
    p = Update.model_validate({'update_id': 6000, 'callback_query': {'id': 'cb', 'chat_instance': 'test',
        'from': {'id': 1, 'first_name': 'Manager', 'is_bot': False}, 'data': f'ig:send:{draft.id}',
        'message': {'message_id': draft.telegram_message_id, 'date': int(time.time()),
            'chat': {'id': -1001, 'type': 'supergroup'}, 'message_thread_id': 51}}})
    await service.telegram_update(p.model_dump(mode='json', exclude_none=True, by_alias=alias))
    await drain(service)
    assert service.repo.get(Draft, draft.id).status == 'sent'
    assert len(service.instagram.sent) == 1


@pytest.mark.asyncio
async def test_unknown_result_not_replayed_after_restart(service):
    from instagram_inbox.instagram import SendError
    original, _ = await direct_prepare(service)
    service.instagram.error = SendError('Unknown outcome', retry_safe=False)
    await service.telegram_update(reply_update(service, original))
    await drain(service)
    manual = service.repo.rows(Draft, Draft.origin == 'manual')[0]
    service.repo.recover()
    await service.dispatch(manual.id)
    await drain(service)
    assert len(service.instagram.sent) == 1 and not manual.retry_safe


@pytest.mark.asyncio
async def test_real_permission_function_still_requires_manager_membership(service):
    async def get_chat_member(chat, uid):
        return SimpleNamespace(status='member')
    api = SimpleNamespace(settings=service.settings, bot=SimpleNamespace(get_chat_member=get_chat_member))
    assert not await TelegramAPI.authorized(api, {'id': 999})
    assert not await TelegramAPI.authorized(api, {'id': 1, 'is_bot': True})
    service.settings.manager_ids = (1,)
    assert await TelegramAPI.authorized(api, {'id': 1})
