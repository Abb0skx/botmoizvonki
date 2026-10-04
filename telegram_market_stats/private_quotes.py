"""Pure matching of supplier DMs to OUR requests across all market groups.

No sending, AI, currency conversion or calculated relative discounts. Price
inferences never become reference prices; ambiguous offers stay unassigned.
"""
import hashlib
import json
import re
import statistics
from datetime import timedelta

from .quotes import prices, timestamp, same_model, compatible, qualifiers, tokens, request_identity, distinct_requests


def key(row):
    return f"{row['group_id']}:{row['message_id']}"


def fingerprint(row):
    fields = ('text_excerpt', 'telegram_date', 'edited_at', 'reply_to_message_id',
              'forward_from_id', 'forward_date', 'forward_group_id', 'forward_message_id')
    return hashlib.sha256(json.dumps({f: row.get(f) for f in fields}, sort_keys=True).encode()).hexdigest()


def normalized(text):
    return ' '.join((text or '').casefold().split())


def price_target(candidates, value, anchors, at, near=.15, far=.40, originals=()):
    """Distances use the same currency/notation. Unknown units remain unknown."""
    if value.get('minor') is None:
        return None, ''
    distances = []
    for request in candidates:
        family = [q for q in originals if request_identity(q) == request_identity(request)] or [request]
        samples = [a for q in family for a in anchors.get(key(q), []) if a[0] == value['currency'] and a[2] <= at]
        if samples:
            median = statistics.median(a[1] for a in samples)
            distances.append((abs(value['minor'] - median) / median, request, min(a[2] for a in samples)))
    distances.sort(key=lambda item: item[0])
    if distances and distances[0][0] <= near and (len(distances) == 1 or distances[1][0] - distances[0][0] >= near):
        return distances[0][1], 'price_near'
    # A large difference is only a hypothesis, never a confirmed price for #2.
    if len(candidates) == 2 and len(distances) == 1 and distances[0][0] >= far:
        other = next(q for q in candidates if key(q) != key(distances[0][1]))
        family = [q for q in originals if request_identity(q) == request_identity(other)] or [other]
        if min(timestamp(q['telegram_date']) for q in family) > distances[0][2]:
            return other, 'price_far'
    return None, ''


def rank_offers(offers):
    latest = {}
    for offer in sorted(offers, key=lambda o: (o['telegram_date'], o['message_id'])):
        offer['highlight'] = ''
        offer['superseded'] = False
        if offer.get('minor') is not None and not offer.get('reason') and not offer.get('inferred'):
            bucket = (offer['supplier_id'], offer['currency'], offer.get('variant_key', ''))
            old = latest.get(bucket)
            if old and (old['message_id'], old.get('source')) != (offer['message_id'], offer.get('source')):
                old['superseded'] = True
            latest[bucket] = offer
    currencies = {o['currency'] for o in offers if o.get('comparable')}
    for currency in currencies:
        valid = [o for o in offers if o.get('comparable') and not o.get('superseded') and o['currency'] == currency]
        if len({o['supplier_id'] for o in valid}) < 2:
            continue
        low, high = min(o['minor'] for o in valid), max(o['minor'] for o in valid)
        if low != high:
            for offer in valid:
                offer['highlight'] = 'min' if offer['minor'] == low else 'max' if offer['minor'] == high else ''


