from __future__ import annotations

import re
from urllib.parse import quote_plus
from urllib.parse import urlparse

CREDIT = (
    "кредит", "рассроч", "в рассрочку", "оплата частями", "платить частями",
    "ежемесячно", "в месяц", "без первоначального взноса", "kredit", "nasiya", "nasiyaga", "bo'lib to'lash",
    "bolib tolash", "muddatli to'lov", "oyma-oy", "boshlang'ich to'lovsiz", "насия", "бўлиб тўлаш",
    "муддатли тўлов", "ойма-ой", "бошланғич тўловсиз",
)
CREDIT_NEGATIVE = (
    "кредитная карта", "кредит не нужен", "без кредита", "оплачу полностью", "оплата полностью",
    "кредитной карт", "кредитную карт", "кредитка",
    "kredit kerak emas", "kreditsiz", "to'liq to'layman", "кредит керак эмас",
    "кредитсиз", "кредит олмайман", "тўлиқ тўлайман", "тўлиқ тўлов",
)
ORDER = (
    "беру", "оформите", "заказываю", "отправьте", "доставьте",
    "olaman", "buyurtma", "yuboring", "yetkazib bering",
    "оламан", "буюртма", "юборинг", "етказиб беринг", "расмийлаштиринг", "жўнатинг",
)
ORDER_NEGATIVE = (
    "не беру", "не оформляйте", "не заказываю", "не отправляйте", "не отправьте",
    "не доставляйте", "не доставьте", "не надо доставлять", "пока не доставляйте",
    "olmayman", "buyurtma qilmayman", "yubormang", "yetkazib bermang",
    "олмайман", "буюртма қилмайман", "юборманг", "етказиб берманг",
)
PRODUCT_SEARCH = ("цена", "цены", "сколько стоит", "модель", "narx", "narxi", "qancha", "нарх", "нархи", "қанча")
CATALOG = ("каталог", "сайт", "katalog", "sayt", "havola")
HANDOFF = {
    "warranty": ("гарант", "kafolat", "кафолат"),
    "return": ("возврат", "вернуть деньги", "qaytar", "қайтар", "пулни қайтар"),
    "complaint": ("жалоб", "брак", "конфликт", "shikoyat", "шикоят", "нуқсон", "бузил", "низо"),
    "human_request": (
        "менеджер", "человек", "operator", "menejer", "менежер", "оператор",
        "одам билан", "инсон билан",
    ),
    "discount": ("скидк", "торг", "последняя цена", "chegirma", "чегирма", "охирги нарх", "савдолаш"),
    "delivery": ("доставк", "доставит", "привез", "yetkazib berish", "етказиб бериш"),
    "pickup": ("самовывоз", "можно забрать", "olib ketish", "олиб кетиш"),
    "availability": ("наличие", "в наличии", "mavjudligi", "mavjudmi", "мавжуд", "мавжудми", "борми"),
    "payment": (
        "оплата картой", "перевод на карту", "реквизит", "karta orqali", "to'lov ma'lumot",
        "карта орқали", "тўлов маълумот", "payment_data_redacted",
    ),
    "active_order": (
        "мой заказ", "где заказ", "статус заказа", "номер заказа", "заказ уже",
        "заказ оформлен", "заказ оформлял", "заказ оформляла", "изменить заказ",
        "измените адрес доставки в заказе", "изменить адрес доставки в заказе",
        "отмените заказ", "отменить заказ", "отмена заказа", "курьер уже едет",
        "курьер едет", "курьер в пути",
        "buyurtmam", "buyurtma qayerda", "buyurtma holati", "buyurtma berilgan",
        "buyurtma manzilini o'zgartir", "buyurtmadagi yetkazib berish manzilini o'zgartir",
        "kecha buyurtma berganman", "buyurtmani kecha rasmiylashtirganman",
        "buyurtmani bekor qil", "kuryer yo'lda", "kuryer kelyapti",
        "буюртмам", "буюртма қаерда", "буюртма ҳолати", "буюртма берилган",
        "буюртма манзилини ўзгартир", "буюртмадаги етказиб бериш манзилини ўзгартир",
        "кеча буюртма берганман", "буюртмани кеча расмийлаштирганман",
        "буюртмани бекор қил", "курьер йўлда", "курьер келяпти",
    ),
    "technical": (
        "не работает", "как настроить", "технический вопрос", "ishlamayapti", "qanday sozlash",
        "ишламаяпти", "қандай созлаш", "характеристик", "какая камера", "какой экран",
        "сколько герц", "поддерживает ли", "совместим", "что лучше", "какая модель лучше",
        "xususiyat", "qanday kamera", "qanday ekran", "necha gerts", "qo'llab-quvvatlaydimi",
        "mos keladimi", "qaysi biri yaxshi", "хусусият", "қандай камера", "қандай экран",
        "неча герц", "қўллаб-қувватлайдими", "мос келадими", "қайси бири яхши",
    ),
}

