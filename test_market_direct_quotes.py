"""Direct supplier requests must never become cross-chat customer evidence."""
from datetime import timedelta

import pytest

from test_market_quotes import NOW, query, row
from telegram_market_stats.private_quotes import fingerprint, key, match_private, private_prices


def direct(mid=1, text='New Gadget Air kere', *, chat='10', **kwargs):
    return query(mid, text, group_id=f'private:{chat}', source='private', chat_id=chat, **kwargs)


def dm(mid, text, *, chat='10', outgoing=False, **kwargs):
    return row(mid, text, chat_id=chat, outgoing=outgoing, forwarded=False, **kwargs)


def outgoing(request):
    return {**request, 'outgoing': True, 'forwarded': False}


def test_direct_request_without_group_and_without_known_model():
    request = direct()
    linked, unknown = match_private([request], [outgoing(request), dm(2, '330', seconds=12)])
    offer, = linked[key(request)]
    assert not unknown
    assert offer['method'] == 'private_context'
    assert offer['minor'] == 33000 and offer['currency'] == 'USD'
    assert offer['comparable'] and offer['seconds_after'] == 12


def test_direct_request_is_only_visible_to_its_supplier():
    request = direct()
    linked, unknown = match_private([request], [outgoing(request), dm(2, '330', chat='11', seconds=12)])
    assert not any(linked.values()) and not unknown


def test_candidate_metadata_identifies_direct_supplier_and_group_origin():
    request = {**direct(), 'sender_name': 'Supplier One', 'sender_username': 'supplier_one'}
    group = query(50, 'Group Device kere', group_id='-1001')
    linked, unknown = match_private([request, group], [dm(2, '330', seconds=12, reply=1)])
    assert not unknown
    options = {option['request_key']: option for option in linked[key(request)][0]['candidates']}
    assert options[key(request)]['source'] == 'private'
    assert options[key(request)]['chat_id'] == '10'
    assert options[key(request)]['supplier_name'] == 'Supplier One'
    assert options[key(request)]['supplier_username'] == 'supplier_one'
    assert options[key(group)]['source'] == 'group'
    assert 'chat_id' not in options[key(group)]
    assert 'supplier_name' not in options[key(group)]


def test_direct_candidate_name_fallback_is_safe_for_missing_supplier_metadata():
    request = {**direct(), 'sender_name': None, 'sender_username': None}
    linked, _ = match_private([request], [dm(2, '330', seconds=12)])
    option, = linked[key(request)][0]['candidates']
    assert option['supplier_name'] == 'ID 10'
    assert option['supplier_username'] == ''


def test_multiple_suppliers_have_independent_context_and_keys():
    first, second = direct(), direct(chat='11', seconds=1)
    linked, unknown = match_private([first, second], [outgoing(first), outgoing(second),
        dm(2, '330', seconds=10), dm(2, '320', chat='11', seconds=12)])
    assert not unknown
    assert linked[key(first)][0]['minor'] == 33000
    assert linked[key(second)][0]['minor'] == 32000
    assert [o['request_key'] for o in linked[key(first)][0]['candidates']] == [key(first)]
    assert [o['request_key'] for o in linked[key(second)][0]['candidates']] == [key(second)]


def test_latest_direct_question_anchors_next_quote_but_explicit_reply_wins():
    first, second = direct(), direct(2, 'Other Device kere', seconds=60)
    linked, unknown = match_private([first, second], [outgoing(first), outgoing(second),
        dm(3, '330', seconds=65), dm(4, '120', seconds=70, reply=1)])
    assert not unknown
    assert linked[key(second)][0]['method'] == 'private_context'
    assert linked[key(first)][0]['method'] == 'private_reply'


def test_direct_reply_resolves_without_outgoing_message_in_loaded_batch():
    first, second = direct(), direct(2, 'Other Device kere', seconds=60)
    linked, unknown = match_private([first, second], [dm(3, '330', seconds=65, reply=1)])
    assert not unknown
    assert linked[key(first)][0]['method'] == 'private_reply'
    assert not linked[key(second)]