def match_private(requests, messages, links=(), *, near=.15, far=.40):
    requests = sorted(requests, key=lambda q: (q['telegram_date'], key(q)))
    by_key = {key(q): q for q in requests}
    manual = {(str(r['chat_id']), int(r['message_id'])): r for r in links}
    anchors, context, reply_context = {}, {}, {}
    linked, unresolved = {key(q): [] for q in requests}, []
    for message in sorted(messages, key=lambda m: (m['telegram_date'], m['message_id'], str(m['chat_id']))):
        chat, mid = str(message['chat_id']), message['message_id']
        at = timestamp(message['telegram_date'])
        active = [q for q in requests if 0 <= (at - timestamp(q['telegram_date'])).total_seconds() <= 420]
        if not active:
            continue
        text = message.get('text_excerpt') or ''
        values = prices(text, has_model=bool(message.get('mentions')))
        target, method, reason = None, '', ''
        if message.get('forwarded'):
            exact = [q for q in active if (
                message.get('forward_group_id') == q['group_id'] and message.get('forward_message_id') == q['message_id']
            ) or (
                normalized(text) == normalized(q.get('text_excerpt'))
                and (not message.get('forward_from_id') or str(message['forward_from_id']) == str(q.get('sender_id')))
                and (not message.get('forward_date') or timestamp(message['forward_date']) == timestamp(q['telegram_date']))
            )]
            # A concrete forwarded post outranks a same-text repost. Only when
            # Telegram hides the origin can identical active posts act as one.
            if message.get('forward_group_id') and message.get('forward_message_id'):
                exact = [q for q in exact if message['forward_group_id'] == q['group_id'] and message['forward_message_id'] == q['message_id']]
            exact = distinct_requests(exact)
            context.pop(chat, None)
            if len(exact) == 1:
                context[chat] = key(exact[0])
                reply_context[(chat, mid)] = key(exact[0])
            # A forwarded request is context, not a supplier's quote.
            continue
        explicit = [q for q in active if same_model(q, message)]
        distinct_explicit = distinct_requests(explicit)
        has_model = bool(message.get('mentions'))
        if message.get('reply_to_message_id'):
            parent = reply_context.get((chat, message['reply_to_message_id']))
            target = by_key.get(parent)
            if target and target in active and compatible(target, message):
                method = 'private_reply'
            else:
                target, reason = None, 'reply_not_found'
        elif len(distinct_explicit) == 1:
            target, method = distinct_explicit[0], 'private_model'
            context[chat] = key(target)
        elif has_model:
            context.pop(chat, None)
            reason = 'ambiguous' if explicit else 'model_not_found'
        elif context.get(chat):
            possible = by_key.get(context[chat])
            if possible in active and compatible(possible, message):
                target, method = possible, 'forward_context'
            else:
                context.pop(chat, None)
        if message.get('outgoing'):
            if target:
                reply_context[(chat, mid)] = key(target)
            continue
        if not values:
            if target:
                reply_context[(chat, mid)] = key(target)
            continue
        # A forward/model introduction anchors the next quote, not every later
        # bare price in this chat. Subsequent quotes must qualify independently
        # (or explicitly reply to the anchored message).
        context.pop(chat, None)
        saved = manual.get((chat, mid))
        if saved:
            choice = by_key.get(saved['request_key'])
            if (choice in active and saved['message_fingerprint'] == fingerprint(message)
                    and saved['request_fingerprint'] == fingerprint(choice)):
                target, method, reason = choice, 'manual', ''
            else:
                target, method, reason = None, '', 'manual_stale'
        if not target and not reason:
            originals = [q for q in active if compatible(q, message)]
            candidates = distinct_requests(originals)
            if len(candidates) == 1:
                target, method = candidates[0], 'private_time'
            elif len(values) == 1:
                target, method = price_target(candidates, values[0], anchors, at, near, far, originals)
            if not target:
                reason = 'ambiguous'
        if target:
            if message.get('edited_at') and timestamp(message['edited_at']) > timestamp(target['telegram_date']) + timedelta(seconds=420):
                reason = 'late'
            elif target.get('edited_at') and timestamp(target['edited_at']) > at:
                reason = 'request_edited'
            if has_model and (len(distinct_explicit) != 1 or not any(key(q) == key(target) for q in explicit)) and method != 'manual':
                reason = 'variant_mismatch'
        options = [{'request_key': key(q), 'request_fingerprint': fingerprint(q), 'text': q.get('text_excerpt', ''), 'telegram_date': q['telegram_date'],
                    'group_id': q['group_id'], 'message_id': q['message_id']} for q in active]
        inferred = method in ('price_near', 'price_far')
        for index, value in enumerate(values):
            offer = {**value, 'source': 'private', 'chat_id': chat, 'message_id': mid, 'index': index,
                     'message_fingerprint': fingerprint(message),
                     'supplier_id': chat, 'supplier_name': message.get('sender_name') or f'ID {chat}',
                     'supplier_username': message.get('sender_username') or '', 'text': text,
                     'telegram_date': message['telegram_date'], 'edited_at': message.get('edited_at'),
                     'request_key': key(target) if target else None, 'request_id': target['message_id'] if target else None,
                     'method': method, 'reason': reason, 'inferred': inferred, 'superseded': False,
                     'highlight': '', 'candidates': options,
                     'variant_key': repr((sorted(qualifiers(message)[0]), sorted(qualifiers(message)[1]), *qualifiers(message)[2:])),
                     'seconds_after': int((at-timestamp(target['telegram_date'])).total_seconds()) if target else None}
            safe_variant = bool(target) and len(target.get('mentions', [])) <= 1 and len(values) == 1
            if target:
                safe_variant = safe_variant and not (
                    (not qualifiers(target)[0] and qualifiers(message)[0]) or
                    (not qualifiers(target)[1] and qualifiers(message)[1]) or
                    qualifiers(message)[3] or
                    re.search(r'\b\d+\s*/\s*\d+\b', target.get('text_excerpt', '')) or
                    len([s for s in target.get('text_excerpt', '').splitlines() if tokens(s)]) > 1)
            offer['comparable'] = bool(target and not reason and not inferred and safe_variant and value['minor'] is not None and value['currency'] != 'UNKNOWN')
            if target:
                linked[key(target)].append(offer)
                if not reason and not inferred and safe_variant:
                    reply_context[(chat, mid)] = key(target)
                    if value['minor'] is not None:
                        anchors.setdefault(key(target), []).append((value['currency'], value['minor'], at))
            else:
                unresolved.append(offer)
    return linked, unresolved
