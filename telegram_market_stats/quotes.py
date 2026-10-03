"""Deterministic supplier offers. No network, AI, FX conversion or fuzzy joins.

Shared with the read-only Finance dashboard as finance/market_quotes.py.
Input rows are from ONE group and contain original text plus Telegram reply links.
"""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

WINDOW_SECONDS = 420
# Owner-approved market convention, applied ONLY when currency is omitted.
# Amounts are stored in hundredths: exactly 5000 is USD, strictly above is UZS.
BARE_USD_LIMIT_MINOR = 5000 * 100
NUMBER = r'\d{1,3}(?:[ \u00a0.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?'
CURRENCY = r"\$|usd|доллар(?:ов|а)?|долл?\.?|у\.?\s?е\.?|so['‘’`ʻʼ]?m|с[уў]м|uzs"
PRICE_RE = re.compile(rf'(?<![\w+])(?:(?P<before>\$|usd)\s*(?P<first>{NUMBER})|(?P<amount>{NUMBER})\s*(?P<scale>млн|million|mln|тыс\.?|ming|k)?\s*(?P<currency>{CURRENCY}))(?!\w)', re.I)
BARE_RE = re.compile(rf'^\s*(?P<amount>{NUMBER})\s*(?P<scale>млн|mln|тыс\.?|ming|k)?\s*[.!]*\s*$', re.I)
STOP = set('kere kerak kerek bormi bor mi у кого есть нужен нужна нужно ищу ищем куплю надо kimda narxi цена narx naxtga naxtka на наличные сотилади продам sotaman mavjud'.split())
CHATTER = re.compile(r'^(?:спасибо|рахмат|rahmat|salom|assalom\w*|здравствуйте|ok|ок|да|нет|ha|yo.?q|bor|b\d+|a\d+)[\s!?.,]*$', re.I)
CAPACITIES = {'32','64','128','256','512','1024','2048'}