def test_reply_id_collision_in_another_supplier_chat_does_not_resolve():
    request = direct()
    group = query(50, 'Group Device kere', group_id='-1001')
    linked, unknown = match_private([request, group], [dm(3, '330', chat='11', seconds=65, reply=1)])
    assert not any(linked.values())
    assert unknown[0]['reason'] == 'reply_not_found'
    assert [q['request_key'] for q in unknown[0]['candidates']] == [key(group)]


def test_context_is_consumed_and_does_not_capture_unrelated_later_price():
    first, second = direct(), direct(2, 'Other Device kere', seconds=60)
    linked, unknown = match_private([first, second], [outgoing(first), outgoing(second),
        dm(3, '330', seconds=65), dm(4, '500', seconds=70)])
    assert linked[key(second)][0]['method'] == 'private_context'
    assert len(linked[key(second)]) == 1
    # Another quote far from the second price is not enough to attach it to an
    # older first request. The original price-far time guard remains intact.
    assert unknown[0]['reason'] == 'ambiguous'


def test_direct_context_wins_over_identical_group_query():
    request = direct(text='iPhone 17 Pro kere', model='iphone17pro')
    group = query(50, request['text_excerpt'], group_id='-1001', model='iphone17pro')
    linked, unknown = match_private([group, request], [outgoing(request),
        dm(2, 'iPhone 17 Pro $1200', model='iphone17pro', seconds=10)])
    offer, = linked[key(request)]
    assert not unknown and not linked[key(group)]
    assert offer['method'] == 'private_context'
    assert offer['reason'] == '' and offer['comparable']


def test_cold_direct_reply_with_identical_group_query_is_not_variant_mismatch():
    request = direct(text='iPhone 17 Pro kere', model='iphone17pro')
    group = query(50, request['text_excerpt'], group_id='-1001', model='iphone17pro')
    linked, unknown = match_private([group, request], [
        dm(2, 'iPhone 17 Pro $1200', model='iphone17pro', seconds=10, reply=1)])
    offer, = linked[key(request)]
    assert not unknown and offer['method'] == 'private_reply'
    assert offer['reason'] == '' and offer['comparable']


def test_forwarded_group_request_overrides_direct_context():
    request = direct()
    group = query(50, 'Group Device kere', group_id='-1001')
    forwarded = {**dm(2, group['text_excerpt'], seconds=10), 'forwarded': True,
                 'forward_group_id': '-1001', 'forward_message_id': 50}
    linked, unknown = match_private([request, group], [outgoing(request), forwarded, dm(3, '330', seconds=12)])
    assert not unknown and not linked[key(request)]
    assert linked[key(group)][0]['method'] == 'forward_context'


def test_direct_expiry_inclusive_and_stale_reply_not_reassigned():
    request = direct()
    later = direct(2, 'Other Device kere', seconds=400)
    linked, unknown = match_private([request, later], [dm(3, '330', seconds=420, reply=1),
        dm(4, '340', seconds=421, reply=1)])
    assert len(linked[key(request)]) == 1 and not linked[key(later)]
    assert unknown[0]['reason'] == 'reply_not_found'


def test_manual_link_to_other_supplier_request_is_stale_not_permitted():
    request, foreign = direct(), direct(chat='11')
    message = dm(2, '330', seconds=10)
    link = dict(chat_id='10', message_id=2, request_key=key(foreign),
                message_fingerprint=fingerprint(message), request_fingerprint=fingerprint(foreign))
    linked, unknown = match_private([request, foreign], [message], [link])
    assert not any(linked.values())
    assert unknown[0]['reason'] == 'manual_stale'
    assert [q['request_key'] for q in unknown[0]['candidates']] == [key(request)]


def test_foreign_direct_price_is_never_an_anchor_for_another_supplier():
    request, foreign = direct(), direct(chat='11')
    later = query(50, 'Group Device kere', group_id='-1001', seconds=60)
    linked, unknown = match_private([request, foreign, later], [dm(2, '330', chat='11', seconds=10),
        dm(3, '335', seconds=70)])
    assert linked[key(foreign)][0]['minor'] == 33000
    assert not linked[key(request)] and not linked[key(later)]
    assert unknown[0]['reason'] == 'ambiguous'


