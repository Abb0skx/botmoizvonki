"""Deterministic templates and approved catalog lookup. No AI or send client."""
import re
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, Field
from telegram_business.products import extract_product_query, normalize_model
from .sources import SheetSource


class Classification(BaseModel):
    action: Literal['reply', 'ignore', 'manager_only']
    category: str
    requires_reply: bool
    confidence: float = Field(ge=0, le=1)
    reason: str
    reply_text: str = ''
    query: str = ''
    source: str = ''


DEFAULT_TEXTS = {
    'telegram_url': 'https://t.me/texnikach',
    'manager_url': 'https://t.me/texnikach_admin',
    'greeting_ru': 'Здравствуйте! Напишите название модели — подскажем цену.',
    'greeting_uz': 'Assalomu alaykum! Model nomini yozing — narxini aytamiz.',
    'greeting_bilingual': 'Здравствуйте! Напишите название модели — подскажем цену.\n\nAssalomu alaykum! Model nomini yozing — narxini aytamiz.',
    'credit_reply': 'К сожалению, у нас нет кредита и рассрочки. Только полная оплата.\n\nBizda kredit va bo‘lib to‘lash yo‘q. Faqat to‘liq to‘lov.',
    'credit_keywords': 'кредит, кредита, рассрочка, рассрочку, частями, по месяцам, kredt, kredit, kreditga, nasiya, bolip tolash, bo‘lib to‘lash, alif, uzum nasiya, variant',
    'manager_only_keywords': 'жалоба, претензия, сломался, сломалось, не работает, возврат, вернуть деньги, shikoyat, ishlamayapti, pulni qaytarish',
    'model_not_found_reply': 'Напишите полное название модели, память и цвет.\nModelning to‘liq nomi, xotirasi va rangini yozing.\n{manager_url}',
    'model_variant_not_found_reply': 'Такого сочетания памяти и цвета нет в текущем прайсе. Уточним у менеджера:\n{manager_url}\n\nBu xotira va rang varianti hozirgi narxnomada yo‘q. Menejerdan aniqlashtiramiz:\n{manager_url}',
    'model_intro': 'TEXNIKACH',
    'model_prices_label': 'Актуальные цены / Aktual narxlar:',
    'model_other_variants': 'Другие варианты уточните у менеджера.',
    'model_empty_memory_label': 'Цена',
    'model_warranty_label': 'гарантия / kafolat: {months} мес. / oy',
    'model_footer': 'Для оформления заказа / Buyurtma uchun:\n{manager_url}',
}


def language(text):
    value = text.casefold()
    if re.search(r'\b(narx\w*|qancha\w*|salom|rahmat|bormi|nasiya|kerak|mavjud\w*|assalomu|alaykum|buyurtma)\b|нарх|қанча|борми|насия|тўлаш|салом|рахмат|керак', value):
        return 'uz'
    if re.search(r'\b(здравствуй\w*|привет|добрый|цена|цену|цены|сколько|стоит|есть|нужен|нужна|купить|заказать|кредит|рассроч\w*)\b', value):
        return 'ru'
    return 'bilingual'


def normalized(text):
    return re.sub(r'\s+', ' ', re.sub(r"[^\w']+", ' ', str(text).casefold()
        .replace('ё', 'е').replace('’', "'").replace('‘', "'").replace('ʻ', "'"))).strip()


def contains(text, keyword):
    key = normalized(keyword)
    return bool(key and re.search(r'(?<!\w)' + re.escape(key).replace(r'\ ', r'\s+') + r'(?!\w)', normalized(text)))


def keywords_match(text, words):
    return any(contains(text, word) for word in re.split(r'[,;\n]+', words))


def resolve_rule(text, rules):
    default = None
    for rule in rules:
        kind = rule['match_type']
        if kind == 'default':
            default = default or rule
            continue
        matches = [contains(text, word) for word in rule['keywords']]
        if ((kind == 'contains_any' and any(matches)) or (kind == 'contains_all' and all(matches))
                or (kind == 'exact' and normalized(text) in [normalized(w) for w in rule['keywords']])):
            return rule
    return default


def render(text, settings):
    for _ in range(3):
        updated = re.sub(r'\{([a-z_]+)\}', lambda m: str(settings.get(m[1], m[0])), text)
        if text == updated:
            break
        text = updated
    if re.search(r'\{[a-z_]+\}', text):
        raise ValueError('Unknown template placeholder')
    return text.strip()


def decision(action, category, reason, text='', query='', source=''):
    if len(text) > 1000:
        raise ValueError('Template exceeds Instagram message limit')
    return Classification(action=action, category=category, requires_reply=action == 'reply',
        confidence=1, reason=reason, reply_text=text, query=query, source=source)


def rule_classification(batch):
    text = '\n'.join(m.text for m in batch if m.direction == 'incoming').strip()
    if not text or not re.search(r'[\w?？]', text) or all(m.message_type in ('reaction', 'deleted') for m in batch):
        return decision('ignore', 'media_only', 'Ответ не требуется — контент или реакция без вопроса.')
    if re.fullmatch(r'\s*(ок[,. ]*|ok[,. ]*)?(спасибо|рахмат|раҳмат|rahmat|raxmat|thanks)[\s.!🙏]*', text, re.I):
        return decision('ignore', 'thanks', 'Ответ не требуется — благодарность.')
    if keywords_match(text, DEFAULT_TEXTS['credit_keywords']) or re.search(r'рассроч|бўлиб|bo.lib|bolip|месяцам', text, re.I):
        return decision('reply', 'credit_or_installment', 'Вопрос о кредите или рассрочке.')
    if re.fullmatch(r'\s*(здравствуйте|привет|добрый день|salom|assalomu alaykum|салом)[\s.!]*', text, re.I):
        return decision('reply', 'greeting', 'Приветствие.')
    return None