_GOOGLE_MAP_HOSTS = frozenset(
    {
        "google.com", "www.google.com", "maps.google.com",
        "google.uz", "www.google.uz", "google.co.uz", "www.google.co.uz",
        "google.ru", "www.google.ru", "maps.app.goo.gl", "goo.gl",
    }
)
_YANDEX_MAP_HOSTS = frozenset(
    {
        "yandex.com", "www.yandex.com", "yandex.uz", "www.yandex.uz",
        "yandex.ru", "www.yandex.ru", "yandex.kz", "www.yandex.kz",
        "yandex.by", "www.yandex.by",
    }
)
_MAP_HOST_PATTERN = "|".join(
    re.escape(host)
    for host in sorted((*_GOOGLE_MAP_HOSTS, *_YANDEX_MAP_HOSTS), key=len, reverse=True)
)
# This expression is also used by service._product_text to remove only known
# map links. It deliberately cannot consume lookalike domains such as
# google.evil or yandex.example.
MAP_URL_RE = re.compile(
    rf"https?://(?:{_MAP_HOST_PATTERN})(?::\d+)?(?:/[^\s<>]*)?",
    re.IGNORECASE,
)
COORD_RE = re.compile(r"(?<!\d)(-?\d{1,2}(?:\.\d{3,}))\s*[,; ]\s*(-?\d{1,3}(?:\.\d{3,}))(?!\d)")
ADDRESS_MARKERS_RE = re.compile(
    r"\b(?:адрес|локаци(?:я|ю|и)|ул(?:ица|ца|\.)?|дом|квартал|махалля|махалла|"
    r"район|массив|проспект|переулок|ko(?:'|\u2019)?cha|uy|mahalla|tuman|mavze|"
    r"manzil|манзил|кўча|уй|маҳалла|туман|мавзе)\b",
    re.IGNORECASE,
)
EXPLICIT_LOCATION_MARKER_RE = re.compile(
    r"\b(?:адрес|локаци(?:я|ю|и)|manzil|манзил)\b",
    re.IGNORECASE,
)
_CLOCK = r"(?:[01]?\d|2[0-3])[:.]\d{2}"
_HOUR = r"(?:[01]?\d|2[0-3])(?::\d{2})?"
_DAY = r"(?:завтра|сегодня|ertaga|bugun)"
_DAY_PART = r"(?:утром|днем|вечером|ertalab|kunduzi|kechqurun)"
TIME_RE = re.compile(
    rf"\b{_DAY}(?:\s+(?:(?:в|soat)\s*{_CLOCK}|(?:после|до|с|from|after)\s+{_HOUR}))?\b|"
    rf"\b(?:в|kuni|soat)?\s*{_CLOCK}\b|"
    rf"\b(?:после|до|с|from|after|soat)\s+{_HOUR}\b|"
    rf"\b{_DAY_PART}\b",
    re.IGNORECASE,
)

# We only make an automatic delivery promise for Tashkent.  This list is
# intentionally conservative: an explicit, well-known city/region outside the
# delivery city is enough to hand the question to a manager, while an unclear
# free-form address remains unclassified instead of being guessed.
OUTSIDE_TASHKENT_RE = re.compile(
    r"\b(?:самарканд(?:ская)?|бухара|бухоро|андижан|андижон|"
    r"фергана|фарғона|наманган|намангон|нукус|нукус|хива|"
    r"хива|джизак|жиззах|карши|қарши|термез|термиз|навои|"
    r"navoiy|samarqand|samarkand|buxoro|bukhara|andijon|andijan|farg(?:'|’)?ona|"
    r"fergana|namangan|nukus|xiva|khiva|jizzax|jizzakh|qarshi|karshi|termiz|termez|"
    r"чирчик|chirchiq|chirchik|ургенч|урганч|urganch|urgench|"
    r"коканд|қўқон|qo(?:'|’)?qon|qoqon|kokand|гулистан|guliston|gulistan|"
    r"ангрен|angren|алмалык|олмалиқ|olmaliq|бекабад|бекобод|bekabad|bekobod|"
    r"янгиюль|янгийўл|yangiyo(?:'|’)?l|yangiyul|шахрисабз|shahrisabz)\b",
    re.IGNORECASE,
)