def test_edited_direct_request_cannot_confirm_earlier_quote():
    request = {**direct(), 'edited_at': (NOW + timedelta(seconds=100)).isoformat()}
    linked, unknown = match_private([request], [dm(2, '330', seconds=20, reply=1)])
    assert not unknown
    assert linked[key(request)][0]['reason'] == 'request_edited'
    assert not linked[key(request)][0]['comparable']


def test_deleted_request_absent_from_input_cannot_resolve_direct_reply():
    other = direct(2, 'Other Device kere')
    linked, unknown = match_private([other], [dm(3, '330', seconds=20, reply=1)])
    assert not any(linked.values()) and unknown[0]['reason'] == 'reply_not_found'


def test_outgoing_direct_question_with_price_is_never_a_quote():
    # An explicit demand can include a target budget. Upstream decides whether
    # it is a request; the matcher must not turn our own number into an offer.
    request = direct(text='New Gadget Air kere $300')
    linked, unknown = match_private([request], [outgoing(request)])
    assert not any(linked.values()) and not unknown


@pytest.mark.parametrize('text,amount,currency', [
    ('Nova Q99 330', 33000, 'USD'), ('NOVA Q99 bor 330', 33000, 'USD'),
    ('Nova Q99 330.50', 33050, 'USD'), ('Nova Q99 6000', 600000, 'UZS'),
])
def test_unknown_direct_model_with_final_price(text, amount, currency):
    request = direct(text='Nova Q99 kere')
    message = dm(2, text, seconds=20)
    values = private_prices(message, [request])
    assert len(values) == 1 and values[0]['minor'] == amount and values[0]['currency'] == currency
    linked, unknown = match_private([request], [message])
    assert not unknown and linked[key(request)][0]['minor'] == amount


@pytest.mark.parametrize('text', [
    'Nova Q100 330', 'Nova Q99 black 330', 'Nova Q99 256', 'Nova Q99 512',
    'Nova Q99 +998901333999', 'Nova Q99 +998 90 133 39 99',
    'Nova Q99 14:30', 'Nova Q99 46mm', 'Nova Q99 46 мм', 'Nova Q99 330 dona',
])
def test_unknown_direct_price_does_not_eat_variants_capacity_phone_time_or_units(text):
    request = direct(text='Nova Q99 kere')
    assert not private_prices(dm(2, text, seconds=20), [request])


def test_unknown_watch_size_without_unit_is_not_fallback_price():
    request = direct(text='Nova Watch Q99 kere')
    assert not private_prices(dm(2, 'Nova Watch Q99 46', seconds=20), [request])
    assert private_prices(dm(2, 'Nova Watch Q99 46$', seconds=20), [request])[0]['minor'] == 4600


@pytest.mark.parametrize('seconds,chat,group', [(-1, '10', False), (421, '10', False),
    (20, '11', False), (20, '10', True)])
def test_raw_direct_price_does_not_expand_time_chat_or_group_scope(seconds, chat, group):
    request = direct(text='Nova Q99 kere')
    if group:
        request = {**request, 'group_id': '-1001', 'source': 'group'}
    assert not private_prices(dm(2, 'Nova Q99 330', seconds=seconds, chat=chat), [request])


def test_raw_single_token_model_outweighs_another_direct_context():
    first, second = direct(text='X99 kere'), direct(2, text='X100 kere', seconds=10)
    linked, unknown = match_private([first, second], [outgoing(first), outgoing(second),
        dm(3, 'X99 330', seconds=20)])
    assert not unknown and not linked[key(second)]
    offer, = linked[key(first)]
    assert offer['minor'] == 33000 and offer['method'] == 'private_model'


def test_raw_exact_variant_must_match_request_variant():
    request = direct(text='Nova Q99 black kere')
    assert private_prices(dm(2, 'Nova Q99 black 330', seconds=20), [request])
    assert not private_prices(dm(2, 'Nova Q99 silver 330', seconds=20), [request])
