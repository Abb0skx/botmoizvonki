from datetime import timedelta
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from test_market_quotes import NOW, OWN, query, row
from telegram_market_stats.private_quotes import match_private, key, fingerprint, rank_offers
from telegram_market_stats.quotes import prices


def q(id=1, text='iPhone kere', **kw):
    return query(id, text, group_id='-1001', **kw)


def dm(id, text, *, chat='10', **kw):
    return row(id, text, chat_id=chat, outgoing=False, forwarded=False, **kw)


def test_overlapping_requests_price_near_far_and_no_cascade():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    linked, unknown = match_private([first, second], [dm(1, '1000', seconds=10),
        dm(2, '990', chat='11', seconds=65), dm(3, '120', chat='12', seconds=70),
        dm(4, '500', chat='13', seconds=75)])
    assert [o['method'] for o in linked[key(first)]] == ['private_time', 'price_near']
    assert [o['method'] for o in linked[key(second)]] == ['price_far', 'price_far']
    assert not unknown
    assert all(not o['comparable'] for o in linked[key(second)])


def test_close_reference_prices_are_ambiguous():
    first, second = q(model='iphone'), q(2, 'Watch kere', seconds=60, model='watch')
    linked, unknown = match_private([first, second], [dm(1, '$1000', seconds=10),
        dm(2, 'Watch $1030', chat='11', seconds=65, model='watch'), dm(3, '$1010', chat='12', seconds=70)])
    assert len(unknown) == 1 and unknown[0]['reason'] == 'ambiguous'


def test_different_currencies_do_not_infer_and_relative_stays_literal():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    linked, unknown = match_private([first, second], [dm(1, '$1000', seconds=10),
        dm(2, '990 сум', seconds=65), dm(3, '-5', seconds=70)])
    assert len(unknown) == 2
    assert unknown[1]['minor'] is None and unknown[1]['relative_minor'] == -500
    assert prices('-5$')[0]['minor'] is None


def test_forward_then_price_wins_over_price_similarity_across_groups():
    first, second = q(), {**q(1, 'Watch kere', seconds=60), 'group_id': '-1002'}
    forward = {**dm(2, second['text_excerpt'], seconds=62), 'forwarded': True,
               'forward_group_id': '-1002', 'forward_message_id': 1}
    linked, unknown = match_private([first, second], [dm(1, '$1000', seconds=10), forward, dm(3, '$990', seconds=65)])
    assert not unknown and linked[key(second)][0]['method'] == 'forward_context'
    assert len(linked[key(first)]) == 1


def test_forward_by_text_and_origin_date_not_saved_messages_metadata():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    forward = {**dm(2, first['text_excerpt'], seconds=62), 'forwarded': True,
               'forward_from_id': OWN, 'forward_date': first['telegram_date']}
    linked, _ = match_private([first, second], [forward, dm(3, '$990', seconds=65)])
    assert linked[key(first)][0]['method'] == 'forward_context'


def test_forward_context_does_not_capture_all_following_prices():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    forward = {**dm(2, first['text_excerpt'], seconds=10), 'forwarded': True}
    linked, unknown = match_private([first, second], [forward, dm(3, '$1000', seconds=20),
        dm(4, '$120', seconds=70)])
    assert linked[key(first)][0]['method'] == 'forward_context'
    assert len(linked[key(first)]) == 1
    assert linked[key(second)][0]['method'] == 'price_far'


def test_context_scoped_to_chat_and_reply_to_unknown_not_reassigned():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    forward = {**dm(2, first['text_excerpt'], seconds=62), 'forwarded': True}
    linked, unknown = match_private([first, second], [forward, dm(3, '$990', chat='12', seconds=65),
        dm(4, '$900', seconds=66, reply=999)])
    assert len(unknown) == 2
    assert unknown[1]['reason'] == 'reply_not_found'
    assert not linked[key(first)]


def test_manual_choice_persists_but_edit_invalidates():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    message = dm(3, '$100', seconds=70)
    link = dict(chat_id='10', message_id=3, request_key=key(second),
                message_fingerprint=fingerprint(message), request_fingerprint=fingerprint(second))
    linked, unknown = match_private([first, second], [message], [link])
    assert linked[key(second)][0]['method'] == 'manual' and not unknown
    linked, unknown = match_private([first, second], [{**message, 'text_excerpt': '$200'}], [link])
    assert unknown[0]['reason'] == 'manual_stale'


