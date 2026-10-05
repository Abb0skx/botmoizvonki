import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from test_market_quotes import NOW, OWN
from test_telegram_market_stats import analyzer
from telegram_market_stats.direct_requests import is_direct_request
from telegram_market_stats.private_collector import PrivateQuotesCollector
from telegram_market_stats.private_store import PrivateStore, SCHEMA, load_messages, load_requests
from telegram_market_stats.repository import MarketStatsRepository


def message(mid, text, seconds=0, *, out=False, **kw):
    return NS(id=mid, date=NOW+timedelta(seconds=seconds), message=text, out=out, **kw)


class Client:
    def __init__(self, chats):
        self.chats = chats
        self.calls = []

    async def iter_dialogs(self, **kw):
        for chat, messages in sorted(self.chats.items(), key=lambda item: max(m.date for m in item[1]), reverse=True):
            yield NS(id=chat, input_entity=chat, is_user=True, message=max(messages, key=lambda m:m.id),
                     entity=NS(first_name=f'Supplier {chat}', last_name='', username=None, bot=False))

    async def get_messages(self, entity, **kw):
        self.calls.append((entity, kw))
        values = self.chats[entity]
        if 'ids' in kw:
            return [next((m for m in values if m.id == mid), NS(id=mid,date=None)) for mid in kw['ids']]
        values = [m for m in values if m.id > kw.get('min_id',0) and m.id < kw.get('max_id',10**20)]
        return sorted(values,key=lambda m:m.id,reverse=not kw.get('reverse',False))[:kw.get('limit',100)]


def setup(tmp_path, *, known=(10,11)):
    repo = MarketStatsRepository(tmp_path/'market.db')
    repo.configure(-1001, 'Market', {int(OWN): 'TEXNIKACH'}, NOW)
    current = analyzer()
    for index, sender in enumerate(known):
        repo.upsert_message(group_id=-1001,message_id=index+1,sender_id=sender,telegram_date=NOW,
            edited_at=None,text='bor',analysis=current.analyze('bor'),competitor_ids=set(),processed_at=NOW)
    clock = lambda: NOW+timedelta(hours=1)
    return repo, PrivateQuotesCollector(repo,current,tmp_path/'missing.db',clock=clock)


def rows(repo):
    with repo.connect() as db:
        return load_messages(db,(NOW-timedelta(days=2)).isoformat(),(NOW+timedelta(days=1)).isoformat())


@pytest.mark.parametrize('text',[
    'iPhone 16 Pro Max', 'Ищу iPhone 16 Pro Max 256 black',
    '16pm 256 qora bormi', 'Apple Watch Series 10', 'Nova Q99 kere',
    'Tab a11 128 wifi kere', '17 max 256 silver sim esim kere',
    '12/46mm light gold/burgundy kere????', 'X99 kere',
])
def test_eligible_direct_queries(text):
    assert is_direct_request(text,analyzer().analyze(text),outgoing=True)


@pytest.mark.parametrize('text',[
    'Продам iPhone 16 Pro Max', 'iPhone 16 Pro Max 1100$', 'iPhone 16 Pro Max 1100',
    'rahmat', 'Нужен номер карты', 'Pul kerak', 'Нужно 100 долларов', 'Как дела?', '1100',
    'Не нужен iPhone 16 Pro Max', 'iPhone 16 Pro Max получил',
    'iPhone 16 Pro Max kerak emas', '100 kere',
    'Курьер нужен в 5', '5 minut kere', 'Нужен 1 человек', 'Завтра в 5 нужен',
    'minut5 kere', 'odam2 kere', 'Нужен курьер B31', 'B31 dostavka kere',
])
def test_direct_query_excludes_supply_payment_and_chatter(text):
    assert not is_direct_request(text,analyzer().analyze(text),outgoing=True)


@pytest.mark.parametrize('text',['Nova Q99 kere','X99 kere'])
def test_unknown_model_code_stays_eligible(text):
    result=analyzer().analyze(text)
    assert not result.mentions
    assert is_direct_request(text,result,outgoing=True)


def test_direct_query_excludes_incoming_and_forwards():
    text='iPhone 16 Pro Max kere'
    result=analyzer().analyze(text)
    assert not is_direct_request(text,result,outgoing=False)
    assert not is_direct_request(text,result,outgoing=True,forwarded=True)


def test_without_group_request_captures_only_known_chat_local_window(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'iPhone 16 Pro Max kere',out=True),message(2,'1100',20),
                       message(3,'-5',420),message(4,'1000',421),message(5,'Спасибо',430,out=True)],
                   11:[message(1,'900',20)],999:[message(1,'iPhone 16 Pro Max kere',out=True),message(2,'900',20)]})
    result=asyncio.run(collector.collect_once(client))
    assert 'error' not in result
    assert {(r['chat_id'],r['message_id']) for r in rows(repo)}=={('10',1),('10',2),('10',3)}
    assert {entity for entity,_ in client.calls}=={10,11}
    with repo.connect() as db:
        requests=load_requests(db,NOW.isoformat(),collector.clock().isoformat())
    assert len(requests)==1
    assert requests[0]['group_id']=='private:10' and requests[0]['source']=='private'
    assert requests[0]['text_excerpt']=='iPhone 16 Pro Max kere'


def test_unknown_model_final_bare_price_retained_only_for_matching_chat_query(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'Nova Q99 kere',out=True),message(2,'Nova Q99 330',20),
                       message(3,'Nova Q98 340',30),message(4,'Nova Q99 350',421)],
                   11:[message(1,'Nova Q99 320',20)]})
    result=asyncio.run(collector.collect_once(client))
    assert 'error' not in result
    assert {(r['chat_id'],r['message_id']) for r in rows(repo)}=={('10',1),('10',2)}


