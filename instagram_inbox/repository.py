"""Short DB transactions; external network calls never run inside them."""
from contextlib import contextmanager
import time
from sqlalchemy import create_engine, event, select, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from .models import Base, Client, Draft, Job, Manager, Message, State


class Repository:
    def __init__(self, url):
        kw = {'connect_args': {'check_same_thread': False, 'timeout': 20}} if url.startswith('sqlite') else {}
        self.engine = create_engine(url, **kw)
        if url.startswith('sqlite'):
            @event.listens_for(self.engine, 'connect')
            def configure(connection, _):
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA busy_timeout=20000')
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.Session.begin() as session:
            yield session

    def get(self, model, ident):
        with self.Session() as s:
            return s.get(model, ident)

    def rows(self, model, *conditions, limit=100, order=None):
        with self.Session() as s:
            stmt = select(model).where(*conditions)
            if order is not None:
                stmt = stmt.order_by(order)
            return list(s.scalars(stmt.limit(limit)))

    def change(self, model, ident, **values):
        with self.transaction() as s:
            s.execute(update(model).where(model.id == ident).values(**values))

    def state(self, key, default=''):
        row = self.get(State, key)
        return row.value if row else default

    @staticmethod
    def put_state(s, key, value):
        row = s.get(State, key)
        if row:
            row.value = str(value)
        else:
            s.add(State(key=key, value=str(value)))

    @staticmethod
    def enqueue(s, key, kind, payload):
        if not s.scalar(select(Job.id).where(Job.key == key)):
            s.add(Job(key=key, kind=kind, payload=payload))

    def ingest(self, incoming, debounce):
        now = time.time()
        for attempt in range(3):
            try:
                with self.transaction() as s:
                    if s.scalar(select(Message.id).where(Message.external_id == incoming['external_id'])):
                        return None
                    c = s.scalar(select(Client).where(Client.account_id == incoming['account_id'],
                                                      Client.external_id == incoming['sender_id']).with_for_update())
                    if c is None:
                        c = Client(account_id=incoming['account_id'], external_id=incoming['sender_id'])
                        s.add(c)
                        s.flush()
                    direction = incoming.get('direction', 'incoming')
                    # Atomic increment serializes concurrent events even with SQLite's deferred transactions.
                    s.execute(update(Client).where(Client.id == c.id).values(
                        revision=Client.revision + 1, status='active', due_at=now + debounce,
                        last_message_at=now, updated_at=now))
                    s.refresh(c)
                    if direction == 'incoming':
                        c.last_customer_at = max(c.last_customer_at, min(now, incoming['created_at']))
                    if incoming.get('username'):
                        c.username = incoming['username'][:150]
                    s.execute(update(Draft).where(Draft.client_id == c.id,
                        or_(Draft.status.in_(['pending', 'failed']),
                            (Draft.status == 'sending') & Draft.dispatch_started_at.is_(None)))
                        .values(status='cancelled', error='superseded', updated_at=now))
                    m = Message(client_id=c.id, external_id=incoming['external_id'],
                        direction=direction, sender_type='client' if direction == 'incoming' else 'manager',
                        text=incoming['text'], attachments=incoming['attachments'],
                        message_type=incoming['message_type'], raw=incoming['raw'], revision=c.revision,
                        reply_to_external_id=incoming.get('reply_to', ''), created_at=incoming['created_at'])
                    s.add(m)
                    s.flush()
                    self.enqueue(s, f'in:{m.id}', 'incoming', {'message_id': m.id})
                    return m.id
            except IntegrityError:
                if attempt == 2:
                    raise

    def history(self, client_id, limit):
        return list(reversed(self.rows(Message, Message.client_id == client_id,
                    Message.direction != 'internal', limit=limit, order=Message.id.desc())))

    def draft(self, client_id, revision, source_id, text, category, origin='ai', replaces_id=None):
        with self.transaction() as s:
            if replaces_id and s.scalar(select(Draft.id).where(Draft.replaces_id == replaces_id)):
                return None
            # A write against the client row closes the race with a new inbound event.
            result = s.execute(update(Client).where(Client.id == client_id, Client.revision == revision)
                               .values(classified_revision=revision))
            if result.rowcount != 1:
                return None
            d = Draft(client_id=client_id, revision=revision, source_message_id=source_id,
                      text=text, category=category, origin=origin, replaces_id=replaces_id)
            s.add(d)
            s.flush()
            self.enqueue(s, f'draft:{d.id}', 'present', {'draft_id': d.id})
            return d.id

    def approve(self, draft_id, manager, retry=False):
        now = time.time()
        with self.transaction() as s:
            d = s.get(Draft, draft_id)
            if d is None:
                return 'not_found'
            c = s.get(Client, d.client_id)
            if c.revision != d.revision:
                return 'superseded'
            allowed = 'failed' if retry else 'pending'
            predicates = [Draft.id == draft_id, Draft.status == allowed,
                          Draft.revision == select(Client.revision).where(Client.id == d.client_id).scalar_subquery()]
            if retry:
                predicates.append(Draft.retry_safe.is_(True))
            result = s.execute(update(Draft).where(*predicates).values(
                status='sending', manager_id=manager['id'], manager_name=manager.get('first_name', '')[:180],
                error='', retry_safe=False, dispatch_started_at=None, updated_at=now))
            if result.rowcount != 1:
                return d.status
            s.merge(Manager(id=manager['id'], username=manager.get('username', '')[:150],
                            first_name=manager.get('first_name', '')[:180], updated_at=now))
            self.enqueue(s, f'send:{draft_id}:{now}', 'send', {'draft_id': draft_id})
            return 'approved'

    def cancel(self, draft_id, manager_id, reason='manager_cancelled'):
        with self.transaction() as s:
            result = s.execute(update(Draft).where(Draft.id == draft_id, Draft.status == 'pending')
                .values(status='cancelled', manager_id=manager_id, error=reason, updated_at=time.time()))
            return result.rowcount == 1

    def store_telegram_update(self, body):
        with self.transaction() as s:
            self.enqueue(s, f'tg:{body["update_id"]}', 'telegram', body)
            self.put_state(s, 'telegram_offset', body['update_id'] + 1)

    def claim_job(self):
        with self.transaction() as s:
            row = s.scalar(select(Job).where(Job.status == 'pending', Job.due_at <= time.time()).order_by(Job.id).limit(1))
            if row is None:
                return None
            r = s.execute(update(Job).where(Job.id == row.id, Job.status == 'pending').values(
                status='running', attempts=Job.attempts + 1, updated_at=time.time()))
            if r.rowcount != 1:
                return None
            s.refresh(row)
            return row

    def recover(self):
        # Only called after acquiring the exclusive worker lock, never while another worker runs.
        with self.transaction() as s:
            s.execute(update(Job).where(Job.status == 'running').values(status='pending'))
            s.execute(update(Draft).where(Draft.status == 'sending', Draft.dispatch_started_at.is_not(None))
                .values(status='failed', retry_safe=False, error='Результат отправки неизвестен. Проверьте Instagram.'))
            s.execute(update(Client).where(Client.topic_state == 'creating').values(topic_state='uncertain'))
            s.execute(update(Message).where(Message.telegram_state == 'sending').values(telegram_state='uncertain'))
            s.execute(update(Draft).where(Draft.presentation_state == 'sending').values(presentation_state='uncertain'))
