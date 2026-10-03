from datetime import datetime, timedelta, timezone
import pytest

from telegram_market_stats.quotes import build_quotes, prices

NOW=datetime(2026,10,3,5,tzinfo=timezone.utc)
OWN='5619452809'


def row(id, text, *, seconds=0, sender='10', reply=None, model='', memory='', color='', intent='unknown', loaded=1, **extra):
    return dict(message_id=id,text_excerpt=text,sender_id=sender,sender_name=f'Supplier {sender}',
                telegram_date=(NOW+timedelta(seconds=seconds)).isoformat(),edited_at=None,
                reply_to_message_id=reply,reply_external=0,reply_metadata_loaded=loaded,intent=intent,
                mentions=[dict(model_key=model,memory=memory,color=color)] if model else [],**extra)


def query(id=1,text='iPhone 16 Pro Max 256 black kere',**kw):
    return row(id,text,sender=OWN,intent='demand',**kw)


def build(rows, ids=(1,)):
    return build_quotes(rows,{OWN},set(ids),NOW+timedelta(minutes=10))


@pytest.mark.parametrize('text,currency,minor',[
    ('$1,250','USD',125000),('1 250 $','USD',125000),('1250 usd','USD',125000),
    ('1 250 000 so‘m','UZS',125000000),('150 ming som','UZS',15000000),
    ('1.5 mln сум','UZS',150000000),('$9.50','USD',950),('1.250$','USD',125000),
    ('1250','UNKNOWN',125000),('120 ming','UNKNOWN',12000000),
    ('1250 B21','UNKNOWN',125000),('Bor 1250','UNKNOWN',125000),
])
def test_price_formats(text,currency,minor):
    assert prices(text)[0]['minor']==minor
    assert prices(text)[0]['currency']==currency


@pytest.mark.parametrize('text',['B21','A8','+998901234567','901234567','12:50','17 pro max','256 GB','46mm','12/46','12.10.2026','AirPods pro 3','скидка 10%'])
def test_not_prices(text):
    assert prices(text)==[]


def test_reply_correct_when_many_models_requested_concurrently():
    q1=query(1);q2=query(2,'Samsung S26 Ultra kere',seconds=20)
    requests,other=build([q1,q2,row(3,'$1200',seconds=30,reply=1),row(4,'$1100',seconds=40,reply=2)],(1,2))
    by_id={q['message_id']:q for q in requests}
    assert by_id[1]['text']==q1['text_excerpt']
    assert by_id[1]['offers'][0]['minor']==120000
    assert by_id[2]['offers'][0]['minor']==110000
    assert not other


def test_min_max_separate_currency_unknown_and_equal_prices_neutral():
    req,_=build([query(),row(2,'100$',seconds=1,reply=1),row(3,'200$',sender='11',seconds=2,reply=1),
                 row(4,'90',sender='12',seconds=3,reply=1),row(5,'100000 so’m',sender='13',seconds=4,reply=1),
                 row(6,'100$',sender='14',seconds=5,reply=1)])
    offers=req[0]['offers']
    assert [o['highlight'] for o in offers]==['min','max','','','min']
    req,_=build([query(),row(2,'100$',seconds=1,reply=1),row(3,'100$',sender='11',seconds=2,reply=1)])
    assert all(o['highlight']=='' for o in req[0]['offers'])


def test_bare_price_ambiguous_even_when_other_request_is_competitor():
    req,other=build([query(),row(2,'Samsung kere',sender='99',intent='demand',seconds=10),row(3,'1200',seconds=11)])
    assert req[0]['offers']==[]
    assert other[0]['reason']=='ambiguous'


def test_one_request_numeric_price_is_linked_but_unknown_currency_not_colored():
    req,other=build([query(),row(2,'1200',seconds=11)])
    assert not other and req[0]['offers'][0]['method']=='single_request'
    assert not req[0]['offers'][0]['comparable']