def price_text(match, settings):
    grouped = {}
    warranties = {v.warranty_months for v in match.variants}
    for variant in match.variants:
        quantum = Decimal('1000') if variant.price_uzs > 10000 else Decimal('1')
        amount = (variant.price_uzs / quantum).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * quantum
        grouped.setdefault((variant.model, variant.memory, amount, variant.warranty_months), set()).add(variant.color)
    lines = []
    for (model, memory, amount, warranty), colors in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        color = ', '.join(sorted(c for c in colors if c))
        label = ' '.join(v for v in (memory, color) if v) or render(settings['model_empty_memory_label'], settings)
        if len({v.model for v in match.variants}) > 1:
            label = model + ' ' + label
        if len(warranties) > 1 and warranty is not None:
            label += ' (' + render(settings['model_warranty_label'].replace('{months}', str(warranty)), settings) + ')'
        formatted_amount = f'{int(amount):,}'.replace(',', ' ')
        lines.append(f'• {label} — {formatted_amount} сум')
    heading = '\n\n'.join(v for v in (render(settings['model_intro'], settings), ', '.join(match.models),
                                      render(settings['model_prices_label'], settings)) if v)
    footer = render(settings['model_footer'], settings)
    omitted = render(settings['model_other_variants'], settings)
    selected = []
    for line in lines:
        candidate = '\n'.join([heading, *selected, line, '', omitted, '', footer])
        if len(candidate) > 950:
            break
        selected.append(line)
    if not selected:
        raise ValueError('Product template too long for prices')
    return '\n'.join([heading, *selected, *(['', omitted] if len(selected) < len(lines) else []), '', footer]).strip()


class TemplateResponder:
    def __init__(self, settings, source=None):
        self.settings = settings
        self.source = source or SheetSource(settings)

    async def classify(self, batch, history, force=False):
        simple = rule_classification(batch)
        if simple and simple.action == 'ignore':
            return simple
        text = '\n'.join(m.text for m in batch if m.direction == 'incoming').strip()
        config = await self.source.get('config', force=force)
        settings = {**DEFAULT_TEXTS, **config['settings']}
        if keywords_match(text, settings['manager_only_keywords']):
            return decision('manager_only', 'after_sale', 'Нужен менеджер — вопрос после покупки или претензия.')
        rule = resolve_rule(text, config['rules'])
        # Credit is a deliberate exception to model-first matching.
        if keywords_match(text, settings['credit_keywords']):
            credit_rules = [r for r in config['rules'] if r['match_type'] != 'default' and
                            any(keywords_match(word, settings['credit_keywords']) for word in r['keywords'])]
            credit_rule = resolve_rule(text, credit_rules)
            reply = credit_rule['reply_text'] if credit_rule else settings['credit_reply']
            return decision('reply', 'credit_or_installment', 'Отказ по кредиту/рассрочке.', render(reply, settings), source='direct_rules')
        if simple and simple.category == 'greeting':
            return decision('reply', 'greeting', 'Приветствие.', render(settings['greeting_' + language(text)], settings), source='direct_settings')
        try:
            catalog, _ = await self.source.get('catalog', force=force)
        except Exception:
            return decision('manager_only', 'unclear', 'Прайс недоступен. Нужен ответ менеджера; старые цены не используем.')
        match = catalog.search(text)
        query = text
        # Only short, explicit follow-ups reuse a recent customer's model; never old outgoing prices.
        if match.status == 'not_found' and is_followup(text):
            batch_ids = {getattr(m, 'id', None) for m in batch}
            for message in reversed(history[-10:]):
                if (message.direction != 'incoming' or getattr(message, 'id', None) in batch_ids
                        or time.time() - getattr(message, 'created_at', 0) > 7200):
                    continue
                previous = catalog.search(message.text)
                if previous.status == 'found':
                    query = previous.models[0] + ' ' + text
                    match = catalog.search(query)
                    break
        if match.status == 'found':
            if not match.filters_matched:
                return decision('reply', 'product_question', 'Запрошенного варианта нет в прайсе.',
                    render(settings['model_variant_not_found_reply'], settings), query, 'direct_settings')
            return decision('reply', 'price_question', 'Модель найдена в прайсе; цены в сумах.',
                price_text(match, settings), query, 'bot_prices')
        if match.status == 'ambiguous' or looks_like_product(text):
            return decision('reply', 'product_question', 'Нужно уточнить модель.',
                render(settings['model_not_found_reply'], settings), text, 'direct_settings')
        return decision('reply', 'unclear', 'Готовый ответ по ключевым словам.' if rule['match_type'] != 'default' else 'Стандартный ответ.',
            render(rule['reply_text'], settings), source='direct_rules')

    async def propose(self, classification, batch, history, previous=''):
        if previous:
            classification = await self.classify(batch, history, force=True)
        return classification.reply_text or None

    async def close(self):
        await self.source.close()


def is_followup(text):
    query, memory, color = extract_product_query(text)
    rest = normalize_model(query).split()
    return len(text) < 80 and (bool(memory or color) or not rest or
        set(rest).issubset({'а', 'такой', 'такое', 'этот', 'эта', 'его', 'у', 'вас'}))


def looks_like_product(text):
    return bool(re.search(r'\b(?:iphone|айфон|samsung|самсунг|redmi|poco|xiaomi|honor|ipad|macbook|airpods|dyson)\b|\b[a-z]\d{1,3}\b', text, re.I))