# A deliberately broad Tashkent-area box avoids misclassifying nearby suburbs
# as inter-city delivery.  It is only a routing hint; it is never used to
# confirm delivery or availability.
TASHKENT_AREA_BOUNDS = (40.85, 41.65, 68.75, 69.80)


def normalize(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").replace("’", "'").replace("‘", "'").replace("ʻ", "'").split())


def _safe_map_url(value: str) -> str | None:
    candidate = str(value or "").rstrip(".,);]")[:1000]
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    path = parsed.path.casefold()
    if hostname in _GOOGLE_MAP_HOSTS:
        if hostname == "maps.app.goo.gl":
            return candidate if path not in {"", "/"} else None
        if hostname == "goo.gl":
            return candidate if path.startswith("/maps/") else None
        return candidate if path == "/maps" or path.startswith("/maps/") else None
    if hostname in _YANDEX_MAP_HOSTS:
        return candidate if path == "/maps" or path.startswith("/maps/") else None
    return None


def extract_text_location(text: str) -> str | None:
    """Return a safe map URL without ever opening a client supplied link."""
    value = str(text or "").strip()
    for match in MAP_URL_RE.finditer(value):
        if safe_url := _safe_map_url(match.group(0)):
            return safe_url
    if match := COORD_RE.search(value):
        latitude, longitude = float(match.group(1)), float(match.group(2))
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return f"https://www.google.com/maps?q={latitude:.6f},{longitude:.6f}"
    # A free-form address is intentionally recognized conservatively to avoid
    # treating product names containing a number as personal location data.
    if ADDRESS_MARKERS_RE.search(value) and (
        EXPLICIT_LOCATION_MARKER_RE.search(value)
        or re.search(r"\d", value)
        or len(ADDRESS_MARKERS_RE.findall(value)) >= 2
    ):
        compact = " ".join(value.split())[:500]
        return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(compact)
    return None


def extract_preferred_time(text: str) -> str | None:
    if match := TIME_RE.search(str(text or "")):
        return " ".join(match.group(0).split())[:100]
    return None


def remove_preferred_time(text: str) -> str:
    """Remove the same time phrase that is persisted as a customer preference."""
    return " ".join(TIME_RE.sub(" ", str(text or "")).split())


def is_outside_tashkent(
    text: str = "",
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> bool:
    """Conservatively identify a clearly non-Tashkent delivery location."""
    if OUTSIDE_TASHKENT_RE.search(str(text or "")):
        return True
    if latitude is None or longitude is None:
        match = COORD_RE.search(str(text or ""))
        if not match:
            return False
        latitude, longitude = float(match.group(1)), float(match.group(2))
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return False
    south, north, west, east = TASHKENT_AREA_BOUNDS
    return not (south <= latitude <= north and west <= longitude <= east)


def classify(text: str, media_only: bool = False, has_location: bool = False) -> list[str]:
    value = normalize(text)
    result: list[str] = []
    if media_only:
        result.append("media_only")
    credit_match = any(
        (
            re.search(r"(?<!\w)кредит(?:а|у|ом|е)?(?!\w)", value)
            if term == "кредит"
            else term in value
        )
        for term in CREDIT
    )
    if credit_match and not any(term in value for term in CREDIT_NEGATIVE):
        result.append("credit")
    order_negative = any(term in value for term in ORDER_NEGATIVE)
    if any(term in value for term in ORDER) and not order_negative:
        result.append("order_request")
    if any(term in value for term in PRODUCT_SEARCH):
        result.append("product_search")
    if any(term in value for term in CATALOG):
        result.append("catalog")
    for code, terms in HANDOFF.items():
        if code == "delivery" and order_negative:
            continue
        if any(term in value for term in terms):
            result.append(code)
    # Uzbek uses ``buyurtma`` for both a new-order request and an existing
    # order.  Once the message clearly refers to an active order, it must be
    # handed to a manager and must not inflate new-order intent statistics.
    if "active_order" in result:
        result = [code for code in result if code != "order_request"]
    if has_location or extract_text_location(text):
        result.append("location")
    return result