def test_boundaries_outgoing_and_late_edit():
    messages = [dm(1, '$100', seconds=-1), dm(2, '$110', seconds=420), dm(3, '$120', seconds=421),
                {**dm(4, '$90', seconds=100), 'outgoing': True},
                {**dm(5, '$80', seconds=101), 'edited_at': (NOW+timedelta(seconds=500)).isoformat()}]
    linked, _ = match_private([q()], messages)
    offers = linked[key(q())]
    assert {o['message_id'] for o in offers} == {2, 5}
    assert next(o for o in offers if o['message_id'] == 5)['reason'] == 'late'


def test_ranking_includes_rule_currency_but_excludes_inferred_and_relative():
    first, second = q(), q(2, 'Watch kere', seconds=60)
    linked, _ = match_private([first, second], [dm(1, '$1000', seconds=10),
        dm(2, '$1050', seconds=20, chat='11'), dm(3, '-5', seconds=30),
        dm(4, '$990', seconds=65), dm(5, '800', seconds=40, chat='12')])
    offers = linked[key(first)]
    rank_offers(offers)
    assert [o['highlight'] for o in offers] == ['', 'max', '', 'min', '']
    assert not offers[0]['superseded']


def test_rule_currency_can_match_explicit_dollar_anchor():
    first, second=q(),q(2,'Watch kere',seconds=60)
    linked,unknown=match_private([first,second],[dm(1,'$1000',seconds=10),dm(2,'990',seconds=70)])
    assert not unknown
    assert linked[key(first)][1]['method']=='price_near'
    assert linked[key(first)][1]['currency']=='USD'
    assert linked[key(first)][1]['currency_source']=='amount_rule'
    assert not linked[key(first)][1]['comparable']  # Target still only a hypothesis.


def test_rule_currency_does_not_cross_dollar_sum_boundary():
    first,second=q(),q(2,'Watch kere',seconds=60)
    linked,unknown=match_private([first,second],[dm(1,'5000',seconds=10),dm(2,'5001',seconds=70)])
    assert linked[key(first)][0]['currency']=='USD'
    assert unknown[0]['currency']=='UZS'
    assert unknown[0]['reason']=='ambiguous'


def test_duplicate_requests_are_one_product_for_bare_prices():
    first,repeat=q(),q(2,seconds=15)
    linked,unknown=match_private([first,repeat],[dm(1,'1470',seconds=20),dm(2,'1475',seconds=30,chat='11')])
    assert not unknown and not linked[key(first)]
    assert [o['minor'] for o in linked[key(repeat)]]==[147000,147500]
    assert all(o['method']=='private_time' for o in linked[key(repeat)])


def test_real_watch_then_duplicate_iphone_prices_remain_hypotheses():
    watch={**q(1,'Amazfit balance 3 kere'),'group_id':'-1002'}
    first=q(2,'17 max 256 silver sim esim kere',seconds=333)
    repeat=q(3,first['text_excerpt'],seconds=364)
    linked,unknown=match_private([watch,first,repeat],[dm(1,'330',seconds=9),
        dm(2,'1470',seconds=393,chat='11'),dm(3,'1475',seconds=411,chat='12')])
    assert not unknown
    assert [o['minor'] for o in linked[key(repeat)]]==[147000,147500]
    assert all(o['method']=='price_far' and o['inferred'] and not o['comparable'] for o in linked[key(repeat)])
    assert len(linked[key(watch)])==1


def test_older_duplicate_price_anchor_is_available_without_inference_cascade():
    first,repeat,watch=q(),q(2,seconds=15),q(3,'Watch kere',seconds=60)
    linked,unknown=match_private([first,repeat,watch],[dm(1,'1000',seconds=10),dm(2,'990',seconds=65,chat='11'),dm(3,'120',seconds=70,chat='12')])
    assert not unknown
    assert linked[key(repeat)][0]['method']=='price_near'
    assert linked[key(watch)][0]['method']=='price_far' and not linked[key(watch)][0]['comparable']


def test_reposting_an_old_product_does_not_make_it_new_for_price_far():
    first=q(model='iphone')
    watch=q(2,'Watch kere',seconds=60,model='watch')
    repeat=q(3,seconds=80,model='iphone')
    linked,unknown=match_private([first,watch,repeat],[dm(1,'Watch 300$',seconds=70,model='watch'),dm(2,'1500',seconds=90)])
    assert len(unknown)==1 and unknown[0]['reason']=='ambiguous'
    assert not linked[key(repeat)]


@pytest.mark.parametrize('text,group', [('iPhone 256 black kere','-1001'),('iPhone 512 silver kere','-1001'),('iPhone 256 silver esim kere','-1001'),('iPhone 256 silver kere','-1002')])
def test_different_variants_or_groups_are_not_duplicates(text,group):
    first=q(text='iPhone 256 silver kere')
    second={**q(2,text,seconds=15),'group_id':group}
    linked,unknown=match_private([first,second],[dm(1,'1470',seconds=20)])
    assert len(unknown)==1 and not any(linked.values())


