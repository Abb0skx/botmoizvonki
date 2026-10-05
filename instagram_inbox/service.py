import asyncio
from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo
from sqlalchemy import select, update
from .replies import Classification, has_contact_details
from .instagram import SendError
from .models import Client, Draft, Job, Manager, Message, TelegramLink
from .telegram import keyboard

log = logging.getLogger(__name__)
HELP = ('Instagram → Telegram\n\nКаждый клиент — отдельная постоянная тема. '
        'При включённом автоответе точная модель получает цену и запрос адреса/телефона автоматически. '
        'Данные заказа проверяет менеджер. Остальные шаблоны — после подтверждения.\n'
        'Кнопка «Отправить» подтверждает отправку шаблона клиенту.\n'
        'Свой ответ: сделайте Reply на сообщение клиента. При включённом прямом Reply он сразу уйдёт в Direct, без кнопки. '
        'Обычные сообщения внутри темы остаются внутренними.\n'
        '/close — закрыть тему; новое сообщение откроет её.\n'
        '/status — состояние сервиса. Отвечать могут администраторы группы и разрешённые менеджеры.')


class InboxService:
    def __init__(self, repo, settings, telegram, instagram, assistant):
        self.repo, self.settings = repo, settings
        self.telegram, self.instagram, self.assistant = telegram, instagram, assistant

    def auto_allowed(self, classification, batch):
        return bool(self.settings.auto_price_enabled and self.settings.auto_price_since > 0
            and classification.category == 'price_question' and classification.source == 'bot_prices'
            and classification.auto_eligible and batch
            and all(m.direction == 'incoming' and m.telegram_state == 'sent' and m.created_at >= max(
                self.settings.auto_price_since, time.time() - 900) for m in batch))

    def active_order(self, client_id):
        # The messages themselves retain the phone/address; do not copy PII into
        # public Sheets or extract/guess an address. This marker survives restarts.
        sent_at = float(self.repo.state(f'order_quote:{client_id}', '0'))
        return sent_at > 0 and time.time() - sent_at < 72 * 3600

    def close_order(self, client_id):
        with self.repo.transaction() as s:
            self.repo.put_state(s, f'order_quote:{client_id}', '0')

    async def ensure_topic(self, client_id):
        c = self.repo.get(Client, client_id)
        if c.topic_id:
            if c.topic_state == 'closed':
                await self.telegram.reopen(c.topic_id)
                self.repo.change(Client, c.id, topic_state='open', status='active')
                log.info('topic_reopened client=%s topic=%s', c.id, c.topic_id)
            return c.topic_id
        if c.topic_state in ('creating', 'uncertain'):
            raise RuntimeError(f'Topic creation uncertain for client {c.id}; bind existing topic before retry')
        profile = await self.instagram.profile(c.external_id)
        username = str(profile.get('username') or c.username)[:150]
        display = str(profile.get('name') or c.display_name)[:150]
        name = ('IG • ' + ('@' + username if username else display or c.external_id[-12:]))[:128]
        with self.repo.transaction() as s:
            result = s.execute(update(Client).where(Client.id == c.id, Client.topic_state == 'new', Client.topic_id.is_(None))
                .values(topic_state='creating', username=username, display_name=display, topic_name=name))
            if result.rowcount != 1:
                raise RuntimeError('Topic already claimed')
        try:
            topic_id = await self.telegram.create_topic(name)
        except Exception:
            self.repo.change(Client, c.id, topic_state='uncertain')
            raise
        self.repo.change(Client, c.id, topic_id=topic_id, topic_state='open')
        log.info('topic_created client=%s topic=%s', c.id, topic_id)
        return topic_id

    def link(self, message_id, telegram_id):
        with self.repo.transaction() as s:
            s.merge(TelegramLink(telegram_id=telegram_id, message_id=message_id))

    async def publish_incoming(self, message_id):
        m = self.repo.get(Message, message_id)
        if m.telegram_state == 'uncertain':
            raise RuntimeError('Telegram delivery needs reconciliation')
        if m.telegram_state != 'pending':
            return
        topic = await self.ensure_topic(m.client_id)
        c = self.repo.get(Client, m.client_id)
        prefix = '👤 ' + ('@' + c.username if c.username else c.display_name or 'Клиент')
        if m.direction == 'outgoing':
            prefix = '↗️ Ответ из Instagram'
        text = prefix + '\n' + (m.text or ('📎 ' + m.message_type))
        reply_id = None
        if m.reply_to_external_id:
            previous = self.repo.rows(Message, Message.external_id == m.reply_to_external_id, limit=1)
            if previous:
                reply_id = previous[0].telegram_message_id
        self.repo.change(Message, m.id, telegram_state='sending')
        try:
            ids = []
            for i in range(0, len(text), 3800):
                mid = await self.telegram.text(topic, text[i:i+3800], reply_id=reply_id)
                ids.append(mid)
                self.link(m.id, mid)
                if i == 0:
                    self.repo.change(Message, m.id, telegram_message_id=mid)
            for attachment in m.attachments:
                mid = await self.telegram.attachment(topic, attachment, ids[0])
                self.link(m.id, mid)
            self.repo.change(Message, m.id, telegram_state='sent')
        except Exception:
            self.repo.change(Message, m.id, telegram_state='uncertain')
            raise
        log.info('telegram_message_sent message=%s client=%s', m.id, c.id)

    async def classify_client(self, client_id, previous=''):
        c = self.repo.get(Client, client_id)
        history = self.repo.history(c.id, self.settings.context_messages)
        batch = self.repo.rows(Message, Message.client_id == c.id, Message.revision > c.classified_revision,
                              Message.direction == 'incoming', order=Message.id, limit=100)
        if not batch:
            self.repo.change(Client, c.id, classified_revision=c.revision)
            return
        if any(m.telegram_state == 'pending' for m in batch):
            return
        try:
            classification = await self.assistant.classify(batch, history)
            if (self.active_order(c.id) and (classification.category != 'price_question'
                    or any(has_contact_details(m.text) for m in batch))
                    and not all(m.message_type in ('reaction', 'deleted') for m in batch)):
                classification = Classification(action='manager_only', category='order_details', requires_reply=False,
                    confidence=1, reason='📦 Продолжение заказа в Direct: проверьте адрес, телефон, цвет и доставку. '
                    'Ответьте клиенту здесь через Reply. Заказ ещё не подтверждён.')
            reply = await self.assistant.propose(classification, batch, history, previous) if classification.requires_reply else None
        except Exception as exc:
            log.error('template_failed client=%s error_type=%s', c.id, type(exc).__name__)
            classification = Classification(action='manager_only', category='unclear', requires_reply=False,
                confidence=0, reason='Нужен ответ менеджера — не удалось загрузить или проверить шаблоны.')
            reply = None
        with self.repo.transaction() as s:
            result = s.execute(update(Client).where(Client.id == c.id, Client.revision == c.revision,
                               Client.classified_revision == c.classified_revision)
                               .values(classified_revision=c.revision))
            if result.rowcount != 1:
                return
            s.execute(update(Message).where(Message.id.in_([m.id for m in batch])).values(
                classification={**classification.model_dump(), 'batch_ids': [m.id for m in batch]},
                requires_reply=classification.requires_reply))
            if reply:
                automatic = self.auto_allowed(classification, batch)
                d = Draft(client_id=c.id, revision=c.revision, source_message_id=batch[-1].id,
                          text=reply, category=classification.category, origin='auto_price' if automatic else 'template')
                s.add(d)
                s.flush()
                self.repo.enqueue(s, f'draft:{d.id}', 'present', {'draft_id': d.id})
            elif c.topic_id:
                self.repo.enqueue(s, f'status:{c.id}:{c.revision}', 'status', {
                    'topic_id': c.topic_id, 'text': classification.reason[:350],
                    'reply_id': batch[-1].telegram_message_id})
        log.info('classification client=%s category=%s action=%s', c.id, classification.category, classification.action)

    async def present(self, draft_id):
        d = self.repo.get(Draft, draft_id)
        c = self.repo.get(Client, d.client_id)
        if d.presentation_state == 'uncertain':
            raise RuntimeError('Draft publication needs reconciliation')
        if d.status != 'pending' or d.revision != c.revision or d.presentation_state != 'pending':
            return
        source = self.repo.get(Message, d.source_message_id)
        automatic = d.origin == 'auto_price'
        direct_reply = d.category == 'manual_reply' and d.manager_id is not None
        immediate = automatic or direct_reply
        title = ('⏳ Ответ менеджера — отправляем в Direct' if direct_reply else
                 '⏳ Автоответ: проверяем цену перед отправкой' if automatic else
                 'Отправить клиенту?' if d.origin == 'manual' else '📝 Готовый ответ по шаблону')
        if d.category == 'price_question' and d.origin != 'manual':
            title += '\nИсточник: bot_prices • цены в сумах'
        self.repo.change(Draft, d.id, presentation_state='sending')
        try:
            tid = await self.telegram.text(c.topic_id, title + '\n\n' + d.text,
                None if immediate else keyboard(d.id, manual=d.origin == 'manual'), reply_id=source.telegram_message_id)
            with self.repo.transaction() as s:
                saved = s.get(Draft, d.id)
                saved.telegram_message_id, saved.presentation_state = tid, 'sent'
                if immediate and saved.status == 'pending' and s.get(Client, c.id).revision == d.revision:
                    saved.status = 'sending'
                    self.repo.enqueue(s, f'immediate-send:{d.id}', 'send', {'draft_id': d.id})
        except Exception:
            self.repo.change(Draft, d.id, presentation_state='uncertain')
            raise

    async def render_status(self, draft_id):
        d = self.repo.get(Draft, draft_id)
        if not d.telegram_message_id:
            return
        markup = None
        if d.status == 'sent':
            stamp = datetime.fromtimestamp(d.sent_at, ZoneInfo('Asia/Tashkent')).strftime('%H:%M')
            author = 'Автоответ по прайсу' if d.origin == 'auto_price' and d.manager_id is None else f'Менеджер: {d.manager_name or d.manager_id}'
            header = f'✅ Отправлено\n{author}\n{stamp}'
        elif d.status == 'failed':
            header = '❌ Не удалось подтвердить отправку.\n' + d.error
            if d.retry_safe:
                markup = keyboard(d.id, retry=True)
        elif d.status == 'cancelled':
            header = {'superseded': 'Отменено: клиент написал новое сообщение.',
                      'price_changed': 'Прайс или шаблон изменился. Подтвердите новый черновик ниже.'}.get(d.error, 'Не отвечаем.')
        elif d.status == 'pending':
            header = '⚠️ Требуется подтверждение менеджера.\n' + d.error
            markup = keyboard(d.id, manual=d.origin == 'manual')
        else:
            header = '⏳ Отправляем…'
        await self.telegram.edit(d.telegram_message_id, header + '\n\n' + d.text, markup)

    async def hold_auto(self, draft, reason):
        with self.repo.transaction() as s:
            s.execute(update(Draft).where(Draft.id == draft.id, Draft.status == 'sending',
                Draft.dispatch_started_at.is_(None)).values(status='pending', origin='template',
                    manager_id=None, manager_name='', error=reason))
        await self.render_status(draft.id)

    async def dispatch(self, draft_id):
        d = self.repo.get(Draft, draft_id)
        c = self.repo.get(Client, d.client_id)
        if d.status in ('sent', 'failed', 'cancelled'):
            await self.render_status(d.id)
            return
        if d.status == 'pending' and d.error:
            await self.render_status(d.id)
            return
        if d.status != 'sending' or d.dispatch_started_at is not None:
            return
        if c.revision != d.revision:
            self.repo.change(Draft, d.id, status='cancelled', error='superseded')
            return
        if time.time() - c.last_customer_at > 24 * 3600:
            self.repo.change(Draft, d.id, status='failed', error='Истекло 24-часовое окно ответа Instagram. Нужно новое сообщение клиента.', retry_safe=False)
            await self.render_status(d.id)
            return
        automatic = d.origin == 'auto_price' and d.manager_id is None
        if automatic and not self.auto_allowed(Classification.model_validate(
                self.repo.get(Message, d.source_message_id).classification or {
                    'action': 'ignore', 'category': '', 'requires_reply': False, 'confidence': 0, 'reason': ''}),
                self.draft_batch(self.repo.get(Message, d.source_message_id))):
            await self.hold_auto(d, 'Автоответ выключен или сообщение не подходит для автоматической отправки.')
            return
        if d.origin != 'manual' and d.category == 'price_question':
            # Source may change while the manager reads the draft. Never silently
            # substitute text after approval; changed prices require a new approval.
            source = self.repo.get(Message, d.source_message_id)
            batch = self.draft_batch(source)
            try:
                check = await self.assistant.classify(batch, self.repo.history(c.id, self.settings.context_messages), force=True)
                current = check.reply_text if check.requires_reply else None
            except Exception:
                current = None
            if automatic:
                if not current or not self.auto_allowed(check, batch):
                    await self.hold_auto(d, 'Автоответ не отправлен: нужна проверка актуального прайса и модели.')
                    return
                # No manager approved this draft: use the just-refreshed price.
                # Store the exact outbound text atomically with the dispatch claim.
                d.text = current
            if not current:
                with self.repo.transaction() as s:
                    restored = s.execute(update(Draft).where(Draft.id == d.id, Draft.status == 'sending',
                        Draft.dispatch_started_at.is_(None),
                        Draft.revision == select(Client.revision).where(Client.id == c.id).scalar_subquery())
                        .values(status='pending', manager_id=None, manager_name=''))
                if restored.rowcount:
                    await self.telegram.edit(d.telegram_message_id,
                        '⚠️ Не отправлено: не удалось проверить актуальные цены. Обновите ответ или напишите вручную.\n\n' + d.text,
                        keyboard(d.id))
                return
            if not automatic and current != d.text:
                with self.repo.transaction() as s:
                    claimed = s.execute(update(Draft).where(Draft.id == d.id, Draft.status == 'sending',
                        Draft.dispatch_started_at.is_(None)).values(status='cancelled', error='price_changed'))
                    if claimed.rowcount and current and s.get(Client, c.id).revision == d.revision:
                        replacement = Draft(client_id=c.id, revision=c.revision, source_message_id=source.id,
                                            text=current, category=check.category, replaces_id=d.id)
                        s.add(replacement)
                        s.flush()
                        self.repo.enqueue(s, f'draft:{replacement.id}', 'present', {'draft_id': replacement.id})
                await self.render_status(d.id)
                return
        with self.repo.transaction() as s:
            authorization = Draft.origin == 'auto_price' if automatic else Draft.manager_id.is_not(None)
            claimed = s.execute(update(Draft).where(Draft.id == d.id, Draft.status == 'sending',
                Draft.dispatch_started_at.is_(None), authorization,
                Draft.revision == select(Client.revision).where(Client.id == c.id).scalar_subquery())
                .values(dispatch_started_at=time.time(), text=d.text))
            if claimed.rowcount != 1:
                return
        try:
            mid = await self.instagram.send(c.external_id, d.text)
        except SendError as exc:
            self.repo.change(Draft, d.id, status='failed', error=str(exc), retry_safe=exc.retry_safe)
            await self.render_status(d.id)
            return
        except Exception:
            self.repo.change(Draft, d.id, status='failed', error='Результат неизвестен. Проверьте Instagram.', retry_safe=False)
            raise
        with self.repo.transaction() as s:
            saved = s.get(Draft, d.id)
            saved.status, saved.external_message_id, saved.sent_at = 'sent', mid, time.time()
            if d.category == 'price_question' and d.origin != 'manual':
                self.repo.put_state(s, f'order_quote:{c.id}', saved.sent_at)
            if not s.scalar(select(Message.id).where(Message.external_id == mid)):
                s.add(Message(client_id=c.id, external_id=mid, text=d.text, direction='outgoing',
                    sender_type='bot' if automatic else 'manager', telegram_state='sent', telegram_message_id=d.telegram_message_id,
                    revision=c.revision, raw={'approved_by': d.manager_id, 'draft_id': d.id, 'automated': automatic,
                        'authorization': 'auto_price' if automatic else 'telegram_reply' if d.category == 'manual_reply' else 'button'}))
        log.info('instagram_send_success draft=%s manager=%s', d.id, d.manager_id)
        await self.render_status(d.id)

    async def callback(self, callback):
        msg, user = callback.get('message') or {}, callback.get('from') or callback.get('from_user') or {}
        if msg.get('chat', {}).get('id') != self.settings.telegram_group_id:
            return
        if not await self.telegram.authorized(user):
            await self.telegram.answer(callback['id'], 'Недостаточно прав.')
            return
        parts = str(callback.get('data', '')).split(':')
        if len(parts) != 3 or parts[0] != 'ig' or not parts[2].isdigit():
            return
        action, did = parts[1], int(parts[2])
        d = self.repo.get(Draft, did)
        if not d or d.telegram_message_id != msg.get('message_id'):
            return
        c = self.repo.get(Client, d.client_id)
        if msg.get('message_thread_id') != c.topic_id:
            return
        if action in ('send', 'retry'):
            state = self.repo.approve(did, user, retry=action == 'retry')
            answer = {'approved': 'Принято, отправляем.', 'sent': 'Сообщение уже отправлено.',
                      'sending': 'Отправка уже выполняется.', 'superseded': 'Есть новое сообщение клиента.',
                      'cancelled': 'Черновик отменён.', 'failed': 'Проверьте результат предыдущей отправки.'}.get(state, 'Действие недоступно.')
            await self.telegram.answer(callback['id'], answer)
            log.info('manager_send_confirmation draft=%s manager=%s result=%s', did, user['id'], state)
        elif action in ('cancel', 'regenerate'):
            if self.repo.cancel(did, user['id'], 'regenerated' if action == 'regenerate' else 'manager_cancelled'):
                if action == 'regenerate' and c.revision == d.revision:
                    with self.repo.transaction() as s:
                        self.repo.enqueue(s, f'regen:{did}', 'regenerate', {'draft_id': did})
                await self.telegram.answer(callback['id'], 'Готово.')
                await self.render_status(did)
            else:
                await self.telegram.answer(callback['id'], 'Черновик уже обработан.')

    async def telegram_update(self, payload):
        if payload.get('callback_query'):
            await self.callback(payload['callback_query'])
            return
        msg = payload.get('message') or {}
        if msg.get('chat', {}).get('id') != self.settings.telegram_group_id:
            return
        topic = msg.get('message_thread_id')
        clients = self.repo.rows(Client, Client.topic_id == topic, limit=1) if topic else []
        c = clients[0] if clients else None
        if c and ('forum_topic_closed' in msg or 'forum_topic_reopened' in msg):
            closed = 'forum_topic_closed' in msg
            self.repo.change(Client, c.id, topic_state='closed' if closed else 'open', status='closed' if closed else 'active')
            if closed:
                self.close_order(c.id)
            return
        user = msg.get('from') or msg.get('from_user') or {}
        if c and msg.get('sender_chat') and msg.get('reply_to_message') and (msg.get('text') or msg.get('caption')):
            await self.telegram.text(topic, 'Не отправлено: отвечайте от своего личного аккаунта Telegram, '
                'не от имени группы или канала. Анонимного отправителя нельзя проверить как менеджера.',
                reply_id=msg['message_id'])
            return
        if not await self.telegram.authorized(user):
            return
        text = str(msg.get('text') or msg.get('caption') or '')
        command = text.split()[0].split('@')[0] if text else ''
        if command in ('/start', '/help'):
            await self.telegram.text(topic, HELP)
            return
        if command == '/status':
            mode = 'Точные модели: автоматический ответ с ценой, запрос адреса и телефона.' if self.settings.auto_price_enabled else 'Все ответы: после подтверждения менеджера.'
            manual_mode = ('Reply менеджера на сообщение клиента: сразу в Direct, без кнопки.'
                if self.settings.direct_reply_since > 0 else 'Reply менеджера: после кнопки «Отправить».')
            await self.telegram.text(topic, 'Instagram: ' + mode + '\n' + manual_mode + '\n'
                'Остальные шаблоны и подтверждение заказа: менеджер.\n'
                'Ответы: шаблоны Google Sheets + поиск модели в прайсе. AI не используется.\n'
                f'Кэш таблиц: {self.settings.sheets_cache_seconds} секунд. Перед отправкой цены проверяются повторно.')
            return
        if command == '/close' and c:
            await self.telegram.close_topic(topic)
            self.repo.change(Client, c.id, topic_state='closed', status='closed')
            self.close_order(c.id)
            return
        if command == '/bind' and user['id'] in self.settings.manager_ids and topic:
            parts = text.split()
            target = self.repo.get(Client, int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else None
            if target and not target.topic_id and target.topic_state == 'uncertain':
                self.repo.change(Client, target.id, topic_id=topic, topic_state='open')
                with self.repo.transaction() as s:
                    s.execute(update(Job).where(Job.kind == 'incoming', Job.status == 'failed').values(status='pending', attempts=0))
                await self.telegram.text(topic, 'Тема привязана к клиенту.')
            return
        if not c or not text.strip():
            return
        external = f'tg:{self.settings.telegram_group_id}:{msg["message_id"]}'
        if self.repo.rows(Message, Message.external_id == external, limit=1):
            return
        reply = msg.get('reply_to_message') or {}
        link = self.repo.get(TelegramLink, reply.get('message_id', 0))
        original = self.repo.get(Message, link.message_id) if link else None
        is_manual = bool(original and original.client_id == c.id and original.direction == 'incoming')
        if is_manual and len(text) > 1000:
            await self.telegram.text(topic, 'Ответ длиннее 1000 символов. Сократите текст и повторите Reply.')
            is_manual = False
        immediate = bool(is_manual and self.settings.direct_reply_since > 0
            and float(msg.get('date') or 0) >= self.settings.direct_reply_since)
        with self.repo.transaction() as s:
            s.add(Message(client_id=c.id, external_id=external, text=text, direction='internal',
                          sender_type='manager', telegram_state='sent', telegram_message_id=msg['message_id'],
                          raw={'manager_id': user['id'], 'direct_reply': immediate,
                               'reply_to': reply.get('message_id'), 'telegram_date': msg.get('date')}, revision=c.revision))
            if is_manual:
                s.execute(update(Draft).where(Draft.client_id == c.id,
                    Draft.category != 'manual_reply',
                    (Draft.status == 'pending') | ((Draft.status == 'sending') & Draft.dispatch_started_at.is_(None)))
                          .values(status='cancelled', error='manual_replacement'))
                # Do not generate a competing automatic reply after a manager has answered.
                s.execute(update(Client).where(Client.id == c.id, Client.revision == c.revision)
                    .values(classified_revision=c.revision))
                # The Reply itself authorizes this immutable text; edits never resend it.
                d = Draft(client_id=c.id, source_message_id=original.id, revision=c.revision,
                          text=text[:1000], category='manual_reply' if immediate else 'manual', origin='manual',
                          manager_id=user['id'] if immediate else None,
                          manager_name=user.get('first_name', '')[:180] if immediate else '')
                if immediate:
                    s.merge(Manager(id=user['id'], username=user.get('username', '')[:150],
                        first_name=user.get('first_name', '')[:180], updated_at=time.time()))
                s.add(d)
                s.flush()
                self.repo.enqueue(s, f'draft:{d.id}', 'present', {'draft_id': d.id})

    def draft_batch(self, source):
        ids = (source.classification or {}).get('batch_ids', [source.id])
        return self.repo.rows(Message, Message.client_id == source.client_id, Message.direction == 'incoming',
                              Message.id.in_(ids), order=Message.id, limit=100) or [source]

    async def regenerate(self, draft_id):
        d = self.repo.get(Draft, draft_id)
        c = self.repo.get(Client, d.client_id)
        if c.revision != d.revision:
            return
        history = self.repo.history(c.id, self.settings.context_messages)
        source = self.repo.get(Message, d.source_message_id)
        classification = await self.assistant.classify(self.draft_batch(source), history, force=True)
        text = await self.assistant.propose(classification, self.draft_batch(source), history)
        if text:
            self.repo.draft(c.id, c.revision, source.id, text, classification.category, replaces_id=d.id)
        else:
            await self.telegram.text(c.topic_id, classification.reason, reply_id=source.telegram_message_id)

    async def handle_job(self, job):
        if job.kind == 'incoming':
            await self.publish_incoming(job.payload['message_id'])
        elif job.kind == 'present':
            await self.present(job.payload['draft_id'])
        elif job.kind == 'send':
            await self.dispatch(job.payload['draft_id'])
        elif job.kind == 'telegram':
            await self.telegram_update(job.payload)
        elif job.kind == 'relay':
            await self.instagram.relay(job.payload)
        elif job.kind == 'regenerate':
            await self.regenerate(job.payload['draft_id'])
        elif job.kind == 'status':
            await self.telegram.text(job.payload['topic_id'], job.payload['text'], reply_id=job.payload.get('reply_id'))
