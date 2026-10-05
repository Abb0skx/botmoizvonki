"""Conservative detection of our own product enquiries in supplier DMs."""
import re

from telegram_business.products import normalize_model
from .quotes import CHATTER, prices, tokens

NON_PRODUCT = re.compile(r'\b(?:карт[аыуе]|card|karta|долг|деньг\w*|оплат\w*|перевод\w*|счет|счёт|счета|валют\w*|доллар\w*|pul|qarz|hisob|to[‘’\x27]?lov|заказ\s*№)\b', re.I)
PRODUCT_HINT = re.compile(r'\b(?:[a-z]{1,12}\d{1,4}[a-z0-9]*|iphone|айфон|samsung|самсунг|xiaomi|redmi|poco|honor|huawei|apple|watch|airpods|ipad|macbook|whoop|fitbit|чехол|case|chexol|chehol|glass|кабель|cable|зарядк\w*|ремешок|strap|наушник\w*)\b', re.I)
FULFILMENT = re.compile(r'\b(?:не\s+нуж\w*|уже\s+есть|получил\w*|забрал\w*|доставил\w*|оплач\w*|продан\w*|вернул\w*|возврат\w*|брак\w*|сломал\w*|заказ\s+(?:готов|отмен)|olib\s+keldim|oldim|sotildi|qaytar\w*|kerak\s+emas)\b', re.I)
UNKNOWN_LOGISTICS = re.compile(r'\b(?:курьер\w*|доставк\w*|человек\w*|сотрудник\w*|водител\w*|завтра|сегодня|минут\w*|час(?:а|ов)?|kuryer\w*|dostavka\w*|haydovchi\w*|odam\w*|minut\w*|soat\w*|ertaga|bugun)\b', re.I)


def is_direct_request(text, analysis, *, outgoing, forwarded=False):
    """An incoming offer/forward never becomes a new demand row.

    A recognised model alone is enough. An unknown model needs an explicit
    demand phrase and a product-like identifier; general supplier conversation
    is not copied into the market list.
    """
    if (not outgoing or forwarded or not text.strip() or CHATTER.fullmatch(text.strip())
            or analysis.intent == 'supply' or NON_PRODUCT.search(text) or FULFILMENT.search(text)):
        return False
    amounts = prices(text, has_model=bool(analysis.mentions))
    if amounts:
        # A model ending in a generation number (Watch Series 10) is not $10.
        exact_model = any(normalize_model(text) == normalize_model(m.model_name) for m in analysis.mentions)
        if not exact_model or any(p.get('currency_source') != 'amount_rule' for p in amounts):
            return False
    product_words = tokens(text)
    return bool(analysis.mentions or (analysis.intent == 'demand' and PRODUCT_HINT.search(text)
                                     and not UNKNOWN_LOGISTICS.search(text)
                                     and any(re.search(r'[a-zа-я]', word, re.I) for word in product_words)))