def test_unknown_model_bare_price_uses_stored_request_outside_current_page(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'Nova Q99 kere',out=True)]})
    asyncio.run(collector.collect_once(client))
    # Keep the request saved, but make this test's fetch batch contain only new
    # replies. Production may reach it again later in the bounded edit recheck.
    collector.store.recheck_ids=lambda *_args: []
    collector.store.pending_windows=lambda *_args: []
    client.chats[10].append(message(2,'Nova Q99 330',20))
    original=client.get_messages
    async def only_new(entity, **kw):
        return [m for m in await original(entity,**kw) if m.id!=1]
    client.get_messages=only_new
    result=asyncio.run(collector.collect_once(client))
    assert 'error' not in result
    assert {r['message_id'] for r in rows(repo)}=={1,2}


def test_edited_unknown_request_cannot_retain_old_model_bare_price(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'Nova Q99 kere',out=True),message(2,'Nova Q99 330',20)]})
    asyncio.run(collector.collect_once(client))
    client.chats[10][0]=message(1,'Nova Q98 kere',out=True,edit_date=NOW+timedelta(seconds=30))
    asyncio.run(collector.collect_once(client))
    assert {r['message_id'] for r in rows(repo)}=={1}


def test_restart_keeps_direct_window_and_deletion_removes_request(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'iPhone 16 Pro Max kere',out=True),message(2,'1100',20)]})
    asyncio.run(collector.collect_once(client))
    restarted=PrivateQuotesCollector(repo,analyzer(),tmp_path/'missing.db',clock=collector.clock)
    client.chats[10].append(message(3,'1090',100))
    asyncio.run(restarted.collect_once(client))
    assert len(rows(repo))==3
    client.chats[10]=[message(2,'1100',20),message(3,'1090',100)]
    asyncio.run(restarted.collect_once(client))
    assert restarted.store.requests(NOW.isoformat(),collector.clock().isoformat())==[]
    assert not rows(repo)  # orphan quotes no longer retained during the rescan


def test_edit_request_into_supply_unflags_it(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'iPhone 16 Pro Max kere',out=True),message(2,'1100',20)]})
    asyncio.run(collector.collect_once(client))
    client.chats[10][0]=message(1,'Продам iPhone 16 Pro Max',out=True,edit_date=NOW+timedelta(seconds=30))
    asyncio.run(collector.collect_once(client))
    assert collector.store.requests(NOW.isoformat(),collector.clock().isoformat())==[]


def test_replay_recovers_replies_across_history_page_after_restart(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'iPhone 16 Pro Max kere',out=True)]+
        [message(mid,'1100',mid) for mid in range(2,152)]})
    asyncio.run(collector.collect_once(client))
    assert not rows(repo)  # request is outside first history page
    asyncio.run(collector.collect_once(client))
    assert collector.store.requests(NOW.isoformat(),collector.clock().isoformat())
    restarted=PrivateQuotesCollector(repo,analyzer(),tmp_path/'missing.db',clock=collector.clock)
    for _ in range(3):
        asyncio.run(restarted.collect_once(client))
    assert {r['message_id'] for r in rows(repo)}==set(range(1,152))
    assert any(kw.get('reverse') and kw.get('min_id')==1 for _,kw in client.calls)


def test_old_completed_cursor_reopens_once_for_two_day_scope(tmp_path):
    repo=MarketStatsRepository(tmp_path/'market.db')
    old_schema=SCHEMA.replace(' is_direct_request INTEGER NOT NULL DEFAULT 0,\n','')
    with repo.connect() as db:
        db.executescript(old_schema)
        db.execute('INSERT INTO market_private_cursors VALUES(?,?,?,?,?,NULL)',('10',500,400,1,NOW.isoformat()))
        assert load_requests(db,NOW.isoformat(),NOW.isoformat())==[]
    store=PrivateStore(repo)
    assert store.cursors()['10']['before_id'] is None
    assert store.cursors()['10']['backfill_complete']==0
    store.save('10',[],500,300,True,NOW.isoformat())
    restarted=PrivateStore(repo)
    assert restarted.cursors()['10']['before_id']==300
    assert restarted.cursors()['10']['backfill_complete']==1


def test_request_edit_restarts_replay_even_if_old_window_completed_same_pass(tmp_path):
    repo,collector=setup(tmp_path)
    client=Client({10:[message(1,'iPhone 16 Pro Max kere',out=True),message(2,'1100',20)]})
    asyncio.run(collector.collect_once(client))
    client.chats[10][0]=message(1,'iPhone 16 Pro Max 512 kere',out=True,edit_date=NOW+timedelta(seconds=30))
    asyncio.run(collector.collect_once(client))
    pending=collector.store.pending_windows('10',NOW.isoformat())
    assert len(pending)==1 and pending[0]['last_message_id']==1
    asyncio.run(collector.collect_once(client))
    assert collector.store.pending_windows('10',NOW.isoformat())==[]


def test_group_windows_still_capture_other_supplier_prices(tmp_path):
    repo,collector=setup(tmp_path)
    current=analyzer()
    repo.upsert_message(group_id=-1001,message_id=99,sender_id=int(OWN),telegram_date=NOW,
        edited_at=None,text='iPhone 16 Pro Max kere',analysis=current.analyze('iPhone 16 Pro Max kere'),
        competitor_ids={int(OWN)},processed_at=NOW)
    client=Client({10:[message(1,'1100',20)],11:[message(1,'1090',30)]})
    asyncio.run(collector.collect_once(client))
    assert len(rows(repo))==2
    assert all(not r['is_direct_request'] for r in rows(repo))