def timestamp(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _minor(raw, scale=''):
    text = raw.replace(' ', '').replace('\u00a0', '')
    if re.search(r'[.,]\d{1,2}$', text):
        split = max(text.rfind(','), text.rfind('.'))
        text = re.sub(r'[.,]', '', text[:split]) + '.' + text[split+1:]
    else:
        text = re.sub(r'[.,]', '', text)
    multiplier = 1_000_000 if scale.casefold() in {'млн','million','mln'} else 1000 if scale else 1
    try:
        value = Decimal(text) * 100 * multiplier
        return int(value) if 0 < value <= 100_000_000_000 else None
    except InvalidOperation:
        return None


def prices(text, *, has_model=False):
    """Never treat capacity, generation, phone, shop number or time as a price."""
    relative = re.fullmatch(rf'\s*[-−–]\s*(?P<n>{NUMBER})\s*(?P<c>{CURRENCY})?\s*', text, re.I)
    if relative:
        amount = _minor(relative['n'])
        currency = relative['c'] or '$'
        return ([{'minor': None, 'relative_minor': -amount,
                  'currency': 'UZS' if re.search(r'so|с[уў]м|uzs', currency, re.I) else 'USD',
                  'raw': text.strip()}] if amount else [])
    found = []
    for match in PRICE_RE.finditer(text):
        raw = match.group('first') or match.group('amount')
        value = _minor(raw, match.group('scale') or '')
        currency = (match.group('before') or match.group('currency')).casefold()
        if value is not None:
            unit = 'USD' if re.search(r'\$|usd|дол|^у', currency) else 'UZS'
            relative = re.search(r'[-−–]\s*$', text[:match.start()])
            if relative:
                found.append({'minor': None, 'relative_minor': -value, 'currency': unit, 'raw': '-'+match.group().strip()})
            else:
                found.append({'minor': value, 'currency': unit, 'raw': match.group().strip()})
    if found:
        return found
    match = BARE_RE.fullmatch(text)
    if match is None and not has_model:
        clean = re.sub(r'\b(?:[abаб]\d{1,3}|bor|бор|есть|naxt)\b', ' ', text, flags=re.I)
        match = BARE_RE.fullmatch(clean)
    if match is None:
        # With a known model, a final standalone price is acceptable only when
        # it cannot be a standard memory capacity; then apply the market rule.
        match = re.search(rf'(?:цена|narx|narxi)\s*[:=-]?\s*(?P<amount>{NUMBER})\s*(?P<scale>ming|тыс|mln|k)?\s*$', text, re.I)
        if match is None and has_model:
            match = re.search(r'\s(?P<amount>\d{2,7}(?:[.,]\d{1,2})?)\s*$', text)
            if match and match.group('amount') in CAPACITIES:
                match = None
    if match:
        raw = match.group('amount')
        # Bare 9+ digit numbers may be phone numbers. Explicit sum prices above
        # are handled separately and are not truncated by this check.
        if len(re.sub(r'\D', '', raw)) < 9:
            value = _minor(raw, match.groupdict().get('scale') or '')
            if value is not None:
                return [{'minor': value,
                         'currency': 'UZS' if value > BARE_USD_LIMIT_MINOR else 'USD',
                         'currency_source': 'amount_rule', 'raw': match.group().strip()}]
    return []


def tokens(text):
    value = text.casefold().replace('ё','е')
    value = PRICE_RE.sub(' ', value)
    return set(re.findall(r'[a-zа-яўқғҳ0-9]+', value)) - STOP


def qualifiers(row):
    text = row.get('text_excerpt') or ''
    words = tokens(text)
    memory = {m.get('memory','').casefold().replace(' ', '') for m in row.get('mentions',[]) if m.get('memory')}
    color = {m.get('color','').casefold() for m in row.get('mentions',[]) if m.get('color')}
    category = next((label for label, pattern in (
        ('case', r'\b(?:case|chexol|chehol|чехол|кейс)\b'),
        ('glass', r'\b(?:стекло|oyna|glass)\b'),
        ('strap', r'\b(?:ремешок|strap)\b'),
        ('cable', r'\b(?:cable|кабель)\b'),
        ('keyboard', r'\b(?:keyboard|клавиатура)\b'),
    ) if re.search(pattern,text,re.I)), '')
    condition = tuple(sorted(words & {'used','бу','б/у','ref','refurbished','витрина','china','global','mdm','неактив','актив'}))
    sim = tuple(sorted(words & {'esim','dual','sim','wifi'}))
    return memory, color, category, condition, sim


def compatible(query, quote):
    qm, qc, qcat, qcond, qsim = qualifiers(query)
    om, oc, ocat, ocond, osim = qualifiers(quote)
    qkeys={m['model_key'] for m in query.get('mentions',[])}
    okeys={m['model_key'] for m in quote.get('mentions',[])}
    if qkeys and okeys and (qkeys != okeys or qcat != ocat):
        return False
    return not ((qm and om and qm != om) or (qc and oc and qc != oc)
                or (qcat and ocat and qcat != ocat)
                or (qcond and ocond and qcond != ocond)
                or (qsim and osim and qsim != osim))


def same_model(query, quote):
    qkeys = {m['model_key'] for m in query.get('mentions',[])}
    okeys = {m['model_key'] for m in quote.get('mentions',[])}
    if qkeys and okeys:
        if qkeys != okeys:
            return False
        # A case and a phone can mention exactly the same phone model.
        if qualifiers(query)[2] != qualifiers(quote)[2]:
            return False
        return compatible(query, quote)
    qtokens, otokens = tokens(query.get('text_excerpt','')), tokens(quote.get('text_excerpt',''))
    return len(qtokens) >= 2 and qtokens.issubset(otokens) and compatible(query, quote)


def is_request(row, own_ids):
    text = (row.get('text_excerpt') or '').strip()
    if not text or CHATTER.fullmatch(text):
        return False
    # Demand markers win over incidental numbers ("18 pro 256 kere").
    if row.get('intent') == 'demand':
        return True
    if row.get('reply_to_message_id') or row.get('intent') == 'supply' or prices(text, has_model=bool(row.get('mentions'))):
        return False
    return bool(row.get('mentions')) or (str(row.get('sender_id')) in own_ids and len(tokens(text)) >= 2)


def build_quotes(rows, own_ids, selected_ids, now):
    """Return request rows and unresolved replies, with transparent match reasons."""
    own_ids = set(map(str,own_ids)); selected_ids = set(selected_ids)
    rows = sorted(rows, key=lambda r:(r['telegram_date'],r['message_id']))
    by_id = {row['message_id']:row for row in rows}
    requests = {row['message_id']:row for row in rows if is_request(row,own_ids)}
    selected = {key:row for key,row in requests.items() if key in selected_ids}
    result = {key:{'message_id':key,'text':row.get('text_excerpt') or '', 'telegram_date':row['telegram_date'],
                   'closes_at':(timestamp(row['telegram_date'])+timedelta(seconds=WINDOW_SECONDS)).isoformat(),
                   'open':now < timestamp(row['telegram_date'])+timedelta(seconds=WINDOW_SECONDS),
                   'offers':[]} for key,row in selected.items()}
    unresolved = []; active = deque()
    for row in rows:
        at = timestamp(row['telegram_date']); message_id=row['message_id']
        while active and (at-timestamp(active[0]['telegram_date'])).total_seconds() > WINDOW_SECONDS:
            active.popleft()
        if message_id in requests:
            active.append(row)
            continue
        if str(row.get('sender_id')) in own_ids:
            continue
        text = row.get('text_excerpt') or ''
        values = prices(text,has_model=bool(row.get('mentions')))
        # Find an explicit reply ancestor, never substitute another request
        # when Telegram points to a different conversation or missing message.
        target = None; method = ''; parent=row.get('reply_to_message_id'); visited={message_id}
        while parent and parent not in visited and len(visited)<12 and not row.get('reply_external'):
            visited.add(parent)
            if parent in requests:
                target=requests[parent]; method='reply' if len(visited)==2 else 'reply_chain'; break
            ancestor=by_id.get(parent)
            if not ancestor or ancestor.get('reply_external'):
                break
            parent=ancestor.get('reply_to_message_id')
        relevant=[q for q in active if q['message_id'] in selected]
        if target and target['message_id'] not in selected:
            continue
        if not target and not relevant:
            continue
        if not values and not target:
            continue
        reason=''
        if not target:
            if row.get('reply_metadata_loaded') != 1:
                reason='metadata_missing'
            elif row.get('reply_external') or row.get('reply_to_message_id'):
                reason='reply_not_found'
            else:
                candidates=[q for q in active if same_model(q,row)] if row.get('mentions') or len(tokens(text))>=2 else list(active)
                # No inference while historical reply metadata is incomplete.
                if any(q.get('reply_metadata_loaded') != 1 for q in active):
                    reason='metadata_missing'
                elif len(candidates)==1 and candidates[0]['message_id'] in selected:
                    target=candidates[0]; method='model' if same_model(target,row) else 'single_request'
                else:
                    reason='ambiguous' if candidates else 'model_not_found'
        if target:
            elapsed=(at-timestamp(target['telegram_date'])).total_seconds()
            effective=timestamp(row.get('edited_at') or row['telegram_date'])
            if elapsed<0 or elapsed>WINDOW_SECONDS or (effective-timestamp(target['telegram_date'])).total_seconds()>WINDOW_SECONDS:
                reason='late'
            elif target.get('edited_at') and timestamp(target['edited_at'])>at:
                reason='request_edited'
            elif not compatible(target,row):
                reason='variant_mismatch'
        items = values or [{'minor':None,'currency':'','raw':''}]
        for index,value in enumerate(items):
            quote={**value, 'message_id':message_id,'index':index,'supplier_id':str(row.get('sender_id') or ''),
                   'supplier_name':row.get('sender_name') or row.get('sender_username') or f"ID {row.get('sender_id') or 'неизвестен'}",
                   'supplier_username':row.get('sender_username') or '', 'text':text,
                   'telegram_date':row['telegram_date'],'edited_at':row.get('edited_at'),
                   'method':method,'reason':reason,'superseded':False,'highlight':'',
                   'request_id':target['message_id'] if target else None}
            parts=qualifiers(row)
            quote['variant_key']=repr((sorted(parts[0]),sorted(parts[1]),*parts[2:]))
            if target:
                quote['seconds_after']=int((at-timestamp(target['telegram_date'])).total_seconds())
                # Multiple models / amounts or unspecified variant alternatives
                # are shown verbatim, but do not pretend to be one comparable SKU.
                qtext=target.get('text_excerpt','')
                unsafe=(len(target.get('mentions',[]))>1 or len(items)>1
                        or len([line for line in qtext.splitlines() if tokens(line)])>1
                        or bool(re.search(r'\b\d+\s*/\s*\d+\b',qtext))
                        or (not qualifiers(target)[0] and bool(qualifiers(row)[0]))
                        or (not qualifiers(target)[1] and bool(qualifiers(row)[1]))
                        or bool(qualifiers(row)[3]))
                quote['comparable']=not reason and not unsafe and value['minor'] is not None and value['currency']!='UNKNOWN'
                result[target['message_id']]['offers'].append(quote)
            else:
                quote['candidate_ids']=[q['message_id'] for q in relevant]
                quote['comparable']=False
                unresolved.append(quote)
    for request in result.values():
        latest={}
        for offer in request['offers']:
            if offer['minor'] is not None and not offer['reason']:
                key=(offer['supplier_id'],offer['currency'],offer['variant_key'])
                if key in latest and latest[key]['message_id'] != offer['message_id']:
                    latest[key]['superseded']=True
                latest[key]=offer
        buckets=defaultdict(list)
        for offer in request['offers']:
            if offer['comparable'] and not offer['superseded']:
                buckets[offer['currency']].append(offer)
        for offers in buckets.values():
            if len({o['supplier_id'] for o in offers})<2:
                continue
            low=min(o['minor'] for o in offers); high=max(o['minor'] for o in offers)
            if low==high:
                continue
            for offer in offers:
                offer['highlight']='min' if offer['minor']==low else 'max' if offer['minor']==high else ''
    return list(result.values()), unresolved