def test_exact_forward_keeps_original_but_hidden_origin_accepts_reposts():
    first,repeat=q(),q(2,seconds=15)
    exact={**dm(1,first['text_excerpt'],seconds=20),'forwarded':True,'forward_group_id':'-1001','forward_message_id':1}
    hidden={**dm(3,first['text_excerpt'],seconds=22,chat='11'),'forwarded':True}
    linked,unknown=match_private([first,repeat],[exact,dm(2,'1000',seconds=21),hidden,dm(4,'1100',seconds=23,chat='11')])
    assert not unknown and linked[key(first)][0]['method']=='forward_context'
    assert linked[key(repeat)][0]['method']=='forward_context'


def test_model_and_outgoing_context_with_reposts():
    first,repeat=q(model='iphone'),q(2,seconds=15,model='iphone')
    linked,unknown=match_private([first,repeat],[dm(1,'iPhone 1000$',seconds=20,model='iphone'),
        {**dm(2,first['text_excerpt'],seconds=30,chat='11',model='iphone'),'outgoing':True},dm(3,'990',seconds=40,chat='11')])
    assert not unknown
    assert [o['method'] for o in linked[key(repeat)]]==['private_model','forward_context']


def test_expired_original_reply_is_not_reassigned_to_repost():
    first,repeat=q(),q(2,seconds=400)
    linked,unknown=match_private([first,repeat],[dm(1,'1000',seconds=10),dm(2,'990',seconds=430,reply=1)])
    assert not linked[key(repeat)]
    assert unknown[0]['reason']=='reply_not_found'


def test_collector_only_known_suppliers_cursor_restart_and_deleted(tmp_path):
    from telegram_market_stats.private_collector import PrivateQuotesCollector
    from telegram_market_stats.private_store import load_messages
    from telegram_market_stats.repository import MarketStatsRepository
    from test_telegram_market_stats import analyzer
    repo = MarketStatsRepository(tmp_path/'market.db')
    repo.configure(-1001, 'Market', {int(OWN): 'TEXNIKACH'}, NOW)
    model_analyzer = analyzer()
    repo.upsert_message(group_id=-1001, message_id=1, sender_id=int(OWN), telegram_date=NOW,
        edited_at=None, text='iPhone 16 Pro Max kere', analysis=model_analyzer.analyze('iPhone 16 Pro Max kere'),
        competitor_ids={int(OWN)}, processed_at=NOW)
    # A supplier who has posted in the configured group.
    repo.upsert_message(group_id=-1001, message_id=2, sender_id=10, telegram_date=NOW,
        edited_at=None, text='bor', analysis=model_analyzer.analyze('bor'), competitor_ids=set(), processed_at=NOW)
    message = NS(id=12, date=NOW+timedelta(seconds=20), message='$1000', out=False)
    class Client:
        calls = []
        gone = False
        async def iter_dialogs(self, **kw):
            for id in (999, 10):
                yield NS(id=id, input_entity=id, is_user=True, message=message,
                         entity=NS(first_name='Supplier', last_name='', username=None, bot=False))
        async def get_messages(self, entity, **kw):
            self.calls.append((entity, kw))
            assert entity == 10
            return [] if self.gone else [message]
    client = Client()
    collector = PrivateQuotesCollector(repo, model_analyzer, tmp_path/'no-business.db', clock=lambda: NOW+timedelta(minutes=15))
    asyncio.run(collector.collect_once(client))
    asyncio.run(collector.collect_once(client))
    with repo.connect() as db:
        assert db.execute('SELECT count(*) FROM market_private_messages').fetchone()[0] == 1
        assert db.execute('SELECT last_message_id,backfill_complete FROM market_private_cursors').fetchone()[:] == (12, 1)
    client.gone = True
    restarted = PrivateQuotesCollector(repo, model_analyzer, tmp_path/'no-business.db', clock=collector.clock)
    asyncio.run(restarted.collect_once(client))
    with repo.connect() as db:
        assert not load_messages(db, NOW.isoformat(), (NOW+timedelta(hours=1)).isoformat())


def test_no_read_or_send_methods_in_private_collector():
    import inspect
    from telegram_market_stats import private_collector
    source = inspect.getsource(private_collector)
    for method in ('send_read_acknowledge(', 'ReadHistoryRequest(', 'readBusinessMessage(', 'send_message('):
        assert method not in source