def test_exact_model_without_reply_and_repeated_identical_requests():
    rows=[query(model='iphone'),query(2,'Samsung kere',seconds=10,model='samsung'),
          row(3,'iPhone $900',seconds=20,model='iphone')]
    req,_=build(rows,(1,2));assert req[0]['offers'][0]['method']=='model'
    rows.append(query(4,seconds=21,model='iphone'))
    rows.append(row(5,'iPhone $800',seconds=22,model='iphone'))
    _,other=build(rows,(1,2,4));assert other[0]['reason']=='ambiguous'


def test_reply_chain_and_later_correction_keeps_history():
    req,_=build([query(),row(2,'$1200',seconds=10,reply=1),row(3,'$1150',seconds=20,reply=2),row(4,'$1100',seconds=30,reply=1,sender='11')])
    offers=req[0]['offers']
    assert offers[0]['superseded'] and not offers[0]['highlight']
    assert offers[1]['method']=='reply_chain' and offers[1]['highlight']=='max'
    assert offers[2]['highlight']=='min'


def test_deadline_inclusive_and_edit_after_deadline_excluded():
    edited=row(4,'50$',seconds=50,reply=1);edited['edited_at']=(NOW+timedelta(seconds=600)).isoformat()
    req,_=build([query(),row(2,'100$',seconds=420,reply=1),row(3,'90$',seconds=421,reply=1),edited])
    offers={o['message_id']:o for o in req[0]['offers']}
    assert offers[2]['reason']==''
    assert offers[3]['reason']==offers[4]['reason']=='late'
    assert all(not o['highlight'] for o in offers.values())


def test_unknown_reply_not_inferred_to_other_model():
    req,other=build([query(),row(2,'900$',seconds=10,reply=999)])
    assert not req[0]['offers'] and other[0]['reason']=='reply_not_found'


def test_historical_missing_links_and_unknown_models():
    req,other=build([query(text='Porodo Kids Watch Kere'),row(2,'40$',seconds=10,loaded=0)])
    assert not req[0]['offers'] and other[0]['reason']=='metadata_missing'
    req,other=build([query(text='Porodo Kids Watch Kere'),row(2,'40$',seconds=10,reply=1)])
    assert len(req[0]['offers'])==1 and not other


def test_different_variants_and_multiple_prices_not_compared():
    req,_=build([query(model='iphone',memory='256 GB'),row(2,'512GB $500',reply=1,model='iphone',memory='512 GB',seconds=10),
                 row(3,'$400 / $450',reply=1,seconds=20,sender='11')])
    assert req[0]['offers'][0]['reason']=='variant_mismatch'
    assert all(not o['comparable'] for o in req[0]['offers'])


def test_non_price_reply_retains_supplier_text_without_inventing_amount():
    req,_=build([query(),row(2,'Bor B21',seconds=11,reply=1)])
    assert req[0]['offers'][0]['minor'] is None
    assert req[0]['offers'][0]['text']=='Bor B21'


def test_external_reply_cannot_cross_group():
    external=row(2,'900$',reply=1,seconds=11);external['reply_external']=1
    req,other=build([query(),external])
    assert not req[0]['offers'] and other[0]['reason']=='reply_not_found'


def test_explicit_reply_with_wrong_model_or_case_is_not_comparable():
    req,_=build([query(model='iphone'),row(2,'Samsung $300',model='samsung',reply=1,seconds=10),
                 row(3,'iPhone case $10',model='iphone',reply=1,seconds=20,sender='11')])
    assert all(o['reason']=='variant_mismatch' for o in req[0]['offers'])


def test_unknown_multiline_request_keeps_prices_without_false_comparison():
    req,_=build([query(text='New watch kere\nOther tablet kere'),row(2,'$10',reply=1,seconds=10),row(3,'$20',reply=1,seconds=20,sender='11')])
    assert all(not o['comparable'] for o in req[0]['offers'])


def test_two_variant_prices_from_one_supplier_are_not_a_correction():
    req,_=build([query(),row(2,'$400 / $450',reply=1,seconds=20)])
    assert len(req[0]['offers'])==2
    assert all(not o['superseded'] for o in req[0]['offers'])
